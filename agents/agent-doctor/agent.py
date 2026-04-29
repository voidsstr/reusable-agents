#!/usr/bin/env python3
"""agent-doctor — continuous self-healer for the reusable-agents fleet.

What it does on each tick (every 5 min):
  1. Polls /api/agents from the framework API.
  2. For each agent: detects FAILURE (last_run_status=failure newer than
     last seen) or STUCK (status=running with updated_at older than 2x
     historical p95 duration).
  3. Reads the failing run's log + status.json + last 3 successful runs
     for diff context. Classifies the error signature.
  4. Looks up the error_signature in `agents/agent-doctor/fixes-log.jsonl`:
     - If the same signature was already attempted unsuccessfully twice,
       escalates to email (do not loop).
     - Otherwise, applies the matching recipe.
  5. Recipes (safe-by-default — only known patterns):
     - oauth-token-expired   → re-mint via mint-token.py
     - transient-network     → no-op; the next cron firing retries
     - stale-lock            → remove file, log
     - missing-env-var       → email operator with the var name
     - python-traceback      → email operator with traceback excerpt
     - timeout               → email operator + suggest cron extension
     - unknown               → email full diagnosis
  6. Writes per-attempt entries to fixes-log.jsonl (durable history).

Anything code-edit-shaped (schema migration, agent.py change) is NEVER
auto-applied — those go to email with full context.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Framework path setup
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.storage import get_storage  # noqa: E402

AGENT_ID = "agent-doctor"

# Tunables
STUCK_GRACE_MULTIPLIER = 2.0      # mark stuck if running > p95 * this
STUCK_FALLBACK_MINUTES = 30       # fallback when no historical p95
MAX_RETRIES_PER_SIGNATURE = 2     # escalate to email after this many
WEEKLY_DIGEST_DAYS = 7            # cadence for the summary email

# Local log paths (host-side scratch, not framework storage)
HOST_LOGS_DIR = Path("/tmp/reusable-agents-logs")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _api_get(url: str, token: str) -> Any:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _classify_error(log_text: str, status_obj: dict) -> tuple[str, str]:
    """Return (signature, short_excerpt). Signature is a stable category
    string used as the recipe-lookup key + dedupe key in fixes-log.jsonl."""
    if not log_text and status_obj.get("state") == "running":
        return ("stuck-no-output", "")
    txt = (log_text or "")[-8000:]  # last 8KB matters most for failures
    excerpt = txt[-1200:] if len(txt) > 1200 else txt

    # OAuth token expired / Graph 401
    if re.search(r"\b(401|invalid[_-]?token|token expired|TokenExpired|"
                 r"insufficient[_-]?scope|InvalidAuthenticationToken)\b", txt, re.I):
        return ("oauth-token-expired", excerpt)

    # IMAP auth failure (different recipe — XOAUTH2 vs Graph)
    if re.search(r"\b(imap.*auth|AUTHENTICATIONFAILED|LOGIN failed)\b", txt, re.I):
        return ("imap-auth-failed", excerpt)

    # Network blips
    if re.search(r"\b(connection refused|connection reset|temporary failure|"
                 r"name resolution|Could not resolve|timed out|gateway timeout|"
                 r"TimeoutError)\b", txt, re.I):
        return ("transient-network", excerpt)

    # Python tracebacks (must be after more-specific matches)
    if re.search(r"^Traceback \(most recent call last\):", txt, re.M):
        # Look for specific subtypes
        if re.search(r"\bFileNotFoundError\b", txt):
            return ("missing-file", excerpt)
        if re.search(r"\b(KeyError|UndefinedColumn|column .* does not exist)\b", txt):
            return ("schema-drift", excerpt)
        if re.search(r"\b(ImportError|ModuleNotFoundError)\b", txt):
            return ("missing-import", excerpt)
        if re.search(r"\b(MemoryError|OOM)\b", txt):
            return ("oom", excerpt)
        return ("python-traceback", excerpt)

    # Missing env var (heuristic)
    m = re.search(r"(\w+) (is required|must be set|not in env|environment variable)", txt)
    if m:
        return (f"missing-env-{m.group(1).lower()}", excerpt)

    # systemd timeout
    if re.search(r"\b(Timed out|TimeoutStopSec|killed.*signal=KILL)\b", txt):
        return ("timeout", excerpt)

    # Stale lock file
    if re.search(r"\b(lock file|lock acquired|already running)\b", txt, re.I):
        return ("stale-lock", excerpt)

    # Default
    return ("unknown", excerpt)


def _read_log(agent_id: str) -> str:
    """Read the most recent agent log from /tmp/reusable-agents-logs.

    Strategy: prefer error-context over chronological tail. Long-running
    agents (kitchen-scraper, market-research-pipeline) generate megabytes
    of routine HTTP-200 log lines that drown out the actual failure when
    the tail captures only the last 32KB. So we:

      1. Scan the WHOLE file for error patterns (Traceback, ERROR, FAIL,
         exited rc=, OperationalError, etc.).
      2. For each match, extract a 1KB window around it.
      3. If we found >0 matches, return concatenated windows (capped 32KB).
      4. Otherwise fall back to the last 32KB tail (preserves the old
         behavior for agents that fail without a clear error pattern).
    """
    if not HOST_LOGS_DIR.is_dir():
        return ""
    candidates = sorted(
        HOST_LOGS_DIR.glob(f"agent-{agent_id}.log"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    candidates += sorted(
        HOST_LOGS_DIR.glob(f"{agent_id}-*.log"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    if not candidates:
        return ""
    try:
        text = candidates[0].read_text(errors="replace")
    except Exception:
        return ""

    # Find all error-pattern matches across the WHOLE file
    error_patterns = [
        r"^Traceback \(most recent call last\):",
        r"\b(ERROR|CRITICAL)\b",
        r"\bOperationalError\b",
        r"\bInterfaceError\b",
        r"\bTimeoutError\b",
        r"\bConnectionError\b",
        r"\bAuthenticationError\b",
        r"exited rc=[1-9]",
        r"FAILURE",
        r"unrecoverable error",
        r"\bkilled by signal\b",
        r"Permission denied",
        r"\b5\d\d (Server Error|Internal Server Error)",
    ]
    pat = re.compile("|".join(error_patterns), re.MULTILINE)
    windows: list[tuple[int, str]] = []
    seen_offsets: set[int] = set()
    for m in pat.finditer(text):
        # Snap to a 1KB context window around each match
        start = max(0, m.start() - 400)
        end = min(len(text), m.end() + 600)
        # Dedupe overlapping windows
        if any(abs(start - o) < 200 for o in seen_offsets):
            continue
        seen_offsets.add(start)
        windows.append((start, text[start:end]))
        if len(windows) >= 12:
            break

    if windows:
        # Sort by file-position so chronological order is preserved
        windows.sort(key=lambda w: w[0])
        body = "\n=== ... ===\n".join(w[1] for w in windows)
        # Always include the LAST 4KB too so the wrapper's exit summary
        # (rc=N, etc.) is visible regardless of where the error patterns
        # landed
        body = body + "\n=== TAIL ===\n" + text[-4000:]
        return body[:32000]

    # No error pattern match — fall back to chronological tail
    return text[-32000:]


def _has_llm_attempt_for(s, agent_id: str, signature: str) -> bool:
    """Has agent-doctor ever LLM-diagnosed this (agent, signature) pair?
    Used to decide whether to bypass the pattern-classifier retry cap
    when 'unknown' has been retried but the LLM hasn't been tried yet."""
    try:
        entries = list(s.read_jsonl(f"agents/{AGENT_ID}/fixes-log.jsonl") or [])
    except Exception:
        return False
    for e in entries:
        if e.get("target_agent") != agent_id:
            continue
        if e.get("error_signature") != signature:
            continue
        notes = (e.get("notes") or "")
        # Markers that indicate the LLM diagnose path ran (vs the static
        # pattern-recipe path). Both branches log to fixes-log; we
        # differentiate via notes prefix and recipe_applied value.
        if "LLM" in notes or "diag=" in notes:
            return True
    return False


# Verdicts the LLM can hand down that mean "no further escalation needed
# even if the issue keeps recurring." If we see ANY of these in fixes-log
# for this (agent, signature), suppress the loop-broken email — the
# operator already knows the situation is fundamentally not-actionable
# from earlier emails.
_NON_ACTIONABLE_LLM_VERDICTS = (
    "fix=data-issue",
    "fix=external-blocker",
    "fix=wait",
)


def _recent_escalation_with_same_diagnosis(s, agent_id: str, signature: str,
                                              current_excerpt: str,
                                              within_hours: int = 24) -> bool:
    """Return True if we already escalated this (agent, signature) with a
    similar log excerpt within the last `within_hours`. Prevents the
    "loop-broken" email from re-firing every 5 min when the underlying
    issue hasn't changed.

    "Similar" = the first 500 chars of the excerpt overlap by >70%
    (cheap shingle-style approximation; doesn't need exact match because
    timestamps and pid numbers in logs vary cycle-to-cycle).
    """
    try:
        entries = list(s.read_jsonl(f"agents/{AGENT_ID}/fixes-log.jsonl") or [])
    except Exception:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    cur_norm = _normalize_excerpt_for_dedup(current_excerpt)
    for e in reversed(entries):
        if e.get("target_agent") != agent_id or e.get("error_signature") != signature:
            continue
        if e.get("outcome") != "escalated":
            continue
        try:
            ts = datetime.fromisoformat(e.get("ts", "").replace("Z", "+00:00"))
            if ts < cutoff:
                break  # entries are append-only & ordered; no point scanning further
        except Exception:
            continue
        prior_norm = _normalize_excerpt_for_dedup(e.get("log_excerpt", ""))
        if _excerpts_similar(cur_norm, prior_norm):
            return True
    return False


def _normalize_excerpt_for_dedup(text: str) -> str:
    """Strip volatile bits (timestamps, pids, ephemeral run-ts) so two log
    samples from different cycles of the same failure compare as equal."""
    if not text:
        return ""
    import re as _re
    t = text[:1500]
    # ISO timestamps (2026-04-29T22:50:04, 2026-04-29 22:50:04,123)
    t = _re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?", "", t)
    # systemd PIDs / process ids
    t = _re.sub(r"\bpid[=\s]+\d+\b", "pid=N", t, flags=_re.I)
    t = _re.sub(r"\b\d{5,7}\b", "N", t)
    # run-ts strings (20260429T123045Z)
    t = _re.sub(r"\d{8}T\d{6}Z", "RUNTS", t)
    # Lowercase + collapse whitespace
    return _re.sub(r"\s+", " ", t.lower()).strip()


def _excerpts_similar(a: str, b: str, threshold: float = 0.70) -> bool:
    """Cheap Jaccard-like similarity on word shingles."""
    if not a or not b:
        return False
    aw = set(a.split())
    bw = set(b.split())
    if not aw or not bw:
        return False
    inter = len(aw & bw)
    union = len(aw | bw)
    return (inter / union) >= threshold if union else False


def _llm_says_non_actionable(s, agent_id: str, signature: str) -> tuple[bool, str]:
    """If a prior LLM diagnosis classified this issue as non-actionable
    (data-issue / external-blocker / wait), return (True, latest_note).
    The operator already got that diagnosis emailed; sending the same
    "loop-broken" escalation every cycle is just noise."""
    try:
        entries = list(s.read_jsonl(f"agents/{AGENT_ID}/fixes-log.jsonl") or [])
    except Exception:
        return False, ""
    # Walk newest first
    for e in reversed(entries):
        if e.get("target_agent") != agent_id:
            continue
        if e.get("error_signature") != signature:
            continue
        notes = (e.get("notes") or "")
        for marker in _NON_ACTIONABLE_LLM_VERDICTS:
            if marker in notes:
                return True, notes[:200]
    return False, ""


def _has_fixes_log_entry_for(s, agent_id: str, last_run_at: str) -> bool:
    """Return True iff fixes-log.jsonl has at least one entry for this
    (target_agent, last_run_at) tuple. Used to validate that seen.json's
    dedup is backed by actual investigation history."""
    if not last_run_at:
        return False
    try:
        entries = list(s.read_jsonl(f"agents/{AGENT_ID}/fixes-log.jsonl") or [])
    except Exception:
        return False
    for e in entries:
        if e.get("target_agent") == agent_id and e.get("last_run_at") == last_run_at:
            return True
    return False


def _signature_attempt_count(s, agent_id: str, signature: str) -> int:
    """How many times have we tried this signature for this agent and
    NOT succeeded? Used to escalate after MAX_RETRIES_PER_SIGNATURE."""
    try:
        entries = list(s.read_jsonl(f"agents/{AGENT_ID}/fixes-log.jsonl") or [])
    except Exception:
        return 0
    count = 0
    for e in reversed(entries):
        if e.get("target_agent") != agent_id:
            continue
        if e.get("error_signature") != signature:
            continue
        if e.get("outcome") == "fixed":
            return 0  # successful fix resets the counter
        count += 1
        if count >= MAX_RETRIES_PER_SIGNATURE + 1:
            break
    return count


def _email_operator(target_agent: str, signature: str, excerpt: str,
                     agent_meta: dict, recipe: str = "(none — escalated)") -> bool:
    """Send a diagnosis email to the operator. Uses framework completion-email
    machinery if available; else logs the would-be email."""
    try:
        from framework.core.resilience import notify_operator
        class _E(Exception):
            pass
        notify_operator(
            agent_id=AGENT_ID,
            error=_E(f"{target_agent}: {signature}"),
            context={
                "phase": "agent-doctor-investigation",
                "target_agent": target_agent,
                "error_signature": signature,
                "recipe": recipe,
                "log_excerpt": excerpt[:1500],
                "last_run_at": agent_meta.get("last_run_at", ""),
                "last_run_status": agent_meta.get("last_run_status", ""),
                "cron_expr": agent_meta.get("cron_expr", ""),
            },
            severity="medium",
        )
        return True
    except Exception as e:
        print(f"[agent-doctor] email_operator failed: {e}", file=sys.stderr)
        return False


# ── Weekly digest ─────────────────────────────────────────────────────────

def _empty_stats() -> dict:
    return {
        "week_start": _now_iso(),
        "ticks": 0,
        "agents_checked_total": 0,
        "investigations": 0,
        "fixes_attempted": 0,
        "fixes_succeeded": 0,
        "escalations": 0,
        "no_ops": 0,
        "signatures": {},        # signature → count
        "targets": {},           # target_agent → count
        "outcomes": {},          # outcome → count
    }


def _load_weekly_stats(s) -> dict:
    try:
        cur = s.read_json(f"agents/{AGENT_ID}/weekly-stats.json")
        if isinstance(cur, dict) and "week_start" in cur:
            return cur
    except Exception:
        pass
    return _empty_stats()


def _save_weekly_stats(s, stats: dict) -> None:
    try:
        s.write_json(f"agents/{AGENT_ID}/weekly-stats.json", stats)
    except Exception as e:
        print(f"[agent-doctor] weekly-stats write failed: {e}", file=sys.stderr)


def _maybe_send_weekly_digest(s, stats: dict) -> bool:
    """If the current week is older than WEEKLY_DIGEST_DAYS *and* there
    is something actionable to report, send the digest email and reset
    stats. Returns True iff sent.

    Actionability bar — at least ONE of:
      - escalations > 0 (operator was asked to act on something)
      - any error signature recurred ≥ MAX_RETRIES_PER_SIGNATURE times
        and wasn't fixed
      - investigations > 0 with no successful fixes (suggests the
        doctor is blind to a class of failure)

    A green week (no escalations, no recurring errors, every fix
    succeeded) gets no email — there is nothing for the operator to
    do, and routine status updates only train them to ignore the
    inbox.
    """
    try:
        ws = stats.get("week_start", "")
        if not ws:
            return False
        started = datetime.fromisoformat(ws.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - started < timedelta(days=WEEKLY_DIGEST_DAYS):
            return False
    except Exception:
        return False

    escalations = int(stats.get("escalations", 0))
    investigations = int(stats.get("investigations", 0))
    fixes_succeeded = int(stats.get("fixes_succeeded", 0))
    has_actionable = (
        escalations > 0
        or (investigations > 0 and fixes_succeeded == 0)
    )
    if not has_actionable:
        # Reset the window without emailing — silence is the signal.
        stats["week_start"] = _now_iso()
        for k in ("ticks", "agents_checked_total", "investigations",
                  "fixes_succeeded", "escalations"):
            stats[k] = 0
        for k in ("signatures", "targets", "outcomes"):
            if isinstance(stats.get(k), dict):
                stats[k].clear()
        _save_weekly_stats(s, stats)
        print("[agent-doctor] digest: green week, no email "
              f"(investigations={investigations} fixes={fixes_succeeded} "
              f"escalations={escalations})", file=sys.stderr)
        return False

    # Build digest using the shared framework email primitives.
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from framework.core import email_templates as et
    except Exception as e:
        sys.stderr.write(f"[agent-doctor] email_templates import failed, skipping digest: {e}\n")
        return False

    week_end = _now_iso()
    sigs_sorted = sorted(stats.get("signatures", {}).items(), key=lambda kv: -kv[1])
    targets_sorted = sorted(stats.get("targets", {}).items(), key=lambda kv: -kv[1])
    outcomes_sorted = sorted(stats.get("outcomes", {}).items(), key=lambda kv: -kv[1])

    summary_table = et.table(
        ["Metric", "Count"],
        [
            ["Wakeups (5-min ticks)", str(stats.get("ticks", 0))],
            ["Agents checked total", str(stats.get("agents_checked_total", 0))],
            ["Investigations", str(stats.get("investigations", 0))],
            [f"<span style='color:{et.SUCCESS_FG}'>Fixes succeeded</span>",
             f"<span style='color:{et.SUCCESS_FG}'>{stats.get('fixes_succeeded', 0)}</span>"],
            [f"<span style='color:{et.ERROR_FG}'>Escalations sent</span>",
             f"<span style='color:{et.ERROR_FG}'>{stats.get('escalations', 0)}</span>"],
        ],
        zebra=True,
    )
    by_outcome = et.table(
        ["Outcome", "Count"],
        [[k, str(v)] for k, v in outcomes_sorted] or [["(none)", ""]],
    )
    by_signature = et.table(
        ["Signature", "Count"],
        [[k, str(v)] for k, v in sigs_sorted] or [["(none)", ""]],
    )
    by_target = et.table(
        ["Agent", "Count"],
        [[k, str(v)] for k, v in targets_sorted] or [["(none)", ""]],
        monospace_first=True,
    )

    body = et.page(
        title="agent-doctor — weekly summary",
        body_parts=[
            et.header_bar(
                "agent-doctor",
                sublabel="weekly activity summary",
                run_id=f"{stats.get('week_start','')[:10]} → {week_end[:10]}",
            ),
            et.callout(
                "info",
                f"{stats.get('investigations', 0)} investigations this week",
                f"Per-tick alerts only fire when an investigation needs your "
                f"attention; this digest is the periodic activity summary. "
                f"<b>{stats.get('fixes_succeeded', 0)}</b> auto-fixes applied · "
                f"<b>{stats.get('escalations', 0)}</b> escalations emailed.",
            ),
            et.section_h2("Totals"),
            summary_table,
            et.section_h2("By outcome"),
            by_outcome,
            et.section_h2("By error signature"),
            by_signature,
            et.section_h2("By target agent"),
            by_target,
        ],
    )

    # Send via shared sender
    try:
        sys.path.insert(0, str(_REPO_ROOT))
        from shared.site_quality import send_via_msmtp
    except Exception as e:
        print(f"[agent-doctor] digest: import send_via_msmtp failed: {e}", file=sys.stderr)
        return False

    operator = (os.environ.get("FRAMEWORK_OPERATOR_EMAIL")
                or "mperry@northernsoftwareconsulting.com")
    sender = (os.environ.get("IMPLEMENTER_FROM")
              or "automation@northernsoftwareconsulting.com")
    subject = (f"[agent-doctor] weekly summary — "
               f"{stats.get('investigations',0)} investigations · "
               f"{stats.get('escalations',0)} escalations")
    try:
        ok, detail = send_via_msmtp(
            subject=subject, body_html=body, to=[operator],
            sender=sender,
        )
        print(f"[agent-doctor] digest send: ok={ok} detail={detail}", file=sys.stderr)
        return bool(ok)
    except Exception as e:
        print(f"[agent-doctor] digest send failed: {e}", file=sys.stderr)
        return False


# ── Recipes ──────────────────────────────────────────────────────────────
# Each recipe returns (outcome, notes). outcome ∈ {"fixed", "no-op",
# "escalated", "skipped"}. "fixed" means we believe the next run will
# succeed; "no-op" means transient and the next cron firing will retry;
# "escalated" means we sent an email and the human must intervene.

def recipe_oauth_token_expired(target: str, ctx: dict) -> tuple[str, str]:
    # Try to mint a fresh token via mint-token.py if it exists.
    mint = _REPO_ROOT / "agents" / "responder-agent" / "mint-token.py"
    if mint.is_file():
        import subprocess
        try:
            r = subprocess.run(
                ["python3", str(mint), "--refresh"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                return ("fixed", "minted fresh OAuth token via mint-token.py")
            return ("escalated", f"mint-token.py exited {r.returncode}: {r.stderr[-300:]}")
        except Exception as e:
            return ("escalated", f"mint-token failed: {e}")
    return ("escalated", "no mint-token.py found")


def recipe_transient_network(target: str, ctx: dict) -> tuple[str, str]:
    return ("no-op", "transient network blip — next cron firing retries")


def recipe_stale_lock(target: str, ctx: dict) -> tuple[str, str]:
    # Look for /tmp/agent-<id>*.lock (convention).
    import glob
    locks = glob.glob(f"/tmp/agent-{target}*.lock") + \
            glob.glob(f"/tmp/{target}*.lock")
    removed = []
    for lk in locks:
        try:
            # Only remove if older than 1h
            if (time.time() - os.path.getmtime(lk)) > 3600:
                os.unlink(lk)
                removed.append(lk)
        except Exception:
            pass
    if removed:
        return ("fixed", f"removed stale lock(s): {', '.join(removed)}")
    return ("no-op", "no stale lock found")


def recipe_timeout(target: str, ctx: dict) -> tuple[str, str]:
    # Don't auto-extend timeouts — escalate so the human can decide if the
    # cron interval is wrong or if there's a runaway loop.
    return ("escalated", "systemd timeout — manual review needed for cron interval / agent runtime")


def _read_agent_runbook(agent_id: str) -> str:
    """Try to find the agent's runbook (AGENT.md or readme) by scanning
    known repo locations. Best-effort — returns empty if not found."""
    candidates = [
        Path("/home/voidsstr/development/reusable-agents/agents") / agent_id / "AGENT.md",
        Path("/home/voidsstr/development/reusable-agents/agents") / agent_id / "README.md",
        Path("/home/voidsstr/development/nsc-assistant/agents") / agent_id / "AGENT.md",
        Path("/home/voidsstr/development/nsc-assistant/agents") / agent_id / "README.md",
        Path("/home/voidsstr/development/specpicks/agents") / agent_id / "AGENT.md",
        Path("/home/voidsstr/development/specpicks/agents") / agent_id / "README.md",
    ]
    # Also try stripping site prefix: aisleprompt-progressive-improvement-agent → progressive-improvement-agent
    parts = agent_id.split("-")
    if len(parts) > 2:
        stem = "-".join(parts[1:])
        candidates += [
            Path("/home/voidsstr/development/reusable-agents/agents") / stem / "AGENT.md",
            Path("/home/voidsstr/development/reusable-agents/agents") / stem / "README.md",
        ]
    for p in candidates:
        if p.is_file():
            try:
                return p.read_text()[:8000]  # cap context
            except Exception:
                pass
    return ""


def _read_agent_manifest_dict(agent_id: str) -> dict:
    """Pull the agent's manifest from the framework API."""
    try:
        api = (os.environ.get("FRAMEWORK_API_URL") or "http://localhost:8093").rstrip("/")
        token = os.environ.get("FRAMEWORK_API_TOKEN", "")
        if not token:
            env = _REPO_ROOT / ".env"
            if env.is_file():
                for L in env.read_text().splitlines():
                    if L.startswith("FRAMEWORK_API_TOKEN="):
                        token = L.split("=", 1)[1].strip().strip('"').strip("'")
        return _api_get(f"{api}/api/agents/{agent_id}", token) or {}
    except Exception:
        return {}


def _invoke_claude_diagnose(target: str, signature: str, log_excerpt: str,
                             manifest: dict, runbook: str) -> dict:
    """Shell to claude with structured prompt; expect JSON back. Returns
    the parsed dict, or {} on any error. The claude call routes through
    the pool automatically (PATH-resolved `claude` is the pool shim)."""
    import subprocess
    prompt = f"""You are the operations diagnostician for the reusable-agents framework.
An agent has failed and the pattern-classifier returned signature={signature!r}
because no known pattern matched. Your job: diagnose the root cause and
propose ONE fix from the structured set below.

# Failed agent
id: {target}
manifest cron: {manifest.get('cron_expr', '?')}
manifest entry_command (truncated):
  {(manifest.get('entry_command', '') or '')[:600]}
last_run_status: {manifest.get('last_run_status', '?')} at {manifest.get('last_run_at', '?')}

# Runbook (first 8KB)
{runbook[:8000] if runbook else '(no runbook found)'}

# Last log excerpt (most recent ~1KB of stderr/stdout)
{log_excerpt[-1500:]}

# Your task

Output ONE single JSON object (no prose, no markdown fences) with this shape:

{{
  "diagnosis": "1-2 sentences on what failed and why",
  "confidence": 0.0,
  "fix_type": "wait" | "manifest-env-inline" | "stale-service-unit" | "manifest-cron-fix" | "runbook-tweak" | "code-edit" | "data-issue" | "external-blocker" | "escalate",
  "fix_detail": "specific change to make. For manifest-env-inline: 'VAR=literal-value'. For runbook-tweak: 1-line description of the change to make to the runbook so this won't recur. For others: brief description.",
  "auto_apply": true|false
}}

# Fix type guide

- "wait"               — transient (network blip, momentary 5xx); next cron firing will succeed (auto_apply=true)
- "manifest-env-inline" — entry_command uses ${{VAR}} that systemd doesn't expand; fix_detail must be 'VAR=literal-value' (auto_apply=true if confident)
- "stale-service-unit" — manifest on disk looks right but running .service is stale (e.g. someone hand-edited the manifest without re-registering); doctor will POST to /api/agents/register (auto_apply=true)
- "manifest-cron-fix"  — cron is wrong (auto_apply=false; needs human review of intent)
- "runbook-tweak"      — agent's runbook (AGENT.md / ARTICLE_AUTHOR.md / etc.) is missing instructions that would have prevented this; fix_detail describes the change in 1 line (auto_apply=true — doctor will shell to claude to make the actual edit + commit + push). Use this when the failure is "claude misunderstood what to do" not "the code is broken"
- "code-edit"          — bug in agent.py / a script (auto_apply=false; we'll email full context for human to apply)
- "data-issue"         — bad input data (e.g. junk GSC query, malformed CSV); not a code/infra problem (auto_apply=true; agent should skip + move on)
- "external-blocker"   — third-party API rate-limited/down/changed; nothing we can do (auto_apply=false)
- "escalate"           — you can't tell from this evidence; ask human (auto_apply=false)

Be DIRECT and SPECIFIC. Don't hedge. confidence ∈ [0, 1] reflects how
sure you are about both diagnosis AND fix. Set auto_apply=true only when
both confidence ≥ 0.8 AND fix_type is in the safe-to-auto set
(wait, manifest-env-inline, stale-service-unit, runbook-tweak,
data-issue).
"""
    try:
        # Use claude CLI via the pool. --print = one-shot, no session.
        # Short max-turns since we want a single JSON response.
        proc = subprocess.run(
            ["claude", "--print", "--dangerously-skip-permissions",
             "--max-turns", "1", "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            return {"error": f"claude rc={proc.returncode}",
                    "stderr": proc.stderr[-300:]}
        out = proc.stdout.strip()
        # Strip optional markdown fences just in case
        if out.startswith("```"):
            out = out.strip("`")
            out = re.sub(r"^json\s*\n", "", out, flags=re.I)
            out = out.rstrip("`").strip()
        return json.loads(out)
    except json.JSONDecodeError as e:
        return {"error": f"non-JSON response: {e}", "raw": out[:500] if 'out' in dir() else ""}
    except Exception as e:
        return {"error": str(e)}


def _re_register_agent(target: str) -> tuple[bool, str]:
    """POST the agent's manifest to /api/agents/register so the framework
    re-reads it from disk and (re)writes its systemd unit. Used when the
    on-disk manifest was edited (by us or by hand) but the running .service
    is stale."""
    api = (os.environ.get("FRAMEWORK_API_URL") or "http://localhost:8093").rstrip("/")
    token = os.environ.get("FRAMEWORK_API_TOKEN", "")
    # Find the manifest on disk
    candidates = []
    for base in [
        "/home/voidsstr/development/nsc-assistant/agents",
        "/home/voidsstr/development/specpicks/agents",
        "/home/voidsstr/development/reusable-agents/agents",
    ]:
        candidates.append(Path(base) / target / "manifest.json")
        parts = target.split("-")
        if len(parts) > 2:
            candidates.append(Path(base) / "-".join(parts[1:]) / "manifest.json")
    manifest_path = None
    for p in candidates:
        if p.is_file():
            try:
                d = json.loads(p.read_text())
                if d.get("id") == target:
                    manifest_path = p
                    break
            except Exception:
                pass
    if not manifest_path:
        return False, f"manifest.json not found for {target}"
    try:
        body = manifest_path.read_text().encode()
        req = urllib.request.Request(
            f"{api}/api/agents/register",
            data=body, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status < 400, f"re-registered via /api/agents/register (rc={resp.status})"
    except Exception as e:
        return False, f"re-register POST failed: {e}"


def _apply_runbook_tweak(target: str, fix_detail: str, log_excerpt: str) -> tuple[bool, str]:
    """Have claude edit the agent's runbook to incorporate the fix.

    fix_detail describes what to change (1-line summary from the diagnostic
    LLM). We shell to claude with: the runbook content + the failing log
    excerpt + the fix_detail; ask it to produce the corrected runbook.
    Then write the result back, git commit + push.
    """
    runbook_path = None
    for base in [
        "/home/voidsstr/development/reusable-agents/agents",
        "/home/voidsstr/development/nsc-assistant/agents",
        "/home/voidsstr/development/specpicks/agents",
    ]:
        for fname in ("AGENT.md", "ARTICLE_AUTHOR.md", "H2H.md", "CATALOG_AUDIT.md", "README.md"):
            cand = Path(base) / target / fname
            if cand.is_file():
                runbook_path = cand
                break
            parts = target.split("-")
            if len(parts) > 2:
                cand = Path(base) / "-".join(parts[1:]) / fname
                if cand.is_file():
                    runbook_path = cand
                    break
        if runbook_path:
            break
    if not runbook_path:
        return False, f"no runbook found for {target}"
    original = runbook_path.read_text()
    # Cap original at 16KB so the prompt stays bounded
    if len(original) > 16000:
        return False, f"runbook too large ({len(original)} chars) for safe auto-edit"

    prompt = f"""Edit the runbook below so the failure described in the
log excerpt won't recur. Return ONLY the corrected runbook in full
markdown; no commentary, no fences. The fix to apply: {fix_detail}

# Failure log excerpt (last ~1500 chars)
{log_excerpt[-1500:]}

# Current runbook ({runbook_path})
{original}
"""
    try:
        proc = subprocess.run(
            ["claude", "--print", "--dangerously-skip-permissions",
             "--max-turns", "1", "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            return False, f"claude edit rc={proc.returncode}: {proc.stderr[-200:]}"
        new_text = proc.stdout
        # Strip any ``` markdown fences claude may have added despite the instruction
        if new_text.startswith("```"):
            new_text = re.sub(r"^```\w*\n", "", new_text)
            new_text = re.sub(r"\n```$", "", new_text.rstrip())
        # Sanity: the edit shouldn't shrink the file by >40% (suggests claude
        # truncated something). And it must not be empty.
        if len(new_text.strip()) < 100:
            return False, "claude returned suspiciously short content; rejecting"
        if len(new_text) < len(original) * 0.6:
            return False, f"edit shrunk file from {len(original)} to {len(new_text)} (>40% drop); rejecting"
        # Write + commit
        runbook_path.write_text(new_text)
        repo_root = subprocess.run(
            ["git", "-C", str(runbook_path.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not repo_root:
            return False, "couldn't resolve git repo root"
        commit_msg = f"agent-doctor: auto-fix runbook for {target}\n\nFix applied: {fix_detail[:200]}\n\nGenerated by agent-doctor recipe_llm_diagnose / runbook-tweak."
        for cmd in (
            ["git", "-C", repo_root, "add", str(runbook_path)],
            ["git", "-C", repo_root, "commit", "-m", commit_msg],
            ["git", "-C", repo_root, "push"],
        ):
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr).lower():
                return False, f"git step {cmd[2:4]} failed: {r.stderr[-200:]}"
        return True, f"runbook edited + committed: {runbook_path}"
    except Exception as e:
        return False, f"runbook tweak exception: {e}"


def _apply_manifest_env_inline(target: str, var_name: str, var_value: str) -> tuple[bool, str]:
    """Replace ${VAR} in the agent's manifest entry_command with a literal
    value. Walks all known agent dirs to find the manifest.json. Returns
    (success, detail)."""
    candidates = []
    for base in [
        "/home/voidsstr/development/nsc-assistant/agents",
        "/home/voidsstr/development/specpicks/agents",
        "/home/voidsstr/development/reusable-agents/agents",
    ]:
        # Direct id match
        candidates.append(Path(base) / target / "manifest.json")
        # Stripped site prefix
        parts = target.split("-")
        if len(parts) > 2:
            candidates.append(Path(base) / "-".join(parts[1:]) / "manifest.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            d = json.loads(path.read_text())
            if d.get("id") != target:
                continue
            cmd = d.get("entry_command", "")
            placeholder_a = "${" + var_name + "}"
            placeholder_b = "$" + var_name
            if placeholder_a in cmd:
                d["entry_command"] = cmd.replace(placeholder_a, var_value)
            elif placeholder_b in cmd and not (placeholder_b + "_") in cmd:
                d["entry_command"] = cmd.replace(placeholder_b, var_value)
            else:
                return False, f"entry_command doesn't contain {placeholder_a} or {placeholder_b}"
            path.write_text(json.dumps(d, indent=2) + "\n")
            return True, f"inlined {var_name} into {path}"
        except Exception as e:
            return False, f"manifest write failed at {path}: {e}"
    return False, f"manifest.json not found for {target} in known locations"


def recipe_llm_diagnose(target: str, ctx: dict) -> tuple[str, str]:
    """LLM-powered fallback for unknown signatures. Asks claude to read the
    failed agent's manifest, runbook, and recent log; returns a structured
    diagnosis. Auto-applies safe fix types (wait, manifest-env-inline with
    high confidence, data-issue); escalates everything else with the
    diagnosis attached so the operator gets actionable context instead of
    an opaque "unknown" escalation."""
    excerpt = ctx.get("excerpt", "")
    manifest = _read_agent_manifest_dict(target)
    runbook = _read_agent_runbook(target)
    print(f"[agent-doctor] LLM-investigating {target} "
          f"(runbook_chars={len(runbook)}, manifest={'yes' if manifest else 'no'})",
          file=sys.stderr)
    diag = _invoke_claude_diagnose(target, "unknown", excerpt, manifest, runbook)
    if diag.get("error"):
        return ("escalated",
                f"LLM diagnosis failed ({diag.get('error')}); falling back to opaque escalation")
    fix_type = diag.get("fix_type", "escalate")
    confidence = float(diag.get("confidence", 0.0))
    diagnosis = (diag.get("diagnosis", "") or "")[:400]
    fix_detail = (diag.get("fix_detail", "") or "")[:400]
    auto_apply = bool(diag.get("auto_apply", False))
    notes_hdr = f"diag={diagnosis} | fix={fix_type} (conf={confidence:.2f}): {fix_detail}"

    # Safe-to-auto-apply fix types
    if not auto_apply:
        return ("escalated", f"LLM (no auto-apply): {notes_hdr}")

    if fix_type == "wait":
        return ("no-op", f"LLM:wait → next cron retries. {notes_hdr}")

    if fix_type == "data-issue":
        return ("no-op", f"LLM:data-issue → bad input, agent should skip + recover. {notes_hdr}")

    if fix_type == "manifest-env-inline":
        # fix_detail should be like "DATABASE_URL=postgresql://..."
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.+)$", fix_detail.strip())
        if not m:
            return ("escalated",
                    f"LLM said manifest-env-inline but fix_detail wasn't 'VAR=value' shape: {notes_hdr}")
        var_name, var_value = m.group(1), m.group(2).strip().strip("'").strip('"')
        ok, det = _apply_manifest_env_inline(target, var_name, var_value)
        if ok:
            # Re-register so the new manifest's entry_command takes effect
            # in the running .service file (framework writes systemd units
            # off the registered manifest, not directly off disk).
            ok2, det2 = _re_register_agent(target)
            return ("fixed", f"LLM auto-fix: {det}; re-register={ok2} ({det2}). {notes_hdr}")
        return ("escalated", f"LLM proposed manifest-env-inline but apply failed: {det}. {notes_hdr}")

    if fix_type == "runbook-tweak":
        # Have claude edit the runbook to incorporate the fix. The doctor
        # commits + pushes the change automatically. Constrained: file must
        # be <16KB; resulting edit can't shrink the file >40%.
        ok, det = _apply_runbook_tweak(target, fix_detail, excerpt)
        if ok:
            return ("fixed", f"LLM auto-fix runbook: {det}. {notes_hdr}")
        return ("escalated", f"runbook-tweak attempt failed: {det}. {notes_hdr}")

    if fix_type == "stale-service-unit":
        # Manifest on disk is correct but the running .service file is stale
        # (manual edit + no re-register). Pull the manifest into the framework
        # registry, which rewrites the unit + restarts the timer.
        ok, det = _re_register_agent(target)
        if ok:
            return ("fixed", f"LLM auto-fix: {det}. {notes_hdr}")
        return ("escalated", f"re-register failed: {det}. {notes_hdr}")

    # Anything else with auto_apply=true is suspicious; escalate to be safe
    return ("escalated", f"LLM said auto_apply=true for fix_type={fix_type} (we don't auto-apply that): {notes_hdr}")


def recipe_missing_env(target: str, ctx: dict) -> tuple[str, str]:
    """When the agent's process env is missing a required var (e.g.
    `DATABASE_URL must be set`), check if the registered manifest's
    entry_command USES shell-syntax `${VAR}` substitution that systemd
    can't expand. If so, escalate with a clear pointer — operator must
    inline the value. Otherwise re-register the agent in case the .service
    file is stale."""
    excerpt = ctx.get("excerpt", "")
    # Extract the missing var name
    m = re.search(r"(\w+)\s+(?:is required|must be set|not in env)", excerpt)
    if not m:
        return ("escalated", "missing-env detected but couldn't parse var name")
    var = m.group(1)
    # Pull the manifest via API
    api = (os.environ.get("FRAMEWORK_API_URL") or "http://localhost:8093").rstrip("/")
    try:
        agents_list = _api_get(f"{api}/api/agents",
                                os.environ.get("FRAMEWORK_API_TOKEN", ""))
    except Exception as e:
        return ("escalated", f"could not fetch /api/agents: {e}")
    found = None
    for a in (agents_list or []):
        if a.get("id") == target:
            found = a
            break
    if not found:
        return ("escalated", f"agent {target} not found in registry")
    cmd = found.get("entry_command") or ""
    if not cmd:
        # Need to fetch the full agent detail (not in the list response)
        try:
            detail = _api_get(f"{api}/api/agents/{target}",
                               os.environ.get("FRAMEWORK_API_TOKEN", ""))
            cmd = (detail or {}).get("entry_command", "")
        except Exception:
            pass
    # If the entry command uses ${VAR} syntax that systemd doesn't expand,
    # that's the bug — escalate with the diagnosis.
    if f"${{{var}}}" in cmd or f"${var}" in cmd.replace("${", "$"):
        return ("escalated",
                f"manifest entry_command uses shell-syntax ${{{var}}} that "
                f"systemd Environment= does not expand. Inline the literal "
                f"value in the manifest's entry_command and re-register.")
    # Otherwise the manifest looks fine — service file may be stale.
    return ("escalated",
            f"agent process didn't see {var}; manifest looks correct so "
            f"check if the systemd service file is stale (re-register the agent)")


# Recipe lookup table
RECIPES: dict[str, callable] = {
    "oauth-token-expired": recipe_oauth_token_expired,
    "transient-network": recipe_transient_network,
    "stale-lock": recipe_stale_lock,
    "timeout": recipe_timeout,
}
# Wildcard prefix matching: any signature starting with `missing-env-` uses
# the same recipe. We register them dynamically in the run loop via a
# fallback lookup.
def _resolve_recipe(signature: str):
    if signature in RECIPES:
        return RECIPES[signature]
    if signature.startswith("missing-env-"):
        return recipe_missing_env
    # LLM-powered fallback for anything our pattern classifier didn't
    # recognize. Disable by setting AGENT_DOCTOR_USE_LLM=0 (e.g. when
    # claude-pool is fully rate-limited and you don't want investigations
    # to add load).
    if os.environ.get("AGENT_DOCTOR_USE_LLM", "1") != "0":
        return recipe_llm_diagnose
    return None


# ── Stuck-queue detection ─────────────────────────────────────────────────

STUCK_QUEUED_MIN_AGE_MIN = 30   # chain queued longer than this with no batch running

def _detect_stuck_queues(api: str, token: str) -> list[dict]:
    """Find dispatch chains that have been pending >STUCK_QUEUED_MIN_AGE_MIN
    minutes without any batch transitioning to running. These are the
    "auto-queue trigger written but implementer never picked it up" cases —
    e.g. tempdir cleanup race, missing route, dispatcher script crash on
    spawn. Returns a list of {chain, age_min} objects."""
    try:
        d = _api_get(f"{api}/api/implementer/batches?limit=20", token)
    except Exception as e:
        print(f"[agent-doctor] stuck-queue: API call failed: {e}", file=sys.stderr)
        return []
    chains = (d or {}).get("chains", []) if isinstance(d, dict) else []
    stuck = []
    now = datetime.now(timezone.utc)
    for ch in chains:
        if ch.get("chain_status") != "queued":
            continue
        # Any batch already started? Then it's running, not stuck.
        any_started = any(
            (b.get("status") in ("running", "completed", "paused"))
            or b.get("started_at")
            for b in ch.get("batches", [])
        )
        if any_started:
            continue
        mtime_iso = ch.get("mtime_iso", "")
        if not mtime_iso:
            continue
        try:
            mtime = datetime.fromisoformat(mtime_iso.replace("Z", "+00:00"))
        except Exception:
            continue
        age_min = (now - mtime).total_seconds() / 60.0
        if age_min >= STUCK_QUEUED_MIN_AGE_MIN:
            stuck.append({"chain": ch, "age_min": age_min})
    return stuck


def recipe_stuck_queue(target: str, ctx: dict) -> tuple[str, str]:
    """Re-write the auto-queue trigger so the responder re-dispatches the
    chain on its next tick. Idempotent — if the trigger file already
    exists, we overwrite with a fresh timestamp."""
    chain = ctx.get("chain") or {}
    site = chain.get("site", "")
    source_agent = chain.get("source_agent", "")
    source_run_ts = chain.get("source_run_ts", "") or ""
    if not (site and source_agent and source_run_ts):
        return ("escalated", f"chain missing site/agent/run_ts: {chain}")
    # Strip the rundir-...- prefix that Azure-materialized run_ts has
    if source_run_ts.startswith("rundir-"):
        import re as _re
        m = _re.search(r"(\d{8}T\d{6}Z)", source_run_ts)
        if m:
            source_run_ts = m.group(1)
    s = get_storage()
    # Read rec_ids from the source run's recommendations.json
    src_path = f"agents/{source_agent}/runs/{source_run_ts}/recommendations.json"
    try:
        rd = s.read_json(src_path) or {}
    except Exception as e:
        return ("escalated", f"could not read {src_path}: {e}")
    rec_ids = [r.get("id") for r in rd.get("recommendations", []) if r.get("id")]
    if not rec_ids:
        return ("escalated", f"no recs in {src_path}")
    request_id = f"r-{source_run_ts}-seo-{site}"
    payload = {
        "schema_version": "1",
        "request_id": request_id,
        "source_agent": source_agent,
        "site": site,
        "run_ts": source_run_ts,
        "rec_ids": rec_ids,
        "action": "implement",
        "queued_at": _now_iso(),
        "source": "agent-doctor:recipe_stuck_queue",
    }
    try:
        s.write_json(f"agents/responder-agent/auto-queue/{request_id}.json", payload)
    except Exception as e:
        return ("escalated", f"auto-queue write failed: {e}")
    return ("fixed",
            f"re-wrote auto-queue trigger for {site}/{source_run_ts} "
            f"({len(rec_ids)} recs) — responder will pick up within 60s")


RECIPES["stuck-queue"] = recipe_stuck_queue


# ── Main agent ────────────────────────────────────────────────────────────

class AgentDoctor(AgentBase):
    agent_id = AGENT_ID
    # Suppress the framework's automatic post-run summary email — this
    # agent only emails when something actionable happens (a fixed issue
    # that recurred, or an unfixable signature). Run-by-run status
    # updates aren't actionable for the operator.
    send_run_summary_email = False

    def _api_token(self) -> str:
        tok = os.environ.get("FRAMEWORK_API_TOKEN") or ""
        if tok:
            return tok
        # Fallback: read from framework .env on disk
        env_file = _REPO_ROOT / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                if line.startswith("FRAMEWORK_API_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""

    def _api_url(self) -> str:
        return os.environ.get("FRAMEWORK_API_URL") or "http://localhost:8093"

    def _state_path(self) -> str:
        return f"agents/{AGENT_ID}/state/seen.json"

    def run(self) -> RunResult:
        s = get_storage()
        token = self._api_token()
        api = self._api_url()
        if not token:
            self.status("FRAMEWORK_API_TOKEN not set; cannot poll API",
                        progress=1.0, state="failure")
            return RunResult(status="failure", summary="missing FRAMEWORK_API_TOKEN")

        # Load "seen" state — agent_id → last_run_at we already investigated
        try:
            seen = s.read_json(self._state_path()) or {}
        except Exception:
            seen = {}

        # Load weekly-stats counters (used for digest cadence)
        weekly = _load_weekly_stats(s)

        try:
            agents = _api_get(f"{api}/api/agents", token)
        except Exception as e:
            self.status(f"API list failed: {e}", state="failure")
            return RunResult(status="failure", summary=f"api: {e}")

        if not isinstance(agents, list):
            return RunResult(status="failure", summary="unexpected /api/agents shape")

        investigated = 0
        fixed = 0
        escalated = 0
        skipped_seen = 0
        for a in agents:
            aid = a.get("id", "")
            if aid in (AGENT_ID, ""):
                continue
            last_status = a.get("last_run_status", "")
            last_at = a.get("last_run_at") or ""
            if last_status not in ("failure", "running"):
                continue

            # Stuck detection: running with stale updated_at
            is_stuck = False
            if last_status == "running" and last_at:
                try:
                    started = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
                    age_min = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
                    if age_min > STUCK_FALLBACK_MINUTES:
                        is_stuck = True
                except Exception:
                    pass
            if last_status != "failure" and not is_stuck:
                continue

            # Dedupe — but ONLY if we have a fixes-log entry for this exact
            # (agent, last_at) tuple. seen.json alone isn't enough: a prior
            # tick may have populated `seen` without writing fixes-log
            # (e.g. a transient framework bug or empty-log skip). When that
            # happens, the agent gets stuck "permanently dedupe'd" and is
            # never investigated. Verify the fixes-log has actual evidence.
            seen_key = f"{last_status}@{last_at}"
            if seen.get(aid) == seen_key:
                if _has_fixes_log_entry_for(s, aid, last_at):
                    skipped_seen += 1
                    continue
                # else: seen but no log entry — fall through and re-investigate

            self.status(f"investigating {aid} ({last_status})",
                        current_action=f"target={aid}")
            investigated += 1
            log_text = _read_log(aid)
            try:
                status_obj = _api_get(f"{api}/api/agents/{aid}/status", token) or {}
            except Exception:
                status_obj = {}
            signature, excerpt = _classify_error(log_text, status_obj)

            # Loop-prevention: too many failed attempts at this signature.
            # Three exit conditions to consider:
            #
            # 1. LLM has previously classified this as non-actionable
            #    (data-issue / external-blocker / wait). The operator
            #    already got that diagnosis email. Don't keep re-emailing
            #    the same conclusion every 5 min — silently accept the
            #    state and move on.
            #
            # 2. Retry cap hit AND LLM has been tried at least once →
            #    escalate as "loop-broken" so a human can intervene.
            #
            # 3. Otherwise → run the recipe (LLM if pattern unknown).
            attempt_count = _signature_attempt_count(s, aid, signature)
            llm_already_tried = _has_llm_attempt_for(s, aid, signature)
            non_actionable, prior_diag = _llm_says_non_actionable(s, aid, signature)

            if non_actionable:
                outcome = "skipped"
                notes = (
                    f"LLM previously classified as non-actionable "
                    f"({prior_diag[:120]}); silently accepting state."
                )
                # Log to fixes-log but DO NOT email — operator already
                # got the diagnosis from the first LLM run.
            elif attempt_count >= MAX_RETRIES_PER_SIGNATURE and llm_already_tried:
                # Suppress the email when we've ALREADY escalated this
                # exact signature+log within the last 24h. The operator
                # got the diagnosis once; sending the same loop-broken
                # email every 5 minutes is just noise. Still log to
                # fixes-log so the audit trail is intact.
                if _recent_escalation_with_same_diagnosis(s, aid, signature, excerpt):
                    outcome = "skipped"
                    notes = (f"signature attempted {attempt_count}× incl. LLM; "
                             f"identical log already escalated within 24h — suppressing duplicate email")
                else:
                    outcome, notes = ("escalated",
                                      f"signature attempted {attempt_count}× incl. LLM — escalating to operator")
                    _email_operator(aid, signature, excerpt, a, recipe="loop-broken")
                    escalated += 1
            else:
                # Apply recipe if known (supports wildcard prefix match)
                recipe = _resolve_recipe(signature)
                if recipe:
                    try:
                        outcome, notes = recipe(aid, {"agent": a, "excerpt": excerpt})
                    except Exception as e:
                        outcome, notes = ("escalated", f"recipe raised: {e}")
                else:
                    outcome, notes = ("escalated",
                                      f"no recipe for signature={signature!r}; escalating")
                if outcome == "fixed":
                    fixed += 1
                elif outcome == "escalated":
                    # Prefix the email log_excerpt with the recipe's notes
                    # (especially the LLM diagnosis) so the operator sees
                    # actionable context — not just the raw stderr tail.
                    enriched = (
                        f"=== AGENT-DOCTOR ANALYSIS ===\n{notes[:1000]}\n\n"
                        f"=== RAW LOG EXCERPT ===\n{excerpt}"
                    ) if notes else excerpt
                    _email_operator(aid, signature, enriched, a,
                                     recipe=signature if recipe else "(none)")
                    escalated += 1

            # Append to durable fixes log
            entry = {
                "ts": _now_iso(),
                "doctor_run_ts": self.run_ts,
                "target_agent": aid,
                "trigger": "stuck" if is_stuck else "failure",
                "last_run_at": last_at,
                "error_signature": signature,
                "recipe_applied": (signature if signature in RECIPES
                                    else ("missing-env-*" if signature.startswith("missing-env-") else "none")),
                "outcome": outcome,
                "notes": notes,
                "log_excerpt": excerpt[:600],
                "attempt_n": attempt_count + 1,
            }
            try:
                s.append_jsonl(f"agents/{AGENT_ID}/fixes-log.jsonl", entry)
            except Exception as e:
                self.decide("log-append-failed", str(e))

            self.decide(
                f"{aid}:{signature}:{outcome}",
                notes[:200],
            )
            seen[aid] = seen_key

            # Update digest counters
            weekly["investigations"] = weekly.get("investigations", 0) + 1
            if outcome == "fixed":
                weekly["fixes_succeeded"] = weekly.get("fixes_succeeded", 0) + 1
                weekly["fixes_attempted"] = weekly.get("fixes_attempted", 0) + 1
            elif outcome == "escalated":
                weekly["escalations"] = weekly.get("escalations", 0) + 1
                weekly["fixes_attempted"] = weekly.get("fixes_attempted", 0) + 1
            elif outcome == "no-op":
                weekly["no_ops"] = weekly.get("no_ops", 0) + 1
            weekly["signatures"][signature] = weekly["signatures"].get(signature, 0) + 1
            weekly["targets"][aid] = weekly["targets"].get(aid, 0) + 1
            weekly["outcomes"][outcome] = weekly["outcomes"].get(outcome, 0) + 1

        # Persist seen state
        try:
            s.write_json(self._state_path(), seen)
        except Exception:
            pass

        # ── Stuck-queue sweep (separate from agent-status detection) ──
        # Even when no agent has last_run_status=failure, the implementer
        # queue can have chains that were dispatched but never picked up
        # (tempdir cleanup race, dispatcher crash on spawn, missing route).
        # Detect + auto-redispatch.
        try:
            stuck = _detect_stuck_queues(api, token)
        except Exception as e:
            print(f"[agent-doctor] stuck-queue sweep failed: {e}", file=sys.stderr)
            stuck = []
        for sq in stuck:
            ch = sq["chain"]
            chain_id = ch.get("dispatch_run_ts") or ch.get("run_dir_basename") or ""
            sigkey = f"stuck-queue@{chain_id}"
            # Loop-prevention via fixes-log
            attempt_count = _signature_attempt_count(s, "implementer", sigkey)
            if attempt_count >= MAX_RETRIES_PER_SIGNATURE:
                outcome, notes = ("escalated",
                                  f"chain {chain_id} re-dispatched {attempt_count}× without progress — escalating")
                _email_operator(
                    "implementer", sigkey,
                    f"chain={chain_id} site={ch.get('site')} age_min={int(sq['age_min'])}",
                    {"last_run_at": ch.get("mtime_iso", ""), "last_run_status": "queued",
                     "cron_expr": ""},
                    recipe="loop-broken",
                )
                escalated += 1
            else:
                try:
                    outcome, notes = recipe_stuck_queue("implementer", {"chain": ch})
                except Exception as e:
                    outcome, notes = ("escalated", f"recipe raised: {e}")
                if outcome == "fixed":
                    fixed += 1
                else:
                    _email_operator(
                        "implementer", sigkey,
                        f"chain={chain_id} site={ch.get('site')} age_min={int(sq['age_min'])} notes={notes}",
                        {"last_run_at": ch.get("mtime_iso", ""), "last_run_status": "queued",
                         "cron_expr": ""},
                        recipe="stuck-queue",
                    )
                    escalated += 1
            investigated += 1
            entry = {
                "ts": _now_iso(),
                "doctor_run_ts": self.run_ts,
                "target_agent": "implementer",
                "trigger": "stuck-queue",
                "last_run_at": ch.get("mtime_iso", ""),
                "error_signature": sigkey,
                "recipe_applied": "stuck-queue",
                "outcome": outcome,
                "notes": notes,
                "log_excerpt": f"chain={chain_id} site={ch.get('site')} age_min={int(sq['age_min'])}",
                "attempt_n": attempt_count + 1,
            }
            try:
                s.append_jsonl(f"agents/{AGENT_ID}/fixes-log.jsonl", entry)
            except Exception as e:
                self.decide("log-append-failed", str(e))
            self.decide(f"implementer:stuck-queue:{outcome}", notes[:200])
            # Update digest counters
            weekly["investigations"] = weekly.get("investigations", 0) + 1
            if outcome == "fixed":
                weekly["fixes_succeeded"] = weekly.get("fixes_succeeded", 0) + 1
                weekly["fixes_attempted"] = weekly.get("fixes_attempted", 0) + 1
            elif outcome == "escalated":
                weekly["escalations"] = weekly.get("escalations", 0) + 1
                weekly["fixes_attempted"] = weekly.get("fixes_attempted", 0) + 1
            weekly["signatures"]["stuck-queue"] = weekly["signatures"].get("stuck-queue", 0) + 1
            weekly["targets"]["implementer"] = weekly["targets"].get("implementer", 0) + 1
            weekly["outcomes"][outcome] = weekly["outcomes"].get(outcome, 0) + 1

        # Bump tick + agents-checked counters and persist; maybe send digest
        weekly["ticks"] = weekly.get("ticks", 0) + 1
        weekly["agents_checked_total"] = weekly.get("agents_checked_total", 0) + len(agents)
        if _maybe_send_weekly_digest(s, weekly):
            # Reset for the new window — start counters at 0
            weekly = _empty_stats()
            self.decide("weekly-digest-sent", "summary email dispatched; counters reset")
        _save_weekly_stats(s, weekly)

        summary = (f"{investigated} investigated · {fixed} fixed · "
                   f"{escalated} escalated · {skipped_seen} already-seen")
        self.status(summary, progress=1.0, state="idle")
        return RunResult(
            status="success",
            summary=summary,
            metrics={
                "investigated": investigated,
                "fixed": fixed,
                "escalated": escalated,
                "skipped_already_seen": skipped_seen,
                "agents_total": len(agents),
            },
        )


if __name__ == "__main__":
    AgentDoctor().run_once()
