#!/usr/bin/env bash
# Entry script invoked by the framework's host-worker on "Run now" + by
# the systemd timer for scheduled runs.
#
# Both paths set AGENT_ID + AGENT_RUN_ID + AGENT_TRIGGERED_BY env vars
# before calling this script. Pass them through to the Python agent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load host secrets (Azure storage conn + per-site DSNs). Non-fatal if absent
# (e.g. in tests where env is injected another way).
if [ -f "$HOME/.reusable-agents/secrets.env" ]; then
  set -a; . "$HOME/.reusable-agents/secrets.env"; set +a
fi

# ── Site config (framework-first: engine is generic, values are env) ──────────
# Default deployment targets specpicks (the site stuck in Discovered-not-
# indexed). A second instance for another site just overrides these three.
export AUTHORITY_SITE_ID="${AUTHORITY_SITE_ID:-specpicks}"
export AUTHORITY_SITE_DOMAIN="${AUTHORITY_SITE_DOMAIN:-specpicks.com}"
export DATABASE_URL="${DATABASE_URL:-${DATABASE_URL_SPECPICKS:-}}"
# Goals + status must land where the dashboard reads them.
export STORAGE_BACKEND="${STORAGE_BACKEND:-azure}"

exec python3 "$SCRIPT_DIR/agent.py" "$@"
