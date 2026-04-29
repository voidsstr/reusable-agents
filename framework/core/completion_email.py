"""Framework-level completion email — sent by an implementer agent after it
finishes applying recs that came in via an email reply.

Use case: user replies to a rec email with "implement rec-001 rec-005",
the responder dispatches to implementer, which applies the changes.
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


def _published_article_urls(run_dir: str, site: str, applied_rec_ids: list[str]) -> list[dict]:
    """When the source agent is an article-author, the implementer wrote
    rows to editorial_articles / buying_guides with slugs from each rec's
    article_proposal.slug. Pull the slugs for applied recs from the run
    dir's recommendations.json and construct live URLs.

    Returns a list of {rec_id, title, url, format}. Empty if recs.json is
    missing or has no article_proposal blocks. Handles both /articles/<slug>
    and /buying-guides/<slug> URL patterns based on the proposal's bucket.
    """
    if not run_dir or not site:
        return []
    recs_path = Path(run_dir) / "recommendations.json"
    if not recs_path.is_file():
        return []
    try:
        d = json.loads(recs_path.read_text())
    except Exception:
        return []
    applied = set(applied_rec_ids or [])
    out: list[dict] = []
    base = f"https://{site}.com" if "." not in site else f"https://{site}"
    for r in d.get("recommendations", []):
        rid = r.get("id")
        if applied and rid not in applied:
            continue
        ap = r.get("article_proposal") or {}
        slug = ap.get("slug") or r.get("slug")
        if not slug:
            continue
        # SpecPicks: ALL article-author output goes to editorial_articles
        # which renders at /reviews/<slug>. The legacy buying_guides table
        # has no public API endpoint — we no longer write to it.
        url_path = f"/reviews/{slug}"
        out.append({
            "rec_id": rid,
            "title": ap.get("title") or r.get("title") or "",
            "url": f"{base}{url_path}",
            "format": ap.get("format", ""),
            "bucket": bucket,
        })
    return out


def _build_html(*, agent_id: str, source_agent: str, site: str, request_id: str,
                rec_ids: list[str], rec_titles: dict[str, str],
                run_dir: str, commit_sha: str, mode: str,
                dashboard_base: str, status: str = "completed",
                status_reason: str = "",
                requested_rec_ids: Optional[list[str]] = None) -> str:
    requested = requested_rec_ids or rec_ids
    applied_set = set(rec_ids) if status == "completed" else set()
    skipped = [rid for rid in requested if rid not in applied_set]

    # Applied rows (the actually-shipped recs)
    rec_rows = "".join(
        f'<tr><td style="padding:4px 8px 4px 0;color:#64748b">{rid}</td>'
        f'<td>{(rec_titles.get(rid) or "—")[:140]}</td></tr>'
        for rid in rec_ids
    ) or '<tr><td colspan=2 style="color:#94a3b8">(none — implementer paused)</td></tr>'

    # Skipped rows (when partial apply)
    skipped_rows = "".join(
        f'<tr><td style="padding:4px 8px 4px 0;color:#64748b">{rid}</td>'
        f'<td>{(rec_titles.get(rid) or "—")[:140]}</td></tr>'
        for rid in skipped
    )

    if status == "paused":
        header = (
            f'<h2 style="margin:0 0 8px 0;color:#b45309">'
            f'⏸ Paused — 0 of {len(requested)} rec(s) applied'
            f'</h2>'
        )
        body_intro = (
            f'<div style="color:#475569;font-size:14px;margin-bottom:16px">'
            f'The implementer (<code>{agent_id}</code>) read your reply but did NOT '
            f'commit or apply any of the {len(requested)} recommendations you asked for. '
            f'Most likely reasons: the batch was too large for one shot, individual '
            f'recs needed clarification (e.g. investigations vs code edits), or recs '
            f'targeted noise/typo data. See the dashboard for the full LLM transcript '
            f'and either reply with a smaller scope (e.g. '
            f'<code>implement rec-001 rec-002 rec-005</code>) or a focused filter '
            f'(e.g. <code>implement high</code>).'
            f'</div>'
        )
    elif skipped:
        header = (
            f'<h2 style="margin:0 0 8px 0;color:#0f172a">'
            f'✓ Shipped {len(rec_ids)} of {len(requested)} rec(s) — '
            f'<span style="color:#b45309">{len(skipped)} skipped</span>'
            f'</h2>'
        )
        body_intro = (
            '<div style="color:#475569;font-size:14px;margin-bottom:16px">'
            f'<code>{agent_id}</code> applied the listed recs but skipped {len(skipped)} '
            f'(see "Skipped" table below). Reply with the skipped ids if you want them '
            f'retried with more context, or a filter like <code>implement high</code>.'
            '</div>'
        )
    else:
        header = f'<h2 style="margin:0 0 8px 0;color:#0f172a">✓ Shipped — {len(rec_ids)} rec(s) applied</h2>'
        body_intro = (
            '<div style="color:#475569;font-size:14px;margin-bottom:16px">'
            f'The recommendations you approved by reply have been implemented by '
            f'<code>{agent_id}</code>.'
            '</div>'
        )

    # Optional reason line — shows the verifying signal (commit sha, applied-recs.json, etc.)
    reason_block = ""
    if status_reason:
        # Escape HTML
        import html as _html
        reason_safe = _html.escape(status_reason)
        reason_block = (
            f'<div style="color:#64748b;font-size:12px;margin-bottom:12px;'
            f'font-family:monospace;background:#f1f5f9;padding:6px 10px;border-radius:4px">'
            f'<b>verified:</b> {reason_safe}'
            f'</div>'
        )
    applied_heading = (
        "Recommendations applied"
        if status == "completed"
        else "Recommendations NOT applied"
    )
    skipped_section = ""
    if status == "completed" and skipped:
        skipped_section = f"""
<h3 style="margin-top:18px;font-size:14px;color:#b45309;border-bottom:1px solid #fed7aa;padding-bottom:4px">Skipped — not applied</h3>
<table style="font-size:13px;border-collapse:collapse;margin-top:6px;width:100%">
{skipped_rows}
</table>
"""

    # Article-author special section: when the source agent is an
    # article-author, surface the live URLs of every published article so
    # the operator can click through and verify. Only fires when the
    # implementer actually wrote articles (status=completed).
    published_section = ""
    if (status == "completed"
            and source_agent
            and source_agent.endswith("-article-author-agent")):
        published = _published_article_urls(run_dir, site, rec_ids)
        if published:
            rows = "".join(
                f'<tr>'
                f'<td style="padding:6px 10px 6px 0;color:#64748b;font-family:monospace;font-size:12px">{p["rec_id"]}</td>'
                f'<td style="padding:6px 10px 6px 0">'
                f'<a href="{p["url"]}" style="color:#2563eb;text-decoration:none;font-weight:500">{p["title"][:140]}</a>'
                f'<div style="color:#64748b;font-size:11px;margin-top:2px">'
                f'{p["bucket"]}{(" · " + p["format"]) if p["format"] else ""}'
                f'</div>'
                f'<div style="color:#94a3b8;font-size:11px;font-family:monospace;margin-top:1px">{p["url"]}</div>'
                f'</td></tr>'
                for p in published
            )
            published_section = f"""
<h3 style="margin-top:18px;font-size:14px;color:#059669;border-bottom:1px solid #a7f3d0;padding-bottom:4px">🌐 Now live on {site}.com</h3>
<table style="font-size:13px;border-collapse:collapse;margin-top:6px;width:100%">
{rows}
</table>
<div style="font-size:11px;color:#64748b;margin-top:6px">
  Articles were generated by the implementer and written to <code>editorial_articles</code> / <code>buying_guides</code>.
  They render at the URLs above as soon as the next scheduled deploy or page revalidation runs.
</div>
"""
    # Tracking id chip — visible in body so the user can correlate the
    # email to the dashboard's outbound-emails record without digging
    # through subject/headers.
    import html as _html_mod
    rid_chip = (
        f'<div style="display:inline-block;font-size:11px;color:#64748b;'
        f'background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;'
        f'padding:4px 10px;margin-bottom:12px">'
        f'<b>Request id:</b> <code style="color:#0f172a">{_html_mod.escape(request_id or "—")}</code>'
        f'</div>'
    ) if request_id else ""
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,sans-serif;color:#0f172a;line-height:1.5">
<div style="max-width:720px;margin:0 auto;padding:20px;border:1px solid #e2e8f0;border-radius:6px">
{header}
{body_intro}
{rid_chip}
{reason_block}

<h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">{applied_heading}</h3>
<table style="font-size:13px;border-collapse:collapse;margin-top:6px;width:100%">
{rec_rows if status == "completed" else "".join(f'<tr><td style=padding:4px_8px_4px_0;color:#64748b>{rid}</td><td>{(rec_titles.get(rid) or "—")[:140]}</td></tr>' for rid in requested) or '<tr><td colspan=2 style="color:#94a3b8">(none)</td></tr>'}
</table>
{skipped_section}
{published_section}

<h3 style="margin-top:18px;font-size:14px;color:#475569;border-bottom:1px solid #e2e8f0;padding-bottom:4px">Run details</h3>
<table style="margin-top:6px;font-size:13px;border-collapse:collapse">
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Source agent</td><td>{source_agent or '—'}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Site</td><td>{site or '—'}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Request id</td><td><code>{request_id}</code></td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Run dir</td><td><code>{run_dir or '—'}</code></td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Commit</td><td><code>{commit_sha or 'no commit'}</code></td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Mode</td><td>{mode or 'claude'}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#64748b">Status</td><td>{status}</td></tr>
</table>

<div style="color:#64748b;font-size:12px;margin-top:18px">
<a href="{dashboard_base}/agents/{agent_id}" style="color:#2563eb">View this run in the dashboard →</a>
</div>
</div></body></html>"""


def _send_via_graph(*, to_list: list[str], from_address: str,
                    subject: str, html_body: str,
                    extra_headers: list[tuple[str, str]],
                    oauth_file: Path) -> tuple[bool, str]:
    """Send via Microsoft Graph /users/<from>/sendMail (Send-As) or /me/sendMail.

    Uses the responder's OAuth refresh token. Required because the host's
    msmtp is sandboxed by AppArmor and can't exec the python3 passwordeval.
    """
    import urllib.error
    import urllib.request as _ur
    # Lazy-import the mint helpers from responder-agent
    here = Path(__file__).resolve().parent
    repo_root = here.parent.parent
    mint_path = repo_root / "agents" / "responder-agent"
    sys.path.insert(0, str(mint_path))
    try:
        from importlib import import_module
        mint = import_module("mint-token")
        token, oauth_user, _ = mint.mint_access_token(
            oauth_file,
            scope_override="offline_access https://graph.microsoft.com/Mail.Send",
        )
    finally:
        try: sys.path.remove(str(mint_path))
        except ValueError: pass

    # Graph rejects internetMessageHeaders with non-x-* names AND rejects
    # an empty list. Filter + omit accordingly.
    x_headers = [(k, v) for k, v in extra_headers if k.lower().startswith("x-")]
    msg: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [
            {"emailAddress": {"address": addr.strip()}}
            for addr in to_list if addr.strip()
        ],
    }
    if x_headers:
        msg["internetMessageHeaders"] = [{"name": k, "value": v} for k, v in x_headers]
    # Try send_as first (shared mailbox), then send_on_behalf, then self
    attempts = [
        ("send_as", f"https://graph.microsoft.com/v1.0/users/{from_address}/sendMail",
         {"message": msg, "saveToSentItems": True}),
        ("send_on_behalf", "https://graph.microsoft.com/v1.0/me/sendMail",
         {"message": dict(msg, **{"from": {"emailAddress": {"address": from_address}}}),
          "saveToSentItems": True}),
        ("self", "https://graph.microsoft.com/v1.0/me/sendMail",
         {"message": msg, "saveToSentItems": True}),
    ]
    last_err = ""
    for method, url, payload in attempts:
        body = json.dumps(payload).encode()
        req = _ur.Request(url, data=body, method="POST",
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json"})
        try:
            with _ur.urlopen(req, timeout=30) as resp:
                if resp.status == 202:
                    return True, f"graph:{method}"
        except urllib.error.HTTPError as e:
            last_err = f"graph:{method} HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
        except Exception as e:
            last_err = f"graph:{method} {type(e).__name__}: {e}"
    return False, last_err


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
    site_label: str = "",
    dashboard_base: str = "",
    oauth_file: str = "",
    storage: Optional[StorageBackend] = None,
    status: str = "completed",  # "completed" or "paused" — paused = claude refused / no work done
    status_reason: str = "",
    requested_rec_ids: Optional[list[str]] = None,
) -> tuple[bool, str]:
    """Send a per-rec confirmation email after an implementer ships changes.

    Send-path priority:
      1. Microsoft Graph /sendMail — if oauth_file exists. This is the
         standard path because the host's msmtp is sandboxed by AppArmor
         and can't exec the python3 passwordeval.
      2. msmtp — fallback for hosts where Graph isn't configured.

    Returns (ok, detail). Best-effort — failure is non-fatal; we record the
    attempt to outbound-emails/<request-id>.completion.json regardless so
    the dashboard's Confirmations view can show the send trail.
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

    # Strip 'Display Name <addr@x>' down to just addr@x for from_address
    sender_addr = sender
    if "<" in sender and ">" in sender:
        sender_addr = sender.split("<", 1)[1].split(">", 1)[0]

    rec_titles = rec_titles or {}

    # Application name in the title — read from site config's site.label
    # if not passed explicitly, fall back to a TitleCase of the site id.
    label = site_label
    if not label and site_config_path and Path(site_config_path).is_file():
        try:
            import yaml  # type: ignore
            cfg = yaml.safe_load(Path(site_config_path).read_text())
            label = (cfg.get("site") or {}).get("label") or ""
        except Exception:
            pass
    if not label:
        # Heuristic: "aisleprompt" → "AislePrompt", "specpicks" → "SpecPicks".
        # Crude but adequate fallback when label is unset.
        special = {"aisleprompt": "AislePrompt", "specpicks": "SpecPicks"}
        label = special.get(site, site.title() if site else "Unknown")

    # Subject: application name first (most readable in the inbox), then
    # the rec count + the agent id as the routing tag. The agent_id used
    # here is responder-agent (since this email is the responder reporting
    # back), not the implementer that did the actual code work.
    requested = requested_rec_ids or rec_ids
    requested_count = len(requested)
    applied_count = len(rec_ids) if status == "completed" else 0
    skipped_count = max(0, requested_count - applied_count)

    if status == "paused":
        subject = (
            f"[{agent_id}:{request_id or 'done'}] {label} — "
            f"PAUSED: implementer needs guidance ({requested_count} rec(s) NOT applied)"
        )
    elif skipped_count > 0:
        # Partial — claude shipped some but not all
        subject = (
            f"[{agent_id}:{request_id or 'done'}] {label} — "
            f"Shipped {applied_count}/{requested_count} rec(s) "
            f"({skipped_count} skipped)"
        )
    else:
        # Article-author dispatches: emphasize "live" since articles render
        # at user-visible URLs as soon as the DB row is written.
        if source_agent and source_agent.endswith("-article-author-agent") and applied_count > 0:
            subject = (
                f"[{agent_id}:{request_id or 'done'}] {label} — "
                f"🌐 {applied_count} article(s) live"
            )
        else:
            subject = (
                f"[{agent_id}:{request_id or 'done'}] {label} — Shipped {applied_count} rec(s)"
            )
    body_html = _build_html(
        agent_id=agent_id, source_agent=source_agent, site=site,
        request_id=request_id, rec_ids=rec_ids, rec_titles=rec_titles,
        run_dir=run_dir, commit_sha=commit_sha, mode=mode,
        dashboard_base=dashboard_base, status=status,
        status_reason=status_reason,
        requested_rec_ids=requested,
    )

    track_key = (
        f"agents/{agent_id}/outbound-emails/"
        f"{request_id or 'completion-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.completion.json"
    )
    sent_at = _now_iso()

    def _track(ok: bool, transport: str, error: str = "") -> None:
        try:
            s.write_json(track_key, {
                "kind": "completion-email", "ok": ok,
                "transport": transport, "error": error,
                "to": to, "subject": subject, "sent_at": sent_at,
                "rec_ids": rec_ids, "agent_id": agent_id,
                "request_id": request_id, "site": site,
                "commit_sha": commit_sha,
            })
        except Exception as e:
            logger.warning(f"completion-email track write failed: {e}")

    to_list = [a.strip() for a in to.split(",") if a.strip()]
    extra_headers = [("X-Reusable-Agent", agent_id)]

    # Try Graph first
    oauth_path = Path(os.path.expanduser(
        oauth_file or os.environ.get("RESPONDER_OAUTH_FILE",
                                       "~/.reusable-agents/responder/.oauth.json")
    ))
    if oauth_path.is_file():
        ok, detail = _send_via_graph(
            to_list=to_list, from_address=sender_addr,
            subject=subject, html_body=body_html,
            extra_headers=extra_headers, oauth_file=oauth_path,
        )
        if ok:
            _track(True, detail)
            return True, detail
        logger.info(f"completion email graph send failed: {detail} — trying msmtp")

    # Fallback to msmtp
    headers = [
        f"From: {sender}", f"To: {to}", f"Subject: {subject}",
        "MIME-Version: 1.0", "Content-Type: text/html; charset=utf-8",
        f"X-Reusable-Agent: {agent_id}",
    ]
    msg_text = "\r\n".join(headers) + "\r\n\r\n" + body_html
    try:
        proc = subprocess.run(
            ["msmtp", "-a", msmtp_account] + to_list,
            input=msg_text.encode(), capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace")[:300]
            _track(False, "msmtp", err)
            return False, f"msmtp rc={proc.returncode}: {err}"
    except FileNotFoundError:
        _track(False, "msmtp", "msmtp not installed")
        return False, "msmtp not installed"
    except subprocess.TimeoutExpired:
        _track(False, "msmtp", "msmtp timed out")
        return False, "msmtp timed out after 60s"

    _track(True, "msmtp")
    return True, "msmtp ok"


# ---------------------------------------------------------------------------
# CLI shim — for shell-based implementers (e.g. implementer/run.sh) to
# invoke without re-implementing the email plumbing.
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Send a completion email after an implementer agent finishes.")
    p.add_argument("--agent-id", required=True)
    p.add_argument("--rec-ids", required=True, help="comma-separated rec ids")
    p.add_argument("--site", default="")
    p.add_argument("--site-label", default="",
                   help="Human-readable application name for the email subject "
                        "(e.g., 'AislePrompt', 'SpecPicks'). Falls back to "
                        "site.label from site-config, then TitleCase of --site.")
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
    p.add_argument("--status", default="completed",
                   choices=("completed", "paused"),
                   help="completed = recs applied; paused = claude bailed without applying")
    p.add_argument("--status-reason", default="",
                   help="optional human-readable reason explaining the status "
                        "(e.g. commit sha, applied-recs.json contents, or why paused)")
    p.add_argument("--requested-rec-ids", default="",
                   help="comma-separated rec ids the user originally asked for. "
                        "When --rec-ids is a strict subset (partial apply), the email "
                        "explicitly lists which were applied vs which were skipped.")
    args = p.parse_args()

    rec_ids = [r.strip() for r in args.rec_ids.split(",") if r.strip()]
    rec_titles: dict[str, str] = {}
    if args.rec_titles_json and Path(args.rec_titles_json).is_file():
        try:
            rec_titles = json.loads(Path(args.rec_titles_json).read_text())
        except Exception:
            pass

    requested_rec_ids = [
        r.strip() for r in (args.requested_rec_ids or "").split(",") if r.strip()
    ] or rec_ids

    ok, detail = send_completion_email(
        agent_id=args.agent_id, rec_ids=rec_ids, site=args.site,
        source_agent=args.source_agent, request_id=args.request_id,
        run_dir=args.run_dir, commit_sha=args.commit_sha, mode=args.mode,
        rec_titles=rec_titles, explicit_to=args.to, sender=args.sender,
        msmtp_account=args.msmtp_account,
        site_config_path=args.site_config,
        site_label=args.site_label,
        dashboard_base=args.dashboard_base,
        status=args.status,
        status_reason=args.status_reason,
        requested_rec_ids=requested_rec_ids,
    )
    print(f"[completion-email] {'sent' if ok else 'skipped/failed'}: {detail}",
          file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
