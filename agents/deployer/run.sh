#!/usr/bin/env bash
# Compat shim — production callers (implementer/run.sh) still invoke
# this path. Forwards to the AgentBase entrypoint. Delete once every
# caller has been migrated to call agent.py directly.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Honor --run-dir <path> by exporting RESPONDER_RUN_DIR (the env var
# agent.py reads). Strip the flag from $@.
RUN_DIR=""
NEW_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --run-dir)
            RUN_DIR="$2"; shift 2 ;;
        --run-dir=*)
            RUN_DIR="${1#*=}"; shift ;;
        *)
            NEW_ARGS+=("$1"); shift ;;
    esac
done
if [ -n "$RUN_DIR" ]; then
    export RESPONDER_RUN_DIR="$RUN_DIR"
fi
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$SCRIPT_DIR/agent.py" "${NEW_ARGS[@]:-}"
