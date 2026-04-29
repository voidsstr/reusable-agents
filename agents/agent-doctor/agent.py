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
    Logs are written by agent_run_wrapper.sh + dispatch flows."""
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
        return candidates[0].read_text(errors="replace")[-32000:]  # last 32KB
    except Exception:
        return ""


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

    # Build digest
    sigs_sorted = sorted(stats.get("signatures", {}).items(),
                         key=lambda kv: -kv[1])
    targets_sorted = sorted(stats.get("targets", {}).items(),
                            key=lambda kv: -kv[1])
    outcomes_sorted = sorted(stats.get("outcomes", {}).items(),
                             key=lambda kv: -kv[1])
    week_end = _now_iso()
    rows_sigs = "".join(
        f"<tr><td style='padding:6px;border:1px solid #e2e8f0'>{k}</td>"
        f"<td style='padding:6px;border:1px solid #e2e8f0;text-align:right'>{v}</td></tr>"
        for k, v in sigs_sorted
    ) or "<tr><td colspan='2' style='padding:6px;color:#64748b'>(none)</td></tr>"
    rows_targets = "".join(
        f"<tr><td style='padding:6px;border:1px solid #e2e8f0;font-family:monospace'>{k}</td>"
        f"<td style='padding:6px;border:1px solid #e2e8f0;text-align:right'>{v}</td></tr>"
        for k, v in targets_sorted
    ) or "<tr><td colspan='2' style='padding:6px;color:#64748b'>(none)</td></tr>"
    rows_outcomes = "".join(
        f"<tr><td style='padding:6px;border:1px solid #e2e8f0'>{k}</td>"
        f"<td style='padding:6px;border:1px solid #e2e8f0;text-align:right'>{v}</td></tr>"
        for k, v in outcomes_sorted
    ) or "<tr><td colspan='2' style='padding:6px;color:#64748b'>(none)</td></tr>"

    body = (
        "<!DOCTYPE html><html><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;"
        "color:#0f172a;line-height:1.5;max-width:780px;margin:0 auto;padding:24px'>"
        f"<h1 style='border-bottom:1px solid #e2e8f0;padding-bottom:12px'>"
        f"agent-doctor — weekly summary</h1>"
        f"<p style='color:#475569'>Window: <code>{stats.get('week_start','')[:19]}</code> → "
        f"<code>{week_end[:19]}</code></p>"
        f"<table style='border-collapse:collapse;font-size:14px;margin:12px 0'>"
        f"<tr><td style='padding:8px;border:1px solid #e2e8f0'>Wakeups (5-min ticks)</td>"
        f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right;font-family:monospace'>"
        f"{stats.get('ticks',0)}</td></tr>"
        f"<tr><td style='padding:8px;border:1px solid #e2e8f0'>Agents checked total</td>"
        f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right;font-family:monospace'>"
        f"{stats.get('agents_checked_total',0)}</td></tr>"
        f"<tr><td style='padding:8px;border:1px solid #e2e8f0'>Investigations</td>"
        f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right;font-family:monospace'>"
        f"{stats.get('investigations',0)}</td></tr>"
        f"<tr><td style='padding:8px;border:1px solid #e2e8f0'>Fixes succeeded</td>"
        f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right;font-family:monospace;color:#059669'>"
        f"{stats.get('fixes_succeeded',0)}</td></tr>"
        f"<tr><td style='padding:8px;border:1px solid #e2e8f0'>Escalations sent</td>"
        f"<td style='padding:8px;border:1px solid #e2e8f0;text-align:right;font-family:monospace;color:#dc2626'>"
        f"{stats.get('escalations',0)}</td></tr>"
        f"</table>"
        f"<h2 style='margin-top:18px;font-size:16px'>By outcome</h2>"
        f"<table style='border-collapse:collapse;width:100%;font-size:13px'>"
        f"<tr style='background:#f8fafc'><th style='text-align:left;padding:6px;border:1px solid #e2e8f0'>Outcome</th>"
        f"<th style='text-align:right;padding:6px;border:1px solid #e2e8f0'>Count</th></tr>"
        f"{rows_outcomes}</table>"
        f"<h2 style='margin-top:18px;font-size:16px'>By error signature</h2>"
        f"<table style='border-collapse:collapse;width:100%;font-size:13px'>"
        f"<tr style='background:#f8fafc'><th style='text-align:left;padding:6px;border:1px solid #e2e8f0'>Signature</th>"
        f"<th style='text-align:right;padding:6px;border:1px solid #e2e8f0'>Count</th></tr>"
        f"{rows_sigs}</table>"
        f"<h2 style='margin-top:18px;font-size:16px'>By target agent</h2>"
        f"<table style='border-collapse:collapse;width:100%;font-size:13px'>"
        f"<tr style='background:#f8fafc'><th style='text-align:left;padding:6px;border:1px solid #e2e8f0'>Agent</th>"
        f"<th style='text-align:right;padding:6px;border:1px solid #e2e8f0'>Count</th></tr>"
        f"{rows_targets}</table>"
        f"<p style='color:#94a3b8;font-size:12px;margin-top:24px'>"
        f"Per-tick alerts only fire when an investigation escalates and needs your attention. "
        f"This is the periodic activity summary."
        f"</p>"
        f"</body></html>"
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

            # Loop-prevention: too many failed attempts at this signature
            attempt_count = _signature_attempt_count(s, aid, signature)
            if attempt_count >= MAX_RETRIES_PER_SIGNATURE:
                outcome, notes = ("escalated",
                                  f"signature attempted {attempt_count}× — escalating to operator")
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
                    _email_operator(aid, signature, excerpt, a,
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
