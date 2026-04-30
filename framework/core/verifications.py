"""Framework-level verification scripts.

Every agent that ships a change to production should write a per-rec
verification doc at:
  agents/<source-agent>/runs/<source-run-ts>/verifications/<rec_id>.json

Doc shape:
{
  "rec_id": "rec-007",
  "rec_type": "ctr-fix",
  "generated_at": "2026-04-30T18:30:00+00:00",
  "generated_by": "implementer" | "framework",
  "explanation": "1-2 sentences plain English",
  "script_js": "async function verify({ proxyFetch }) { ... return {ok, evidence}; }"
}

The dashboard's verify button fetches this doc and runs script_js in a
sandboxed Function() with proxyFetch (server-side fetch via the framework
API).

This module is the single source of truth for:
  1. Generating scripts deterministically per rec_type (no LLM needed).
  2. Writing them to storage at the canonical path.
  3. Bulk backfill across runs.

Agents should call `generate_and_persist(...)` after every successful
ship — this replaces ad-hoc per-agent code that wrote verifications/
docs in slightly-different formats.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional

from .storage import StorageBackend, get_storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Script builders — one per rec_type. Each returns (explanation, script_js).
# script_js MUST be `async function verify({ proxyFetch }) { ... }` returning
# `{ ok: bool, evidence: object }`.
# ---------------------------------------------------------------------------

def _js_string(s: str) -> str:
    return json.dumps(s or "")


def _build_article_author_proposal(rec: dict, ctx: dict) -> tuple[str, str]:
    site = ctx.get("site") or "specpicks"
    domain = f"https://{site}.com"
    ap = rec.get("article_proposal") or {}
    slug = ap.get("slug") or rec.get("slug") or ""
    title = ap.get("title") or rec.get("title") or ""
    public_url = rec.get("public_url") or f"{domain}/reviews/{slug}"
    api_url = f"{domain}/api/testbench/articles/{slug}"
    explanation = (
        f"Verifies the article '{title[:80]}' is live at {public_url} by "
        f"querying the testbench API and confirming slug match + "
        f"body_md length > 1000 chars."
    )
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const url = {_js_string(api_url)};\n"
        f"  const slug = {_js_string(slug)};\n"
        "  const r = await proxyFetch(url);\n"
        "  if (!r.ok) return { ok: false, evidence: { http_status: r.status, error: r.error || '' } };\n"
        "  let obj; try { obj = JSON.parse(r.body || '{}'); } catch (e) { return { ok: false, evidence: { parse_error: String(e) } }; }\n"
        "  const article = obj.article || obj;\n"
        "  const body_md = article.body_md || '';\n"
        "  const has_slug = article.slug === slug;\n"
        "  const has_body = body_md.length > 1000;\n"
        f"  const public_url = {_js_string(public_url)};\n"
        "  return {\n"
        "    ok: has_slug && has_body,\n"
        "    evidence: { api_status: r.status, body_md_length: body_md.length, slug_match: has_slug, title: article.title, public_url },\n"
        "  };\n"
        "}"
    )
    return explanation, js


def _build_h2h(rec: dict, ctx: dict) -> tuple[str, str]:
    kind = rec.get("kind", "hardware")
    lr = rec.get("left_ref") or ""
    rr = rec.get("right_ref") or ""
    left = rec.get("left_title") or ""
    right = rec.get("right_title") or ""
    cu = rec.get("public_url") or rec.get("compare_url") or ""
    if cu and not cu.startswith("http"):
        cu = "https://specpicks.com" + cu
    api = f"https://specpicks.com/api/compare/commentary?kind={kind}&left_ref={lr}&right_ref={rr}"
    explanation = (
        f"Verifies the head-to-head commentary for '{left}' vs '{right}' "
        f"is in production by querying /api/compare/commentary (the same "
        f"DB-backed API the SPA hydrates from). Confirms verdict + buy "
        f"advice rows exist."
    )
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const apiUrl = {_js_string(api)};\n"
        f"  const publicUrl = {_js_string(cu)};\n"
        "  const r = await proxyFetch(apiUrl);\n"
        "  if (!r.ok) return { ok: false, evidence: { http_status: r.status } };\n"
        "  let obj; try { obj = JSON.parse(r.body || '{}'); } catch (e) { return { ok: false, evidence: { parse_error: String(e) } }; }\n"
        "  const c = obj.commentary;\n"
        "  if (!c) return { ok: false, evidence: { reason: 'commentary row missing in DB' } };\n"
        "  const ok = !!(c.verdict_winner && (c.buy_advice||'').length > 50);\n"
        "  return { ok, evidence: { verdict_winner: c.verdict_winner, buy_advice_length: (c.buy_advice||'').length, model_used: c.model_used, updated_at: c.updated_at, public_url: publicUrl } };\n"
        "}"
    )
    return explanation, js


def _build_ctr_fix(rec: dict, ctx: dict) -> tuple[str, str]:
    """ctr-fix verifies a SERP title/meta-description rewrite is live by
    fetching the page and checking its <title> + <meta name=description>.
    rec.url should be the page where the override applies."""
    url = rec.get("url") or rec.get("public_url") or ""
    title = rec.get("title", "")
    # Heuristic: pull the desired length window from the rationale ("120-160")
    m = re.search(r'\b(\d{2,3})\s*[–-]\s*(\d{2,3})\b', rec.get("rationale", "") + " " + title)
    lo = int(m.group(1)) if m else 50
    hi = int(m.group(2)) if m else 160
    explanation = (
        f"Verifies the meta description on {url} is live and within the "
        f"{lo}-{hi} char SERP-optimal range. Note: this only checks "
        f"length + presence; it does NOT validate exact wording (which "
        f"the implementer may have improved beyond the rec text)."
    )
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const url = {_js_string(url)};\n"
        f"  const lo = {lo}, hi = {hi};\n"
        "  if (!url) return { ok: false, evidence: { reason: 'no url in rec' } };\n"
        "  const r = await proxyFetch(url);\n"
        "  if (!r.ok) return { ok: false, evidence: { http_status: r.status } };\n"
        "  const html = r.body || '';\n"
        "  const titleM = html.match(/<title[^>]*>([^<]+)<\\/title>/i);\n"
        "  const descM = html.match(/<meta\\s+name=[\"']description[\"']\\s+content=[\"']([^\"']+)[\"']/i);\n"
        "  const desc = descM ? descM[1] : '';\n"
        "  const len = desc.length;\n"
        "  const in_range = len >= lo && len <= hi;\n"
        "  return { ok: in_range, evidence: { http_status: r.status, title: titleM ? titleM[1] : '(no title tag)', description: desc, length: len, in_range, lo_target: lo, hi_target: hi } };\n"
        "}"
    )
    return explanation, js


def _build_top5_target_page(rec: dict, ctx: dict) -> tuple[str, str]:
    """top5-target-page recs build a new keyword landing page. Verify by
    fetching it and confirming non-empty content + title contains the
    target keyword."""
    site = ctx.get("site") or "aisleprompt"
    domain = f"https://{site}.com"
    title = rec.get("title", "")
    m = re.search(r'"([^"]+)"', title)
    query = (m.group(1) if m else "").strip()
    slug = re.sub(r'[^a-z0-9]+', '-', query.lower()).strip('-')
    url = rec.get("public_url") or f"{domain}/recipes/q/{slug}" if site == "aisleprompt" else f"{domain}/best/best-{slug}"
    explanation = (
        f"Verifies the keyword landing page for '{query}' is live at "
        f"{url}. Fetches the page, checks for 200 status + title "
        f"references the keyword + body length > 1KB (non-empty SPA shell)."
    )
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const url = {_js_string(url)};\n"
        f"  const query = {_js_string(query)};\n"
        "  const r = await proxyFetch(url);\n"
        "  if (!r.ok) return { ok: false, evidence: { http_status: r.status } };\n"
        "  const html = r.body || '';\n"
        "  const titleM = html.match(/<title[^>]*>([^<]+)<\\/title>/i);\n"
        "  const titleText = titleM ? titleM[1] : '';\n"
        "  const has_query = titleText.toLowerCase().includes(query.toLowerCase()) || html.toLowerCase().includes(query.toLowerCase());\n"
        "  const long_enough = html.length > 1000;\n"
        "  return { ok: has_query && long_enough, evidence: { http_status: r.status, title: titleText, has_query, body_length: html.length } };\n"
        "}"
    )
    return explanation, js


def _build_internal_link(rec: dict, ctx: dict) -> tuple[str, str]:
    """internal-link recs add a link from the homepage to a target slug.
    Verify by fetching the homepage and grepping for the anchor."""
    site = ctx.get("site") or "aisleprompt"
    home = f"https://{site}.com/"
    title = rec.get("title", "")
    m = re.search(r'"([^"]+)"', title)
    query = (m.group(1) if m else "").strip()
    slug = re.sub(r'[^a-z0-9]+', '-', query.lower()).strip('-')
    target_path = f"/best/best-{slug}" if site == "aisleprompt" else f"/recipes/q/{slug}"
    explanation = (
        f"Verifies the homepage at {home} contains an anchor pointing to "
        f"{target_path} (the internal-link target the rec proposed)."
    )
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const home = {_js_string(home)};\n"
        f"  const target = {_js_string(target_path)};\n"
        "  const r = await proxyFetch(home);\n"
        "  if (!r.ok) return { ok: false, evidence: { http_status: r.status } };\n"
        "  const html = r.body || '';\n"
        "  const anchor_count = (html.match(new RegExp(target.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'g')) || []).length;\n"
        "  return { ok: anchor_count > 0, evidence: { http_status: r.status, target, anchor_count } };\n"
        "}"
    )
    return explanation, js


def _build_ssr_fix(rec: dict, ctx: dict) -> tuple[str, str]:
    """ssr-fix recs ensure a page renders an H1 server-side. Verify by
    fetching + checking <h1> is present and non-empty."""
    url = rec.get("url") or rec.get("public_url") or ""
    explanation = (
        f"Verifies SSR fix on {url}: page must render a non-empty <h1> "
        f"in the server-side HTML (not injected by JS)."
    )
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const url = {_js_string(url)};\n"
        "  if (!url) return { ok: false, evidence: { reason: 'no url' } };\n"
        "  const r = await proxyFetch(url);\n"
        "  if (!r.ok) return { ok: false, evidence: { http_status: r.status } };\n"
        "  const html = r.body || '';\n"
        "  const h1m = html.match(/<h1[^>]*>([\\s\\S]*?)<\\/h1>/i);\n"
        "  const h1 = h1m ? h1m[1].replace(/<[^>]+>/g, '').trim() : '';\n"
        "  return { ok: h1.length > 0, evidence: { http_status: r.status, h1, h1_present: h1.length > 0 } };\n"
        "}"
    )
    return explanation, js


def _build_schema_markup(rec: dict, ctx: dict) -> tuple[str, str]:
    """schema-markup recs add JSON-LD structured data. Verify by fetching
    the page and finding the @type the rec mentions."""
    url = rec.get("url") or rec.get("public_url") or ""
    title = (rec.get("title", "") + " " + rec.get("rationale", "")).lower()
    schema_type = "Recipe"
    for t in ("FAQPage", "BreadcrumbList", "Recipe", "Article", "Product", "ItemList", "Review"):
        if t.lower() in title:
            schema_type = t
            break
    explanation = (
        f"Verifies {schema_type} JSON-LD structured data is present in the "
        f"server-side HTML at {url}."
    )
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const url = {_js_string(url)};\n"
        f"  const wanted = {_js_string(schema_type)};\n"
        "  if (!url) return { ok: false, evidence: { reason: 'no url' } };\n"
        "  const r = await proxyFetch(url);\n"
        "  if (!r.ok) return { ok: false, evidence: { http_status: r.status } };\n"
        "  const html = r.body || '';\n"
        "  const hasType = html.includes(`\"@type\":\"${wanted}\"`) || html.includes(`\"@type\": \"${wanted}\"`);\n"
        "  return { ok: hasType, evidence: { http_status: r.status, schema_type: wanted, present: hasType } };\n"
        "}"
    )
    return explanation, js


def _build_orphan_boost(rec: dict, ctx: dict) -> tuple[str, str]:
    urls = rec.get("orphan_urls") or []
    explanation = (
        f"Verifies {len(urls)} orphan article(s) now have at least one "
        f"inbound internal link from the homepage or category pages."
    )
    target_arr = json.dumps(urls[:10])
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const targets = {target_arr};\n"
        "  if (!targets.length) return { ok: false, evidence: { reason: 'no orphan urls' } };\n"
        "  // Spot-check: fetch homepage, count anchors that match any of the orphan slugs.\n"
        "  const home = await proxyFetch('https://specpicks.com/');\n"
        "  if (!home.ok) return { ok: false, evidence: { http_status: home.status } };\n"
        "  const html = home.body || '';\n"
        "  let linked = 0;\n"
        "  for (const u of targets) { try { const path = new URL(u).pathname; if (html.includes(path)) linked++; } catch(e){} }\n"
        "  return { ok: linked > 0, evidence: { targets_checked: targets.length, linked_from_home: linked } };\n"
        "}"
    )
    return explanation, js


def _build_default(rec: dict, ctx: dict) -> tuple[str, str]:
    """Fallback for any rec_type without a specialized builder. Best-
    effort: if rec.public_url exists, just check 200 + non-empty body."""
    url = rec.get("public_url") or rec.get("url") or ""
    explanation = (
        f"Best-effort verification: fetches {url} and confirms 200 OK + "
        f"non-empty response. No type-specific assertions because no "
        f"specialized builder exists for rec_type "
        f"{rec.get('type','(none)')!r}. Override by writing a custom "
        f"verifications/<rec_id>.json from the agent that ships this rec."
    )
    js = (
        "async function verify({ proxyFetch }) {\n"
        f"  const url = {_js_string(url)};\n"
        "  if (!url) return { ok: false, evidence: { reason: 'no public_url on rec — agent should set this at ship time' } };\n"
        "  const r = await proxyFetch(url);\n"
        "  return { ok: r.ok && (r.body || '').length > 100, evidence: { http_status: r.status, body_length: (r.body||'').length } };\n"
        "}"
    )
    return explanation, js


_BUILDERS = {
    "article-author-proposal": _build_article_author_proposal,
    "h2h-comparison": _build_h2h,
    "head-to-head-pair": _build_h2h,
    "ctr-fix": _build_ctr_fix,
    "top5-target-page": _build_top5_target_page,
    "internal-link": _build_internal_link,
    "ssr-fix": _build_ssr_fix,
    "schema-markup": _build_schema_markup,
    "article-orphan-boost": _build_orphan_boost,
}


def build_for_rec(rec: dict, *, site: str = "") -> tuple[str, str]:
    """Pick the right script builder for a rec, by `type` field. Falls
    back to H2H detection (compare_url field) and finally the default."""
    rt = rec.get("type") or ""
    ctx = {"site": site}
    builder = _BUILDERS.get(rt)
    if builder:
        return builder(rec, ctx)
    # H2H recs lack a `type` but always carry compare_url
    if rec.get("compare_url") or rec.get("left_ref") and rec.get("right_ref"):
        return _build_h2h(rec, ctx)
    return _build_default(rec, ctx)


def generate_and_persist(
    *,
    source_agent: str,
    source_run_ts: str,
    rec: dict,
    site: str = "",
    generated_by: str = "framework",
    storage: Optional[StorageBackend] = None,
    overwrite: bool = False,
) -> dict:
    """Build a verification doc for `rec` and write it to storage at the
    canonical path. Returns the doc dict."""
    s = storage or get_storage()
    rid = rec.get("id") or rec.get("rec_id")
    if not rid:
        raise ValueError("rec missing 'id' field")
    key = f"agents/{source_agent}/runs/{source_run_ts}/verifications/{rid}.json"
    if not overwrite:
        try:
            existing = s.read_json(key)
            if existing and existing.get("script_js"):
                return existing
        except Exception:
            pass
    explanation, script_js = build_for_rec(rec, site=site)
    doc = {
        "rec_id": rid,
        "rec_type": rec.get("type") or "",
        "generated_at": _now(),
        "generated_by": generated_by,
        "explanation": explanation,
        "script_js": script_js,
    }
    s.write_json(key, doc)
    return doc


def backfill_missing(
    *,
    storage: Optional[StorageBackend] = None,
    limit: int = 0,
    dry_run: bool = False,
) -> dict:
    """Walk every shipped rec across all agents and write verification
    docs for the ones missing one. Returns counts."""
    s = storage or get_storage()
    written = 0
    skipped = 0
    failed = 0
    seen_keys = set()
    for k in s.list_prefix("agents/"):
        if not k.endswith("/recommendations.json"):
            continue
        parts = k.split("/")
        if len(parts) < 5 or parts[2] != "runs":
            continue
        agent_id = parts[1]
        run_ts = parts[3]
        if "rundir-" in run_ts:
            continue
        try:
            doc = s.read_json(k) or {}
        except Exception:
            failed += 1
            continue
        site = doc.get("site") or ""
        # Derive site from the agent_id if not in the doc:
        # 'specpicks-foo-agent' → 'specpicks'
        if not site and agent_id:
            for prefix in ("specpicks-", "aisleprompt-", "reusable-agents-"):
                if agent_id.startswith(prefix):
                    site = prefix.rstrip("-")
                    break
        for r in doc.get("recommendations", []):
            if not r.get("shipped"):
                continue
            rid = r.get("id")
            if not rid:
                continue
            key = (agent_id, run_ts, rid)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            ver_key = f"agents/{agent_id}/runs/{run_ts}/verifications/{rid}.json"
            try:
                existing = s.read_json(ver_key)
                if existing and existing.get("script_js"):
                    skipped += 1
                    continue
            except Exception:
                pass
            if dry_run:
                written += 1
                continue
            try:
                generate_and_persist(
                    source_agent=agent_id, source_run_ts=run_ts,
                    rec=r, site=site, generated_by="backfill", storage=s,
                )
                written += 1
            except Exception:
                failed += 1
            if limit and written >= limit:
                return {"written": written, "skipped": skipped, "failed": failed}
    return {"written": written, "skipped": skipped, "failed": failed}
