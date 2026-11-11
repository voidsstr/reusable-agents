#!/usr/bin/env python3
"""search-demand-agent — GSC + GA4 → topic steering for author agents.

WHAT IT DOES
------------
Once a day per site, pulls 28 days of Google Search Console search analytics
and GA4 page views, distills them into a compact steering signal (what page
TEMPLATES win, which CATEGORY×FORMAT lanes have proven demand, which queries
we don't rank for, which are in strike distance), and publishes it via
``framework.core.demand_signal`` for the article-proposal and head-to-head
agents to inject into their LLM prompts.

DESIGN
------
* Engine is generic (this file); per-site VALUES live in the instance's
  ``site.yaml`` (SEARCH_DEMAND_CONFIG env). Mirrors seo-opportunity-agent.
* Analysis is DETERMINISTIC — no LLM. The author's LLM does the creative
  work; this agent only ranks evidence. A tick costs a handful of HTTP
  calls, and `signals()` short-circuits to one real run per UTC day
  (GSC data itself only refreshes daily).
* Token minting reuses the SEO collector's refresh-token.py + oauth file so
  there is exactly one Google re-auth runbook on the host.

CONFIG (site.yaml)
------------------
  site_id: specpicks
  data_sources:
    gsc: {site_url: "sc-domain:specpicks.com"}
    ga4: {property_id: "531274480"}
    db:  {dsn_env: DATABASE_URL}          # optional: category vocabulary + outcome metric
  auth: {oauth_file: "~/.reusable-agents/seo/.oauth.json"}
  templates:                              # ordered; first regex match wins
    - {name: product-vs,       pattern: "^/vs/"}
    - {name: hardware-compare, pattern: "^/compare/."}
    - {name: article,          pattern: "^/(articles|blog)/"}
  min_impressions: 20

AI-ASSISTANT DEMAND (optional, data_sources.ai_traffic)
--------------------------------------------------------
GSC/GA4 cannot see what ChatGPT, Perplexity or Claude send people to. With
this block the agent also reads the site's ai_traffic_log through
framework/core/ai_traffic.py (human referrals + live user fetches,
spoof-filtered) and publishes ``ai_assistant_demand``:

  clusters      article topic clusters with AI referrals / fetches PER 100
                published articles (a yield, not a raw count — raw counts
                just reward whichever cluster already has the most articles)
  top_products  the product pages assistants land on most (specific-product
                lookups: work for PDP / head-to-head improvement, not for
                category buying guides)

    data_sources:
      ai_traffic:
        dsn_env: DATABASE_URL             # default: data_sources.db.dsn_env
        config: {referral_sources: [chatgpt, perplexity, copilot, gemini, claude],
                 live_fetch_sources: [chatgpt-user, perplexity-user, claude-user]}
        articles_query: "SELECT slug, title FROM editorial_articles WHERE status = 'published'"
        article_path_template: "/reviews/{slug}"
        clusters: [{name: "LLM / local AI", pattern: "llm|qwen|llama"}, ...]
        product_prefix: /product/
        product_key_regex: "[A-Z0-9]{10}"
        products_query: "SELECT asin AS key, title, category FROM ... WHERE asin = ANY(%(keys)s)"
        top_products: 15

WHAT SELLS (optional, data_sources.affiliate_clicks)
-----------------------------------------------------
GA4 views include crawlers and GSC/AI counts say nothing about purchases. With
this block the agent reads the site's first-party affiliate click table
through framework/core/human_clicks.py (only rows with the verdict "human"
count; the storage config's by_profile[<AGENT_ID>] overrides apply) and
publishes ``conversion_winners`` (30d, last 7d vs the 7d before):

  total / templates / pages   clicks per page (template = first `templates`
                              regex match; `articles` + `clicks_per_100` for
                              templates that published articles map to)
  clusters                    article topic clusters, clicks per 100 published
                              articles (ai_traffic.cluster_yield)
  products / categories       clicks per product key, resolved by
                              products_query (key, title, category[, price]
                              [, link = the internal path to cite])
  placements                  clicks per on-page slot

    data_sources:
      affiliate_clicks:
        label: Amazon                     # merchant name in the block title
        dsn_env: DATABASE_URL             # default: data_sources.db.dsn_env
        table: <click table>
        spec: {time_col: clicked_at, referer_col: source_page, bot_flag_col: is_bot}
        match: {target: amazon}           # human_clicks match (list = IN)
        page_col: source_page             # path or full URL (alias: group_col)
        product_col: asin                 # optional
        placement_col: placement          # optional
        category_col: category_slug       # optional; else categories via products_query
        products_query: "SELECT ... AS key, ... AS title, ... AS category,
                         ... AS price FROM ... WHERE ... = ANY(%(keys)s)"
        templates: [{name, pattern}]      # default: the top-level templates
        articles_query / article_path_template / clusters
                                          # default: data_sources.ai_traffic's;
                                          # a published_at column enables the
                                          # steering-share and new-article metrics
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from framework.core import demand_signal  # noqa: E402
from framework.core.agent_base import AgentBase, RunResult  # noqa: E402

REFRESH_TOKEN_PY = _REPO_ROOT / "agents" / "seo-opportunity-agent" / "lib" / "collector" / "refresh-token.py"

GSC_ROW_LIMIT = 5000
GA4_ROW_LIMIT = 2000


def _load_config() -> dict:
    import yaml
    path = os.environ.get("SEARCH_DEMAND_CONFIG", "").strip()
    if not path:
        raise SystemExit("[search-demand] SEARCH_DEMAND_CONFIG is not set")
    cfg = yaml.safe_load(Path(path).read_text()) or {}
    for req in ("site_id", "data_sources", "auth"):
        if req not in cfg:
            raise SystemExit(f"[search-demand] config missing required key: {req}")
    return cfg


def _mint_token(oauth_file: str) -> str:
    """Google access token via the shared SEO refresh-token helper."""
    proc = subprocess.run(
        [sys.executable, str(REFRESH_TOKEN_PY), "--oauth-file",
         os.path.expanduser(oauth_file)],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(
            f"token mint failed rc={proc.returncode}: {(proc.stderr or '')[:200]} "
            "— re-auth with: bash install/reauth-google-oauth.sh")
    token = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    if not token.startswith("ya29"):
        raise RuntimeError(f"token mint returned no ya29 token: {token[:60]!r}")
    return token


def _post_json(url: str, token: str, body: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _norm_path(url_or_path: str) -> str:
    p = re.sub(r"^https?://[^/]+", "", url_or_path or "") or "/"
    return p.split("?")[0].split("#")[0] or "/"


# ---- WHAT SELLS: verified-human affiliate clicks (pure helpers) -----------
CONVERSION_WINDOW_DAYS = 30
CONVERSION_TREND_DAYS = 7
# window, trend, 2×trend: "prev7" = clicks(2×trend) − clicks(trend)
_CONVERSION_WINDOWS = (CONVERSION_WINDOW_DAYS, CONVERSION_TREND_DAYS,
                       2 * CONVERSION_TREND_DAYS)
TOP_CLUSTERS_FOR_SHARE = 5


def _template_classifier(templates: list[dict]):
    """First-regex-match page classifier over `[{name, pattern}]`."""
    compiled = [(str(t["name"]), re.compile(str(t["pattern"])))
                for t in templates or [] if t.get("name") and t.get("pattern")]

    def classify(path: str) -> str:
        for name, rx in compiled:
            if rx.search(path):
                return name
        return "other"
    return classify


def _click_path(value, template: str = "{value}") -> str:
    """A click table's page value → normalized site path ("" if not a path).

    Renders like human_clicks.human_click_paths (a full-URL referer keeps its
    path; query and fragment dropped), then ai_traffic.normalize_path so click
    paths and published-article paths compare equal."""
    from urllib.parse import urlparse
    from framework.core import ai_traffic
    if value in (None, ""):
        return ""
    raw = template.replace("{value}", str(value))
    parsed = urlparse(raw)
    path = (parsed.path if parsed.scheme else raw).split("?", 1)[0].split("#", 1)[0]
    return ai_traffic.normalize_path(path) if path.startswith("/") else ""


def _windowed(counts: dict, key_fn=None) -> dict[str, list[int]]:
    """``{window_days: {value: clicks}}`` → ``{key: [window, trend, 2×trend]}``.
    Keys go through `key_fn`; empty keys are dropped and equal keys merged."""
    out: dict[str, list[int]] = {}
    for i, win in enumerate(_CONVERSION_WINDOWS):
        for value, n in ((counts or {}).get(win) or {}).items():
            key = key_fn(value) if key_fn else ("" if value is None else str(value).strip())
            if key and int(n or 0) > 0:
                out.setdefault(key, [0, 0, 0])[i] += int(n)
    return out


def _trend_row(v: list[int]) -> dict:
    return {"clicks": v[0], "d7": v[1], "prev7": max(0, v[2] - v[1])}


def _price(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if f > 0 else None


def _published_within(ts, now: datetime, days: int) -> bool:
    if ts in (None, ""):
        return False
    if not isinstance(ts, datetime):
        try:
            ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - timedelta(days=days) < ts <= now + timedelta(days=1)


def summarize_conversions(*, page_counts: dict, product_counts: dict | None = None,
                          placement_counts: dict | None = None,
                          category_counts: dict | None = None,
                          product_meta: dict | None = None,
                          articles: list[dict] | None = None,
                          clusters: list[dict] | None = None,
                          article_path_template: str = "",
                          templates: list[dict] | None = None,
                          page_path_template: str = "{value}",
                          label: str = "affiliate", definition: str = "",
                          top_pages: int = 12, top_products: int = 12,
                          top_categories: int = 10,
                          now: datetime | None = None) -> tuple[dict, dict]:
    """Aggregate verified-human click counts into (conversion_winners, metrics).

    Every ``*_counts`` argument is ``{window_days: {group value: clicks}}`` for
    the windows in ``_CONVERSION_WINDOWS``, as human_clicks.human_counts_by
    returns them. `page_counts` must cover every click (NULL pages as ""):
    the totals are its sums. Pure: no DB, no storage.
    """
    from framework.core import ai_traffic
    now = now or datetime.now(timezone.utc)
    classify = _template_classifier(templates or [])
    articles = list(articles or [])

    totals = [sum(int(n or 0) for n in ((page_counts or {}).get(w) or {}).values())
              for w in _CONVERSION_WINDOWS]
    pages = _windowed(page_counts, lambda v: _click_path(v, page_path_template))

    # Published articles → their paths (for template yields and the metrics).
    art_paths: dict[str, dict] = {}
    if article_path_template:
        for a in articles:
            slug = str(a.get("slug") or "").strip()
            if slug:
                p = ai_traffic.normalize_path(article_path_template.replace("{slug}", slug))
                art_paths[p] = a
    tpl_articles: dict[str, int] = defaultdict(int)
    for p in art_paths:
        tpl_articles[classify(p)] += 1

    tpl: dict[str, list[int]] = {}
    for p, v in pages.items():
        agg = tpl.setdefault(classify(p), [0, 0, 0, 0])
        for i in range(3):
            agg[i] += v[i]
        agg[3] += 1 if v[0] > 0 else 0
    for name in tpl_articles:                  # article templates nobody clicked
        tpl.setdefault(name, [0, 0, 0, 0])
    templates_out = []
    for name, agg in tpl.items():
        row = {"template": name, **_trend_row(agg[:3]), "pages": agg[3]}
        if tpl_articles.get(name):
            row["articles"] = tpl_articles[name]
            row["clicks_per_100"] = round(agg[0] * 100.0 / tpl_articles[name], 1)
        templates_out.append(row)
    templates_out.sort(key=lambda r: (-r["clicks"], -r.get("articles", 0), r["template"]))

    pages_out = [{"path": p, "template": classify(p), **_trend_row(v)}
                 for p, v in sorted(pages.items(), key=lambda kv: (-kv[1][0], kv[0]))
                 if v[0] > 0][:max(0, int(top_pages))]

    clusters_out = []
    if articles and clusters and article_path_template:
        landed = [{"path": p, "referrals": v[0], "fetches": v[1]} for p, v in pages.items()]
        for c in ai_traffic.cluster_yield(landed, articles, clusters,
                                          path_template=article_path_template):
            clusters_out.append({"cluster": c["cluster"], "articles": c["articles"],
                                 "clicks": c["referrals"], "d7": c["fetches"],
                                 "clicks_per_100": c["referrals_per_100"]})

    meta = product_meta or {}
    prods = _windowed(product_counts or {})
    products_out = []
    for key, v in sorted(prods.items(), key=lambda kv: (-kv[1][0], kv[0])):
        if v[0] <= 0:
            continue
        if len(products_out) >= max(0, int(top_products)):
            break
        m = meta.get(key) or {}
        row = {"key": key, "title": str(m.get("title") or "")[:90],
               "category": m.get("category") or None, **_trend_row(v)}
        if _price(m.get("price")):
            row["price"] = _price(m.get("price"))
        if m.get("link"):
            row["link"] = str(m["link"])
        products_out.append(row)

    categories_out = []
    if category_counts:
        for cat, v in _windowed(category_counts).items():
            if v[0] > 0:
                categories_out.append({"category": cat, **_trend_row(v)})
    else:
        cat_agg: dict[str, list] = {}
        for key, v in prods.items():
            m = meta.get(key) or {}
            cat = str(m.get("category") or "").strip()
            if not cat or v[0] <= 0:
                continue
            a = cat_agg.setdefault(cat, [0, 0, 0, 0, 0.0])
            for i in range(3):
                a[i] += v[i]
            a[3] += 1
            a[4] += (_price(m.get("price")) or 0.0) * v[0]
        for cat, a in cat_agg.items():
            row = {"category": cat, **_trend_row(a[:3]), "products": a[3]}
            if a[4] > 0:
                row["value"] = round(a[4], 2)
            categories_out.append(row)
    categories_out.sort(key=lambda r: (-r["clicks"], r["category"]))

    placements_out = sorted(
        ({"placement": k, **_trend_row(v)} for k, v in _windowed(placement_counts or {}).items()
         if v[0] > 0),
        key=lambda r: (-r["clicks"], r["placement"]))

    cw = {
        "label": label or "affiliate",
        "window_days": CONVERSION_WINDOW_DAYS,
        "trend_days": CONVERSION_TREND_DAYS,
        "definition": definition,
        "total": {"d30": totals[0], "d7": totals[1], "prev7": max(0, totals[2] - totals[1])},
        "templates": templates_out,
        "placements": placements_out,
        "clusters": clusters_out,
        "categories": categories_out[:max(0, int(top_categories))],
        "products": products_out,
        "pages": pages_out,
    }

    metrics = {"conversion_clicks_30d": totals[0],
               "conversion_clicks_7d": totals[1],
               "conversion_clicks_prev7d": cw["total"]["prev7"]}
    if art_paths:
        metrics["conversion_article_clicks_30d"] = sum(
            pages[p][0] for p in art_paths if p in pages)
        dated = [a for a in articles if a.get("published_at") not in (None, "")]
        if dated:
            new = {p for p, a in art_paths.items()
                   if _published_within(a.get("published_at"), now, CONVERSION_WINDOW_DAYS)}
            metrics["conversion_new_article_clicks_30d"] = sum(
                pages[p][0] for p in new if p in pages)
            recent = [a for a in dated
                      if _published_within(a.get("published_at"), now, CONVERSION_TREND_DAYS)]
            metrics["conversion_recent_articles_7d"] = len(recent)
            top = demand_signal.top_conversion_clusters({"conversion_winners": cw},
                                                        TOP_CLUSTERS_FOR_SHARE)
            if clusters and top and recent:
                per = {c["cluster"]: c["articles"] for c in ai_traffic.cluster_yield(
                    [], recent, clusters, path_template=article_path_template)}
                metrics["conversion_top5_cluster_share_7d_pct"] = round(
                    100.0 * sum(per.get(n, 0) for n in top) / len(recent), 1)
    return cw, metrics


def _conversion_definition(table: str, match: dict | None, profile: str) -> str:
    def val(v) -> str:
        return "|".join(map(str, v)) if isinstance(v, (list, tuple, set)) else str(v)
    where = ", ".join(f"{k}={val(v)}" for k, v in (match or {}).items())
    return (f"verified-human clicks (framework/core/human_clicks.py verdict 'human', "
            f"profile {profile}) on {table}" + (f" where {where}" if where else ""))


class SearchDemandAgent(AgentBase):
    agent_id = "search-demand-agent"  # per-site AGENT_ID env overrides

    # ---- short-circuit: GSC/GA4 refresh daily; one real run per UTC day --
    def signals(self) -> dict | None:
        if os.environ.get("AGENT_FORCE_RUN", "").lower() in ("1", "true", "yes"):
            return None
        cfg = _load_config()
        return {"day": date.today().isoformat(),
                "site": cfg.get("site_id"),
                "cfg": json.dumps(cfg, sort_keys=True, default=str)}

    # ---- data pulls ------------------------------------------------------
    def _pull_gsc(self, token: str, site_url: str) -> list[dict]:
        end = (date.today() - timedelta(days=2)).isoformat()   # GSC lags ~2d
        start = (date.today() - timedelta(days=30)).isoformat()
        url = ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
               + urllib.request.quote(site_url, safe="") + "/searchAnalytics/query")
        resp = _post_json(url, token, {
            "startDate": start, "endDate": end,
            "dimensions": ["query", "page"],
            "rowLimit": GSC_ROW_LIMIT, "dataState": "final"})
        rows = []
        for r in resp.get("rows") or []:
            q, page = (r.get("keys") or ["", ""])[:2]
            rows.append({"query": q, "page": _norm_path(page),
                         "clicks": r.get("clicks", 0),
                         "impressions": r.get("impressions", 0),
                         "position": round(r.get("position", 0.0), 1)})
        return rows

    def _pull_ga4(self, token: str, property_id: str) -> list[dict]:
        resp = _post_json(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
            token, {
                "dateRanges": [{"startDate": "28daysAgo", "endDate": "yesterday"}],
                "dimensions": [{"name": "pagePath"}],
                "metrics": [{"name": "screenPageViews"}, {"name": "sessions"}],
                "limit": GA4_ROW_LIMIT,
                "orderBys": [{"desc": True,
                              "metric": {"metricName": "screenPageViews"}}]})
        rows = []
        for r in resp.get("rows") or []:
            path = _norm_path((r.get("dimensionValues") or [{}])[0].get("value", ""))
            m = r.get("metricValues") or []
            rows.append({"page": path,
                         "views": int((m[0] if m else {}).get("value", 0) or 0),
                         "sessions": int((m[1:] or [{}])[0].get("value", 0) or 0)})
        return rows

    def _pull_ga4_template_totals(self, token: str, property_id: str,
                                  templates: list[dict]) -> dict[str, dict]:
        """Exact per-template totals via BEGINS_WITH filters.

        Why not derive from the top-2000 pages pull: specpicks' comparison
        family is 140k pages × a few views each — a long tail that never
        reaches a top-N-by-views list but in AGGREGATE is the site's
        biggest surface. One filtered report per template (a handful of
        calls, once a day) measures the whole tail.
        """
        out: dict[str, dict] = {}
        for t in templates:
            prefix = t.get("prefix")
            if not prefix:
                m = re.match(r"\^(/[a-z0-9/-]+)", t.get("pattern", ""))
                prefix = m.group(1) if m else None
            if not prefix:
                continue
            try:
                resp = _post_json(
                    f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
                    token, {
                        "dateRanges": [{"startDate": "28daysAgo", "endDate": "yesterday"}],
                        "dimensions": [{"name": "pagePath"}],
                        "metrics": [{"name": "screenPageViews"}],
                        "dimensionFilter": {"filter": {"fieldName": "pagePath",
                            "stringFilter": {"matchType": "BEGINS_WITH", "value": prefix}}},
                        "metricAggregations": ["TOTAL"], "limit": 1})
                total = 0
                for row in resp.get("totals") or []:
                    mv = row.get("metricValues") or []
                    total = int((mv[0] if mv else {}).get("value", 0) or 0)
                out[t["name"]] = {"views": total,
                                  "pages": int(resp.get("rowCount", 0) or 0)}
            except Exception as e:
                self.decide("warning", f"GA4 template total failed for {t['name']}: {e}")
        return out

    def _pull_ga4_top_paths(self, token: str, property_id: str,
                            prefix: str, limit: int = 12) -> list[dict]:
        try:
            resp = _post_json(
                f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport",
                token, {
                    "dateRanges": [{"startDate": "28daysAgo", "endDate": "yesterday"}],
                    "dimensions": [{"name": "pagePath"}],
                    "metrics": [{"name": "screenPageViews"}],
                    "dimensionFilter": {"filter": {"fieldName": "pagePath",
                        "stringFilter": {"matchType": "BEGINS_WITH", "value": prefix}}},
                    "limit": limit,
                    "orderBys": [{"desc": True, "metric": {"metricName": "screenPageViews"}}]})
            out = []
            for r in resp.get("rows") or []:
                mv = r.get("metricValues") or []
                out.append({"page": (r.get("dimensionValues") or [{}])[0].get("value", ""),
                            "views": int((mv[0] if mv else {}).get("value", 0) or 0)})
            return out
        except Exception:
            return []

    def _resolve_vs_pairs(self, dsn: str, paths: list[dict]) -> list[dict]:
        """/vs/<asinA>/<asinB> → human-readable pair + category via the catalog."""
        if not dsn or not paths:
            return []
        asins = set()
        parsed = []
        for r in paths:
            m = re.match(r"^/vs/([A-Z0-9]{10})/([A-Z0-9]{10})", r["page"])
            if m:
                parsed.append((m.group(1), m.group(2), r["views"]))
                asins.update(m.groups())
        if not parsed:
            return []
        try:
            import psycopg2
            conn = psycopg2.connect(dsn); cur = conn.cursor()
            cur.execute(
                """SELECT p.asin, COALESCE(NULLIF(split_part(p.title, ',', 1), ''), p.title),
                          c.slug
                     FROM products p LEFT JOIN categories c ON c.id = p.category_id
                    WHERE p.asin = ANY(%s)""", (list(asins),))
            names = {a: (t[:48], slug) for a, t, slug in cur.fetchall()}
            conn.close()
        except Exception:
            return []
        out = []
        for a, b, views in parsed:
            na, ca = names.get(a, (a, None))
            nb, _ = names.get(b, (b, None))
            out.append({"pair": f"{na} vs {nb}" + (f" ({ca})" if ca else ""),
                        "views": views, "source": "ga4-views",
                        "category_slug": ca})
        return out

    def _category_vocab(self, dsn: str) -> list[tuple[str, str, set]]:
        """[(slug, name, token_set)] from the site catalog — the canonical
        topic vocabulary, so steering speaks the site's own category names."""
        try:
            import psycopg2
            conn = psycopg2.connect(dsn)
            cur = conn.cursor()
            cur.execute("SELECT slug, name FROM categories WHERE is_active = true")
            out = []
            for slug, name in cur.fetchall():
                toks = {t for t in re.split(r"[^a-z0-9]+", f"{slug} {name}".lower())
                        if len(t) > 2 and t not in ("and", "the", "for")}
                out.append((slug, name, toks))
            conn.close()
            return out
        except Exception as e:
            self.decide("warning", f"category vocab unavailable ({e}); steering by template only")
            return []

    def _steered_published_7d(self, dsn: str, topics: list[dict]) -> int:
        """Outcome metric: published articles in the last 7d whose title
        matches a currently-steered topic. Imperfect but directionally honest."""
        if not dsn or not topics:
            return 0
        try:
            import psycopg2
            conn = psycopg2.connect(dsn); cur = conn.cursor()
            n = 0
            seen = set()
            for t in topics[:15]:
                tok = (t.get("topic") or "").split("(")[0].strip()
                if not tok or tok in seen:
                    continue
                seen.add(tok)
                cur.execute(
                    """SELECT count(*) FROM editorial_articles
                       WHERE status='published'
                         AND published_at > now() - interval '7 days'
                         AND title ILIKE %s""", (f"%{tok.split()[0]}%",))
                n += cur.fetchone()[0]
            conn.close()
            return n
        except Exception:
            return 0

    def _ai_assistant_demand(self, cfg: dict) -> dict | None:
        """data_sources.ai_traffic → payload["ai_assistant_demand"] (see the
        module docstring). Optional: returns None, with a warning decision,
        on any failure — GSC/GA4 steering still ships."""
        ds = cfg.get("data_sources") or {}
        block = ds.get("ai_traffic") or {}
        if not block or block.get("enabled") is False:
            return None
        dsn_env = block.get("dsn_env") or (ds.get("db") or {}).get("dsn_env") or "DATABASE_URL"
        dsn = os.environ.get(dsn_env, "")
        if not dsn:
            self.decide("warning", f"ai_traffic skipped: ${dsn_env} not set")
            return None
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            from framework.core import ai_traffic
        except Exception as e:
            self.decide("warning", f"ai_traffic skipped: {e}")
            return None
        at_cfg = ai_traffic.config(block.get("config") or {})
        art_tmpl = str(block.get("article_path_template") or "/reviews/{slug}")
        art_prefix = art_tmpl.split("{slug}", 1)[0] or "/"
        prod_prefix = str(block.get("product_prefix") or "/product/")
        timeout_ms = int(at_cfg.get("statement_timeout_ms") or 30000)
        conn = None
        try:
            conn = psycopg2.connect(dsn)
            conn.set_session(readonly=True)
            landed = ai_traffic.landed_paths(conn, cfg=at_cfg,
                                             prefixes=[art_prefix, prod_prefix],
                                             limit=int(block.get("max_paths") or 20000))
            conn.rollback()
            articles: list[dict] = []
            if block.get("articles_query"):
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                    cur.execute(block["articles_query"])
                    articles = [dict(r) for r in cur.fetchall()]
                conn.rollback()
            clusters = ai_traffic.cluster_yield(
                landed, articles, block.get("clusters") or [],
                path_template=art_tmpl)
            prod_rows = [r for r in landed
                         if ai_traffic.normalize_path(r["path"]).startswith(prod_prefix)]
            keys = ai_traffic.path_keys(prod_rows, prod_prefix,
                                        key_regex=block.get("product_key_regex"))
            n_top = int(block.get("top_products") or 15)
            by_key = {}
            for r in prod_rows:
                k = ai_traffic.normalize_path(r["path"])[len(prod_prefix):].split("/", 1)[0]
                by_key.setdefault(k, r)
            top = []
            meta: dict = {}
            if block.get("products_query") and keys[:n_top]:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                    cur.execute(block["products_query"], {"keys": keys[:n_top]})
                    for r in cur.fetchall():
                        meta.setdefault(str(r.get("key")), dict(r))
                conn.rollback()
            for k in keys[:n_top]:
                r = by_key.get(k) or {}
                m = meta.get(k) or {}
                top.append({"key": k, "title": str(m.get("title") or "")[:90],
                            "category": m.get("category"),
                            "referrals": int(r.get("referrals") or 0),
                            "fetches": int(r.get("fetches") or 0)})
        except Exception as e:
            self.decide("warning", f"ai_traffic demand failed ({str(e)[:160]}); "
                                   "steering from GSC/GA4 only")
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return {
            "referral_days": int(at_cfg.get("referral_days") or 90),
            "fetch_days": int(at_cfg.get("fetch_days") or 30),
            "article_path_template": art_tmpl,
            "clusters": clusters,
            "top_products": top,
        }

    def _conversion_winners(self, cfg: dict) -> tuple[dict, dict] | None:
        """data_sources.affiliate_clicks → (payload["conversion_winners"],
        run metrics); see the module docstring. Optional: returns None, with a
        warning decision, on any failure — the rest of the signal still ships.
        Read-only session; every statement under a statement_timeout."""
        ds = cfg.get("data_sources") or {}
        block = ds.get("affiliate_clicks") or {}
        if not block or block.get("enabled") is False:
            return None
        table = block.get("table")
        page_col = block.get("page_col") or block.get("group_col")
        if not table or not page_col:
            self.decide("warning", "affiliate_clicks skipped: needs table and page_col")
            return None
        dsn_env = block.get("dsn_env") or (ds.get("db") or {}).get("dsn_env") or "DATABASE_URL"
        dsn = os.environ.get(dsn_env, "")
        if not dsn:
            self.decide("warning", f"affiliate_clicks skipped: ${dsn_env} not set")
            return None
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
            from framework.core import human_clicks
        except Exception as e:
            self.decide("warning", f"affiliate_clicks skipped: {e}")
            return None
        at = ds.get("ai_traffic") or {}
        profile = str(block.get("profile") or self.agent_id)
        spec = human_clicks.resolve_spec(block.get("spec") or {}, profile=profile)
        match = dict(block.get("match") or {}) or None
        timeout_ms = int(block.get("statement_timeout_ms") or 30000)
        articles_query = block.get("articles_query") or at.get("articles_query")
        conn = None
        try:
            conn = psycopg2.connect(dsn, connect_timeout=15)
            conn.set_session(readonly=True)

            def timeout(cur) -> None:
                cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")

            def counts_by(col) -> dict:
                if not col:
                    return {}
                out = {}
                for win in _CONVERSION_WINDOWS:
                    with conn.cursor() as cur:
                        timeout(cur)
                    out[win] = human_clicks.human_counts_by(
                        conn, spec, table=table, group_col=col, window_days=win,
                        match=match)
                    conn.rollback()
                return out

            page_counts = counts_by(page_col)
            product_counts = counts_by(block.get("product_col"))
            placement_counts = counts_by(block.get("placement_col"))
            category_counts = counts_by(block.get("category_col"))
            meta: dict = {}
            keys = [k for k in (product_counts.get(CONVERSION_WINDOW_DAYS) or {}) if k]
            keys = keys[:int(block.get("max_product_lookup") or 5000)]
            if block.get("products_query") and keys:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    timeout(cur)
                    cur.execute(block["products_query"], {"keys": keys})
                    for r in cur.fetchall():
                        meta.setdefault(str(r.get("key")), dict(r))
                conn.rollback()
            articles: list[dict] = []
            if articles_query:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    timeout(cur)
                    cur.execute(articles_query)
                    articles = [dict(r) for r in cur.fetchall()]
                conn.rollback()
        except Exception as e:
            self.decide("warning", f"affiliate_clicks failed ({str(e)[:160]}); "
                                   "signal ships without WHAT SELLS")
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return summarize_conversions(
            page_counts=page_counts, product_counts=product_counts,
            placement_counts=placement_counts, category_counts=category_counts,
            product_meta=meta, articles=articles,
            clusters=block.get("clusters") or at.get("clusters") or [],
            article_path_template=str(block.get("article_path_template")
                                      or at.get("article_path_template") or ""),
            templates=block.get("templates") or cfg.get("templates") or [],
            page_path_template=str(block.get("page_path_template") or "{value}"),
            label=str(block.get("label") or "affiliate"),
            definition=_conversion_definition(table, match, profile),
            top_pages=int(block.get("top_pages") or 12),
            top_products=int(block.get("top_products") or 12),
            top_categories=int(block.get("top_categories") or 10))

    # ---- the run ---------------------------------------------------------
    def run(self) -> RunResult:
        cfg = _load_config()
        site_id = cfg["site_id"]
        ds = cfg["data_sources"]
        min_imp = int(cfg.get("min_impressions", 20))
        classify = _template_classifier(cfg.get("templates") or [])

        token = _mint_token(cfg["auth"]["oauth_file"])

        self.status("pulling GSC", 10)
        gsc = self._pull_gsc(token, ds["gsc"]["site_url"])
        self.status(f"GSC rows={len(gsc)}; pulling GA4", 30)
        try:
            ga4 = self._pull_ga4(token, str(ds["ga4"]["property_id"]))
        except Exception as e:
            # GA4 degrades (GSC alone still steers); GSC failure aborts above.
            self.decide("warning", f"GA4 pull failed ({e}); steering from GSC only")
            ga4 = []

        dsn = os.environ.get((ds.get("db") or {}).get("dsn_env", ""), "")
        vocab = self._category_vocab(dsn) if dsn else []

        # Adaptive threshold: a fixed floor (20) blanks the whole signal on a
        # pre-indexing site (specpicks 2026-08: 867 impressions TOTAL/28d).
        total_imp = sum(r["impressions"] for r in gsc)
        eff_min_imp = min(min_imp, max(2, total_imp // 200))

        # -- template winners: exact per-template GA4 aggregates -------------
        # (captures the 140k-page /vs/ long tail the top-N pull cannot see)
        self.status("analyzing", 55)
        agg = self._pull_ga4_template_totals(token, str(ds["ga4"]["property_id"]),
                                             cfg.get("templates") or [])
        tpl = defaultdict(lambda: {"views": 0, "clicks": 0, "impressions": 0,
                                   "pages": set()})
        for r in gsc:
            t = classify(r["page"])
            tpl[t]["clicks"] += r["clicks"]
            tpl[t]["impressions"] += r["impressions"]
            tpl[t]["pages"].add(r["page"])
        for r in ga4:
            t = classify(r["page"])
            if t not in agg:                 # aggregate is authoritative when present
                tpl[t]["views"] += r["views"]
                tpl[t]["pages"].add(r["page"])
        template_winners = []
        for name in {*(t["name"] for t in cfg.get("templates") or []), *tpl.keys()}:
            if name == "other":
                continue
            views = agg.get(name, {}).get("views", tpl[name]["views"])
            pages = agg.get(name, {}).get("pages") or len(tpl[name]["pages"])
            template_winners.append({
                "template": name, "views": views,
                "clicks": tpl[name]["clicks"],
                "impressions": tpl[name]["impressions"],
                "pages": pages,
                "views_per_page": views / max(1, pages)})
        template_winners.sort(key=lambda w: -w["views"])
        template_winners = [w for w in template_winners if w["views"] or w["impressions"]]

        # -- per-query aggregates -------------------------------------------
        by_query = defaultdict(lambda: {"clicks": 0, "impressions": 0,
                                        "best_pos": 999.0, "best_page": ""})
        for r in gsc:
            q = by_query[r["query"]]
            q["clicks"] += r["clicks"]
            q["impressions"] += r["impressions"]
            if r["position"] < q["best_pos"]:
                q["best_pos"], q["best_page"] = r["position"], r["page"]

        strike, zero = [], []
        for query, q in by_query.items():
            if q["impressions"] < eff_min_imp:
                continue
            if 6 <= q["best_pos"] <= 20:
                strike.append({"query": query, "page": q["best_page"],
                               "position": q["best_pos"],
                               "impressions": q["impressions"]})
            elif q["best_pos"] > 20:
                zero.append({"query": query, "impressions": q["impressions"],
                             "position": q["best_pos"]})
        strike.sort(key=lambda s: -s["impressions"])
        zero.sort(key=lambda z: -z["impressions"])

        # -- steer topics: category vocab × winning template -----------------
        best_tpl = template_winners[0]["template"] if template_winners else "article"
        cat_stats = defaultdict(lambda: {"impressions": 0, "clicks": 0, "queries": []})
        for query, q in by_query.items():
            qtoks = set(re.split(r"[^a-z0-9]+", query.lower()))
            for slug, name, toks in vocab:
                if toks & qtoks:
                    c = cat_stats[(slug, name)]
                    c["impressions"] += q["impressions"]
                    c["clicks"] += q["clicks"]
                    if len(c["queries"]) < 6 and q["impressions"] >= eff_min_imp:
                        c["queries"].append(query)
        # GA4 evidence per category: /category/<slug> views + views on
        # resolved /vs/ pairs whose products belong to the category. On a
        # pre-indexing site this is most of the demand evidence.
        cat_views: dict[str, int] = defaultdict(int)
        for r in ga4:
            m = re.match(r"^/category/([a-z0-9-]+)", r["page"])
            if m:
                cat_views[m.group(1)] += r["views"]
        vs_top = self._pull_ga4_top_paths(token, str(ds["ga4"]["property_id"]),
                                          "/vs/", limit=25)
        vs_pairs = self._resolve_vs_pairs(dsn, vs_top)
        for vp in vs_pairs:
            if vp.get("category_slug"):
                cat_views[vp["category_slug"]] += vp["views"]

        name_by_slug = {slug: name for slug, name, _ in vocab}
        merged: dict[str, dict] = {}
        for (slug, name), c in cat_stats.items():
            merged[slug] = {"topic": name, "category_slug": slug,
                            "impressions": c["impressions"], "clicks": c["clicks"],
                            "views": cat_views.get(slug, 0),
                            "sample_queries": c["queries"][:4]}
        for slug, views in cat_views.items():
            if slug not in merged:
                merged[slug] = {"topic": name_by_slug.get(slug, slug),
                                "category_slug": slug, "impressions": 0,
                                "clicks": 0, "views": views, "sample_queries": []}
        steer = sorted(
            ({**m, "template": best_tpl,
              "score": m["impressions"] * 3 + m["views"]}
             for m in merged.values()),
            key=lambda s: -s["score"])[:20]
        steer = [s for s in steer if s["score"] > 0]

        # -- hot head-to-head pairs ------------------------------------------
        h2h_hot = list(vs_pairs[:10])
        vs_q = [(query, q) for query, q in by_query.items()
                if " vs " in f" {query} " and q["impressions"] >= eff_min_imp]
        for query, q in sorted(vs_q, key=lambda kv: -kv[1]["impressions"])[:10]:
            h2h_hot.append({"pair": query, "impressions": q["impressions"],
                            "source": "gsc-query"})
        h2h_hot = h2h_hot[:15]

        payload = {
            "template_winners": template_winners,
            "steer_topics": steer,
            "zero_coverage": zero[:25],
            "strike_distance": strike[:25],
            "h2h_hot": h2h_hot,
        }
        self.status("AI-assistant demand", 85)
        ai_demand = self._ai_assistant_demand(cfg)
        if ai_demand:
            payload["ai_assistant_demand"] = ai_demand
        self.status("what sells (verified-human affiliate clicks)", 90)
        conv = self._conversion_winners(cfg)
        conv_metrics: dict = {}
        if conv:
            payload["conversion_winners"], conv_metrics = conv
            cw = payload["conversion_winners"]
            self.decide(
                "info",
                f"WHAT SELLS: {cw['total']['d30']} verified clicks in "
                f"{cw['window_days']}d ({cw['trend_days']}d {cw['total']['d7']} vs "
                f"{cw['total']['prev7']}); top clusters "
                f"{demand_signal.top_conversion_clusters(payload, TOP_CLUSTERS_FOR_SHARE)}",
                evidence={"metrics": conv_metrics, "definition": cw.get("definition")})
        path = demand_signal.write_demand(self.storage, site_id, payload)
        self.decide("info", f"demand signal written to {path}")

        steered_pub = self._steered_published_7d(dsn, steer)

        metrics = {
            "gsc_rows": len(gsc), "ga4_rows": len(ga4),
            "topics_emitted": len(steer),
            "strike_found": len(strike), "zero_coverage_found": len(zero),
            "h2h_hot_found": len(h2h_hot),
            "steered_published_7d": steered_pub,
        }
        if ai_demand:
            cl = ai_demand.get("clusters") or []
            metrics["ai_demand_clusters"] = len(cl)
            metrics["ai_referrals_on_articles"] = sum(c.get("referrals", 0) for c in cl)
            metrics["ai_demand_top_products"] = len(ai_demand.get("top_products") or [])
        metrics.update(conv_metrics)
        top = template_winners[0]["template"] if template_winners else "n/a"
        sells = (f" what_sells={conv_metrics['conversion_clicks_30d']}"
                 if "conversion_clicks_30d" in conv_metrics else "")
        return RunResult(
            status="success",
            summary=(f"steered {len(steer)} topics (winning template: {top}); "
                     f"strike={len(strike)} uncovered={len(zero)} "
                     f"h2h_hot={len(h2h_hot)} steered_published_7d={steered_pub}{sells}"),
            metrics=metrics,
        )


if __name__ == "__main__":
    SearchDemandAgent().run_once()
