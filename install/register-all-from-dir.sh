#!/usr/bin/env bash
# Walk a directory of agent subdirs and register each with the framework.
# Each subdir must contain a manifest.json — others (lib/, _legacy-*, etc.) are skipped.
#
# Usage:
#   register-all-from-dir.sh <agents-parent-dir>
#   register-all-from-dir.sh /home/voidsstr/development/nsc-assistant/agents

set -euo pipefail

PARENT_DIR="${1:-}"
if [ -z "$PARENT_DIR" ]; then
    echo "usage: register-all-from-dir.sh <agents-parent-dir>" >&2
    exit 2
fi
PARENT_DIR="$(cd "$PARENT_DIR" && pwd)"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REGISTER="$SCRIPT_DIR/register-agent.sh"

n_ok=0
n_skip=0
n_err=0
echo "Scanning $PARENT_DIR for agents…"
for d in "$PARENT_DIR"/*/; do
    name=$(basename "$d")
    case "$name" in
        lib|_legacy-*|_template|tests|*.bak)
            n_skip=$((n_skip+1))
            continue
            ;;
    esac
    if [ ! -f "$d/manifest.json" ]; then
        n_skip=$((n_skip+1))
        continue
    fi
    # Skip blueprints (code-only entries with no per-site config) — they're
    # documentation, not runnable agents. Site-specific instances in customer
    # repos register the actual scheduled agent.
    if python3 -c "import json,sys; d=json.load(open('$d/manifest.json')); sys.exit(0 if d.get('metadata',{}).get('is_blueprint') else 1)" 2>/dev/null; then
        echo "  ⊘ $name (blueprint — skipped)"
        n_skip=$((n_skip+1))
        continue
    fi
    if bash "$REGISTER" "$d"; then
        n_ok=$((n_ok+1))
    else
        n_err=$((n_err+1))
        echo "  ✗ $name failed"
    fi
done

echo ""
echo "registered: $n_ok | skipped: $n_skip | failed: $n_err"
[ "$n_err" -eq 0 ]
