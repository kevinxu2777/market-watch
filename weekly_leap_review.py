"""Weekly Monday pre-market review for the LEAP options portfolio.

Pipeline:
  1. Refresh market-monitor-agent's data (macro snapshots, watchlist prices,
     news alerts) via `market_watch.py --once`.
  2. Read the SQLite DB it just wrote to build a macro snapshot section and
     decide which LEAP-portfolio tickers need a fresh TradingAgents deep dive
     this week (earnings within +/-7 days, or a stock alert fired on them in
     the last 7 days).
  3. For triggered tickers only, run TradingAgents (Claude Opus/Haiku, output
     in Chinese) via trading-agent/scripts/analyze_ticker.py.
  4. Email a combined, Chinese, HTML-formatted report (macro backdrop +
     per-position status + deep-dive sections for triggered tickers) using
     market_watch's existing SMTP config.

Intended to run every Monday morning via launchd (see
com.kevin.weekly-leap-review.plist). Costs: this script itself is free;
each triggered TradingAgents deep dive costs roughly $1-3 in Claude API
usage, only incurred for tickers that actually earn a trigger this week.
"""

from __future__ import annotations

import html as html_lib
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

MONITOR_DIR = Path(__file__).resolve().parent
TRADING_AGENT_DIR = MONITOR_DIR.parent / "trading-agent"
CONFIG_PATH = MONITOR_DIR / "config.local.json"
DB_PATH = MONITOR_DIR / "market_watch.sqlite3"
DASHBOARD_PATH = MONITOR_DIR / "dashboard.html"
ANALYZE_SCRIPT = TRADING_AGENT_DIR / "scripts" / "analyze_ticker.py"
ANALYZE_PYTHON = TRADING_AGENT_DIR / ".venv" / "bin" / "python"

sys.path.insert(0, str(MONITOR_DIR))
import market_watch  # noqa: E402

# English -> Chinese labels for the fixed set of macro instruments and LEAP
# stock names market_watch.py tracks. Alert titles / earnings strings still
# come through in English (they're free-text from Yahoo/Nasdaq/FMP) but all
# labels this script controls are Chinese.
INSTRUMENT_NAMES_ZH = {
    "10Y Treasury Yield": "10年期美债收益率",
    "Gold": "黄金",
    "Nasdaq 100 Futures": "纳斯达克100期货",
    "S&P 500 Futures": "标普500期货",
    "US Dollar Index": "美元指数",
    "VIX": "VIX 恐慌指数",
    "WTI Crude Oil": "WTI原油",
    "Effective Federal Funds Rate": "联邦基金有效利率",
    "SOFR": "SOFR",
    "Next FOMC Decision": "下次FOMC议息决议",
}
STOCK_NAMES_ZH = {
    "MSFT": "微软",
    "GOOGL": "谷歌/Alphabet",
    "NVDA": "英伟达",
    "TSM": "台积电",
    "ORCL": "甲骨文",
    "BRK-B": "伯克希尔哈撒韦",
    "CRWV": "CoreWeave",
}


def setup_env() -> None:
    """Mirror run_market_watch.command's environment setup."""
    env_path = MONITOR_DIR / ".env.local"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    os.environ.setdefault("SMTP_HOST", "smtp.gmail.com")
    email = os.environ.get("SMTP_USERNAME") or os.environ.get("MARKET_WATCH_EMAIL", "")
    if email:
        os.environ.setdefault("SMTP_USERNAME", email)
        os.environ.setdefault("SMTP_FROM", email)
        os.environ.setdefault("ALERT_EMAIL_TO", email)
    os.environ.setdefault("MARKET_WATCH_INSECURE_SSL", "1")
    if not os.environ.get("SMTP_PASSWORD"):
        account = os.environ.get("SMTP_USERNAME", "")
        if not account:
            return
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                "Market Watch Tool Gmail SMTP",
                "-w",
            ],
            capture_output=True,
            text=True,
        )
        password = result.stdout.strip()
        if password:
            os.environ["SMTP_PASSWORD"] = password


def refresh_market_data() -> None:
    subprocess.run(
        [
            sys.executable,
            str(MONITOR_DIR / "market_watch.py"),
            "--once",
            "--config",
            str(CONFIG_PATH),
            "--db",
            str(DB_PATH),
            "--dashboard",
            str(DASHBOARD_PATH),
        ],
        cwd=str(MONITOR_DIR),
        check=False,
    )


def leap_portfolio_stocks(config: dict) -> list[dict]:
    return [
        s
        for s in config.get("equity_watchlist", {}).get("stocks", [])
        if s.get("in_leap_portfolio")
    ]


def parse_earnings_date(value: str) -> date | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", value or "")
    if not match:
        return None
    try:
        y, m, d = (int(part) for part in match.group(1).split("-"))
        return date(y, m, d)
    except ValueError:
        return None


def macro_snapshots(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT category, name, value FROM snapshots WHERE category IN ('market', 'fed', 'macro') ORDER BY category, name"
    ).fetchall()
    return [(cat, INSTRUMENT_NAMES_ZH.get(name, name), value) for cat, name, value in rows]


_SNAP_ZH_REPLACEMENTS = [
    ("; earnings ", "；财报："),
    ("not confirmed", "未确认"),
    ("pre market", "盘前"),
    ("after market", "盘后"),
    ("EPS est", "预期EPS"),
]


def zh_snap(raw: str) -> str:
    for eng, zh in _SNAP_ZH_REPLACEMENTS:
        raw = raw.replace(eng, zh)
    return raw


def stock_snapshot(conn: sqlite3.Connection, symbol: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM snapshots WHERE category LIKE 'stock:%' AND name LIKE ? ORDER BY updated_at DESC LIMIT 1",
        (f"%({symbol})%",),
    ).fetchone()
    return zh_snap(row[0]) if row else None


def recent_stock_alerts(conn: sqlite3.Connection, names: list[str]) -> list[tuple[str, str, int]]:
    rows = conn.execute(
        """
        SELECT title, severity, score FROM alerts
        WHERE category = 'stock' AND created_at >= datetime('now', '-7 days')
        ORDER BY created_at DESC
        """
    ).fetchall()
    hits = []
    seen_titles = set()
    lowered_names = [n.lower() for n in names]
    for title, severity, score in rows:
        if title in seen_titles:
            continue
        if any(n in title.lower() for n in lowered_names):
            hits.append((title, severity, score))
            seen_titles.add(title)
    return hits


DEEP_DIVE_TIMEOUT_SECONDS = 1500  # 25 min; TSM completed cleanly in ~11 min in testing


def run_deep_dive(symbol: str) -> str:
    try:
        result = subprocess.run(
            [str(ANALYZE_PYTHON), str(ANALYZE_SCRIPT), symbol, date.today().isoformat()],
            cwd=str(TRADING_AGENT_DIR),
            capture_output=True,
            text=True,
            timeout=DEEP_DIVE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            f"（分析超时，已跑{DEEP_DIVE_TIMEOUT_SECONDS}秒仍未完成——大概率是数据源限流/卡住"
            "（比如Yahoo Finance）。本周跳过这个标的，可以手动重跑或等下周。）"
        )
    if result.returncode != 0:
        return f"（分析失败：{result.stderr[-2000:]}）"
    return result.stdout


def main() -> None:
    setup_env()
    try:
        run_review()
    except Exception as exc:  # noqa: BLE001 - last-resort net so a failure is never silent
        import traceback

        tb = traceback.format_exc()
        print(f"[error] weekly review crashed: {exc}\n{tb}", file=sys.stderr)
        try:
            config = market_watch.load_config(CONFIG_PATH)
            market_watch.send_email(
                config,
                f"LEAP组合周报 运行失败 - {date.today().isoformat()}",
                f"周报脚本在跑完之前崩溃了，报错信息：\n\n{tb}",
            )
        except Exception:
            pass  # if even the failure email can't be sent, the launchd log is the fallback
        raise


def run_review() -> None:
    refresh_market_data()

    config = market_watch.load_config(CONFIG_PATH)
    conn = sqlite3.connect(DB_PATH)

    leap_stocks = leap_portfolio_stocks(config)
    earnings_calendar = market_watch.load_earnings_calendar(config, conn)

    today = date.today()
    window_start, window_end = today - timedelta(days=7), today + timedelta(days=7)
    macro_rows = macro_snapshots(conn)

    positions: list[dict] = []
    triggered: list[tuple[str, str]] = []
    for stock in leap_stocks:
        symbol = stock["symbol"]
        name_zh = STOCK_NAMES_ZH.get(symbol, stock.get("name", symbol))
        snap = stock_snapshot(conn, symbol) or "无数据"
        earnings_raw = zh_snap(earnings_calendar.get(symbol.upper(), "尚未确认"))
        earnings_date = parse_earnings_date(earnings_raw)
        earnings_trigger = bool(earnings_date and window_start <= earnings_date <= window_end)
        alert_hits = recent_stock_alerts(conn, stock_aliases_for_match(stock))
        alert_trigger = len(alert_hits) > 0

        positions.append(
            {
                "symbol": symbol,
                "name_zh": name_zh,
                "snap": snap,
                "earnings_raw": earnings_raw,
                "earnings_trigger": earnings_trigger,
                "alert_hits": alert_hits,
            }
        )
        if earnings_trigger or alert_trigger:
            reason = []
            if earnings_trigger:
                reason.append(f"财报临近：{earnings_raw}")
            if alert_trigger:
                reason.append(f"本周{len(alert_hits)}次异动提醒：" + "；".join(t for t, _, _ in alert_hits[:3]))
            triggered.append((symbol, "；".join(reason)))

    dry_run = "--dry-run" in sys.argv
    deep_dives: list[tuple[str, str, str]] = []  # (symbol, reason, report_text)
    if triggered and not dry_run:
        for i, (symbol, reason) in enumerate(triggered):
            if i > 0:
                time.sleep(60)  # space out Yahoo Finance load between back-to-back deep dives
            deep_dives.append((symbol, reason, run_deep_dive(symbol)))
    elif triggered and dry_run:
        for symbol, reason in triggered:
            deep_dives.append((symbol, reason, "[试运行模式，未真正调用 TradingAgents]"))

    text_body = render_text(today, macro_rows, positions, triggered, deep_dives, dry_run)
    html_body = render_html(today, macro_rows, positions, triggered, deep_dives, dry_run)

    subject = f"LEAP组合周报 - {today.isoformat()}" + (" [试运行]" if dry_run else "") + (" [有深度分析]" if triggered and not dry_run else "")
    ok = market_watch.send_email(config, subject, text_body, html_body)
    print("Email sent." if ok else "Email NOT sent (check SMTP config).")
    print(text_body)


def stock_aliases_for_match(stock: dict) -> list[str]:
    names = [stock.get("symbol", ""), stock.get("name", "")]
    names.extend(stock.get("aliases", []))
    return [n for n in names if n]


def render_text(today, macro_rows, positions, triggered, deep_dives, dry_run) -> str:
    lines = [f"LEAP组合周报 - {today.isoformat()}", ""]
    lines.append("【宏观背景】")
    for cat, name, value in macro_rows:
        lines.append(f"- {name}：{value}")
    lines.append("")
    lines.append("【持仓状态】")
    for p in positions:
        flags = []
        if p["earnings_trigger"]:
            flags.append("近期有财报")
        if p["alert_hits"]:
            flags.append(f"本周{len(p['alert_hits'])}次异动")
        flag_text = f"  ⚠ {'，'.join(flags)}" if flags else ""
        lines.append(f"- {p['name_zh']}（{p['symbol']}）：{p['snap']}{flag_text}")
    lines.append("")
    if deep_dives:
        lines.append("【本周触发的深度分析（TradingAgents / Claude）】")
        for symbol, reason, report in deep_dives:
            lines.append(f"--- {symbol}，触发原因：{reason} ---")
            lines.append(report)
            lines.append("")
    else:
        lines.append("【本周触发的深度分析】")
        lines.append("本周没有标的触发（无财报窗口、无明显异动），跳过深度分析以节省成本。")
    return "\n".join(lines)


def render_html(today, macro_rows, positions, triggered, deep_dives, dry_run) -> str:
    def esc(s: str) -> str:
        return html_lib.escape(str(s))

    macro_items = "".join(
        f'<li><span class="label">{esc(name)}</span><span class="value">{esc(value)}</span></li>'
        for _, name, value in macro_rows
    )

    position_rows = ""
    for p in positions:
        badges = ""
        if p["earnings_trigger"]:
            badges += '<span class="badge badge-earn">财报临近</span>'
        if p["alert_hits"]:
            badges += f'<span class="badge badge-alert">{len(p["alert_hits"])}次异动</span>'
        position_rows += f"""
        <tr>
          <td><strong>{esc(p['name_zh'])}</strong><br><span class="muted">{esc(p['symbol'])}</span></td>
          <td>{esc(p['snap'])}</td>
          <td>{badges or '-'}</td>
        </tr>"""

    if deep_dives:
        deep_dive_html = ""
        for symbol, reason, report in deep_dives:
            report_html = esc(report).replace("\n", "<br>")
            deep_dive_html += f"""
        <div class="deep-dive">
          <h3>{esc(symbol)} <span class="muted">触发原因：{esc(reason)}</span></h3>
          <div class="report">{report_html}</div>
        </div>"""
    else:
        deep_dive_html = '<p class="muted">本周没有标的触发（无财报窗口、无明显异动），跳过深度分析以节省成本。</p>'

    dry_run_banner = '<div class="banner">试运行模式 — 未真正调用 TradingAgents</div>' if dry_run else ""

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f5f6f8; color: #1d2129; margin: 0; padding: 0; }}
  .container {{ max-width: 680px; margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .subtitle {{ color: #6b7280; font-size: 13px; margin-bottom: 20px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
  .card h2 {{ font-size: 15px; margin: 0 0 12px; color: #111827; border-left: 4px solid #2563eb; padding-left: 8px; }}
  ul.macro {{ list-style: none; margin: 0; padding: 0; }}
  ul.macro li {{ display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid #f0f1f3; font-size: 13.5px; }}
  ul.macro li:last-child {{ border-bottom: none; }}
  .label {{ color: #4b5563; }}
  .value {{ font-weight: 600; color: #111827; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  table td {{ padding: 8px 4px; border-bottom: 1px solid #f0f1f3; vertical-align: top; }}
  .muted {{ color: #9ca3af; font-size: 12px; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; margin-right: 4px; }}
  .badge-earn {{ background: #fef3c7; color: #92400e; }}
  .badge-alert {{ background: #fee2e2; color: #991b1b; }}
  .deep-dive {{ margin-bottom: 18px; }}
  .deep-dive h3 {{ font-size: 14px; margin: 0 0 8px; }}
  .report {{ font-size: 13px; line-height: 1.7; background: #fafafa; border-radius: 8px; padding: 12px 14px; white-space: normal; }}
  .banner {{ background: #dbeafe; color: #1e40af; padding: 8px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 16px; }}
</style>
</head>
<body>
<div class="container">
  <h1>LEAP 组合周报</h1>
  <div class="subtitle">{today.isoformat()}</div>
  {dry_run_banner}
  <div class="card">
    <h2>宏观背景</h2>
    <ul class="macro">{macro_items}</ul>
  </div>
  <div class="card">
    <h2>持仓状态</h2>
    <table>{position_rows}</table>
  </div>
  <div class="card">
    <h2>本周触发的深度分析（TradingAgents / Claude）</h2>
    {deep_dive_html}
  </div>
</div>
</body>
</html>"""


if __name__ == "__main__":
    main()
