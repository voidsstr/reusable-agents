"""Framework-level completion email — sent by an implementer agent after it
finishes applying recs that came in via an email reply.

Use case: user replies to a rec email with "implement rec-001 rec-005",
the responder dispatches to seo-implementer, which applies the changes.
This module is what the implementer calls at the end to email the user
back ("done — here's what shipped").

Why this is in the framework, not per-agent:
  - Every implementer-style agent should send the same kind of confirmation.
  - The recipient + subject thread chain are FRAMEWORK concerns (request_id
    lookups, X-Reusable-Agent headers, outbound-email tracking).
  - Bash-based implementers can shell out to this module's __main__ — no
    need to duplicate the HTML template per agent.

The send is best-effort: failure to email never blocks dispatch completion.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.completion_email")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_recipient(*, request_id: Optional[str], source_agent: Optional[str],
                       site_config_path: Optional[str], explicit_to: Optional[str],
                       storage: StorageBackend) -> str:
    """Figure out who to email.

    Order:
      1. explicit_to (env var IMPLEMENTER_NOTIFY_EMAIL or arg)
      2. outbound-emails/<request-id>.json on the source agent — the address
         we sent the original recs email to
      3. site config's reporter.email.to[] (legacy SEO config path)
    """
    if explicit_to:
        return explicit_to
    if source_agent and request_id:
        meta = storage.read_json(
            f"agents/{source_agent}/outbound-emails/{request_id}.json"
        ) or {}
        to = meta.get("to")
        if isinstance(to, list) and to:
            return ",".join(to)
        if isinstance(to, str) and to:
            return to
    if site_config_path and Path(site_config_path).is_file():
        try:
            import yaml  # type: ignore
            cfg = yaml.safe_load(Path(site_config_path).read_text())
            rcp = (((cfg.get("reporter") or {}).get("email") or {}).get("to") or [])
            if rcp:
                return ",".join(rcp) if isinstance(rcp, list) else str(rcp)
        except Exception as e:
            logger.warning(f"site-config recipient parse failed: {e}")
    return ""


def _build_html(*, agent_id: str, source_agent: str, site: str, request_id: str,
                rec_ids: list[str], rec_titles: dict[str, str],
                run_dir: str, commit_sha: str, mode: str,
                dashboard_base: str) -> str:
    rec_rows = "".join(
        f'<tr><td style="padding:4px 8px 4px 0;color:#64748b">{rid}</td>'
        f'<td>{(rec_titles.get(rid) or "—")[:140]}</td></tr>'
        for rid in rec_ids
    ) or '<tr><td colspan=2 style="color:#94a3b8">(no rec ids)</td></tr>'
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;color:#0f172a;line-height:1.5">
<div style="max-width:720px;margin:0 auto;padding:20px;border:1px solid #e2e8f0;border-radius:6px">
<h2 style="margin:0 0 8px 0;color:#0f172a">✓ Shipped — {len(rec_ids)} rec(s) applied</h2>
<div style="color:#475569;font-size:14px;margin-bottom:16px">
The recommendations you approved by reply have been implemented by
<code>{agent_id}</code>.
</div>

<h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Recommendations applied</h3>
<table style="font-size:13px;border-collapse:collapse;margin-top:6px;width:100%">
{rec_rows}
</table>

<h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Run details</h3>
<table style="margin-top:6px;font-size:13px;border-collapse:collapse">
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Source agent</td><td>{source_agent or '—'}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Site</td><td>{site or '—'}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Request id</td><td><code>{request_id}</code></td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Run dir</td><td><code>{run_dir or '—'}</code></td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Commit</td><td><code>{commit_sha or 'pending'}</code></td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Mode</td><td>{mode or 'claude'}</td></tr>
</table>

<div style="color:#64748b;font-size:12px;margin-top:18px">
<a href="{dashboard_base}/agents/{agent_id}" style="color:#2563eb">View this run in the dashboard →</a>
</div>
</div></body></html>"""


def send_completion_email(
    *,
    agent_id: str,
    rec_ids: list[str],
    site: str = "",
    source_agent: str = "",
    request_id: str = "",
    run_dir: str = "",
    commit_sha: str = "",
    mode: str = "",
    rec_titles: Optional[dict[str, str]] = None,
    explicit_to: str = "",
    sender: str = "",
    msmtp_account: str = "automation",
    site_config_path: str = "",
    dashboard_base: str = "",
    storage: Optional[StorageBackend] = None,
) -> tuple[bool, str]:
    """Send a per-rec confirmation email after an implementer ships changes.

    Returns (ok, detail). Best-effort — failure is non-fatal; we record the
    attempt to outbound-emails/<request-id>-completion.json regardless so
    the dashboard can show the send attempt.
    """
    s = storage or get_storage()
    sender = sender or os.environ.get("IMPLEMENTER_FROM",
                                       "automation@northernsoftwareconsulting.com")
    dashboard_base = dashboard_base or os.environ.get(
        "FRAMEWORK_DASHBOARD_URL", "http://localhost:8091")
    to = _resolve_recipient(
        request_id=request_id, source_agent=source_agent,
        site_config_path=site_config_path or None,
        explicit_to=explicit_to or os.environ.get("IMPLEMENTER_NOTIFY_EMAIL", ""),
        storage=s,
    )
    if not to:
        logger.info(f"completion email skipped: no recipient resolved for {agent_id}/{request_id}")
        return False, "no recipient"

    rec_titles = rec_titles or {}
    subject = (
        f"[{agent_id}:{request_id or 'done'}] Shipped {len(rec_ids)} rec(s)"
        f" — site={site or 'unknown'}"
    )
    body_html = _build_html(
        agent_id=agent_id, source_agent=source_agent, site=site,
        request_id=request_id, rec_ids=rec_ids, rec_titles=rec_titles,
        run_dir=run_dir, commit_sha=commit_sha, mode=mode,
        dashboard_base=dashboard_base,
    )
    headers = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        "MIME-Version: 1.0",
        "Content-Type: text/html; charset=utf-8",
        f"X-Reusable-Agent: {agent_id}",
    ]
    msg = "\r\n".join(headers) + "\r\n\r\n" + body_html

    # Track the attempt — even if msmtp fails, we want a paper trail
    track_key = f"agents/{agent_id}/outbound-emails/{request_id or 'completion-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.completion.json"
    sent_at = _now_iso()

    try:
        proc = subprocess.run(
            ["msmtp", "-a", msmtp_account] + [a.strip() for a in to.split(",") if a.strip()],
            input=msg.encode(), capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace")[:300]
            try:
                s.write_json(track_key, {
                    "kind": "completion-email", "ok": False,
                    "to": to, "subject": subject, "sent_at": sent_at,
                    "rec_ids": rec_ids, "agent_id": agent_id,
                    "error": err,
                })
            except Exception:
                pass
            return False, f"msmtp rc={proc.returncode}: {err}"
    except FileNotFoundError:
        return False, "msmtp not installed"
    except subprocess.TimeoutExpired:
        return False, "msmtp timed out after 60s"

    try:
        s.write_json(track_key, {
            "kind": "completion-email", "ok": True,
            "to": to, "subject": subject, "sent_at": sent_at,
            "rec_ids": rec_ids, "agent_id": agent_id,
            "request_id": request_id, "site": site,
            "commit_sha": commit_sha,
        })
    except Exception as e:
        logger.warning(f"completion-email track write failed: {e}")
    return True, "ok"


# ---------------------------------------------------------------------------
# CLI shim — for shell-based implementers (e.g. seo-implementer/run.sh) to
# invoke without re-implementing the email plumbing.
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Send a completion email after an implementer agent finishes.")
    p.add_argument("--agent-id", required=True)
    p.add_argument("--rec-ids", required=True, help="comma-separated rec ids")
    p.add_argument("--site", default="")
    p.add_argument("--source-agent", default="",
                   help="agent that emitted the recs (lookup target for outbound-emails)")
    p.add_argument("--request-id", default="")
    p.add_argument("--run-dir", default="")
    p.add_argument("--commit-sha", default="")
    p.add_argument("--mode", default="")
    p.add_argument("--site-config", default="")
    p.add_argument("--rec-titles-json", default="",
                   help="optional JSON file of {rec_id: title}")
    p.add_argument("--to", default="", help="explicit recipient override")
    p.add_argument("--sender", default="")
    p.add_argument("--msmtp-account", default="automation")
    p.add_argument("--dashboard-base", default="")
    args = p.parse_args()

    rec_ids = [r.strip() for r in args.rec_ids.split(",") if r.strip()]
    rec_titles: dict[str, str] = {}
    if args.rec_titles_json and Path(args.rec_titles_json).is_file():
        try:
            rec_titles = json.loads(Path(args.rec_titles_json).read_text())
        except Exception:
            pass

    ok, detail = send_completion_email(
        agent_id=args.agent_id, rec_ids=rec_ids, site=args.site,
        source_agent=args.source_agent, request_id=args.request_id,
        run_dir=args.run_dir, commit_sha=args.commit_sha, mode=args.mode,
        rec_titles=rec_titles, explicit_to=args.to, sender=args.sender,
        msmtp_account=args.msmtp_account,
        site_config_path=args.site_config,
        dashboard_base=args.dashboard_base,
    )
    print(f"[completion-email] {'sent' if ok else 'skipped/failed'}: {detail}",
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
