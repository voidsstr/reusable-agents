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
    local store_path="${AGENT_STORAGE_LOCAL_PATH:-$HOME/.reusable-agents/data}"
    AGENT_ID="$agent_id" RUN_ID="$run_id" STATE="$state" MESSAGE="$message" \
    PROGRESS="$progress" STORE="$store_path" python3 - <<'PY' 2>/dev/null || true
import json, os, time
from datetime import datetime, timezone
agent = os.environ["AGENT_ID"]
state = os.environ["STATE"]
store = os.environ["STORE"]
run_ts = os.environ.get("RUN_ID") or ""
now = datetime.now(timezone.utc).isoformat(timespec="seconds")
agent_dir = os.path.join(store, "agents", agent)
os.makedirs(agent_dir, exist_ok=True)
sp = os.path.join(agent_dir, "status.json")
prev = {}
if os.path.exists(sp):
    try:
        prev = json.load(open(sp))
    except Exception:
        prev = {}
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
tmp = sp + ".tmp"
with open(tmp, "w") as f:
    json.dump(payload, f, indent=2)
os.replace(tmp, sp)
# Append to global event log so the activity feed sees it
events = os.path.join(store, "registry", "events.jsonl")
os.makedirs(os.path.dirname(events), exist_ok=True)
with open(events, "a") as f:
    f.write(json.dumps({
        "ts": now, "agent_id": agent, "run_ts": run_ts,
        "state": state, "message": os.environ.get("MESSAGE", ""),
        "current_action": "host-worker",
    }) + "\n")
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
        local store_path="${AGENT_STORAGE_LOCAL_PATH:-$HOME/.reusable-agents/data}"
        AGENT_ID="$agent_id" STORE="$store_path" python3 - <<'PY' 2>/dev/null || true
import json, os
from datetime import datetime, timezone
agent = os.environ["AGENT_ID"]
store = os.environ["STORE"]
sp = os.path.join(store, "agents", agent, "status.json")
if not os.path.exists(sp):
    raise SystemExit
try:
    s = json.load(open(sp))
except Exception:
    raise SystemExit
if s.get("state") not in ("running", "starting"):
    raise SystemExit
s["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
tmp = sp + ".tmp"
with open(tmp, "w") as f:
    json.dump(s, f, indent=2)
os.replace(tmp, sp)
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

    # Pass through framework storage config so agents write to the SAME
    # store the API container reads from (otherwise the dashboard sees
    # an empty Runs / Storage / current_status). Defaults match the
    # docker-compose bind-mount target on this dev box.
    set +e
    AGENT_RUN_ID="$run_id" \
    AGENT_ID="$agent_id" \
    AGENT_TRIGGERED_BY="$triggered_by" \
    STORAGE_BACKEND="${STORAGE_BACKEND:-local}" \
    AGENT_STORAGE_LOCAL_PATH="${AGENT_STORAGE_LOCAL_PATH:-$HOME/.reusable-agents/data}" \
    FRAMEWORK_API_URL="${FRAMEWORK_API_URL:-http://localhost:8093}" \
        bash -c "$cmd" > "$job_log" 2>&1
    local rc=$?
    set -e

    kill "$hb_pid" 2>/dev/null || true
    wait "$hb_pid" 2>/dev/null || true

    log "done agent=$agent_id run_id=$run_id rc=$rc log=$job_log"

    # Final terminal state — only write if the agent didn't already set one.
    # We check by reading status.json: if it's still "running" (i.e. the agent
    # never wrote success/failure itself), we set it.
    local final_state="success"
    [ "$rc" -ne 0 ] && final_state="failure"
    local store_path="${AGENT_STORAGE_LOCAL_PATH:-$HOME/.reusable-agents/data}"
    AGENT_ID="$agent_id" STORE="$store_path" RC="$rc" FINAL="$final_state" python3 - <<'PY' 2>/dev/null || true
import json, os
from datetime import datetime, timezone
agent = os.environ["AGENT_ID"]
store = os.environ["STORE"]
final = os.environ["FINAL"]
rc = os.environ.get("RC", "0")
sp = os.path.join(store, "agents", agent, "status.json")
prev = {}
if os.path.exists(sp):
    try:
        prev = json.load(open(sp))
    except Exception:
        prev = {}
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
    tmp = sp + ".tmp"
    with open(tmp, "w") as f:
        json.dump(prev, f, indent=2)
    os.replace(tmp, sp)
    events = os.path.join(store, "registry", "events.jsonl")
    os.makedirs(os.path.dirname(events), exist_ok=True)
    with open(events, "a") as f:
        f.write(json.dumps({
            "ts": prev["updated_at"], "agent_id": agent,
            "state": final, "message": prev["message"],
        }) + "\n")
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
