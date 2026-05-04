#!/usr/bin/env bash
# Shared driver for GSC URL Inspection auditor. Per-site instances in
# nsc-assistant/agents/<site>-gsc-coverage-auditor/ exec this with
# GSC_INSPECT_SITE=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${GSC_COVERAGE_LOG:-/tmp/reusable-agents-gsc-coverage.log}"
{
  echo "── $(date -u +%Y-%m-%dT%H:%M:%SZ) — gsc-coverage-auditor (site=${GSC_INSPECT_SITE:-?}) ──"
  python3 "$HERE/inspect.py" "$@" \
    || echo "(gsc-coverage-auditor failed or timed out)"
  echo ""
} >> "$LOG" 2>&1

# Cap log size
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5000000 ]; then
  tail -c 3000000 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
