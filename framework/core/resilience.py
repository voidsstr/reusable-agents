"""Resilience primitives for reusable agents.

Three things every agent needs when calling external systems (IMAP,
Microsoft Graph, claude CLI, subprocess audits, Azure blob, etc.):

  1. Retry with exponential backoff on transient errors.
  2. A hard wall-clock timeout so a hang can't pin the agent forever.
  3. A way to email the operator when an error is unrecoverable, so they
     know to investigate without watching log files.

This module ships small, composable helpers any agent can use:

  - `@with_retry(retries=3, backoff=1.5, on=(URLError, TimeoutError))`
    decorator that retries with jittered exponential backoff.

  - `notify_operator(agent_id, error, context)` sends a single error email
    via Graph (same path agents already use for normal mail). Rate-limited
    by (agent_id, error_class) so a flapping API doesn't spam the inbox.

  - `record_error(agent_id, error, context)` writes a single JSON entry to
    `agents/<agent_id>/errors/<run_ts>.json` in framework storage so the
    dashboard can render an "Errors" tab and the operator can see what
    happened across all runs.

  - `safe_run(callable, *, agent_id, context, retries=3, alert=True)` is
    the ergonomic top-level wrapper: runs the callable with retries +
    timeouts, records + emails on permanent failure, returns the
    callable's result on success.

Usage from an agent:
    from framework.core.resilience import safe_run, with_retry, notify_operator

    @with_retry(retries=3, on=(ConnectionError, TimeoutError))
    def fetch_data():
        return requests.get(url, timeout=30).json()

    try:
        data = fetch_data()
    except Exception as e:
        notify_operator(agent_id="my-agent", error=e,
                        context={"url": url, "phase": "fetch"})
        raise
"""
from __future__ import annotations

import functools
import json
import logging
import os
import random
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("framework.resilience")


# ---------------------------------------------------------------------------
# @with_retry decorator
# ---------------------------------------------------------------------------

def with_retry(
    retries: int = 3,
    backoff: float = 1.5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    on: tuple[type[BaseException], ...] = (Exception,),
    on_giveup: Optional[Callable[[BaseException, dict], None]] = None,
):
    """Decorator: call fn; on exception in `on`, sleep + retry up to N times.

    Backoff schedule: base_delay * (backoff ** attempt) with ±20% jitter,
    capped at max_delay. Default: 1s, 1.5s, 2.25s, 3.4s, 5.1s …

    Args:
      retries: number of RETRY attempts after the initial call (so total
               attempts = retries + 1).
      on: tuple of exception types to retry on. Anything else is re-raised.
      on_giveup: optional callback fn(exception, context_dict) called when
               we've exhausted retries — useful for logging or alerting.

    Example:
        @with_retry(retries=4, on=(ConnectionError, TimeoutError))
        def fetch():
            return urllib.request.urlopen(url, timeout=30).read()
    """
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc: Optional[BaseException] = None
            for attempt in range(retries + 1):
                try:
                    return fn(*args, **kwargs)
                except on as e:
                    last_exc = e
                    if attempt == retries:
                        break
                    delay = min(max_delay, base_delay * (backoff ** attempt))
                    delay = delay * random.uniform(0.8, 1.2)
                    logger.warning(
                        f"{fn.__name__} attempt {attempt + 1}/{retries + 1} "
                        f"failed: {type(e).__name__}: {e} — retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
            # Exhausted
            ctx = {"fn": fn.__name__, "attempts": retries + 1,
                   "args": str(args)[:200], "kwargs": str(kwargs)[:200]}
            if on_giveup:
                try:
                    on_giveup(last_exc, ctx)  # type: ignore[arg-type]
                except Exception as cb_e:
                    logger.warning(f"on_giveup callback failed: {cb_e}")
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Operator notification (email + storage record)
# ---------------------------------------------------------------------------

# In-memory rate-limit map. Keyed by (agent_id, error_class), value is
# the unix ts of the last alert. We don't persist this between processes
# — per-agent ephemeral suppression is enough since most agents are oneshot.
_ALERT_LAST: dict[tuple[str, str], float] = {}


def _should_send_alert(agent_id: str, error_class: str,
                        cooldown_s: float = 3600.0) -> bool:
    """Return True only if we haven't alerted for (agent_id, error_class)
    within the last `cooldown_s` seconds. Default 1 hour."""
    key = (agent_id, error_class)
    now = time.time()
    last = _ALERT_LAST.get(key, 0)
    if now - last < cooldown_s:
        return False
    _ALERT_LAST[key] = now
    return True


def notify_operator(
    *,
    agent_id: str,
    error: BaseException,
    context: dict,
    operator_email: Optional[str] = None,
    sender: Optional[str] = None,
    cooldown_s: float = 3600.0,
    severity: str = "high",
) -> tuple[bool, str]:
    """Email the operator about an unrecoverable error and record the
    incident in framework storage.

    Always records — emailing is best-effort (rate-limited per error class).
    Returns (email_sent, detail).

    Args:
      agent_id: which agent failed.
      error: the exception.
      context: free-form dict — phase ('imap-poll', 'graph-send'),
               run_ts, request_id, last 1000 chars of stderr, etc.
      operator_email: defaults to env $FRAMEWORK_OPERATOR_EMAIL or
                      mperry@northernsoftwareconsulting.com.
      sender: defaults to env $IMPLEMENTER_FROM or automation@nsc.
      cooldown_s: per (agent, error_class) suppression window. Set to 0
                  to disable rate-limiting (e.g., test the alert path).
      severity: 'critical' / 'high' / 'medium' — affects subject prefix.
    """
    operator_email = (operator_email
                      or os.environ.get("FRAMEWORK_OPERATOR_EMAIL")
                      or "mperry@northernsoftwareconsulting.com")
    sender = (sender
              or os.environ.get("IMPLEMENTER_FROM")
              or "automation@northernsoftwareconsulting.com")
    error_class = type(error).__name__

    # Always record the incident — the email may be skipped (rate-limit) or
    # fail (Graph down), but the storage entry is the authoritative log.
    record_error(agent_id=agent_id, error=error, context=context, severity=severity)

    # Rate-limit emails so a flapping endpoint doesn't spam
    if cooldown_s > 0 and not _should_send_alert(agent_id, error_class, cooldown_s):
        return False, "suppressed by rate limit"

    sev_prefix = {"critical": "🚨 CRITICAL", "high": "⚠ HIGH", "medium": "ℹ"}.get(severity, "⚠")
    subject = f"[agent-error:{agent_id}] {sev_prefix} {error_class}: {str(error)[:80]}"

    body = _format_error_email(
        agent_id=agent_id, error=error, context=context, severity=severity,
    )

    # Send via shared.site_quality.send_via_msmtp which already prefers Graph.
    try:
        # Late import — resilience.py is imported from many places, keep it
        # decoupled from shared.site_quality at module load.
        import sys
        from pathlib import Path as _P
        _ROOT = _P(__file__).resolve().parent.parent.parent
        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))
        from shared.site_quality import send_via_msmtp
    except Exception as e:
        return False, f"send_via_msmtp unavailable: {e}"

    try:
        ok, detail = send_via_msmtp(
            subject=subject, body_html=body, to=[operator_email], sender=sender,
            extra_headers={
                "X-Reusable-Agent": agent_id,
                "X-Error-Class": error_class,
                "X-Severity": severity,
            },
        )
        return ok, detail
    except Exception as e:
        logger.warning(f"notify_operator email failed for {agent_id}: {e}")
        return False, f"send raised: {e}"


def record_error(
    *,
    agent_id: str,
    error: BaseException,
    context: dict,
    severity: str = "high",
) -> None:
    """Append the error to agents/<agent_id>/errors/<ts>.json so it's
    visible from the dashboard's Errors tab. Safe to call from any path —
    silent on storage failure (we don't want resilience to itself fail)."""
    try:
        from framework.core.storage import get_storage
        s = get_storage()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"agents/{agent_id}/errors/{ts}-{type(error).__name__}.json"
        s.write_json(key, {
            "schema_version": "1",
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent_id": agent_id,
            "severity": severity,
            "error_class": type(error).__name__,
            "error_message": str(error),
            "traceback": "".join(traceback.format_exception(type(error), error,
                                                            error.__traceback__))[:8000],
            "context": context,
        })
    except Exception as e:
        logger.warning(f"record_error storage write failed for {agent_id}: {e}")


def _format_error_email(*, agent_id: str, error: BaseException,
                        context: dict, severity: str) -> str:
    """HTML body for the operator-error email."""
    error_class = type(error).__name__
    error_msg = str(error)
    tb = "".join(traceback.format_exception(type(error), error,
                                             error.__traceback__))[-4000:]
    ctx_rows = "\n".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#64748b">{k}</td>'
        f'<td><code style="font-size:12px">{str(v)[:300]}</code></td></tr>'
        for k, v in (context or {}).items()
    )
    sev_color = {"critical": "#dc2626", "high": "#ea580c", "medium": "#ca8a04"}.get(severity, "#ea580c")
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;color:#0f172a;line-height:1.5">
<div style="max-width:720px;margin:0 auto;padding:20px;border:1px solid #e2e8f0;border-radius:6px">
  <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
    <div style="width:8px;height:8px;border-radius:99px;background:{sev_color}"></div>
    <h2 style="margin:0;color:{sev_color}">{severity.upper()}: {error_class}</h2>
  </div>
  <div style="color:#475569;font-size:14px;margin-bottom:18px">
    Agent <code>{agent_id}</code> hit an unrecoverable error after exhausting retries.
  </div>

  <h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Error</h3>
  <pre style="background:#fef2f2;border:1px solid #fecaca;color:#991b1b;padding:12px;border-radius:6px;font-size:12px;white-space:pre-wrap">{error_msg[:500]}</pre>

  <h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Context</h3>
  <table style="font-size:13px;border-collapse:collapse">{ctx_rows}</table>

  <h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Traceback</h3>
  <pre style="background:#f8fafc;border:1px solid #e2e8f0;padding:8px;border-radius:6px;font-size:11px;color:#475569;white-space:pre-wrap;overflow-x:auto">{tb}</pre>

  <div style="color:#64748b;font-size:12px;margin-top:18px">
    Recorded at <code>agents/{agent_id}/errors/&lt;ts&gt;-{error_class}.json</code>.
    Auto-recovery (retries) failed; investigation required. Subsequent
    instances of the same error class will be suppressed for 1 hour to
    avoid inbox flooding.
  </div>
</div></body></html>"""


# ---------------------------------------------------------------------------
# safe_run — the ergonomic top-level wrapper
# ---------------------------------------------------------------------------

def safe_run(
    fn: Callable[[], Any],
    *,
    agent_id: str,
    context: Optional[dict] = None,
    retries: int = 3,
    backoff: float = 1.5,
    on: tuple[type[BaseException], ...] = (Exception,),
    alert: bool = True,
    severity: str = "high",
) -> Any:
    """Run `fn()` with retries + on permanent failure record + (optionally)
    email the operator. Re-raises the final exception so the caller can
    decide whether to bail.

    Use as the outermost wrapper around any external-call-heavy phase:

        result = safe_run(
            lambda: graph_sendmail(payload),
            agent_id="seo-reporter", context={"phase": "graph-send"},
            retries=3, on=(URLError, HTTPError),
        )
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except on as e:
            last_exc = e
            if attempt == retries:
                break
            delay = 1.0 * (backoff ** attempt) * random.uniform(0.8, 1.2)
            delay = min(30.0, delay)
            logger.warning(
                f"safe_run[{agent_id}] attempt {attempt + 1}/{retries + 1} "
                f"failed: {type(e).__name__}: {e} — retrying in {delay:.1f}s"
            )
            time.sleep(delay)
    assert last_exc is not None
    if alert:
        try:
            notify_operator(agent_id=agent_id, error=last_exc,
                             context=context or {}, severity=severity)
        except Exception as e:
            logger.warning(f"safe_run alert failed: {e}")
    raise last_exc
