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

    # Pass through framework storage config so agents write to the SAME
    # store the API container reads from (otherwise the dashboard sees
    # an empty Runs / Storage / current_status). Defaults match the
    # docker-compose bind-mount target on this dev box.
    AGENT_RUN_ID="$run_id" \
    AGENT_ID="$agent_id" \
    AGENT_TRIGGERED_BY="$triggered_by" \
    STORAGE_BACKEND="${STORAGE_BACKEND:-local}" \
    AGENT_STORAGE_LOCAL_PATH="${AGENT_STORAGE_LOCAL_PATH:-$HOME/.reusable-agents/data}" \
    FRAMEWORK_API_URL="${FRAMEWORK_API_URL:-http://localhost:8093}" \
        bash -c "$cmd" > "$job_log" 2>&1
    local rc=$?
    log "done agent=$agent_id run_id=$run_id rc=$rc log=$job_log"

    rm -f "$tmp"
}

if [ "${1:-}" = "--once" ]; then
    ONCE=1
fi

log "host-worker starting (queue=$QUEUE_DIR, poll=${POLL_INTERVAL_S}s, once=$ONCE)"

while true; do
    for job in "$QUEUE_DIR"/*.json; do
        [ -e "$job" ] || continue
        process_one "$job" &
    done
    wait
    if [ "$ONCE" -eq 1 ]; then
        break
    fi
    sleep "$POLL_INTERVAL_S"
done
