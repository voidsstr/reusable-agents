"""Search-demand steering signal — what GSC + GA4 say readers actually open.

WHY THIS EXISTS
---------------
Author agents propose topics from trends feeds + editorial gap analysis, but
the strongest predictor of what will earn organic traffic on a site is what
ALREADY earns it there. On specpicks, GA4 shows head-to-head comparison pages
("A vs B: Which RAM Wins in 2026?") out-viewing every other template — yet
nothing fed that fact back into what the authors chose to write next.

This module is the contract between the producer and the consumers:

  * PRODUCER — ``agents/search-demand-agent`` (per-site instances). Pulls
    GSC search analytics + GA4 page views, distills them into a compact
    steering signal, and calls :func:`write_demand`.
  * CONSUMERS — article-proposal agents and the head-to-head agent call
    :func:`read_demand` + :func:`build_prompt_block` and inject the block
    into their LLM user message, exactly like ``seasonal_calendar`` and
    ``trends_signal`` blocks. No block (stale/missing signal) means authors
    behave as they do today — the signal only ever ADDS bias.

Framework-first: the logic lives here; per-site VALUES (GSC property, GA4
property id, URL-template regexes) live in the producer instance's
``site.yaml``. Storage path is per-site under one shared prefix.

Signal payload shape (all keys optional — consumers must tolerate absence):

  {
    "site": "specpicks",
    "generated_at": "2026-08-18T18:20:00+00:00",
    "template_winners": [{"template", "views", "clicks", "impressions",
                          "pages", "views_per_page"}],
    "steer_topics":     [{"topic", "category_slug", "template",
                          "impressions", "clicks", "sample_queries": []}],
    "zero_coverage":    [{"query", "impressions", "position"}],
    "strike_distance":  [{"query", "page", "position", "impressions"}],
    "h2h_hot":          [{"pair", "views" | "impressions", "source"}],
    "ai_assistant_demand": {            # from ai_traffic_log (ai_traffic.py)
        "referral_days", "fetch_days", "article_path_template",
        "clusters":     [{"cluster", "articles", "referrals", "fetches",
                          "referrals_per_100", "fetches_per_100"}],
        "top_products": [{"key", "title", "category", "referrals", "fetches"}],
    },
  }
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

STORAGE_PREFIX = "framework/demand-signal"

# Consumers ignore a signal older than this: steering from a dead producer
# is worse than no steering, because it looks authoritative.
DEFAULT_MAX_AGE_HOURS = 72


def _path(site_id: str) -> str:
    return f"{STORAGE_PREFIX}/{site_id}.json"


def write_demand(storage, site_id: str, payload: dict) -> str:
    """Persist the steering signal for `site_id`. Returns the storage path."""
    body = dict(payload)
    body["site"] = site_id
    body["generated_at"] = datetime.now(timezone.utc).isoformat()
    path = _path(site_id)
    storage.write_json(path, body)
    return path


def read_demand(storage, site_id: str,
                *, max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> Optional[dict]:
    """Return the current signal, or None when missing/stale/corrupt.

    None simply means "no steering available" — callers proceed unsteered.
    """
    try:
        body = storage.read_json(_path(site_id)) or None
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    try:
        ts = datetime.fromisoformat(str(body.get("generated_at", "")))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - ts > timedelta(hours=max_age_hours):
        return None
    return body


def build_prompt_block(sig: dict, *, max_topics: int = 10,
                       max_queries: int = 12) -> str:
    """Render the signal as an LLM prompt section.

    Deliberately compact: the block competes for prompt budget with the
    trends + featured-product sections, and a 40-line block the model
    actually reads beats a 400-line dump it skims.
    """
    if not sig:
        return ""
    lines: list[str] = []
    gen = str(sig.get("generated_at", ""))[:16].replace("T", " ")
    lines.append(f"PROVEN SEARCH DEMAND (GSC+GA4, 28d, generated {gen} UTC) — this is what")
    lines.append("readers of THIS site actually open and search. Weight it above generic trends.")

    winners = sig.get("template_winners") or []
    if winners:
        lines.append("Page-template performance (views per page — the format that wins):")
        for w in winners[:5]:
            lines.append(
                f"  - {w.get('template')}: {w.get('views', 0):,} views / "
                f"{w.get('pages', 0)} pages ({w.get('views_per_page', 0):.0f}/page), "
                f"{w.get('clicks', 0):,} GSC clicks")

    topics = sig.get("steer_topics") or []
    if topics:
        lines.append(f"STEER — propose in these category × format lanes (top {min(max_topics, len(topics))}):")
        for t in topics[:max_topics]:
            qs = ", ".join(repr(q) for q in (t.get("sample_queries") or [])[:3])
            ev = []
            if t.get("impressions"):
                ev.append(f"{t['impressions']:,} impressions")
            if t.get("views"):
                ev.append(f"{t['views']:,} page views")
            if t.get("clicks"):
                ev.append(f"{t['clicks']:,} clicks")
            lines.append(
                f"  - {t.get('topic')} [{t.get('template')}] — "
                + (", ".join(ev) or "weak evidence")
                + (f"; queries: {qs}" if qs else ""))

    zero = sig.get("zero_coverage") or []
    if zero:
        lines.append(f"UNCOVERED DEMAND — searches we do not rank for (new-article candidates):")
        for z in zero[:max_queries]:
            lines.append(f"  - {z.get('query')!r} — {z.get('impressions', 0):,} impressions, "
                         f"best position {z.get('position', '?')}")

    strike = sig.get("strike_distance") or []
    if strike:
        lines.append("STRIKE DISTANCE (pos 6–20 — expand/refresh the existing page, or write the")
        lines.append("stronger sibling article that captures the same query):")
        for s in strike[:max_queries]:
            lines.append(f"  - {s.get('query')!r} → {str(s.get('page', ''))[:70]} "
                         f"(pos {s.get('position', '?')}, {s.get('impressions', 0):,} imp)")

    hot = sig.get("h2h_hot") or []
    if hot:
        lines.append("HOT HEAD-TO-HEAD PAIRS (highest-viewed comparisons + 'X vs Y' searches):")
        for h in hot[:8]:
            n = h.get("views") or h.get("impressions") or 0
            lines.append(f"  - {h.get('pair')} ({n:,} {'views' if h.get('views') else 'imp'}, {h.get('source')})")

    lines.extend(_ai_demand_lines(sig.get("ai_assistant_demand") or {}))

    lines.append("Every proposal that follows a STEER/UNCOVERED row must cite the query it")
    lines.append("targets in its why_now. Do not propose against this data without a reason.")
    return "\n".join(lines)


def _ai_demand_lines(ai: dict, *, max_clusters: int = 8, max_products: int = 8,
                     min_articles: int = 20) -> list[str]:
    """The AI ASSISTANT DEMAND section: which article clusters AI assistants
    actually send people to, per 100 articles already published (a yield —
    raw counts just reward the cluster with the most articles), and which
    single products they land on. Clusters with fewer than `min_articles`
    articles are listed after the rest: one referral on 9 articles is not
    a trend."""
    all_clusters = [c for c in (ai.get("clusters") or []) if c.get("articles")]
    judged = [c for c in all_clusters if c.get("articles", 0) >= min_articles]
    small = [c for c in all_clusters if c.get("articles", 0) < min_articles]
    clusters = judged + small
    products = ai.get("top_products") or []
    if not clusters and not products:
        return []
    out = [f"AI ASSISTANT DEMAND (ChatGPT/Perplexity/Claude/Copilot/Gemini referrals "
           f"{ai.get('referral_days', 90)}d + live user fetches {ai.get('fetch_days', 30)}d, "
           "from the site's own log):"]
    if clusters:
        out.append("Article clusters ranked by referrals per 100 published articles — the "
                   "lane where one more article is most likely to be used by an assistant:")
        for c in clusters[:max_clusters]:
            few = (f"; under {min_articles} articles, too few to judge"
                   if c.get("articles", 0) < min_articles else "")
            out.append(f"  - {c.get('cluster')}: {c.get('referrals_per_100', 0):g} referrals/100 "
                       f"articles ({c.get('referrals', 0)} referrals, {c.get('fetches', 0)} "
                       f"assistant fetches, {c.get('articles', 0)} articles{few})")
        lead = clusters[0]
        out.append(f"A cluster well below {lead.get('cluster')}'s yield "
                   f"({lead.get('referrals_per_100', 0):g}/100) is not a reason to widen the "
                   "topic scope; high raw fetches with low yield mean the lane is already covered.")
    if products:
        out.append("Product pages assistants land on most (single-product lookups — they call "
                   "for PDP / head-to-head work on that product, not a category round-up):")
        for p in products[:max_products]:
            label = p.get("title") or p.get("key")
            cat = f" [{p['category']}]" if p.get("category") else ""
            out.append(f"  - {label}{cat}: {p.get('referrals', 0)} referrals, "
                       f"{p.get('fetches', 0)} fetches")
    return out
