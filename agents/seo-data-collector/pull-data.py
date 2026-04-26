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

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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
    """Run the configured DB query script. The script's stdout is parsed
    as JSON and written to db-stats.json."""
    if db_cfg.get("type") == "docker-exec":
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
    else:
        err(f"  DB type {db_cfg.get('type')!r} not yet implemented")


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
    cfg = load_config_from_env()
    run_ts, run_dir = cfg.make_run_dir()
    err(f"site={cfg.site_id} run_ts={run_ts}")

    token = get_access_token(cfg.auth_oauth_path())
    err(f"  token: {len(token)} chars")

    data_dir = run_dir / "data"
    data_dir.mkdir(exist_ok=True)
    pull_gsc(token, cfg["data_sources"]["gsc"]["site_url"],
             cfg["data_sources"]["gsc"].get("default_country_filter", "usa"),
             data_dir)
    pull_ga4(token, cfg["data_sources"]["ga4"]["property_id"], data_dir)
    if cfg["data_sources"].get("db"):
        pull_db(cfg["data_sources"]["db"], data_dir)
    else:
        err("  DB: not configured, skipping")

    write_run_summary(cfg.site_id, run_dir)
    print(str(run_dir))


if __name__ == "__main__":
    main()
