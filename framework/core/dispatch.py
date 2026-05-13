"""Direct-dispatch helper for sending recommendations to the implementer.

Replaces the responder-agent's drain_auto_queue path. Producer agents now
call `dispatch_now()` at the end of their run instead of writing to
`agents/responder-agent/auto-queue/<id>.json`. The responder is now
strictly the IMAP-reply path (user confirmations) and runs on demand.

Architecture:
    Producer agent → dispatch_now() → site lock → systemd-run scope
                                                  → implementer
                                                  → deployer (per site)

Resilience:
  - Retries on transient failures (script-not-found, permission errors,
    systemd-run process spawn failures) up to MAX_RETRIES with exponential
    backoff.
  - On permanent failure: emails the operator (configurable) AND
    optionally falls back to writing the auto-queue file so the
    responder can retry on its next IMAP-only run. The fallback is
    transitional — once we trust direct dispatch, it'll be removed.
  - Site lock (framework.core.locks.site_dispatch_lock) ensures at most
    one implementer dispatch per site at a time. Different sites run
    in parallel.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .locks import site_dispatch_lock
from .storage import get_storage


# Configuration ----------------------------------------------------------

MAX_RETRIES = int(os.environ.get("FRAMEWORK_DISPATCH_RETRIES", "3"))
RETRY_BASE_DELAY_S = int(os.environ.get("FRAMEWORK_DISPATCH_RETRY_DELAY_S", "5"))
NOTIFY_EMAIL = os.environ.get(
    "FRAMEWORK_DISPATCH_NOTIFY_EMAIL",
    os.environ.get("OPERATOR_EMAIL", ""),
)
LOG_DIR = Path(os.environ.get(
    "FRAMEWORK_DISPATCH_LOG_DIR", "/tmp/reusable-agents-logs"
))
LOG_DIR.mkdir(parents=True, exist_ok=True)


# Errors -----------------------------------------------------------------

class DispatchError(Exception):
    """Permanent dispatch failure after retries exhausted."""


class DispatchTransient(Exception):
    """Transient — retryable."""


# Result -----------------------------------------------------------------

class DispatchHandle:
    """Returned by dispatch_now. Doesn't block on the implementer; the
    systemd-run scope outlives this process. Use .pid + .log_path for
    follow-up debugging."""

    def __init__(self, *, pid: int, unit: str, log_path: Path,
                 site: str, agent_id: str, rec_ids: list[str],
                 fell_back_to_queue: bool = False,
                 request_id: str = ""):
        self.pid = pid
        self.unit = unit
        self.log_path = log_path
        self.site = site
        self.agent_id = agent_id
        self.rec_ids = list(rec_ids)
        self.fell_back_to_queue = fell_back_to_queue
        # Stable per-dispatch id used by the source agent to attribute
        # downstream rec lifecycle events back to this dispatch. The
        # finalizer logs this in its decision trail; the responder also
        # echoes it on email-reply confirmations.
        self.request_id = request_id

    def __repr__(self) -> str:
        return (
            f"<DispatchHandle site={self.site} agent={self.agent_id} "
            f"unit={self.unit} pid={self.pid} fallback={self.fell_back_to_queue}>"
        )


# Public API -------------------------------------------------------------

def gated_dispatch_now(
    *,
    cfg: Optional[dict] = None,
    agent: Optional["object"] = None,  # noqa: F821 — AgentBase, avoid circular import
    agent_id: str,
    run_dir: str,
    rec_ids: list[str],
    action: str = "implement",
    site: str = "",
    subject_tag: str = "seo",
    request_id: Optional[str] = None,
    **dispatch_kwargs,
) -> Optional["DispatchHandle"]:
    """Single source of truth for "auto-ship vs email-approval" per agent.

    Behavior is controlled by ONE flag in the agent's site.yaml:

        auto_implement: true   # auto-ship via dispatch_now() (default)
        auto_implement: false  # don't dispatch — email is the proposal,
                               # responder dispatches on user reply
                               # (`ship rec-NNN` / `implement rec-NNN`)

    Producers call this INSTEAD of dispatch_now() and stop carrying their
    own "if auto_implement: dispatch else email-only" branching. Comp-
    research, which used to be hardcoded email-only, now relies on the
    yaml flag like everyone else.

    Defaults to TRUE (auto-ship) when the flag is missing — preserves
    pre-2026-05-12 behavior for agents that never had a site.yaml.

    Args mirror dispatch_now() except for the leading `cfg` (site config
    dict) and `agent` (optional AgentBase instance for self.decide()
    logging). Pass either or both; cfg is what drives the gate decision.

    Returns:
      * DispatchHandle when auto_implement=true and the dispatch fired
      * None when auto_implement=false (the email path takes over)
    """
    auto_implement = True
    if cfg is not None and hasattr(cfg, "get"):
        # Works for both plain dict AND SiteQualityConfig wrapper.
        # Defaults to True so agents without a site.yaml flag (legacy)
        # keep their pre-gate behavior (auto-ship).
        auto_implement = bool(cfg.get("auto_implement", True))

    if not auto_implement:
        # Email-approval path. The producer's own run() should have
        # already sent the proposal email; we just log the decision so
        # the dashboard's run history reflects "did not auto-dispatch".
        if agent is not None and hasattr(agent, "decide"):
            try:
                agent.decide(
                    "observation",
                    f"auto_implement=false — {len(rec_ids)} rec(s) "
                    f"awaiting email approval. Reply `ship rec-NNN` or "
                    f"`implement rec-NNN` to dispatch.",
                    evidence={"rec_ids": list(rec_ids),
                                "gate": "auto_implement",
                                "value": False},
                )
            except Exception:
                pass
        return None

    return dispatch_now(
        agent_id=agent_id,
        run_dir=run_dir,
        rec_ids=rec_ids,
        action=action,
        site=site,
        subject_tag=subject_tag,
        request_id=request_id,
        **dispatch_kwargs,
    )


def dispatch_now(
    *,
    agent_id: str,
    run_dir: str,
    rec_ids: list[str],
    action: str = "implement",
    site: str = "",
    subject_tag: str = "seo",
    implementer_script: Optional[str] = None,
    batch_index: int = 1,
    batch_total: int = 1,
    request_id: Optional[str] = None,
    max_retries: int = MAX_RETRIES,
    fallback_to_queue: bool = True,
    notify_on_failure: bool = True,
    lock_timeout_s: int = 1800,
) -> DispatchHandle:
    """Dispatch a list of rec ids to the implementer.

    Resilience:
      - Acquires a SITE-LEVEL lock. Two dispatches against the same site
        queue up; different sites run in parallel.
      - Retries up to `max_retries` on transient failures with backoff.
      - On permanent failure:
          - Writes to agents/responder-agent/auto-queue/<id>.json (so
            responder can retry) iff `fallback_to_queue=True`.
          - Emails the operator iff `notify_on_failure=True`.
          - Raises DispatchError.

    Returns a DispatchHandle when the systemd-run scope spawned successfully.

    `subject_tag` selects the implementer runbook (seo / article / h2h /
    catalog-audit). The implementer's run.sh switches based on it.
    """
    request_id = request_id or _make_request_id(site)
    if not rec_ids:
        raise DispatchError("no rec_ids provided")
    if not Path(run_dir).is_dir():
        raise DispatchError(f"run_dir does not exist: {run_dir}")

    script = implementer_script or _resolve_implementer_script()
    if not script or not Path(script).is_file():
        raise DispatchError(f"implementer script not found at {script}")

    # The minimal dispatch-batches.json is written by _spawn_implementer
    # AFTER the run_dir is copied to the persistent dispatch-rundir.
    # Writing it here would land it in the caller's tempdir which gets
    # rmtree'd when the caller's RunDir context manager exits — racing
    # against the implementer (now decoupled by the persistent copy)
    # AND against the queue endpoint (which scans Azure blob storage,
    # so anything written only to the local tempdir is invisible).

    last_err: Optional[Exception] = None

    # Parallelism gate: only app-code dispatch kinds serialize per site
    # (one frontend/backend deploy at a time). DB-only kinds (catalog-
    # audit / h2h) and content-only kinds (article-author) bypass the
    # site lock so a deploy doesn't block a DB migration or article
    # insert. Override by setting DATA_ONLY_KINDS / APP_CODE_KINDS env.
    _data_only = set((os.environ.get("DATA_ONLY_KINDS") or
                      "catalog-audit,h2h,article-author,product-hydration"
                      ).split(","))
    _data_only.discard("")
    needs_site_lock = (subject_tag or "").strip() not in _data_only

    @contextmanager
    def _maybe_site_lock():
        if needs_site_lock:
            with site_dispatch_lock(site or "shared", timeout_s=lock_timeout_s) as _l:
                yield _l
        else:
            _log(f"[dispatch] kind={subject_tag!r} is data-only — bypassing site lock for {site}")
            yield None

    with _maybe_site_lock():
        for attempt in range(1, max_retries + 1):
            try:
                handle = _spawn_implementer(
                    agent_id=agent_id,
                    run_dir=run_dir,
                    rec_ids=rec_ids,
                    action=action,
                    site=site,
                    subject_tag=subject_tag,
                    request_id=request_id,
                    batch_index=batch_index,
                    batch_total=batch_total,
                    script=script,
                )
                return handle
            except DispatchTransient as e:
                last_err = e
                _log(f"[dispatch] transient on attempt {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    time.sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))
                continue
            except DispatchError:
                raise

    # All retries exhausted — handle fallback + notification
    msg = f"dispatch_now failed after {max_retries} attempts: {last_err}"
    _log(f"[dispatch] PERMANENT FAILURE: {msg}")

    if fallback_to_queue:
        try:
            _write_auto_queue_fallback(
                agent_id=agent_id, run_dir=run_dir, rec_ids=rec_ids,
                action=action, site=site, subject_tag=subject_tag,
                request_id=request_id,
            )
            _log(f"[dispatch] fell back to auto-queue for {request_id}")
            fallback_ok = True
        except Exception as e:
            _log(f"[dispatch] auto-queue fallback ALSO failed: {e}")
            fallback_ok = False
    else:
        fallback_ok = False

    if notify_on_failure:
        try:
            _notify_dispatch_failure(
                agent_id=agent_id, request_id=request_id,
                rec_ids=rec_ids, error=str(last_err),
                fell_back_to_queue=fallback_ok,
            )
        except Exception as e:
            _log(f"[dispatch] notify failed: {e}")

    if fallback_ok:
        return DispatchHandle(pid=0, unit="", log_path=LOG_DIR,
                              site=site, agent_id=agent_id, rec_ids=rec_ids,
                              fell_back_to_queue=True,
                              request_id=request_id)
    raise DispatchError(msg)


# Internals --------------------------------------------------------------

def _spawn_implementer(*, agent_id: str, run_dir: str, rec_ids: list[str],
                       action: str, site: str, subject_tag: str,
                       request_id: str, batch_index: int, batch_total: int,
                       script: str) -> DispatchHandle:
    """Spawn the implementer in a detached systemd-run --scope.
    Returns DispatchHandle on successful spawn. Raises DispatchTransient
    on retryable failures.

    Run-dir lifecycle: callers like the SEO opportunity agent dispatch
    while still inside their own RunDir context manager — when the
    agent exits, that context manager runs `shutil.rmtree(run_dir)`,
    which races against the just-spawned (and detached) implementer.
    Symptom: implementer crashes mid-run with FileNotFoundError on
    something like `<run_dir>/handoffs-sent.json` because the parent
    dir vanished. We defend by copying the inputs to a separate,
    persistent dispatch-rundir under LOG_DIR — that copy outlives the
    caller's tempdir, the implementer owns its lifecycle, and we keep
    it on disk for debugging just like the log file.
    """
    env = os.environ.copy()
    # Honor any KEY=VAL prefix the implementer's manifest puts on its
    # entry_command — `python3 agent.py` is invoked DIRECTLY by
    # systemd-run, so a shell-style env prefix (e.g.
    # IMPLEMENTER_ALLOW_REC_TYPES=foo,bar python3 agent.py) is lost
    # unless we parse it here.
    try:
        from . import registry as _reg
        impl_manifest = _reg.get_agent("implementer")
        if impl_manifest and impl_manifest.entry_command:
            for tok in impl_manifest.entry_command.split():
                if "=" in tok and tok[0].isalpha() and not tok.startswith(
                    ("/", "python", "bash", "sh")
                ):
                    k, v = tok.split("=", 1)
                    # Caller-set env wins (e.g. test overrides via
                    # `os.environ['IMPLEMENTER_ALLOW_REC_TYPES'] = ...`)
                    if k not in env:
                        env[k] = v
                else:
                    # Stop at the first non-KEY=VAL token (the
                    # interpreter or script path).
                    break
    except Exception:
        pass
    env["RESPONDER_ACTION"] = action
    env["RESPONDER_REC_IDS"] = ",".join(rec_ids)
    env["RESPONDER_SITE"] = site
    env["RESPONDER_RUN_TS"] = _extract_run_ts(run_dir) or ""
    # Decouple the implementer's run-dir from the caller's tempdir so
    # the caller's RunDir.__exit__ shutil.rmtree doesn't pull files out
    # from under us. Each dispatch gets its own persistent copy.
    try:
        import tempfile, shutil as _shutil
        src_run_ts = _extract_run_ts(run_dir) or "unknown"
        dispatch_rundir_root = LOG_DIR / "dispatch-rundirs"
        dispatch_rundir_root.mkdir(parents=True, exist_ok=True)
        persisted = Path(tempfile.mkdtemp(
            prefix=f"rundir-{agent_id}-{src_run_ts}-",
            dir=str(dispatch_rundir_root),
        ))
        _shutil.copytree(run_dir, persisted, dirs_exist_ok=True)
        run_dir = str(persisted)
        _log(f"[dispatch] persisted run_dir copy → {run_dir}")
    except Exception as _e:
        # Copy is best-effort — if it fails (disk full, permission), fall
        # back to the original tempdir and accept the race risk. Logging
        # so an operator can see why we degraded.
        _log(f"[dispatch] run_dir copy failed ({_e}); using caller's "
             f"tempdir directly — implementer may race with caller cleanup")
    # Write the minimal dispatch-batches.json into the persistent rundir
    # so the dashboard's Queue page (/api/implementer/batches → list of
    # blobs ending in /dispatch-batches.json) discovers this dispatch.
    # Done AFTER the copy so the file lands in the right rundir; doing
    # it before would write to the caller's tempdir which gets cleaned
    # up immediately after _spawn_implementer returns.
    try:
        _write_minimal_dispatch_manifest(
            run_dir=run_dir, rec_ids=rec_ids,
            request_id=request_id, agent_id=agent_id,
        )
    except Exception as _e:
        _log(f"[dispatch] failed to write dispatch-batches.json: {_e}")
    env["RESPONDER_RUN_DIR"] = str(run_dir)
    env["RESPONDER_REQUEST_ID"] = request_id
    env["RESPONDER_AGENT_ID"] = agent_id
    env["RESPONDER_SOURCE_AGENT"] = agent_id
    env["RESPONDER_SUBJECT_TAG"] = subject_tag
    if batch_total:
        env["RESPONDER_BATCH_INDEX"] = str(batch_index)
        env["RESPONDER_BATCH_TOTAL"] = str(batch_total)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_DIR / f"dispatch-implementer-{site or 'shared'}-{ts}.log"
    env["DISPATCH_LOG_PATH"] = str(log_path)
    # Stable run_ts for the implementer's own progress.json + run-index
    # entry (so each dispatch shows up as a row in the dashboard's
    # implementer Runs tab). Same value as the systemd unit suffix —
    # makes log → run mapping trivial.
    env["IMPLEMENTER_RUN_TS"] = ts

    use_systemd_run = bool(shutil.which("systemd-run"))
    try:
        log_f = open(log_path, "ab")
    except OSError as e:
        raise DispatchTransient(f"cannot open log {log_path}: {e}")

    # Pick interpreter from the script's extension. Default path is now the
    # python AgentBase entry point (agent.py) so the dispatched run flips
    # the implementer's status.json to running/success and shows up in the
    # dashboard's Runs tab + "Working now" hero. Fallback to bash for
    # legacy callers that still hand us run.sh directly.
    is_python = script.endswith(".py")
    interp = [sys.executable] if is_python else ["bash"]

    try:
        if use_systemd_run:
            unit_name = f"agent-dispatch-implementer-{site or 'shared'}-{ts}"
            cmd = [
                "systemd-run", "--user", "--scope", "--collect",
                f"--unit={unit_name}",
                "--property=KillMode=process",
                "--property=TimeoutStopSec=0",
                *interp, script,
            ]
            try:
                proc = subprocess.Popen(
                    cmd, env=env, start_new_session=True,
                    stdout=log_f, stderr=log_f,
                )
            except (OSError, FileNotFoundError) as e:
                raise DispatchTransient(f"systemd-run spawn failed: {e}")
            _log(
                f"[dispatch] spawned-via-systemd-run unit={unit_name} "
                f"pid={proc.pid} action={action} recs={rec_ids} log={log_path}"
            )
            return DispatchHandle(
                pid=proc.pid, unit=unit_name, log_path=log_path,
                site=site, agent_id=agent_id, rec_ids=rec_ids,
                request_id=request_id,
            )
        else:
            try:
                proc = subprocess.Popen(
                    [*interp, script], env=env, start_new_session=True,
                    stdout=log_f, stderr=log_f,
                )
            except (OSError, FileNotFoundError) as e:
                raise DispatchTransient(f"{interp[0]} spawn failed: {e}")
            _log(
                f"[dispatch] spawned {script} pid={proc.pid} action={action} "
                f"recs={rec_ids} log={log_path} (no systemd-run)"
            )
            return DispatchHandle(
                pid=proc.pid, unit="", log_path=log_path,
                site=site, agent_id=agent_id, rec_ids=rec_ids,
                request_id=request_id,
            )
    finally:
        # Don't close the log_f — Popen has it as stdout/stderr. Decrement
        # the parent's reference so the implementer keeps writing after we
        # exit. Popen increments refcount internally, so closing here is
        # safe and just ensures we don't keep an extra handle open.
        try:
            log_f.close()
        except Exception:
            pass


def _resolve_implementer_script() -> Optional[str]:
    """Return the path to the implementer's entry point.

    Defaults to `agents/implementer/agent.py` (the AgentBase wrapper) so the
    dispatched run goes through AgentBase's status/run-index lifecycle —
    `state=running` shows up on the dashboard "Working now" hero, and the
    dispatch lands in the implementer's Runs tab. agent.py invokes the
    legacy run.sh as a subprocess so the heavy bash logic still runs.

    Override with FRAMEWORK_IMPLEMENTER_SCRIPT — the spawn picks the
    interpreter from the file extension (.py → python, otherwise bash).
    """
    explicit = os.environ.get("FRAMEWORK_IMPLEMENTER_SCRIPT")
    if explicit and Path(explicit).is_file():
        return explicit
    here = Path(__file__).resolve()
    py = here.parent.parent.parent / "agents" / "implementer" / "agent.py"
    if py.is_file():
        return str(py)
    sh = here.parent.parent.parent / "agents" / "implementer" / "run.sh"
    if sh.is_file():
        return str(sh)
    return None


def _make_request_id(site: str) -> str:
    return f"r-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{site or 'shared'}-{uuid.uuid4().hex[:6]}"


def _extract_run_ts(run_dir: str) -> Optional[str]:
    name = Path(run_dir).name
    # Common run-ts pattern: 20260504T123000Z
    import re
    m = re.search(r"(\d{8}T\d{6}Z)", name)
    return m.group(1) if m else None


def _write_auto_queue_fallback(*, agent_id: str, run_dir: str,
                                rec_ids: list[str], action: str, site: str,
                                subject_tag: str, request_id: str) -> None:
    """Transitional fallback: write to responder-agent/auto-queue/. The
    responder picks it up if/when its IMAP cron runs OR the user manually
    triggers it. Slated for removal once direct dispatch is trusted."""
    s = get_storage()
    payload = {
        "schema_version": "1",
        "request_id": request_id,
        "source_agent": agent_id,
        "site": site,
        "run_ts": _extract_run_ts(run_dir) or "",
        "rec_ids": rec_ids,
        "action": action,
        "subject_tag": subject_tag,
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": f"dispatch-fallback-from-{agent_id}",
    }
    s.write_json(f"agents/responder-agent/auto-queue/{request_id}.json", payload)


def _notify_dispatch_failure(*, agent_id: str, request_id: str,
                              rec_ids: list[str], error: str,
                              fell_back_to_queue: bool) -> None:
    """Email the operator about a dispatch that exhausted its retries.
    Best-effort — uses the goals-tracker's Graph send path if available,
    falls back to msmtp.
    """
    subject = f"[dispatch failure] {agent_id} — {len(rec_ids)} rec(s) could not dispatch"
    body_lines = [
        f"<p>Direct dispatch from <code>{agent_id}</code> failed after retries.</p>",
        f"<p><b>Request id:</b> {request_id}<br>",
        f"<b>Rec ids:</b> {', '.join(rec_ids)}<br>",
        f"<b>Site:</b> n/a<br>",
        f"<b>Error:</b> {error}<br>",
        f"<b>Fell back to auto-queue?</b> {'YES — responder will retry on next run' if fell_back_to_queue else 'NO — items are LOST until manually re-queued'}</p>",
        f"<p>Check <code>{LOG_DIR}</code> for spawn logs.</p>",
    ]
    html = f"<html><body style='font-family:sans-serif'>{''.join(body_lines)}</body></html>"

    # Prefer the goals-tracker's Graph-API send (works through tenant
    # SMTP-auth disable). Fall back to msmtp if that fails.
    try:
        sent = _try_graph_send(NOTIFY_EMAIL, subject, html)
        if sent:
            return
    except Exception as e:
        _log(f"[dispatch] graph notify failed: {e}")
    try:
        _try_msmtp(NOTIFY_EMAIL, subject, html, account="personal")
    except Exception as e:
        _log(f"[dispatch] msmtp notify failed: {e}")


def _try_graph_send(to_addr: str, subject: str, html: str) -> bool:
    import importlib.util, urllib.request
    here = Path(__file__).resolve()
    mt_path = here.parent.parent.parent / "agents" / "responder-agent" / "mint-token.py"
    if not mt_path.is_file():
        return False
    spec = importlib.util.spec_from_file_location("mint_token", str(mt_path))
    mt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mt)
    oauth_file = Path(os.path.expanduser("~/.reusable-agents/responder/.oauth.json"))
    if not oauth_file.is_file():
        return False
    token, _, _ = mt.mint_access_token(
        oauth_file,
        scope_override="offline_access https://graph.microsoft.com/Mail.Send.Shared",
    )
    # Sender mailbox — set OPERATOR_FROM_EMAIL in .env (or per-agent
    # IMPLEMENTER_FROM). Required for Graph /users/<mailbox>/sendMail.
    sender_addr = (os.environ.get("IMPLEMENTER_FROM")
                   or os.environ.get("OPERATOR_FROM_EMAIL")
                   or "")
    if not sender_addr:
        raise RuntimeError(
            "no sender mailbox configured — set OPERATOR_FROM_EMAIL "
            "(or IMPLEMENTER_FROM) in .env"
        )
    body = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
            "from": {"emailAddress": {"address": sender_addr}},
            "sender": {"emailAddress": {"address": sender_addr}},
        },
        "saveToSentItems": "false",
    }
    import urllib.parse, json as _j
    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{urllib.parse.quote(sender_addr, safe='')}"
        "/sendMail"
    )
    req = urllib.request.Request(
        url, method="POST", data=_j.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=20).read()
    return True


def _try_msmtp(to_addr: str, subject: str, html: str, *, account: str) -> bool:
    import email.message
    msg = email.message.EmailMessage()
    sender_addr = (os.environ.get("IMPLEMENTER_FROM")
                   or os.environ.get("OPERATOR_FROM_EMAIL")
                   or "")
    if not sender_addr:
        return False
    msg["From"] = sender_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content("Plain-text fallback")
    msg.add_alternative(html, subtype="html")
    proc = subprocess.run(
        ["msmtp", "-a", account, to_addr],
        input=msg.as_bytes(), capture_output=True, timeout=60,
    )
    return proc.returncode == 0


def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
    try:
        with (LOG_DIR / "dispatch.log").open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, file=sys.stderr)


def _write_minimal_dispatch_manifest(
    *,
    run_dir: str,
    rec_ids: list[str],
    request_id: str,
    agent_id: str,
) -> None:
    """Write a single-batch dispatch-batches.json so the dashboard's
    Queue page sees direct dispatch_now() invocations.

    The responder's _write_batch_manifest writes this when it splits a
    multi-rec auto-queue payload into smaller chunks. Producers that
    call dispatch_now() directly (catalog-audit, PI, SEO finalizer,
    competitor-research) skipped it, which made their work invisible
    on /api/implementer/batches.

    Idempotent: if the file already exists (e.g. responder already wrote
    one), don't overwrite — preserve whatever batching plan is in place.
    """
    import json as _json
    rd = Path(run_dir)
    if not rd.is_dir():
        return
    manifest_path = rd / "dispatch-batches.json"
    if manifest_path.is_file():
        return
    manifest = {
        "schema_version": "1",
        "source_agent_id": agent_id,
        "request_id": request_id,
        "batch_size": len(rec_ids),
        "total_recs": len(rec_ids),
        "batches": [
            {
                "index": 1,
                "rec_ids": list(rec_ids),
                "rec_count": len(rec_ids),
                "status": "pending",
                "started_at": "",
                "completed_at": "",
                "completion_status": "",
            },
        ],
    }
    manifest_path.write_text(_json.dumps(manifest, indent=2))
