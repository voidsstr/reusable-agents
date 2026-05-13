#!/usr/bin/env bash
# Universal status-writing wrapper for systemd-fired agents.
#
# Guarantees that EVERY scheduled agent writes a "starting" status
# before exec and a final "success"/"failure" status after exec, even
# if the agent's own entry script never calls framework.cli.status.
#
# This is the resilient floor — agents that DO call framework.cli.status
# from inside their entry script add richer mid-run progress on top.
#
# Usage (set by framework/core/scheduler.py in the .service ExecStart):
#
#     bash /path/to/framework/agent_run_wrapper.sh <agent-id> <entry-command...>
#
# The agent_id must be the first arg; everything after is the verbatim
# entry_command from the manifest.
#
# Behavior:
#   1. Generate RUN_TS (UTC), write "starting" status.
#   2. Exec the entry_command. Inherit env. Stream stdout/stderr to
#      systemd journal as usual.
#   3. On exit, capture rc. Write "success" if rc==0, else "failure"
#      with the rc encoded in the message.
#   4. Always exit with the entry command's rc so systemd sees the
#      true result (Failed vs OK).
#
# The status writes are best-effort — failures inside the wrapper
# never mask the agent's own exit code.

set -uo pipefail

AGENT_ID="${1:-}"
shift || true
if [ -z "$AGENT_ID" ]; then
    echo "ERROR: agent_run_wrapper called without agent_id" >&2
    exit 64  # EX_USAGE
fi

# Digest mode (5-1): suppress per-agent emails fleet-wide. The
# digest-rollup-agent fires every 3h with a single consolidated email.
# Set DIGEST_ONLY=0 in the environment before running this wrapper to
# re-enable individual emails (the rollup agent's own manifest sets
# DIGEST_ONLY=0 inline so it can actually send its own digest).
export DIGEST_ONLY="${DIGEST_ONLY:-1}"
if [ "$#" -eq 0 ]; then
    echo "ERROR: agent_run_wrapper called without entry command" >&2
    exit 64
fi

# Resolve framework repo from this script's location so PYTHONPATH works
# regardless of where systemd cd's to.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RA_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="${RA_REPO}:${PYTHONPATH:-}"
# All cron-fired agents call the prod API for status/dispatch/messaging.
# 2026-05-11: localhost dashboard removed; prod is the sole API plane.
export FRAMEWORK_API_URL="${FRAMEWORK_API_URL:-https://agents.happysky-24190067.eastus.azurecontainerapps.io}"

# Route any `claude` invocation through the pool when configured. The
# init step writes a `claude` shim at $CLAUDE_POOL_ROOT/bin/ that picks
# the next-due Max account profile and execs the real claude under
# its HOME. Setting CLAUDE_POOL=0 in the environment skips this.
CLAUDE_POOL_ROOT="${CLAUDE_POOL_ROOT:-$HOME/.reusable-agents/claude-pool}"
if [ "${CLAUDE_POOL:-1}" != "0" ] && [ -x "$CLAUDE_POOL_ROOT/bin/claude" ]; then
    export PATH="$CLAUDE_POOL_ROOT/bin:$PATH"
fi

# Per-profile SOCKS5 proxy egress: when claude-via-proxy is installed,
# point CLAUDE_POOL_REAL_CLAUDE at it so the pool dispatches through the
# proxy wrapper. The wrapper reads $CLAUDE_PROXY_FOR_PROFILE (set by the
# pool's _run_one_dispatch) and looks up the per-profile SOCKS5 URL in
# $CLAUDE_POOL_ROOT/proxies.conf, giving each profile its own egress IP.
# Without proxies.conf the wrapper falls back to the legacy WARP socket
# on :40000, then to direct. Disable per-call with IMPLEMENTER_USE_PROXY=0
# (legacy name kept for back-compat) or AGENT_USE_PROXY=0.
if [ "${AGENT_USE_PROXY:-${IMPLEMENTER_USE_PROXY:-1}}" != "0" ] \
        && [ -x "$HOME/.local/bin/claude-via-proxy" ]; then
    export CLAUDE_POOL_REAL_CLAUDE="$HOME/.local/bin/claude-via-proxy"
fi

# RUN_TS — agents that use the framework can override by exporting their
# own RUN_TS before calling. Otherwise we generate one.
RUN_TS="${AGENT_RUN_TS:-${RUN_TS:-$(date -u +%Y%m%dT%H%M%SZ)}}"
export RUN_TS

# Triggered by — useful breadcrumb in the status payload.
TRIGGERED_BY="${AGENT_TRIGGERED_BY:-cron}"

_write_status() {
    local state="$1"
    local message="$2"
    local progress="${3:-0}"
    # Best-effort, suppress all output (the agent's stdout owns the journal).
    python3 -m framework.cli.status \
        --agent-id "$AGENT_ID" \
        --run-ts "$RUN_TS" \
        --state "$state" \
        --message "$message" \
        --progress "$progress" \
        >/dev/null 2>&1 || true
}

_write_status starting "triggered by $TRIGGERED_BY" 0.0

# Run the entry command. Use exec-style with rc capture; can't use
# `exec "$@"` directly because we need to write the final status.
"$@"
RC=$?

if [ $RC -eq 0 ]; then
    _write_status success "completed cleanly" 1.0
else
    _write_status failure "exited rc=$RC" 1.0
fi

exit $RC
