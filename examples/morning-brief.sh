#!/usr/bin/env bash
# Example cron job: run a headless Grok prompt and push the result to Telegram.
# Install (e.g. 7:00 weekdays):
#   0 7 * * 1-5 /home/nick/grok-telegram/examples/morning-brief.sh
set -euo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:$PATH"
GROK_BIN="${GROK_BIN:-$(command -v grok || true)}"
if [ -z "$GROK_BIN" ]; then
  echo "grok not found" | tg-push-grok --title "Morning brief FAILED"
  exit 1
fi

OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

if ! "$GROK_BIN" -p "Write a concise morning brief: markets, calendar risks, and anything I should check today. Keep it under 400 words." \
  --output-format plain --always-approve --cwd "$HOME" >"$OUT" 2>&1; then
  tg-push-grok --title "Morning brief FAILED" --file "$OUT" || true
  exit 1
fi

tg-push-grok --title "Morning brief" --file "$OUT"
