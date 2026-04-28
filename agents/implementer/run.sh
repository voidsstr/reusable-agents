#!/usr/bin/env bash
# implementer entry script.
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
# don't get inherited from the host-worker. Default depends on whether
# we have an Azure connection string available. Post-2026-04-27 SEO
# migration: the responder materializes runs from Azure, so syncing back
# requires Azure too. Auto-detect.
if [ -z "${STORAGE_BACKEND:-}" ]; then
    if [ -n "${AZURE_STORAGE_CONNECTION_STRING:-}" ]; then
        export STORAGE_BACKEND=azure
    else
        export STORAGE_BACKEND=local
    fi
fi
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
echo "[implementer] agent_id=${RESPONDER_AGENT_ID:-${RESPONDER_SOURCE_AGENT:-?}}"
echo "[implementer] run_ts=${RESPONDER_RUN_TS:-?}"

# Validate the run dir exists and has recommendations.json
if [ ! -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
    echo "ERROR: $RESPONDER_RUN_DIR/recommendations.json not found" >&2
    exit 2
fi

# Ensure changes/ dir exists
mkdir -p "$RESPONDER_RUN_DIR/changes"

# When the responder materialized this run dir from Azure, the env carries
# RESPONDER_AGENT_ID + RESPONDER_RUN_TS so we can sync recommendations.json
# updates + changes/ artifacts back to Azure when we exit. Captured here so
# the EXIT trap fires on every code path (success and failure both).
SYNC_BACK_AGENT_ID="${RESPONDER_AGENT_ID:-${RESPONDER_SOURCE_AGENT:-}}"
SYNC_BACK_RUN_TS="${RESPONDER_RUN_TS:-}"
SYNC_BACK_RUN_DIR="$RESPONDER_RUN_DIR"

_sync_back_to_azure() {
    if [ -z "$SYNC_BACK_AGENT_ID" ] || [ -z "$SYNC_BACK_RUN_TS" ]; then
        return 0
    fi
    if [ ! -d "$SYNC_BACK_RUN_DIR" ]; then
        return 0
    fi
    PYTHONPATH="$REPO_ROOT" \
    SYNC_BACK_AGENT_ID="$SYNC_BACK_AGENT_ID" \
    SYNC_BACK_RUN_TS="$SYNC_BACK_RUN_TS" \
    SYNC_BACK_RUN_DIR="$SYNC_BACK_RUN_DIR" \
    python3 - <<'PY' 2>&1 | sed 's/^/[sync-back] /'
import os
from framework.core.run_dir import RunDir
agent_id = os.environ["SYNC_BACK_AGENT_ID"]
run_ts = os.environ["SYNC_BACK_RUN_TS"]
local = os.environ["SYNC_BACK_RUN_DIR"]
rd = RunDir(agent_id, run_ts)
n = rd.sync_back(local, only_new=True, skip_globs=["data/page-cache/*"])
print(f"synced {n} files to agents/{agent_id}/runs/{run_ts}/")
PY
}

trap _sync_back_to_azure EXIT

# ── Live LLM output sidecar ─────────────────────────────────────────────────
# Push the tail of our dispatch log to agents/<RESPONDER_AGENT_ID>/live-llm-output.txt
# every ~3s so the dashboard's Live LLM tab can read it in one fast Azure
# blob round-trip. Without this, only the local /tmp dispatch log exists
# and the dashboard's fast-path read returns empty (the JSONL replay
# fallback only fires for Python agents that go through LLMStream).
#
# Best-effort: failure to start the sidecar must NOT abort the implementer
# run. The local /tmp log capture (via the responder's stdout/stderr
# redirect) is the durable copy; the sidecar just publishes a tail of it.
LLM_FLUSH_SIDECAR_PID=""
if [ -n "${DISPATCH_LOG_PATH:-}" ] && [ -n "${RESPONDER_AGENT_ID:-}" ]; then
    # Wait for the dispatch log to exist (responder creates it before
    # spawning us, but order-of-operations across systemd-run scopes is
    # not guaranteed) — poll up to 5 seconds.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -f "$DISPATCH_LOG_PATH" ] && break
        sleep 0.5
    done
    PYTHONPATH="$REPO_ROOT" python3 -m framework.core.llm_flush_sidecar \
        --agent-id "$RESPONDER_AGENT_ID" \
        --run-ts   "${RESPONDER_RUN_TS:-$(date -u +%Y%m%dT%H%M%SZ)}" \
        --watch    "$DISPATCH_LOG_PATH" \
        --flush-interval 3 \
        --max-bytes 262144 \
        > /dev/null 2>&1 &
    LLM_FLUSH_SIDECAR_PID=$!
    echo "[implementer] live-llm sidecar pid=$LLM_FLUSH_SIDECAR_PID watching=$DISPATCH_LOG_PATH agent=$RESPONDER_AGENT_ID"
fi

# Stop the sidecar on exit so its final flush flips is_active=false.
# Append to existing trap rather than replace it (keeps _sync_back_to_azure).
_stop_llm_flush_sidecar() {
    [ -n "$LLM_FLUSH_SIDECAR_PID" ] && kill -TERM "$LLM_FLUSH_SIDECAR_PID" 2>/dev/null || true
    [ -n "$LLM_FLUSH_SIDECAR_PID" ] && wait "$LLM_FLUSH_SIDECAR_PID" 2>/dev/null || true
}
trap '_sync_back_to_azure; _stop_llm_flush_sidecar' EXIT

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

# Detect dispatch type. The H2H agent (and any future "structured-data
# write" implementer-flavor) ships recommendations.json with a top-level
# `agent_id` ending in `-head-to-head-agent`. When detected, we use the
# H2H runbook + the H2H system prompt so Claude doesn't try to apply a
# code edit when its actual job is to call Claude itself, parse JSON,
# and upsert into comparison_commentary.
DISPATCH_KIND="seo"
SOURCE_AGENT_ID_FROM_RECS=""
if [ -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
    SOURCE_AGENT_ID_FROM_RECS=$(python3 -c "
import json, sys
try:
    d = json.load(open('$RESPONDER_RUN_DIR/recommendations.json'))
    # Newer runs write 'agent_id'; older PI/CR/catalog-audit runs only have
    # 'agent'. Either is a per-site id like 'aisleprompt-catalog-audit-agent'
    # for catalog-audit (where agent.py was updated to write both keys),
    # or the generic blueprint id for PI/CR (which is fine — the default
    # AGENT.md branch handles those).
    print(d.get('agent_id') or d.get('agent', ''))
except Exception:
    pass
" 2>/dev/null || echo "")
fi
case "$SOURCE_AGENT_ID_FROM_RECS" in
    *-head-to-head-agent)   DISPATCH_KIND="h2h" ;;
    *-article-author-agent) DISPATCH_KIND="article-author" ;;
    *-catalog-audit-agent)  DISPATCH_KIND="catalog-audit" ;;
esac
# Belt-and-braces: article-author also marks its recs with
# type='article-author-proposal'. Detect that case too in case the
# top-level agent_id field is missing (older runs).
if [ "$DISPATCH_KIND" = "seo" ] && [ -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
    if python3 -c "
import json, sys
try:
    d = json.load(open('$RESPONDER_RUN_DIR/recommendations.json'))
    if any(r.get('type') == 'article-author-proposal'
           for r in d.get('recommendations', [])):
        sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        DISPATCH_KIND="article-author"
    fi
fi
echo "[implementer] dispatch_kind=$DISPATCH_KIND source_agent_from_recs=$SOURCE_AGENT_ID_FROM_RECS"

case "$IMPLEMENTER_LLM" in
    claude)
        # Claude Code CLI — feeds AGENT.md (or H2H.md) as the prompt
        if ! command -v claude > /dev/null 2>&1; then
            echo "ERROR: claude CLI not installed. Install Claude Code or set IMPLEMENTER_LLM." >&2
            exit 3
        fi
        # Pick the runbook based on dispatch kind
        case "$DISPATCH_KIND" in
            h2h)            RUNBOOK="$SCRIPT_DIR/H2H.md" ;;
            catalog-audit)  RUNBOOK="$SCRIPT_DIR/CATALOG_AUDIT.md" ;;
            *)              RUNBOOK="$SCRIPT_DIR/AGENT.md" ;;
        esac
        if [ ! -f "$RUNBOOK" ]; then
            echo "ERROR: runbook $RUNBOOK not found" >&2
            exit 5
        fi
        echo "[implementer] using runbook=$RUNBOOK"
        # Build the prompt: prepend env summary, then runbook
        PROMPT_FILE=$(mktemp)
        cat > "$PROMPT_FILE" <<EOF
You are implementer. Process these recommendation(s):

  Rec IDs: $RESPONDER_REC_IDS
  Run dir: $RESPONDER_RUN_DIR
  Site config: $SEO_AGENT_CONFIG
  Site: ${RESPONDER_SITE:-?}
  Run ts: ${RESPONDER_RUN_TS:-?}
  Dispatch kind: $DISPATCH_KIND
  Source agent: $SOURCE_AGENT_ID_FROM_RECS

Your runbook is below — follow it exactly.

EOF
        cat "$RUNBOOK" >> "$PROMPT_FILE"
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
You are implementer. Process recommendation(s): $RESPONDER_REC_IDS

Context dir: $RESPONDER_RUN_DIR
Site config: $SEO_AGENT_CONFIG

Read $SCRIPT_DIR/AGENT.md for the runbook and apply the named recommendations.
EOF
        OUT_FILE="$RESPONDER_RUN_DIR/changes/${RESPONDER_REC_IDS//,/-}.framework-ai-response.md"
        mkdir -p "$(dirname "$OUT_FILE")"
        cat "$PROMPT_FILE" | python3 -m framework.cli.ai_chat \
            --agent "${AGENT_ID:-implementer}" \
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
# H2H dispatches don't ship code — they upsert DB rows. Skip the
# deployer entirely so we don't trigger an unintended site rebuild.
if [ "$DISPATCH_KIND" = "h2h" ]; then
    echo "[implementer] H2H dispatch — skipping deployer chain"
elif [ "$DISPATCH_KIND" = "article-author" ]; then
    echo "[implementer] article-author dispatch — skipping deployer chain (articles ship via DB insert, no build)"
elif [ "$DISPATCH_KIND" = "catalog-audit" ]; then
    echo "[implementer] catalog-audit dispatch — skipping deployer chain (DB-only fixes, no build)"
elif [ -x "$DEPLOYER_SCRIPT" ] && [ "${IMPLEMENTER_SKIP_DEPLOY:-0}" != "1" ]; then
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

# The completion email is conceptually "the responder reporting back to
# the user that their reply has been actioned". So the user-facing agent
# id is responder-agent — that's what shows in the subject + tracking
# path (agents/responder-agent/outbound-emails/<request-id>.completion.json).
# The actual code-editing implementer (implementer) is internal detail.
COMPLETION_AGENT_ID="${COMPLETION_EMAIL_FROM_AGENT:-responder-agent}"

PYTHONPATH="$REPO_ROOT" python3 -m framework.core.completion_email \
    --agent-id "$COMPLETION_AGENT_ID" \
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

# ── End-of-run results file ────────────────────────────────────────────────
# Persist the implementer's full claude --print output + summary metadata
# to framework storage so it's queryable from the dashboard's per-run
# artifact view. The /tmp dispatch log is host-only (deleted on reboot).
PYTHONPATH="$REPO_ROOT" \
SOURCE_AGENT="$SOURCE_AGENT" \
IMPL_AGENT_ID="${IMPLEMENTER_AGENT_ID:-implementer}" \
RESPONDER_RUN_TS="${RESPONDER_RUN_TS:-}" \
RESPONDER_REC_IDS="$RESPONDER_REC_IDS" \
RESPONDER_SITE="${RESPONDER_SITE:-}" \
RESPONDER_RUN_DIR="${RESPONDER_RUN_DIR:-}" \
DISPATCH_LOG_PATH="${DISPATCH_LOG_PATH:-}" \
GIT_SHA="$GIT_SHA" \
python3 - <<'PY' 2>/dev/null || true
import json, os, sys
from datetime import datetime, timezone
from framework.core.storage import get_storage
s = get_storage()
agent_id = os.environ.get("IMPL_AGENT_ID", "implementer")
run_ts = os.environ.get("RESPONDER_RUN_TS") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

# Read the dispatch log (the actual claude --print output) — fall back to
# the most-recent dispatch log for this site if DISPATCH_LOG_PATH wasn't set.
log_path = os.environ.get("DISPATCH_LOG_PATH") or ""
if not log_path:
    log_dir = "/tmp/reusable-agents-logs"
    site = os.environ.get("RESPONDER_SITE", "")
    if os.path.isdir(log_dir):
        from glob import glob
        matches = sorted(glob(f"{log_dir}/dispatch-implementer-{site}-*.log"),
                          key=os.path.getmtime, reverse=True)
        log_path = matches[0] if matches else ""

llm_output = ""
if log_path and os.path.isfile(log_path):
    try:
        with open(log_path) as f:
            llm_output = f.read()
    except Exception:
        pass

results = {
    "schema_version": "1",
    "agent_id": agent_id,
    "run_ts": run_ts,
    "source_agent": os.environ.get("SOURCE_AGENT", ""),
    "site": os.environ.get("RESPONDER_SITE", ""),
    "rec_ids": (os.environ.get("RESPONDER_REC_IDS", "") or "").split(","),
    "run_dir": os.environ.get("RESPONDER_RUN_DIR", ""),
    "git_sha": os.environ.get("GIT_SHA", ""),
    "dispatch_log_path": log_path,
    "llm_output": llm_output,
    "llm_output_chars": len(llm_output),
    "ai_provider": "claude-cli",
    "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
}
try:
    s.write_json(f"agents/{agent_id}/runs/{run_ts}/results.json", results)
    # Also save just the LLM stdout as a separate text file for easy
    # download / grep from the dashboard's Storage tab.
    s.write_text(f"agents/{agent_id}/runs/{run_ts}/llm-output.txt", llm_output or "")
except Exception as e:
    print(f"[implementer] results.json write failed: {e}", file=sys.stderr)
PY

echo "[implementer] done"
