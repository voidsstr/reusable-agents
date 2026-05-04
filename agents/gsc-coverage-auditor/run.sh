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

# Record metrics from a cwd OUTSIDE this dir so Python's stdlib `inspect`
# module isn't shadowed by our local inspect.py. (The inspect.py script
# has its own metric block but it can't import metric_helper because the
# local file shadows the stdlib `inspect` that framework.core imports.)
SITE="${GSC_INSPECT_SITE:-}"
if [ -n "$SITE" ]; then
  ( cd /tmp && python3 - <<PY 2>>"$LOG"
import json, os, sys
sys.path.insert(0, "/home/voidsstr/development/reusable-agents")
from datetime import datetime, timedelta, timezone
from pathlib import Path
from framework.core import metric_helper
site = "${SITE}"
agent_id = f"{site}-gsc-coverage-auditor"
coverage = Path(os.path.expanduser(f"~/.reusable-agents/gsc-coverage-auditor/{site}-coverage.jsonl"))
if not coverage.is_file():
    print(f"[metric] no coverage file at {coverage}"); sys.exit(0)
cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
n_recent = 0
latest = {}
with coverage.open() as fh:
    for raw in fh:
        try: r = json.loads(raw)
        except Exception: continue
        url = r.get("url"); ts = r.get("inspected_at", "")
        if not url: continue
        if ts >= cutoff: n_recent += 1
        prev = latest.get(url, ("", ""))
        if ts > prev[1]: latest[url] = (r.get("coverageState",""), ts)
total = max(len(latest), 1)
n_idx = sum(1 for cs,_ in latest.values() if cs == "Submitted and indexed")
n_unk = sum(1 for cs,_ in latest.values() if cs == "URL is unknown to Google")
metrics = {
    "goal-urls-inspected-7d": float(n_recent),
    "goal-indexed-pct": round(100.0 * n_idx / total, 2),
    "goal-unknown-pct": round(100.0 * n_unk / total, 2),
}
metric_helper.record_many(agent_id, metrics,
    run_ts=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    note=f"recorded by gsc-coverage-auditor wrapper (latest_urls={total})")
print(f"[metric] {agent_id}: {metrics}")
PY
  ) >> "$LOG" 2>&1
fi

# Cap log size
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5000000 ]; then
  tail -c 3000000 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
