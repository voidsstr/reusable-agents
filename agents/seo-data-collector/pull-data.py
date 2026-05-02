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

# Bot/spam GSC queries appear as <word><digits> patterns (e.g.
# "celery25678", "rice45225", "thyme43450") — SEO probe traffic with
# zero clicks that floats into top5-targets and gets re-proposed every
# run as "build a keyword page". Real queries with digits ("rtx 5090",
# "2026 inflation") have a space, so anchored ^[a-z]+\d{2+}$ is safe.
_BOT_QUERY_PAT = re.compile(r'^[a-z]+\d{2,}$', re.I)


def _is_bot_noise(query: str, clicks: int) -> bool:
    return bool(_BOT_QUERY_PAT.match((query or "").strip())) and clicks == 0


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


def http_post(url: str, body: dict, headers: dict, *,
              retries: int = 3, base_delay: float = 2.0) -> dict:
    """POST with auto-retry on transient failures. Google's GSC + GA4 APIs
    occasionally return 5xx or hang the SSL handshake — without retries
    every cron run that hits the slow window 100% fails. Exponential
    backoff: 2s, 4s, 8s.

    Retries on:
      - HTTP 5xx (server errors)
      - HTTP 429 (rate limit)
      - SSL handshake timeouts
      - URLError / OSError (DNS / connect / read timeout)

    Re-raises immediately on 4xx (not 429) since those are caller errors
    that won't fix themselves with a retry.
    """
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_exc = e
            # 4xx (other than 429) is caller's fault — don't retry
            if 400 <= e.code < 500 and e.code != 429:
                raise
            err(f"  http_post {e.code} on attempt {attempt + 1}/{retries + 1}; retrying...")
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            last_exc = e
            err(f"  http_post {type(e).__name__} on attempt {attempt + 1}/{retries + 1}: {str(e)[:120]}; retrying...")
        if attempt < retries:
            _time.sleep(base_delay * (2 ** attempt))
    # Exhausted retries — re-raise the last exception so the caller sees it
    raise last_exc if last_exc else RuntimeError("http_post: unreachable")


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

    def _query_of(row):
        keys = row.get("keys") or []
        return keys[0] if keys else ""

    def _not_bot(row):
        return not _is_bot_noise(_query_of(row), row.get("clicks", 0))

    quick_wins = sorted(
        (r for r in raw.get("rows", []) if _not_bot(r) and 11 <= r["position"] <= 30 and r["impressions"] >= 5),
        key=lambda r: -r["impressions"],
    )
    (out_dir / "gsc-quick-wins.json").write_text(json.dumps({"rows": quick_wins[:200]}, indent=2))

    striking = sorted(
        (r for r in raw.get("rows", []) if _not_bot(r) and 4 <= r["position"] <= 10 and r["impressions"] >= 10),
        key=lambda r: -r["impressions"],
    )
    (out_dir / "gsc-striking-distance.json").write_text(json.dumps({"rows": striking[:200]}, indent=2))

    zero = sorted(
        (r for r in raw.get("rows", []) if _not_bot(r) and r["clicks"] == 0 and r["impressions"] >= 10 and r["position"] <= 20),
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
            query = keys[0]
            position = r.get("position", 100)
            impressions = r.get("impressions", 0)
            clicks = r.get("clicks", 0)
            if _is_bot_noise(query, clicks): continue
            if position > 50 or position <= 5 or impressions < impr_threshold: continue
            targets.append({
                "query": query,
                "position": round(position, 2),
                "impressions": impressions,
                "clicks": clicks,
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
        body_idx = cols.index("body") if "body" in cols else None
        audit_full_body = bool(articles_cfg.get("audit_full_body"))
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
            entry = {
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
            }
            # Embed body when revenue_focus / article-attribution audit
            # needs full text (audit_full_body=true). Capped per-article so
            # one giant guide can't bloat the run dir.
            if audit_full_body and body_idx is not None and body_idx < len(r):
                body = r[body_idx]
                if isinstance(body, str) and body:
                    entry["body"] = body[:200_000]  # 200KB hard cap per article
            inventory.append(entry)

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
) -> tuple[int, bytes, str, int]:
    """Fetch URL with a per-URL HTML cache (sha1(url) → filename).
    If a cache file exists, sends If-Modified-Since from its mtime;
    304 returns the cached body.
    Returns (status, body_bytes, cache_path_str, fetch_ms) — fetch_ms is
    the wall-clock time spent on the network round-trip (0 for 304 cache
    hits since no body was transferred). The analyzer's `cwv-ttfb-slow`
    rule compares fetch_ms against a threshold (default 600ms)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha1(url.encode()).hexdigest()
    cache_file = cache_dir / f"{sha}.html"
    headers = {"User-Agent": _PAGE_USER_AGENT}
    if cache_file.is_file():
        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=timezone.utc)
        headers["If-Modified-Since"] = mtime.strftime("%a, %d %b %Y %H:%M:%S GMT")
    req = urllib.request.Request(url, headers=headers)
    fetch_ms = 0
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            status = resp.status
            fetch_ms = int((time.monotonic() - started) * 1000)
            cache_file.write_bytes(body)
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache_file.is_file():
            body = cache_file.read_bytes()
            status = 304
            fetch_ms = 0  # served from cache, no body transfer
        else:
            time.sleep(throttle_s)
            raise
    time.sleep(throttle_s)
    return status, body, str(cache_file), fetch_ms


def _extract_page_meta(html: bytes) -> dict:
    """HTML metadata extraction — title, description, canonical, headings,
    body text, OG/Twitter cards, JSON-LD schema types, image alt-rate, and
    link counts. The expanded signal set drives the on-page SEO analyzer
    rules (onpage-* recs) modeled on the studio-supplies.com playbook.

    Avoids a hard bs4 dependency — regex-based on raw HTML.
    """
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
    meta_robots = _find(r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']([^"\']*)["\']')
    h1 = _find(r"<h1[^>]*>(.*?)</h1>")
    h1 = re.sub(r"<[^>]+>", "", h1).strip()

    # Heading counts
    h1_count = len(re.findall(r"<h1[\s>]", text, re.IGNORECASE))
    h2_count = len(re.findall(r"<h2[\s>]", text, re.IGNORECASE))
    h3_count = len(re.findall(r"<h3[\s>]", text, re.IGNORECASE))

    # Open Graph + Twitter cards
    og = dict(re.findall(
        r'<meta[^>]+property=["\']og:([^"\']+)["\'][^>]+content=["\']([^"\']*)',
        text, re.IGNORECASE,
    ))
    tw = sorted(set(re.findall(
        r'<meta[^>]+name=["\']twitter:([^"\']+)["\']',
        text, re.IGNORECASE,
    )))

    # JSON-LD blocks — collect @type values across all blocks (handles
    # both single-object and array forms and @graph nesting).
    # Also capture per-type field-name coverage so the analyzer can flag
    # incomplete schemas (e.g. Product missing offers.priceCurrency,
    # Article missing wordCount/dateModified) rather than only "missing
    # entirely". Studio-supplies' Product schema includes
    # sku+mpn+model+itemCondition+aggregateRating+image[18]+offers — the
    # superset that maximizes rich-result eligibility.
    jsonld_types: list[str] = []
    jsonld_field_coverage: dict[str, list[str]] = {}
    for blk in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.IGNORECASE | re.DOTALL,
    ):
        try:
            doc = json.loads(blk.strip())
        except Exception:
            continue
        def _walk(node):
            if isinstance(node, dict):
                t = node.get("@type")
                tnames: list[str] = []
                if isinstance(t, str):
                    tnames = [t]
                elif isinstance(t, list):
                    tnames = [str(x) for x in t]
                jsonld_types.extend(tnames)
                # Field coverage — record top-level keys this @type carries
                # so analyzer can compute per-type completeness.
                node_keys = sorted(k for k in node.keys() if not k.startswith("@"))
                # Also record offers.* / aggregateRating.* / brand.* sub-fields
                # since Google's product-rich-result spec requires
                # offers.priceCurrency / aggregateRating.reviewCount /
                # brand.name etc.
                for sub_key in ("offers", "aggregateRating", "brand", "author", "publisher"):
                    sub = node.get(sub_key)
                    if isinstance(sub, dict):
                        for k in sub.keys():
                            if not k.startswith("@"):
                                node_keys.append(f"{sub_key}.{k}")
                # image[] presence and count (Product wants ≥3 for rich result)
                if isinstance(node.get("image"), list):
                    node_keys.append(f"image.count={len(node['image'])}")
                elif isinstance(node.get("image"), (str, dict)):
                    node_keys.append("image.count=1")
                for tn in tnames:
                    jsonld_field_coverage.setdefault(tn, []).extend(node_keys)
                if isinstance(node.get("@graph"), list):
                    for c in node["@graph"]:
                        _walk(c)
            elif isinstance(node, list):
                for c in node:
                    _walk(c)
        _walk(doc)
    jsonld_types = sorted(set(jsonld_types))
    jsonld_field_coverage = {
        t: sorted(set(fields)) for t, fields in jsonld_field_coverage.items()
    }

    # Image alt + lazy-load coverage
    imgs = re.findall(r"<img\b[^>]*>", text, re.IGNORECASE)
    img_count = len(imgs)
    img_with_alt = sum(1 for i in imgs if re.search(r'\salt=["\'][^"\']{2,}', i))
    img_lazy = sum(1 for i in imgs if re.search(r'loading=["\']lazy["\']', i, re.IGNORECASE))

    # Link counts (inside <body>) + body-only counts (inside <main> or
    # <article>, falling back to <body>). The body-only counts are the
    # studio-supplies-relevant signal — sitewide nav + footer inflate
    # `internal_links` to 80+ on every page even when the actual
    # in-content link density is just 5-8.
    body_html = _find(r"<body[^>]*>(.*?)</body>") or text
    internal_links = len(re.findall(r'<a[^>]+href=["\']/[^"\']*["\']', body_html))
    external_links = len(re.findall(r'<a[^>]+href=["\']https?://[^"\']+["\']', body_html))
    nofollow_links = len(re.findall(r'<a[^>]+rel=["\'][^"\']*nofollow', body_html, re.IGNORECASE))

    main_html = (
        _find(r"<main[^>]*>(.*?)</main>")
        or _find(r"<article[^>]*>(.*?)</article>")
        or body_html
    )
    body_internal_links = len(re.findall(r'<a[^>]+href=["\']/[^"\']*["\']', main_html))
    body_external_links = len(re.findall(r'<a[^>]+href=["\']https?://[^"\']+["\']', main_html))

    # Internal-link target list — distinct same-origin paths this page links
    # to from <main>/<article> body. Drives the topical-cluster graph
    # (cluster-orphan detector) and the inbound-link regression alarm —
    # both compare per-page inbound counts vs prior runs to spot pages
    # that have been silently de-linked by a template or nav refactor.
    # Capped at 200 unique targets to avoid pathological pages blowing
    # up the JSONL line size.
    internal_link_targets_raw = re.findall(
        r'<a[^>]+href=["\'](/[^"\']*)["\']', main_html
    )
    internal_link_targets: list[str] = []
    seen_targets: set[str] = set()
    for href in internal_link_targets_raw:
        # Strip fragment + query so /foo, /foo?utm=1, /foo#anchor collapse
        path = href.split("#", 1)[0].split("?", 1)[0].rstrip("/") or "/"
        if path in seen_targets:
            continue
        seen_targets.add(path)
        internal_link_targets.append(path)
        if len(internal_link_targets) >= 200:
            break

    # Outbound-domain breakdown — classify external links so the analyzer
    # can compute the "≥3 authoritative outbound citations" signal that
    # high-E-E-A-T review pages emit. Same-domain (canonical) links are
    # excluded so a site linking back to its own variants doesn't inflate.
    outbound_domains: list[str] = []
    try:
        canonical_host = ""
        if canonical:
            try:
                canonical_host = urllib.parse.urlparse(canonical).hostname or ""
            except Exception:
                canonical_host = ""
        for href in re.findall(r'<a[^>]+href=["\'](https?://[^"\']+)["\']', main_html):
            try:
                d = (urllib.parse.urlparse(href).hostname or "").lower()
                if d and d != canonical_host:
                    outbound_domains.append(d)
            except Exception:
                pass
    except Exception:
        pass
    outbound_domains = sorted(set(outbound_domains))

    # Affiliate-link health — outbound Amazon links are the revenue path
    # for AislePrompt + SpecPicks. Flag when amazon.com links lack a
    # `?tag=<id>` query string (lost commission). Cheap deterministic
    # check that runs before any LLM pass.
    amazon_outbound = re.findall(r'<a[^>]+href=["\'](https?://[^"\']*amazon\.[a-z.]+/[^"\']+)["\']', main_html)
    amazon_outbound_total = len(amazon_outbound)
    amazon_outbound_tagged = sum(1 for h in amazon_outbound if "tag=" in h)

    # Body text + word count (capped excerpt)
    body_html_clean = re.sub(r"<script[^>]*>.*?</script>", " ", body_html, flags=re.IGNORECASE | re.DOTALL)
    body_html_clean = re.sub(r"<style[^>]*>.*?</style>", " ", body_html_clean, flags=re.IGNORECASE | re.DOTALL)
    body_text = re.sub(r"<[^>]+>", " ", body_html_clean)
    body_text = re.sub(r"\s+", " ", body_text).strip()
    word_count = len(body_text.split())
    body_excerpt = body_text[:8000]

    # FAQ heading present in body (a common signal even without FAQPage JSON-LD)
    faq_heading = bool(re.search(
        r'<h[1-3][^>]*>\s*(?:FAQ|Frequently Asked Questions?)',
        body_html, re.IGNORECASE,
    ))

    # Heading text arrays — used downstream by the studio-supplies-template
    # validator (review pages must hit 5+ of the canonical 9 H2 sections:
    # methodology, specs, independent-testing, owners-say, strengths,
    # limitations, who-should-buy, alternatives, sources/citations).
    def _heading_texts(tag: str, limit: int = 30) -> list[str]:
        out = []
        for h in re.findall(rf'<{tag}[^>]*>(.*?)</{tag}>', body_html, re.IGNORECASE | re.DOTALL):
            t = re.sub(r"<[^>]+>", "", h).strip()[:140]
            if t:
                out.append(t)
            if len(out) >= limit:
                break
        return out
    h2_texts = _heading_texts("h2")
    h3_texts = _heading_texts("h3")

    # Studio-supplies article-template section presence — checks each
    # canonical section against the joined H2+H3 text. Each section's
    # regex deliberately matches synonyms that competing review sites use,
    # so the validator works for AislePrompt/SpecPicks even though their
    # voice differs from studio-supplies'.
    heading_blob = " | ".join(h2_texts + h3_texts).lower()
    template_section_patterns = {
        "methodology":         r'how\s+we\s+(?:approached|tested|evaluated|reviewed|chose)|methodology|testing\s+process',
        "specs":               r'\bspecs?\b|specifications?|technical\s+details?',
        "independent_testing": r'independent\s+(?:testing|review|labs)|third[\s-]party\s+(?:test|review)',
        "owner_feedback":      r'(?:what\s+)?owners?\s+say|user\s+(?:reviews|feedback)|customer\s+(?:reviews|opinions)',
        "strengths":           r'\bstrengths?\b|\bpros\b|\badvantages?\b|what\s+we\s+(?:like|love)|highlights?',
        "limitations":         r'\blimitations?\b|\bcons\b|\bweaknesses?\b|\bdrawbacks?\b|what\s+we\s+don.?t',
        "buyer_persona":       r'who\s+(?:should\s+)?(?:buy|skip|use|consider|avoid)|best\s+for\b|ideal\s+for\b|not\s+for\b',
        "alternatives":        r'\balternatives?\b|competitor[s]?|similar\s+(?:options|products|picks)|other\s+options',
        "citations":           r'\bsources?\b|\bcitations?\b|\breferences?\b',
    }
    template_sections_seen = sorted(
        k for k, pat in template_section_patterns.items()
        if re.search(pat, heading_blob)
    )

    # Pros/Cons block presence — a discrete content pattern beyond the
    # template (some product pages have it without a full review structure).
    has_pros_cons = bool(
        re.search(r'\bpros\s*[&/]?\s*cons\b|\bpros\s+(?:and|&)\s+cons\b|\badvantages?\s*&\s*(?:disadvantages?|drawbacks?)', heading_blob)
    ) or (re.search(r'\bpros\b', heading_blob) and re.search(r'\bcons\b', heading_blob)) is not None

    # Source-attribution heading pattern: "(per X and Y)" / "according to X" /
    # "via X" — tells LLM-search the data has provenance.
    has_specs_attribution = bool(
        re.search(r'(?:\(per\s+|according\s+to\s+|sourced\s+from\s+|via\s+)[A-Z][A-Za-z\s,&]+', " | ".join(h2_texts))
    )

    # Body-visible publish/update date — independent of JSON-LD, this is
    # the date a reader (or LLM) sees rendered. Studio-supplies displays
    # `Apr 4, 2026` next to every guide title.
    visible_date_patterns = [
        r'\b(?:Updated|Published|Last\s+(?:updated|modified)|Posted|Reviewed)[\s:]+(?:[A-Z][a-z]+\s+\d{1,2},?\s+\d{4})',
        r'<time[^>]+datetime=["\'][^"\']+["\']',
        r'\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}\b',
    ]
    has_visible_date = any(
        re.search(p, body_html, re.IGNORECASE) for p in visible_date_patterns
    )

    # Hreflang inventory — for multi-locale sites the analyzer needs to
    # confirm every alternate-language URL declared elsewhere on the site
    # has a matching hreflang link on this page (or at minimum that the
    # page declares its own hreflang for the canonical locale).
    hreflang_links = sorted(set(
        re.findall(
            r'<link[^>]+rel=["\']alternate["\'][^>]+hreflang=["\']([^"\']+)["\']',
            text, re.IGNORECASE,
        )
    ))

    # Footer trust-link reachability — privacy / terms / contact / about /
    # affiliate-disclosure must be reachable from every page (Google E-E-A-T
    # baseline + FTC affiliate-disclosure requirement). We flag absence by
    # looking for href patterns; the analyzer aggregates the "missing on
    # how many pages" signal.
    footer_trust_keys = {
        "privacy":   r'/(?:privacy|privacy-policy)/?(?:$|["\'#?])',
        "terms":     r'/(?:terms|terms-of-service|tos|terms-conditions)/?(?:$|["\'#?])',
        "contact":   r'/(?:contact|contact-us)/?(?:$|["\'#?])',
        "about":     r'/(?:about|about-us)/?(?:$|["\'#?])',
        "affiliate": r'/(?:affiliate-disclosure|disclosure|disclosures)/?(?:$|["\'#?])',
    }
    footer_trust_links: dict[str, bool] = {}
    for key, pat in footer_trust_keys.items():
        footer_trust_links[key] = bool(re.search(pat, body_html, re.IGNORECASE))

    # Breadcrumb HTML <-> JSON-LD parity. Visible breadcrumb is detected
    # via common patterns (nav[aria-label="Breadcrumb"], ol.breadcrumb,
    # rel="up" anchors, or " › " / " > " / " / " separators between links).
    # JSON-LD breadcrumb items are extracted from the BreadcrumbList block.
    breadcrumb_visible_items: list[str] = []
    breadcrumb_html = ""
    for pat in [
        r'<nav[^>]+(?:aria-label|class)=["\'][^"\']*[Bb]readcrumb[^"\']*["\'][^>]*>(.*?)</nav>',
        r'<ol[^>]+class=["\'][^"\']*breadcrumb[^"\']*["\'][^>]*>(.*?)</ol>',
        r'<ul[^>]+class=["\'][^"\']*breadcrumb[^"\']*["\'][^>]*>(.*?)</ul>',
        r'<div[^>]+class=["\'][^"\']*breadcrumb[^"\']*["\'][^>]*>(.*?)</div>',
    ]:
        m = re.search(pat, body_html, re.IGNORECASE | re.DOTALL)
        if m:
            breadcrumb_html = m.group(1)
            break
    if breadcrumb_html:
        breadcrumb_visible_items = [
            re.sub(r"<[^>]+>", "", a).strip()[:120]
            for a in re.findall(r'<a[^>]*>(.*?)</a>', breadcrumb_html, re.IGNORECASE | re.DOTALL)
        ]
        breadcrumb_visible_items = [t for t in breadcrumb_visible_items if t]
    breadcrumb_jsonld_items: list[str] = []
    for blk in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.IGNORECASE | re.DOTALL,
    ):
        try:
            doc = json.loads(blk.strip())
        except Exception:
            continue
        def _bc_walk(node):
            if isinstance(node, dict):
                if node.get("@type") == "BreadcrumbList":
                    for item in node.get("itemListElement", []) or []:
                        nm = item.get("name") if isinstance(item, dict) else None
                        if nm:
                            breadcrumb_jsonld_items.append(str(nm)[:120])
                if isinstance(node.get("@graph"), list):
                    for c in node["@graph"]:
                        _bc_walk(c)
            elif isinstance(node, list):
                for c in node:
                    _bc_walk(c)
        _bc_walk(doc)

    # Trust-signal density — keyword count for conversion-page trust cues.
    # Studio-supplies' product pages have visible "Sony USA Authorized" +
    # "Full SONY USA Warranty" callouts. We count occurrences of common
    # trust phrases across the body text (cheap heuristic; LLM checklist
    # also has a deeper trust-signals-missing category).
    trust_signal_terms = [
        r"\bguarantee\b", r"\bwarrant(?:y|ies)\b", r"\bauthor[i]z(?:ed|ation)\b",
        r"\bcertified\b", r"\bofficial\b", r"\bverified\b", r"\bsecure\b",
        r"\breturn(?:s|ed)?\s+polic", r"\bmoney[\s-]?back\b", r"\b30[\s-]?day\b",
        r"\bfree\s+ship", r"\btrusted\b", r"\bsatisfaction\b",
    ]
    trust_signal_count = 0
    for pat in trust_signal_terms:
        trust_signal_count += len(re.findall(pat, body_text, re.IGNORECASE))

    # FAQ quality — extract Q/A counts + answer-length stats from FAQPage
    # JSON-LD blocks. Studio-supplies' product pages run 5 questions × ~80
    # words per answer; thin FAQs (<3 questions or <20-word answers) miss
    # the citation-extraction sweet spot for AI search.
    faq_question_count = 0
    faq_short_answer_count = 0
    faq_avg_answer_words = 0.0
    faq_answer_words: list[int] = []
    for blk in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        text, re.IGNORECASE | re.DOTALL,
    ):
        try:
            doc = json.loads(blk.strip())
        except Exception:
            continue
        def _faq_walk(node):
            nonlocal faq_question_count, faq_short_answer_count
            if isinstance(node, dict):
                if node.get("@type") == "FAQPage":
                    for q in node.get("mainEntity", []) or []:
                        if not isinstance(q, dict):
                            continue
                        faq_question_count += 1
                        ans = q.get("acceptedAnswer") or {}
                        text_val = ans.get("text", "") if isinstance(ans, dict) else ""
                        wc = len(str(text_val).split())
                        faq_answer_words.append(wc)
                        if wc < 20:
                            faq_short_answer_count += 1
                if isinstance(node.get("@graph"), list):
                    for c in node["@graph"]:
                        _faq_walk(c)
            elif isinstance(node, list):
                for c in node:
                    _faq_walk(c)
        _faq_walk(doc)
    if faq_answer_words:
        faq_avg_answer_words = round(sum(faq_answer_words) / len(faq_answer_words), 1)

    # CollectionPage / ItemList numberOfItems coverage. Google's rich-result
    # docs say ItemList must declare numberOfItems for carousel eligibility.
    has_itemlist_numberOfItems = (
        "numberOfItems" in (jsonld_field_coverage.get("ItemList") or [])
        or "numberOfItems" in (jsonld_field_coverage.get("CollectionPage") or [])
    )

    # Featured-product Amazon CTA position — for revenue_focus sites, an
    # above-the-fold Amazon affiliate link on the PDP is the conversion
    # path. We compute a coarse signal: does the FIRST 25% of <main>
    # already contain at least one outbound amazon.com link?
    main_quartile = main_html[: max(1, len(main_html) // 4)]
    has_amazon_above_fold = bool(
        re.search(r'<a[^>]+href=["\'][^"\']*amazon\.[a-z.]+/[^"\']+["\']', main_quartile)
    )

    # Performance-hint tags in <head>
    head_html = _find(r"<head[^>]*>(.*?)</head>") or text
    preconnect = len(re.findall(r'rel=["\']preconnect["\']', head_html, re.IGNORECASE))
    dns_prefetch = len(re.findall(r'rel=["\']dns-prefetch["\']', head_html, re.IGNORECASE))
    preload = len(re.findall(r'rel=["\']preload["\']', head_html, re.IGNORECASE))

    return {
        "title": title,
        "description": description,
        "canonical": canonical,
        "meta_robots": meta_robots,
        "h1": h1,
        "h1_count": h1_count,
        "h2_count": h2_count,
        "h3_count": h3_count,
        "h2_texts": h2_texts,
        "h3_texts": h3_texts,
        "og_keys": sorted(og.keys()),
        "twitter_card_keys": tw,
        "jsonld_types": jsonld_types,
        "jsonld_field_coverage": jsonld_field_coverage,
        "img_count": img_count,
        "img_with_alt": img_with_alt,
        "img_lazy": img_lazy,
        "internal_links": internal_links,
        "external_links": external_links,
        "nofollow_links": nofollow_links,
        "body_internal_links": body_internal_links,
        "body_external_links": body_external_links,
        "internal_link_targets": internal_link_targets,
        "outbound_domains": outbound_domains,
        "outbound_domain_count": len(outbound_domains),
        "amazon_outbound_total": amazon_outbound_total,
        "amazon_outbound_tagged": amazon_outbound_tagged,
        "word_count": word_count,
        "faq_heading": faq_heading,
        "template_sections_seen": template_sections_seen,
        "has_pros_cons": has_pros_cons,
        "has_specs_attribution": has_specs_attribution,
        "has_visible_date": has_visible_date,
        "hreflang_links": hreflang_links,
        "footer_trust_links": footer_trust_links,
        "breadcrumb_visible_items": breadcrumb_visible_items,
        "breadcrumb_jsonld_items": breadcrumb_jsonld_items,
        "trust_signal_count": trust_signal_count,
        "faq_question_count": faq_question_count,
        "faq_short_answer_count": faq_short_answer_count,
        "faq_avg_answer_words": faq_avg_answer_words,
        "has_itemlist_numberOfItems": has_itemlist_numberOfItems,
        "has_amazon_above_fold": has_amazon_above_fold,
        "preconnect": preconnect,
        "dns_prefetch": dns_prefetch,
        "preload": preload,
        "body_text": body_excerpt,
    }


def audit_site_signals(site_cfg, out_dir: Path) -> None:
    """Site-wide SEO signals — robots.txt rules + homepage schema.

    Audits:
      • robots.txt presence, AI-crawler allow rules (GPTBot, ClaudeBot,
        PerplexityBot, OAI-SearchBot, ChatGPT-User, Bingbot, etc.),
        Sitemap directive, /admin /cart /checkout disallows.
      • Homepage JSON-LD — WebSite + Organization presence (modeled on
        studio-supplies.com which carries both at the root).
      • robots.txt size + AI-crawler coverage score.

    Writes site-signals.json. Best-effort — every probe wraps any
    network/parse error so a partial result still ships.
    """
    domain = site_cfg.get("site", {}).get("domain", "")
    if not domain:
        err("  site-signals: no domain configured, skipping")
        return
    base = f"https://{domain}"

    signals: dict = {
        "domain": domain,
        "robots": {},
        "homepage": {},
    }

    # ── robots.txt ───────────────────────────────────────────────────
    try:
        url = f"{base}/robots.txt"
        cache_dir = out_dir / "page-cache"
        status, body, _, _ = _http_get_with_cache(url, cache_dir)
        text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
        signals["robots"]["status"] = status
        signals["robots"]["bytes"] = len(text)

        # AI / LLM / chat-agent crawlers — every user-agent must be
        # named EXPLICITLY in robots.txt. A `User-agent: *` block does
        # NOT cover most of these; many bots fall back to conservative
        # behavior or skip the site entirely when their exact name
        # isn't listed. Source: each crawler's published docs (2026-04).
        # We probe every name; the analyzer flags any missing block.
        ai_bots = [
            # OpenAI — four distinct user-agents
            "GPTBot", "OAI-SearchBot", "ChatGPT-User", "OpenAI-Image",
            # Anthropic — current + legacy + new search/user agents
            "ClaudeBot", "anthropic-ai", "claude-web",
            "Claude-User", "Claude-SearchBot",
            # Perplexity
            "PerplexityBot", "Perplexity-User",
            # Google — core + AI-Overviews + Bard/Gemini opt-out token
            "Google-Extended", "GoogleOther",
            "Googlebot", "Googlebot-Image", "Googlebot-News", "Googlebot-Video",
            "AdsBot-Google", "Mediapartners-Google",
            # Apple Intelligence
            "Applebot", "Applebot-Extended",
            # Microsoft / Bing / Copilot
            "Bingbot", "BingPreview", "msnbot",
            # Amazon (Alexa + Amazon AI)
            "Amazonbot",
            # ByteDance / TikTok / Doubao
            "Bytespider",
            # Meta (Llama crawler + link-preview)
            "Meta-ExternalAgent", "FacebookBot", "facebookexternalhit",
            # Cohere
            "cohere-ai", "cohere-training-data-crawler",
            # Mistral
            "MistralAI-User",
            # You.com / Kagi / DuckDuckGo / Huawei / Yandex
            "YouBot", "Kagibot", "DuckAssistBot", "PetalBot", "YandexBot",
            # Diffbot (real-time data backbone for many LLM agents)
            "Diffbot",
            # Common Crawl (most LLM training corpora trace back here)
            "CCBot",
            # AI2 (OLMo / Dolma)
            "ai2bot", "ai2bot-Dolma",
            # Image AI
            "ImagesiftBot",
            # Social agents that surface links into AI summaries
            "Twitterbot", "LinkedInBot", "Slackbot", "Discordbot", "TelegramBot",
        ]
        # Per-bot status: present (User-agent declared), allowed (Allow: / present)
        bot_blocks: dict = {}
        for bot in ai_bots:
            block_pat = re.compile(
                rf"User-agent:\s*{re.escape(bot)}\s*\n(.*?)(?=\nUser-agent:|\Z)",
                re.IGNORECASE | re.DOTALL,
            )
            m = block_pat.search(text)
            if m:
                blk = m.group(1)
                bot_blocks[bot] = {
                    "declared": True,
                    "has_allow_root": bool(re.search(r"^\s*Allow:\s*/\s*$", blk, re.MULTILINE)),
                    "has_disallow_root": bool(re.search(r"^\s*Disallow:\s*/\s*$", blk, re.MULTILINE)),
                }
            else:
                bot_blocks[bot] = {
                    "declared": False,
                    "has_allow_root": False,
                    "has_disallow_root": False,
                }
        signals["robots"]["bots"] = bot_blocks
        signals["robots"]["sitemap_directive"] = bool(re.search(r"(?im)^\s*Sitemap:\s*https?://", text))
        signals["robots"]["disallow_admin"] = bool(re.search(r"(?im)^\s*Disallow:\s*/admin", text))
        signals["robots"]["disallow_cart"] = bool(re.search(r"(?im)^\s*Disallow:\s*/cart", text))
        signals["robots"]["disallow_checkout"] = bool(re.search(r"(?im)^\s*Disallow:\s*/checkout", text))
    except Exception as e:
        signals["robots"]["error"] = str(e)[:300]

    # ── homepage schema sniff ────────────────────────────────────────
    try:
        cache_dir = out_dir / "page-cache"
        status, body, _, home_fetch_ms = _http_get_with_cache(base + "/", cache_dir)
        meta = _extract_page_meta(body) if isinstance(body, (bytes, bytearray)) else _extract_page_meta(str(body).encode())
        signals["homepage"] = {
            "status": status,
            "fetch_ms": home_fetch_ms,
            "title": meta.get("title", ""),
            "description": meta.get("description", ""),
            "canonical": meta.get("canonical", ""),
            "og_keys": meta.get("og_keys", []),
            "twitter_card_keys": meta.get("twitter_card_keys", []),
            "jsonld_types": meta.get("jsonld_types", []),
            "h1_count": meta.get("h1_count", 0),
            "internal_links": meta.get("internal_links", 0),
            "word_count": meta.get("word_count", 0),
            "footer_trust_links": meta.get("footer_trust_links", {}),
            "hreflang_links": meta.get("hreflang_links", []),
        }
    except Exception as e:
        signals["homepage"]["error"] = str(e)[:300]

    (out_dir / "site-signals.json").write_text(json.dumps(signals, indent=2))
    err(f"  site-signals: wrote {(out_dir / 'site-signals.json').name}")


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
    # Always persist the full sitemap URL list so the analyzer's
    # content-gap detector can count pages matching `coverage_targets`
    # patterns regardless of whether they were sampled into pages-by-type.
    (out_dir / "sitemap-urls.json").write_text(json.dumps({
        "fetched_from": sitemap_url,
        "count": len(all_urls),
        "urls": all_urls,
    }))
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
                    status, body, cache_path, fetch_ms = _http_get_with_cache(url, cache_dir)
                    rec["status"] = status
                    rec["html_size"] = len(body)
                    rec["cache_path"] = cache_path
                    rec["fetch_ms"] = fetch_ms
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


def pull_rich_results_test(token: str, data_dir: Path, cfg: dict) -> None:
    """Test sampled pages through Google Rich Results Test API + Schema Markup Validator.

    Reads already-crawled URLs from pages-by-type.jsonl (no new HTTP fetches for
    the main site). Calls:
      1. Google Rich Results Test API — per-URL rich-result eligibility + per-type
         issues (ERROR/WARNING). Uses the same OAuth token as GSC queries.
      2. schema.org/validator — structural validation of JSON-LD blocks per
         validator.schema.org (same rules Google uses internally).

    Writes data/rich-results-test.jsonl (one JSON object per URL).
    Capped at max_pages_rrt (default 40) and max_pages_smv (default 20) to
    preserve GSC API quota across multiple runs/day. Non-fatal — failures for
    individual URLs are logged but don't abort the run.

    Config keys (all optional, under rich_results_test: in site.yaml):
      enabled: true/false  (default true)
      max_pages_rrt: N     (default 40)
      max_pages_smv: N     (default 20)
    """
    rrt_cfg = cfg.get("rich_results_test") or {}
    if str(rrt_cfg.get("enabled", "true")).lower() == "false":
        err("  rich-results: disabled in site config, skipping")
        return

    pages_file = data_dir / "pages-by-type.jsonl"
    if not pages_file.is_file():
        err("  rich-results: pages-by-type.jsonl not found — run crawl_page_inventory first")
        return

    # Read sampled URLs from the already-crawled inventory
    urls_seen: set = set()
    url_entries: list[dict] = []
    with pages_file.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            u = rec.get("url", "")
            if u and u not in urls_seen and 200 <= int(rec.get("status", 0) or 0) < 300:
                url_entries.append({"url": u, "type": rec.get("type", "")})
                urls_seen.add(u)

    # Always prepend the homepage (highest-value single URL for rich-result checks)
    domain = (cfg.get("site") or {}).get("domain", "")
    if domain:
        hp = f"https://{domain}/"
        if hp not in urls_seen:
            url_entries.insert(0, {"url": hp, "type": "home"})

    max_pages_rrt = int(rrt_cfg.get("max_pages_rrt", 40))
    max_pages_smv = int(rrt_cfg.get("max_pages_smv", 20))
    urls_rrt = url_entries[:max_pages_rrt]
    urls_smv = url_entries[:max_pages_smv]

    err(f"  rich-results: {len(urls_rrt)} pages → RRT,  {len(urls_smv)} pages → SMV")

    # Accumulate per-URL results keyed by URL
    results: dict[str, dict] = {}

    # ── 1. Google Rich Results Test API ─────────────────────────────────
    # POST https://searchconsole.googleapis.com/v1/urlTestingTools/richResultsTest:run
    # Auth: same Bearer token used for GSC queries (search-console scope).
    # Quota: 2,000 calls/day per Search Console property — well within budget
    # for 40 pages × 5 runs/day = 200 calls.
    RRT_ENDPOINT = (
        "https://searchconsole.googleapis.com/v1/urlTestingTools/richResultsTest:run"
    )
    rrt_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    for entry in urls_rrt:
        url = entry["url"]
        rec = results.setdefault(url, {"url": url, "type": entry["type"]})
        try:
            resp = http_post(
                RRT_ENDPOINT,
                {"url": url, "userAgent": "MOBILE"},
                rrt_headers,
                retries=2,
                base_delay=3.0,
            )
            items = resp.get("richResultsItems") or []
            # Flatten to a compact summary: per rich-result-type → list of issues
            summary: list[dict] = []
            for rich_type in items:
                type_name = rich_type.get("richResultType", "")
                type_issues: list[dict] = []
                for item in (rich_type.get("items") or []):
                    for issue in (item.get("issues") or []):
                        type_issues.append({
                            "severity": issue.get("severity", ""),
                            "message": issue.get("issueMessage", ""),
                            "type": issue.get("issueType", ""),
                        })
                summary.append({
                    "rich_result_type": type_name,
                    "eligible": not any(
                        i["severity"] == "ERROR" for i in type_issues
                    ),
                    "issues": type_issues,
                })
            rec["rich_results_test"] = {
                "test_status": (resp.get("testStatus") or {}).get("status", "UNKNOWN"),
                "types_detected": [s["rich_result_type"] for s in summary],
                "has_errors": any(
                    not s["eligible"] for s in summary
                ),
                "has_warnings": any(
                    any(i["severity"] == "WARNING" for i in s["issues"])
                    for s in summary
                ),
                "summary": summary,
            }
            err(f"  rich-results: RRT {url} → {', '.join(s['rich_result_type'] for s in summary) or 'no types'}")
        except Exception as e:
            err(f"  rich-results: RRT failed for {url}: {str(e)[:120]}")
            rec["rich_results_test"] = {"error": str(e)[:200]}
        time.sleep(0.5)

    # ── 2. Schema Markup Validator (validator.schema.org) ───────────────
    # POST https://validator.schema.org/validate with form body url=<url>.
    # Returns JSON: {"items": [{"id","type","properties","errors","warnings"}], "meta":{...}}
    # Structural validation — property type mismatches, missing required fields,
    # malformed values. Uses the same schema.org vocabulary rules as Google.
    SMV_ENDPOINT = "https://validator.schema.org/validate"
    for entry in urls_smv:
        url = entry["url"]
        rec = results.setdefault(url, {"url": url, "type": entry["type"]})
        try:
            form_data = urllib.parse.urlencode({"url": url}).encode("utf-8")
            smv_req = urllib.request.Request(
                SMV_ENDPOINT,
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; SEO-agent/1.0)",
                },
            )
            with urllib.request.urlopen(smv_req, timeout=45) as smv_resp:
                smv_body = smv_resp.read().decode("utf-8", errors="replace")
            smv_data = json.loads(smv_body)
            items = smv_data.get("items") or []
            total_errors = sum(len(item.get("errors") or []) for item in items)
            total_warnings = sum(len(item.get("warnings") or []) for item in items)
            # Compact representation — keep only type + errors + warnings per item
            compact_items = []
            for item in items:
                compact_items.append({
                    "type": item.get("type", ""),
                    "errors": [
                        {
                            "property": e.get("property", ""),
                            "message": e.get("message", ""),
                        }
                        for e in (item.get("errors") or [])
                    ],
                    "warnings": [
                        {
                            "property": w.get("property", ""),
                            "message": w.get("message", ""),
                        }
                        for w in (item.get("warnings") or [])
                    ],
                })
            rec["schema_markup_validator"] = {
                "total_errors": total_errors,
                "total_warnings": total_warnings,
                "items": compact_items,
            }
            err(f"  rich-results: SMV {url} → {len(items)} types, {total_errors} errors, {total_warnings} warnings")
        except Exception as e:
            err(f"  rich-results: SMV failed for {url}: {str(e)[:120]}")
            rec.setdefault("schema_markup_validator", {})["error"] = str(e)[:200]
        time.sleep(1.0)  # be polite to validator.schema.org

    # Write output file
    out_path = data_dir / "rich-results-test.jsonl"
    with out_path.open("w") as fh:
        for rec in results.values():
            fh.write(json.dumps(rec) + "\n")
    err(f"  rich-results: wrote {len(results)} URL records → rich-results-test.jsonl")


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

    # Site-wide signal audit — robots.txt + homepage schema. Always
    # runs (no opt-in needed) so the analyzer's robots-/home- rules
    # have data to work with on every site. Failures are non-fatal.
    try:
        audit_site_signals(cfg, data_dir)
    except Exception as e:
        err(f"  site-signals: unhandled error {e}")

    # Optional: page-type inventory (config-driven)
    if cfg.get("page_inventory"):
        try:
            crawl_page_inventory(cfg, data_dir)
        except Exception as e:
            err(f"  inventory: unhandled error {e}")
    else:
        err("  inventory: page_inventory not configured, skipping")

    # Rich Results Test + Schema Markup Validator.
    # Runs after crawl_page_inventory so pages-by-type.jsonl exists.
    # Falls back gracefully when pages-by-type.jsonl is absent (inventory
    # not configured) by using only the homepage URL.
    try:
        pull_rich_results_test(token, data_dir, cfg)
    except Exception as e:
        err(f"  rich-results: unhandled error {e}")

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
