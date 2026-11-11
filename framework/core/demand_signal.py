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
                          "pages", "views_per_page"}],   # GA4 views include crawlers
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
    "conversion_winners": {             # first-party affiliate clicks that pass
        "label",                        #   framework/core/human_clicks.py
        "window_days", "trend_days",    # 30, 7
        "definition",                   # which rows count, in words
        "total":      {"d30", "d7", "prev7"},
        "templates":  [{"template", "clicks", "d7", "prev7", "pages",
                        "articles", "article_clicks", "clicks_per_100"}],
        "placements": [{"placement", "clicks", "d7", "prev7"}],
        "clusters":   [{"cluster", "articles", "clicks", "d7",
                        "clicks_per_100"}],   # via ai_traffic.cluster_yield
        "categories": [{"category", "clicks", "d7", "prev7", "products",
                        "value"}],
        "products":   [{"key", "title", "category", "price", "link", "active",
                        "clicks", "d7", "prev7"}],
        "pages":      [{"path", "template", "clicks", "d7", "prev7"}],
    },
  }

In ``conversion_winners`` "clicks" / "d30" count the last ``window_days``,
"d7" the last ``trend_days`` and "prev7" the ``trend_days`` before those.
"articles" / "article_clicks" / "clicks_per_100" appear only where published
articles map to the row; a template's yield counts only the clicks on its
published-article paths ("article_clicks"), not on other pages its pattern
also matches. "value" (summed clicked list price) and "price" appear only when
the producer's product lookup returns a price, "link" (the internal path to
cite) only when it returns one for a product the site still lists, and
"active": false marks a product whose catalog row is inactive (no link; the
block never asks for a link to it). GA4 views include crawlers, so
``template_winners`` measures reach; ``conversion_winners`` measures what sold.

"WHAT SELLS clusters" are the article clusters :func:`top_conversion_clusters`
returns: at least one click, enough articles or clicks to judge, a yield at or
above the site's average article, best first, at most
:data:`WHAT_SELLS_TOP_CLUSTERS`. The block lists exactly those under that name
and names them in its RULE, and the producer's steering-share metric counts
the same set, so the prompt and the metric cannot disagree.
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
    # WHAT SELLS leads: verified purchase intent outranks every traffic count.
    lines: list[str] = _conversion_lines(sig.get("conversion_winners") or {})
    gen = str(sig.get("generated_at", ""))[:16].replace("T", " ")
    lines.append(f"PROVEN SEARCH DEMAND (GSC+GA4, 28d, generated {gen} UTC) — this is what")
    lines.append("readers of THIS site actually open and search. Weight it above generic trends.")

    winners = sig.get("template_winners") or []
    if winners:
        lines.append("Page-template GA4 views (includes crawlers — reach, not sales):")
        for w in winners[:5]:
            lines.append(
                f"  - {w.get('template')}: {w.get('views', 0):,} GA4 views / "
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


# ── WHAT SELLS (conversion_winners) ─────────────────────────────────────────

# facet → the row field that names it (conversion_clicks keys).
_CONVERSION_FACETS = {
    "templates": "template", "placements": "placement", "clusters": "cluster",
    "categories": "category", "products": "key", "pages": "path",
}

# A cluster's per-100 yield is worth ranking on once it has enough articles OR
# enough clicks: 2 clicks on 3 articles is noise, 60 clicks on 5 guides is not.
CONVERSION_MIN_ARTICLES = 10
CONVERSION_MIN_CLICKS = 10
# How many clusters count as WHAT SELLS lanes: the block's list, its RULE and
# the producer's steering-share metric (conversion_top5_cluster_share_7d_pct).
WHAT_SELLS_TOP_CLUSTERS = 5


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _yield(row: dict) -> float:
    try:
        return float(row.get("clicks_per_100") or 0)
    except (TypeError, ValueError):
        return 0.0


def _judged(row: dict, *, min_articles: int, min_clicks: int) -> bool:
    return _int(row.get("articles")) >= min_articles or _int(row.get("clicks")) >= min_clicks


def _site_article_yield(clusters: list) -> Optional[float]:
    """Clicks per 100 published articles across every cluster (the site's
    average article), or None without articles."""
    rows = [c for c in clusters or [] if _int(c.get("articles"))]
    n = sum(_int(c.get("articles")) for c in rows)
    return 100.0 * sum(_int(c.get("clicks")) for c in rows) / n if n else None


def _ranked_conversion_clusters(rows: list, *, min_articles: int, min_clicks: int,
                                above_average: bool = True,
                                exclude: tuple[str, ...] = ("other",)) -> list[dict]:
    """Every cluster row that qualifies as a WHAT SELLS cluster, best first
    (see :func:`top_conversion_clusters`). Rows without articles have no
    yield and never qualify."""
    floor = round(_site_article_yield(rows) or 0.0, 1) if above_average else 0.0
    return sorted(
        (c for c in rows or []
         if _int(c.get("articles")) and _int(c.get("clicks")) > 0
         and c.get("cluster") not in exclude
         and _judged(c, min_articles=min_articles, min_clicks=min_clicks)
         and _yield(c) >= floor),
        key=lambda c: (-_yield(c), str(c.get("cluster"))))


def _citable_product(row: dict) -> bool:
    """A product row the block may ask authors to link: its catalog row is
    not inactive, and it resolved to a title or a link (a bare key is a
    product the catalog lookup did not find)."""
    return row.get("active") is not False and bool(row.get("title") or row.get("link"))


def _conversion_lines(cw: dict, *, max_templates: int = 6,
                      max_clusters: int = WHAT_SELLS_TOP_CLUSTERS,
                      max_categories: int = 6, max_products: int = 6,
                      max_pages: int = 6, max_placements: int = 6,
                      min_articles: int = CONVERSION_MIN_ARTICLES,
                      min_clicks: int = CONVERSION_MIN_CLICKS) -> list[str]:
    """The WHAT SELLS section: where the site's verified-human affiliate
    clicks come from — page templates, article topic clusters per 100
    published articles (a yield, like the AI-demand clusters), product
    categories, single pages and products, on-page placements. Empty when the
    window has no clicks: there is nothing to steer toward.

    Only the clusters :func:`top_conversion_clusters` returns (at most
    `max_clusters`) are listed as WHAT SELLS clusters and named in the RULE;
    judged clusters below the site average go on a separate line that says
    they are not WHAT SELLS lanes. The RULE defers to the consumer's own
    scope: a BLOCKED / do-not-propose list in the same prompt still wins."""
    total = cw.get("total") or {}
    if not _int(total.get("d30")):
        return []
    w, t = _int(cw.get("window_days")) or 30, _int(cw.get("trend_days")) or 7
    label = str(cw.get("label") or "affiliate")

    def trend(r: dict) -> str:
        return f"{t}d {_int(r.get('d7')):,} vs {_int(r.get('prev7')):,}"

    def clicks(r: dict) -> str:
        n = _int(r.get("clicks"))
        return f"{n:,} click{'' if n == 1 else 's'}"

    out = [f"WHAT SELLS (verified-human {label} clicks, {w}d; last {t}d vs prior {t}d): "
           f"{_int(total.get('d30')):,} clicks, {trend(total)}.",
           "Only clicks the framework's human-click filter accepts count (no bots, crawlers or "
           "datacenter IPs). This is the purchase signal; it outranks every view, impression "
           "and citation count below."]

    tpl_rows = cw.get("templates") or []
    tpls = [r for r in tpl_rows if _int(r.get("clicks"))][:max_templates]
    if tpls:
        out.append("Page templates by clicks:")
        for r in tpls:
            yld = ""
            if _int(r.get("articles")):
                yld = (f"; {r.get('clicks_per_100', 0):g} per 100 of {_int(r['articles']):,} "
                       "published articles")
                # The yield counts only clicks on published-article paths; say
                # so when the template's pattern also matched other pages.
                if r.get("article_clicks") is not None and \
                        _int(r.get("article_clicks")) != _int(r.get("clicks")):
                    yld += f" ({_int(r['article_clicks']):,} of the clicks are on them)"
            out.append(f"  - {r.get('template')}: {clicks(r)} ({trend(r)}) "
                       f"from {_int(r.get('pages')):,} pages{yld}")
        # Article formats below the cut, zero-click ones included: a format
        # with hundreds of articles and no clicks is the strongest "stop" signal.
        shown_names = {r.get("template") for r in tpls}
        low = sorted((r for r in tpl_rows
                      if _int(r.get("articles")) and r.get("template") not in shown_names),
                     key=lambda r: (-float(r.get("clicks_per_100") or 0),
                                    -_int(r.get("articles"))))
        if low:
            out.append("  Other article templates: " + " · ".join(
                f"{r.get('template')} {r.get('clicks_per_100', 0):g}/100 of "
                f"{_int(r['articles']):,} articles" for r in low[:6]))

    rows = [c for c in (cw.get("clusters") or []) if _int(c.get("articles"))]
    lanes = _ranked_conversion_clusters(rows, min_articles=min_articles,
                                        min_clicks=min_clicks)[:max(0, int(max_clusters))]
    if rows:
        avg = _site_article_yield(rows) or 0.0
        if not avg:
            out.append(f"Article topic clusters: no published article earned a verified click "
                       f"in {w}d, so no cluster is a WHAT SELLS lane yet.")
        elif lanes:
            out.append("WHAT SELLS clusters (verified clicks per 100 published articles, at or "
                       f"above the site average of {avg:.1f}, on enough articles or clicks to "
                       "judge) — the lanes where one more article is likeliest to sell:")
            for c in lanes:
                out.append(f"  - {c.get('cluster')}: {c.get('clicks_per_100', 0):g}/100 "
                           f"({clicks(c)}, {t}d {_int(c.get('d7')):,}, "
                           f"{_int(c.get('articles')):,} articles)")
        else:
            out.append("Article topic clusters (verified clicks per 100 published articles; "
                       f"site average {avg:.1f}): none sells at or above the average on enough "
                       "articles or clicks to judge, so no cluster is a WHAT SELLS lane yet.")
        if avg:
            names = {str(c.get("cluster")) for c in lanes}
            rest = sorted((c for c in rows if str(c.get("cluster")) not in names
                           and _judged(c, min_articles=min_articles, min_clicks=min_clicks)),
                          key=lambda c: (-_yield(c), -_int(c.get("articles"))))
            if rest:
                # Usually all below the average; a cluster past the cap or the
                # catch-all bucket can sit above it, so only then say less.
                below = all(_yield(c) < round(avg, 1) for c in rest)
                out.append("  " + ("Below the site average (not WHAT SELLS lanes): " if below
                                   else "Not WHAT SELLS lanes: ")
                           + " · ".join(f"{c.get('cluster')} {c.get('clicks_per_100', 0):g}/100 "
                                        f"({_int(c.get('articles')):,} articles)"
                                        for c in rest[:8]))
            # Promising but unproven: clicks on too few articles to call a lane.
            small = [c for c in rows if _int(c.get("clicks"))
                     and not _judged(c, min_articles=min_articles, min_clicks=min_clicks)]
            if small:
                out.append("  Too few articles and clicks to judge yet: " + " · ".join(
                    f"{c.get('cluster')} {c.get('clicks_per_100', 0):g}/100 ({clicks(c)}, "
                    f"{_int(c.get('articles')):,} articles)" for c in small[:6]))

    cats = [r for r in (cw.get("categories") or []) if _int(r.get("clicks"))]
    if cats:
        out.append("Product categories by clicks:")
        for r in cats[:max_categories]:
            extra = f", {_int(r['products']):,} products" if _int(r.get("products")) else ""
            if r.get("value"):
                extra += f", ${float(r['value']):,.0f} clicked list value"
            out.append(f"  - {r.get('category')}: {clicks(r)} ({trend(r)}){extra}")

    pages = [r for r in (cw.get("pages") or []) if _int(r.get("clicks"))]
    if pages:
        out.append("Top converting pages (extend these, or link them from new articles):")
        for r in pages[:max_pages]:
            tp = f" [{r['template']}]" if r.get("template") else ""
            out.append(f"  - {r.get('path')}{tp}: {clicks(r)} ({trend(r)})")

    # Only products the site still lists: a link to an inactive row is
    # unlinked again at insert by the site's link checks (and 404s).
    prods = [r for r in (cw.get("products") or [])
             if _int(r.get("clicks")) and _citable_product(r)]
    if prods:
        # No per-product price here: a price in the prompt invites a stale
        # price quote in the article. Category value above is aggregate.
        out.append("Top converting products (link them where they fit):")
        for r in prods[:max_products]:
            # With a link the title only has to be recognisable.
            name = str(r.get("title") or r.get("key") or "")[:60 if r.get("link") else 90]
            cat = f" [{r['category']}]" if r.get("category") else ""
            ref = r.get("link") or (r.get("key") if r.get("title") else "")
            ref = f" {ref}" if ref else ""
            out.append(f"  - {name}{cat}{ref}: {clicks(r)} ({trend(r)})")

    pls = [r for r in (cw.get("placements") or []) if _int(r.get("clicks"))]
    if pls:
        out.append("Click placements (the on-page slot that got the click): " + " · ".join(
            f"{r.get('placement')} {_int(r['clicks']):,}" for r in pls[:max_placements]))

    # The RULE names the qualifying clusters (the same set the steering-share
    # metric counts) and defers to the consumer's own scope: this block is
    # shared by every proposer, and an operator's BLOCKED list is theirs.
    # " · " like the block's other inline lists: names can contain commas
    # ("Knives, prep & storage").
    named = (f"WHAT SELLS clusters: {' · '.join(str(c.get('cluster')) for c in lanes)}. "
             if lanes else "")
    out.append(f"RULE: {named}Within the topics this prompt allows, a proposal that extends a "
               "WHAT SELLS cluster or a product category listed above ranks above an "
               "AI-citation-only lane, and must link the converting page or product it extends "
               "(name it in why_now). A BLOCKED or do-not-propose list elsewhere in this prompt "
               "still wins over this rule.")
    return out


def build_conversion_block(sig: Optional[dict], **caps: int) -> str:
    """Only the WHAT SELLS section of `sig` ("" when it has no click data).

    For a consumer that wants the purchase evidence without the GSC/GA4
    sections, e.g. a head-to-head picker whose prompt already lists its pairs.
    Keyword arguments are :func:`_conversion_lines` caps (``max_pages=…``).
    """
    return "\n".join(_conversion_lines((sig or {}).get("conversion_winners") or {}, **caps))


def conversion_clicks(sig: Optional[dict], facet: str) -> dict[str, int]:
    """``{name: verified clicks over the window}`` for one WHAT SELLS facet.

    `facet` is "products" (keyed by product key, e.g. an ASIN), "categories",
    "pages" (keyed by path), "templates", "clusters" or "placements". Empty
    when the signal has no conversion data. For deterministic rankers, e.g.
    boosting a head-to-head pair whose product sells. Covers the payload's
    rows only (the producer's ``top_products`` / ``top_pages`` caps), and
    includes products marked ``active: false``: the clicks happened, and the
    ranker applies its own catalog filters.
    """
    field = _CONVERSION_FACETS.get(facet)
    if not field:
        raise ValueError(f"unknown conversion facet {facet!r}; "
                         f"expected one of {sorted(_CONVERSION_FACETS)}")
    out: dict[str, int] = {}
    for r in ((sig or {}).get("conversion_winners") or {}).get(facet) or []:
        key = r.get(field)
        if key not in (None, ""):
            out[str(key)] = out.get(str(key), 0) + _int(r.get("clicks"))
    return out


def top_conversion_clusters(sig: Optional[dict], n: int = WHAT_SELLS_TOP_CLUSTERS, *,
                            min_articles: int = CONVERSION_MIN_ARTICLES,
                            min_clicks: int = CONVERSION_MIN_CLICKS,
                            above_average: bool = True,
                            exclude: tuple[str, ...] = ("other",)) -> list[str]:
    """Names of the `n` article clusters with the best clicks-per-100 yield.

    These are the "WHAT SELLS clusters": the ones the block lists under that
    name and names in its RULE, and the ones a new article is measured
    against. A cluster qualifies only with at least one click, enough
    articles or clicks to judge, and (`above_average`) a yield at or above the
    site's average article — on a site with nine clusters a plain top 5
    would count a below-average lane as a winner. `exclude` drops the
    catch-all bucket of unmatched articles (ai_traffic.cluster_yield's
    "other").
    """
    rows = ((sig or {}).get("conversion_winners") or {}).get("clusters") or []
    ranked = _ranked_conversion_clusters(rows, min_articles=min_articles,
                                         min_clicks=min_clicks,
                                         above_average=above_average, exclude=exclude)
    return [str(c.get("cluster")) for c in ranked[:max(0, int(n))]]
