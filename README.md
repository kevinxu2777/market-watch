<div align="center">

# 📈 Market Watch

**A local-first US market monitor — news, macro events, and your watchlist, scored and delivered to your inbox.**

![Python](https://img.shields.io/badge/Python-3-3776AB?style=flat-square&logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/dependencies-none-success?style=flat-square)
![Tests](https://img.shields.io/badge/tests-9%20passing-brightgreen?style=flat-square)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey?style=flat-square)
![Storage](https://img.shields.io/badge/storage-SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white)

**English** · [简体中文](README-CN.md)

</div>

<div align="center">

⚡ [Quick Start](#-quick-start) &nbsp;|&nbsp; 📬 [Email Alerts](#-email-alerts) &nbsp;|&nbsp; 🔧 [Configuration](#-configuration) &nbsp;|&nbsp; 🔍 [What It Watches](#-what-it-watches) &nbsp;|&nbsp; 🔇 [Noise Control](#-noise-control) &nbsp;|&nbsp; 🚀 [Autostart](#-autostart-macos)

</div>

---

Market Watch tracks global financial news, macro and geopolitical events, index/commodity indicators (equity futures, VIX, Treasury yields, oil, gold, the dollar index) and your own stock watchlist. Every event gets a score; the important ones reach you by email. Everything lands in a local SQLite database and a live web dashboard.

Written in pure Python standard library — **zero third-party dependencies, no `pip install` required.**

## ✨ Features

| | |
|---|---|
| 📰 **News scanning** | Multiple RSS feeds scored by weighted keywords; low-scoring and duplicate coverage filtered out automatically |
| 📊 **Indices & commodities** | S&P/Nasdaq futures, VIX, 10-year Treasury, crude, gold, dollar index — alerts on threshold breaches |
| 📈 **Stock watchlist** | Price moves, earnings-date countdowns, bull/bear catalyst news (government contracts, rating changes, litigation…) |
| 🗓️ **Macro calendar** | FOMC schedule plus advance reminders for CPI/PPI/PCE/Nonfarm Payrolls/GDP releases |
| 📬 **Tiered email** | Critical events sent immediately, routine alerts batched hourly, low-score items only in the daily digest |
| 🖥️ **Live dashboard** | `http://127.0.0.1:8765` with search, filters, charts, a health indicator, and one-click noise feedback |

## ⚡ Quick Start

```bash
git clone https://github.com/kevinxu2777/market-watch.git
cd market-watch

# Single pass (no configuration needed)
python3 market_watch.py --once

# Continuous monitoring (polls every 300s by default, starts the dashboard)
python3 market_watch.py
```

This produces:

- `market_watch.sqlite3` — alert history, deduplication state, metric history
- `dashboard.html` — a redirect page; the real dashboard runs at `http://127.0.0.1:8765`

The dashboard header carries a **health indicator**: green means polling is healthy; it turns red when no successful poll has happened in more than twice the poll interval, so a silently dead monitor is visible. The dashboard also supports search, category/score filters, a `Not important` noise-feedback button, and a `Send Test Email` button.

Run the tests:

```bash
python3 -m unittest test_market_watch
```

## 📬 Email Alerts

Email is optional — without it, alerts still reach the dashboard. To enable it, create `.env.local` in the project directory (gitignored, never committed):

```bash
SMTP_USERNAME="your_email@gmail.com"
# Optional (defaults shown):
# SMTP_HOST="smtp.gmail.com"
# SMTP_FROM="$SMTP_USERNAME"
# ALERT_EMAIL_TO="$SMTP_USERNAME"     # comma-separate multiple recipients
```

Gmail users need an [App Password](https://myaccount.google.com/apppasswords) (not your account password), then run once:

```bash
./setup_gmail_password.command   # stores the password in the macOS Keychain
```

Verify delivery works:

```bash
./send_test_email.command
```

You can also skip `.env.local` and the Keychain entirely by exporting `SMTP_USERNAME` / `SMTP_PASSWORD` as environment variables before running `python3 market_watch.py`.

### Delivery strategy

Alerts are graded into three tiers, so you are not emailed on every poll:

1. **Critical** (score ≥ `alerting.critical_email_score`, default 85) — sent immediately, subject prefixed 🚨, exempt from cooldown
2. **Routine** (score ≥ `alerting.min_email_score`, default 55) — queued and batched into one email at most every `alerting.batch_email_minutes` (default 60); a critical event flushes the queue along with it
3. **Low score** — dashboard and daily digest only

The daily digest runs three times by default (`digest.times_local`): `08:30` pre-market, `12:00` midday, `16:15` after the close, each containing a market snapshot plus high-scoring events from the last 12 hours. Sending a digest clears the pending queue, so no alert appears twice. Set `digest.enabled` to `false` to turn digests off.

## 🔧 Configuration

```bash
cp config.example.json config.local.json
# Edit config.local.json — the .command launchers prefer it automatically (gitignored)
```

From the CLI, point at it explicitly: `python3 market_watch.py --config config.local.json`

Commonly tuned keys:

| Key | Purpose |
|---|---|
| `poll_interval_seconds` | Poll interval, default 300s |
| `news.feeds` / `keywords` / `keyword_weights` | RSS sources, trigger keywords and their weights |
| `news.source_weights` | Boost more trustworthy news sources |
| `news.suppress_keywords` | Blocklist (personal finance, travel, shopping…) |
| `news.min_score` | Minimum score to store a story, default 35 |
| `news.topic_cooldown_hours` | Per-topic cooldown, default 4 hours |
| `market_data.instruments` | Index/commodity symbols and move thresholds |
| `equity_watchlist.stocks` | Your stocks (name, aliases, themes, per-symbol thresholds) |
| `econ_calendar.events` | Macro releases to track (CPI, Nonfarm Payrolls…) |
| `econ_calendar.horizon_days` | How far ahead to look for releases, default 40 days |
| `fed.series` | FRED series and alert bounds |
| `alerting.*` | Email score thresholds and batching interval |

## 🔍 What It Watches

### Stock watchlist

Ships watching MAG7 (AAPL/MSFT/GOOGL/AMZN/META/NVDA/TSLA) and AI-theme names (AVGO/AMD/TSM/MU/SMCI/PLTR/ARM/ORCL/ADBE), refreshing price, daily move and next earnings date on every poll.

- **Price moves** — anything beyond `daily_move_alert_pct` (default 3%, overridable per symbol) creates an alert; ordinary moves stay on the dashboard, and only moves scoring at least `stock_price_move_email_score` (default 85) trigger email
- **Catalyst news** — watchlist news matching bull/bear keywords is prioritized for email and tagged Bullish/Bearish. Bullish covers government contracts, supply agreements, large orders, raised guidance, analyst upgrades; Bearish covers investigations, litigation, antitrust, downgrades, export restrictions, SEC charges, cyberattacks
- **Earnings reminders** — email once a symbol enters the `earnings_email_days_before` window (default 7 days). Dates come from the Nasdaq earnings calendar (cached daily); setting `FMP_API_KEY` ([Financial Modeling Prep](https://financialmodelingprep.com)) improves coverage further out. `not confirmed` means the public calendar has no confirmed date yet — not a bug. You can also pin dates manually:

```json
"earnings_overrides": {
  "AAPL": "2026-07-30 after hours, manual"
}
```

### FOMC and macro calendar

- FOMC dates come from the Federal Reserve website, with reminders 7/2/1/0 days ahead by default; the dashboard shows a countdown to the next decision
- CPI, Core CPI, PPI, PCE, Nonfarm Payrolls, Unemployment Rate, GDP and Retail Sales schedules come from the Nasdaq economic calendar, with reminders the day before (medium) and on the day (high, queued for email immediately). The dashboard's Macro tab shows `Next CPI`-style countdown cards. The lookahead window is **40 days** by default and results are cached per day — the window has to exceed the ~31-day monthly cadence, otherwise a release falls outside it between prints and its card goes stale

### Federal Reserve rates (optional)

With a [FRED API key](https://fred.stlouisfed.org/docs/api/api_key.html) configured, it tracks EFFR (effective federal funds rate) and SOFR, alerting when either leaves the configured band:

```bash
export FRED_API_KEY="your_fred_key"
```

## 🔇 Noise Control

- **Score filtering** — keyword weights + source weights + breaking/market-relevance bonuses; anything below `news.min_score` is never stored
- **Topic cooldown** — the same keyword combination ("the same event, reworded") alerts only once per `topic_cooldown_hours` (default 4). A score exceeding the previous one by more than `topic_escalation_score` (default 15) counts as escalation and still gets through
- **Move deduplication** — the same symbol moving the same direction alerts only once per 12 hours; a reversal counts as a new event
- **Human feedback** — every dashboard alert has a `Not important` button, and similar stories are down-weighted afterwards

## 📉 Charts

The dashboard's Charts tab plots history for every index/commodity metric, built from readings recorded on each poll (two polls minimum before a line appears). To backfill quickly:

```bash
python3 market_watch.py --import-history --history-days 180
```

## 🚀 Autostart (macOS)

Create `~/Library/LaunchAgents/com.yourname.market-watch.plist`, replacing both paths with your clone location:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.yourname.market-watch</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/path/to/market-watch/run_market_watch.command</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>/path/to/market-watch/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/path/to/market-watch/launchd.log</string>
</dict>
</plist>
```

```bash
# Load (starts at login, restarts on crash)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yourname.market-watch.plist

# Check status / stop
launchctl print gui/$(id -u)/com.yourname.market-watch | grep state
launchctl bootout gui/$(id -u)/com.yourname.market-watch
```

Early startup errors go to `launchd.log`; runtime logs go to `market_watch.log`. After changing code or config, bootout and bootstrap again for it to take effect.

## 📁 Files

| File | Purpose |
|---|---|
| `market_watch.py` | Main program (single file, pure standard library) |
| `test_market_watch.py` | Unit tests |
| `config.example.json` | Default config; copy to `config.local.json` to customize |
| `run_market_watch.command` | Double-click launcher (reads `.env.local` + Keychain) |
| `setup_gmail_password.command` | One-time Gmail App Password → Keychain |
| `send_test_email.command` | Send a test email to verify configuration |
| `weekly_leap_review.py` | The author's personal weekly-report script; depends on a `trading-agent` project outside this repo and can be ignored |

## 🔣 Common Yahoo Finance Symbols

Crude `CL=F` · Gold `GC=F` · S&P futures `ES=F` · Nasdaq futures `NQ=F` · VIX `^VIX` · 10-year Treasury `^TNX` · Dollar index `DX-Y.NYB` · Bitcoin `BTC-USD`

## ⚠️ Notes

Data comes from public endpoints (RSS, Yahoo Finance, Nasdaq, the Federal Reserve website, FRED); availability depends on your network and each service's terms. All HTTPS requests verify certificates fully. If your local Python reports certificate errors, fix the certificates or point `SSL_CERT_FILE` at a trusted CA bundle — use `MARKET_WATCH_INSECURE_SSL=1` only for temporary local debugging, never permanently.

This tool is built for early warning and information aggregation. It should not be your sole basis for trading decisions.
