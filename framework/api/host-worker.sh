#!/usr/bin/env bash
# Host-side worker — picks up agent runs queued by the API container.
#
# The API container itself can't exec agents (it doesn't have docker, az,
# git, ssh, the user's repo paths, etc). Instead, `POST /api/agents/<id>/trigger`
# writes a JSON file to /tmp/agent-trigger-queue/. This worker (which runs
# *on the host*, typically as a systemd --user service) polls the queue,
# atomically claims each job, and exec's the agent's entry_command.
#
# Run interactively:
#   bash framework/api/host-worker.sh
# Or as a one-shot pump:
#   bash framework/api/host-worker.sh --once
# Or as a systemd-user service: see install/host-worker.service

set -euo pipefail

QUEUE_DIR="${QUEUE_DIR:-/tmp/agent-trigger-queue}"
LOG_DIR="${LOG_DIR:-/tmp/reusable-agents-logs}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-2}"
ONCE=0

mkdir -p "$QUEUE_DIR" "$LOG_DIR"

log() { printf '[host-worker %s] %s\n' "$(date -Is)" "$*" >&2 ; }

# Write a status.json + append an event for the dashboard to pick up. This
# gives shell-only agents (no AgentBase) an immediate "tile is glowing" signal
# without having to refactor each script. Agents that DO use StatusReporter
# will overwrite this with their own richer messages on first .status() call.
status_write() {
    local agent_id="$1"
    local run_id="$2"
    local state="$3"
    local message="$4"
    local progress="${5:-0.0}"
    AGENT_ID="$agent_id" RUN_ID="$run_id" STATE="$state" MESSAGE="$message" \
    PROGRESS="$progress" PYTHONPATH="/home/voidsstr/development/reusable-agents" \
    python3 - <<'PY' 2>/dev/null || true
import json, os
from datetime import datetime, timezone
# Use the framework's storage backend — same as the API/agents — so writes
# go to Azure blob (or local FS) per the configured STORAGE_BACKEND env.
from framework.core.storage import get_storage
s = get_storage()

agent = os.environ["AGENT_ID"]
state = os.environ["STATE"]
run_ts = os.environ.get("RUN_ID") or ""
now = datetime.now(timezone.utc).isoformat(timespec="seconds")
sp_key = f"agents/{agent}/status.json"

prev = s.read_json(sp_key) or {}
started = prev.get("started_at") if state in ("running", "starting") and prev.get("state") in ("running", "starting") else now
payload = {
    "schema_version": "1",
    "agent_id": agent,
    "state": state,
    "message": os.environ.get("MESSAGE", ""),
    "progress": float(os.environ.get("PROGRESS", "0") or 0),
    "current_action": "host-worker" if state in ("running","starting") else "",
    "started_at": started,
    "updated_at": now,
    "current_run_ts": run_ts,
    "iteration_count": int(prev.get("iteration_count", 0)) + (1 if state == "starting" else 0),
    "internal": {"source": "host-worker"},
}
s.write_json(sp_key, payload)
# Append to global event log
s.append_jsonl("registry/events.jsonl", {
    "ts": now, "agent_id": agent, "run_ts": run_ts,
    "state": state, "message": os.environ.get("MESSAGE", ""),
    "current_action": "host-worker",
})
PY
}

# Background heartbeat — refreshes updated_at every 5s while the agent runs so
# the dashboard knows the run is still alive even if the agent itself is silent
# (shell-only / long external commands). Killed when process_one returns.
heartbeat_loop() {
    local agent_id="$1"
    local run_id="$2"
    while true; do
        sleep 5
        # Only re-touch updated_at if state is still running/starting — don't
        # clobber a terminal state the agent set.
        AGENT_ID="$agent_id" PYTHONPATH="/home/voidsstr/development/reusable-agents" \
        python3 - <<'PY' 2>/dev/null || true
import os
from datetime import datetime, timezone
from framework.core.storage import get_storage
s = get_storage()
agent = os.environ["AGENT_ID"]
sp_key = f"agents/{agent}/status.json"
prev = s.read_json(sp_key)
if not prev or prev.get("state") not in ("running", "starting"):
    raise SystemExit
prev["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
s.write_json(sp_key, prev)
PY
    done
}

process_one() {
    local job_path="$1"
    local tmp="$job_path.processing"
    if ! mv "$job_path" "$tmp" 2>/dev/null; then
        return 0
    fi

    local agent_id run_id cmd triggered_by
    agent_id=$(python3 -c "import json,sys;print(json.load(sys.stdin)['agent_id'])" < "$tmp")
    run_id=$(python3 -c "import json,sys;print(json.load(sys.stdin).get('run_id',''))" < "$tmp")
    cmd=$(python3 -c "import json,sys;print(json.load(sys.stdin)['entry_command'])" < "$tmp")
    triggered_by=$(python3 -c "import json,sys;print(json.load(sys.stdin).get('triggered_by','manual'))" < "$tmp")

    log "claim agent=$agent_id run_id=$run_id"
    local job_log="$LOG_DIR/${agent_id}-${run_id}.log"

    # Glow tile + activity feed: mark running BEFORE exec so the dashboard
    # lights up immediately. Agents using StatusReporter will overwrite this
    # on their first self.status(...) call.
    status_write "$agent_id" "$run_id" "running" "Triggered ($triggered_by) — running…" "0.05"

    # Background heartbeat keeps updated_at fresh while agent runs.
    heartbeat_loop "$agent_id" "$run_id" &
    local hb_pid=$!

    # Crash-safety: if THIS bash subshell dies (SIGTERM, SIGKILL not
    # catchable, but EXIT covers normal termination + most signals), make
    # sure we've written a terminal state so the dashboard doesn't show a
    # phantom "running" forever. The API-side ghost-reaper is the
    # belt-and-suspenders backup for SIGKILL cases.
    cleanup_on_exit() {
        local rc_local=$?
        kill "$hb_pid" 2>/dev/null || true
        # If status is still running/starting, flip it to failure with a
        # crash note. Idempotent — agent's own terminal write wins if it
        # already happened.
        AGENT_ID="$agent_id" RC="$rc_local" \
        PYTHONPATH="/home/voidsstr/development/reusable-agents" \
        python3 - <<'PY' 2>/dev/null || true
import os
from datetime import datetime, timezone
from framework.core.storage import get_storage
s = get_storage()
agent = os.environ["AGENT_ID"]
sp_key = f"agents/{agent}/status.json"
prev = s.read_json(sp_key) or {}
if prev.get("state") in ("running", "starting"):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev.update({
        "state": "failure",
        "updated_at": now,
        "progress": 1.0,
        "current_action": "",
        "message": f"host-worker bash exited rc={os.environ.get('RC','?')} before agent posted terminal state",
    })
    prev.setdefault("agent_id", agent)
    prev.setdefault("schema_version", "1")
    prev.setdefault("internal", {})["reaped_by"] = "host-worker-trap"
    s.write_json(sp_key, prev)
    s.append_jsonl("registry/events.jsonl", {
        "ts": now, "agent_id": agent,
        "state": "failure", "message": prev["message"],
        "current_action": "host-worker-trap",
    })
PY
    }
    trap cleanup_on_exit EXIT

    # Stuck-process watchdog: kill the agent if it runs longer than the
    # configured wall-clock cap. Default 2h — most legitimate runs finish
    # well under this; anything beyond is almost certainly hung. Override
    # per-agent via $AGENT_TIMEOUT_<ID> env var (uppercased, dashes →
    # underscores) or globally via $AGENT_DEFAULT_TIMEOUT_S.
    local default_timeout="${AGENT_DEFAULT_TIMEOUT_S:-7200}"
    local id_env_key="AGENT_TIMEOUT_$(echo "$agent_id" | tr 'a-z-' 'A-Z_')"
    local agent_timeout="${!id_env_key:-$default_timeout}"

    # Pass through framework storage config so agents write to the SAME
    # store the API container reads from. Defaults to azure since that's the
    # production backend; falls back to local FS if STORAGE_BACKEND=local.
    set +e
    timeout --signal=TERM --kill-after=30s "$agent_timeout" \
    env AGENT_RUN_ID="$run_id" \
        AGENT_ID="$agent_id" \
        AGENT_TRIGGERED_BY="$triggered_by" \
        STORAGE_BACKEND="${STORAGE_BACKEND:-azure}" \
        AZURE_STORAGE_CONNECTION_STRING="${AZURE_STORAGE_CONNECTION_STRING:-}" \
        AZURE_STORAGE_CONTAINER="${AZURE_STORAGE_CONTAINER:-agents}" \
        AGENT_STORAGE_LOCAL_PATH="${AGENT_STORAGE_LOCAL_PATH:-$HOME/.reusable-agents/data}" \
        FRAMEWORK_API_URL="${FRAMEWORK_API_URL:-http://localhost:8093}" \
        bash -c "$cmd" > "$job_log" 2>&1
    local rc=$?
    set -e
    # rc=124 means the timeout fired
    if [ "$rc" -eq 124 ]; then
        log "TIMEOUT agent=$agent_id run_id=$run_id after ${agent_timeout}s — killed"
        # Notify operator via the framework's resilience module
        AGENT_ID="$agent_id" RUN_ID="$run_id" TIMEOUT_S="$agent_timeout" LOG_PATH="$job_log" \
        PYTHONPATH="/home/voidsstr/development/reusable-agents" \
        python3 - <<'PY' 2>/dev/null || true
import os
from framework.core.resilience import notify_operator
class TimeoutFromHostWorker(TimeoutError):
    pass
err = TimeoutFromHostWorker(
    f"{os.environ['AGENT_ID']} run {os.environ['RUN_ID']} exceeded "
    f"{os.environ['TIMEOUT_S']}s wall-clock — host-worker killed it.")
notify_operator(
    agent_id=os.environ["AGENT_ID"], error=err,
    context={
        "phase": "host-worker-watchdog",
        "run_id": os.environ["RUN_ID"],
        "timeout_s": int(os.environ["TIMEOUT_S"]),
        "log_path": os.environ["LOG_PATH"],
    },
    severity="high",
    cooldown_s=600,  # 10 min — timeouts on the same agent are usually correlated
)
PY
    fi

    kill "$hb_pid" 2>/dev/null || true
    wait "$hb_pid" 2>/dev/null || true

    log "done agent=$agent_id run_id=$run_id rc=$rc log=$job_log"

    # Final terminal state — only write if the agent didn't already set one.
    # We check by reading status.json: if it's still "running" (i.e. the agent
    # never wrote success/failure itself), we set it.
    local final_state="success"
    [ "$rc" -ne 0 ] && final_state="failure"
    AGENT_ID="$agent_id" RC="$rc" FINAL="$final_state" \
    PYTHONPATH="/home/voidsstr/development/reusable-agents" \
    python3 - <<'PY' 2>/dev/null || true
import os
from datetime import datetime, timezone
from framework.core.storage import get_storage
s = get_storage()
agent = os.environ["AGENT_ID"]
final = os.environ["FINAL"]
rc = os.environ.get("RC", "0")
sp_key = f"agents/{agent}/status.json"
prev = s.read_json(sp_key) or {}
# Only overwrite if the agent didn't already write a terminal state.
if prev.get("state") in ("running", "starting", None, ""):
    prev.update({
        "state": final,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "progress": 1.0,
        "current_action": "",
        "message": prev.get("message") or (f"Run finished rc={rc}"),
    })
    prev.setdefault("agent_id", agent)
    prev.setdefault("schema_version", "1")
    s.write_json(sp_key, prev)
    s.append_jsonl("registry/events.jsonl", {
        "ts": prev["updated_at"], "agent_id": agent,
        "state": final, "message": prev["message"],
    })
PY

    rm -f "$tmp"
}

if [ "${1:-}" = "--once" ]; then
    ONCE=1
fi

log "host-worker starting (queue=$QUEUE_DIR, poll=${POLL_INTERVAL_S}s, once=$ONCE)"

while true; do
    for job in "$QUEUE_DIR"/*.json; do
        [ -e "$job" ] || continue
        # Detach so the loop keeps scanning the queue while long-running
        # jobs continue in the background. Without this a slow agent
        # (e.g., specpicks-catalog-audit, ~30 min of claude-haiku calls)
        # blocks all other triggers until it finishes.
        # NOTE: only stdin is redirected — keep stderr open so the log()
        # messages from process_one still reach the systemd journal /
        # log file. The agent's own stdout/stderr is captured to job_log.
        process_one "$job" </dev/null &
        disown
    done
    if [ "$ONCE" -eq 1 ]; then
        # In one-shot mode we DO want to wait so the script doesn't exit
        # before the dispatched jobs finish.
        wait
        break
    fi
    sleep "$POLL_INTERVAL_S"
done
