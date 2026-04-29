#!/usr/bin/env python3
"""seo-data-collector — pull fresh GSC + GA4 + (optional) DB data for one site.

Reads its config from $SEO_AGENT_CONFIG (a YAML file matching
shared/schemas/site-config.schema.json). Writes a standardized run dir to
<runs_root>/<site-id>/<UTC-ts>/data/.

Output layout:
  data/
    gsc-queries-90d.json       gsc-pages-90d.json
    gsc-query-page-90d.json    gsc-query-device-90d.json
    gsc-page-country-90d.json  gsc-us-queries-90d.json
    ga4-organic-landing-90d.json  ga4-summary-28d.json
    ga4-events-28d.json        db-stats.json (if configured)
    ... 18 GSC reports + 7 derived + 23 GA4 reports + DB
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add repo root to sys.path so `from shared...` works regardless of cwd
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.site_config import load_config_from_env  # noqa: E402


REFRESH_SCRIPT = Path(__file__).resolve().parent / "refresh-token.py"


def err(*a):
    print(*a, file=sys.stderr)


def get_access_token(oauth_file: Path) -> str:
    out = subprocess.check_output(
        [sys.executable, str(REFRESH_SCRIPT), "--oauth-file", str(oauth_file)],
        stderr=subprocess.PIPE, timeout=60,
    ).decode().strip()
    if not out:
        raise SystemExit("refresh-token.py returned empty output")
    return out


def http_post(url: str, body: dict, headers: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def gsc_query(token: str, site_url: str, body: dict) -> dict:
    enc = urllib.parse.quote(site_url, safe="")
    url = f"https://www.googleapis.com/webmasters/v3/sites/{enc}/searchAnalytics/query"
    return http_post(url, body, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })


def ga4_run_report(token: str, property_id: str, body: dict) -> dict:
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    return http_post(url, body, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })


def date_window(days_back: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=days_back - 1)).isoformat(), today.isoformat()


def pull_gsc(token: str, site_url: str, default_country: str, out_dir: Path) -> None:
    start_90, end_90 = date_window(90)
    start_28, end_28 = date_window(28)
    end_prev_dt = datetime.fromisoformat(start_90).date() - timedelta(days=1)
    start_prev_dt = end_prev_dt - timedelta(days=89)
    end_prev = end_prev_dt.isoformat()
    start_prev = start_prev_dt.isoformat()

    queries = {
        # 90-day broad SEO surface
        "queries-90d.json":      {"startDate": start_90, "endDate": end_90, "dimensions": ["query"], "rowLimit": 5000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "pages-90d.json":        {"startDate": start_90, "endDate": end_90, "dimensions": ["page"], "rowLimit": 5000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "query-page-90d.json":   {"startDate": start_90, "endDate": end_90, "dimensions": ["query", "page"], "rowLimit": 5000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "devices-90d.json":      {"startDate": start_90, "endDate": end_90, "dimensions": ["device"]},
        "countries-90d.json":    {"startDate": start_90, "endDate": end_90, "dimensions": ["country"], "rowLimit": 50},
        "appearance-90d.json":   {"startDate": start_90, "endDate": end_90, "dimensions": ["searchAppearance"]},
        "page-device-90d.json":  {"startDate": start_90, "endDate": end_90, "dimensions": ["page", "device"], "rowLimit": 2000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "page-country-90d.json": {"startDate": start_90, "endDate": end_90, "dimensions": ["page", "country"], "rowLimit": 2000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "query-country-90d.json":{"startDate": start_90, "endDate": end_90, "dimensions": ["query", "country"], "rowLimit": 2000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "query-device-90d.json": {"startDate": start_90, "endDate": end_90, "dimensions": ["query", "device"], "rowLimit": 2000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "date-90d.json":         {"startDate": start_90, "endDate": end_90, "dimensions": ["date"], "rowLimit": 100},
        # Country-specific (segment that pays for many monetization models)
        f"{default_country}-queries-90d.json": {
            "startDate": start_90, "endDate": end_90, "dimensions": ["query"], "rowLimit": 1000,
            "dimensionFilterGroups": [{"filters": [{"dimension": "country", "operator": "equals", "expression": default_country}]}],
            "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}],
        },
        f"{default_country}-pages-90d.json": {
            "startDate": start_90, "endDate": end_90, "dimensions": ["page"], "rowLimit": 1000,
            "dimensionFilterGroups": [{"filters": [{"dimension": "country", "operator": "equals", "expression": default_country}]}],
            "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}],
        },
        # 28-day fresh window
        "queries-28d.json":      {"startDate": start_28, "endDate": end_28, "dimensions": ["query"], "rowLimit": 2000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "pages-28d.json":        {"startDate": start_28, "endDate": end_28, "dimensions": ["page"], "rowLimit": 2000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        # Prior 90d for trend
        "prev-period-queries.json":    {"startDate": start_prev, "endDate": end_prev, "dimensions": ["query"], "rowLimit": 5000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "prev-period-pages.json":      {"startDate": start_prev, "endDate": end_prev, "dimensions": ["page"], "rowLimit": 5000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
        "prev-period-query-page.json": {"startDate": start_prev, "endDate": end_prev, "dimensions": ["query", "page"], "rowLimit": 5000, "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}]},
    }

    for fname, body in queries.items():
        try:
            data = gsc_query(token, site_url, body)
        except urllib.error.HTTPError as e:
            err(f"  GSC {fname} failed: {e.code} {e.reason}")
            data = {"rows": [], "error": str(e)}
        with (out_dir / f"gsc-{fname}").open("w") as f:
            json.dump(data, f, indent=2)
        time.sleep(0.1)

    # ---- Derived files ----
    raw = json.loads((out_dir / "gsc-query-page-90d.json").read_text())

    quick_wins = sorted(
        (r for r in raw.get("rows", []) if 11 <= r["position"] <= 30 and r["impressions"] >= 5),
        key=lambda r: -r["impressions"],
    )
    (out_dir / "gsc-quick-wins.json").write_text(json.dumps({"rows": quick_wins[:200]}, indent=2))

    striking = sorted(
        (r for r in raw.get("rows", []) if 4 <= r["position"] <= 10 and r["impressions"] >= 10),
        key=lambda r: -r["impressions"],
    )
    (out_dir / "gsc-striking-distance.json").write_text(json.dumps({"rows": striking[:200]}, indent=2))

    zero = sorted(
        (r for r in raw.get("rows", []) if r["clicks"] == 0 and r["impressions"] >= 10 and r["position"] <= 20),
        key=lambda r: -r["impressions"],
    )
    (out_dir / "gsc-zero-click.json").write_text(json.dumps({"rows": zero[:200]}, indent=2))

    # Rank trend per page (cur 90d vs prev 90d)
    try:
        cur_pages = {r["keys"][0]: r for r in json.loads((out_dir / "gsc-pages-90d.json").read_text()).get("rows", [])}
        prev_pages = {r["keys"][0]: r for r in json.loads((out_dir / "gsc-prev-period-pages.json").read_text()).get("rows", [])}
        movers = []
        for url, cur in cur_pages.items():
            prev = prev_pages.get(url)
            if prev and cur["impressions"] >= 20 and prev["impressions"] >= 20:
                movers.append({
                    "page": url,
                    "cur_position": cur["position"],
                    "prev_position": prev["position"],
                    "position_delta": cur["position"] - prev["position"],
                    "cur_impressions": cur["impressions"],
                    "prev_impressions": prev["impressions"],
                    "impressions_delta_pct": ((cur["impressions"] - prev["impressions"]) / max(prev["impressions"], 1)) * 100.0,
                    "cur_clicks": cur["clicks"],
                    "prev_clicks": prev["clicks"],
                })
        (out_dir / "gsc-rank-regressions.json").write_text(json.dumps({"rows": sorted(movers, key=lambda r: -r["position_delta"])[:50]}, indent=2))
        (out_dir / "gsc-rank-improvements.json").write_text(json.dumps({"rows": sorted(movers, key=lambda r: r["position_delta"])[:50]}, indent=2))
    except Exception as e:
        err(f"  rank-trend derivation failed: {e}")

    # Position-bucket histogram
    try:
        buckets = {"top3": 0, "top5": 0, "pos4_10": 0, "pos11_20": 0, "pos21_50": 0, "pos51plus": 0}
        for r in raw.get("rows", []):
            p = r["position"]
            if p <= 3:   buckets["top3"] += 1
            if p <= 5:   buckets["top5"] += 1
            if p <= 10 and p > 3: buckets["pos4_10"] += 1
            elif p <= 20: buckets["pos11_20"] += 1
            elif p <= 50: buckets["pos21_50"] += 1
            else: buckets["pos51plus"] += 1
        (out_dir / "gsc-position-distribution.json").write_text(json.dumps(buckets, indent=2))
    except Exception as e:
        err(f"  position-distribution derivation failed: {e}")

    # TOP-5 RANK TARGETS (global + country-specific)
    def _build_targets(rows, impr_threshold=3):
        targets = []
        for r in rows:
            keys = r.get("keys", [])
            if not keys: continue
            position = r.get("position", 100)
            impressions = r.get("impressions", 0)
            if position > 50 or position <= 5 or impressions < impr_threshold: continue
            targets.append({
                "query": keys[0],
                "position": round(position, 2),
                "impressions": impressions,
                "clicks": r.get("clicks", 0),
                "ctr": round(r.get("ctr", 0), 4),
                "score": round(impressions / max(position, 1.0), 2),
                "tier": "striking" if position <= 10 else "page-2" if position <= 20 else "deep",
            })
        return sorted(targets, key=lambda x: -x["score"])

    try:
        q90 = json.loads((out_dir / "gsc-queries-90d.json").read_text())
        targets = _build_targets(q90.get("rows", []), impr_threshold=3)
        (out_dir / "gsc-top5-targets.json").write_text(json.dumps({
            "rows": targets[:300],
            "summary": {
                "total_candidates": len(targets),
                "striking_count": sum(1 for t in targets if t["tier"] == "striking"),
                "page2_count":    sum(1 for t in targets if t["tier"] == "page-2"),
                "deep_count":     sum(1 for t in targets if t["tier"] == "deep"),
            },
        }, indent=2))
    except Exception as e:
        err(f"  top5-targets derivation failed: {e}")

    try:
        cq = json.loads((out_dir / f"gsc-{default_country}-queries-90d.json").read_text())
        country_targets = _build_targets(cq.get("rows", []), impr_threshold=2)
        (out_dir / f"gsc-{default_country}-top5-targets.json").write_text(json.dumps({
            "rows": country_targets[:300],
            "summary": {
                "total_candidates": len(country_targets),
                "striking_count": sum(1 for t in country_targets if t["tier"] == "striking"),
                "page2_count":    sum(1 for t in country_targets if t["tier"] == "page-2"),
                "deep_count":     sum(1 for t in country_targets if t["tier"] == "deep"),
            },
        }, indent=2))
    except Exception as e:
        err(f"  {default_country}-top5-targets derivation failed: {e}")

    err(f"  GSC: pulled {len(queries)} reports + 7 derived")


def pull_ga4(token: str, property_id: str, out_dir: Path) -> None:
    organic_filter = {"filter": {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {"value": "Organic Search"}}}
    us_filter = {"filter": {"fieldName": "country", "stringFilter": {"value": "United States"}}}

    def report(date_range, *, dimensions=None, metrics, dimension_filter=None,
               order_by_metric=None, limit=None):
        body = {
            "dateRanges": [{"startDate": date_range[0], "endDate": date_range[1]}],
            "metrics": [{"name": m} for m in metrics],
        }
        if dimensions:
            body["dimensions"] = [{"name": d} for d in dimensions]
        if dimension_filter:
            body["dimensionFilter"] = dimension_filter
        if order_by_metric:
            body["orderBys"] = [{"metric": {"metricName": order_by_metric}, "desc": True}]
        if limit:
            body["limit"] = limit
        return body

    R = ("90daysAgo", "today"); R28 = ("28daysAgo", "today")

    reports = {
        "summary-28d.json":   report(R28, metrics=["sessions","activeUsers","newUsers","bounceRate","averageSessionDuration","screenPageViews","engagementRate"]),
        "summary-90d.json":   report(R, metrics=["sessions","activeUsers","newUsers","bounceRate","averageSessionDuration","screenPageViews","engagementRate"]),
        "landing-90d.json":   report(R, dimensions=["landingPage"], metrics=["sessions","bounceRate","averageSessionDuration","engagementRate"], order_by_metric="sessions", limit=250),
        "landing-28d.json":   report(R28, dimensions=["landingPage"], metrics=["sessions","bounceRate","averageSessionDuration","engagementRate"], order_by_metric="sessions", limit=250),
        "pages-28d.json":     report(R28, dimensions=["pagePath"], metrics=["screenPageViews","activeUsers","averageSessionDuration","engagementRate"], order_by_metric="screenPageViews", limit=250),
        "pages-90d.json":     report(R, dimensions=["pagePath"], metrics=["screenPageViews","activeUsers","averageSessionDuration","engagementRate"], order_by_metric="screenPageViews", limit=500),
        "events-28d.json":    report(R28, dimensions=["eventName"], metrics=["eventCount","totalUsers"], order_by_metric="eventCount", limit=100),
        "events-90d.json":    report(R, dimensions=["eventName"], metrics=["eventCount","totalUsers"], order_by_metric="eventCount", limit=100),
        "geo-28d.json":       report(R28, dimensions=["country"], metrics=["sessions","bounceRate","engagementRate"], order_by_metric="sessions", limit=50),
        "geo-90d.json":       report(R, dimensions=["country"], metrics=["sessions","bounceRate","engagementRate"], order_by_metric="sessions", limit=50),
        "devices-28d.json":   report(R28, dimensions=["deviceCategory"], metrics=["sessions","bounceRate","engagementRate"]),
        "traffic-sources-28d.json": report(R28, dimensions=["sessionDefaultChannelGroup"], metrics=["sessions","activeUsers","bounceRate","engagementRate"], order_by_metric="sessions"),
        "traffic-sources-90d.json": report(R, dimensions=["sessionDefaultChannelGroup"], metrics=["sessions","activeUsers","bounceRate","engagementRate"], order_by_metric="sessions"),
        "session-source-medium-28d.json": report(R28, dimensions=["sessionSourceMedium"], metrics=["sessions","activeUsers","engagementRate"], order_by_metric="sessions", limit=50),
        "us-pages-28d.json":  report(R28, dimensions=["pagePath"], metrics=["screenPageViews","activeUsers","engagementRate"], dimension_filter=us_filter, order_by_metric="screenPageViews", limit=100),
        "us-landing-90d.json": report(R, dimensions=["landingPage"], metrics=["sessions","bounceRate","engagementRate"], dimension_filter=us_filter, order_by_metric="sessions", limit=100),
        "organic-summary-28d.json":   report(R28, metrics=["sessions","activeUsers","newUsers","bounceRate","engagementRate","screenPageViews"], dimension_filter=organic_filter),
        "organic-summary-90d.json":   report(R,   metrics=["sessions","activeUsers","newUsers","bounceRate","engagementRate","screenPageViews"], dimension_filter=organic_filter),
        "organic-landing-90d.json":   report(R, dimensions=["landingPage"], metrics=["sessions","bounceRate","engagementRate","averageSessionDuration"], dimension_filter=organic_filter, order_by_metric="sessions", limit=250),
        "organic-landing-28d.json":   report(R28, dimensions=["landingPage"], metrics=["sessions","bounceRate","engagementRate"], dimension_filter=organic_filter, order_by_metric="sessions", limit=250),
        "organic-events-28d.json":    report(R28, dimensions=["eventName"], metrics=["eventCount","totalUsers"], dimension_filter=organic_filter, order_by_metric="eventCount", limit=100),
        "organic-geo-28d.json":       report(R28, dimensions=["country"], metrics=["sessions","engagementRate"], dimension_filter=organic_filter, order_by_metric="sessions", limit=50),
        "organic-source-28d.json":    report(R28, dimensions=["sessionSource"], metrics=["sessions","engagementRate"], dimension_filter=organic_filter, order_by_metric="sessions", limit=25),
    }

    for fname, body in reports.items():
        try:
            data = ga4_run_report(token, property_id, body)
        except urllib.error.HTTPError as e:
            err(f"  GA4 {fname} failed: {e.code} {e.reason}")
            data = {"rows": [], "error": str(e)}
        with (out_dir / f"ga4-{fname}").open("w") as f:
            json.dump(data, f, indent=2)
        time.sleep(0.1)
    err(f"  GA4: pulled {len(reports)} reports")


def pull_db(db_cfg: dict, out_dir: Path) -> None:
    """Run the configured DB queries and write results to db-stats.json.

    Supported types:
      - docker-exec: run a JS file inside a docker container; stdout JSON.
      - postgres:    psycopg2-connect to a DSN (env var or literal) and run
                     a multi-statement SQL file. Each statement may emit one
                     row of JSON via `SELECT json_agg(...) AS <key>` or
                     `SELECT to_jsonb(...) AS <key>`. Results are merged into
                     a single dict keyed by the column alias.
    """
    db_type = db_cfg.get("type")
    if db_type == "docker-exec":
        container = db_cfg["container"]
        queries_file = db_cfg.get("queries_file")
        if not queries_file or not Path(queries_file).is_file():
            err(f"  DB: queries_file not found: {queries_file}")
            return
        try:
            res = subprocess.run(
                ["docker", "exec", "-i", container, "node", "-e", Path(queries_file).read_text()],
                capture_output=True, timeout=120, text=True,
            )
            if res.returncode != 0:
                err(f"  DB failed: {res.stderr[:400]}")
                return
            stats = json.loads(res.stdout)
            (out_dir / "db-stats.json").write_text(json.dumps(stats, indent=2))
            err(f"  DB: {len(json.dumps(stats))} bytes of stats written")
        except Exception as e:
            err(f"  DB failed: {e}")
        return

    if db_type == "postgres":
        # DSN may be (a) a literal connection string in db_cfg['dsn'] or
        # (b) the name of an env var holding the DSN in db_cfg['dsn_env'].
        dsn = db_cfg.get("dsn")
        if not dsn and db_cfg.get("dsn_env"):
            dsn = os.environ.get(db_cfg["dsn_env"])
        if not dsn:
            err(f"  DB: postgres dsn missing (set dsn or dsn_env)")
            return
        queries_file = db_cfg.get("queries_file")
        if not queries_file or not Path(queries_file).is_file():
            err(f"  DB: queries_file not found: {queries_file}")
            return
        try:
            import psycopg2  # noqa: F401
            import psycopg2.extras  # noqa: F401
        except ImportError:
            err(f"  DB: psycopg2 not installed; run pip install psycopg2-binary")
            return
        try:
            sql_text = Path(queries_file).read_text()
            stats: dict = {}
            with psycopg2.connect(dsn, connect_timeout=15) as conn:
                conn.set_session(readonly=True, autocommit=True)
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Split on `-- @@QUERY:` markers so each named query is independent.
                    # Format expected:
                    #   -- @@QUERY: total_products
                    #   SELECT json_agg(...) AS total_products FROM ...;
                    if "-- @@QUERY:" in sql_text:
                        chunks = sql_text.split("-- @@QUERY:")
                        # First chunk is the file prefix (header comments) before
                        # any @@QUERY marker — skip it.
                        for chunk in chunks[1:]:
                            chunk = chunk.strip()
                            if not chunk:
                                continue
                            # First line is the name, rest is SQL.
                            first_nl = chunk.find("\n")
                            name = chunk[:first_nl].strip() if first_nl > 0 else chunk
                            body = chunk[first_nl + 1:] if first_nl > 0 else ""
                            # Strip pure-comment / whitespace-only bodies so we
                            # don't hit "can't execute an empty query".
                            non_comment = "\n".join(
                                ln for ln in body.splitlines()
                                if ln.strip() and not ln.strip().startswith("--")
                            ).strip()
                            if not non_comment:
                                continue
                            try:
                                cur.execute(body)
                                rows = cur.fetchall()
                                stats[name] = [dict(r) for r in rows] if rows else []
                            except Exception as e:
                                err(f"  DB query {name!r} failed: {str(e)[:200]}")
                                stats[name] = {"error": str(e)[:300]}
                    else:
                        # Single-statement file — execute and store all rows.
                        cur.execute(sql_text)
                        rows = cur.fetchall()
                        stats["rows"] = [dict(r) for r in rows]
            (out_dir / "db-stats.json").write_text(json.dumps(stats, indent=2, default=str))
            err(f"  DB: {len(stats)} query result(s), {len(json.dumps(stats, default=str))} bytes written")
        except Exception as e:
            err(f"  DB failed: {str(e)[:300]}")
        return

    err(f"  DB type {db_type!r} not yet implemented")


# ---------------------------------------------------------------------------
# Google Ads (optional)
# ---------------------------------------------------------------------------

# Reports keyed by output filename; each is a (resource, GAQL) pair.
# Uses 90-day window. We write empty arrays on any failure rather than
# fail-fast so the rest of the run continues.
_ADS_REPORTS: dict[str, tuple[str, str]] = {
    # Search-terms — what users actually typed
    "ads-search-terms.json": (
        "search_term_view",
        """
        SELECT
          search_term_view.search_term,
          campaign.name,
          ad_group.name,
          metrics.impressions,
          metrics.clicks,
          metrics.ctr,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value,
          metrics.top_impression_percentage
        FROM search_term_view
        WHERE segments.date DURING LAST_90_DAYS
          AND metrics.impressions > 0
        ORDER BY metrics.impressions DESC
        LIMIT 5000
        """,
    ),
    # Keyword performance
    "ads-keyword-perf.json": (
        "keyword_view",
        """
        SELECT
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          metrics.impressions,
          metrics.clicks,
          metrics.ctr,
          metrics.cost_per_conversion,
          metrics.conversions
        FROM keyword_view
        WHERE segments.date DURING LAST_90_DAYS
          AND metrics.impressions > 0
        ORDER BY metrics.impressions DESC
        LIMIT 5000
        """,
    ),
    # Ad copy — top performing headlines/descriptions
    "ads-ad-copy.json": (
        "ad_group_ad",
        """
        SELECT
          ad_group_ad.ad.responsive_search_ad.headlines,
          ad_group_ad.ad.responsive_search_ad.descriptions,
          metrics.ctr,
          metrics.conversions
        FROM ad_group_ad
        WHERE segments.date DURING LAST_90_DAYS
          AND metrics.impressions > 0
        ORDER BY metrics.ctr DESC
        LIMIT 1000
        """,
    ),
    # Paid vs Organic — the goldmine. Shows queries where paid wins
    # but organic doesn't, signaling SEO opportunities.
    "ads-paid-vs-organic.json": (
        "paid_organic_search_term_view",
        """
        SELECT
          paid_organic_search_term_view.search_term,
          metrics.organic_clicks,
          metrics.organic_impressions,
          metrics.clicks,
          metrics.impressions,
          metrics.combined_clicks,
          metrics.combined_queries
        FROM paid_organic_search_term_view
        WHERE segments.date DURING LAST_90_DAYS
        ORDER BY metrics.combined_clicks DESC
        LIMIT 5000
        """,
    ),
}


def _empty_ads(out_dir: Path, reason: str) -> None:
    """Write an empty placeholder for every Ads report so downstream
    code can `_load()` without special-casing presence."""
    for fname in _ADS_REPORTS:
        (out_dir / fname).write_text(json.dumps({
            "results": [], "skipped": True, "reason": reason,
        }, indent=2))


def pull_google_ads(token: str, ads_cfg: dict, out_dir: Path) -> None:
    """Pull Google Ads reports. Skips silently with a warning on any error.

    Uses the official google-ads Python library if installed (preferred,
    handles pagination + protobuf decoding); otherwise falls back to the
    REST-style Google Ads API via urllib (POST /v17/customers/<id>/googleAds:searchStream).
    """
    customer_id = (ads_cfg.get("customer_id") or "").strip().replace("-", "")
    developer_token = (ads_cfg.get("developer_token") or "").strip()
    login_customer_id = (ads_cfg.get("login_customer_id") or "").strip().replace("-", "")

    if not customer_id or customer_id.startswith("{"):
        err("  Ads: customer_id missing/placeholder, skipping")
        _empty_ads(out_dir, "customer_id missing")
        return
    if not developer_token or developer_token.startswith("{"):
        err("  Ads: developer_token missing/placeholder, skipping")
        _empty_ads(out_dir, "developer_token missing")
        return

    # Try google-ads SDK first
    try:
        from google.ads.googleads.client import GoogleAdsClient  # type: ignore
        from google.protobuf.json_format import MessageToDict  # type: ignore
        sdk_available = True
    except ImportError:
        sdk_available = False

    if sdk_available:
        _pull_google_ads_via_sdk(
            token, customer_id, developer_token, login_customer_id, out_dir,
        )
    else:
        _pull_google_ads_via_rest(
            token, customer_id, developer_token, login_customer_id, out_dir,
        )


def _pull_google_ads_via_sdk(
    token: str, customer_id: str, developer_token: str,
    login_customer_id: str, out_dir: Path,
) -> None:
    try:
        from google.ads.googleads.client import GoogleAdsClient  # type: ignore
        from google.ads.googleads.errors import GoogleAdsException  # type: ignore
        from google.protobuf.json_format import MessageToDict  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
    except ImportError as e:
        err(f"  Ads SDK import failed: {e}")
        _empty_ads(out_dir, "sdk import failed")
        return

    config = {
        "developer_token": developer_token,
        "use_proto_plus": True,
    }
    if login_customer_id:
        config["login_customer_id"] = login_customer_id

    try:
        # The SDK normally wants client_id/client_secret/refresh_token;
        # we already have a fresh access token from refresh-token.py so
        # we hand it Credentials directly via load_from_dict's escape
        # hatch (set client manually).
        client = GoogleAdsClient.load_from_dict({
            **config,
            "client_id": "noop", "client_secret": "noop", "refresh_token": "noop",
        })
        # Override the underlying credentials with our access token
        client.credentials = Credentials(token=token)  # type: ignore
        ga_service = client.get_service("GoogleAdsService")
    except Exception as e:
        err(f"  Ads SDK init failed: {e}")
        _empty_ads(out_dir, f"sdk init failed: {e}")
        return

    for fname, (_resource, gaql) in _ADS_REPORTS.items():
        try:
            stream = ga_service.search_stream(customer_id=customer_id, query=gaql)
            rows = []
            for batch in stream:
                for row in batch.results:
                    rows.append(MessageToDict(row._pb, preserving_proto_field_name=True))
            (out_dir / fname).write_text(json.dumps({"results": rows}, indent=2))
            err(f"  Ads {fname}: {len(rows)} rows")
        except Exception as e:
            err(f"  Ads {fname} failed: {str(e)[:200]}")
            (out_dir / fname).write_text(json.dumps({
                "results": [], "error": str(e)[:500],
            }, indent=2))
        time.sleep(0.2)


def _pull_google_ads_via_rest(
    token: str, customer_id: str, developer_token: str,
    login_customer_id: str, out_dir: Path,
) -> None:
    """Fallback: hit the Ads API REST endpoint with the access token."""
    err("  Ads: google-ads SDK not installed, falling back to REST. "
        "Install with: pip install google-ads")
    base = f"https://googleads.googleapis.com/v17/customers/{customer_id}/googleAds:searchStream"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "developer-token": developer_token,
    }
    if login_customer_id:
        headers["login-customer-id"] = login_customer_id

    for fname, (_resource, gaql) in _ADS_REPORTS.items():
        body = {"query": " ".join(gaql.split())}
        try:
            req = urllib.request.Request(
                base,
                data=json.dumps(body).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode())
            # searchStream returns a list of batches; flatten results
            rows: list = []
            if isinstance(payload, list):
                for batch in payload:
                    rows.extend(batch.get("results", []))
            elif isinstance(payload, dict):
                rows = payload.get("results", [])
            (out_dir / fname).write_text(json.dumps({"results": rows}, indent=2))
            err(f"  Ads {fname}: {len(rows)} rows (REST)")
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()[:400]
            except Exception:
                err_body = ""
            err(f"  Ads {fname} REST {e.code} {e.reason}: {err_body}")
            (out_dir / fname).write_text(json.dumps({
                "results": [], "error": f"{e.code} {e.reason}", "body": err_body,
            }, indent=2))
        except Exception as e:
            err(f"  Ads {fname} REST failed: {str(e)[:200]}")
            (out_dir / fname).write_text(json.dumps({
                "results": [], "error": str(e)[:500],
            }, indent=2))
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# Page-inventory crawl (optional)
# ---------------------------------------------------------------------------

_PAGE_USER_AGENT = "SEOAgent/1.0 (+https://reusable-agents.dev/seo)"


def _fetch_sitemap_urls(sitemap_url: str, depth: int = 0) -> list[str]:
    """Fetch sitemap (or sitemap-index, recursing once into sub-sitemaps).
    Returns a flat list of URLs. Best-effort; logs and returns [] on error."""
    if depth > 3:
        return []
    try:
        req = urllib.request.Request(sitemap_url, headers={"User-Agent": _PAGE_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except Exception as e:
        err(f"  inventory: sitemap fetch {sitemap_url} failed: {e}")
        return []
    try:
        # Strip both xmlns declarations AND element/attribute prefixes so any
        # namespaced extension (image:image, video:video, news:news, xhtml:link)
        # parses without "unbound prefix" errors. Without the prefix strip the
        # xmlns:image="..." declaration is removed but <image:image> elements
        # remain — producing the unbound-prefix error we hit on specpicks.com.
        text = body.decode(errors="replace")
        text = re.sub(r"\sxmlns(:\w+)?=\"[^\"]+\"", "", text)
        text = re.sub(r"<(/?)([a-zA-Z0-9]+):", r"<\1", text)
        root = ET.fromstring(text)
    except Exception as e:
        err(f"  inventory: sitemap parse {sitemap_url} failed: {e}")
        return []
    tag = root.tag.lower()
    urls: list[str] = []
    if tag.endswith("sitemapindex"):
        for sm in root.findall(".//sitemap/loc"):
            if sm.text:
                urls.extend(_fetch_sitemap_urls(sm.text.strip(), depth + 1))
    else:
        for u in root.findall(".//url/loc"):
            if u.text:
                urls.append(u.text.strip())
    return urls


def pull_site_articles(articles_cfg: dict, out_dir: Path) -> None:
    """Pull every article slug from each configured source (e.g.
    editorial_articles, buying_guides) and join with this run's GSC
    pages-90d data. Writes articles-inventory.json with per-article
    URL + GSC stats (impressions/clicks/CTR/avg position) + a
    boost_signal field so the analyzer can emit targeted recs.

    Config shape (in site.yaml):

        articles:
          domain: "specpicks.com"
          dsn_env: DATABASE_URL
          sources:
            - name: editorial_articles
              query: "SELECT slug, title FROM editorial_articles WHERE site_id='specpicks' AND status='published'"
              url_template: "https://{domain}/reviews/{slug}"
            - name: buying_guides
              query: "SELECT slug, title FROM buying_guides WHERE status='published'"
              url_template: "https://{domain}/buying-guides/{slug}"

    Boost signals (any combination):
      - "high-impressions-low-ctr"     impressions ≥ 100 and CTR < 1.5%
      - "high-position-zero-clicks"    avg position ≤ 10 and clicks == 0
      - "orphan-no-gsc-data"           no GSC entry for this URL
      - "low-position"                 avg position > 30 (needs link boosting)
      - "ranking-well"                 (no boost needed; informational)
    """
    domain = articles_cfg.get("domain", "")
    if not domain:
        err("  articles: domain missing, skipping")
        return
    sources = articles_cfg.get("sources") or []
    if not sources:
        err("  articles: no sources configured, skipping")
        return
    dsn = articles_cfg.get("dsn") or os.environ.get(articles_cfg.get("dsn_env", ""), "")
    if not dsn:
        err("  articles: DSN missing (set dsn or dsn_env), skipping")
        return
    try:
        import psycopg2
    except ImportError:
        err("  articles: psycopg2 not installed, skipping")
        return

    # Load GSC URL stats for cross-ref
    gsc_pages_path = out_dir / "gsc-pages-90d.json"
    gsc_by_url: dict[str, dict] = {}
    if gsc_pages_path.is_file():
        try:
            for r in json.loads(gsc_pages_path.read_text()).get("rows", []):
                keys = r.get("keys") or []
                if keys:
                    gsc_by_url[keys[0]] = {
                        "impressions": r.get("impressions", 0),
                        "clicks": r.get("clicks", 0),
                        "ctr": r.get("ctr", 0.0),
                        "position": r.get("position", 0.0),
                    }
        except Exception as e:
            err(f"  articles: GSC pages parse failed: {e}")

    inventory: list[dict] = []
    counts_by_signal: dict[str, int] = {}
    try:
        conn = psycopg2.connect(dsn)
    except Exception as e:
        err(f"  articles: DB connect failed: {e}")
        return

    for src in sources:
        name = src.get("name", "(unnamed)")
        query = src.get("query")
        url_tpl = src.get("url_template", "")
        if not query or not url_tpl:
            err(f"  articles: source {name!r} missing query/url_template, skipping")
            continue
        try:
            cur = conn.cursor()
            cur.execute(query)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            cur.close()
        except Exception as e:
            err(f"  articles: source {name!r} query failed: {e}")
            try: conn.rollback()
            except Exception: pass
            continue
        slug_idx = cols.index("slug") if "slug" in cols else 0
        title_idx = cols.index("title") if "title" in cols else (1 if len(cols) > 1 else 0)
        for r in rows:
            slug = r[slug_idx]
            title = r[title_idx] if title_idx < len(r) else ""
            url = url_tpl.format(domain=domain, slug=slug)
            gsc = gsc_by_url.get(url, {})
            impr = gsc.get("impressions", 0) or 0
            clicks = gsc.get("clicks", 0) or 0
            ctr = gsc.get("ctr", 0.0) or 0.0
            pos = gsc.get("position", 0.0) or 0.0
            # Derive boost signal
            signals = []
            if not gsc:
                signals.append("orphan-no-gsc-data")
            else:
                if impr >= 100 and ctr < 0.015:
                    signals.append("high-impressions-low-ctr")
                if pos > 0 and pos <= 10 and clicks == 0:
                    signals.append("high-position-zero-clicks")
                if pos > 30:
                    signals.append("low-position")
                if not signals:
                    signals.append("ranking-well")
            for s in signals:
                counts_by_signal[s] = counts_by_signal.get(s, 0) + 1
            inventory.append({
                "source": name,
                "slug": slug,
                "title": title,
                "url": url,
                "gsc": {
                    "impressions": impr,
                    "clicks": clicks,
                    "ctr": ctr,
                    "position": pos,
                    "in_gsc": bool(gsc),
                },
                "boost_signals": signals,
            })

    try: conn.close()
    except Exception: pass

    out = {
        "schema_version": "1",
        "domain": domain,
        "total_articles": len(inventory),
        "by_signal": counts_by_signal,
        "articles": inventory,
    }
    (out_dir / "articles-inventory.json").write_text(json.dumps(out, indent=2))
    err(f"  articles: wrote {len(inventory)} entries; signals={counts_by_signal}")


def _gsc_impressions_by_url(out_dir: Path) -> dict[str, int]:
    """Load this run's gsc-pages-90d.json and return {url: impressions}."""
    p = out_dir / "gsc-pages-90d.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    out = {}
    for r in data.get("rows", []):
        keys = r.get("keys") or []
        if keys:
            out[keys[0]] = r.get("impressions", 0)
    return out


def _select_sample(
    matching: list[str], sample_size: int, weights: dict[str, int],
) -> list[str]:
    """Sample up to sample_size URLs, weighted toward higher GSC impressions
    when weights are available, else random."""
    if len(matching) <= sample_size:
        return matching
    if any(weights.get(u, 0) > 0 for u in matching):
        # Weighted (without replacement, simple top-N + a few random tails)
        ranked = sorted(matching, key=lambda u: -weights.get(u, 0))
        head_n = max(1, int(sample_size * 0.7))
        head = ranked[:head_n]
        tail = random.sample(
            [u for u in ranked[head_n:]] or ranked,
            min(sample_size - len(head), max(0, len(matching) - head_n)),
        )
        return head + tail
    return random.sample(matching, sample_size)


def _http_get_with_cache(
    url: str, cache_dir: Path, throttle_s: float = 1.5,
) -> tuple[int, bytes, str]:
    """Fetch URL with a per-URL HTML cache (sha1(url) → filename).
    If a cache file exists, sends If-Modified-Since from its mtime;
    304 returns the cached body. Returns (status, body_bytes, cache_path_str)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha1(url.encode()).hexdigest()
    cache_file = cache_dir / f"{sha}.html"
    headers = {"User-Agent": _PAGE_USER_AGENT}
    if cache_file.is_file():
        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc)
        headers["If-Modified-Since"] = mtime.strftime("%a, %d %b %Y %H:%M:%S GMT")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            status = resp.status
            cache_file.write_bytes(body)
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache_file.is_file():
            body = cache_file.read_bytes()
            status = 304
        else:
            time.sleep(throttle_s)
            raise
    time.sleep(throttle_s)
    return status, body, str(cache_file)


def _extract_page_meta(html: bytes) -> dict:
    """Crude HTML metadata extraction — title, description, h1, canonical, body text.
    Avoids a hard bs4 dependency; uses regex for the few fields the LLM audit needs."""
    try:
        text = html.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    def _find(pat, flags=re.IGNORECASE | re.DOTALL):
        m = re.search(pat, text, flags)
        return (m.group(1) or "").strip() if m else ""
    title = _find(r"<title[^>]*>(.*?)</title>")
    description = _find(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']')
    canonical = _find(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']*)["\']')
    h1 = _find(r"<h1[^>]*>(.*?)</h1>")
    h1 = re.sub(r"<[^>]+>", "", h1).strip()
    # Crude body text extraction
    body_html = _find(r"<body[^>]*>(.*?)</body>") or text
    body_html = re.sub(r"<script[^>]*>.*?</script>", " ", body_html, flags=re.IGNORECASE | re.DOTALL)
    body_html = re.sub(r"<style[^>]*>.*?</style>", " ", body_html, flags=re.IGNORECASE | re.DOTALL)
    body_text = re.sub(r"<[^>]+>", " ", body_html)
    body_text = re.sub(r"\s+", " ", body_text).strip()[:8000]
    return {
        "title": title, "description": description, "canonical": canonical,
        "h1": h1, "body_text": body_text,
    }


def crawl_page_inventory(site_cfg, out_dir: Path) -> None:
    """Sitemap-driven page-type inventory crawl. Idempotent + cached."""
    rules = site_cfg.get("page_inventory") or []
    if not rules:
        err("  inventory: page_inventory not configured, skipping")
        return

    domain = site_cfg.get("site", {}).get("domain", "")
    sitemap_url = (site_cfg.get("sitemap_url")
                   or (f"https://{domain}/sitemap.xml" if domain else ""))
    if not sitemap_url:
        err("  inventory: no sitemap_url + no domain — cannot crawl")
        return

    err(f"  inventory: fetching {sitemap_url}")
    all_urls = _fetch_sitemap_urls(sitemap_url)
    err(f"  inventory: {len(all_urls)} URLs in sitemap")
    if not all_urls:
        # Write an empty file so downstream stages can rely on it existing
        (out_dir / "pages-by-type.jsonl").write_text("")
        return

    weights = _gsc_impressions_by_url(out_dir)
    cache_dir = out_dir / "page-cache"
    out_path = out_dir / "pages-by-type.jsonl"
    written = 0
    rng_seed = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
    random.seed(rng_seed)

    with out_path.open("w") as out_f:
        for rule in rules:
            page_type = rule.get("type", "unknown")
            pat = rule.get("sitemap_pattern", "")
            sample_size = int(rule.get("sample_size", 20))
            if not pat:
                continue
            try:
                rx = re.compile(pat)
            except re.error as e:
                err(f"  inventory[{page_type}]: bad regex {pat!r}: {e}")
                continue
            # Match against URL path or full URL — try path first
            matching = []
            for u in all_urls:
                try:
                    parsed = urllib.parse.urlparse(u)
                    if rx.search(parsed.path) or rx.search(u):
                        matching.append(u)
                except Exception:
                    continue
            err(f"  inventory[{page_type}]: {len(matching)} matches, sampling {min(sample_size, len(matching))}")
            sampled = _select_sample(matching, sample_size, weights)
            for url in sampled:
                rec: dict = {
                    "url": url, "type": page_type, "fetched_at": _iso_now(),
                }
                try:
                    status, body, cache_path = _http_get_with_cache(url, cache_dir)
                    rec["status"] = status
                    rec["html_size"] = len(body)
                    rec["cache_path"] = cache_path
                    if 200 <= status < 300 or status == 304:
                        meta = _extract_page_meta(body)
                        rec.update(meta)
                except Exception as e:
                    rec["status"] = 0
                    rec["error"] = str(e)[:300]
                out_f.write(json.dumps(rec) + "\n")
                written += 1
    err(f"  inventory: wrote {written} pages → pages-by-type.jsonl")


# ---------------------------------------------------------------------------
# Repo-routes scanner (optional — drives implementation_outline.files)
# ---------------------------------------------------------------------------

_REPO_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", ".venv",
    "__pycache__", ".pytest_cache", ".turbo", "coverage", "out",
    ".cache", "vendor",
}
_REPO_SKIP_FILE_PATTERNS = re.compile(
    r"\.(lock|log|db|sqlite|sqlite3)$|\.test\.|\.spec\.|\.min\.(js|css)$"
)
_SEO_SURFACE_HINTS = (
    "<title>", "<title", "<meta name=", "Helmet", "<Head>", "<Head ",
    "useSEO", "next/head", "JSON-LD", "structured-data",
    'application/ld+json', "ld+json", "<link rel=\"canonical\"",
    'rel="canonical"', "<sitemap", "sitemap.xml", "robots.txt",
    "og:title", "og:image", "twitter:card", "hreflang",
)
_ROUTE_PATTERNS = [
    # Express / Fastify / Koa: app.get('/path', ...)
    re.compile(r"""\b(?:app|router|server)\.(get|post|put|delete|patch|all|use)\(\s*['"]([^'"]+)['"]"""),
    # Next.js / React Router config-style
    re.compile(r"""path:\s*['"]([^'"]+)['"]"""),
    # Python flask: @app.route("/path")
    re.compile(r"""@\w+\.route\(\s*['"]([^'"]+)['"]"""),
    # FastAPI: @app.get("/path")
    re.compile(r"""@\w+\.(?:get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]"""),
]


def _looks_seo_relevant(name: str, content: str) -> bool:
    lname = name.lower()
    if (lname.endswith((".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte"))
            and ("/pages/" in name.replace("\\", "/") or "/app/" in name.replace("\\", "/"))):
        return True
    if "server" in lname or "/routes/" in name.replace("\\", "/") or "/api/" in name.replace("\\", "/"):
        if lname.endswith((".ts", ".js", ".py")):
            return True
    if lname in ("sitemap.ts", "sitemap.js", "robots.txt", "sitemap.xml"):
        return True
    return any(h in content for h in _SEO_SURFACE_HINTS)


def _detect_emits(content: str) -> list[str]:
    emits = []
    if re.search(r"<title[ >]|setTitle\(|document\.title|<Helmet", content, re.IGNORECASE):
        emits.append("title")
    if re.search(r'meta[^>]+name=["\']description["\']|meta-description|metaDescription',
                 content, re.IGNORECASE):
        emits.append("description")
    if re.search(r'rel=["\']canonical["\']|setCanonical|canonicalUrl', content, re.IGNORECASE):
        emits.append("canonical")
    if re.search(r'(og:title|og:image|og:description)', content):
        emits.append("og:*")
    if re.search(r'(twitter:card|twitter:title|twitter:image)', content):
        emits.append("twitter:*")
    m = re.search(r'application/ld\+json[^<]*?>\s*\{[^<]*"@type"\s*:\s*"([^"]+)"',
                  content, re.IGNORECASE | re.DOTALL)
    if m:
        emits.append(f"jsonld:{m.group(1)}")
    elif "ld+json" in content or "JSON-LD" in content:
        emits.append("jsonld")
    if re.search(r'\bhreflang\b', content):
        emits.append("hreflang")
    return emits


_SEO_EMITS_ALL = ("title", "description", "canonical", "og:*", "twitter:*", "jsonld", "hreflang")


def scan_repo(site_cfg, out_dir: Path) -> None:
    """Walk implementer.repo_path, write repo-routes.json with SEO-relevant routes.
    No-op if repo_path missing or unreadable."""
    impl = site_cfg.get("implementer") or {}
    repo_path = impl.get("repo_path", "")
    if not repo_path:
        err("  repo-scan: implementer.repo_path not set, skipping")
        (out_dir / "repo-routes.json").write_text(json.dumps({
            "scanned_at": _iso_now(), "repo_path": "", "routes": [], "skipped": True,
        }, indent=2))
        return
    repo_root = Path(repo_path).expanduser()
    if not repo_root.is_dir():
        err(f"  repo-scan: repo_path not a dir: {repo_root}")
        (out_dir / "repo-routes.json").write_text(json.dumps({
            "scanned_at": _iso_now(), "repo_path": str(repo_root), "routes": [],
            "error": "repo_path not a directory",
        }, indent=2))
        return

    err(f"  repo-scan: walking {repo_root}")
    routes: list[dict] = []
    files_scanned = 0
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skip dirs in-place
        dirnames[:] = [d for d in dirnames if d not in _REPO_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if _REPO_SKIP_FILE_PATTERNS.search(fn):
                continue
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext not in {"ts", "tsx", "js", "jsx", "vue", "svelte", "py", "html", "xml", "txt"}:
                continue
            fpath = Path(dirpath) / fn
            try:
                if fpath.stat().st_size > 500_000:
                    continue
                content = fpath.read_text(errors="replace")
            except Exception:
                continue
            files_scanned += 1
            rel = str(fpath.relative_to(repo_root))
            if not _looks_seo_relevant(rel, content):
                continue

            # Collect URL patterns from this file
            url_patterns: list[tuple[str, int]] = []
            for pattern in _ROUTE_PATTERNS:
                for m in pattern.finditer(content):
                    # Group order varies; the URL is always the last group
                    url = m.group(m.lastindex)
                    if url and url.startswith(("/", "http")):
                        line = content[:m.start()].count("\n") + 1
                        url_patterns.append((url, line))
            # File-based routing inference (Next.js / SvelteKit-style)
            rel_norm = rel.replace("\\", "/")
            for prefix in ("/pages/", "/app/", "/src/pages/", "/src/app/"):
                if prefix in "/" + rel_norm:
                    idx = ("/" + rel_norm).index(prefix) + len(prefix)
                    suffix = ("/" + rel_norm)[idx:]
                    suffix = re.sub(r"/(page|index|route)\.(tsx?|jsx?|svelte|vue)$", "", suffix)
                    suffix = re.sub(r"\.(tsx?|jsx?|svelte|vue)$", "", suffix)
                    suffix = re.sub(r"\[\.\.\.([^\]]+)\]", r":\1*", suffix)
                    suffix = re.sub(r"\[([^\]]+)\]", r":\1", suffix)
                    inferred = "/" + suffix.strip("/")
                    url_patterns.append((inferred or "/", 1))
                    break

            emits = _detect_emits(content)
            missing = [e for e in _SEO_EMITS_ALL if e not in emits and not any(e2.startswith(e + ":") for e2 in emits)]
            todos = re.findall(r"(?:TODO|FIXME)[^\n]{0,200}(?:SEO|seo|meta|sitemap|canonical)[^\n]{0,200}", content)

            if url_patterns:
                seen_url = set()
                for url, line in url_patterns:
                    if url in seen_url:
                        continue
                    seen_url.add(url)
                    routes.append({
                        "url_pattern": url, "file": rel, "line": line,
                        "emits": emits, "missing": missing, "todos": todos[:3],
                    })
            elif emits:
                # SEO-surface helper file with no explicit route
                routes.append({
                    "url_pattern": "", "file": rel, "line": 1,
                    "emits": emits, "missing": missing, "todos": todos[:3],
                    "kind": "seo-helper",
                })
    err(f"  repo-scan: {files_scanned} files scanned, {len(routes)} SEO-relevant routes")
    (out_dir / "repo-routes.json").write_text(json.dumps({
        "scanned_at": _iso_now(),
        "repo_path": str(repo_root),
        "routes": routes,
    }, indent=2))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_run_summary(site: str, run_dir: Path) -> None:
    """Write a tiny human-readable run-summary.md so the agent + reporter
    can quote it directly."""
    out_md = [f"# Data pull — {site} — {datetime.now(timezone.utc).isoformat(timespec='seconds')}", ""]
    try:
        top5 = json.loads((run_dir / "data" / "gsc-top5-targets.json").read_text())
        out_md += [
            f"## Top-5 rank targets ({top5.get('summary', {}).get('total_candidates', 0)})",
            *(f"- pos {r['position']:.1f} | {r['impressions']} impr | {r['tier']:<8} | {r['query']}"
              for r in top5["rows"][:8]),
            "",
        ]
    except Exception: pass
    try:
        zc = json.loads((run_dir / "data" / "gsc-zero-click.json").read_text())
        out_md += [
            f"## Zero-click queries ({len(zc.get('rows', []))})",
            *(f"- pos {r['position']:.1f} | {r['impressions']} impr | {r['keys'][0]}" for r in zc["rows"][:8]),
            "",
        ]
    except Exception: pass
    (run_dir / "run-summary.md").write_text("\n".join(out_md))


def main() -> None:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--agent-id", default=None,
                   help="Framework orchestrator agent id (e.g. aisleprompt-seo-opportunity-agent). "
                        "When set, run dir is materialized from / synced back to Azure under "
                        "agents/<agent-id>/runs/<run-ts>/. Required with --run-ts.")
    p.add_argument("--run-ts", default=None,
                   help="UTC run timestamp (YYYYmmddTHHMMSSZ). Required with --agent-id.")
    args = p.parse_args()

    cfg = load_config_from_env()

    # Two execution modes:
    #   1. Azure-backed (NEW): orchestrator pins (--agent-id, --run-ts). Run
    #      dir lives in Azure; we materialize a tempdir, write everything
    #      into it, and sync_back on exit.
    #   2. Legacy local-FS: no args. Use cfg.make_run_dir() to create a run
    #      dir under cfg.runs_root (~/.reusable-agents/seo/runs/<site>/<ts>).
    #      Kept for ad-hoc invocation; the per-site orchestrator run.sh now
    #      always supplies --agent-id + --run-ts.
    if args.agent_id and args.run_ts:
        from framework.core.run_dir import RunDir
        run_ts = args.run_ts
        rd = RunDir(args.agent_id, run_ts, site=cfg.site_id)
        err(f"site={cfg.site_id} run_ts={run_ts} agent_id={args.agent_id} (azure)")
        token = get_access_token(cfg.auth_oauth_path())
        err(f"  token: {len(token)} chars")
        with rd.tempdir(skip_globs=["data/page-cache/*"]) as td:
            run_dir = td
            data_dir = run_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            _do_pulls(cfg, token, run_dir, data_dir)
        # Print stable handle the orchestrator captures.
        print(rd.handle)
        return

    if args.agent_id or args.run_ts:
        raise SystemExit("--agent-id and --run-ts must be supplied together")

    # Legacy path: local FS run dir.
    run_ts, run_dir = cfg.make_run_dir()
    err(f"site={cfg.site_id} run_ts={run_ts} (legacy local-fs)")

    token = get_access_token(cfg.auth_oauth_path())
    err(f"  token: {len(token)} chars")

    data_dir = run_dir / "data"
    data_dir.mkdir(exist_ok=True)
    _do_pulls(cfg, token, run_dir, data_dir)
    print(str(run_dir))


def _do_pulls(cfg, token: str, run_dir: Path, data_dir: Path) -> None:
    """The actual data-pull body, factored out so both Azure-backed and
    legacy local-FS paths share it."""
    pull_gsc(token, cfg["data_sources"]["gsc"]["site_url"],
             cfg["data_sources"]["gsc"].get("default_country_filter", "usa"),
             data_dir)
    pull_ga4(token, cfg["data_sources"]["ga4"]["property_id"], data_dir)
    if cfg["data_sources"].get("db"):
        pull_db(cfg["data_sources"]["db"], data_dir)
    else:
        err("  DB: not configured, skipping")

    # Optional: Google Ads (config-driven)
    ads_cfg = (cfg["data_sources"].get("google_ads") or {})
    if ads_cfg:
        try:
            pull_google_ads(token, ads_cfg, data_dir)
        except Exception as e:
            err(f"  Ads: unhandled error {e}")
    else:
        err("  Ads: not configured, skipping")

    # Optional: page-type inventory (config-driven)
    if cfg.get("page_inventory"):
        try:
            crawl_page_inventory(cfg, data_dir)
        except Exception as e:
            err(f"  inventory: unhandled error {e}")
    else:
        err("  inventory: page_inventory not configured, skipping")

    # Optional: repo route scan (drives implementation_outline.files)
    try:
        scan_repo(cfg, data_dir)
    except Exception as e:
        err(f"  repo-scan: unhandled error {e}")

    # Optional: site articles inventory (config-driven). Lets the analyzer
    # cross-ref every article we publish with GSC traffic data and surface
    # SEO-boost recs (snippet rewrite, title fix, orphan article, etc.).
    if cfg.get("articles"):
        try:
            pull_site_articles(cfg.get("articles"), data_dir)
        except Exception as e:
            err(f"  articles: unhandled error {e}")
    else:
        err("  articles: not configured, skipping")

    write_run_summary(cfg.site_id, run_dir)


if __name__ == "__main__":
    main()
