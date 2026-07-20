#!/usr/bin/env python3
"""Market-moving event monitor.

Polls global market news, macro indicators, and commodities, then sends email
alerts when configured anomaly rules trigger.
"""

from __future__ import annotations

import argparse
import email.message
import html
import http.server
import json
import os
import re
import smtplib
import ssl
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.example.json"
DEFAULT_DB = APP_DIR / "market_watch.sqlite3"
DEFAULT_DASHBOARD = APP_DIR / "dashboard.html"
DEFAULT_LOG = APP_DIR / "market_watch.log"
USER_AGENT = "MarketWatchTool/1.0 (+local-monitor)"
FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"


@dataclass
class Alert:
    category: str
    title: str
    severity: str
    summary: str
    url: str = ""
    value: str = ""
    source: str = ""
    score: int = 0

    @property
    def key(self) -> str:
        raw = "|".join([self.category, normalize_title(self.title), self.url, self.value])
        return re.sub(r"\s+", " ", raw.strip().lower())


@dataclass
class Snapshot:
    category: str
    name: str
    value: str
    source: str
    updated_at: str
    numeric_value: float | None = None

    @property
    def key(self) -> str:
        return f"{self.category}:{self.name}".lower()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log_exception(context: str, exc: BaseException) -> None:
    message = f"[{utc_now()}] {context}: {type(exc).__name__}: {exc}"
    print(f"[error] {message}", file=sys.stderr)
    try:
        with DEFAULT_LOG.open("a", encoding="utf-8") as f:
            f.write(message + "\n")
    except OSError:
        pass


def local_now() -> datetime:
    return datetime.now().astimezone()


def local_now_text() -> str:
    return local_now().strftime("%Y-%m-%d %I:%M:%S %p %Z")


def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[^a-z0-9$%.\s-]", " ", title)
    title = re.sub(r"\b(the|a|an|to|of|and|or|for|in|on|as|with|after|before)\b", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def env_value(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("$"):
        return os.environ.get(value[1:], "")
    return value


def env_values(value: str | None) -> list[str]:
    raw = env_value(value)
    return [item.strip() for item in raw.split(",") if item.strip()]


def ssl_context() -> ssl.SSLContext:
    if os.environ.get("MARKET_WATCH_INSECURE_SSL") == "1":
        return ssl._create_unverified_context()
    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def request_text(url: str, timeout: int = 20, headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_key TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            severity TEXT NOT NULL,
            summary TEXT NOT NULL,
            url TEXT,
            value TEXT,
            source TEXT,
            score INTEGER NOT NULL DEFAULT 0,
            emailed INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    try:
        conn.execute("ALTER TABLE alerts ADD COLUMN score INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_key TEXT PRIMARY KEY,
            updated_at TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            source TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            kind TEXT NOT NULL,
            phrase TEXT NOT NULL,
            alert_title TEXT NOT NULL,
            UNIQUE(kind, phrase)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metric_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_key TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            numeric_value REAL NOT NULL,
            source TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        DELETE FROM metric_history
        WHERE id NOT IN (SELECT MIN(id) FROM metric_history GROUP BY snapshot_key, recorded_at)
        """
    )
    conn.execute("DROP INDEX IF EXISTS idx_metric_history_key_time")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_history_key_time ON metric_history (snapshot_key, recorded_at)"
    )
    conn.commit()
    return conn


def move_fingerprint(title: str) -> str:
    """Collapse numbers so 'AMD moved up 7.53%' and 'AMD moved up 7.38%' match."""
    return re.sub(r"\s+", " ", re.sub(r"[\d.,+%-]+", " ", title.lower())).strip()


def matched_topic(summary: str) -> frozenset[str]:
    match = re.search(r"Matched: (.+)$", summary or "")
    if not match:
        return frozenset()
    return frozenset(part.strip() for part in match.group(1).split(",") if part.strip())


def same_topic(new_topic: frozenset[str], old_topic: frozenset[str]) -> bool:
    if not new_topic or not old_topic:
        return False
    if new_topic == old_topic:
        return True
    return (new_topic <= old_topic or old_topic <= new_topic) and len(new_topic & old_topic) >= 2


def seen_alert(conn: sqlite3.Connection, alert: Alert, config: dict[str, Any] | None = None) -> bool:
    cur = conn.execute("SELECT 1 FROM alerts WHERE alert_key = ?", (alert.key,))
    if cur.fetchone() is not None:
        return True
    since = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S UTC")
    if alert.category in ("market", "stock"):
        fingerprint = move_fingerprint(alert.title)
        if not fingerprint:
            return False
        rows = conn.execute(
            "SELECT title FROM alerts WHERE category = ? AND created_at >= ?",
            (alert.category, since),
        ).fetchall()
        return any(move_fingerprint(row[0]) == fingerprint for row in rows)
    if alert.category != "news":
        return False
    fingerprint = normalize_title(alert.title)
    if not fingerprint:
        return False
    cur = conn.execute(
        """
        SELECT 1 FROM alerts
        WHERE category = 'news'
          AND lower(title) LIKE ?
          AND created_at >= ?
        LIMIT 1
        """,
        (f"%{fingerprint[:48]}%", since),
    )
    if cur.fetchone() is not None:
        return True
    # Topic cooldown: rewordings of the same story keep matching the same
    # keyword combination; only a clear score escalation gets through.
    news_cfg = (config or {}).get("news", {})
    cooldown_hours = float(news_cfg.get("topic_cooldown_hours", 4))
    if cooldown_hours <= 0:
        return False
    topic = matched_topic(alert.summary)
    if not topic:
        return False
    escalation = int(news_cfg.get("topic_escalation_score", 15))
    topic_since = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = conn.execute(
        "SELECT summary, score FROM alerts WHERE category = 'news' AND created_at >= ?",
        (topic_since,),
    ).fetchall()
    for summary, score in rows:
        if same_topic(topic, matched_topic(summary)) and alert.score < int(score or 0) + escalation:
            return True
    return False


def save_alert(conn: sqlite3.Connection, alert: Alert, emailed: bool) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO alerts
        (alert_key, created_at, category, title, severity, summary, url, value, source, score, emailed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alert.key,
            utc_now(),
            alert.category,
            alert.title,
            alert.severity,
            alert.summary,
            alert.url,
            alert.value,
            alert.source,
            alert.score,
            1 if emailed else 0,
        ),
    )
    conn.commit()


def save_snapshot(conn: sqlite3.Connection, snapshot: Snapshot) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO snapshots (snapshot_key, updated_at, category, name, value, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.key,
            snapshot.updated_at,
            snapshot.category,
            snapshot.name,
            snapshot.value,
            snapshot.source,
        ),
    )
    if snapshot.numeric_value is not None:
        last = conn.execute(
            """
            SELECT numeric_value, recorded_at FROM metric_history
            WHERE snapshot_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (snapshot.key,),
        ).fetchone()
        should_insert = True
        if last:
            last_value, last_time = last
            should_insert = abs(float(last_value) - snapshot.numeric_value) > 0.000001 or last_time[:16] != utc_now()[:16]
        if should_insert:
            conn.execute(
                """
                INSERT OR IGNORE INTO metric_history (snapshot_key, recorded_at, category, name, numeric_value, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot.key, utc_now(), snapshot.category, snapshot.name, snapshot.numeric_value, snapshot.source),
            )
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def feedback_phrase(title: str) -> str:
    words = [word for word in normalize_title(title).split() if len(word) >= 4]
    return " ".join(words[:8])


def save_feedback(conn: sqlite3.Connection, kind: str, title: str) -> str:
    phrase = feedback_phrase(title)
    if not phrase:
        return ""
    conn.execute(
        """
        INSERT OR IGNORE INTO feedback (created_at, kind, phrase, alert_title)
        VALUES (?, ?, ?, ?)
        """,
        (utc_now(), kind, phrase, title),
    )
    conn.commit()
    return phrase


def feedback_phrases(conn: sqlite3.Connection | None, kind: str = "not_important") -> list[str]:
    if conn is None:
        return []
    rows = conn.execute("SELECT phrase FROM feedback WHERE kind = ? ORDER BY id DESC LIMIT 200", (kind,)).fetchall()
    return [row[0] for row in rows if row[0]]


def rss_items(feed_url: str) -> Iterable[dict[str, str]]:
    try:
        text = request_text(feed_url)
        root = ET.fromstring(text)
    except (urllib.error.URLError, ET.ParseError, TimeoutError) as exc:
        print(f"[warn] RSS failed: {feed_url} ({exc})", file=sys.stderr)
        return []

    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = re.sub("<[^<]+?>", " ", item.findtext("description") or "")
        items.append(
            {
                "title": html.unescape(title),
                "link": html.unescape(link),
                "description": html.unescape(re.sub(r"\s+", " ", description).strip()),
                "source": feed_url,
            }
        )
    return items


def scan_news(config: dict[str, Any], conn: sqlite3.Connection | None = None) -> list[Alert]:
    news_cfg = config.get("news", {})
    keywords = [kw.lower() for kw in news_cfg.get("keywords", [])]
    keyword_weights = {k.lower(): int(v) for k, v in news_cfg.get("keyword_weights", {}).items()}
    source_weights = {k.lower(): int(v) for k, v in news_cfg.get("source_weights", {}).items()}
    suppress = [kw.lower() for kw in news_cfg.get("suppress_keywords", [])]
    suppressed_phrases = feedback_phrases(conn)
    min_score = int(news_cfg.get("min_score", 35))
    alerts: list[Alert] = []

    for feed in news_cfg.get("feeds", []):
        for item in rss_items(feed):
            haystack = f"{item['title']} {item['description']}".lower()
            if any(kw in haystack for kw in suppress):
                continue
            matched = [kw for kw in keywords if kw in haystack]
            if not matched:
                continue
            score = sum(keyword_weights.get(kw, 10) for kw in matched)
            score += max((weight for source, weight in source_weights.items() if source in feed.lower()), default=0)
            if re.search(r"\b(breaking|urgent|exclusive|live updates?)\b", haystack):
                score += 15
            if re.search(r"\b(futures|stocks|market|nasdaq|s&p|dow|treasury|yields?)\b", haystack):
                score += 10
            if any(phrase and phrase in normalize_title(item["title"]) for phrase in suppressed_phrases):
                score -= int(news_cfg.get("feedback_penalty", 35))
            score = min(100, score)
            if score < min_score:
                continue
            severity = "high" if score >= 70 else "medium"
            alerts.append(
                Alert(
                    category="news",
                    title=item["title"],
                    severity=severity,
                    summary=f"Matched: {', '.join(matched[:6])}",
                    url=item["link"],
                    source=item["source"],
                    score=score,
                )
            )
    return alerts


def term_matches(text: str, terms: list[str]) -> list[str]:
    matches = []
    for term in terms:
        if re.search(rf"\b{re.escape(term.lower())}\b", text):
            matches.append(term)
    return matches


def stock_aliases(stock: dict[str, Any]) -> list[str]:
    aliases = [stock.get("symbol", ""), stock.get("name", "")]
    aliases.extend(stock.get("aliases", []))
    clean = []
    for alias in aliases:
        alias = str(alias).strip()
        if alias and alias.lower() not in {item.lower() for item in clean}:
            clean.append(alias)
    return clean


def mentions_stock(text: str, stock: dict[str, Any]) -> bool:
    for alias in stock_aliases(stock):
        if re.search(rf"(?<![a-z0-9]){re.escape(alias.lower())}(?![a-z0-9])", text):
            return True
    return False


def scan_stock_catalyst_news(config: dict[str, Any], conn: sqlite3.Connection | None = None) -> list[Alert]:
    watch_cfg = config.get("equity_watchlist", {})
    catalyst_cfg = watch_cfg.get("stock_catalyst_news", {})
    if not catalyst_cfg.get("enabled", True):
        return []
    news_cfg = config.get("news", {})
    source_weights = {k.lower(): int(v) for k, v in news_cfg.get("source_weights", {}).items()}
    suppressed_phrases = feedback_phrases(conn)
    bullish_terms = catalyst_cfg.get("bullish_terms", [])
    bearish_terms = catalyst_cfg.get("bearish_terms", [])
    min_score = int(catalyst_cfg.get("min_score", 50))
    alerts: list[Alert] = []

    for feed in news_cfg.get("feeds", []):
        source_boost = max((weight for source, weight in source_weights.items() if source in feed.lower()), default=0)
        for item in rss_items(feed):
            text = f"{item['title']} {item['description']}".lower()
            if any(phrase and phrase in normalize_title(item["title"]) for phrase in suppressed_phrases):
                continue
            bullish = term_matches(text, bullish_terms)
            bearish = term_matches(text, bearish_terms)
            if not bullish and not bearish:
                continue
            for stock in watch_cfg.get("stocks", []):
                if not mentions_stock(text, stock):
                    continue
                sentiment = "Bullish" if len(bullish) >= len(bearish) else "Bearish"
                matched = bullish if sentiment == "Bullish" else bearish
                score = min(100, 45 + len(matched) * 12 + source_boost)
                if score < min_score:
                    continue
                symbol = stock["symbol"].upper()
                alerts.append(
                    Alert(
                        category="stock_news",
                        title=f"{sentiment} {symbol}: {item['title']}",
                        severity="high" if score >= 75 else "medium",
                        summary=f"{sentiment} catalyst for {stock.get('name', symbol)}. Matched: {', '.join(matched[:5])}",
                        url=item["link"],
                        value=sentiment,
                        source=item["source"],
                        score=score,
                    )
                )
    return alerts


def yahoo_chart(symbol: str, range_: str = "5d", interval: str = "1d") -> dict[str, Any] | None:
    encoded = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_}&interval={interval}"
    try:
        data = json.loads(request_text(url))
        result = data.get("chart", {}).get("result", [])
        return result[0] if result else None
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, IndexError) as exc:
        print(f"[warn] Yahoo chart failed: {symbol} ({exc})", file=sys.stderr)
        return None


def last_two_closes(symbol: str) -> tuple[float, float] | None:
    result = yahoo_chart(symbol)
    if not result:
        return None
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    clean = [float(c) for c in closes if c is not None]
    if len(clean) < 2:
        return None
    return clean[-2], clean[-1]


def import_historical_market_data(config: dict[str, Any], conn: sqlite3.Connection, days: int = 180) -> int:
    imported = 0
    for instrument in config.get("market_data", {}).get("instruments", []):
        symbol = instrument["symbol"]
        name = instrument.get("name", symbol)
        result = yahoo_chart(symbol, range_=f"{days}d", interval="1d")
        if not result:
            continue
        timestamps = result.get("timestamp") or []
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        snapshot_key = f"market:{name}".lower()
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            recorded = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            conn.execute(
                """
                INSERT OR IGNORE INTO metric_history
                (snapshot_key, recorded_at, category, name, numeric_value, source)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot_key, recorded, "market", name, float(close), f"Yahoo Finance: {symbol}"),
            )
            imported += 1
    conn.commit()
    return imported


def nasdaq_earnings_on_date(day: date) -> list[dict[str, Any]] | None:
    qs = urllib.parse.urlencode({"date": day.isoformat()})
    url = f"https://api.nasdaq.com/api/calendar/earnings?{qs}"
    try:
        data = json.loads(request_text(url, timeout=10, headers=NASDAQ_API_HEADERS))
    # OSError covers URLError/TimeoutError plus raw socket errors like
    # ConnectionResetError raised mid-read, which urlopen does not wrap.
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] Nasdaq earnings failed for {day.isoformat()}: {exc}", file=sys.stderr)
        return None
    return (data.get("data") or {}).get("rows") or []


def fmp_earnings_calendar(config: dict[str, Any], symbols: set[str], start: date, end: date) -> dict[str, str]:
    watch_cfg = config.get("equity_watchlist", {})
    api_key = env_value(watch_cfg.get("fmp_api_key"))
    if not api_key:
        return {}
    qs = urllib.parse.urlencode({"from": start.isoformat(), "to": end.isoformat(), "apikey": api_key})
    url = f"https://financialmodelingprep.com/stable/earnings-calendar?{qs}"
    try:
        rows = json.loads(request_text(url, timeout=20))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"[warn] FMP earnings calendar failed: {exc}", file=sys.stderr)
        return {}
    if not isinstance(rows, list):
        return {}
    found: dict[str, str] = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if symbol not in symbols or symbol in found:
            continue
        report_date = row.get("date") or row.get("epsDate")
        if not report_date:
            continue
        eps = row.get("epsEstimated") or row.get("epsEstimate")
        revenue = row.get("revenueEstimated")
        detail = f"{report_date} (FMP)"
        if eps not in (None, ""):
            detail += f", EPS est {eps}"
        if revenue not in (None, ""):
            detail += f", revenue est {revenue}"
        found[symbol] = detail
    return found


def load_earnings_calendar(config: dict[str, Any], conn: sqlite3.Connection | None = None) -> dict[str, str]:
    watch_cfg = config.get("equity_watchlist", {})
    symbols = {stock["symbol"].upper() for stock in watch_cfg.get("stocks", [])}
    if not symbols:
        return {}
    today = local_now().date()
    horizon_days = int(watch_cfg.get("earnings_horizon_days", 120))
    end_day = today + timedelta(days=horizon_days)
    has_fmp = bool(env_value(watch_cfg.get("fmp_api_key")))
    cache_key = f"earnings:v2:{'fmp' if has_fmp else 'nasdaq'}:{today.isoformat()}:{horizon_days}"
    if conn:
        cached = get_state(conn, cache_key)
        if cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass
    overrides = {symbol.upper(): value for symbol, value in watch_cfg.get("earnings_overrides", {}).items() if value}
    found: dict[str, str] = {symbol: overrides[symbol] for symbol in symbols if symbol in overrides}

    found.update({symbol: value for symbol, value in fmp_earnings_calendar(config, symbols - set(found), today, end_day).items()})

    nasdaq_failures = 0
    consecutive_failures = 0
    for offset in range(horizon_days + 1):
        if found.keys() >= symbols:
            break
        if consecutive_failures >= 8:
            # Network is almost certainly down; don't grind through the rest
            # of the horizon at ~10s of timeout per day.
            print("[warn] Nasdaq earnings: giving up after 8 consecutive failures (network down?).", file=sys.stderr)
            nasdaq_failures = horizon_days  # ensure the partial result is not cached
            break
        day = today + timedelta(days=offset)
        rows = nasdaq_earnings_on_date(day)
        if rows is None:
            nasdaq_failures += 1
            consecutive_failures += 1
            continue
        consecutive_failures = 0
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if symbol not in symbols or symbol in found:
                continue
            report_time = str(row.get("time", "")).replace("time-", "").replace("-", " ").strip()
            if report_time.lower() in ("not supplied", "n/a"):
                report_time = ""
            eps = row.get("epsForecast")
            detail = f"{day.isoformat()}"
            if report_time:
                detail += f" {report_time}"
            if eps:
                detail += f", EPS est {eps}"
            found[symbol] = detail
    for symbol in symbols:
        found.setdefault(symbol, "not confirmed")
    too_many_failures = nasdaq_failures > max(3, horizon_days // 2) and not has_fmp
    if conn and not too_many_failures:
        set_state(conn, cache_key, json.dumps(found, sort_keys=True))
    elif too_many_failures:
        print("[warn] Earnings calendar not cached because Nasdaq requests mostly failed.", file=sys.stderr)
    return found


def yahoo_earnings_date(symbol: str, earnings_calendar: dict[str, str]) -> str:
    return earnings_calendar.get(symbol.upper(), "not confirmed")


def scan_market_data(config: dict[str, Any]) -> tuple[list[Alert], list[Snapshot]]:
    alerts: list[Alert] = []
    snapshots: list[Snapshot] = []
    for instrument in config.get("market_data", {}).get("instruments", []):
        symbol = instrument["symbol"]
        pair = last_two_closes(symbol)
        if not pair:
            continue
        prev_close, last_close = pair
        if prev_close == 0:
            continue
        pct = (last_close - prev_close) / prev_close * 100
        snapshots.append(
            Snapshot(
                category="market",
                name=instrument.get("name", symbol),
                value=f"{last_close:.2f} ({pct:+.2f}%)",
                source=f"Yahoo Finance: {symbol}",
                updated_at=local_now_text(),
                numeric_value=last_close,
            )
        )
        threshold = float(instrument.get("daily_move_alert_pct", 2.0))
        if abs(pct) >= threshold:
            direction = "up" if pct > 0 else "down"
            alerts.append(
                Alert(
                    category="market",
                    title=f"{instrument.get('name', symbol)} moved {direction} {pct:.2f}%",
                    severity="high" if abs(pct) >= threshold * 1.8 else "medium",
                    summary=f"{symbol} latest close {last_close:.2f}, previous close {prev_close:.2f}",
                    value=f"{last_close:.2f} ({pct:+.2f}%)",
                    source="Yahoo Finance chart API",
                    score=int(min(100, 45 + abs(pct / threshold) * 25)),
                )
            )
    return alerts, snapshots


def scan_equity_watchlist(
    config: dict[str, Any], earnings_calendar: dict[str, str] | None = None
) -> tuple[list[Alert], list[Snapshot]]:
    watch_cfg = config.get("equity_watchlist", {})
    if not watch_cfg.get("enabled", True):
        return [], []
    alerts: list[Alert] = []
    snapshots: list[Snapshot] = []
    default_threshold = float(watch_cfg.get("daily_move_alert_pct", 3.0))
    show_earnings = bool(watch_cfg.get("show_earnings_date", True))
    earnings_calendar = earnings_calendar or {}
    for stock in watch_cfg.get("stocks", []):
        symbol = stock["symbol"]
        pair = last_two_closes(symbol)
        earnings = yahoo_earnings_date(symbol, earnings_calendar) if show_earnings else "disabled"
        if not pair:
            snapshots.append(
                Snapshot(
                    category="watchlist",
                    name=f"{stock.get('name', symbol)} ({symbol})",
                    value=f"price unavailable; earnings {earnings}",
                    source="Yahoo Finance price; FMP/Nasdaq earnings calendar",
                    updated_at=local_now_text(),
                )
            )
            continue
        prev_close, last_close = pair
        pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0
        label = stock.get("theme", "watchlist")
        snapshots.append(
            Snapshot(
                category=f"stock:{label}",
                name=f"{stock.get('name', symbol)} ({symbol})",
                value=f"{last_close:.2f} ({pct:+.2f}%); earnings {earnings}",
                source="Yahoo Finance price; FMP/Nasdaq earnings calendar",
                updated_at=local_now_text(),
            )
        )
        threshold = float(stock.get("daily_move_alert_pct", default_threshold))
        if abs(pct) >= threshold:
            direction = "up" if pct > 0 else "down"
            alerts.append(
                Alert(
                    category="stock",
                    title=f"{stock.get('name', symbol)} moved {direction} {pct:.2f}%",
                    severity="high" if abs(pct) >= threshold * 1.8 else "medium",
                    summary=f"{symbol} latest close {last_close:.2f}, previous close {prev_close:.2f}; next earnings {earnings}",
                    value=f"{last_close:.2f} ({pct:+.2f}%)",
                    source="Yahoo Finance",
                    score=int(min(100, 45 + abs(pct / threshold) * 25)),
                )
            )
    return alerts, snapshots


def parse_fomc_dates(page_html: str) -> list[datetime]:
    now_year = local_now().year
    month_lookup = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    text = re.sub(r"<[^>]+>", " ", page_html)
    text = html.unescape(re.sub(r"\s+", " ", text))
    dates: list[datetime] = []
    month_names = "|".join(month_lookup.keys())
    for year in range(now_year, now_year + 3):
        year_marker = f"{year} fomc meetings"
        year_pos = text.lower().find(year_marker)
        if year_pos < 0:
            continue
        next_year_pos = text.lower().find("fomc meetings", year_pos + len(year_marker))
        segment = text[year_pos:next_year_pos] if next_year_pos > year_pos else text[year_pos : year_pos + 2500]
        pattern = rf"\b({month_names})\s+(\d{{1,2}})(?:-(\d{{1,2}}))?\*?(?=\s+(?:Statement:|{month_names}|\* Meeting|$))"
        for match in re.finditer(pattern, segment, re.IGNORECASE):
            month_name = match.group(1).lower()
            month_number = month_lookup[month_name]
            end_day = int(match.group(3) or match.group(2))
            try:
                dates.append(datetime(year, month_number, end_day, 14, 0).astimezone())
            except ValueError:
                continue
    return sorted(set(dates))


def scan_fomc_calendar(config: dict[str, Any]) -> tuple[list[Alert], list[Snapshot]]:
    fomc_cfg = config.get("fomc_calendar", {})
    if not fomc_cfg.get("enabled", True):
        return [], []
    try:
        dates = parse_fomc_dates(request_text(FOMC_CALENDAR_URL))
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[warn] FOMC calendar failed: {exc}", file=sys.stderr)
        return [], []
    now = local_now()
    future_dates = [dt for dt in dates if dt.date() >= now.date()]
    if not future_dates:
        return [], []
    next_meeting = future_dates[0]
    days = (next_meeting.date() - now.date()).days
    snapshot = Snapshot(
        category="macro",
        name="Next FOMC Decision",
        value=f"{next_meeting.strftime('%Y-%m-%d')} 2:00 PM ET ({days} days)",
        source="Federal Reserve FOMC calendar",
        updated_at=local_now_text(),
    )
    alerts: list[Alert] = []
    alert_days = [int(day) for day in fomc_cfg.get("alert_days_before", [7, 2, 1, 0])]
    if days in alert_days:
        alerts.append(
            Alert(
                category="fed",
                title=f"FOMC decision in {days} day(s)",
                severity="high" if days <= 1 else "medium",
                summary=f"Next scheduled FOMC decision date is {next_meeting.strftime('%Y-%m-%d')}.",
                value=f"{days} days",
                source="Federal Reserve FOMC calendar",
                score=80 if days <= 1 else 65,
            )
        )
    return alerts, [snapshot]


NASDAQ_API_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

DEFAULT_ECON_EVENTS = [
    "CPI",
    "Core CPI",
    "PPI",
    "PCE Price Index",
    "Core PCE Price Index",
    "Nonfarm Payrolls",
    "Unemployment Rate",
    "GDP",
    "Retail Sales",
]


def normalize_event_name(name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(name)).strip().lower()


def nasdaq_econ_events_on_date(day: date) -> list[dict[str, Any]] | None:
    qs = urllib.parse.urlencode({"date": day.isoformat()})
    url = f"https://api.nasdaq.com/api/calendar/economicevents?{qs}"
    try:
        data = json.loads(request_text(url, timeout=10, headers=NASDAQ_API_HEADERS))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"[warn] Nasdaq econ calendar failed for {day.isoformat()}: {exc}", file=sys.stderr)
        return None
    return (data.get("data") or {}).get("rows") or []


def load_econ_calendar(config: dict[str, Any], conn: sqlite3.Connection | None = None) -> dict[str, dict[str, str]]:
    """Nearest upcoming date/time for each watched US economic release."""
    econ_cfg = config.get("econ_calendar", {})
    watched = {normalize_event_name(name): str(name) for name in econ_cfg.get("events", DEFAULT_ECON_EVENTS)}
    if not watched:
        return {}
    today = local_now().date()
    horizon_days = int(econ_cfg.get("horizon_days", 14))
    cache_key = f"econ:v1:{today.isoformat()}:{horizon_days}"
    if conn:
        cached = get_state(conn, cache_key)
        if cached:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass
    found: dict[str, dict[str, str]] = {}
    failures = 0
    for offset in range(horizon_days + 1):
        if found.keys() >= watched.keys():
            break
        day = today + timedelta(days=offset)
        rows = nasdaq_econ_events_on_date(day)
        if rows is None:
            failures += 1
            continue
        for row in rows:
            if row.get("country") != "United States":
                continue
            key = normalize_event_name(row.get("eventName", ""))
            if key not in watched or key in found:
                continue
            found[key] = {
                "name": watched[key],
                "date": day.isoformat(),
                "time": str(row.get("gmt", "")).strip(),
            }
    if conn and found and failures <= horizon_days // 2:
        set_state(conn, cache_key, json.dumps(found, sort_keys=True))
    return found


def scan_econ_calendar(config: dict[str, Any], conn: sqlite3.Connection | None = None) -> tuple[list[Alert], list[Snapshot]]:
    econ_cfg = config.get("econ_calendar", {})
    if not econ_cfg.get("enabled", True):
        return [], []
    calendar = load_econ_calendar(config, conn)
    if not calendar:
        return [], []
    today = local_now().date()
    alert_days = [int(day) for day in econ_cfg.get("alert_days_before", [1, 0])]
    alerts: list[Alert] = []
    snapshots: list[Snapshot] = []
    for entry in sorted(calendar.values(), key=lambda item: item["date"]):
        try:
            release_day = datetime.strptime(entry["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (release_day - today).days
        if days < 0:
            continue
        time_note = f" {entry['time']} ET" if entry.get("time") else ""
        snapshots.append(
            Snapshot(
                category="macro",
                name=f"Next {entry['name']}",
                value=f"{entry['date']}{time_note} ({days} day{'s' if days != 1 else ''})",
                source="Nasdaq economic calendar",
                updated_at=local_now_text(),
            )
        )
        if days in alert_days:
            when = "today" if days == 0 else f"in {days} day(s)"
            alerts.append(
                Alert(
                    category="macro",
                    title=f"{entry['name']} release {when} ({entry['date']}{time_note})",
                    severity="high" if days == 0 else "medium",
                    summary=f"US {entry['name']} is scheduled for {entry['date']}{time_note}. Expect volatility around the release.",
                    value=f"{entry['date']}{time_note}",
                    source="Nasdaq economic calendar",
                    score=75 if days == 0 else 65,
                )
            )
    return alerts, snapshots


def fred_latest(series_id: str, api_key: str) -> tuple[str, float] | None:
    qs = urllib.parse.urlencode(
        {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        }
    )
    url = f"https://api.stlouisfed.org/fred/series/observations?{qs}"
    try:
        data = json.loads(request_text(url))
        obs = data.get("observations", [])
        if not obs or obs[0].get("value") in (None, "."):
            return None
        return obs[0]["date"], float(obs[0]["value"])
    except (urllib.error.URLError, json.JSONDecodeError, ValueError, TimeoutError) as exc:
        print(f"[warn] FRED failed: {series_id} ({exc})", file=sys.stderr)
        return None


def scan_fed(config: dict[str, Any]) -> tuple[list[Alert], list[Snapshot]]:
    fed_cfg = config.get("fed", {})
    api_key = env_value(fed_cfg.get("fred_api_key"))
    if not api_key:
        return [], []
    alerts: list[Alert] = []
    snapshots: list[Snapshot] = []
    for series in fed_cfg.get("series", []):
        latest = fred_latest(series["id"], api_key)
        if not latest:
            continue
        date, value = latest
        snapshots.append(
            Snapshot(
                category="fed",
                name=series.get("name", series["id"]),
                value=f"{value:.2f}",
                source=f"FRED {series['id']} ({date})",
                updated_at=utc_now(),
            )
        )
        lower = series.get("alert_below")
        upper = series.get("alert_above")
        if (lower is not None and value < float(lower)) or (upper is not None and value > float(upper)):
            alerts.append(
                Alert(
                    category="fed",
                    title=f"{series.get('name', series['id'])} at {value:.2f}",
                    severity="medium",
                    summary=f"Latest FRED observation on {date}",
                    value=f"{value:.2f}",
                    source="FRED",
                    score=65,
                )
            )
    return alerts, snapshots


def alert_sort_key(alert: Alert) -> tuple[int, int]:
    severity_rank = {"high": 2, "medium": 1, "low": 0}.get(alert.severity, 0)
    return severity_rank, alert.score


def group_alerts(alerts: list[Alert]) -> dict[str, list[Alert]]:
    groups: dict[str, list[Alert]] = {}
    for alert in sorted(alerts, key=alert_sort_key, reverse=True):
        groups.setdefault(alert.category, []).append(alert)
    return groups


def alert_bucket(category: str) -> str:
    category = category.lower()
    if category in ("fed", "market", "news", "macro"):
        return "Macro"
    if category in ("stock", "stock_news"):
        return "Stock"
    return "Other"


def snapshots_from_db(conn: sqlite3.Connection) -> list[tuple[str, str, str, str, str]]:
    return conn.execute(
        """
        SELECT updated_at, category, name, value, source
        FROM snapshots
        ORDER BY category, name
        """
    ).fetchall()


def macro_snapshots(snapshots: list[tuple[str, str, str, str, str]]) -> list[tuple[str, str, str, str, str]]:
    return [row for row in snapshots if snapshot_section(row[1]) == "macro"]


def earnings_alerts(config: dict[str, Any], conn: sqlite3.Connection, days_before: int | None = None) -> list[Alert]:
    watch_cfg = config.get("equity_watchlist", {})
    days_before = int(days_before if days_before is not None else watch_cfg.get("earnings_email_days_before", 7))
    earnings_calendar = load_earnings_calendar(config, conn)
    today = local_now().date()
    alerts: list[Alert] = []
    for stock in watch_cfg.get("stocks", []):
        symbol = stock["symbol"].upper()
        earnings = earnings_calendar.get(symbol, "not confirmed")
        match = re.search(r"\d{4}-\d{2}-\d{2}", earnings)
        if not match:
            continue
        try:
            earnings_day = datetime.strptime(match.group(0), "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (earnings_day - today).days
        if 0 <= days <= days_before:
            alerts.append(
                Alert(
                    category="stock",
                    title=f"{stock.get('name', symbol)} earnings in {days} day(s)",
                    severity="high" if days <= 1 else "medium",
                    summary=f"{symbol} earnings date: {earnings}",
                    value=earnings,
                    source="FMP/Nasdaq earnings calendar",
                    score=78 if days <= 1 else 62,
                )
            )
    return alerts


SEVERITY_COLORS = {"high": "#b42318", "medium": "#b47d02", "low": "#5f6b6f"}


def pretty_source(source: str) -> str:
    if source.startswith("http"):
        return urllib.parse.urlparse(source).netloc or source
    return source


def alert_email_card(alert: Alert) -> str:
    color = SEVERITY_COLORS.get(alert.severity, "#5f6b6f")
    score_pill = (
        f"<span style=\"display:inline-block;min-width:24px;text-align:center;padding:1px 8px;border-radius:999px;"
        f"background:{color};color:#ffffff;font-size:12px;font-weight:700;\">{alert.score}</span>"
    )
    category_label = (
        f"<span style=\"color:#5b666a;font-size:11px;text-transform:uppercase;letter-spacing:.4px;\">"
        f"&nbsp;{html.escape(alert.category)}</span>"
    )
    badge = ""
    if alert.category == "stock_news" and alert.value:
        badge_color = "#067647" if alert.value == "Bullish" else "#b42318"
        badge = (
            f" <span style=\"display:inline-block;padding:1px 8px;border-radius:999px;"
            f"background:{badge_color};color:#fff;font-size:12px;\">{html.escape(alert.value)}</span>"
        )
    title_text = html.escape(alert.title)
    if alert.url:
        title_html = f"<a href=\"{html.escape(alert.url)}\" style=\"color:#0b5cad;text-decoration:none;\">{title_text}</a>"
    else:
        title_html = title_text
    meta_bits = [bit for bit in (alert.value if alert.category != "stock_news" else "", pretty_source(alert.source)) if bit]
    meta = (
        f"<div style=\"color:#8a959a;font-size:12px;margin-top:4px;\">{html.escape(' · '.join(meta_bits))}</div>"
        if meta_bits
        else ""
    )
    return (
        f"<div style=\"background:#ffffff;border:1px solid #e2e8e5;border-left:4px solid {color};"
        f"border-radius:6px;padding:10px 12px;margin-bottom:8px;\">"
        f"<div style=\"margin-bottom:4px;\">{score_pill}{category_label}{badge}</div>"
        f"<div style=\"font-size:15px;font-weight:700;line-height:1.35;\">{title_html}</div>"
        f"<div style=\"color:#3f4c50;font-size:13px;margin-top:3px;line-height:1.45;\">{html.escape(alert.summary)}</div>"
        f"{meta}</div>"
    )


def build_email_body(
    title: str,
    alerts: list[Alert],
    snapshots: list[tuple[str, str, str, str, str]],
    dashboard_url: str = "http://127.0.0.1:8765",
    max_alerts: int = 15,
) -> tuple[str, str]:
    ordered = sorted(alerts, key=alert_sort_key, reverse=True)
    shown = ordered[:max_alerts]
    hidden = len(ordered) - len(shown)

    text_lines = [title, f"Generated: {local_now_text()}", ""]
    html_parts = [
        "<html><head><meta charset=\"utf-8\"></head>"
        "<body style=\"margin:0;background:#f2f4f3;font-family:-apple-system,'Segoe UI',Arial,sans-serif;color:#1d2528;\">",
        "<div style=\"max-width:680px;margin:0 auto;padding:20px;\">",
        f"<h2 style=\"margin:0 0 2px;font-size:20px;\">{html.escape(title)}</h2>",
        f"<p style=\"margin:0 0 16px;color:#5b666a;font-size:13px;\">{html.escape(local_now_text())}</p>",
    ]

    if shown:
        for bucket in ("Macro", "Stock", "Other"):
            bucket_alerts = [alert for alert in shown if alert_bucket(alert.category) == bucket]
            if not bucket_alerts:
                continue
            text_lines.append(bucket.upper())
            html_parts.append(
                f"<h3 style=\"margin:18px 0 8px;font-size:13px;text-transform:uppercase;"
                f"letter-spacing:.5px;color:#5b666a;\">{html.escape(bucket)}</h3>"
            )
            for alert in bucket_alerts:
                text_lines.extend(
                    [
                        f"- [{alert.severity.upper()} {alert.score}] {alert.title}",
                        f"  {alert.summary}",
                        f"  Value: {alert.value}" if alert.value else "",
                        f"  Source: {alert.source}" if alert.source else "",
                        f"  URL: {alert.url}" if alert.url else "",
                    ]
                )
                html_parts.append(alert_email_card(alert))
            text_lines.append("")
        if hidden > 0:
            text_lines.append(f"(+{hidden} more on the dashboard)")
            html_parts.append(
                f"<p style=\"color:#8a959a;font-size:13px;margin:4px 0 0;\">+{hidden} more on the dashboard</p>"
            )
    else:
        text_lines.append("No alerts in this window.")
        html_parts.append("<p style=\"color:#5b666a;\">No alerts in this window.</p>")

    macro = macro_snapshots(snapshots)
    if macro:
        text_lines.extend(["", "Market Snapshot"])
        html_parts.append(
            "<h3 style=\"margin:22px 0 8px;font-size:13px;text-transform:uppercase;"
            "letter-spacing:.5px;color:#5b666a;\">Market Snapshot</h3>"
        )
        html_parts.append(
            "<table cellpadding=\"7\" cellspacing=\"0\" style=\"width:100%;border-collapse:collapse;"
            "background:#ffffff;border:1px solid #e2e8e5;border-radius:6px;\">"
        )
        for row_index, (updated_at, category, name, value, source) in enumerate(macro):
            text_lines.append(f"- {name}: {value}")
            border = "border-top:1px solid #eef2f0;" if row_index else ""
            html_parts.append(
                f"<tr><td style=\"{border}font-size:13px;color:#3f4c50;\">{html.escape(name)}</td>"
                f"<td style=\"{border}font-size:13px;font-weight:700;text-align:right;\">{html.escape(value)}</td></tr>"
            )
        html_parts.append("</table>")

    text_lines.extend(["", f"Dashboard: {dashboard_url}"])
    html_parts.append(
        f"<p style=\"margin:16px 0 0;font-size:12px;color:#8a959a;\">Dashboard: "
        f"<a href=\"{dashboard_url}\" style=\"color:#0b5cad;\">{dashboard_url}</a> (while the monitor is running)</p>"
    )
    html_parts.append("</div></body></html>")
    return "\n".join(line for line in text_lines if line is not None), "".join(html_parts)


def send_email(config: dict[str, Any], subject: str, text_body: str, html_body: str = "") -> bool:
    if not text_body:
        return False
    email_cfg = config.get("email", {})
    smtp_host = env_value(email_cfg.get("smtp_host"))
    smtp_port = int(env_value(str(email_cfg.get("smtp_port", 587))) or 587)
    username = env_value(email_cfg.get("username"))
    password = env_value(email_cfg.get("password"))
    sender = env_value(email_cfg.get("from")) or username
    recipients: list[str] = []
    for item in email_cfg.get("to", []):
        recipients.extend(env_values(item))
    if not smtp_host or not sender or not recipients:
        print("[warn] Email is not fully configured; alerts saved but not emailed.", file=sys.stderr)
        return False

    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        if email_cfg.get("starttls", True):
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)
    return True


def write_dashboard(path: Path, conn: sqlite3.Connection) -> None:
    doc = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="0; url=http://127.0.0.1:8765">
  <title>Market Watch Dashboard</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f5f6f4; color: #1d2528; }}
    main {{ max-width: 520px; padding: 28px; }}
    h1 {{ margin: 0 0 10px; font-size: 24px; }}
    p {{ margin: 0 0 16px; color: #5d676b; line-height: 1.45; }}
    a {{ color: #0b5cad; font-weight: 700; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #111716; color: #e7ece9; }}
      p {{ color: #aebbb6; }}
      a {{ color: #8ec7ff; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Opening Market Watch Live</h1>
    <p>The full dashboard with tabs, Charts, Stock Watchlist, and Send Test Email runs at:</p>
    <p><a href="http://127.0.0.1:8765">http://127.0.0.1:8765</a></p>
    <p>If that page does not open, start <code>run_market_watch.command</code> first.</p>
  </main>
</body>
</html>
"""
    path.write_text(doc, encoding="utf-8")


def snapshot_section(category: str) -> str:
    category = category.lower()
    if category.startswith("stock:"):
        return "stock"
    if category in ("macro", "market", "fed"):
        return "macro"
    return "alerts"


def chart_series(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT name, recorded_at, numeric_value
        FROM metric_history
        WHERE category = 'market'
        ORDER BY name, recorded_at
        """
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for name, recorded_at, value in rows:
        grouped.setdefault(name, []).append({"time": recorded_at, "value": float(value)})
    return [{"name": name, "points": points[-240:]} for name, points in grouped.items()]


def alert_section(category: str) -> str:
    category = category.lower()
    if category in ("stock", "stock_news"):
        return "stock"
    if category in ("fed", "market", "macro"):
        return "macro"
    if category == "news":
        return "news"
    return "alerts"


def format_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour(s) ago"
    return f"{hours // 24} day(s) ago"


def monitor_health_badge(conn: sqlite3.Connection) -> str:
    last_poll = get_state(conn, "last_poll_at")
    if not last_poll:
        return '<span class="health warn">&#9679; No poll recorded yet &mdash; start the monitor to collect data</span>'
    try:
        last_dt = datetime.strptime(last_poll, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    interval = int(get_state(conn, "poll_interval_seconds") or 300)
    age = (datetime.now(timezone.utc) - last_dt).total_seconds()
    if age <= max(interval * 2, 600):
        return f'<span class="health ok">&#9679; Monitor active &middot; last poll {format_age(age)}</span>'
    return f'<span class="health bad">&#9679; Last poll {format_age(age)} &mdash; monitor may be stopped</span>'


def web_dashboard_html(conn: sqlite3.Connection) -> str:
    snapshots = snapshots_from_db(conn)
    health_badge = monitor_health_badge(conn)
    charts_json = json.dumps(chart_series(conn))
    alerts = conn.execute(
        """
        SELECT id, created_at, category, severity, score, title, summary, value, source, url, emailed
        FROM alerts
        ORDER BY id DESC
        LIMIT 300
        """
    ).fetchall()
    categories = sorted({row[2] for row in alerts})
    snapshot_cards = []
    for updated_at, category, name, value, source in snapshots:
        section = snapshot_section(category)
        snapshot_cards.append(
            f"""
            <article class="snapshot" data-section="{html.escape(section)}" data-category="{html.escape(category)}">
              <span>{html.escape(category)}</span>
              <h2>{html.escape(name)}</h2>
              <strong>{html.escape(value)}</strong>
              <small>{html.escape(source)} · {html.escape(updated_at)}</small>
            </article>
            """
        )
    rows = []
    for alert_id, created_at, category, severity, score, title, summary, value, source, url, emailed in alerts:
        link = f'<a href="{html.escape(url)}" target="_blank" rel="noreferrer">Open</a>' if url else ""
        section = alert_section(category)
        rows.append(
            f"""
            <tr data-section="{html.escape(section)}" data-category="{html.escape(category)}" data-score="{int(score or 0)}" data-text="{html.escape((title + ' ' + summary + ' ' + source).lower())}">
              <td>{html.escape(created_at)}</td>
              <td><span class="pill">{html.escape(category)}</span></td>
              <td class="{html.escape(severity)}">{html.escape(severity)}</td>
              <td>{int(score or 0)}</td>
              <td>{html.escape(title)}</td>
              <td>{html.escape(summary)}</td>
              <td>{html.escape(value or "")}</td>
              <td>{html.escape(source or "")}</td>
              <td>{link}</td>
              <td>{'yes' if emailed else 'no'}</td>
              <td><button type="button" class="quiet" data-id="{alert_id}" title="Lower future similar alerts">Not important</button></td>
            </tr>
            """
        )
    category_options = "".join(f'<option value="{html.escape(cat)}">{html.escape(cat)}</option>' for cat in categories)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Market Watch Live</title>
  <style>
    :root {{ color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; }}
    body {{ margin: 0; background: #f5f6f4; color: #1d2528; }}
    header {{ position: sticky; top: 0; z-index: 2; padding: 16px 24px 12px; border-bottom: 1px solid #d8dfda; background: rgba(255,255,255,.96); backdrop-filter: blur(10px); }}
    h1 {{ margin: 0 0 4px; font-size: 22px; letter-spacing: 0; }}
    p {{ margin: 0; color: #5f6b6f; }}
    .header-row {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }}
    .header-actions {{ display: flex; align-items: center; gap: 10px; }}
    .send-test {{ border-color: #1f6f5b; background: #1f6f5b; color: #fff; font-weight: 650; }}
    .send-status {{ color: #5f6b6f; font-size: 13px; min-width: 120px; }}
    main {{ padding: 20px 24px 36px; }}
    .tabs {{ display: flex; gap: 8px; margin-top: 14px; overflow-x: auto; }}
    .tab {{ min-height: 34px; border-radius: 999px; padding: 0 14px; color: #344247; }}
    .tab.active {{ background: #1f6f5b; border-color: #1f6f5b; color: #fff; }}
    .section-title {{ margin: 0 0 12px; font-size: 18px; }}
    .controls {{ display: grid; grid-template-columns: minmax(180px, 1fr) 150px 160px 120px; gap: 10px; margin: 18px 0 12px; }}
    input, select, button {{ min-height: 36px; border: 1px solid #cbd5d1; border-radius: 6px; background: #fff; color: inherit; padding: 0 10px; font: inherit; }}
    button {{ cursor: pointer; }}
    button.quiet {{ min-height: 30px; white-space: nowrap; }}
    .snapshots {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin-bottom: 18px; }}
    .charts {{ display: none; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; margin-bottom: 18px; }}
    .chart-card {{ border: 1px solid #d8dfda; background: #fff; border-radius: 8px; padding: 14px; min-height: 280px; }}
    .chart-card h3 {{ margin: 0 0 4px; font-size: 15px; }}
    .chart-meta {{ margin-bottom: 8px; color: #5f6b6f; font-size: 12px; }}
    .chart-card svg {{ width: 100%; height: 220px; display: block; }}
    .axis {{ fill: #516064; font-size: 11px; font-weight: 650; }}
    .grid {{ stroke: #ccd6d1; stroke-width: 1; }}
    .line {{ stroke: #0b78d0; stroke-width: 3.5; fill: none; stroke-linejoin: round; stroke-linecap: round; }}
    article {{ border: 1px solid #d8dfda; background: #fff; border-radius: 8px; padding: 14px; min-height: 112px; }}
    article span, .pill {{ color: #5f6b6f; font-size: 12px; text-transform: uppercase; }}
    article h2 {{ margin: 8px 0 10px; font-size: 15px; line-height: 1.25; }}
    article strong {{ display: block; font-size: 18px; line-height: 1.25; overflow-wrap: anywhere; }}
    article small {{ display: block; margin-top: 10px; color: #5f6b6f; line-height: 1.35; }}
    .table-wrap {{ overflow-x: auto; border: 1px solid #d8dfda; background: #fff; max-height: 62vh; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 9px 10px; border-bottom: 1px solid #e7ece8; vertical-align: top; font-size: 13px; }}
    th {{ background: #edf2ef; text-align: left; font-size: 12px; text-transform: uppercase; color: #516064; }}
    .high {{ color: #b42318; font-weight: 700; }}
    .medium {{ color: #9a6700; font-weight: 700; }}
    .muted {{ color: #6b777a; }}
    .health {{ font-size: 13px; font-weight: 650; white-space: nowrap; }}
    .health.ok {{ color: #1f8a5b; }}
    .health.warn {{ color: #9a6700; }}
    .health.bad {{ color: #d13438; }}
    @media (max-width: 760px) {{ .controls {{ grid-template-columns: 1fr 1fr; }} th {{ position: static; }} }}
    @media (max-width: 480px) {{
      .tabs {{ flex-wrap: wrap; overflow-x: visible; }}
      .header-row {{ flex-wrap: wrap; }}
    }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #111716; color: #e7ece9; }}
      header, article, .chart-card, .table-wrap, input, select, button {{ background: #18201f; border-color: #2c3936; }}
      th {{ background: #202b29; color: #aebbb6; }}
      td {{ border-color: #2c3936; }}
      p, article small, article span, .pill {{ color: #aebbb6; }}
      a {{ color: #8ec7ff; }}
      .chart-meta {{ color: #b9c7c2; }}
      .axis {{ fill: #d5e0dc; }}
      .grid {{ stroke: #51625e; }}
      .line {{ stroke: #55d6ff; }}
      .health.ok {{ color: #4ade80; }}
      .health.warn {{ color: #fbbf24; }}
      .health.bad {{ color: #f87171; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-row">
      <div>
        <h1>Market Watch Live</h1>
        <p>Updated {html.escape(local_now_text())} {health_badge}</p>
      </div>
      <div class="header-actions">
        <button type="button" class="send-test" id="sendTestEmail">Send Test Email</button>
        <span class="send-status" id="sendStatus"></span>
      </div>
    </div>
    <nav class="tabs" aria-label="Dashboard sections">
      <button type="button" class="tab active" data-tab="macro">Macro</button>
      <button type="button" class="tab" data-tab="stock">Stock</button>
      <button type="button" class="tab" data-tab="charts">Charts</button>
      <button type="button" class="tab" data-tab="news">News</button>
      <button type="button" class="tab" data-tab="alerts">All Alerts</button>
    </nav>
  </header>
  <main>
    <h2 class="section-title" id="sectionTitle">Macro</h2>
    <section class="snapshots">{''.join(snapshot_cards) if snapshot_cards else '<article><h2>No snapshots yet</h2><small>Run a poll to populate live indicators.</small></article>'}</section>
    <section class="charts" id="charts"></section>
    <section class="controls">
      <input id="search" type="search" placeholder="Search alerts, symbols, sources">
      <select id="category"><option value="">All categories</option>{category_options}</select>
      <select id="severity"><option value="">All severity</option><option value="high">High</option><option value="medium">Medium</option></select>
      <select id="score"><option value="0">Score >= 0</option><option value="55">Score >= 55</option><option value="70">Score >= 70</option><option value="85">Score >= 85</option></select>
    </section>
    <p class="muted" id="count"></p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Time</th><th>Cat</th><th>Severity</th><th>Score</th><th>Title</th><th>Summary</th><th>Value</th><th>Source</th><th>Link</th><th>Emailed</th><th>Feedback</th></tr></thead>
        <tbody>{''.join(rows) if rows else '<tr><td colspan="11">No alerts yet.</td></tr>'}</tbody>
      </table>
    </div>
  </main>
  <script>
    let activeTab = 'macro';
    const titles = {{macro: 'Macro', stock: 'Stock Watchlist', charts: 'Charts', news: 'News Alerts', alerts: 'All Alerts'}};
    const chartData = {charts_json};
    const rows = [...document.querySelectorAll('tbody tr[data-category]')];
    const cards = [...document.querySelectorAll('.snapshot[data-section]')];
    const charts = document.querySelector('#charts');
    const tabs = [...document.querySelectorAll('.tab')];
    const search = document.querySelector('#search');
    const category = document.querySelector('#category');
    const severity = document.querySelector('#severity');
    const score = document.querySelector('#score');
    const count = document.querySelector('#count');
    const sectionTitle = document.querySelector('#sectionTitle');
    const sendTestEmail = document.querySelector('#sendTestEmail');
    const sendStatus = document.querySelector('#sendStatus');
    function renderCharts() {{
      if (!charts || charts.dataset.rendered) return;
      charts.dataset.rendered = '1';
      charts.innerHTML = chartData.length ? chartData.map(series => {{
        const points = series.points || [];
        if (points.length < 2) {{
          return `<div class="chart-card"><h3>${{series.name}}</h3><p class="muted">Need at least two polling points to draw a chart.</p></div>`;
        }}
        const values = points.map(p => Number(p.value));
        const min = Math.min(...values);
        const max = Math.max(...values);
        const pad = (max - min) || 1;
        const coords = points.map((p, i) => {{
          const x = 36 + (i / Math.max(points.length - 1, 1)) * 300;
          const y = 178 - ((Number(p.value) - min) / pad) * 140;
          return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
        }}).join(' ');
        const last = values[values.length - 1];
        const first = values[0];
        const change = last - first;
        const pct = first ? (change / first) * 100 : 0;
        const startLabel = points[0].time.slice(0, 10);
        const endLabel = points[points.length - 1].time.slice(0, 10);
        return `<div class="chart-card"><h3>${{series.name}}</h3>
          <div class="chart-meta">${{startLabel}} to ${{endLabel}} · last ${{last.toFixed(2)}} · ${{change >= 0 ? '+' : ''}}${{change.toFixed(2)}} (${{pct >= 0 ? '+' : ''}}${{pct.toFixed(2)}}%)</div>
          <svg viewBox="0 0 360 210" role="img" aria-label="${{series.name}} chart">
            <line class="grid" x1="36" y1="178" x2="342" y2="178"/>
            <line class="grid" x1="36" y1="104" x2="342" y2="104" opacity=".45"/>
            <line class="grid" x1="36" y1="30" x2="342" y2="30" opacity=".45"/>
            <line class="grid" x1="36" y1="30" x2="36" y2="178"/>
            <polyline class="line" points="${{coords}}"/>
            <circle cx="${{coords.split(' ').pop().split(',')[0]}}" cy="${{coords.split(' ').pop().split(',')[1]}}" r="4" fill="currentColor"/>
            <text x="36" y="18" class="axis">max ${{max.toFixed(2)}}</text>
            <text x="36" y="202" class="axis">min ${{min.toFixed(2)}}</text>
            <text x="252" y="18" class="axis">last ${{last.toFixed(2)}}</text>
          </svg></div>`;
      }}).join('') : '<div class="chart-card"><h3>No chart data yet</h3><p class="muted">Run the monitor for at least two polling cycles.</p></div>';
    }}
    function applyFilters() {{
      const q = search.value.trim().toLowerCase();
      const cat = category.value;
      const sev = severity.value;
      const minScore = Number(score.value || 0);
      let visible = 0;
      let visibleCards = 0;
      if (activeTab === 'charts') renderCharts();
      charts.style.display = activeTab === 'charts' ? 'grid' : 'none';
      cards.forEach(card => {{
        const ok = activeTab === 'alerts' ? card.dataset.section !== 'stock' : activeTab !== 'charts' && card.dataset.section === activeTab;
        card.style.display = ok ? '' : 'none';
        if (ok) visibleCards += 1;
      }});
      rows.forEach(row => {{
        const tabOk = activeTab === 'alerts' || row.dataset.section === activeTab;
        const ok = tabOk &&
          (!q || row.dataset.text.includes(q)) &&
          (!cat || row.dataset.category === cat) &&
          (!sev || row.children[2].textContent.trim() === sev) &&
          Number(row.dataset.score || 0) >= minScore;
        row.style.display = ok ? '' : 'none';
        if (ok) visible += 1;
      }});
      sectionTitle.textContent = titles[activeTab] || 'Dashboard';
      count.textContent = `${{visibleCards}} cards, ${{visible}} visible alerts`;
    }}
    tabs.forEach(tab => tab.addEventListener('click', () => {{
      activeTab = tab.dataset.tab;
      tabs.forEach(item => item.classList.toggle('active', item === tab));
      applyFilters();
    }}));
    [search, category, severity, score].forEach(el => el.addEventListener('input', applyFilters));
    document.querySelectorAll('button.quiet').forEach(button => {{
      button.addEventListener('click', async () => {{
        button.disabled = true;
        const body = new URLSearchParams({{id: button.dataset.id, kind: 'not_important'}});
        const res = await fetch('/feedback', {{method: 'POST', body}});
        button.textContent = res.ok ? 'Saved' : 'Failed';
      }});
    }});
    sendTestEmail.addEventListener('click', async () => {{
      sendTestEmail.disabled = true;
      sendStatus.textContent = 'Sending...';
      try {{
        const res = await fetch('/send-test-email', {{method: 'POST'}});
        const data = await res.json();
        sendStatus.textContent = data.ok ? 'Sent' : (data.error || 'Failed');
      }} catch (err) {{
        sendStatus.textContent = 'Failed';
      }} finally {{
        setTimeout(() => {{
          sendTestEmail.disabled = false;
          if (sendStatus.textContent === 'Sent') sendStatus.textContent = '';
        }}, 3500);
      }}
    }});
    applyFilters();
    setTimeout(() => location.reload(), 60000);
  </script>
</body>
</html>
"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB
    config_path: Path = DEFAULT_CONFIG

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path not in ("/", "/dashboard"):
            self.send_error(404)
            return
        conn = init_db(self.db_path)
        try:
            body = web_dashboard_html(conn).encode("utf-8")
        finally:
            conn.close()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path == "/send-test-email":
            conn = init_db(self.db_path)
            try:
                config = load_config(self.config_path)
                sent = send_test_email(config, conn)
                payload = json.dumps({"ok": bool(sent)}).encode("utf-8")
                status = 200 if sent else 500
            except Exception as exc:
                payload = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                status = 500
            finally:
                conn.close()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path != "/feedback":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        data = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        alert_id = data.get("id", [""])[0]
        kind = data.get("kind", ["not_important"])[0]
        conn = init_db(self.db_path)
        try:
            row = conn.execute("SELECT title FROM alerts WHERE id = ?", (alert_id,)).fetchone()
            if not row:
                self.send_error(404)
                return
            phrase = save_feedback(conn, kind, row[0])
            conn.execute("UPDATE alerts SET score = 0, severity = 'low' WHERE id = ?", (alert_id,))
            conn.commit()
        finally:
            conn.close()
        payload = json.dumps({"ok": True, "phrase": phrase}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_dashboard_server(db_path: Path, port: int, config_path: Path = DEFAULT_CONFIG) -> Any:
    handler = type("MarketWatchDashboardHandler", (DashboardHandler,), {"db_path": db_path, "config_path": config_path})
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        print(f"[warn] Dashboard server not started on port {port}: {exc}")
        print(f"[warn] If http://127.0.0.1:{port} already works, another Market Watch window is probably running.")
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Dashboard server: http://127.0.0.1:{port}")
    return server


def collect_alerts(config: dict[str, Any], conn: sqlite3.Connection | None = None) -> tuple[list[Alert], list[Snapshot]]:
    alerts: list[Alert] = []
    snapshots: list[Snapshot] = []
    alerts.extend(scan_news(config, conn))
    alerts.extend(scan_stock_catalyst_news(config, conn))
    market_alerts, market_snapshots = scan_market_data(config)
    earnings_calendar = load_earnings_calendar(config, conn) if config.get("equity_watchlist", {}).get("show_earnings_date", True) else {}
    stock_alerts, stock_snapshots = scan_equity_watchlist(config, earnings_calendar)
    fomc_alerts, fomc_snapshots = scan_fomc_calendar(config)
    econ_alerts, econ_snapshots = scan_econ_calendar(config, conn)
    fed_alerts, fed_snapshots = scan_fed(config)
    alerts.extend(market_alerts)
    alerts.extend(stock_alerts)
    alerts.extend(fomc_alerts)
    alerts.extend(econ_alerts)
    alerts.extend(fed_alerts)
    snapshots.extend(market_snapshots)
    snapshots.extend(stock_snapshots)
    snapshots.extend(fomc_snapshots)
    snapshots.extend(econ_snapshots)
    snapshots.extend(fed_snapshots)
    if conn is not None:
        alerts.extend(earnings_alerts(config, conn))
    return alerts, snapshots


def is_emailable(alert: Alert, alerting_cfg: dict[str, Any]) -> bool:
    min_email_score = int(alerting_cfg.get("min_email_score", 55))
    stock_price_move_min = int(alerting_cfg.get("stock_price_move_email_score", 85))
    if alert.category == "stock" and "earnings calendar" in alert.source.lower():
        return True
    if alert.category == "stock" and alert.source == "Yahoo Finance":
        return alert.score >= stock_price_move_min or alert.severity == "high"
    return alert.score >= min_email_score or alert.severity == "high"


def filter_email_alerts(alerts: list[Alert], alerting_cfg: dict[str, Any]) -> list[Alert]:
    return [alert for alert in alerts if is_emailable(alert, alerting_cfg)]


def pending_email_alerts(conn: sqlite3.Connection, config: dict[str, Any], hours: int = 12) -> list[tuple[int, Alert]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = conn.execute(
        """
        SELECT id, category, title, severity, summary, url, value, source, score
        FROM alerts
        WHERE emailed = 0 AND created_at >= ?
        ORDER BY score DESC, id DESC
        LIMIT 50
        """,
        (since,),
    ).fetchall()
    alerting_cfg = config.get("alerting", {})
    pairs: list[tuple[int, Alert]] = []
    for alert_id, category, title, severity, summary, url, value, source, score in rows:
        alert = Alert(
            category=category,
            title=title,
            severity=severity,
            summary=summary,
            url=url or "",
            value=value or "",
            source=source or "",
            score=int(score or 0),
        )
        if is_emailable(alert, alerting_cfg):
            pairs.append((int(alert_id), alert))
    return pairs


def mark_alerts_emailed(conn: sqlite3.Connection, alert_ids: list[int]) -> None:
    if not alert_ids:
        return
    placeholders = ",".join("?" * len(alert_ids))
    conn.execute(f"UPDATE alerts SET emailed = 1 WHERE id IN ({placeholders})", alert_ids)
    conn.commit()


def minutes_since_state(conn: sqlite3.Connection, key: str) -> float:
    text = get_state(conn, key)
    if not text:
        return float("inf")
    try:
        then = datetime.strptime(text, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


def send_alert_emails_if_due(config: dict[str, Any], conn: sqlite3.Connection) -> bool:
    """Email policy: critical alerts go out immediately; everything else is
    batched into at most one email per batch window. Remaining items land in
    the scheduled digests."""
    alerting_cfg = config.get("alerting", {})
    pairs = pending_email_alerts(conn, config)
    if not pairs:
        return False
    critical_score = int(alerting_cfg.get("critical_email_score", 85))
    batch_minutes = int(alerting_cfg.get("batch_email_minutes", 60))
    critical = [alert for _, alert in pairs if alert.score >= critical_score]
    elapsed = minutes_since_state(conn, "last_alert_email_at")
    if not critical and elapsed < batch_minutes:
        print(f"[info] {len(pairs)} alert(s) queued; next batch email in about {max(1, int(batch_minutes - elapsed))} min.")
        return False
    alerts = [alert for _, alert in pairs]
    top = max(alerts, key=lambda alert: alert.score)
    top_title = top.title if len(top.title) <= 70 else top.title[:67] + "..."
    base_subject = config.get("email", {}).get("subject", "Market Watch")
    extra = f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""
    prefix = "\U0001f6a8 " if critical else ""
    subject = f"{prefix}{base_subject}: {top_title}{extra}"
    body_title = "Critical Alert" if critical else "Market Update"
    text_body, html_body = build_email_body(body_title, alerts, snapshots_from_db(conn))
    if not send_email(config, subject, text_body, html_body):
        return False
    mark_alerts_emailed(conn, [alert_id for alert_id, _ in pairs])
    set_state(conn, "last_alert_email_at", utc_now())
    return True


def recent_alerts(conn: sqlite3.Connection, hours: int) -> list[Alert]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = conn.execute(
        """
        SELECT category, title, severity, summary, url, value, source, score
        FROM alerts
        WHERE created_at >= ?
        ORDER BY score DESC, id DESC
        LIMIT 50
        """,
        (since,),
    ).fetchall()
    return [
        Alert(
            category=category,
            title=title,
            severity=severity,
            summary=summary,
            url=url or "",
            value=value or "",
            source=source or "",
            score=int(score or 0),
        )
        for category, title, severity, summary, url, value, source, score in rows
    ]


def digest_due(config: dict[str, Any], conn: sqlite3.Connection) -> str:
    digest_cfg = config.get("digest", {})
    if not digest_cfg.get("enabled", True):
        return ""
    now = local_now()
    today = date.today().isoformat()
    due_window_minutes = int(digest_cfg.get("due_window_minutes", 5))
    for item in digest_cfg.get("times_local", []):
        name = item.get("name", "digest")
        time_text = item.get("time", "")
        if not re.match(r"^\d{2}:\d{2}$", time_text):
            continue
        hour, minute = [int(part) for part in time_text.split(":")]
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta_minutes = abs((now - scheduled).total_seconds()) / 60
        state_key = f"digest:{name}:{today}"
        if delta_minutes <= due_window_minutes and get_state(conn, state_key) != "sent":
            return name
    return ""


def send_digest_if_due(config: dict[str, Any], conn: sqlite3.Connection) -> bool:
    name = digest_due(config, conn)
    if not name:
        return False
    digest_cfg = config.get("digest", {})
    hours = int(digest_cfg.get("lookback_hours", 12))
    alerts = filter_email_alerts(recent_alerts(conn, hours), config.get("alerting", {}))
    title = f"{name.replace('_', ' ').title()} Brief"
    subject = f"{title} · {local_now().strftime('%b %d')}"
    text_body, html_body = build_email_body(title, alerts, snapshots_from_db(conn))
    sent = send_email(config, subject, text_body, html_body)
    if sent:
        set_state(conn, f"digest:{name}:{date.today().isoformat()}", "sent")
        mark_alerts_emailed(conn, [alert_id for alert_id, _ in pending_email_alerts(conn, config)])
        set_state(conn, "last_alert_email_at", utc_now())
    return sent


def send_test_email(config: dict[str, Any], conn: sqlite3.Connection) -> bool:
    recent = recent_alerts(conn, int(config.get("digest", {}).get("lookback_hours", 12)))
    alerts = filter_email_alerts(recent, config.get("alerting", {}))
    if not alerts:
        alerts = recent[:5]
    title = "Market Watch Test Email"
    text_body, html_body = build_email_body(title, alerts, snapshots_from_db(conn))
    subject = f"{config.get('email', {}).get('subject', 'Market Watch Alert')}: Test"
    return send_email(config, subject, text_body, html_body)


def run_once(config: dict[str, Any], conn: sqlite3.Connection, dashboard: Path) -> int:
    collected_alerts, snapshots = collect_alerts(config, conn)
    for snapshot in snapshots:
        save_snapshot(conn, snapshot)
    alerts = [alert for alert in collected_alerts if not seen_alert(conn, alert, config)]
    for alert in alerts:
        save_alert(conn, alert, False)
    try:
        send_alert_emails_if_due(config, conn)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"[warn] Email send failed: {exc}", file=sys.stderr)
    set_state(conn, "last_poll_at", utc_now())
    set_state(conn, "poll_interval_seconds", str(int(config.get("poll_interval_seconds", 300))))
    write_dashboard(dashboard, conn)
    print(f"[{utc_now()}] collected {len(alerts)} new alert(s); dashboard: {dashboard}")
    return len(alerts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor market-moving events and send email alerts.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Path to config JSON.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to SQLite database.")
    parser.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD, help="Path to generated dashboard HTML.")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit.")
    parser.add_argument("--serve", action="store_true", help="Serve the live dashboard and exit without polling.")
    parser.add_argument("--import-history", action="store_true", help="Import historical market chart data and exit.")
    parser.add_argument("--history-days", type=int, default=180, help="Days of market history to import.")
    parser.add_argument("--send-test-email", action="store_true", help="Send a test email using current dashboard data and exit.")
    args = parser.parse_args()

    config = load_config(args.config)
    conn = init_db(args.db)
    if args.import_history:
        imported = import_historical_market_data(config, conn, args.history_days)
        print(f"Imported {imported} historical market data points.")
        return 0
    if args.send_test_email:
        sent = send_test_email(config, conn)
        print("Sent test email." if sent else "Test email was not sent.")
        return 0
    dashboard_cfg = config.get("dashboard", {})
    dashboard_enabled = bool(dashboard_cfg.get("enabled", True))
    dashboard_port = int(dashboard_cfg.get("port", 8765))
    if args.serve:
        server = start_dashboard_server(args.db, dashboard_port, args.config)
        if server is None:
            return 1
        print("Press Ctrl+C to stop the dashboard server.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    interval = int(config.get("poll_interval_seconds", 300))
    if args.once:
        run_once(config, conn, args.dashboard)
        return 0

    if dashboard_enabled:
        start_dashboard_server(args.db, dashboard_port, args.config)
    print(f"Market Watch running. Poll interval: {interval}s. Press Ctrl+C to stop.")
    while True:
        try:
            run_once(config, conn, args.dashboard)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log_exception("Polling cycle failed; monitor will continue", exc)
        try:
            send_digest_if_due(config, conn)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log_exception("Digest send failed; monitor will continue", exc)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
