#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/.tmp/logs"
mkdir -p "$LOG_DIR"

CRON_EXPR="${1:-0 9 * * *}"
CMD="cd \"$ROOT_DIR\" && /usr/bin/env python3 tools/run_phase5_trigger.py >> \"$LOG_DIR/phase5_cron.log\" 2>&1"

CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
if echo "$CURRENT_CRON" | grep -F "$CMD" >/dev/null 2>&1; then
  echo "Cron entry already exists:"
  echo "$CRON_EXPR $CMD"
  exit 0
fi

{
  echo "$CURRENT_CRON"
  echo "$CRON_EXPR $CMD"
} | crontab -

echo "Installed cron entry:"
echo "$CRON_EXPR $CMD"
