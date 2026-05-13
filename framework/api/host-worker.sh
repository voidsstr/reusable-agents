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

# Self-discover the repo root from the script's own location so the
# worker doesn't depend on hardcoded paths. Override with
# RA_REPO_ROOT in the systemd unit if you've moved framework/ around.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RA_REPO_ROOT="${RA_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
export RA_REPO_ROOT

QUEUE_DIR="${QUEUE_DIR:-/tmp/agent-trigger-queue}"
LOG_DIR="${LOG_DIR:-/tmp/reusable-agents-logs}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-2}"
ONCE=0

mkdir -p "$QUEUE_DIR" "$LOG_DIR"

log() { printf '[host-worker %s] %s\n' "$(date -Is)" "$*" >&2 ; }

# Single-instance guard. We previously had two host-workers racing for
# triggers — a systemd-managed one with the current env, and a long-lived
# zombie from a prior session whose env still had a stale (truncated)
# AZURE_STORAGE_CONNECTION_STRING. Whichever process won the atomic mv
# claim ran the agent, so ~half of triggers exec'd with broken env.
#
# flock takes an exclusive lock on a fixed path; if another host-worker
# already holds it we exit immediately rather than silently dual-running.
LOCK_FILE="${LOCK_FILE:-/tmp/reusable-agents-host-worker.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    other_pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "?")
    log "another host-worker already running (lock=$LOCK_FILE pid=$other_pid) — exiting"
    exit 0
fi
echo "$$" >&9
# Lock auto-releases when fd 9 closes (process exit).

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
    PROGRESS="$progress" PYTHONPATH="$RA_REPO_ROOT" \
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
        AGENT_ID="$agent_id" PYTHONPATH="$RA_REPO_ROOT" \
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
        # already happened. Also auto-invoke the doctor for the crash.
        AGENT_ID="$agent_id" RUN_ID="$run_id" RC="$rc_local" LOG_PATH="$job_log" \
        PYTHONPATH="$RA_REPO_ROOT" \
        python3 - <<'PY' 2>/dev/null || true
import os
from datetime import datetime, timezone
from framework.core.storage import get_storage
s = get_storage()
agent = os.environ["AGENT_ID"]
sp_key = f"agents/{agent}/status.json"
prev = s.read_json(sp_key) or {}
crash_msg = f"host-worker bash exited rc={os.environ.get('RC','?')} before agent posted terminal state"
flipped = False
if prev.get("state") in ("running", "starting"):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev.update({
        "state": "failure",
        "updated_at": now,
        "progress": 1.0,
        "current_action": "",
        "message": crash_msg,
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
    flipped = True

# Only invoke doctor when we actually flipped to failure (i.e., the agent
# never wrote its own terminal state). If the agent already wrote success
# or its own failure, the run_once auto-recovery has already fired.
if flipped and agent != "agent-doctor":
    from framework.core.resilience import invoke_doctor
    class HostWorkerCrash(RuntimeError):
        pass
    err = HostWorkerCrash(crash_msg)
    invoke_doctor(
        failed_agent_id=agent, error=err,
        context={
            "phase": "host-worker-trap",
            "run_id": os.environ.get("RUN_ID", ""),
            "rc": int(os.environ.get("RC", "0") or "0"),
            "log_path": os.environ.get("LOG_PATH", ""),
        },
        run_id=os.environ.get("RUN_ID", ""), severity="critical",
        triggered_by="host-worker-trap",
    )
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
        AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}" \
        AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}" \
        AZURE_OPENAI_DEPLOYMENT="${AZURE_OPENAI_DEPLOYMENT:-implementer}" \
        AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2024-10-21}" \
        AGENT_STORAGE_LOCAL_PATH="${AGENT_STORAGE_LOCAL_PATH:-$HOME/.reusable-agents/data}" \
        FRAMEWORK_API_URL="${FRAMEWORK_API_URL:-http://localhost:8093}" \
        SENTRY_API_BASE="${SENTRY_API_BASE:-}" \
        SENTRY_API_TOKEN="${SENTRY_API_TOKEN:-}" \
        SENTRY_ORG_SLUG="${SENTRY_ORG_SLUG:-}" \
        SENTRY_PROJECT_SLUG="${SENTRY_PROJECT_SLUG:-}" \
        CRASH_WATCHER_TARGET_SITE="${CRASH_WATCHER_TARGET_SITE:-}" \
        bash -c "$cmd" > "$job_log" 2>&1
    local rc=$?
    set -e
    # rc=124 means the timeout fired
    if [ "$rc" -eq 124 ]; then
        log "TIMEOUT agent=$agent_id run_id=$run_id after ${agent_timeout}s — killed"
        # Notify operator via the framework's resilience module AND
        # auto-invoke agent-doctor with full incident context. Skip the
        # auto-invoke when the doctor itself timed out (don't recurse).
        AGENT_ID="$agent_id" RUN_ID="$run_id" TIMEOUT_S="$agent_timeout" LOG_PATH="$job_log" \
        PYTHONPATH="$RA_REPO_ROOT" \
        python3 - <<'PY' 2>/dev/null || true
import os
from framework.core.resilience import notify_operator, invoke_doctor
class TimeoutFromHostWorker(TimeoutError):
    pass
err = TimeoutFromHostWorker(
    f"{os.environ['AGENT_ID']} run {os.environ['RUN_ID']} exceeded "
    f"{os.environ['TIMEOUT_S']}s wall-clock — host-worker killed it.")
context = {
    "phase": "host-worker-watchdog",
    "run_id": os.environ["RUN_ID"],
    "timeout_s": int(os.environ["TIMEOUT_S"]),
    "log_path": os.environ["LOG_PATH"],
}
notify_operator(
    agent_id=os.environ["AGENT_ID"], error=err, context=context,
    severity="high",
    cooldown_s=600,  # 10 min — timeouts on the same agent are usually correlated
)
if os.environ["AGENT_ID"] != "agent-doctor":
    invoke_doctor(
        failed_agent_id=os.environ["AGENT_ID"], error=err, context=context,
        run_id=os.environ["RUN_ID"], severity="high",
        triggered_by="host-worker-watchdog",
    )
PY
    fi

    kill "$hb_pid" 2>/dev/null || true
    wait "$hb_pid" 2>/dev/null || true

    log "done agent=$agent_id run_id=$run_id rc=$rc log=$job_log"

    # Auto-recovery for non-AgentBase agents: a non-zero, non-timeout exit
    # didn't go through AgentBase.run_once, so we invoke the doctor here
    # too. AgentBase agents that already enqueued from inside their process
    # are deduped by invoke_doctor's 10-min cooldown.
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && [ "$agent_id" != "agent-doctor" ]; then
        AGENT_ID="$agent_id" RUN_ID="$run_id" RC="$rc" LOG_PATH="$job_log" \
        PYTHONPATH="$RA_REPO_ROOT" \
        python3 - <<'PY' 2>/dev/null || true
import os
from framework.core.resilience import invoke_doctor
class HostWorkerNonZeroExit(RuntimeError):
    pass
err = HostWorkerNonZeroExit(
    f"{os.environ['AGENT_ID']} run {os.environ['RUN_ID']} exited rc={os.environ['RC']}"
)
# Read the tail of the job log so the doctor has actual error text.
log_excerpt = ""
try:
    with open(os.environ["LOG_PATH"], "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 4096))
        log_excerpt = f.read().decode("utf-8", "replace")
except Exception:
    pass
invoke_doctor(
    failed_agent_id=os.environ["AGENT_ID"], error=err,
    context={
        "phase": "host-worker-rc",
        "run_id": os.environ["RUN_ID"],
        "rc": int(os.environ["RC"]),
        "log_path": os.environ["LOG_PATH"],
        "log_tail": log_excerpt[-2000:],
    },
    run_id=os.environ["RUN_ID"], severity="high",
    triggered_by="host-worker-rc",
)
PY
    fi

    # Final terminal state — only write if the agent didn't already set one.
    # We check by reading status.json: if it's still "running" (i.e. the agent
    # never wrote success/failure itself), we set it.
    local final_state="success"
    [ "$rc" -ne 0 ] && final_state="failure"
    AGENT_ID="$agent_id" RC="$rc" FINAL="$final_state" \
    PYTHONPATH="$RA_REPO_ROOT" \
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

# Source-agent priority ranks (lower = higher priority). When multiple
# triggers are queued, process them in this order so the limited LLM
# budget burns on the highest-impact work first.
#
# Tier resolution lives in the FRAMEWORK now (framework/core/priority.py)
# so any deployment of reusable-agents can configure priorities via
# `config/priority-config.json` in storage. We just shell out to the
# Python CLI that wraps the same logic the responder + dashboard use.
# Per-agent override remains supported via $AGENT_PRIORITY_<UPPER_AGENT_ID>=<int>.
RA_REPO_FOR_PRIORITY="${RA_REPO:-$RA_REPO_ROOT}"
agent_priority() {
    local aid="$1"
    local env_key="AGENT_PRIORITY_$(echo "$aid" | tr 'a-z-' 'A-Z_')"
    local override="${!env_key:-}"
    if [ -n "$override" ]; then
        echo "$override"
        return
    fi
    # Defer to framework primitive. PYTHONPATH must include the
    # reusable-agents repo root so `import framework.core.priority` works.
    local tier
    tier=$(PYTHONPATH="$RA_REPO_FOR_PRIORITY${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m framework.cli.priority "$aid" 2>/dev/null) || tier=""
    if [ -z "$tier" ]; then
        # Defensive fallback if the framework CLI errors (storage down,
        # import path broken). Equivalent to default_tier=5 in the config.
        tier=5
    fi
    echo "$tier"
}

# Sort the queued trigger files by (priority, mtime) so high-priority
# work claims the next slot regardless of when it landed in the queue.
# Outputs paths separated by newlines on stdout; caller iterates.
sorted_queue() {
    local f
    for f in "$QUEUE_DIR"/*.json; do
        [ -e "$f" ] || continue
        # Extract agent_id from job json. For implementer dispatches, ALSO
        # extract the source agent id (RESPONDER_AGENT_ID embedded in the
        # entry_command) so implementer triggers inherit the source's
        # priority — otherwise every implementer trigger is treated the
        # same regardless of whether it's shipping SEO recs or article
        # recs. The user wants source-agent-priority enforced.
        local aid src_aid effective_aid
        aid=$(python3 -c "import json,sys; print(json.load(sys.stdin).get('agent_id',''))" < "$f" 2>/dev/null) || aid=""
        if [ -z "$aid" ]; then
            aid=$(basename "$f" .json)
        fi
        effective_aid="$aid"
        if [ "$aid" = "implementer" ] || [ "$aid" = "seo-implementer" ]; then
            # Pull RESPONDER_AGENT_ID from the entry_command (set by the
            # responder when it queues an implementer dispatch). Pattern:
            #     RESPONDER_AGENT_ID=specpicks-seo-opportunity-agent
            src_aid=$(python3 -c "
import json, sys, re
try:
  d = json.load(sys.stdin)
  cmd = d.get('entry_command', '') or ''
  m = re.search(r'RESPONDER_AGENT_ID=([\\w-]+)', cmd)
  print(m.group(1) if m else '')
except Exception: pass
" < "$f" 2>/dev/null) || src_aid=""
            [ -n "$src_aid" ] && effective_aid="$src_aid"
        fi
        local prio
        prio=$(agent_priority "$effective_aid")
        # mtime as secondary sort so older triggers within a tier go first
        local mtime
        mtime=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        printf '%d\t%d\t%s\n' "$prio" "$mtime" "$f"
    done | sort -k1,1n -k2,2n | cut -f3
}

while true; do
    # 2026-05-11: pull any pending triggers from Azure blob storage into
    # /tmp. This makes the queue cross-host: the prod API (in Azure)
    # writes to `_trigger-queue/` blob; this host-worker downloads them
    # locally and processes via the existing /tmp loop below. Set
    # PULL_BLOB_TRIGGERS=0 to disable (e.g., local-only dev box without
    # storage backend).
    if [ "${PULL_BLOB_TRIGGERS:-1}" != "0" ]; then
        PYTHONPATH="$RA_REPO_ROOT" python3 -m framework.cli.pull_blob_triggers \
            --queue-dir "$QUEUE_DIR" >/dev/null 2>&1 || true
    fi

    while IFS= read -r job; do
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
    done < <(sorted_queue)
    if [ "$ONCE" -eq 1 ]; then
        # In one-shot mode we DO want to wait so the script doesn't exit
        # before the dispatched jobs finish.
        wait
        break
    fi
    sleep "$POLL_INTERVAL_S"
done
