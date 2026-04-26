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
) -> tuple[str, str]:
    """Return (subject, html_body) for an email summarizing recommendations.

    Subject is prefixed with [<agent-id>:<request-id>] so the responder can
    route replies back. Body includes rec-id reply syntax instructions.
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

    header = f"""
        <div style="padding:16px 20px;background:#0f172a;color:#f1f5f9">
          <div style="font-size:18px;font-weight:600">{label} — {agent_id}</div>
          <div style="font-size:13px;color:#94a3b8;margin-top:4px">
            {len(recs)} recommendations · 🟢 {auto_count} auto-eligible
            · 🟡 {review_count} review · 🔵 {exp_count} speculative
          </div>
        </div>
    """
    if summary:
        header += f"""<div style="padding:14px 20px;background:#f8fafc;color:#334155;font-size:13px;border-bottom:1px solid #e2e8f0">{summary}</div>"""
    if extra_intro_html:
        header += extra_intro_html

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

    body = f"""<!DOCTYPE html>
<html><body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#0f172a;background:#fff">
  <div style="max-width:760px;margin:0 auto;border:1px solid #e2e8f0;border-radius:6px;overflow:hidden">
    {header}
    {reply_help}
    <table style="width:100%;border-collapse:collapse">
      {''.join(rec_rows) if rec_rows else '<tr><td style="padding:20px;color:#64748b">No recommendations this run — site is clean.</td></tr>'}
    </table>
    <div style="padding:14px 20px;background:#f8fafc;color:#94a3b8;font-size:11px;border-top:1px solid #e2e8f0">
      Generated by <code>{agent_id}</code> · run-id <code>{request_id}</code>
    </div>
  </div>
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
    target = impl.get("agent_id", "seo-implementer")
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
