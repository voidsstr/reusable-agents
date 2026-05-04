#!/usr/bin/env bash
# Site-agnostic IndexNow submission — runs every 15 minutes via crontab.
# Pings Bing/Yandex/Seznam/Naver for every site configured in sites.json.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

LOG="${INDEXNOW_LOG:-/tmp/reusable-agents-indexnow.log}"
# Use the SpecPicks app's node_modules — it has ts-node + pg installed.
# (Was AislePrompt's previously, but that dir's node_modules was reorganized
# and no longer has pg locally; specpicks has both reliably.)
APP_DIR="/home/voidsstr/development/specpicks"
{
  echo "── $(date -u +%Y-%m-%dT%H:%M:%SZ) — indexnow submit ──"
  ( cd "$APP_DIR" && \
    NODE_PATH="$APP_DIR/node_modules" \
    timeout 600 npx ts-node --transpile-only --compiler-options '{"module":"node16","moduleResolution":"node16","esModuleInterop":true,"skipLibCheck":true,"resolveJsonModule":true}' "$HERE/submit.ts" "$@" ) 2>&1 \
    || echo "(indexnow submit failed or timed out)"
  echo ""
} >> "$LOG" 2>&1

# Cap log size at ~5MB
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5000000 ]; then
  tail -c 3000000 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
