#!/usr/bin/env bash
# Entry script invoked by the framework's host-worker on "Run now" + by
# the systemd timer for scheduled runs.
#
# Both paths set AGENT_ID + AGENT_RUN_ID + AGENT_TRIGGERED_BY env vars
# before calling this script. Pass them through to the Python agent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/agent.py" "$@"
