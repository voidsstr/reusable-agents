#!/usr/bin/env bash
# seo-implementer entry script.
#
# Reads $RESPONDER_REC_IDS + $RESPONDER_RUN_DIR (set by responder-agent or
# the dashboard) and drives an LLM session against AGENT.md to apply the
# proposed code changes. Then chains to seo-deployer if configured.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Required env
: "${SEO_AGENT_CONFIG:?SEO_AGENT_CONFIG must be set}"
: "${RESPONDER_REC_IDS:?RESPONDER_REC_IDS must be set (comma-separated rec ids)}"
: "${RESPONDER_RUN_DIR:?RESPONDER_RUN_DIR must be set}"

echo "[implementer] config=$SEO_AGENT_CONFIG"
echo "[implementer] recs=$RESPONDER_REC_IDS"
echo "[implementer] run_dir=$RESPONDER_RUN_DIR"

# Validate the run dir exists and has recommendations.json
if [ ! -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
    echo "ERROR: $RESPONDER_RUN_DIR/recommendations.json not found" >&2
    exit 2
fi

# Ensure changes/ dir exists
mkdir -p "$RESPONDER_RUN_DIR/changes"

# LLM driver:
#   IMPLEMENTER_LLM=claude     Claude Code CLI (default — uses the user's
#                              local Claude Code CLI auth, no framework
#                              provider needed). Best for code-editing
#                              tasks since Claude Code has tool use.
#   IMPLEMENTER_LLM=framework  Use the reusable-agents framework's
#                              configured AI provider (Azure / Anthropic
#                              / Ollama / Copilot) via framework.cli.ai_chat.
#                              Best when running unattended at scale.
#   IMPLEMENTER_LLM=noop       Dry-run / smoke-test (no LLM call).
IMPLEMENTER_LLM="${IMPLEMENTER_LLM:-claude}"

case "$IMPLEMENTER_LLM" in
    claude)
        # Claude Code CLI — feeds AGENT.md as the prompt
        if ! command -v claude > /dev/null 2>&1; then
            echo "ERROR: claude CLI not installed. Install Claude Code or set IMPLEMENTER_LLM." >&2
            exit 3
        fi
        # Build the prompt: prepend env summary, then AGENT.md
        PROMPT_FILE=$(mktemp)
        cat > "$PROMPT_FILE" <<EOF
You are seo-implementer. Process these recommendation(s):

  Rec IDs: $RESPONDER_REC_IDS
  Run dir: $RESPONDER_RUN_DIR
  Site config: $SEO_AGENT_CONFIG
  Site: ${RESPONDER_SITE:-?}
  Run ts: ${RESPONDER_RUN_TS:-?}

Your runbook is below — follow it exactly.

EOF
        cat "$SCRIPT_DIR/AGENT.md" >> "$PROMPT_FILE"
        # Run claude in non-interactive mode (one prompt, allow tool use)
        # Inherit env so AGENT_RUN_ID / etc reach the recorder if used
        claude --dangerously-skip-permissions --print < "$PROMPT_FILE" || {
            rc=$?
            echo "[implementer] claude exited rc=$rc" >&2
            rm -f "$PROMPT_FILE"
            exit $rc
        }
        rm -f "$PROMPT_FILE"
        ;;
    framework)
        # Use the reusable-agents framework's configured AI provider.
        # Resolves provider from agents/<id>/manifest.json or the global
        # default in config/ai-defaults.json. The framework writes the
        # response to a file the script can post-process (this stub just
        # captures it for inspection — production use would wire it back
        # into a code-editing flow).
        if [ ! -x "$REPO_ROOT/install/seed-providers.sh" ]; then
            echo "ERROR: reusable-agents not at $REPO_ROOT (set RA_REPO env)" >&2
            exit 3
        fi
        PROMPT_FILE=$(mktemp)
        cat > "$PROMPT_FILE" <<EOF
You are seo-implementer. Process recommendation(s): $RESPONDER_REC_IDS

Context dir: $RESPONDER_RUN_DIR
Site config: $SEO_AGENT_CONFIG

Read $SCRIPT_DIR/AGENT.md for the runbook and apply the named recommendations.
EOF
        OUT_FILE="$RESPONDER_RUN_DIR/changes/${RESPONDER_REC_IDS//,/-}.framework-ai-response.md"
        mkdir -p "$(dirname "$OUT_FILE")"
        cat "$PROMPT_FILE" | python3 -m framework.cli.ai_chat \
            --agent "${AGENT_ID:-seo-implementer}" \
            --system "$(cat "$SCRIPT_DIR/AGENT.md")" \
            > "$OUT_FILE" || {
                rc=$?
                echo "[implementer] framework AI call failed rc=$rc" >&2
                rm -f "$PROMPT_FILE"
                exit $rc
            }
        rm -f "$PROMPT_FILE"
        echo "[implementer] framework AI response saved to $OUT_FILE"
        ;;
    none|noop)
        # Dry-run / smoke-test mode — just log and exit
        echo "[implementer] IMPLEMENTER_LLM=$IMPLEMENTER_LLM — skipping actual implementation"
        ;;
    *)
        echo "ERROR: unknown IMPLEMENTER_LLM=$IMPLEMENTER_LLM" >&2
        exit 4
        ;;
esac

# Optional chain to seo-deployer. The deployer reads SEO_AGENT_CONFIG and
# the run dir, runs tests + builds + deploys.
DEPLOYER_SCRIPT="$REPO_ROOT/seo-deployer/run.sh"
if [ -x "$DEPLOYER_SCRIPT" ] && [ "${IMPLEMENTER_SKIP_DEPLOY:-0}" != "1" ]; then
    echo "[implementer] chaining to seo-deployer"
    SEO_AGENT_CONFIG="$SEO_AGENT_CONFIG" \
        bash "$DEPLOYER_SCRIPT" --run-dir "$RESPONDER_RUN_DIR" || {
            rc=$?
            echo "[implementer] deployer failed rc=$rc" >&2
            exit $rc
        }
fi

echo "[implementer] done"
