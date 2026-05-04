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
RUN_OUTPUT=$(mktemp)
{
  echo "── $(date -u +%Y-%m-%dT%H:%M:%SZ) — indexnow submit ──"
  ( cd "$APP_DIR" && \
    NODE_PATH="$APP_DIR/node_modules" \
    timeout 600 npx ts-node --transpile-only --compiler-options '{"module":"node16","moduleResolution":"node16","esModuleInterop":true,"skipLibCheck":true,"resolveJsonModule":true}' "$HERE/submit.ts" "$@" ) 2>&1 | tee "$RUN_OUTPUT" \
    || echo "(indexnow submit failed or timed out)"
  echo ""
} >> "$LOG" 2>&1

# Extract submission counts from the run output and record as metrics. The
# log lines look like: "[indexnow:specpicks] done submitted=5184 failed=0".
# We parse those and call metric_helper for whichever agent is being run.
# Determine the agent_id from the --site arg (must match an *-indexnow-submitter
# or *-indexnow-bulk per-site instance in nsc-assistant).
if [ -s "$RUN_OUTPUT" ]; then
  python3 - <<'PY' "$RUN_OUTPUT" "$@" 2>&1 >> "$LOG"
import re, sys, os
sys.path.insert(0, "/home/voidsstr/development/reusable-agents")
try:
    from framework.core import metric_helper
except Exception as e:
    print(f"[metric] import failed: {e}", file=sys.stderr); sys.exit(0)

run_output_path = sys.argv[1]
args = sys.argv[2:]
# Was this --bulk?
is_bulk = any(a == "--bulk" or a == "--bulk-mode" for a in args)
# Which site?
site = None
for a in args:
    if a.startswith("--site="):
        site = a.split("=", 1)[1]
        break
if not site:
    sys.exit(0)

agent_suffix = "indexnow-bulk" if is_bulk else "indexnow-submitter"
agent_id = f"{site}-{agent_suffix}"
metric_id = "goal-bulk-urls-submitted" if is_bulk else "goal-urls-submitted-30d"

submitted = 0
failed = 0
try:
    with open(run_output_path) as f:
        for line in f:
            m = re.search(r"\[indexnow:" + re.escape(site) + r"\]\s*done\s*submitted=(\d+)\s*failed=(\d+)", line)
            if m:
                submitted += int(m.group(1))
                failed += int(m.group(2))
except Exception as e:
    print(f"[metric] parse failed: {e}", file=sys.stderr); sys.exit(0)

# For incremental submitter, the metric is a 30d cumulative count — but we
# can't easily compute the rolling 30d sum from a single run. Best
# approximation: add this run's submitted count as a delta. The dashboard
# will see growth over time. For bulk, latest run count is the goal value.
import datetime
run_ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
if is_bulk:
    metric_helper.record(agent_id, "goal-bulk-urls-submitted", float(submitted), run_ts=run_ts,
                         note=f"failed={failed}")
else:
    # Read prior 30d cumulative from cache and add this run
    cache = metric_helper.read_cache(agent_id)
    prior = ((cache.get("goals") or {}).get("goal-urls-submitted-30d") or {}).get("latest_value") or 0
    metric_helper.record(agent_id, "goal-urls-submitted-30d", float(prior) + float(submitted),
                         run_ts=run_ts, note=f"+{submitted} this run, failed={failed}")
    # success rate — treat any failed=0 + non-empty submission as a 100% run; otherwise pro-rate
    rate = 100.0 if failed == 0 else max(0.0, 100.0 - 100.0 * failed / max(submitted+failed, 1))
    metric_helper.record(agent_id, "goal-runs-success-rate-7d", float(rate), run_ts=run_ts,
                         note=f"submitted={submitted} failed={failed}")
print(f"[metric] recorded for {agent_id}: submitted={submitted} failed={failed}", file=sys.stderr)
PY
fi
rm -f "$RUN_OUTPUT"

# Cap log size at ~5MB
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 5000000 ]; then
  tail -c 3000000 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi
