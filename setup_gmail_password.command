#!/bin/bash
set -e

SERVICE="Market Watch Tool Gmail SMTP"

if [ -f .env.local ]; then
  set -a
  . ./.env.local
  set +a
fi

ACCOUNT="${SMTP_USERNAME:-${MARKET_WATCH_EMAIL:-}}"
if [ -z "$ACCOUNT" ]; then
  read -r -p "Gmail address: " ACCOUNT
fi

echo "Market Watch Gmail Password Setup"
echo "Paste your Gmail App Password once, then press Enter."
echo "Input will be hidden and saved to macOS Keychain."
read -rs SMTP_PASSWORD
echo

if [ -z "$SMTP_PASSWORD" ]; then
  echo "Password was empty. Nothing saved."
  exit 1
fi

security add-generic-password \
  -a "$ACCOUNT" \
  -s "$SERVICE" \
  -w "$SMTP_PASSWORD" \
  -U

echo "Saved to macOS Keychain."
echo "You can now run run_market_watch.command without pasting the password each time."
