"""Direct-dispatch helper for sending recommendations to the implementer.

Replaces the responder-agent's drain_auto_queue path. Producer agents now
call `dispatch_now()` at the end of their run instead of writing to
`agents/responder-agent/auto-queue/<id>.json`. The responder is now
strictly the IMAP-reply path (user confirmations) and runs on demand.

Architecture:
    Producer agent → dispatch_now() → site lock → systemd-run scope
                                                  → implementer
                                                  → seo-deployer (per site)

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
    "mperry@northernsoftwareconsulting.com",
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
                 fell_back_to_queue: bool = False):
        self.pid = pid
        self.unit = unit
        self.log_path = log_path
        self.site = site
        self.agent_id = agent_id
        self.rec_ids = list(rec_ids)
        self.fell_back_to_queue = fell_back_to_queue

    def __repr__(self) -> str:
        return (
            f"<DispatchHandle site={self.site} agent={self.agent_id} "
            f"unit={self.unit} pid={self.pid} fallback={self.fell_back_to_queue}>"
        )


# Public API -------------------------------------------------------------

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

    last_err: Optional[Exception] = None
    with site_dispatch_lock(site or "shared", timeout_s=lock_timeout_s):
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
                              fell_back_to_queue=True)
    raise DispatchError(msg)


# Internals --------------------------------------------------------------

def _spawn_implementer(*, agent_id: str, run_dir: str, rec_ids: list[str],
                       action: str, site: str, subject_tag: str,
                       request_id: str, batch_index: int, batch_total: int,
                       script: str) -> DispatchHandle:
    """Spawn the implementer in a detached systemd-run --scope.
    Returns DispatchHandle on successful spawn. Raises DispatchTransient
    on retryable failures."""
    env = os.environ.copy()
    env["RESPONDER_ACTION"] = action
    env["RESPONDER_REC_IDS"] = ",".join(rec_ids)
    env["RESPONDER_SITE"] = site
    env["RESPONDER_RUN_TS"] = _extract_run_ts(run_dir) or ""
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

    use_systemd_run = bool(shutil.which("systemd-run"))
    try:
        log_f = open(log_path, "ab")
    except OSError as e:
        raise DispatchTransient(f"cannot open log {log_path}: {e}")

    try:
        if use_systemd_run:
            unit_name = f"agent-dispatch-implementer-{site or 'shared'}-{ts}"
            cmd = [
                "systemd-run", "--user", "--scope", "--collect",
                f"--unit={unit_name}",
                "--property=KillMode=process",
                "--property=TimeoutStopSec=0",
                "bash", script,
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
            )
        else:
            try:
                proc = subprocess.Popen(
                    ["bash", script], env=env, start_new_session=True,
                    stdout=log_f, stderr=log_f,
                )
            except (OSError, FileNotFoundError) as e:
                raise DispatchTransient(f"bash spawn failed: {e}")
            _log(
                f"[dispatch] spawned {script} pid={proc.pid} action={action} "
                f"recs={rec_ids} log={log_path} (no systemd-run)"
            )
            return DispatchHandle(
                pid=proc.pid, unit="", log_path=log_path,
                site=site, agent_id=agent_id, rec_ids=rec_ids,
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
    """Return the path to the implementer's run.sh. Looks in standard
    locations + an env override."""
    explicit = os.environ.get("FRAMEWORK_IMPLEMENTER_SCRIPT")
    if explicit and Path(explicit).is_file():
        return explicit
    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent / "agents" / "implementer" / "run.sh"
    if candidate.is_file():
        return str(candidate)
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
    body = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
            "from": {"emailAddress": {"address": "automation@northernsoftwareconsulting.com"}},
            "sender": {"emailAddress": {"address": "automation@northernsoftwareconsulting.com"}},
        },
        "saveToSentItems": "false",
    }
    import urllib.parse, json as _j
    url = (
        "https://graph.microsoft.com/v1.0/users/"
        f"{urllib.parse.quote('automation@northernsoftwareconsulting.com', safe='')}"
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
    msg["From"] = "automation@northernsoftwareconsulting.com"
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
