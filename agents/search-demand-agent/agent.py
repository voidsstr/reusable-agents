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

    # ---- the run ---------------------------------------------------------
    def run(self) -> RunResult:
        cfg = _load_config()
        site_id = cfg["site_id"]
        ds = cfg["data_sources"]
        min_imp = int(cfg.get("min_impressions", 20))
        templates = [(t["name"], re.compile(t["pattern"]))
                     for t in (cfg.get("templates") or [])]

        def classify(path: str) -> str:
            for name, rx in templates:
                if rx.search(path):
                    return name
            return "other"

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
        top = template_winners[0]["template"] if template_winners else "n/a"
        return RunResult(
            status="success",
            summary=(f"steered {len(steer)} topics (winning template: {top}); "
                     f"strike={len(strike)} uncovered={len(zero)} "
                     f"h2h_hot={len(h2h_hot)} steered_published_7d={steered_pub}"),
            metrics=metrics,
        )


if __name__ == "__main__":
    SearchDemandAgent().run_once()
