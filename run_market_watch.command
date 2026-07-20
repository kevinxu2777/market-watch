#!/bin/bash
set -e

cd "$(dirname "$0")"

touch market_watch.log
exec > >(tee -a market_watch.log) 2>&1

if [ -f .env.local ]; then
  set -a
  . ./.env.local
  set +a
fi

export SMTP_HOST="${SMTP_HOST:-smtp.gmail.com}"
export SMTP_USERNAME="${SMTP_USERNAME:-${MARKET_WATCH_EMAIL:-}}"
export SMTP_FROM="${SMTP_FROM:-$SMTP_USERNAME}"
export ALERT_EMAIL_TO="${ALERT_EMAIL_TO:-$SMTP_USERNAME}"
export PYTHONUNBUFFERED=1

SERVICE="Market Watch Tool Gmail SMTP"
ACCOUNT="$SMTP_USERNAME"

echo "Market Watch Tool"
if [ -z "$ACCOUNT" ]; then
  echo "Set SMTP_USERNAME or MARKET_WATCH_EMAIL in .env.local first."
  exit 1
fi
SMTP_PASSWORD="$(security find-generic-password -a "$ACCOUNT" -s "$SERVICE" -w 2>/dev/null || true)"
if [ -z "$SMTP_PASSWORD" ]; then
  echo "No Gmail App Password found in macOS Keychain."
  echo "Run setup_gmail_password.command first."
  exit 1
fi
export SMTP_PASSWORD
echo "Starting monitor. Press Ctrl+C to stop."
echo "Live dashboard: http://127.0.0.1:8765"
if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
else
  PYTHON=python3
fi
if [ -f config.local.json ]; then
  echo "Using config.local.json"
  exec "$PYTHON" market_watch.py --config config.local.json
else
  exec "$PYTHON" market_watch.py
fi
