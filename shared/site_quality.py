"""Shared helpers for progressive-improvement-agent and competitor-research-agent.

Both agents follow the "site analyzer + email recommendations + reply gates
implementation" pattern. This module centralizes:

  - Config loading + JSON-schema validation
  - Recommendation tier scoring (auto / review / experimental)
  - HTML email rendering with rec-id reply syntax
  - Response-queue parsing (implement/skip/modify/merge rec-NNN)
  - Dispatch of auto-tier recs to a downstream implementer agent

Importing:
    from shared.site_quality import (
        load_quality_config, score_tier, render_recs_email,
        apply_user_responses, dispatch_auto_recs,
    )
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError as e:
    raise SystemExit("PyYAML not installed. pip install pyyaml jsonschema") from e
try:
    import jsonschema
except ImportError as e:
    raise SystemExit("jsonschema not installed. pip install jsonschema") from e


_HERE = Path(__file__).resolve().parent
SCHEMA_PATH = _HERE / "schemas" / "site-quality-config.schema.json"
RECS_SCHEMA_PATH = _HERE / "schemas" / "quality-recommendations.schema.json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class SiteQualityConfig(dict):
    """Thin dict wrapper with a few path/derived helpers."""

    @property
    def site_id(self) -> str:
        return self["site"]["id"]

    @property
    def domain(self) -> str:
        return self["site"]["domain"]

    @property
    def base_url(self) -> str:
        return self["site"].get("base_url") or f"https://{self.domain}"

    @property
    def label(self) -> str:
        return self["site"].get("label") or self.site_id

    @property
    def what_we_do(self) -> str:
        return self["site"].get("what_we_do", "").strip()

    def runs_root_for(self, agent_id: str) -> Path:
        custom = self.get("runs_root")
        root = Path(os.path.expanduser(custom)) if custom \
            else Path.home() / ".reusable-agents" / agent_id / "runs"
        return root

    def run_dir_for_now(self, agent_id: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        d = self.runs_root_for(agent_id) / self.site_id / ts
        d.mkdir(parents=True, exist_ok=True)
        return d


def load_quality_config(path: str | os.PathLike) -> SiteQualityConfig:
    """Load + validate a site-quality YAML config."""
    p = Path(os.path.expanduser(path)).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"site config not found: {p}")
    raw = yaml.safe_load(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a YAML mapping: {p}")
    schema = json.loads(SCHEMA_PATH.read_text())
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as e:
        path_s = ".".join(str(x) for x in e.absolute_path)
        raise ValueError(f"config invalid at {path_s}: {e.message}") from e
    return SiteQualityConfig(raw)


def load_quality_config_from_env(env_var: str = "PROGRESSIVE_IMPROVEMENT_CONFIG") -> SiteQualityConfig:
    """Load config from a path stored in an env var. Used by entry_command shells."""
    path = os.environ.get(env_var)
    if not path:
        raise SystemExit(f"set {env_var} to a path to your site config YAML")
    return load_quality_config(path)


# ---------------------------------------------------------------------------
# Recommendation tier scoring
# ---------------------------------------------------------------------------

_AUTO_SEVERITIES = frozenset({"medium", "high", "critical"})


def score_tier(*, confidence: float, severity: str, threshold: float) -> str:
    """Decide the auto/review/experimental tier for a single rec."""
    if confidence >= threshold and severity in _AUTO_SEVERITIES:
        return "auto"
    if confidence < 0.5:
        return "experimental"
    return "review"


def assign_rec_ids(recs: list[dict]) -> list[dict]:
    """Assign rec-001..rec-NNN ids in the order given."""
    for i, r in enumerate(recs, start=1):
        r["id"] = f"rec-{i:03d}"
    return recs


def validate_recs_doc(doc: dict) -> None:
    """Validate against quality-recommendations.schema.json. Raises on failure."""
    schema = json.loads(RECS_SCHEMA_PATH.read_text())
    jsonschema.validate(doc, schema)


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

def _send_via_graph_simple(*, subject: str, body_html: str, to: list[str],
                           sender: str,
                           extra_headers: Optional[dict[str, str]] = None,
                           oauth_file: Optional[str] = None) -> tuple[bool, str]:
    """Send an HTML email via Microsoft Graph /sendMail using the responder's
    refresh token. Used as the primary path because the host's msmtp is
    sandboxed by AppArmor and can't exec the python3 OAuth passwordeval."""
    import json as _j
    import os as _os
    import sys as _s
    import urllib.error
    import urllib.request as _ur
    from pathlib import Path as _P

    here = _P(__file__).resolve().parent
    repo_root = here.parent
    mint_path = repo_root / "agents" / "responder-agent"
    _s.path.insert(0, str(mint_path))
    try:
        from importlib import import_module
        mint = import_module("mint-token")
        oauth_path = _P(_os.path.expanduser(
            oauth_file or "~/.reusable-agents/responder/.oauth.json"
        ))
        if not oauth_path.is_file():
            return False, f"no oauth file at {oauth_path}"
        token, oauth_user, _ = mint.mint_access_token(
            oauth_path,
            scope_override="offline_access https://graph.microsoft.com/Mail.Send",
        )
    finally:
        try: _s.path.remove(str(mint_path))
        except ValueError: pass

    sender_addr = sender
    if "<" in sender and ">" in sender:
        sender_addr = sender.split("<", 1)[1].split(">", 1)[0]

    # Graph requires internetMessageHeaders names to start with "x-"; non-x
    # headers (like Reply-To) get mapped to native message fields. Only
    # include the field at all when there's at least one valid x-* header
    # — passing an empty list returns HTTP 400 InvalidInternetMessageHeaderCollection.
    headers = extra_headers or {}
    x_headers = [(k, v) for k, v in headers.items() if k.lower().startswith("x-")]
    msg = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body_html},
        "toRecipients": [{"emailAddress": {"address": a.strip()}} for a in to if a.strip()],
    }
    if x_headers:
        msg["internetMessageHeaders"] = [{"name": k, "value": v} for k, v in x_headers]
    if "Reply-To" in headers:
        # Graph requires a bare email address (no display-name + angle
        # brackets). Strip any "Display Name <addr@x>" wrapper before
        # passing through, otherwise Graph 400s with
        # ErrorParticipantDoesntHaveAnEmailAddress.
        reply_to_raw = headers["Reply-To"]
        if "<" in reply_to_raw and ">" in reply_to_raw:
            reply_to_addr = reply_to_raw.split("<", 1)[1].split(">", 1)[0]
        else:
            reply_to_addr = reply_to_raw.strip()
        if reply_to_addr:
            msg["replyTo"] = [{"emailAddress": {"address": reply_to_addr}}]
    attempts = [
        ("send_as", f"https://graph.microsoft.com/v1.0/users/{sender_addr}/sendMail",
         {"message": msg, "saveToSentItems": True}),
        ("send_on_behalf", "https://graph.microsoft.com/v1.0/me/sendMail",
         {"message": dict(msg, **{"from": {"emailAddress": {"address": sender_addr}}}),
          "saveToSentItems": True}),
        ("self", "https://graph.microsoft.com/v1.0/me/sendMail",
         {"message": msg, "saveToSentItems": True}),
    ]
    last_err = ""
    for method, url, payload in attempts:
        body = _j.dumps(payload).encode()
        # Per-method retry: 3 attempts on transient failures (5xx, timeouts,
        # SSL handshake errors, rate limits). Permanent failures (4xx other
        # than 429) fall through immediately to try the next method.
        retry_attempts = 3
        for retry_i in range(retry_attempts):
            req = _ur.Request(url, data=body, method="POST",
                              headers={"Authorization": f"Bearer {token}",
                                       "Content-Type": "application/json"})
            try:
                with _ur.urlopen(req, timeout=30) as resp:
                    if resp.status == 202:
                        return True, f"graph:{method}" + (f" (retry {retry_i})" if retry_i else "")
                    last_err = f"graph:{method} unexpected status {resp.status}"
                    break  # not transient
            except urllib.error.HTTPError as e:
                code = e.code
                err_body = e.read().decode(errors='replace')[:200]
                last_err = f"graph:{method} HTTP {code}: {err_body}"
                # Retry on 429 (rate limit) and 5xx (transient server error)
                if code == 429 or 500 <= code < 600:
                    if retry_i < retry_attempts - 1:
                        # Honor Retry-After if present, else exponential backoff
                        try:
                            ra = float(e.headers.get("Retry-After", "0"))
                        except Exception:
                            ra = 0
                        delay = ra if ra > 0 else (1.5 ** retry_i)
                        import time as _t
                        _t.sleep(delay)
                        continue
                break  # permanent
            except (TimeoutError, urllib.error.URLError) as e:
                last_err = f"graph:{method} {type(e).__name__}: {e}"
                if retry_i < retry_attempts - 1:
                    import time as _t
                    _t.sleep(1.5 ** retry_i)
                    continue
                break
            except Exception as e:
                last_err = f"graph:{method} {type(e).__name__}: {e}"
                break
    return False, last_err


def send_via_msmtp(
    *,
    subject: str,
    body_html: str,
    to: list[str],
    sender: str,
    msmtp_account: str = "automation",
    extra_headers: Optional[dict[str, str]] = None,
    bypass_digest: bool = False,
) -> tuple[bool, str]:
    """Send an HTML email. Despite the legacy name, tries Microsoft Graph
    sendMail first (host's msmtp is sandboxed by AppArmor and can't exec the
    OAuth passwordeval), then falls back to msmtp. Caller doesn't need to
    care which path is taken; success looks identical.

    Digest mode (set DIGEST_ONLY=1, default): all individual agent emails
    are SUPPRESSED — the rollup-digest agent fires every 3h with a single
    consolidated email instead. Callers that genuinely need to send (the
    digest agent itself, the responder for confirmation flows the user
    explicitly opted into, etc) pass bypass_digest=True.

    To re-enable individual emails, set DIGEST_ONLY=0 in the agent host's
    environment.
    """
    import subprocess
    import os as _os
    from email.utils import formatdate, make_msgid

    if not to:
        return False, "no recipients"

    # Digest gate — suppress everything except explicitly bypassed senders.
    # Was added 5-1 after the user reported "too many emails". The
    # rollup-digest agent runs every 3h and packages all this content
    # into one summary email.
    if not bypass_digest and _os.environ.get("DIGEST_ONLY", "1") == "1":
        # Persist the would-have-been email to digest-queue/ so the
        # digest agent can include it in the next rollup. Each entry is
        # a small JSON file: subject + 1KB body excerpt + agent + ts.
        try:
            import json, datetime, hashlib
            from .storage import get_storage  # local import — avoids
            # the storage layer initializing at module import time.
        except Exception:
            try:
                from framework.core.storage import get_storage  # type: ignore
            except Exception:
                get_storage = None  # type: ignore
        try:
            if get_storage is not None:
                s = get_storage()
                ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                key_hash = hashlib.sha1(f"{subject}|{ts}".encode()).hexdigest()[:10]
                # Pull agent from headers if present
                agent_hint = (extra_headers or {}).get("X-Reusable-Agent", "")
                body_excerpt = body_html[:30000]  # cap so the queue is bounded
                s.write_json(
                    f"digest-queue/{ts}-{key_hash}.json",
                    {"ts": ts, "agent": agent_hint, "to": to, "sender": sender,
                     "subject": subject, "body_html": body_excerpt,
                     "extra_headers": extra_headers or {}},
                )
        except Exception as _e:
            # Logging the failure but never blocking — if the queue is
            # broken, just silently suppress. Better than spamming.
            print(f"[digest-queue] suppress {subject[:60]!r}: {_e}",
                  file=__import__('sys').stderr)
        return True, "suppressed: digest-mode"

    # Prefer Graph if oauth file is available
    import os as _os
    if _os.path.isfile(_os.path.expanduser("~/.reusable-agents/responder/.oauth.json")):
        ok, detail = _send_via_graph_simple(
            subject=subject, body_html=body_html, to=to,
            sender=sender, extra_headers=extra_headers,
        )
        if ok:
            return True, detail

    msg_lines = [
        f"From: {sender}",
        f"To: {', '.join(to)}",
        f"Subject: {subject}",
        f"Date: {formatdate(localtime=True)}",
        f"Message-ID: {make_msgid()}",
        "MIME-Version: 1.0",
        "Content-Type: text/html; charset=utf-8",
    ]
    for k, v in (extra_headers or {}).items():
        msg_lines.append(f"{k}: {v}")
    msg_lines.append("")  # blank line
    msg_lines.append(body_html)
    msg_bytes = "\r\n".join(msg_lines).encode("utf-8")

    try:
        proc = subprocess.run(
            ["msmtp", "-a", msmtp_account, *to],
            input=msg_bytes,
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            return False, f"msmtp rc={proc.returncode}: {proc.stderr.decode('utf-8','replace')[:300]}"
        return True, f"sent to {len(to)} recipient(s)"
    except FileNotFoundError:
        return False, "msmtp not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "msmtp timed out"
    except Exception as e:
        return False, f"msmtp error: {e}"


_TIER_BADGE = {
    "auto": ("🟢", "#16a34a", "auto-eligible"),
    "review": ("🟡", "#ca8a04", "needs review"),
    "experimental": ("🔵", "#2563eb", "speculative"),
}
_SEVERITY_COLOR = {
    "critical": "#dc2626", "high": "#ea580c",
    "medium": "#ca8a04", "low": "#65a30d",
}


def render_recs_email(
    *,
    cfg: SiteQualityConfig,
    agent_id: str,
    request_id: str,
    recs: list[dict],
    summary: str = "",
    extra_intro_html: str = "",
    auto_queued: bool = False,
) -> tuple[str, str]:
    """Return (subject, html_body) for an email summarizing recommendations.

    Subject is prefixed with [<agent-id>:<request-id>] so the responder can
    route replies back. Body includes rec-id reply syntax instructions.

    auto_queued=True swaps the "Reply to ship" block for an "auto-queued"
    confirmation block. Used by agents (PI, SEO) that drop a trigger file
    in agents/responder-agent/auto-queue/ at the same time they send the
    email — recipient replies only to override (defer/skip/revert).
    """
    site = cfg.site_id
    label = cfg.label
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    template = cfg.get("reporter", {}).get("email", {}).get(
        "subject_template",
        f"{agent_id} — {{site}} — {{tag}}",
    )
    try:
        subject_inner = template.format(site=site, label=label, tag=tag,
                                          recs_count=len(recs))
    except KeyError:
        subject_inner = f"{agent_id} — {site} — {tag}"
    subject = f"[{agent_id}:{request_id}] {subject_inner}"

    rec_rows = []
    for r in recs:
        sev = r.get("severity", "low")
        sev_color = _SEVERITY_COLOR.get(sev, "#64748b")
        tier = r.get("tier", "review")
        emoji, tier_color, tier_label = _TIER_BADGE.get(tier, ("•", "#64748b", tier))
        conf_pct = int(round(float(r.get("confidence", 0)) * 100))
        evidence_html = ""
        for e in r.get("evidence", [])[:3]:
            url = e.get("url", "")
            snip = (e.get("snippet") or "").strip()
            if url:
                evidence_html += (
                    f'<div style="font-size:12px;color:#475569;margin-top:4px">'
                    f'<a href="{url}" style="color:#2563eb">{url}</a>'
                    f'{(": " + snip[:140]) if snip else ""}'
                    f"</div>"
                )
        rec_rows.append(f"""
            <tr>
              <td style="vertical-align:top;padding:12px 14px;border-bottom:1px solid #e2e8f0;width:80px">
                <div style="font-family:monospace;font-size:13px;color:#0f172a"><b>{r['id']}</b></div>
                <div style="font-size:11px;color:{sev_color};margin-top:2px;text-transform:uppercase">{sev}</div>
                <div style="font-size:11px;color:{tier_color};margin-top:4px">{emoji} {tier_label}</div>
                <div style="font-size:11px;color:#64748b;margin-top:2px">conf {conf_pct}%</div>
              </td>
              <td style="vertical-align:top;padding:12px 14px;border-bottom:1px solid #e2e8f0">
                <div style="font-weight:600;color:#0f172a;font-size:14px">{r.get('title', '')}</div>
                <div style="color:#334155;font-size:13px;margin-top:4px;line-height:1.45">{r.get('rationale', '')}</div>
                {evidence_html}
              </td>
            </tr>""")

    auto_count = sum(1 for r in recs if r.get("tier") == "auto")
    review_count = sum(1 for r in recs if r.get("tier") == "review")
    exp_count = sum(1 for r in recs if r.get("tier") == "experimental")

    # Header restyled to match the article-author proposal email (the
    # reference visual style across all agent emails). Clean white
    # container, h1 with bottom-border, soft summary stats, no dark navy.
    header = f"""
        <h1 style="border-bottom:1px solid #e2e8f0;padding:0 0 12px 0;margin:0 0 12px 0;font-size:22px;color:#0f172a">
          {label} <span style="color:#64748b;font-weight:normal;font-size:14px">— {agent_id}</span>
        </h1>
        <p style="color:#475569;font-size:13px;margin:0 0 14px 0">
          <b style="color:#0f172a">{len(recs)} recommendation(s)</b> ·
          🟢 {auto_count} auto-eligible · 🟡 {review_count} review · 🔵 {exp_count} speculative
        </p>
    """
    if summary:
        header += f"""<p style="color:#334155;font-size:13px;margin:0 0 16px 0">{summary}</p>"""
    if extra_intro_html:
        header += extra_intro_html

    if auto_queued:
        reply_help = f"""
        <div style="padding:16px 20px;background:#ecfdf5;color:#065f46;font-size:13px;line-height:1.6;border-left:3px solid #10b981">
          <b>✅ All recommendations have been auto-queued for implementation.</b>
          <span style="color:#047857;font-size:12px">
            The implementer will pick them up on the responder's next tick (~60s)
            and process in priority order. Track progress in the dashboard.
          </span>
          <br><br>
          <b>Reply only to override:</b><br>
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">defer rec-002</code> &nbsp;
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">skip rec-005</code> &nbsp;
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">revert rec-007</code>
          <br>
          <span style="color:#64748b;font-size:12px">Subject must stay <code>Re: …</code>. Defer/skip removes a not-yet-started rec from the queue; revert rolls back a shipped rec by reverting its commit.</span>
        </div>
    """
    else:
        reply_help = f"""
        <div style="padding:16px 20px;background:#f1f5f9;color:#334155;font-size:13px;line-height:1.6">
          <b>Reply to ship recommendations.</b> Subject must stay <code>Re: …</code>.
          <br><br>
          <b>By rec id (most precise):</b><br>
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">implement rec-001 rec-005</code> &nbsp;
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">skip rec-002</code> &nbsp;
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">merge rec-003 rec-004</code>
          <br><br>
          <b>Bulk by tier or severity:</b><br>
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">implement all</code> &nbsp;
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">implement auto</code> &nbsp;
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">implement high</code> &nbsp;
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">implement critical and high</code> &nbsp;
          <code style="background:#fff;padding:2px 6px;border:1px solid #e2e8f0;border-radius:3px">skip experimental</code>
          <br>
          <span style="color:#64748b;font-size:12px">Tier filters: <code>auto</code>, <code>review</code>, <code>experimental</code>. Severity filters: <code>critical</code>, <code>high</code>, <code>medium</code>, <code>low</code>. <code>all</code> matches everything. Combine with <code>and</code> / <code>+</code> / commas.</span>
          <br><br>
          Auto-eligible recs ship without a reply <i>only</i> if you've enabled
          <code>auto_implement: true</code> in the site config (off by default).
        </div>
    """

    # Body container restyled to match article-author proposal email style:
    # white background, max-width 920px, padding, no surrounding border.
    body = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#0f172a;line-height:1.5;max-width:920px;margin:0 auto;padding:24px;background:#fff">
    {header}
    {reply_help}
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:16px">
      {''.join(rec_rows) if rec_rows else '<tr><td style="padding:20px;color:#64748b">No recommendations this run — site is clean.</td></tr>'}
    </table>
    <hr style="margin:32px 0 16px;border:none;border-top:1px solid #e2e8f0">
    <p style="color:#94a3b8;font-size:11px;margin:0">
      Generated by <code>{agent_id}</code> · request <code>{request_id}</code>
    </p>
</body></html>
"""
    return subject, body


# ---------------------------------------------------------------------------
# Response-queue handling (replies the responder dropped into our queue)
# ---------------------------------------------------------------------------

_REC_ID_RE = re.compile(r"\brec-(\d{3})\b")


_VERBS = ("implement", "skip", "modify", "merge")
_TIERS = ("auto", "review", "experimental")
_SEVERITIES = ("critical", "high", "medium", "low")
_BULK_KEYWORDS = ("all",) + _TIERS + _SEVERITIES


def parse_user_action(payload: dict) -> tuple[str, list[str], list[str], str]:
    """From a responses-queue/<request-id>.json payload, return
    (verb, rec_ids, filter_keywords, notes).

    verb           ∈ {implement, skip, modify, merge, unknown}
    rec_ids        ∈ explicit "rec-NNN" tokens in the body (precise selection)
    filter_keywords∈ tier or severity filters (bulk selection):
                       'all'                                  — every rec
                       'auto', 'review', 'experimental'       — by tier
                       'critical', 'high', 'medium', 'low'    — by severity
                     Multiple keywords combine as a UNION
                     ("implement high and critical" → both).
    notes          ∈ first 500 chars of the body, lowercased

    Both rec_ids and filter_keywords can be present — the agent applies the
    union when expanding to actual rec ids.
    """
    body = (payload.get("body") or payload.get("text") or "").lower()
    rec_ids = sorted({f"rec-{m.group(1)}" for m in _REC_ID_RE.finditer(body)})
    verb = "unknown"
    for v in _VERBS:
        if re.search(rf"\b{v}\b", body):
            verb = v
            break
    # Capture bulk filter keywords. Match "implement all" / "implement high and low" / etc.
    # We scan the whole body for these tokens AFTER a verb has been seen.
    filter_keywords: list[str] = []
    if verb in ("implement", "skip"):
        # Take the substring from the verb onward to avoid false positives in
        # quoted prior-run text below the user's reply.
        m = re.search(rf"\b{verb}\b", body)
        scan = body[m.end():] if m else body
        # Stop at common reply boundaries (forwarded original message, etc.)
        for boundary in ("\n\n--", "\n\n>", "\n----", "from:", "sent:"):
            i = scan.find(boundary)
            if i > 0:
                scan = scan[:i]
        for kw in _BULK_KEYWORDS:
            if re.search(rf"\b{kw}\b", scan):
                filter_keywords.append(kw)
    notes = body[:500]
    return verb, rec_ids, filter_keywords, notes


def expand_filters_to_rec_ids(
    recs: list[dict], filter_keywords: list[str]
) -> list[str]:
    """Given a recs list + filter keywords (all / auto|review|experimental /
    critical|high|medium|low), return the union of matching rec ids."""
    if not filter_keywords:
        return []
    out: set[str] = set()
    for r in recs:
        if "all" in filter_keywords:
            out.add(r["id"]); continue
        if r.get("tier") in filter_keywords:
            out.add(r["id"]); continue
        if r.get("severity") in filter_keywords:
            out.add(r["id"]); continue
        # Legacy SEO recs use `priority` instead of `severity`/`tier`
        if r.get("priority") in filter_keywords:
            out.add(r["id"]); continue
    return sorted(out)


def apply_user_responses(
    *,
    responses: list[dict],
    prior_recs_path: Optional[Path],
) -> list[dict]:
    """Apply user-reply choices back to the previous run's recommendations.json.
    Mutates + writes the file in-place. Returns the list of (rec_id, action)
    pairs applied (precise rec-id matches AND bulk-filter expansions)."""
    if prior_recs_path is None or not prior_recs_path.is_file():
        return []
    doc = json.loads(prior_recs_path.read_text())
    recs = doc.get("recommendations", [])
    by_id = {r["id"]: r for r in recs}
    applied: list[dict] = []
    for resp in responses:
        verb, rec_ids, filter_keywords, notes = parse_user_action(resp)
        if verb == "unknown":
            continue
        target_ids: set[str] = set(rec_ids)
        target_ids.update(expand_filters_to_rec_ids(recs, filter_keywords))
        if not target_ids:
            continue
        for rid in sorted(target_ids):
            r = by_id.get(rid)
            if not r:
                continue
            r["user_response"] = {
                "action": verb,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "notes": notes,
                "matched_via": "explicit-rec-id" if rid in rec_ids else "bulk-filter",
                "filters": filter_keywords or None,
            }
            applied.append({"id": rid, "action": verb})
    prior_recs_path.write_text(json.dumps(doc, indent=2))
    return applied


# ---------------------------------------------------------------------------
# Auto-tier dispatch to downstream implementer
# ---------------------------------------------------------------------------

def dispatch_auto_recs(
    *,
    cfg: SiteQualityConfig,
    agent_id: str,
    recs: list[dict],
    storage,
) -> list[str]:
    """If cfg.auto_implement is true, drop a message into the implementer
    agent's response-queue with the auto-tier rec ids. Returns the list of
    rec ids dispatched. No-op (returns []) if auto_implement is false."""
    if not cfg.get("auto_implement"):
        return []
    impl = cfg.get("implementer") or {}
    target = impl.get("agent_id", "implementer")
    auto_recs = [r for r in recs if r.get("tier") == "auto"
                 and not r.get("user_response")]
    if not auto_recs:
        return []
    rec_ids = [r["id"] for r in auto_recs]
    msg_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-auto-{agent_id}"
    payload = {
        "schema_version": "1",
        "request_id": msg_id,
        "from_agent": agent_id,
        "site": cfg.site_id,
        "action": "implement",
        "rec_ids": rec_ids,
        "auto_dispatched": True,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "notes": f"auto-tier dispatch from {agent_id}",
    }
    key = f"agents/{target}/responses-queue/{msg_id}.json"
    storage.write_json(key, payload)
    return rec_ids
