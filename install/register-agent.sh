#!/usr/bin/env bash
# Register an agent with the local reusable-agents framework instance.
#
# Reads <agent-dir>/manifest.json, POSTs it to /api/agents/register, which:
#   1. Stores the manifest in the registry (Azure Blob or local FS)
#   2. Writes a systemd --user timer + service if cron_expr + entry_command
#      are set, then `systemctl --user daemon-reload && enable --now`
#   3. Returns 200 with the agent's summary
#
# Idempotent — re-running updates the existing registration.
#
# Usage:
#   register-agent.sh <agent-dir>
#   register-agent.sh /home/voidsstr/development/nsc-assistant/agents/seo-opportunity-agent
#
# Environment:
#   FRAMEWORK_API_URL    default http://localhost:8090
#   FRAMEWORK_API_TOKEN  required if the API has auth enabled

set -euo pipefail

AGENT_DIR="${1:-}"
if [ -z "$AGENT_DIR" ]; then
    echo "usage: register-agent.sh <agent-dir>" >&2
    exit 2
fi
AGENT_DIR="$(cd "$AGENT_DIR" && pwd)"

MANIFEST="$AGENT_DIR/manifest.json"
if [ ! -f "$MANIFEST" ]; then
    echo "ERROR: no manifest.json at $MANIFEST" >&2
    exit 2
fi

API="${FRAMEWORK_API_URL:-http://localhost:8090}"
TOKEN="${FRAMEWORK_API_TOKEN:-}"
AUTH_HEADER=()
if [ -n "$TOKEN" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer $TOKEN")
fi

# Build the registration payload by enriching the manifest with resolved
# absolute paths the framework needs.
PAYLOAD=$(python3 - <<PY
import json, os, sys
m = json.load(open("$MANIFEST"))
agent_dir = "$AGENT_DIR"
payload = {
    "id": m["id"],
    "name": m.get("name") or m["id"],
    "description": m.get("description", ""),
    "category": m.get("category", "misc"),
    "task_type": m.get("task_type", "desktop-task"),
    "cron_expr": m.get("cron_expr", ""),
    "timezone": m.get("timezone", "UTC"),
    "enabled": bool(m.get("enabled", True)),
    "owner": m.get("owner", ""),
    "repo_dir": agent_dir,
    "entry_command": m.get("entry_command", ""),
    "autowire_cron": True,
    "metadata": m.get("metadata", {}),
    "depends_on": m.get("depends_on", []),
    "runnable_modes": m.get("runnable_modes", ["cron", "manual"]),
    "confirmation_flow": m.get("confirmation_flow", {}),
}
def _resolve(d, name, fallbacks):
    p = m.get(name, "")
    if p and os.path.isfile(os.path.join(d, p)):
        return os.path.join(d, p)
    for f in fallbacks:
        fp = os.path.join(d, f)
        if os.path.isfile(fp):
            return fp
    return ""

runbook_path = _resolve(agent_dir, "runbook", ["AGENT.md", "README.md"])
skill_path   = _resolve(agent_dir, "skill", ["SKILL.md"])
if runbook_path:
    payload["runbook_path"] = runbook_path
    try:
        payload["runbook_body"] = open(runbook_path).read()
    except Exception:
        pass
if skill_path:
    payload["skill_path"] = skill_path
    try:
        payload["skill_body"] = open(skill_path).read()
    except Exception:
        pass
# Also embed README.md if it differs from runbook
readme_path = os.path.join(agent_dir, "README.md")
if os.path.isfile(readme_path) and readme_path != runbook_path:
    try:
        payload["readme_body"] = open(readme_path).read()
    except Exception:
        pass
print(json.dumps(payload))
PY
)

ID=$(python3 -c "import json,sys;print(json.load(open('$MANIFEST'))['id'])")
echo "[register] $ID → $API"

RESP_FILE=$(mktemp)
HTTP_CODE=$(curl -sS -o "$RESP_FILE" -w "%{http_code}" \
    -X POST "$API/api/agents/register" \
    -H 'Content-Type: application/json' \
    "${AUTH_HEADER[@]}" \
    -d "$PAYLOAD" || true)

if [ "$HTTP_CODE" != "200" ]; then
    echo "[register] ✗ HTTP $HTTP_CODE" >&2
    cat "$RESP_FILE" >&2
    echo >&2
    rm -f "$RESP_FILE"
    exit 1
fi

# Parse + print the summary
python3 -c "
import json
d = json.load(open('$RESP_FILE'))
print(f'  ✓ {d[\"id\"]:<35} cron=\"{d[\"cron_expr\"] or \"(none)\"}\" enabled={d[\"enabled\"]}')
"
rm -f "$RESP_FILE"
