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

# Route claude through the pool (round-robin across Max accounts). The
# pool writes a `claude` shim at $CLAUDE_POOL_ROOT/bin/ on init. CLAUDE_POOL=0
# disables routing and falls back to the user's default claude account.
CLAUDE_POOL_ROOT="${CLAUDE_POOL_ROOT:-$HOME/.reusable-agents/claude-pool}"
if [ "${CLAUDE_POOL:-1}" != "0" ] && [ -x "$CLAUDE_POOL_ROOT/bin/claude" ]; then
    export PATH="$CLAUDE_POOL_ROOT/bin:$PATH"
    echo "[implementer] claude-pool routing active (root=$CLAUDE_POOL_ROOT)" >&2
fi

# Route the implementer's claude --print calls through Cloudflare WARP's
# local SOCKS5 proxy so we don't hammer Anthropic's edge from the same
# IP as the interactive Claude Code session. On 2026-05-02 a burst of
# 8 parallel implementer dispatches tripped Anthropic's IP-level rate
# limit and blocked all accounts from this IP for hours. Routing only
# the batched implementer calls through a separate egress IP (WARP)
# isolates that load. Wrapper falls back to direct exec if WARP isn't
# running, so this never breaks the implementer if the proxy is down.
# Disable with IMPLEMENTER_USE_PROXY=0.
if [ "${IMPLEMENTER_USE_PROXY:-1}" != "0" ] && [ -x "$HOME/.local/bin/claude-via-proxy" ]; then
    export CLAUDE_POOL_REAL_CLAUDE="$HOME/.local/bin/claude-via-proxy"
    echo "[implementer] proxy wrapper active (CLAUDE_POOL_REAL_CLAUDE=$CLAUDE_POOL_REAL_CLAUDE)" >&2
fi

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

# ── Implementer-side run record (dashboard visibility) ──────────────────────
# Each dispatch is a first-class implementer run. Write progress.json +
# run-index entry at agents/implementer/runs/<IMPLEMENTER_RUN_TS>/ so the
# dashboard's implementer Runs tab shows every dispatched batch. Falls back
# to a fresh ts if dispatch.py didn't supply one (e.g. manual invocation).
IMPLEMENTER_RUN_TS="${IMPLEMENTER_RUN_TS:-$(date -u +%Y%m%dT%H%M%SZ)}"
export IMPLEMENTER_RUN_TS

_record_implementer_start() {
    PYTHONPATH="$REPO_ROOT" python3 -m framework.cli.dispatch_run_record start \
        --run-ts        "$IMPLEMENTER_RUN_TS" \
        --source-agent  "${RESPONDER_AGENT_ID:-${RESPONDER_SOURCE_AGENT:-}}" \
        --source-run-ts "${RESPONDER_RUN_TS:-}" \
        --site          "${RESPONDER_SITE:-}" \
        --request-id    "${RESPONDER_REQUEST_ID:-}" \
        --subject-tag   "${RESPONDER_SUBJECT_TAG:-}" \
        --rec-ids       "${RESPONDER_REC_IDS:-}" \
        --log-path      "${DISPATCH_LOG_PATH:-}" \
        2>&1 | sed 's/^/[run-record] /' || true
}

_record_implementer_end() {
    local rc=$1
    PYTHONPATH="$REPO_ROOT" python3 -m framework.cli.dispatch_run_record end \
        --rc            "$rc" \
        --run-ts        "$IMPLEMENTER_RUN_TS" \
        --source-agent  "${RESPONDER_AGENT_ID:-${RESPONDER_SOURCE_AGENT:-}}" \
        --source-run-ts "${RESPONDER_RUN_TS:-}" \
        --site          "${RESPONDER_SITE:-}" \
        --request-id    "${RESPONDER_REQUEST_ID:-}" \
        --subject-tag   "${RESPONDER_SUBJECT_TAG:-}" \
        --rec-ids       "${RESPONDER_REC_IDS:-}" \
        --log-path      "${DISPATCH_LOG_PATH:-}" \
        2>&1 | sed 's/^/[run-record] /' || true
}

_record_implementer_start

# Validate the run dir exists and has recommendations.json
if [ ! -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
    echo "ERROR: $RESPONDER_RUN_DIR/recommendations.json not found" >&2
    exit 2
fi

# Ensure changes/ dir exists
mkdir -p "$RESPONDER_RUN_DIR/changes"

# ── Materialize per-rec deep context (framework convention) ─────────────────
# Producing agents may stash extra supporting material at
# agents/<source>/runs/<run_ts>/rec-context/<rec-id>/. Pull every rec id in
# this batch's $RESPONDER_REC_IDS down to the run dir so claude can read
# attachments directly off disk. See framework/core/rec_context.py.
if [ -n "${RESPONDER_REC_IDS:-}" ] && [ -n "${RESPONDER_AGENT_ID:-}" ] && [ -n "${RESPONDER_RUN_TS:-}" ]; then
    PYTHONPATH="$REPO_ROOT" \
    SOURCE_AGENT_ID="$RESPONDER_AGENT_ID" \
    SOURCE_RUN_TS="$RESPONDER_RUN_TS" \
    REC_IDS="$RESPONDER_REC_IDS" \
    TARGET_DIR="$RESPONDER_RUN_DIR" \
    python3 - <<'PY' 2>&1 | sed 's/^/[rec-context] /' || true
import os, sys
from framework.core.rec_context import materialize_rec_context_to_dir
agent = os.environ["SOURCE_AGENT_ID"]
ts = os.environ["SOURCE_RUN_TS"]
target = os.environ["TARGET_DIR"]
recs = [r.strip() for r in os.environ.get("REC_IDS","").split(",") if r.strip()]
n = 0
for rid in recs:
    out = materialize_rec_context_to_dir(agent, ts, rid, target)
    if out:
        n += 1
print(f"materialized {n}/{len(recs)} rec-context bundles → {target}/rec-context/", file=sys.stderr)
PY
fi

# ── Handoff routing — recs with handoff_target go to the named agent ───────
# The seo-analyzer tags each rec with handoff_target when its rec_type
# implies content-team / specialist work that the implementer (a code
# editor) shouldn't try to ship itself. We send handoff messages here
# and filter those rec ids out of RESPONDER_REC_IDS so the LLM session
# below only sees recs we're actually expected to ship.
#
# When the entire batch is handed off, we exit cleanly (rc=0) — the
# downstream agents will pick up the work on their next run.
if [ -n "${RESPONDER_REC_IDS:-}" ] && [ -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
    HANDOFF_OUTPUT=$(
        PYTHONPATH="$REPO_ROOT" \
        SOURCE_AGENT_ID="${RESPONDER_AGENT_ID:-}" \
        SOURCE_RUN_TS="${RESPONDER_RUN_TS:-}" \
        REC_IDS="$RESPONDER_REC_IDS" \
        RUN_DIR="$RESPONDER_RUN_DIR" \
        python3 - <<'PY' 2>&1
import json, os, sys
sys.path.insert(0, os.environ.get("REPO_ROOT") or os.getcwd())
try:
    from framework.core.handoff import send_handoff
except Exception as e:
    print(f"FRAMEWORK_IMPORT_FAILED: {e}", file=sys.stderr)
    print("KEEPS: " + os.environ.get("REC_IDS", ""))
    sys.exit(0)

run_dir = os.environ["RUN_DIR"]
src_agent = os.environ.get("SOURCE_AGENT_ID", "")
src_ts = os.environ.get("SOURCE_RUN_TS", "")
batch_ids = {r.strip() for r in os.environ.get("REC_IDS", "").split(",") if r.strip()}

try:
    recs_doc = json.load(open(os.path.join(run_dir, "recommendations.json")))
except Exception as e:
    print(f"RECS_READ_FAILED: {e}", file=sys.stderr)
    print("KEEPS: " + os.environ.get("REC_IDS", ""))
    sys.exit(0)

recs = recs_doc.get("recommendations", []) if isinstance(recs_doc, dict) else recs_doc
recs_by_id = {r.get("id"): r for r in (recs or []) if r.get("id")}

handoff_count = 0
deduped_count = 0
keeps = []
handoff_log = []

# ── Dedupe: don't re-send a handoff for a (target, src_ts, rec_id) tuple
# that's already in the receiver's queue OR was processed in the past 7
# days. Without this, every SEO run re-sends the same coverage-gap recs
# and the receiver's queue churns. Cheap to compute — list_prefix is
# already used by drain_handoffs, just one extra read per receiver.
try:
    from framework.core.storage import get_storage as _get_storage
    _store = _get_storage()
    _recent_signatures: dict[str, set[tuple[str, str]]] = {}

    def _signature_set_for(target: str) -> set[tuple[str, str]]:
        """Set of (source_run_ts, rec_id) tuples this target has seen.
        Includes both pending queue + past 7 days of processed."""
        if target in _recent_signatures:
            return _recent_signatures[target]
        sigs: set[tuple[str, str]] = set()
        for prefix in (
            f"agents/{target}/handoff-queue/",
            f"agents/{target}/handoff-processed/",
        ):
            for k in _store.list_prefix(prefix, limit=500):
                env = _store.read_json(k) or {}
                ts = env.get("source_run_ts") or ""
                rid = env.get("rec_id") or ""
                if rid:
                    sigs.add((ts, rid))
        _recent_signatures[target] = sigs
        return sigs
except Exception as _e:
    print(f"DEDUPE_INIT_FAILED: {_e}", file=sys.stderr)
    _signature_set_for = None  # type: ignore[assignment]

for rid in sorted(batch_ids):
    rec = recs_by_id.get(rid)
    if not rec:
        keeps.append(rid)
        continue
    target = (rec.get("handoff_target") or "").strip()
    work_type = (rec.get("work_type") or "").strip() or "code_edit"
    if not target:
        keeps.append(rid)
        continue
    # Dedupe check: same (target, src_ts, rec_id) sent before? skip.
    if _signature_set_for is not None:
        try:
            sigs = _signature_set_for(target)
            if (src_ts, rid) in sigs:
                deduped_count += 1
                handoff_log.append({
                    "rec_id": rid, "to": target, "work_type": work_type,
                    "request_id": "(deduped — already in receiver's queue/archive)",
                })
                continue
        except Exception as e:
            print(f"DEDUPE_CHECK_FAILED rec={rid}: {e}", file=sys.stderr)
    try:
        request_id = send_handoff(
            from_agent="implementer",
            to_agent=target,
            work_type=work_type,
            rec_id=rid,
            rec=rec,
            source_run_ts=src_ts,
            source_agent=src_agent,
            rationale=("Routed by analyzer's handoff tagger — "
                        f"rec.type={rec.get('type','?')} maps to "
                        f"work_type={work_type} handled by {target}."),
        )
        handoff_count += 1
        handoff_log.append({
            "rec_id": rid, "to": target, "work_type": work_type,
            "request_id": request_id,
        })
    except Exception as e:
        # If the handoff itself fails, fall back to keeping the rec on
        # the implementer's plate — better to defer than drop.
        print(f"HANDOFF_FAILED rec={rid} target={target}: {e}",
              file=sys.stderr)
        keeps.append(rid)

print(f"HANDOFFS: {handoff_count}  deduped={deduped_count}", file=sys.stderr)
for h in handoff_log:
    print(f"  → {h['rec_id']} → {h['to']} ({h['work_type']})  request_id={h['request_id']}",
          file=sys.stderr)

# Persist a sidecar so the post-run summary can attribute "shipped" vs
# "handed off" cleanly in the digest email.
sidecar = os.path.join(run_dir, "handoffs-sent.json")
with open(sidecar, "w") as f:
    json.dump({"handoffs": handoff_log, "kept_for_self": keeps}, f, indent=2)

print("KEEPS: " + ",".join(keeps))
PY
    )
    HANDOFF_KEEPS=$(echo "$HANDOFF_OUTPUT" | grep -E "^KEEPS:" | sed 's/^KEEPS: *//')
    HANDOFF_LOG=$(echo "$HANDOFF_OUTPUT" | grep -vE "^KEEPS:")
    if [ -n "$HANDOFF_LOG" ]; then
        echo "$HANDOFF_LOG" | sed 's/^/[handoff] /'
    fi
    if [ -z "$HANDOFF_KEEPS" ]; then
        echo "[handoff] all recs in batch routed to specialist agents — implementer has nothing to ship locally"
        echo "[implementer] done agent_id=${RESPONDER_AGENT_ID:-?} run_ts=${RESPONDER_RUN_TS:-?} (handoff-only batch)"
        exit 0
    fi
    if [ "$HANDOFF_KEEPS" != "$RESPONDER_REC_IDS" ]; then
        echo "[handoff] filtered RESPONDER_REC_IDS: $RESPONDER_REC_IDS → $HANDOFF_KEEPS"
        export RESPONDER_REC_IDS="$HANDOFF_KEEPS"
    fi
fi

# ── Pre-run git SHA capture ─────────────────────────────────────────────────
# Used at end-of-run to verify a NEW commit happened — the only reliable
# signal that claude actually shipped code (vs. bailing out asking for
# clarification). Read repo_path from the site config; export so the
# end-of-run logic can use it.
GIT_SHA_BEFORE=""
if [ -z "${IMPLEMENTER_REPO_PATH:-}" ] && [ -f "$SEO_AGENT_CONFIG" ]; then
    IMPLEMENTER_REPO_PATH=$(python3 - "$SEO_AGENT_CONFIG" <<'PY' 2>/dev/null
import sys
try:
    import yaml
    cfg = yaml.safe_load(open(sys.argv[1]))
    impl = (cfg or {}).get("implementer") or {}
    print(impl.get("repo_path", ""))
except Exception:
    pass
PY
    )
    [ -n "$IMPLEMENTER_REPO_PATH" ] && export IMPLEMENTER_REPO_PATH
fi
# Article-author / catalog-audit dispatches don't have an
# implementer.repo_path in their site.yaml (they're DB-only operations).
# Fall back to the canonical site repo location so the framework code-
# editor chain has a CWD to run psql from. Keeps the framework primitive
# generic — sites name their repo location once via the convention
# /home/voidsstr/development/<site>.
if [ -z "${IMPLEMENTER_REPO_PATH:-}" ] && [ -n "${RESPONDER_SITE:-}" ]; then
    SITE_REPO_GUESS="/home/voidsstr/development/${RESPONDER_SITE}"
    if [ -d "$SITE_REPO_GUESS" ]; then
        export IMPLEMENTER_REPO_PATH="$SITE_REPO_GUESS"
        echo "[implementer] derived IMPLEMENTER_REPO_PATH=$IMPLEMENTER_REPO_PATH from RESPONDER_SITE=$RESPONDER_SITE (no implementer.repo_path in site.yaml)"
    fi
fi
# Use `git rev-parse` which walks up to find .git — the customer-app dir
# may itself not be a git root (e.g. "Customer Applications/aisleprompt/v1.0"
# is a subdir of the nsc-assistant repo, no .git of its own). The earlier
# `[ -d "$path/.git" ]` test missed this and produced false-paused status.
if [ -n "${IMPLEMENTER_REPO_PATH:-}" ] && [ -d "$IMPLEMENTER_REPO_PATH" ] \
        && git -C "$IMPLEMENTER_REPO_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_SHA_BEFORE=$(git -C "$IMPLEMENTER_REPO_PATH" log -1 --format='%H' 2>/dev/null || echo "")
    GIT_TOPLEVEL=$(git -C "$IMPLEMENTER_REPO_PATH" rev-parse --show-toplevel 2>/dev/null || echo "")
    echo "[implementer] pre-run git_sha=${GIT_SHA_BEFORE:0:8} repo=$IMPLEMENTER_REPO_PATH (toplevel=$GIT_TOPLEVEL)"
fi
export GIT_SHA_BEFORE

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

trap '_rc=$?; _record_implementer_end "$_rc"; _sync_back_to_azure' EXIT

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
trap '_rc=$?; _record_implementer_end "$_rc"; _sync_back_to_azure; _stop_llm_flush_sidecar' EXIT

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

# DB access for dispatches that write rows directly (article-author writes
# to editorial_articles / buying_guides; h2h writes to comparison_commentary;
# catalog-audit writes to product flag tables). The responder's process env
# doesn't carry DATABASE_URL, so we need a way to resolve it per-site
# without hardcoding which sites exist. Resolution order:
#   1. DATABASE_URL already in env — use it.
#   2. Per-site env var DATABASE_URL_<UPPER_SNAKE_SITE> (e.g.
#      DATABASE_URL_SPECPICKS, DATABASE_URL_AISLEPROMPT) seeded by
#      ~/.reusable-agents/secrets.env.
#   3. site.yaml's database.url_env → look that env var up.
#   4. The site.yaml's site.id literal in DATABASE_URL_<UPPER>.
# No hardcoded connection strings — adding a new site is purely
# secrets.env + site.yaml work.
if [ -z "${DATABASE_URL:-}" ] && [ -n "${RESPONDER_SITE:-}" ]; then
    SITE_UPPER="$(echo "$RESPONDER_SITE" | tr '[:lower:]-' '[:upper:]_')"
    PER_SITE_VAR="DATABASE_URL_${SITE_UPPER}"
    if [ -n "${!PER_SITE_VAR:-}" ]; then
        export DATABASE_URL="${!PER_SITE_VAR}"
        echo "[implementer] resolved DATABASE_URL from \$$PER_SITE_VAR" >&2
    fi
fi
# Also honor an explicit cfg.database.url_env from SEO_AGENT_CONFIG.
if [ -z "${DATABASE_URL:-}" ] && [ -f "${SEO_AGENT_CONFIG:-/dev/null}" ]; then
    URL_ENV=$(python3 -c "
import sys, yaml
try:
    cfg = yaml.safe_load(open('$SEO_AGENT_CONFIG'))
    print((cfg.get('database') or {}).get('url_env', ''))
except Exception:
    pass
" 2>/dev/null)
    if [ -n "$URL_ENV" ] && [ -n "${!URL_ENV:-}" ]; then
        export DATABASE_URL="${!URL_ENV}"
        echo "[implementer] resolved DATABASE_URL from cfg.database.url_env=$URL_ENV" >&2
    fi
fi

case "$IMPLEMENTER_LLM" in
    claude)
        # Claude Code CLI — feeds AGENT.md (or H2H.md) as the prompt
        if ! command -v claude > /dev/null 2>&1; then
            echo "ERROR: claude CLI not installed. Install Claude Code or set IMPLEMENTER_LLM." >&2
            exit 3
        fi
        # Pick the runbook based on dispatch kind
        case "$DISPATCH_KIND" in
            h2h)             RUNBOOK="$SCRIPT_DIR/H2H.md" ;;
            catalog-audit)   RUNBOOK="$SCRIPT_DIR/CATALOG_AUDIT.md" ;;
            article-author)  RUNBOOK="$SCRIPT_DIR/ARTICLE_AUTHOR.md" ;;
            *)               RUNBOOK="$SCRIPT_DIR/AGENT.md" ;;
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
        # FAIL_FAST tells claude-pool to exit rc=75 (EX_TEMPFAIL) when ALL
        # Max profiles are rate-limited, instead of sleeping for hours.
        # We catch that below + try the Copilot/aider fallback.
        export CLAUDE_POOL_FAIL_FAST="${CLAUDE_POOL_FAIL_FAST:-1}"
        # Test/validation knob: IMPLEMENTER_FORCE_FALLBACK=1 skips the
        # claude-pool path entirely and goes straight to the framework
        # code-editor chain. Used when validating the framework chain
        # end-to-end on a real rec without burning Max profile quota.
        # Default: 0 (use claude pool first; fall back automatically on
        # rc=75 = all Max profiles rate-limited).
        if [ "${IMPLEMENTER_FORCE_FALLBACK:-0}" = "1" ]; then
            echo "[implementer] IMPLEMENTER_FORCE_FALLBACK=1 — skipping claude, forcing framework code-editor chain" >&2
            rc=75
        else
            # 2026-05-11: Smart model-tier auto-switch. Anthropic enforces
            # PER-MODEL seven-day caps (seven_day_sonnet / seven_day_opus /
            # seven_day_haiku — separate quota pools). Start with Sonnet
            # (price/quality sweet spot), auto-downgrade to Opus if Sonnet
            # is family-capped, then Haiku as last claude resort, then fall
            # through to the framework chain (rc=75). The claude-pool shim
            # picks the right profile per family on each call. Override the
            # start tier via IMPLEMENTER_CLAUDE_MODEL.
            START_MODEL="${IMPLEMENTER_CLAUDE_MODEL:-claude-opus-4-7}"
            TIER_ORDER=("$START_MODEL")
            case "$START_MODEL" in
                *sonnet*) TIER_ORDER+=("claude-opus-4-7" "claude-haiku-4-5") ;;
                *opus*)   TIER_ORDER+=("claude-sonnet-4-6" "claude-haiku-4-5") ;;
                *haiku*)  TIER_ORDER+=("claude-opus-4-7" "claude-sonnet-4-6") ;;
            esac
            set +e
            rc=75
            # Per-tier wall-clock timeout. Overnight 2026-05-13: claude
            # processes hung indefinitely when the Max account hit opus
            # quota — claude-cli waits forever for an API response that
            # never comes (or comes back as "rate limited" and the cli
            # keeps retrying internally). Without an outer timeout,
            # stuck scopes consume throttle slots for 30+ min until
            # manually killed. `timeout 1500s` = 25 min. timeout exits
            # rc=124 on kill, which we treat as tier-fall (same as 75).
            CLAUDE_TIER_TIMEOUT_S="${CLAUDE_TIER_TIMEOUT_S:-1500}"
            for IMPL_MODEL in "${TIER_ORDER[@]}"; do
                echo "[implementer] trying $IMPL_MODEL (timeout=${CLAUDE_TIER_TIMEOUT_S}s)" >&2
                timeout --signal=TERM --kill-after=10s "$CLAUDE_TIER_TIMEOUT_S" \
                    claude --dangerously-skip-permissions \
                       --print --output-format text \
                       --model "$IMPL_MODEL" \
                       --max-turns "$IMPLEMENTER_MAX_TURNS" \
                       < "$PROMPT_FILE"
                rc=$?
                # rc=0 → success, stop.
                # rc=124 → hit the 25-min wall (claude hung). Tier-fall.
                # rc=75 → claude-pool exhausted (all Max profiles rate-
                #         limited for this model family). Tier-fall.
                # Other → bad prompt / transient. Tier-fall.
                [ "$rc" -eq 0 ] && break
                if [ "$rc" -eq 124 ]; then
                    echo "[implementer] $IMPL_MODEL TIMED OUT after ${CLAUDE_TIER_TIMEOUT_S}s — likely API hang; trying next tier" >&2
                else
                    echo "[implementer] $IMPL_MODEL rc=$rc; trying next tier" >&2
                fi
            done
            set -e
        fi

        # ── Pool-exhausted fallback path ─────────────────────────────────
        # rc=75 from claude-pool means "all Max profiles rate-limited".
        # Hand off to framework.cli.code_edit, which walks a configured
        # backend chain (aider via Copilot proxy / github_copilot native /
        # Azure → goose via Azure → plandex if installed) and applies
        # whichever wins. Chain is configurable via:
        #   - config/code-editor-config.json (per-deployment)
        #   - manifest.code_editor_chain (per-agent)
        #   - site.yaml `code_editor.chain` (per-site)
        # See framework/core/code_editor.py.
        if [ "$rc" -eq 75 ]; then
            echo "[implementer] claude-pool fail-fast: all Max profiles rate-limited" >&2

            # Per-rec build-out: produce a focused prompt + preload
            # file list. This is what makes the framework backends
            # actually do useful work (vs. handing them an abstract
            # runbook + 'figure out which recs to apply').
            CE_PROMPT=$(mktemp)
            CE_FILES=$(mktemp)
            CE_PRE_DIRTY=$(mktemp)
            CE_PRE_HASHES=$(mktemp)
            CE_RECS_JSON="$RESPONDER_RUN_DIR/recommendations.json"

            # Pre-snapshot working tree. We capture both the file LIST
            # (for set-diff) and per-file CONTENT HASHES (so files that
            # were already dirty pre-run but whose content the editor
            # legitimately advanced still get committed — fixes the
            # systemic "stranded work" bug where pre-dirty files were
            # blanket-excluded).
            if [ -d "${IMPLEMENTER_REPO_PATH:-}" ]; then
                pushd "$IMPLEMENTER_REPO_PATH" >/dev/null 2>&1 || true
                git status --porcelain 2>/dev/null \
                    | awk '{print $NF}' | sort -u > "$CE_PRE_DIRTY" || true
                # Hash each pre-dirty file's CURRENT content (working tree)
                # so we can detect editor-driven modifications later.
                while IFS= read -r f; do
                    [ -n "$f" ] && [ -f "$f" ] \
                        && printf "%s  %s\n" "$(sha1sum "$f" 2>/dev/null | awk '{print $1}')" "$f"
                done < "$CE_PRE_DIRTY" > "$CE_PRE_HASHES" 2>/dev/null || true
                popd >/dev/null 2>&1 || true
                CE_PRE_COUNT=$(wc -l < "$CE_PRE_DIRTY" 2>/dev/null || echo 0)
                echo "[implementer] pre-edit dirty file count: $CE_PRE_COUNT (will commit if content advances)" >&2
            fi

            # All dispatch kinds are eligible for the framework chain.
            # Aider/opencode/crush have `/run` shell, which can invoke
            # psql for DB writes when DATABASE_URL is in the env (the
            # implementer wrapper already exports DATABASE_URL_<SITE>
            # → DATABASE_URL above). h2h is the only kind that still
            # requires speculative reasoning a code-editor can't do
            # well, so it stays gated.
            CE_AVAILABLE=1
            case "$DISPATCH_KIND" in
                h2h)
                    echo "[implementer] h2h requires speculative product comparisons — skipping framework code-editor fallback (defer until claude-pool reset)" >&2
                    CE_AVAILABLE=0
                    ;;
            esac

            CE_DEFERRED_BY_ALLOWLIST=$(mktemp)
            if [ "$CE_AVAILABLE" = "1" ] && [ -f "$CE_RECS_JSON" ]; then
                # Build the per-rec focused prompt + preload file list.
                set +e
                ALLOW_TYPES_ARG=()
                if [ -n "${IMPLEMENTER_ALLOW_REC_TYPES:-}" ]; then
                    ALLOW_TYPES_ARG=(--allow-rec-types "$IMPLEMENTER_ALLOW_REC_TYPES")
                fi
                python3 "$SCRIPT_DIR/build-aider-invocation.py" \
                        --recs "$CE_RECS_JSON" \
                        --rec-ids "$RESPONDER_REC_IDS" \
                        --repo-path "$IMPLEMENTER_REPO_PATH" \
                        --site "${RESPONDER_SITE:-}" \
                        --site-config "${SEO_AGENT_CONFIG:-}" \
                        --dispatch-kind "$DISPATCH_KIND" \
                        --pre-dirty-file "$CE_PRE_DIRTY" \
                        --out-prompt "$CE_PROMPT" \
                        --out-files "$CE_FILES" \
                        --out-deferred "$CE_DEFERRED_BY_ALLOWLIST" \
                        "${ALLOW_TYPES_ARG[@]}" 2>&1
                BUILDER_RC=$?
                set -e
                if [ "$BUILDER_RC" -eq 3 ]; then
                    # All recs deferred by the trusted-rec-types allowlist —
                    # write a deferred.json with the per-rec reasons and exit
                    # cleanly. The dashboard surfaces them as "deferred-by-
                    # allowlist" rather than "failed".
                    echo "[implementer] all recs deferred by allowlist; framework chain skipped" >&2
                    if [ -d "$RESPONDER_RUN_DIR" ] && [ -s "$CE_DEFERRED_BY_ALLOWLIST" ]; then
                        cp "$CE_DEFERRED_BY_ALLOWLIST" "$RESPONDER_RUN_DIR/deferred-by-allowlist.json"
                        cat > "$RESPONDER_RUN_DIR/deferred.json" <<DEFERRED_EOF
{
  "deferred_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "reason": "all recs in this dispatch are not yet in TRUSTED_REC_TYPES allowlist (framework code-editor would hallucinate)",
  "rec_ids": "$RESPONDER_REC_IDS",
  "next_action": "wait for claude-pool reset, OR add the rec types to TRUSTED_REC_TYPES after one supervised validation run",
  "details_path": "deferred-by-allowlist.json"
}
DEFERRED_EOF
                    fi
                    rm -f "$PROMPT_FILE" "$CE_PROMPT" "$CE_FILES" "$CE_DEFERRED_BY_ALLOWLIST" "$CE_PRE_DIRTY" "$CE_PRE_HASHES"
                    exit 0
                elif [ "$BUILDER_RC" -ne 0 ]; then
                    echo "[implementer] build-aider-invocation failed rc=$BUILDER_RC — deferring" >&2
                    CE_AVAILABLE=0
                fi
            else
                CE_AVAILABLE=0
            fi

            if [ "$CE_AVAILABLE" = "1" ]; then
                # Hand off to framework code-editor CLI. It walks the
                # backend chain (aider / goose / plandex / ...) and
                # returns rc=0 on first success.
                set +e
                # 2026-05-12: lowered from 900s → 300s after diagnosing the
                # slow dispatches. jcode-ollama frequently hit the 900s ceiling
                # producing no files (rc=124), gating the next backend from
                # trying. With 5 backends in the chain × 900s = 75 min worst-
                # case. Most successful runs complete a single backend in
                # 20-60s; 300s is plenty of headroom for the slowest
                # legitimate path (claude-cli multi-step reasoning).
                CE_TIMEOUT="${IMPLEMENTER_CE_TIMEOUT:-300}"
                # Find reusable-agents repo for PYTHONPATH (run.sh sits
                # at agents/implementer/, so REPO_ROOT is two up).
                RA_FRAMEWORK_ROOT="${RA_REPO:-$REPO_ROOT}"
                # Two-phase chain (when multi-rec dispatch + ollama-first):
                #
                #   Phase 1 — per-rec ollama-only pass: ollama is slow
                #     (qwen3-coder-next at 30-60 tok/s on the 5090) and
                #     rarely converges on multi-rec batches inside its
                #     15-min cap. Run ollama PER REC instead — each rec
                #     has 1/N the work and finishes in 2-3 min if ollama
                #     can ship it.
                #   Phase 2 — classic multi-rec batch on remaining recs:
                #     copilot/claude handle multi-rec efficiently (one
                #     prompt, big context). Send all recs ollama didn't
                #     ship to copilot→claude as a single batch.
                #
                # Disable via IMPLEMENTER_PER_REC_SPLIT=0 (everything
                # runs as one classic chain with full ollama→copilot→claude).
                # Default OFF: try paid subscriptions (claude Max, copilot)
                # and Azure first because those are already-paid quota.
                # Ollama stays in the chain at the tail. To opt INTO the
                # per-rec ollama-first pass for specific dispatch kinds
                # (e.g. quality-tolerant work), set IMPLEMENTER_PER_REC_SPLIT=1.
                PER_REC_SPLIT="${IMPLEMENTER_PER_REC_SPLIT:-0}"
                REC_LIST=$(echo "$RESPONDER_REC_IDS" | tr ',' '\n' | sed '/^$/d')
                REC_COUNT=$(echo "$REC_LIST" | wc -l)
                CE_WINNERS_AGG=""
                SHIPPED_RECS=""
                REMAINING_RECS="$RESPONDER_REC_IDS"
                rc=1

                if [ "$PER_REC_SPLIT" = "1" ] && [ "$REC_COUNT" -gt 1 ]; then
                    # ── Phase 1: ollama-only per-rec pass ──────────────
                    echo "[implementer] phase-1: per-rec ollama pass on $REC_COUNT recs" >&2
                    PHASE1_OK=0
                    while IFS= read -r single_rec; do
                        [ -z "$single_rec" ] && continue
                        echo "[implementer] phase-1: rec=$single_rec" >&2
                        SUB_PROMPT=$(mktemp); SUB_FILES=$(mktemp); SUB_DEFERRED=$(mktemp)
                        python3 "$SCRIPT_DIR/build-aider-invocation.py" \
                                --recs "$CE_RECS_JSON" \
                                --rec-ids "$single_rec" \
                                --repo-path "$IMPLEMENTER_REPO_PATH" \
                                --site "${RESPONDER_SITE:-}" \
                                --site-config "${SEO_AGENT_CONFIG:-}" \
                                --dispatch-kind "$DISPATCH_KIND" \
                                --pre-dirty-file "$CE_PRE_DIRTY" \
                                --out-prompt "$SUB_PROMPT" \
                                --out-files "$SUB_FILES" \
                                --out-deferred "$SUB_DEFERRED" \
                                "${ALLOW_TYPES_ARG[@]}" >/dev/null 2>&1
                        SUB_BUILDER_RC=$?
                        if [ "$SUB_BUILDER_RC" -ne 0 ]; then
                            echo "[implementer] phase-1: build-aider rc=$SUB_BUILDER_RC for $single_rec — skipping (will retry in phase-2)" >&2
                            rm -f "$SUB_PROMPT" "$SUB_FILES" "$SUB_DEFERRED"
                            continue
                        fi
                        SUB_OUT=$(mktemp)
                        PYTHONPATH="$RA_FRAMEWORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
                            python3 -m framework.cli.code_edit \
                                --repo "$IMPLEMENTER_REPO_PATH" \
                                --prompt-file "$SUB_PROMPT" \
                                --files-file "$SUB_FILES" \
                                --pre-dirty-file "$CE_PRE_DIRTY" \
                                --agent-id "${RESPONDER_SOURCE_AGENT:-implementer}" \
                                --site "${RESPONDER_SITE:-}" \
                                --site-config "${SEO_AGENT_CONFIG:-}" \
                                --dispatch-kind "${DISPATCH_KIND:-}" \
                                --chain "jcode-ollama" \
                                --timeout "${IMPLEMENTER_OLLAMA_TIMEOUT:-600}" \
                                --json 2>&1 | tee "$SUB_OUT"
                        SUB_RC=${PIPESTATUS[0]}
                        if [ "$SUB_RC" -eq 0 ]; then
                            SHIPPED_RECS="$SHIPPED_RECS,$single_rec"
                            CE_WINNERS_AGG="$CE_WINNERS_AGG,jcode-ollama"
                            PHASE1_OK=$((PHASE1_OK + 1))
                            echo "[implementer] phase-1: rec=$single_rec → SHIPPED via jcode-ollama" >&2
                        else
                            echo "[implementer] phase-1: rec=$single_rec → ollama did not ship (rc=$SUB_RC) — deferring to phase-2" >&2
                        fi
                        rm -f "$SUB_PROMPT" "$SUB_FILES" "$SUB_DEFERRED" "$SUB_OUT"
                    done <<< "$REC_LIST"
                    echo "[implementer] phase-1: ollama shipped $PHASE1_OK/$REC_COUNT recs" >&2
                    # Compute remaining recs = original - shipped
                    REMAINING_RECS=$(python3 -c "
shipped = set('$SHIPPED_RECS'.strip(',').split(','))
shipped.discard('')
orig = '$RESPONDER_REC_IDS'.split(',')
remaining = [r for r in orig if r and r not in shipped]
print(','.join(remaining))
")
                    if [ "$PHASE1_OK" -gt 0 ]; then
                        rc=0  # at least one rec shipped — phase 1 partial success
                    fi
                fi

                # ── Phase 2: classic multi-rec batch on remaining recs
                #              through copilot→claude (no ollama) ──────
                # Skipped when per-rec-split off OR everything already shipped.
                if [ -n "$REMAINING_RECS" ]; then
                    if [ "$PER_REC_SPLIT" = "1" ] && [ "$REC_COUNT" -gt 1 ]; then
                        # Build a fresh prompt for just the remaining recs
                        REM_PROMPT=$(mktemp); REM_FILES=$(mktemp); REM_DEFERRED=$(mktemp)
                        python3 "$SCRIPT_DIR/build-aider-invocation.py" \
                                --recs "$CE_RECS_JSON" \
                                --rec-ids "$REMAINING_RECS" \
                                --repo-path "$IMPLEMENTER_REPO_PATH" \
                                --site "${RESPONDER_SITE:-}" \
                                --site-config "${SEO_AGENT_CONFIG:-}" \
                                --dispatch-kind "$DISPATCH_KIND" \
                                --pre-dirty-file "$CE_PRE_DIRTY" \
                                --out-prompt "$REM_PROMPT" \
                                --out-files "$REM_FILES" \
                                --out-deferred "$REM_DEFERRED" \
                                "${ALLOW_TYPES_ARG[@]}" >/dev/null 2>&1
                        REM_BUILDER_RC=$?
                        if [ "$REM_BUILDER_RC" -eq 0 ]; then
                            echo "[implementer] phase-2: classic multi-rec batch through jcode-copilot→claude-cli for $(echo $REMAINING_RECS | tr ',' '\n' | wc -l) remaining recs" >&2
                            CE_OUT=$(mktemp)
                            PYTHONPATH="$RA_FRAMEWORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
                                python3 -m framework.cli.code_edit \
                                    --repo "$IMPLEMENTER_REPO_PATH" \
                                    --prompt-file "$REM_PROMPT" \
                                    --files-file "$REM_FILES" \
                                    --pre-dirty-file "$CE_PRE_DIRTY" \
                                    --agent-id "${RESPONDER_SOURCE_AGENT:-implementer}" \
                                    --site "${RESPONDER_SITE:-}" \
                                    --site-config "${SEO_AGENT_CONFIG:-}" \
                                    --dispatch-kind "${DISPATCH_KIND:-}" \
                                    --chain "claude-cli,jcode-copilot,aider-azure,jcode-ollama" \
                                    --timeout "$CE_TIMEOUT" \
                                    --json 2>&1 | tee "$CE_OUT"
                            REM_RC=${PIPESTATUS[0]}
                            REM_WINNER=$(python3 -c "
import json
try:
    raw = open('$CE_OUT').read()
    start = raw.rfind('{\\n  \"winner\"')
    if start < 0: start = raw.find('{\\n  \"winner\"')
    if start >= 0:
        print(json.loads(raw[start:]).get('winner') or 'none')
except Exception: pass
" 2>/dev/null || echo "?")
                            if [ "$REM_RC" -eq 0 ]; then
                                CE_WINNERS_AGG="$CE_WINNERS_AGG,$REM_WINNER"
                                rc=0
                            else
                                [ "$rc" != "0" ] && rc="$REM_RC"
                            fi
                            rm -f "$REM_PROMPT" "$REM_FILES" "$REM_DEFERRED" "$CE_OUT"
                        else
                            echo "[implementer] phase-2: build-aider rc=$REM_BUILDER_RC — falling back to original prompt" >&2
                            rm -f "$REM_PROMPT" "$REM_FILES" "$REM_DEFERRED"
                        fi
                    else
                        # Classic single-batch path through full chain
                        # (preserved for IMPLEMENTER_PER_REC_SPLIT=0 + single-rec)
                        CE_OUT=$(mktemp)
                        PYTHONPATH="$RA_FRAMEWORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
                            python3 -m framework.cli.code_edit \
                                --repo "$IMPLEMENTER_REPO_PATH" \
                                --prompt-file "$CE_PROMPT" \
                                --files-file "$CE_FILES" \
                                --pre-dirty-file "$CE_PRE_DIRTY" \
                                --agent-id "${RESPONDER_SOURCE_AGENT:-implementer}" \
                                --site "${RESPONDER_SITE:-}" \
                                --site-config "${SEO_AGENT_CONFIG:-}" \
                                --dispatch-kind "${DISPATCH_KIND:-}" \
                                --timeout "$CE_TIMEOUT" \
                                --json 2>&1 | tee "$CE_OUT"
                        rc=${PIPESTATUS[0]}
                        CE_WINNER=$(python3 -c "
import json
try:
    raw = open('$CE_OUT').read()
    start = raw.rfind('{\\n  \"winner\"')
    if start < 0: start = raw.find('{\\n  \"winner\"')
    if start >= 0:
        print(json.loads(raw[start:]).get('winner') or 'none')
except Exception: pass
" 2>/dev/null || echo "?")
                        CE_WINNERS_AGG="$CE_WINNERS_AGG,$CE_WINNER"
                        rm -f "$CE_OUT"
                    fi
                fi
                set -e

                # Pick the dominant winner across all phases for the
                # commit-message tag (or "mixed" when both phases shipped).
                CE_WINNER=$(echo "$CE_WINNERS_AGG" | tr ',' '\n' | sed '/^$/d' | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
                [ -z "$CE_WINNER" ] && CE_WINNER="none"
                UNIQUE_WINNERS=$(echo "$CE_WINNERS_AGG" | tr ',' '\n' | sed '/^$/d' | sort -u | wc -l)
                [ "$UNIQUE_WINNERS" -gt 1 ] && CE_WINNER="${CE_WINNER}+others"

                echo "[implementer] code-editor winner=$CE_WINNER rc=$rc" >&2

                # Loud failure when every backend in the chain skipped
                # (preflight failed or auth unset). Previously we exited
                # silently rc=0 which produced "shipped 0 of N" runs that
                # looked successful in the dashboard. 2026-05-11: caught
                # via aider-azure-skip incident where AZURE_OPENAI_API_KEY
                # was unset and the dispatch consumed work without doing it.
                if [ "$CE_WINNER" = "none" ] && [ "$rc" -eq 0 ]; then
                    echo "[implementer] FATAL: no code-editor backend ran (every chain member skipped — check preflight env)" >&2
                    rc=78  # distinct from 75 (no-change) and 87 (SSR mismatch)
                fi
            else
                rc=75  # unchanged — falls into the deferred path below
            fi
            rm -f "$CE_PROMPT" "$CE_FILES"

            # ── Bulk-commit step (post/pre set diff) ─────────────────
            # Only commit files that became dirty during the editor's
            # run — never anything that was dirty beforehand.
            if [ "$rc" -eq 0 ] && [ -d "${IMPLEMENTER_REPO_PATH:-}" ] \
                    && [ -s "$CE_PRE_DIRTY" -o -f "$CE_PRE_DIRTY" ]; then
                pushd "$IMPLEMENTER_REPO_PATH" >/dev/null 2>&1 || true
                CE_POST_DIRTY=$(mktemp)
                CE_NEW_FILES=$(mktemp)
                git status --porcelain 2>/dev/null \
                    | awk '{print $NF}' | sort -u > "$CE_POST_DIRTY" || true
                # Newly-dirty files (not in pre-dirty set) — always commit.
                comm -23 "$CE_POST_DIRTY" "$CE_PRE_DIRTY" \
                    > "$CE_NEW_FILES" 2>/dev/null || true
                # Pre-dirty files whose CONTENT changed during the run —
                # these were previously dropped silently. Append to commit
                # set so legitimate edits to in-progress work are kept.
                # Compare current sha1 vs pre-run sha1 from CE_PRE_HASHES.
                if [ -s "$CE_PRE_HASHES" ]; then
                    CE_ADVANCED=$(mktemp)
                    while IFS=' ' read -r prev_hash _ pf; do
                        # awk emits "hash  path"; rebuild path (may have spaces).
                        [ -z "$prev_hash" ] && continue
                        # The file may have been deleted by the editor — skip if missing.
                        [ -f "$pf" ] || continue
                        cur_hash=$(sha1sum "$pf" 2>/dev/null | awk '{print $1}')
                        if [ -n "$cur_hash" ] && [ "$cur_hash" != "$prev_hash" ]; then
                            echo "$pf" >> "$CE_ADVANCED"
                        fi
                    done < <(awk '{h=$1; $1=""; sub(/^  /,""); print h" "$0}' "$CE_PRE_HASHES")
                    if [ -s "$CE_ADVANCED" ]; then
                        CE_ADVANCED_COUNT=$(wc -l < "$CE_ADVANCED" 2>/dev/null || echo 0)
                        echo "[implementer] pre-dirty files advanced by editor: $CE_ADVANCED_COUNT (including in commit)" >&2
                        cat "$CE_ADVANCED" >> "$CE_NEW_FILES"
                        sort -u -o "$CE_NEW_FILES" "$CE_NEW_FILES" 2>/dev/null || true
                    fi
                    rm -f "$CE_ADVANCED"
                fi
                CE_NEW_COUNT=$(wc -l < "$CE_NEW_FILES" 2>/dev/null || echo 0)

                # ── Orphan-scaffolding gate ──────────────────────────
                # Catches the partial-completion failure mode where the
                # LLM creates a new component / module / utility but
                # never imports it from any existing page. The gate
                # finds files that ONLY appear as added (no callers in
                # the rest of the repo) and treats the dispatch as a
                # soft-fail so the rec gets re-dispatched with the
                # corrective Rule 8 prompt instead of shipping dead
                # code. Set IMPLEMENTER_SKIP_ORPHAN_GATE=1 to disable
                # (e.g., for genuine library-only commits).
                CE_ORPHANS=""
                if [ "${IMPLEMENTER_SKIP_ORPHAN_GATE:-0}" != "1" ] \
                        && [ "$CE_NEW_COUNT" -gt 0 ]; then
                    while IFS= read -r f; do
                        # Only check NEWLY-CREATED source files (not edits
                        # to existing files). Limit to TS/TSX/JS/JSX/PY —
                        # other extensions don't have grep-able imports.
                        case "$f" in
                            *.tsx|*.ts|*.jsx|*.js|*.py) ;;
                            *) continue ;;
                        esac
                        # Was the file pre-existing? (a modification, not creation)
                        if git log --oneline -1 -- "$f" 2>/dev/null | grep -qv "^$"; then
                            # File existed before this commit — modification
                            # is fine, no orphan check needed.
                            continue
                        fi
                        # Derive the basename-without-extension as the
                        # symbol/module to grep for. Modern bundlers
                        # import either by name (e.g. JsonLdScripts) or
                        # by path. Check both.
                        bn=$(basename "$f")
                        sym="${bn%.*}"
                        path_no_ext="${f%.*}"
                        # Search the rest of the repo for any reference
                        # other than the file itself. Look for either
                        # the symbol name OR the path stem.
                        # excludes: the file itself, build artifacts, lock files
                        if ! grep -rEln \
                                --include='*.ts' --include='*.tsx' \
                                --include='*.js' --include='*.jsx' \
                                --include='*.py' \
                                --exclude-dir=node_modules \
                                --exclude-dir=.next \
                                --exclude-dir=dist \
                                --exclude-dir=build \
                                --exclude-dir=__pycache__ \
                                "(\b${sym}\b|['\"]${path_no_ext}['\"]|['\"]@/${path_no_ext}['\"])" \
                                . 2>/dev/null \
                                | grep -v "^\./${f}$" \
                                | head -1 | grep -q .; then
                            CE_ORPHANS="${CE_ORPHANS}${f}\n"
                        fi
                    done < "$CE_NEW_FILES"

                    if [ -n "$CE_ORPHANS" ]; then
                        echo "[implementer] ORPHAN SCAFFOLDING DETECTED — these new files have no callers:" >&2
                        printf "%b" "$CE_ORPHANS" | sed 's/^/  /' >&2
                        echo "[implementer] Per Rule 8, partial-completion is not acceptable. Discarding edits + deferring for re-dispatch with explicit integration instructions." >&2
                        # Reset the working tree so we don't commit dead code
                        git reset --hard HEAD 2>/dev/null || true
                        # Mark dispatch as soft-fail so chain falls over
                        rc=87  # custom orphan-scaffolding exit code
                        # Skip the rest of the commit block
                        CE_NEW_COUNT=0
                    fi
                fi

                # ── SSR-mismatch gate ────────────────────────────────
                # Catches the OTHER class of "shipped but invisible to
                # Google" failure: the rec affects what crawlers see
                # (JSON-LD, structured data, blocklists/filters that
                # gate ItemList items, meta tags) but the LLM only
                # edited React component files. Since the site has an
                # SSR layer, the change is invisible to bots even though
                # the symbols are imported + used in the React tree.
                #
                # Heuristic: if the repo has an SSR file AND any rec in
                # the batch has a SEO-flavored type AND the commit set
                # touched ZERO SSR files, defer with rc=87 so the chain
                # re-dispatches with the SSR-mirror prompt now active.
                # Set IMPLEMENTER_SKIP_SSR_GATE=1 to disable.
                if [ "${IMPLEMENTER_SKIP_SSR_GATE:-0}" != "1" ] \
                        && [ "$CE_NEW_COUNT" -gt 0 ]; then
                    SSR_FILE=""
                    for cand in \
                        "src/services/ssrHead.ts" \
                        "src/services/ssrRender.ts" \
                        "frontend/src/services/ssrHead.ts" \
                        "src/entry-server.tsx" \
                        "src/entry-server.ts"; do
                        [ -f "$cand" ] && SSR_FILE="$cand" && break
                    done
                    if [ -n "$SSR_FILE" ]; then
                        # Did the rec batch include any SEO/structured-data/
                        # filter type? Check the dispatch_kind + scan rec types
                        # in the recs JSON for the trigger keywords.
                        SEO_FLAVORED=0
                        case "${DISPATCH_KIND:-}" in
                            seo*|opportunity*|structured-data*|jsonld*) SEO_FLAVORED=1 ;;
                        esac
                        if [ "$SEO_FLAVORED" -eq 0 ] && [ -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
                            if grep -qiE '"type"\s*:\s*"(schema-|jsonld|json-ld|structured-data|meta-|canonical|breadcrumb|itemlist|filter|blocklist|quality-gate|category-pollution|ranking|featured-pick)' \
                                    "$RESPONDER_RUN_DIR/recommendations.json" 2>/dev/null; then
                                SEO_FLAVORED=1
                            fi
                        fi
                        if [ "$SEO_FLAVORED" -eq 1 ]; then
                            # Did the commit set touch any SSR file?
                            SSR_TOUCHED=0
                            while IFS= read -r f; do
                                case "$f" in
                                    src/services/ssrHead.ts|src/services/ssrRender.ts|frontend/src/services/ssrHead.ts|frontend/src/services/ssrRender.ts|src/entry-server.ts|src/entry-server.tsx)
                                        SSR_TOUCHED=1
                                        break
                                        ;;
                                esac
                            done < "$CE_NEW_FILES"
                            if [ "$SSR_TOUCHED" -eq 0 ]; then
                                echo "[implementer] SSR-MISMATCH DETECTED — SEO-flavored rec but commit touched 0 SSR files (expected one of $SSR_FILE etc)." >&2
                                echo "[implementer] Bots/crawlers see only the SSR'd HTML. A blocklist/JSON-LD/filter change in a React component is invisible to Google. Discarding edits + deferring for re-dispatch with SSR-mirror guidance." >&2
                                git reset --hard HEAD 2>/dev/null || true
                                rc=87
                                CE_NEW_COUNT=0
                            fi
                        fi
                    fi
                fi

                if [ "$CE_NEW_COUNT" -gt 0 ]; then
                    # ---- Post-LLM scope gate ----
                    # aider / copilot / claude-cli often roam outside the
                    # files we asked them to touch. If the site.yaml has
                    # an implementer.allowed_paths / excluded_paths
                    # policy, revert every newly-touched file that fails
                    # it BEFORE staging. This is the second checkpoint
                    # (the first is in build-aider-invocation.py, which
                    # filters recs whose `target_files` violate scope;
                    # this one catches drift inside an otherwise-allowed
                    # rec's edit, like an SEO rec that legitimately
                    # touched `frontend/foo.tsx` but the LLM also
                    # decided to refactor `mobile/...` on the way out).
                    if [ -n "${SEO_AGENT_CONFIG:-}" ] && [ -f "$SEO_AGENT_CONFIG" ]; then
                        SCOPE_REVERTED=$(
                            REPO_ROOT="$REPO_ROOT" \
                            PYTHONPATH="$REPO_ROOT" \
                            CHANGED_FILES_LIST="$CE_NEW_FILES" \
                            SITE_CONFIG_PATH="$SEO_AGENT_CONFIG" \
                            REPO_PATH="$IMPLEMENTER_REPO_PATH" \
                            DISPATCH_KIND="$DISPATCH_KIND" \
                            python3 - <<'PY'
import json, os, subprocess, sys, yaml
from pathlib import Path
sys.path.insert(0, os.environ['REPO_ROOT'])
from framework.core.implementer_scope import ScopePolicy

files = [ln.strip() for ln in Path(os.environ['CHANGED_FILES_LIST']).read_text().splitlines() if ln.strip()]
cfg = yaml.safe_load(Path(os.environ['SITE_CONFIG_PATH']).read_text()) or {}
policy = ScopePolicy.from_site_config(cfg, dispatch_kind=os.environ.get('DISPATCH_KIND') or None)
if not (policy.allowed_paths or policy.excluded_paths):
    sys.exit(0)
allowed, denied = policy.filter_files(files)
if not denied:
    sys.exit(0)
# Revert each denied file in the implementer repo (HEAD).
for f in denied:
    full = Path(os.environ['REPO_PATH']) / f
    try:
        subprocess.run(['git', '-C', os.environ['REPO_PATH'], 'checkout', '--', f],
                       check=False, capture_output=True)
        # If the file didn't exist at HEAD, `git checkout` is a no-op;
        # delete the new file directly so it doesn't get staged below.
        if full.exists() and full.is_file():
            r = subprocess.run(['git', '-C', os.environ['REPO_PATH'], 'cat-file', '-e',
                                f'HEAD:{f}'], capture_output=True)
            if r.returncode != 0:
                full.unlink()
    except Exception as e:
        print(f"[scope-revert] error on {f}: {e}", file=sys.stderr)
# Rewrite the changed-files list to drop denied entries.
Path(os.environ['CHANGED_FILES_LIST']).write_text('\n'.join(allowed) + '\n' if allowed else '')
print(json.dumps({'reverted': denied, 'kept': allowed}))
PY
                        )
                        if [ -n "$SCOPE_REVERTED" ]; then
                            echo "[implementer] scope-revert: $SCOPE_REVERTED" >&2
                            # Refresh CE_NEW_COUNT from the rewritten list.
                            CE_NEW_COUNT=$(grep -c . "$CE_NEW_FILES" 2>/dev/null || echo 0)
                            if [ "$CE_NEW_COUNT" -eq 0 ]; then
                                echo "[implementer] every touched file was out-of-scope after policy revert — nothing to commit" >&2
                                CE_NEW_COUNT=0
                            fi
                        fi
                    fi

                    # ── Article-scaffold gate (2026-05-12) ──────────────
                    # Pre-LLM hook writes a scaffold template into
                    # changes/<slug>.body.md before the LLM runs. The LLM
                    # is supposed to REPLACE that scaffold with real prose
                    # (1500–3000 words). When claude-cli fails or returns
                    # only a summary, the scaffold remains on disk but
                    # shows as `M` in git status (because it's different
                    # from the previously-committed body). Without this
                    # gate the implementer would happily commit the
                    # scaffold, the article-insert step would reject it
                    # (< 400 words floor), and the rec gets marked
                    # "shipped" while the DB has no article. Hit on
                    # aisleprompt 2026-05-12 — 5 stuck mediterranean /
                    # vegetarian meal-plan slugs.
                    #
                    # Detection: a body.md file is "still a scaffold" if
                    # it BOTH (a) contains the pre-LLM scaffold marker
                    # `<!-- art-` near the top AND (b) is under 400 words.
                    # The 400-word floor matches the article-insert step's
                    # rejection threshold, so the gate stays consistent.
                    if [ -n "$CE_NEW_COUNT" ] && [ "$CE_NEW_COUNT" -gt 0 ]; then
                        _scaffold_reverted=""
                        _scaffold_kept=""
                        while IFS= read -r f; do
                            [ -z "$f" ] && continue
                            case "$f" in
                                changes/*.body.md|*/changes/*.body.md) ;;
                                *) continue ;;
                            esac
                            full="${REPO_PATH}/$f"
                            [ -f "$full" ] || continue
                            # Cheap scaffold marker check: first 5 lines
                            # contain the pre-LLM HTML comment.
                            if ! head -5 "$full" 2>/dev/null | grep -q "<!-- art-"; then
                                continue
                            fi
                            _wc=$(wc -w < "$full" 2>/dev/null || echo 0)
                            if [ "$_wc" -lt 400 ]; then
                                # Scaffold not overwritten — revert + drop
                                # from CE_NEW_FILES so we don't commit it.
                                if git -C "$REPO_PATH" cat-file -e "HEAD:$f" 2>/dev/null; then
                                    git -C "$REPO_PATH" checkout -- "$f" 2>/dev/null || true
                                else
                                    rm -f "$full"
                                fi
                                _scaffold_reverted="${_scaffold_reverted}${f} (${_wc}w)\n"
                            fi
                        done < "$CE_NEW_FILES"
                        if [ -n "$_scaffold_reverted" ]; then
                            echo "[implementer] article-scaffold gate: REVERTED unreplaced scaffolds:" >&2
                            printf "%b" "$_scaffold_reverted" | sed 's/^/  /' >&2
                            # Rebuild CE_NEW_FILES list, dropping the reverted entries.
                            _tmp_new=$(mktemp)
                            while IFS= read -r f; do
                                [ -z "$f" ] && continue
                                _was_reverted=0
                                printf "%b" "$_scaffold_reverted" | while IFS= read -r line; do
                                    case "$line" in
                                        "${f} "*) exit 1 ;;
                                    esac
                                done || _was_reverted=1
                                [ "$_was_reverted" -eq 0 ] && echo "$f" >> "$_tmp_new"
                            done < "$CE_NEW_FILES"
                            mv "$_tmp_new" "$CE_NEW_FILES"
                            CE_NEW_COUNT=$(grep -c . "$CE_NEW_FILES" 2>/dev/null || echo 0)
                            if [ "$CE_NEW_COUNT" -eq 0 ]; then
                                echo "[implementer] every body.md was scaffold-only — nothing to commit, dispatch will soft-fail for retry" >&2
                                rc=88  # custom scaffold-unreplaced exit code
                            fi
                        fi
                    fi

                    set +e
                    if [ "$CE_NEW_COUNT" -gt 0 ]; then
                    while IFS= read -r f; do
                        [ -n "$f" ] && [ -e "$f" ] && git add "$f"
                    done < "$CE_NEW_FILES"
                    # Commit-shape linter (2026-05-11 retro): reject commits
                    # over IMPLEMENTER_MAX_COMMIT_LINES (default 800) — big-
                    # commit anti-pattern in the LLM backends, 20 commits >500
                    # lines in 17 days. Soft cap; operator can override.
                    _shape_cap="${IMPLEMENTER_MAX_COMMIT_LINES:-800}"
                    _shape_lines=$(git diff --cached --numstat | awk '{a+=$1; d+=$2} END {print (a+d)+0}')
                    if [ "$_shape_lines" -gt "$_shape_cap" ]; then
                        echo "[implementer] WARN commit-shape: ${_shape_lines} lines > IMPLEMENTER_MAX_COMMIT_LINES=${_shape_cap}. Letting it through, but this should be split — flagging for retro." >&2
                        # Record so the dashboard / retro agent can spot it.
                        echo "{\"ts\":\"$(date -Iseconds)\",\"agent\":\"implementer\",\"run\":\"${RESPONDER_RUN_TS}\",\"backend\":\"${CE_WINNER:-?}\",\"lines\":${_shape_lines},\"cap\":${_shape_cap}}" >> "$RESPONDER_RUN_DIR/_big_commits.jsonl" 2>/dev/null || true
                    fi
                    git commit -m "implementer (${CE_WINNER:-fallback}): apply ${RESPONDER_REC_IDS#,} for ${RESPONDER_AGENT_ID:-?}/${RESPONDER_RUN_TS:-?}

Recs: $RESPONDER_REC_IDS
Source: $SOURCE_AGENT_ID_FROM_RECS
Run: $RESPONDER_RUN_TS
Backend: ${CE_WINNER:-fallback}
Mode: framework-code-editor (claude-pool rate-limited)
Files staged: $CE_NEW_COUNT (set-diff of post-edit vs pre-edit)" 2>&1 | head -5
                    fi  # close: if CE_NEW_COUNT > 0 after scope-revert
                    set -e
                    SHA=$(git rev-parse --short HEAD 2>/dev/null || echo '?')
                    echo "[implementer] code-editor commit: SHA=$SHA, files=$CE_NEW_COUNT, backend=${CE_WINNER:-?}" >&2
                    # Record ship status — agent.py reads this for an
                    # accurate "shipped N of M" RunResult.summary.
                    SHIP_TOTAL=$(echo "$RESPONDER_REC_IDS" | tr ',' '\n' | grep -cv '^[[:space:]]*$' || echo 0)
                    cat > "$RESPONDER_RUN_DIR/_ship_status.json" <<SHIP_EOF
{"shipped": $SHIP_TOTAL, "deferred": 0, "reason": "code_edit_committed", "rc": 0, "sha": "$SHA", "files": $CE_NEW_COUNT, "backend": "${CE_WINNER:-?}"}
SHIP_EOF

                    # ---- Post-apply mobile-build hook ----
                    # If the commit touched mobile/ AND the policy says
                    # post_apply.kick_mobile_build is true (typically
                    # only under dispatch_kind=crash-fix), kick an EAS
                    # build via the site's scripts/build-and-submit.sh.
                    # Backgrounded so it doesn't block the implementer
                    # run completion. Output → /tmp/eas-build-<sha>.log.
                    if [ -n "${SEO_AGENT_CONFIG:-}" ] && [ -f "$SEO_AGENT_CONFIG" ] && [ "$CE_NEW_COUNT" -gt 0 ]; then
                        EAS_KICK_CMD=$(
                            REPO_ROOT="$REPO_ROOT" \
                            PYTHONPATH="$REPO_ROOT" \
                            CHANGED_FILES_LIST="$CE_NEW_FILES" \
                            SITE_CONFIG_PATH="$SEO_AGENT_CONFIG" \
                            REPO_PATH="$IMPLEMENTER_REPO_PATH" \
                            DISPATCH_KIND="$DISPATCH_KIND" \
                            python3 - <<'PY'
import os, sys, yaml
from pathlib import Path
sys.path.insert(0, os.environ['REPO_ROOT'])
from framework.core.implementer_scope import ScopePolicy

files = [ln.strip() for ln in Path(os.environ['CHANGED_FILES_LIST']).read_text().splitlines() if ln.strip()]
cfg = yaml.safe_load(Path(os.environ['SITE_CONFIG_PATH']).read_text()) or {}
policy = ScopePolicy.from_site_config(cfg, dispatch_kind=os.environ.get('DISPATCH_KIND') or None)

# Only fire when (a) the touched fileset includes a mobile/ path AND
# (b) the policy says kick_mobile_build for this dispatch_kind.
if not policy.kick_mobile_build:
    sys.exit(0)
if not policy.touched_mobile(files):
    sys.exit(0)
# Build candidate paths the site might ship a build script at — fall
# back through common conventions. Sites without a build script will
# silently no-op (the kick is opportunistic, not mandatory).
repo = Path(os.environ['REPO_PATH'])
for cand in ('mobile/scripts/build-and-submit.sh',
             'mobile/scripts/build-and-submit',
             'scripts/build-and-submit.sh'):
    p = repo / cand
    if p.is_file() and os.access(p, os.X_OK):
        print(f'cd {repo} && bash {cand} build')
        break
PY
                        )
                        if [ -n "$EAS_KICK_CMD" ]; then
                            EAS_LOG="/tmp/eas-build-${SHA:-unknown}.log"
                            echo "[implementer] kicking mobile build: $EAS_KICK_CMD (log: $EAS_LOG)" >&2
                            nohup bash -c "$EAS_KICK_CMD" >"$EAS_LOG" 2>&1 < /dev/null &
                            disown
                        else
                            echo "[implementer] mobile build hook eligible but no scripts/build-and-submit.sh found — skipping" >&2
                        fi
                    fi
                    # Mark each dispatched rec as implemented:true in
                    # the run-dir's recommendations.json so the dash-
                    # board sees it shipped (claude-mode does this via
                    # the AGENT.md runbook; framework-chain mode bypasses
                    # the LLM so we update from the wrapper). Sets
                    # `implemented_via=framework-<backend>` for traceability.
                    if [ -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
                        python3 - "$RESPONDER_RUN_DIR/recommendations.json" \
                                  "$RESPONDER_REC_IDS" \
                                  "${CE_WINNER:-framework}" \
                                  "$SHA" \
                                  "$RESPONDER_RUN_TS" <<'PY' 2>&1 | sed 's/^/[mark-implemented] /' >&2 || true
import json, sys
from datetime import datetime, timezone
p, ids_csv, backend, sha, run_ts = sys.argv[1:6]
ids = [s.strip() for s in ids_csv.split(',') if s.strip()]
d = json.load(open(p))
recs = d.get('recommendations', d) if isinstance(d, dict) else d
if isinstance(d, dict): recs = d['recommendations']
now = datetime.now(timezone.utc).isoformat(timespec='seconds')
hit = 0
for r in recs:
    if r.get('id') in ids:
        r['implemented'] = True
        r.setdefault('implemented_at', now)
        r.setdefault('implemented_run_ts', run_ts)
        r.setdefault('implemented_via', f'framework-{backend}')
        r.setdefault('implementation_commit_sha', sha)
        hit += 1
open(p, 'w').write(json.dumps(d, indent=2))
print(f'marked implemented={hit}/{len(ids)} via framework-{backend}')
PY
                    fi

                    # Ship-back: mark the producer's accumulator entries
                    # as implemented IMMEDIATELY so backlog-dispatcher
                    # doesn't re-queue them on its next tick. Without
                    # this, the producer's accumulator state stays
                    # `open` until the producer agent re-runs (which
                    # may be hours away), and the dispatcher keeps
                    # picking the same recs up.
                    if [ -n "${RESPONDER_AGENT_ID:-}" ] && [ -n "${RESPONDER_RUN_TS:-}" ]; then
                        PYTHONPATH="${RA_REPO_ROOT:-/home/voidsstr/development/reusable-agents}" \
                        python3 -m framework.cli.mark_shipped_in_accumulator \
                            --source-agent "$RESPONDER_AGENT_ID" \
                            --source-run-ts "$RESPONDER_RUN_TS" \
                            --rec-ids "$RESPONDER_REC_IDS" \
                            --implementation-sha "$SHA" 2>&1 | sed 's/^/[ship-back] /' >&2 || true
                    fi
                else
                    echo "[implementer] code-editor rc=0 but produced 0 NEW files — nothing to ship" >&2
                    # rc=0 with zero file changes = LLM concluded no edit needed
                    # (or hit an SSR/orphan gate). Recs stay unshipped.
                    SHIP_TOTAL=$(echo "$RESPONDER_REC_IDS" | tr ',' '\n' | grep -cv '^[[:space:]]*$' || echo 0)
                    cat > "$RESPONDER_RUN_DIR/_ship_status.json" <<SHIP_EOF
{"shipped": 0, "deferred": $SHIP_TOTAL, "reason": "no_op_or_gated", "rc": 0}
SHIP_EOF
                fi
                rm -f "$CE_POST_DIRTY" "$CE_NEW_FILES"
                popd >/dev/null 2>&1 || true
            fi
            rm -f "$CE_PRE_DIRTY" "$CE_PRE_HASHES"

            # ── Article-author write-then-insert post-step ────────────
            # The prompt builder told the LLM to write
            # `changes/<rec>.body.md` + `changes/<rec>.meta.json` for
            # any article-author rec instead of running psql itself.
            # Here we materialize those into actual editorial_articles
            # rows. This is the trust boundary: the wrapper VERIFIES
            # the row exists before reporting success — no more "claimed
            # success but never inserted" hallucinations.
            # Look in the REPO's changes/ dir (where build-aider-
            # invocation pre-stubs the body.md/meta.json files), NOT
            # the run_dir's changes/ — aider can only write to files
            # in its --file context, which are repo-relative.
            ART_CHANGES_DIR=""
            if [ -d "${IMPLEMENTER_REPO_PATH:-}/changes" ]; then
                ART_CHANGES_DIR="$IMPLEMENTER_REPO_PATH/changes"
            elif [ -d "${RESPONDER_RUN_DIR:-}/changes" ]; then
                ART_CHANGES_DIR="$RESPONDER_RUN_DIR/changes"
            fi
            if [ "$rc" -eq 0 ] && [ -n "$ART_CHANGES_DIR" ]; then
                CHANGES_DIR="$ART_CHANGES_DIR"
                ART_INSERT_LOG=$(mktemp)
                set +e
                PYTHONPATH="${RA_FRAMEWORK_ROOT:-/home/voidsstr/development/reusable-agents}${PYTHONPATH:+:$PYTHONPATH}" \
                    python3 - "$CHANGES_DIR" "$RESPONDER_RUN_DIR" \
                             "${RESPONDER_REC_IDS}" \
                             "${RESPONDER_SITE:-}" \
                             "${RESPONDER_SOURCE_AGENT:-?}" \
                             "${RESPONDER_RUN_TS:-?}" \
                             "${CE_WINNER:-?}" \
                             > "$ART_INSERT_LOG" 2>&1 <<'ART_PY'
import json, os, sys
from pathlib import Path
changes_dir = Path(sys.argv[1])
run_dir     = Path(sys.argv[2])
rec_ids     = [s for s in sys.argv[3].split(',') if s.strip()]
site        = sys.argv[4]
source_agent = sys.argv[5]
run_ts      = sys.argv[6]
backend     = sys.argv[7]


def _live_url(site_id: str, slug: str) -> str:
    """Compose the canonical article URL for this site.

    The framework historically hardcoded `/blog/{slug}` which works for
    AislePrompt but 404s on SpecPicks (no /blog route — articles render
    at /reviews/<slug>). Sites declare their canonical URL pattern via
    `articles.url_template` in site.yaml, e.g.:
        articles:
          url_template: "https://specpicks.com/reviews/{slug}"
    Falls back to the legacy `/blog/{slug}` shape if no template is set.
    """
    cfg_path = os.environ.get("SEO_AGENT_CONFIG", "")
    if cfg_path and Path(cfg_path).is_file():
        try:
            import yaml as _yaml
            cfg = _yaml.safe_load(open(cfg_path).read()) or {}
            tmpl = ((cfg.get("articles") or {}).get("url_template") or "").strip()
            if tmpl:
                return tmpl.replace("{slug}", slug).replace("{site}", site_id)
        except Exception:
            pass
    return f"https://{site_id}.com/blog/{slug}"

# Locate per-rec body/meta files. The build-aider-invocation step
# names them by SLUG (not rec_id) to prevent cross-run rec_id
# collisions from leaking one article's body under another's slug.
# We need to look up each rec's slug from recommendations.json BEFORE
# we can find the matching files. Legacy fallback: if a slug-named
# file isn't present but a rec_id-named one is, accept it (back-compat
# for in-flight rundirs from before the rename).
recs_doc = {}
recs_p = run_dir / "recommendations.json"
if recs_p.is_file():
    raw = json.loads(recs_p.read_text())
    if isinstance(raw, dict):
        recs_doc = {r.get("id"): r for r in raw.get("recommendations", [])}
    elif isinstance(raw, list):
        recs_doc = {r.get("id"): r for r in raw}

pairs = []
for rid in rec_ids:
    rec = recs_doc.get(rid) or {}
    proposal = rec.get("proposal") or rec.get("article_proposal") or {}
    slug = (proposal.get("slug") or "").strip()
    body_p = meta_p = None
    if slug:
        sb = changes_dir / f"{slug}.body.md"
        sm = changes_dir / f"{slug}.meta.json"
        if sb.is_file() and sm.is_file():
            body_p, meta_p = sb, sm
    if body_p is None:  # legacy fallback
        legacy_b = changes_dir / f"{rid}.body.md"
        legacy_m = changes_dir / f"{rid}.meta.json"
        if legacy_b.is_file() and legacy_m.is_file():
            body_p, meta_p = legacy_b, legacy_m
            print(f"[article-insert] {rid}: using legacy rec-id-named "
                  f"files (pre-slug-rename)")
    if body_p and meta_p:
        pairs.append((rid, body_p, meta_p))
if not pairs:
    print(f"[article-insert] no body.md/meta.json pairs found for "
          f"recs {rec_ids} — not an article dispatch, skipping")
    sys.exit(0)

dsn = os.environ.get("DATABASE_URL", "")
if not dsn:
    print(f"[article-insert] DATABASE_URL not set — cannot insert "
          f"{len(pairs)} article(s); leaving body files in place "
          f"for next retry", file=sys.stderr)
    sys.exit(2)

# recs_doc loaded above when resolving body/meta filenames; reuse it.

import psycopg2
conn = psycopg2.connect(dsn)
inserted_ids: list[tuple[str, int, str]] = []
errors: list[tuple[str, str]] = []
for rid, body_p, meta_p in pairs:
    try:
        body_md = body_p.read_text()
        # Anti-hallucination gate (lifted from leaked Claude Code prompt:
        # "if you can't verify, say so explicitly rather than claiming
        # success"). The LLM is instructed to write
        # 'EDIT INCOMPLETE: under target' as the last line if it can't
        # hit the word count; we honor that and skip the INSERT.
        if "EDIT INCOMPLETE" in body_md.splitlines()[-3:][-1] \
                if body_md.strip().splitlines() else False:
            errors.append((rid, "LLM marked body as EDIT INCOMPLETE — "
                                "skipping insert; will retry next tick"))
            continue
        word_count = len(body_md.split())
        # Hard floor — even if the LLM didn't mark INCOMPLETE, refuse
        # to insert articles below 400 words. Better to defer than ship
        # a stub that hurts SEO.
        if word_count < 400:
            errors.append((rid, f"body too short ({word_count} words "
                                "< 400 floor) — skipping insert"))
            continue
        # ── INTEGRITY CHECK: body H1 must match proposal title ──────
        # Defends against the cross-run rec_id collision class that
        # shipped 4 mismatched specpicks articles (wrong body content
        # under correct title). If the body's first H1 doesn't share
        # at least 30% of significant words with the proposal.title,
        # the LLM either wrote the wrong topic or never overwrote the
        # stub. SKIP the INSERT — the file stays in changes/ for a
        # human to review.
        import re as _re
        rec = recs_doc.get(rid) or {}
        proposal = rec.get("proposal") or rec.get("article_proposal") or {}
        proposal_title = (proposal.get("title") or "").strip()
        h1_match = _re.search(r"^#\s+(.+)$", body_md, _re.MULTILINE)
        body_h1 = h1_match.group(1).strip() if h1_match else ""
        def _kw(s):
            return {w for w in _re.sub(r"[^a-z0-9 ]", " ",
                                        (s or "").lower()).split()
                    if len(w) > 3}
        title_kw = _kw(proposal_title)
        h1_kw = _kw(body_h1)
        head_kw = _kw(body_md[:1500])
        # The body must reflect the proposed topic SOMEWHERE in the
        # H1 + first 1500 chars (covers cases where the LLM uses a
        # paraphrased H1 but the body content is on-topic).
        ref_kw = h1_kw | head_kw
        overlap = title_kw & ref_kw
        match_pct = len(overlap) / max(len(title_kw), 1)
        if title_kw and match_pct < 0.3:
            errors.append((rid,
                f"INTEGRITY: body content does not match proposed title "
                f"(overlap={match_pct:.0%}); proposed={proposal_title!r} "
                f"body H1={body_h1[:60]!r} — refusing INSERT to prevent "
                f"shipping wrong content under correct slug"))
            print(f"[article-insert] {rid}: ✗ INTEGRITY mismatch "
                  f"title='{proposal_title[:50]}' h1='{body_h1[:50]}' "
                  f"overlap={match_pct:.0%} — SKIP", file=sys.stderr)
            continue
        # Tolerate malformed JSON — only subtitle/excerpt come from the
        # LLM-written file. Everything else is wrapper-injected from
        # the proposal in recommendations.json so the LLM can't break
        # critical fields like slug/title/category.
        try:
            user_meta = json.loads(meta_p.read_text())
            if not isinstance(user_meta, dict):
                user_meta = {}
        except json.JSONDecodeError as je:
            print(f"[article-insert] {rid}: meta.json invalid JSON "
                  f"({je}) — falling back to proposal-only meta")
            user_meta = {}
        rec = recs_doc.get(rid) or {}
        proposal = rec.get("proposal") or rec.get("article_proposal") or {}
        if not proposal.get("slug"):
            errors.append((rid, "no proposal.slug — wrong dispatch type?"))
            continue
        meta = {
            "slug":  proposal["slug"],
            "title": proposal.get("title") or proposal["slug"],
            "subtitle": user_meta.get("subtitle") or "",
            "excerpt":  user_meta.get("excerpt") or "",
            "category": proposal.get("bucket") or proposal.get("category") or "",
            "tags":     proposal.get("tags") or [],
            "primary_keyword": proposal.get("primary_keyword") or "",
            "secondary_keywords": proposal.get("secondary_keywords") or [],
            "related_hardware_slugs": proposal.get("related_hardware_slugs") or [],
            "related_product_asins": proposal.get("related_product_asins") or [],
        }
        slug = meta["slug"]
        title = meta["title"]
        # Schema-aware INSERT: introspect the table once, build the
        # column list dynamically, only include columns that exist.
        # Lets the same wrapper handle aisleprompt's editorial_articles
        # (has related_recipe_slugs, diet_tags, hero_image_url) and
        # specpicks's editorial_articles (has related_hardware_slugs,
        # related_product_asins) without per-site forks.
        with conn.cursor() as cur:
            table = meta.get("table") or "editorial_articles"
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (table,))
            existing_cols = {row[0] for row in cur.fetchall()}
            if not existing_cols:
                errors.append((rid, f"table {table!r} not found"))
                continue
            # Map of (column_name → value) to INSERT. Only kept if
            # column actually exists in the live schema.
            candidate_values = {
                "slug": slug,
                "title": title,
                "subtitle": meta.get("subtitle", ""),
                "excerpt": meta.get("excerpt", ""),
                "body_md": body_md,
                "category": meta.get("category", ""),
                "bucket":   meta.get("category", ""),  # aliased
                "tags":     meta.get("tags") or [],
                "primary_keyword": meta.get("primary_keyword", ""),
                "secondary_keywords": meta.get("secondary_keywords") or [],
                "related_recipe_slugs": (
                    (rec.get("proposal") or {}).get("expected_recipe_slugs")
                    or []),
                "related_kitchen_slugs": (
                    (rec.get("proposal") or {}).get("expected_kitchen_slugs")
                    or []),
                "related_hardware_slugs": meta.get("related_hardware_slugs") or [],
                "related_product_asins":  meta.get("related_product_asins") or [],
                "author": "Mike Perry",
                "status": "published",
                "written_by": f"framework-{backend}",
            }
            insert_cols = [c for c in candidate_values
                           if c in existing_cols]
            insert_vals = [candidate_values[c] for c in insert_cols]
            # Add bookkeeping timestamps if those columns exist.
            ts_cols = [c for c in ("published_at", "written_at",
                                   "created_at", "updated_at")
                       if c in existing_cols]
            update_cols = [c for c in
                           ("title", "subtitle", "excerpt", "body_md",
                            "category", "bucket", "tags", "status")
                           if c in insert_cols]
            placeholders = ", ".join(["%s"] * len(insert_vals)
                                     + ["now()"] * len(ts_cols))
            update_clause = ", ".join(
                [f"{c} = EXCLUDED.{c}" for c in update_cols] +
                [f"{c} = now()" for c in ("updated_at", "written_at")
                 if c in existing_cols])
            sql = (
                f"INSERT INTO {table} "
                f"({', '.join(insert_cols + ts_cols)}) "
                f"VALUES ({placeholders}) "
                f"ON CONFLICT (slug) DO UPDATE SET {update_clause} "
                f"RETURNING id")
            cur.execute(sql, insert_vals)
            row_id = cur.fetchone()[0]
            conn.commit()
            inserted_ids.append((rid, row_id, slug))
            print(f"[article-insert] {rid}: INSERTed {table} "
                  f"id={row_id} slug={slug} body_len={len(body_md)} "
                  f"cols={len(insert_cols)}")
    except Exception as e:
        errors.append((rid, str(e)[:300]))
        try: conn.rollback()
        except: pass
        print(f"[article-insert] {rid}: ERROR {e}", file=sys.stderr)

# Summary doc per rec — overwrites whatever the LLM might have written
# (which was prone to hallucinated "INSERTed id=42" claims).
for rid, row_id, slug in inserted_ids:
    s = changes_dir / f"{rid}.summary.md"
    s.write_text(f"""# {rid} Summary

## Status: SHIPPED (verified by wrapper)

## INSERT Result
- Table: editorial_articles
- Row id: {row_id}
- Slug: {slug}
- Backend that wrote body_md: {backend}

## Live URL
{LIVE_URL}
""".replace("{LIVE_URL}", _live_url(site, slug)))

print(f"[article-insert] applied={len(inserted_ids)} errors={len(errors)}")
if errors:
    sys.exit(3)
ART_PY
                INSERT_RC=$?
                set -e
                cat "$ART_INSERT_LOG" >&2
                rm -f "$ART_INSERT_LOG"
                if [ "$INSERT_RC" -ne 0 ] && [ "$INSERT_RC" -ne 0 ]; then
                    echo "[implementer] article-insert post-step rc=$INSERT_RC — body files preserved at $CHANGES_DIR/" >&2
                fi

                # Article link resolver: fix invented `/recipes/<slug>`
                # links in any newly-INSERTed editorial_articles row by
                # fuzzy-matching to recipe_catalog (real slugs include
                # a trailing `-<id>`). Runs after every article-author
                # dispatch so future articles ship with valid inner
                # links. No-ops if there were no matches.
                if [ -n "${DATABASE_URL:-}" ] && \
                        [ -x "$SCRIPT_DIR/resolve-article-links.py" ]; then
                    DATABASE_URL="$DATABASE_URL" \
                        python3 "$SCRIPT_DIR/resolve-article-links.py" --apply \
                        2>&1 | sed 's/^/[link-resolver] /' >&2 || true
                fi

                # Shipped-flag reconciler: walks every article-author
                # rundir's recommendations.json and flips shipped:true
                # for any rec whose proposal.slug now exists in
                # editorial_articles. Closes the gap where the INSERT
                # step succeeds but the JSON file (which the dashboard
                # + future implementer runs read as authority) never
                # learns about it.
                if [ -x "$SCRIPT_DIR/reconcile-shipped.py" ]; then
                    DATABASE_URL_AISLEPROMPT="${DATABASE_URL_AISLEPROMPT:-}" \
                        DATABASE_URL_SPECPICKS="${DATABASE_URL_SPECPICKS:-}" \
                        python3 "$SCRIPT_DIR/reconcile-shipped.py" \
                        2>&1 | sed 's/^/[reconcile-shipped] /' >&2 || true
                fi
            fi

            # If the framework chain failed too, defer cleanly.
            if [ "$rc" -ne 0 ]; then
                echo "[implementer] graceful defer: pool exhausted + no working code-editor backend (rc=$rc)" >&2
                if [ -d "$RESPONDER_RUN_DIR" ]; then
                    cat > "$RESPONDER_RUN_DIR/deferred.json" <<DEFERRED_EOF
{
  "deferred_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "reason": "claude-pool exhausted (all Max profiles rate-limited) and no framework code-editor backend succeeded",
  "rec_ids": "$RESPONDER_REC_IDS",
  "soonest_pool_reset": "see /api/agents/responder-agent/status.json or claude-pool state.json",
  "next_action": "host-worker will NOT auto-retry; next cron tick of the source agent re-emits the recs if still relevant"
}
DEFERRED_EOF
                    # Mark ship-status so agent.py reports "deferred N"
                    # rather than the misleading "implemented N".
                    DEFERRED_COUNT=$(echo "$RESPONDER_REC_IDS" | tr ',' '\n' | grep -cv '^[[:space:]]*$' || echo 0)
                    cat > "$RESPONDER_RUN_DIR/_ship_status.json" <<SHIP_EOF
{"shipped": 0, "deferred": $DEFERRED_COUNT, "reason": "graceful_defer", "rc": $rc}
SHIP_EOF
                fi
                rm -f "$PROMPT_FILE"
                # Exit 0 so host-worker doesn't infinite-retry. The
                # source agent's next cron tick re-emits any unshipped recs.
                exit 0
            fi
        elif [ "$rc" -ne 0 ]; then
            echo "[implementer] claude exited rc=$rc" >&2
            rm -f "$PROMPT_FILE"
            exit $rc
        fi
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

# ── Deploy gating ───────────────────────────────────────────────────────────
# Per-batch deployment: every successful implementer batch chains
# straight to the seo-deployer (build + tests + Azure push). Smaller
# blast radius per ship, faster feedback if a batch breaks tests, and
# avoids the "uncommitted work locked behind a never-completing chain"
# failure mode of per-chain deploys.
#
# Skipped automatically for DB-only dispatches (article-author, h2h,
# catalog-audit) since those don't need a docker build.
#
# Path note: deployer lives at agents/deployer/run.sh. The legacy
# seo-deployer name + path is kept as a fallback so older clones
# still work; both checked in priority order.
DEPLOYER_SCRIPT="$REPO_ROOT/agents/deployer/run.sh"
if [ ! -x "$DEPLOYER_SCRIPT" ] && [ -x "$REPO_ROOT/agents/seo-deployer/run.sh" ]; then
    DEPLOYER_SCRIPT="$REPO_ROOT/agents/seo-deployer/run.sh"
fi
if [ ! -x "$DEPLOYER_SCRIPT" ] && [ -x "$REPO_ROOT/seo-deployer/run.sh" ]; then
    DEPLOYER_SCRIPT="$REPO_ROOT/seo-deployer/run.sh"
fi

_is_last_batch_in_chain() {
    local mf="$RESPONDER_RUN_DIR/dispatch-batches.json"
    [ -f "$mf" ] || return 0  # no manifest → not batched → "last" by definition
    PYTHONPATH="$REPO_ROOT" \
    RESPONDER_RUN_DIR="$RESPONDER_RUN_DIR" \
    RESPONDER_BATCH_INDEX="${RESPONDER_BATCH_INDEX:-0}" \
    python3 - <<'PY' 2>/dev/null
import json, os, sys
from pathlib import Path
mf = Path(os.environ["RESPONDER_RUN_DIR"]) / "dispatch-batches.json"
m = json.loads(mf.read_text())
cur_idx = int(os.environ.get("RESPONDER_BATCH_INDEX", "0") or "0")
batches = m.get("batches", [])
# Mark current as completed in-memory so the "any pending/running" check
# correctly considers it done (the chain logic later writes the manifest).
for b in batches:
    if b.get("index") == cur_idx and b.get("status") in ("pending", "running"):
        b["status"] = "completed"
        break
# Any other batch still incomplete?
for b in batches:
    if b.get("status") in ("pending", "running"):
        sys.exit(1)
sys.exit(0)
PY
}

if [ "$DISPATCH_KIND" = "h2h" ]; then
    echo "[implementer] H2H dispatch — skipping deployer chain"
elif [ "$DISPATCH_KIND" = "article-author" ]; then
    echo "[implementer] article-author dispatch — skipping deployer chain (articles ship via DB insert, no build)"
elif [ "$DISPATCH_KIND" = "catalog-audit" ]; then
    echo "[implementer] catalog-audit dispatch — skipping deployer chain (DB-only fixes, no build)"
elif [ -x "$DEPLOYER_SCRIPT" ] && [ "${IMPLEMENTER_SKIP_DEPLOY:-0}" != "1" ]; then
    # Per-batch deploy — fire after every batch, not waiting for chain
    # end. The _is_last_batch_in_chain helper is preserved (still used
    # by the legacy per-chain gate) but no longer required here.
    #
    # No-commit guard: aider sometimes returns success with zero edits
    # (rec already deferred, already implemented in a prior pass, or
    # determined to be a no-op). Running the full test→build→push→
    # deploy→smoke pipeline against an unchanged HEAD wastes ~15-20 min
    # of a throttle slot for zero value. Compare current HEAD against
    # GIT_SHA_BEFORE captured at run start; skip the deployer if they
    # match. We still drop into the artifact-detection block below so
    # the run is correctly marked status=paused (no ship).
    GIT_SHA_NOW=""
    if [ -n "${IMPLEMENTER_REPO_PATH:-}" ] && [ -d "$IMPLEMENTER_REPO_PATH" ] \
            && git -C "$IMPLEMENTER_REPO_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        GIT_SHA_NOW=$(git -C "$IMPLEMENTER_REPO_PATH" log -1 --format='%H' 2>/dev/null || echo "")
    fi
    if [ -n "$GIT_SHA_NOW" ] && [ "$GIT_SHA_NOW" = "${GIT_SHA_BEFORE:-}" ]; then
        echo "[implementer] no commits this batch (HEAD unchanged ${GIT_SHA_NOW:0:8}) — skipping deployer"
        # CRITICAL: claude no-op'd (rec was already-deferred / no actionable
        # change). If we don't mark the source rec as deferred in
        # recommendations.json, the backlog-dispatcher will re-dispatch
        # the SAME rec on every subsequent tick, burning claude pool
        # quota indefinitely. Overnight 2026-05-13: rec-001 "celery25678"
        # re-dispatched ~50× over 2h producing zero commits. This block
        # closes the loop by writing `deferred: true` + reason to each
        # rec in this batch.
        if [ -n "${RESPONDER_RUN_TS:-}" ] && [ -n "${RESPONDER_SOURCE_AGENT:-}" ] \
                && [ -n "${RESPONDER_REC_IDS:-}" ]; then
            PYTHONPATH=/home/voidsstr/development/reusable-agents python3 - <<MARK_DEFERRED
import os, json
from datetime import datetime, timezone
from framework.core.storage import get_storage
s = get_storage()
key = f"agents/{os.environ['RESPONDER_SOURCE_AGENT']}/runs/{os.environ['RESPONDER_RUN_TS']}/recommendations.json"
try:
    doc = s.read_json(key) or {}
except Exception as e:
    print(f"[no-commit-mark] read failed: {e}")
    raise SystemExit(0)
recs = doc.get("recommendations") or []
target_ids = set(os.environ["RESPONDER_REC_IDS"].split(","))
updated = 0
ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
for r in recs:
    rid = str(r.get("id") or r.get("rec_id") or "")
    if rid in target_ids and not (r.get("shipped") or r.get("implemented") or r.get("deferred")):
        r["deferred"] = True
        r["deferred_at"] = ts
        r["deferred_reason"] = (
            "implementer no-op: HEAD unchanged after claude pass — "
            "rec is either already-deferred from a prior run or has no "
            "actionable change. Marked deferred to prevent re-dispatch."
        )
        updated += 1
if updated:
    s.write_json(key, doc)
    print(f"[no-commit-mark] marked {updated} rec(s) as deferred in {key}")
MARK_DEFERRED
        fi
    else
        echo "[implementer] batch complete — chaining to deployer (per-batch deploy, smoke tests only)"
        # Per-batch deploys ALWAYS run smoke tests, never the full suite.
        # The full suite (when present in site.yaml under test.full) is
        # long-running (15-20 min playwright runs) and holds a throttle
        # slot hostage. If a periodic full-suite cadence is needed, run
        # it as a separate scheduled task with DEPLOYER_TEST_SCOPE=full.
        SEO_AGENT_CONFIG="$SEO_AGENT_CONFIG" \
        DEPLOYER_TEST_SCOPE="${DEPLOYER_TEST_SCOPE:-smoke}" \
            bash "$DEPLOYER_SCRIPT" --run-dir "$RESPONDER_RUN_DIR" || {
                rc=$?
                echo "[implementer] deployer failed rc=$rc" >&2
                exit $rc
            }
    fi
fi


# ── Did claude actually apply anything? ─────────────────────────────────────
# Truth-source for "this run shipped work" is conservative: we MUST have a
# verifiable artifact. Otherwise the email lies and the user can't tell
# apart a real ship from claude bailing out.
#
# Three valid signals (any of them = real work):
#   1. NEW git commit on the implementer repo (SHA differs from pre-run)
#      → SEO code-editing path
#   2. <run_dir>/applied-recs.json with non-empty rec_ids list, written by
#      claude when it patches DB rows
#      → catalog-audit / h2h / article-author paths
#   3. <run_dir>/changes/<rec>.* artifacts that match the input rec_ids
#      → fallback for SEO recs that produced files but no commit yet
#         (uncommon; usually means deployer hasn't run)
#
# If NONE of these, status=paused and the email subject says so.

# Signal 1: git commit. Use `git rev-parse --is-inside-work-tree` so we
# correctly handle subdirs whose .git lives in an ancestor (e.g. the
# customer-app subdir of nsc-assistant). The earlier `[ -d "$IMPL_REPO/.git" ]`
# check missed this and produced false-paused status.
GIT_SHA=""
IMPL_REPO="${IMPLEMENTER_REPO_PATH:-}"
if [ -n "$IMPL_REPO" ] && [ -d "$IMPL_REPO" ] \
        && git -C "$IMPL_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_SHA_AFTER=$(git -C "$IMPL_REPO" log -1 --format='%H' 2>/dev/null || echo "")
    if [ -n "$GIT_SHA_AFTER" ] && [ "$GIT_SHA_AFTER" != "${GIT_SHA_BEFORE:-}" ]; then
        GIT_SHA="$GIT_SHA_AFTER"
    fi
fi

# Signal 2: applied-recs.json (DB-write convention)
APPLIED_RECS_JSON="$RESPONDER_RUN_DIR/applied-recs.json"
APPLIED_REC_IDS=""
if [ -f "$APPLIED_RECS_JSON" ]; then
    APPLIED_REC_IDS=$(python3 -c "
import json, sys
try:
    d = json.load(open('$APPLIED_RECS_JSON'))
    ids = d.get('applied_rec_ids') or d.get('rec_ids') or []
    if isinstance(ids, list):
        print(','.join(str(x) for x in ids if x))
except Exception:
    pass
" 2>/dev/null || true)
fi

# Signal 2b: recommendations.json scan — count recs marked as
# already-implemented in this run (implemented: true + implemented_run_ts ==
# THIS run). These don't need a commit because there was nothing to change,
# but they ARE a successful outcome (claude verified the rec is already
# satisfied in code).
ALREADY_IMPLEMENTED_REC_IDS=""
RECS_JSON="$RESPONDER_RUN_DIR/recommendations.json"
if [ -f "$RECS_JSON" ]; then
    ALREADY_IMPLEMENTED_REC_IDS=$(python3 -c "
import json, os
try:
    d = json.load(open('$RECS_JSON'))
    this_run = '${RESPONDER_RUN_TS:-}'
    requested = set(s.strip() for s in '${RESPONDER_REC_IDS:-}'.split(',') if s.strip())
    out = []
    for r in d.get('recommendations', []):
        rid = r.get('id')
        if rid in requested and r.get('implemented') is True \
                and (r.get('implemented_run_ts') == this_run
                     or r.get('implemented_via') == 'pre-existing'):
            out.append(rid)
    print(','.join(out))
except Exception:
    pass
" 2>/dev/null || true)
fi

# Signal 3 was previously "changes/ has files" — REMOVED, because claude
# routinely writes `<rec>.summary.md` files to document DEFERRALS and SKIPS
# (e.g. "DEFERRED: target query is 'celery25678' — malformed GSC data, no
# code change made"). Counting those as "applied" produces false "Shipped"
# emails. Real applies must show up as a git commit OR an explicit
# applied-recs.json entry from the agent.

# Decide status. Default = paused (safer to under-claim than over-claim).
# A run is "completed" if any of the following:
#   - a NEW git commit was made (real code change)
#   - applied-recs.json names rec_ids (DB-write evidence)
#   - recommendations.json marks recs as already-implemented in THIS run
#     (claude verified the change already exists in code — valid success)
COMPLETION_STATUS="paused"
COMPLETION_REASON=""
if [ -n "$GIT_SHA" ]; then
    COMPLETION_STATUS="completed"
    COMPLETION_REASON="commit ${GIT_SHA:0:8}"
elif [ -n "$APPLIED_REC_IDS" ]; then
    COMPLETION_STATUS="completed"
    COMPLETION_REASON="applied-recs.json: ${APPLIED_REC_IDS//,/, }"
elif [ -n "$ALREADY_IMPLEMENTED_REC_IDS" ]; then
    COMPLETION_STATUS="completed"
    COMPLETION_REASON="already-implemented: ${ALREADY_IMPLEMENTED_REC_IDS//,/, }"
fi

# 2026-05-13: stamp `shipped=True` + `shipped_at` on the SOURCE producer's
# recommendations.json when the implementer succeeds via applied-recs.json
# (article-author DB-write convention) OR when claude marked recs as
# already-implemented. Without this, the source producer's
# recommendations.json keeps showing those recs as "open" forever even
# after the article is LIVE on the site — the dashboard's pending count
# stays inflated and the dispatcher relies entirely on the title-cache to
# avoid re-dispatch (fragile: cache resets reintroduce loops).
#
# Symptom that prompted this fix: 6 sampled "pending" articles all
# returned HTTP 200 on their public URLs but stayed marked open in their
# producer's recommendations.json. Reality is shipping, producer state
# was lying.
if [ "$COMPLETION_STATUS" = "completed" ] && [ -f "$RECS_JSON" ]; then
    MARK_IDS=""
    if [ -n "$APPLIED_REC_IDS" ]; then
        MARK_IDS="$APPLIED_REC_IDS"
    elif [ -n "$ALREADY_IMPLEMENTED_REC_IDS" ]; then
        MARK_IDS="$ALREADY_IMPLEMENTED_REC_IDS"
    fi
    if [ -n "$MARK_IDS" ]; then
        python3 - "$RECS_JSON" "$MARK_IDS" "$RESPONDER_RUN_TS" "$COMPLETION_REASON" <<'PY' 2>&1 | sed 's/^/[mark-shipped] /' >&2 || true
import json, sys
from datetime import datetime, timezone
path, ids_csv, run_ts, reason = sys.argv[1:5]
ids = set(s.strip() for s in ids_csv.split(',') if s.strip())
doc = json.load(open(path))
recs = doc.get('recommendations') if isinstance(doc, dict) else doc
if not recs: sys.exit(0)
now = datetime.now(timezone.utc).isoformat(timespec='seconds')
marked = 0
for r in recs:
    if not isinstance(r, dict): continue
    rid = r.get('id') or r.get('rec_id')
    if rid in ids and not r.get('shipped'):
        r['shipped'] = True
        r['implemented'] = True
        r.setdefault('shipped_at', now)
        r.setdefault('shipped_via', reason or 'implementer')
        r.setdefault('shipped_run_ts', run_ts)
        marked += 1
if marked:
    open(path, 'w').write(json.dumps(doc, indent=2))
print(f"marked shipped={marked}/{len(ids)} in source recommendations.json")
PY
    fi
fi
# Useful diagnostic: even when paused, surface that claude DID write
# deferral-summary files so the user knows the LLM at least ran.
CHANGES_COUNT=0
if [ -d "$RESPONDER_RUN_DIR/changes" ]; then
    CHANGES_COUNT=$(find "$RESPONDER_RUN_DIR/changes" -type f 2>/dev/null | wc -l)
fi
if [ "$COMPLETION_STATUS" = "paused" ] && [ "$CHANGES_COUNT" != "0" ]; then
    COMPLETION_REASON="claude wrote $CHANGES_COUNT artifact(s) to changes/ but did NOT commit (likely deferral notes)"
elif [ "$COMPLETION_STATUS" = "paused" ]; then
    COMPLETION_REASON="no commit + no applied-recs.json + empty changes/ — claude exited without acting"
fi
echo "[implementer] status=$COMPLETION_STATUS reason=$COMPLETION_REASON" >&2

# Email rec_ids: when paused, list the INPUT recs so the user knows which
# ones DIDN'T get applied. When completed, prefer the precise applied set
# (applied-recs.json or already-implemented) if we have it, else fall
# back to the input list.
EMAIL_REC_IDS="$RESPONDER_REC_IDS"
if [ "$COMPLETION_STATUS" = "completed" ]; then
    if [ -n "$APPLIED_REC_IDS" ]; then
        EMAIL_REC_IDS="$APPLIED_REC_IDS"
    elif [ -n "$ALREADY_IMPLEMENTED_REC_IDS" ]; then
        EMAIL_REC_IDS="$ALREADY_IMPLEMENTED_REC_IDS"
    fi
fi

# ── Close handoff loop on shipped recs ──────────────────────────────────
# When this run shipped a rec carrying fulfills_handoff_request_id (set by
# article-author / progressive-improvement / etc. in their proposal-to-rec
# conversion), call record_handoff_outcome(outcome="shipped") on the
# RECEIVING agent so the handoffs.jsonl ledger reflects a terminal state.
# Without this, every handoff sits at in_progress forever and the digest
# eventually flags it as stuck even though the work shipped.
#
# Only runs on completed-status runs (commit happened OR rec was verified
# already-implemented). Reads recommendations.json for the source-of-truth
# fulfills_handoff_request_id field, scoped to the EMAIL_REC_IDS that
# actually shipped this run.
if [ "$COMPLETION_STATUS" = "completed" ] && [ -n "$EMAIL_REC_IDS" ] \
        && [ -f "$RESPONDER_RUN_DIR/recommendations.json" ]; then
    PYTHONPATH="$REPO_ROOT" \
        REPO_ROOT="$REPO_ROOT" \
        SHIPPED_REC_IDS="$EMAIL_REC_IDS" \
        RECS_JSON="$RESPONDER_RUN_DIR/recommendations.json" \
        DOWNSTREAM_RUN_TS="${RESPONDER_RUN_TS:-}" \
        GIT_SHA="$GIT_SHA" \
        python3 - <<'PY' 2>&1 | sed 's/^/[handoff-close] /' || true
import json, os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
try:
    from framework.core.handoff import record_handoff_outcome
except Exception as e:
    print(f"framework import failed: {e}")
    sys.exit(0)
shipped_ids = {
    r.strip() for r in os.environ.get("SHIPPED_REC_IDS", "").split(",")
    if r.strip()
}
recs_path = os.environ["RECS_JSON"]
try:
    recs = json.load(open(recs_path)).get("recommendations", [])
except Exception as e:
    print(f"recs.json read failed: {e}")
    sys.exit(0)
n = 0
for r in recs:
    rid = r.get("id")
    if rid not in shipped_ids:
        continue
    handoff_rid = r.get("fulfills_handoff_request_id")
    receiver = r.get("fulfills_handoff_receiver_agent")
    if not handoff_rid or not receiver:
        continue
    try:
        record_handoff_outcome(
            agent_id=receiver,
            request_id=handoff_rid,
            outcome="shipped",
            outcome_detail=(
                f"implementer shipped rec={rid} via commit "
                f"{(os.environ.get('GIT_SHA','') or '')[:8]}"
            ),
            downstream_run_ts=os.environ.get("DOWNSTREAM_RUN_TS",""),
        )
        n += 1
        print(f"shipped → {receiver}/{handoff_rid} (rec={rid})")
    except Exception as e:
        print(f"record failed for {handoff_rid}: {e}")
print(f"recorded {n} shipped outcome(s)")
PY
fi

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
    --rec-ids "$EMAIL_REC_IDS" \
    --requested-rec-ids "$RESPONDER_REC_IDS" \
    --site "${RESPONDER_SITE:-}" \
    --source-agent "$SOURCE_AGENT" \
    --request-id "${RESPONDER_REQUEST_ID:-${RESPONDER_RUN_TS:-}}" \
    --run-dir "${RESPONDER_RUN_DIR:-}" \
    --commit-sha "$GIT_SHA" \
    --mode "${IMPLEMENTER_LLM:-claude}" \
    --site-config "${SEO_AGENT_CONFIG:-}" \
    --status "$COMPLETION_STATUS" \
    --status-reason "$COMPLETION_REASON" \
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


# ── Propagate lifecycle markers to source agent's recommendations.json ──────
# Claude writes implemented:true / implemented_via:pre-existing into the
# dispatch-dir copy of recommendations.json. The dashboard API reads from
# the SOURCE agent's run dir copy, so the markers never surface. Copy
# them back. Also auto-flip shipped:true for already-implemented (those
# are by definition already in production code — no deploy needed).
PYTHONPATH="$REPO_ROOT" \
SOURCE_AGENT="$SOURCE_AGENT" \
SOURCE_RUN_TS="${RESPONDER_RUN_TS:-}" \
DISPATCH_RUN_DIR="${RESPONDER_RUN_DIR:-}" \
GIT_SHA="$GIT_SHA" \
python3 - <<'PY' 2>&1 | sed 's/^/[propagate] /' || true
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path
from framework.core.storage import get_storage

source_agent = os.environ.get("SOURCE_AGENT", "")
source_run_ts = os.environ.get("SOURCE_RUN_TS", "")
dispatch_dir = os.environ.get("DISPATCH_RUN_DIR", "")
git_sha = os.environ.get("GIT_SHA", "") or ""
if not (source_agent and source_run_ts and dispatch_dir):
    print(f"missing inputs (agent={source_agent} run_ts={source_run_ts} dir={dispatch_dir}); skip")
    sys.exit(0)

dispatch_recs_path = Path(dispatch_dir) / "recommendations.json"
if not dispatch_recs_path.is_file():
    print(f"no recommendations.json in dispatch dir; skip")
    sys.exit(0)

dr = json.loads(dispatch_recs_path.read_text())
dispatch_by_id = {r.get("id"): r for r in dr.get("recommendations", []) if r.get("id")}

s = get_storage()
src_path = f"agents/{source_agent}/runs/{source_run_ts}/recommendations.json"
try:
    sd = s.read_json(src_path) or {}
except Exception as e:
    print(f"could not read {src_path}: {e}; skip")
    sys.exit(0)

now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
copy_fields = ("implemented", "implemented_at", "implemented_run_ts",
               "implemented_via", "implemented_commit",
               "shipped", "shipped_at", "shipped_tag", "shipped_image")
n_impl = n_ship = 0
for r in sd.get("recommendations", []):
    rid = r.get("id")
    src_rec = dispatch_by_id.get(rid)
    if not src_rec:
        continue
    # Copy any lifecycle fields claude wrote into dispatch copy
    for f in copy_fields:
        if f in src_rec and src_rec[f] is not None:
            r[f] = src_rec[f]
    # If marked implemented in this run but no implemented_at, stamp it
    if r.get("implemented") is True and not r.get("implemented_at"):
        r["implemented_at"] = now_iso
        r["implemented_run_ts"] = source_run_ts
        n_impl += 1
    # If git_sha was produced this run + this rec was implemented + no commit recorded
    if r.get("implemented") is True and git_sha and not r.get("implemented_commit"):
        r["implemented_commit"] = git_sha
    # Auto-ship: implemented_via=pre-existing means code already lives
    # in prod, mark shipped now (no deployer chain will run for these)
    if r.get("implemented") is True and r.get("implemented_via") == "pre-existing" and not r.get("shipped"):
        r["shipped"] = True
        r["shipped_at"] = now_iso
        r["shipped_via"] = "pre-existing"
        n_ship += 1
    # Auto-ship for article-author-proposal recs: the implementer writes
    # the markdown body + DB INSERT, no separate deployer step runs
    # (article-author dispatch_kind explicitly skips the deployer per
    # CLAUDE.md). Implementing the rec IS publishing it — the DB row
    # is already serving traffic. Mark shipped immediately so the
    # dashboard's lifetime stats stop showing them as
    # "implemented but not shipped" forever.
    if (r.get("implemented") is True
            and not r.get("shipped")
            and (r.get("type") == "article-author-proposal"
                 or "article-author" in (r.get("implemented_via") or ""))):
        r["shipped"] = True
        r["shipped_at"] = now_iso
        r["shipped_via"] = "article-author-db-insert"
        n_ship += 1

s.write_json(src_path, sd)
# Also update the LOCAL dispatch-dir copy with the same lifecycle markers
# (shipped:true etc.) — otherwise sync-back will overwrite our auto-ship
# flips with the unflipped tempdir copy on EXIT.
try:
    dispatch_dr = json.loads(dispatch_recs_path.read_text())
    src_by_id = {r.get("id"): r for r in sd.get("recommendations", [])}
    for r in dispatch_dr.get("recommendations", []):
        rid = r.get("id")
        sr = src_by_id.get(rid)
        if not sr:
            continue
        for f in copy_fields:
            if f in sr and sr[f] is not None:
                r[f] = sr[f]
    dispatch_recs_path.write_text(json.dumps(dispatch_dr, indent=2))
except Exception as e:
    print(f"local dispatch-dir update failed: {e}")
print(f"propagated to {src_path}: implemented={n_impl} auto-shipped={n_ship}")
PY


# ── Auto-chain to next batch ────────────────────────────────────────────────
# When the responder split a large reply (e.g. "implement all" → 123 recs)
# into smaller batches, it dropped a `dispatch-batches.json` manifest in the
# run dir and dispatched batch 1. Each batch is a separate implementer run;
# we need to mark this batch as done in the manifest, then spawn the next
# pending batch as a fresh systemd-run scope so it survives our exit.
if [ -f "$RESPONDER_RUN_DIR/dispatch-batches.json" ]; then
    NEXT_BATCH_INFO=$(
        RESPONDER_RUN_DIR="$RESPONDER_RUN_DIR" \
        RESPONDER_BATCH_INDEX="${RESPONDER_BATCH_INDEX:-0}" \
        COMPLETION_STATUS="$COMPLETION_STATUS" \
        DISPATCH_LOG_PATH="${DISPATCH_LOG_PATH:-}" \
        python3 - <<'PY' 2>/dev/null
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

run_dir = Path(os.environ["RESPONDER_RUN_DIR"])
manifest_path = run_dir / "dispatch-batches.json"
if not manifest_path.is_file():
    sys.exit(0)
m = json.loads(manifest_path.read_text())
cur_idx = int(os.environ.get("RESPONDER_BATCH_INDEX", "0") or "0")
status = os.environ.get("COMPLETION_STATUS", "completed")
log_path = os.environ.get("DISPATCH_LOG_PATH", "")
ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

next_batch = None
for b in m.get("batches", []):
    # Mark current batch as completed/paused. Accept either 'pending'
    # (never-flipped — possible if a manual dispatch skipped the
    # "running" handoff) or 'running' (the normal case once the
    # previous batch's chain code flipped it).
    if b.get("index") == cur_idx and b.get("status") in ("pending", "running"):
        b["status"] = status
        b["completed_at"] = ts
        b["completion_status"] = status
        if log_path and not b.get("dispatch_log"):
            b["dispatch_log"] = log_path
for b in m.get("batches", []):
    if b.get("status") == "pending":
        next_batch = b
        break

# Stop chaining if too many consecutive pauses (claude keeps refusing —
# something structural is wrong, don't waste LLM time on the rest).
recent = m.get("batches", [])[: cur_idx]
recent_paused = [b for b in recent if b.get("completion_status") == "paused"]
if len(recent_paused) >= 3:
    print("[chain] 3+ consecutive paused batches — stopping auto-chain", file=sys.stderr)
    next_batch = None

manifest_path.write_text(json.dumps(m, indent=2))
if next_batch:
    next_batch["started_at"] = ts
    next_batch["status"] = "running"
    manifest_path.write_text(json.dumps(m, indent=2))
    # Stdout: <index>\t<comma-separated rec_ids>
    print(f"{next_batch['index']}\t{','.join(next_batch['rec_ids'])}")
PY
)
    if [ -n "$NEXT_BATCH_INFO" ]; then
        NEXT_IDX=$(echo "$NEXT_BATCH_INFO" | cut -f1)
        NEXT_RECS=$(echo "$NEXT_BATCH_INFO" | cut -f2)
        echo "[implementer] auto-chain → batch $NEXT_IDX ($(echo "$NEXT_RECS" | tr ',' '\n' | wc -l) recs)"

        # Read total batch count for the env
        BATCH_TOTAL=$(python3 -c "import json; print(len(json.load(open('$RESPONDER_RUN_DIR/dispatch-batches.json'))['batches']))" 2>/dev/null || echo "0")

        # Dispatch the next batch in its own systemd-run scope so it
        # survives our exit. Inherit all current env (storage creds,
        # site config, etc.) and override the rec ids + batch index.
        NEW_LOG="/tmp/reusable-agents-logs/dispatch-implementer-${RESPONDER_SITE}-$(date -u +%Y%m%dT%H%M%SZ).log"
        UNIT_NAME="agent-dispatch-implementer-${RESPONDER_SITE}-batch${NEXT_IDX}-$(date -u +%Y%m%dT%H%M%SZ)"
        if command -v systemd-run >/dev/null 2>&1; then
            systemd-run --user --scope --collect \
                --unit="$UNIT_NAME" \
                --property=KillMode=process \
                --property=TimeoutStopSec=0 \
                --setenv=RESPONDER_REC_IDS="$NEXT_RECS" \
                --setenv=RESPONDER_BATCH_INDEX="$NEXT_IDX" \
                --setenv=RESPONDER_BATCH_TOTAL="$BATCH_TOTAL" \
                --setenv=DISPATCH_LOG_PATH="$NEW_LOG" \
                bash "$0" </dev/null >"$NEW_LOG" 2>&1 &
            disown $! 2>/dev/null || true
            echo "[implementer] auto-chain spawned batch $NEXT_IDX log=$NEW_LOG"
        else
            # No systemd-run available: best-effort nohup chain.
            RESPONDER_REC_IDS="$NEXT_RECS" \
            RESPONDER_BATCH_INDEX="$NEXT_IDX" \
            RESPONDER_BATCH_TOTAL="$BATCH_TOTAL" \
            DISPATCH_LOG_PATH="$NEW_LOG" \
            nohup bash "$0" </dev/null >"$NEW_LOG" 2>&1 &
            disown $! 2>/dev/null || true
            echo "[implementer] auto-chain spawned batch $NEXT_IDX (nohup) log=$NEW_LOG"
        fi
    else
        echo "[implementer] no further batches — chain complete"
    fi
fi
