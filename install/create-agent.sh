#!/usr/bin/env bash
# Scaffold a new agent in the target repo, conforming to the
# reusable-agents framework standards.
#
# Copies _template/agent/* into <target-dir>/<agent-id>/, substitutes
# placeholders, and (optionally) registers the new agent with a running
# framework instance.
#
# Usage:
#   create-agent.sh <agent-id> <target-agents-dir> [options]
#
# Examples:
#   # Create a new agent in nsc-assistant
#   bash create-agent.sh my-new-agent /home/voidsstr/development/nsc-assistant/agents
#
#   # Create + auto-register
#   bash create-agent.sh my-new-agent /home/voidsstr/development/nsc-assistant/agents --register
#
#   # Bash-only agent (no Python boilerplate)
#   bash create-agent.sh my-new-agent /path/to/repo/agents --kind=bash
#
# Options:
#   --name "Display Name"
#   --description "What it does"
#   --category seo|research|fleet|personal|ops|misc   (default: misc)
#   --cron "0 4 * * *"                                (default: empty = manual)
#   --timezone "America/Detroit"                      (default: UTC)
#   --owner you@example.com                           (default: empty)
#   --kind python|bash                                (default: python)
#   --register                                         (POST to framework API after scaffold)
#   --framework-url http://localhost:8090

set -euo pipefail

# ── Argument parsing ────────────────────────────────────────────────────────

AGENT_ID="${1:-}"
TARGET_DIR="${2:-}"
shift $(( $# >= 2 ? 2 : $# )) || true

NAME=""
DESCRIPTION=""
CATEGORY="misc"
CRON=""
TIMEZONE="UTC"
OWNER=""
KIND="python"
REGISTER=0
FRAMEWORK_URL="${FRAMEWORK_API_URL:-http://localhost:8090}"

while [ $# -gt 0 ]; do
    case "$1" in
        --name)         NAME="$2"; shift 2 ;;
        --description)  DESCRIPTION="$2"; shift 2 ;;
        --category)     CATEGORY="$2"; shift 2 ;;
        --cron)         CRON="$2"; shift 2 ;;
        --timezone)     TIMEZONE="$2"; shift 2 ;;
        --owner)        OWNER="$2"; shift 2 ;;
        --kind)         KIND="$2"; shift 2 ;;
        --register)     REGISTER=1; shift ;;
        --framework-url) FRAMEWORK_URL="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "unknown option: $1" >&2
            exit 2
            ;;
    esac
done

# ── Validation ──────────────────────────────────────────────────────────────

if [ -z "$AGENT_ID" ] || [ -z "$TARGET_DIR" ]; then
    echo "usage: create-agent.sh <agent-id> <target-agents-dir> [options]" >&2
    echo "       run with --help for the full option list" >&2
    exit 2
fi

# Validate agent_id (kebab-case, no path traversal)
if ! [[ "$AGENT_ID" =~ ^[a-z][a-z0-9-]{1,62}$ ]]; then
    echo "ERROR: agent_id must be kebab-case, start with a letter, ≤63 chars" >&2
    echo "  got: $AGENT_ID" >&2
    exit 2
fi

case "$KIND" in
    python|bash) ;;
    *) echo "ERROR: --kind must be 'python' or 'bash' (got '$KIND')" >&2; exit 2 ;;
esac

if [ ! -d "$TARGET_DIR" ]; then
    mkdir -p "$TARGET_DIR"
fi
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

DEST_DIR="$TARGET_DIR/$AGENT_ID"
if [ -e "$DEST_DIR" ]; then
    echo "ERROR: $DEST_DIR already exists — refusing to overwrite" >&2
    exit 2
fi

# ── Locate the template ─────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE_DIR="$REPO_ROOT/_template/agent"
if [ ! -d "$TEMPLATE_DIR" ]; then
    echo "ERROR: template not found at $TEMPLATE_DIR" >&2
    exit 2
fi

# ── Compute defaults from inputs ────────────────────────────────────────────

NAME="${NAME:-$(echo "$AGENT_ID" | sed 's/-/ /g' | awk '{for(i=1;i<=NF;i++)$i=toupper(substr($i,1,1)) substr($i,2)} 1')}"
DESCRIPTION="${DESCRIPTION:-TODO: describe what $AGENT_ID does}"

# Class name (PascalCase)
CLASS_NAME=$(python3 -c "
parts = '$AGENT_ID'.split('-')
print(''.join(p.capitalize() for p in parts))
")

# Detect the source repo (the parent of $TARGET_DIR)
SOURCE_REPO="$(basename "$(cd "$TARGET_DIR/.." && pwd)")"
SOURCE_PATH="$(realpath --relative-to="$(cd "$TARGET_DIR/.." && pwd)" "$DEST_DIR" 2>/dev/null || echo "agents/$AGENT_ID")"

# Repo where the agent lives — try `git rev-parse` from the target dir
REGISTER_SCRIPT_REL=""
if [ -d "$TARGET_DIR/.git" ] || git -C "$TARGET_DIR" rev-parse --show-toplevel > /dev/null 2>&1; then
    GIT_TOPLEVEL=$(git -C "$TARGET_DIR" rev-parse --show-toplevel 2>/dev/null || echo "")
    if [ -f "$GIT_TOPLEVEL/agents/register-with-framework.sh" ]; then
        REGISTER_SCRIPT_REL="$GIT_TOPLEVEL/agents/register-with-framework.sh"
    elif [ -f "$GIT_TOPLEVEL/scripts/register-agents.sh" ]; then
        REGISTER_SCRIPT_REL="$GIT_TOPLEVEL/scripts/register-agents.sh"
    fi
fi
REGISTER_SCRIPT_REL="${REGISTER_SCRIPT_REL:-$REPO_ROOT/install/register-all-from-dir.sh \"$TARGET_DIR\"}"

# Default entry command — point at the run.sh (or the python script) we'll create
if [ "$KIND" = "python" ]; then
    ENTRY_COMMAND="bash $DEST_DIR/run.sh"
else
    ENTRY_COMMAND="bash $DEST_DIR/run.sh"
fi

CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ── Render the template ─────────────────────────────────────────────────────

mkdir -p "$DEST_DIR"

# Files we copy + render with placeholder substitution
ALWAYS_RENDER=(manifest.json AGENT.md SKILL.md README.md requirements.txt run.sh)
PYTHON_RENDER=(agent.py.template)

render_file() {
    local src="$1" dst="$2"
    if [ ! -f "$src" ]; then return 0; fi
    # Pass substitutions via env vars (avoids shell-quoting hazards inside the heredoc)
    AGENT_ID="$AGENT_ID" \
    AGENT_NAME="$NAME" \
    AGENT_DESCRIPTION="$DESCRIPTION" \
    AGENT_CATEGORY="$CATEGORY" \
    AGENT_CRON="$CRON" \
    AGENT_TIMEZONE="$TIMEZONE" \
    AGENT_OWNER="$OWNER" \
    AGENT_ENTRY_COMMAND="$ENTRY_COMMAND" \
    AGENT_CLASS_NAME="$CLASS_NAME" \
    ABSOLUTE_AGENT_DIR="$DEST_DIR" \
    FRAMEWORK_REPO_DIR="$REPO_ROOT" \
    REGISTER_SCRIPT_PATH="$REGISTER_SCRIPT_REL" \
    SOURCE_REPO="$SOURCE_REPO" \
    SOURCE_PATH="$SOURCE_PATH" \
    CREATED_AT="$CREATED_AT" \
    python3 - "$src" "$dst" <<'PY'
import os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    body = f.read()
keys = ["AGENT_ID", "AGENT_NAME", "AGENT_DESCRIPTION", "AGENT_CATEGORY",
        "AGENT_CRON", "AGENT_TIMEZONE", "AGENT_OWNER", "AGENT_ENTRY_COMMAND",
        "AGENT_CLASS_NAME", "ABSOLUTE_AGENT_DIR", "FRAMEWORK_REPO_DIR",
        "REGISTER_SCRIPT_PATH", "SOURCE_REPO", "SOURCE_PATH", "CREATED_AT"]
for k in keys:
    body = body.replace("{{" + k + "}}", os.environ.get(k, ""))
with open(dst, "w") as f:
    f.write(body)
PY
}

for f in "${ALWAYS_RENDER[@]}"; do
    render_file "$TEMPLATE_DIR/$f" "$DEST_DIR/$f"
done

if [ "$KIND" = "python" ]; then
    render_file "$TEMPLATE_DIR/agent.py.template" "$DEST_DIR/agent.py"
else
    # Bash-only: replace agent.py with a richer run.sh that includes work hooks
    cat > "$DEST_DIR/run.sh" <<BASH_AGENT
#!/usr/bin/env bash
# {{AGENT_NAME}} — bash entry script.
#
# Invoked by the framework's host-worker on "Run now" + by systemd timer
# for scheduled runs. Both paths set AGENT_ID, AGENT_RUN_ID,
# AGENT_TRIGGERED_BY in the env.

set -euo pipefail

SCRIPT_DIR="\$(cd "\$(dirname "\$0")" && pwd)"
echo "[$AGENT_ID] starting run \${AGENT_RUN_ID:-adhoc} (triggered_by=\${AGENT_TRIGGERED_BY:-manual})"

# ── Your work goes here ──────────────────────────────────────────────────────

# Example: append a heartbeat to a log
echo "\$(date -Is) [$AGENT_ID] heartbeat" >> /tmp/$AGENT_ID.log

# ────────────────────────────────────────────────────────────────────────────

echo "[$AGENT_ID] done"
BASH_AGENT
    # Replace placeholders in the just-written run.sh
    render_file "$DEST_DIR/run.sh" "$DEST_DIR/run.sh"
    rm -f "$DEST_DIR/agent.py"
fi

# Make scripts executable
chmod +x "$DEST_DIR/run.sh" 2>/dev/null || true
[ -f "$DEST_DIR/agent.py" ] && chmod +x "$DEST_DIR/agent.py" 2>/dev/null || true

echo ""
echo "✓ Created: $DEST_DIR"
echo "  files: $(ls "$DEST_DIR" | tr '\n' ' ')"
echo ""
echo "Next steps:"
echo "  1. Edit $DEST_DIR/AGENT.md (the runbook)"
if [ "$KIND" = "python" ]; then
    echo "  2. Edit $DEST_DIR/agent.py (implement run())"
else
    echo "  2. Edit $DEST_DIR/run.sh (implement the work)"
fi
echo "  3. Edit $DEST_DIR/manifest.json (description, owner, anything else)"
echo "  4. Register with the framework:"
echo "       bash $REGISTER_SCRIPT_REL"
echo ""

# ── Optional: register immediately ──────────────────────────────────────────

if [ "$REGISTER" -eq 1 ]; then
    echo "Registering with $FRAMEWORK_URL …"
    if FRAMEWORK_API_URL="$FRAMEWORK_URL" \
       bash "$REPO_ROOT/install/register-agent.sh" "$DEST_DIR"; then
        echo ""
        echo "  ✓ live at $FRAMEWORK_URL/api/agents/$AGENT_ID"
        echo "  ✓ UI:    http://localhost:8091/agents/$AGENT_ID"
    else
        echo "  ✗ registration failed (you can re-run manually after fixing)"
    fi
fi
