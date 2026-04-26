#!/usr/bin/env bash
# seo-implementer entry script.
#
# Reads $RESPONDER_REC_IDS + $RESPONDER_RUN_DIR (set by responder-agent or
# the dashboard) and drives an LLM session against AGENT.md to apply the
# proposed code changes. Then chains to seo-deployer if configured.

set -euo pipefail

# Ensure user-local bins (where claude / claude-cli live) are reachable
# even when invoked from systemd-user services that don't inherit
# interactive shell PATH.
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# Trace mode (debugging silent failures from systemd spawn).
# Disable with IMPLEMENTER_TRACE=0 in env.
if [ "${IMPLEMENTER_TRACE:-1}" != "0" ]; then
    set -x
    export PS4='+[$(date +%H:%M:%S)] '
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Storage env — when dispatched via systemd-run by the responder, these
# don't get inherited from the host-worker. Default to local FS so the
# completion-email module can write its tracking file alongside other
# framework storage.
export STORAGE_BACKEND="${STORAGE_BACKEND:-local}"
export AGENT_STORAGE_LOCAL_PATH="${AGENT_STORAGE_LOCAL_PATH:-$HOME/.reusable-agents/data}"

# If the responder dispatched us, it sets RESPONDER_SITE — derive
# SEO_AGENT_CONFIG from the well-known site-config dir.
if [ -z "${SEO_AGENT_CONFIG:-}" ] && [ -n "${RESPONDER_SITE:-}" ]; then
    CANDIDATE="$REPO_ROOT/examples/sites/$RESPONDER_SITE.yaml"
    if [ -f "$CANDIDATE" ]; then
        export SEO_AGENT_CONFIG="$CANDIDATE"
        echo "[implementer] derived SEO_AGENT_CONFIG=$SEO_AGENT_CONFIG from RESPONDER_SITE=$RESPONDER_SITE"
    fi
fi

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
        # Run claude in non-interactive mode (one prompt, allow tool use).
        # --output-format text     deterministic stdout
        # --max-turns N            bound the agentic loop (without it, claude
        #                          can spin indefinitely on multi-rec runs)
        # Inherit env so AGENT_RUN_ID / etc reach the recorder if used.
        # NOTE: removed --no-session-persistence — older claude builds reject
        # this flag. Session files are written to ~/.claude regardless.
        IMPLEMENTER_MAX_TURNS="${IMPLEMENTER_MAX_TURNS:-200}"
        claude --dangerously-skip-permissions \
               --print --output-format text \
               --max-turns "$IMPLEMENTER_MAX_TURNS" \
               < "$PROMPT_FILE" || {
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


# ── Send completion-confirmation email to the user ───────────────────────────
# The user originally got a recs email; now that we've shipped the recs they
# selected, email them back so they know it's done. This delegates to the
# framework's completion_email module so the same template/lookup flow is
# reused by every implementer-style agent.
GIT_SHA=""
IMPL_REPO="${IMPLEMENTER_REPO_PATH:-}"
[ -d "$IMPL_REPO/.git" ] && GIT_SHA=$(git -C "$IMPL_REPO" log -1 --format='%H' 2>/dev/null || echo "")

# Walk the run dir's recommendations.json to extract titles for nicer email body
REC_TITLES_JSON=""
if [ -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
    REC_TITLES_JSON=$(mktemp)
    python3 - "$RESPONDER_RUN_DIR/recommendations.json" "$REC_TITLES_JSON" <<'PY' 2>/dev/null || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    titles = {r.get("id",""): (r.get("title") or r.get("description") or "")[:140]
              for r in d.get("recommendations", [])}
    json.dump(titles, open(sys.argv[2], "w"))
except Exception:
    pass
PY
fi

# Source agent: where the rec originated. RESPONDER_SOURCE_AGENT is set by the
# framework-aware responder when it dispatches; legacy paths derive from site.
SOURCE_AGENT="${RESPONDER_SOURCE_AGENT:-${RESPONDER_SITE:-}-seo-opportunity-agent}"

# Hard-code our own agent id — AGENT_ID env may have been inherited from the
# responder service ("responder-agent") since we run inside its dispatched
# scope. Use a different, locally-set variable so tracking lands at
# agents/seo-implementer/outbound-emails/.
IMPL_AGENT_ID="${SEO_IMPLEMENTER_AGENT_ID:-seo-implementer}"

PYTHONPATH="$REPO_ROOT" python3 -m framework.core.completion_email \
    --agent-id "$IMPL_AGENT_ID" \
    --rec-ids "$RESPONDER_REC_IDS" \
    --site "${RESPONDER_SITE:-}" \
    --source-agent "$SOURCE_AGENT" \
    --request-id "${RESPONDER_REQUEST_ID:-${RESPONDER_RUN_TS:-}}" \
    --run-dir "${RESPONDER_RUN_DIR:-}" \
    --commit-sha "$GIT_SHA" \
    --mode "${IMPLEMENTER_LLM:-claude}" \
    --site-config "${SEO_AGENT_CONFIG:-}" \
    ${REC_TITLES_JSON:+--rec-titles-json "$REC_TITLES_JSON"} \
    --to "${IMPLEMENTER_NOTIFY_EMAIL:-}" \
    --sender "${IMPLEMENTER_FROM:-automation@northernsoftwareconsulting.com}" \
    --msmtp-account "${IMPLEMENTER_MSMTP_ACCOUNT:-automation}" \
    --dashboard-base "${FRAMEWORK_DASHBOARD_URL:-http://localhost:8091}" \
    || echo "[implementer] completion email failed (non-fatal)" >&2

[ -n "$REC_TITLES_JSON" ] && rm -f "$REC_TITLES_JSON"

echo "[implementer] done"
