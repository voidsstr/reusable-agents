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


# ── Send completion-confirmation email to the user ───────────────────────────
# The user originally got a recs email; now that we've shipped the recs they
# selected, email them back so they know it's done.
COMPLETION_TO="${IMPLEMENTER_NOTIFY_EMAIL:-}"
COMPLETION_FROM="${IMPLEMENTER_FROM:-automation@northernsoftwareconsulting.com}"
COMPLETION_ACCOUNT="${IMPLEMENTER_MSMTP_ACCOUNT:-automation}"
# Default recipient: pull `to` from the SEO config if available
if [ -z "$COMPLETION_TO" ] && [ -n "${SEO_AGENT_CONFIG:-}" ] && [ -f "$SEO_AGENT_CONFIG" ]; then
    COMPLETION_TO=$(python3 - "$SEO_AGENT_CONFIG" 2>/dev/null <<'PY' || true
import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1]))
    rcp = (((cfg.get("reporter") or {}).get("email") or {}).get("to") or [])
    print(",".join(rcp))
except Exception:
    pass
PY
)
fi

if [ -n "$COMPLETION_TO" ] && command -v msmtp > /dev/null 2>&1; then
    SUBJECT="[seo-implementer:done] Shipped recs ${RESPONDER_REC_IDS} — site=${RESPONDER_SITE:-unknown}"
    GIT_SHA=""
    if [ -n "${RESPONDER_RUN_DIR:-}" ]; then
        # Try to find a recent commit reflecting this rec's changes
        IMPL_REPO="${IMPLEMENTER_REPO_PATH:-}"
        [ -d "$IMPL_REPO/.git" ] && GIT_SHA=$(git -C "$IMPL_REPO" log -1 --format='%H' 2>/dev/null || echo "")
    fi
    BODY=$(cat <<EOF
<!doctype html>
<html><body style="font-family:sans-serif;color:#0f172a;line-height:1.5">
<div style="max-width:680px;margin:0 auto;padding:20px;border:1px solid #e2e8f0;border-radius:6px">
<h2 style="margin:0 0 12px 0">✓ Shipped: ${RESPONDER_REC_IDS}</h2>
<div style="color:#475569;font-size:14px">
The recommendations you selected have been applied. Details:
</div>
<table style="margin-top:12px;font-size:13px;border-collapse:collapse">
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Site</td><td>${RESPONDER_SITE:-—}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Run dir</td><td><code>${RESPONDER_RUN_DIR:-—}</code></td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Commit</td><td><code>${GIT_SHA:-pending}</code></td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Mode</td><td>${IMPLEMENTER_LLM:-claude}</td></tr>
</table>
<div style="color:#64748b;font-size:12px;margin-top:14px">
Sent automatically by seo-implementer after rec dispatch. View the full run
in the dashboard at http://localhost:8080/agents/seo-implementer.
</div>
</div></body></html>
EOF
)
    {
        echo "From: $COMPLETION_FROM"
        echo "To: $COMPLETION_TO"
        echo "Subject: $SUBJECT"
        echo "MIME-Version: 1.0"
        echo "Content-Type: text/html; charset=utf-8"
        echo "X-Reusable-Agent: seo-implementer"
        echo ""
        echo "$BODY"
    } | msmtp -a "$COMPLETION_ACCOUNT" "$COMPLETION_TO" \
        || echo "[implementer] completion email failed (non-fatal)" >&2
    echo "[implementer] sent completion email to $COMPLETION_TO"
fi

echo "[implementer] done"
