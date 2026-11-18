#!/usr/bin/env bash
# Register this site's agents with the reusable-agents framework.
#
# Every agents/<id>/ with a manifest.json is POSTed to the framework's
# /api/agents/register; the framework writes a systemd --user timer for each
# manifest that declares a cron_expr. Registering is what makes an agent run,
# so run this only once the operator has approved the site's agents going
# live. Nothing in this repository calls it.
#
#   FRAMEWORK_API_URL=http://localhost:8093 bash agents/register-with-framework.sh
#
# Prerequisites: the framework API and the host-worker on this host
# (reusable-agents install/standup-fleet-host.sh).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RA_REPO="${RA_REPO:-/home/voidsstr/development/reusable-agents}"

if [[ ! -f "$RA_REPO/install/register-all-from-dir.sh" ]]; then
    echo "ERROR: reusable-agents framework not found at $RA_REPO (set RA_REPO)" >&2
    exit 2
fi

exec bash "$RA_REPO/install/register-all-from-dir.sh" "$REPO_ROOT/agents"
