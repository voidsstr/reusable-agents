"""Site goals tracker — defines + tracks the canonical per-site SEO + conversion
goals for AislePrompt and SpecPicks. Every per-site agent contributes to these
goals, but they live in the SITE'S own goal sets so the metrics survive
individual-agent restructuring.

What this script does on each daily run:
  1. Pulls fresh metrics from GA4 + GSC + DB:
      - GSC: 30d organic clicks, 30d organic impressions
      - GA4: 30d Instacart cart-creates (aisleprompt) / Amazon-clicks /
             eBay-clicks (specpicks)
      - DB: page counts (recipes, products, articles)
      - GSC URL Inspection cache: % of URLs in "Submitted and indexed"
        coverage state
  2. Calls metric_helper.record_many() with all metrics for THIS site,
     which updates active.json + per-goal jsonl + timeseries-cache.json
     atomically.

Conversion goals (the ultimate KPIs):
  AislePrompt → instacart_cart_creates_30d  — Instacart button click is
    the monetization endpoint. Driving organic traffic to recipes that
    converts to cart creates is the whole point of the site.
  SpecPicks   → amazon_clicks_30d + ebay_clicks_30d — affiliate
    monetization on products linked from review/buying-guide pages.

Leading-indicator goals (move first, predict conversion):
  Both → organic_clicks_30d (GSC), organic_impressions_30d (GSC),
         indexed_pages_count (GSC URL Inspection), unknown_to_google_count

Run mode: invoked by per-site wrappers in nsc-assistant/agents/
<aisleprompt|specpicks>-site-goals-tracker/. Each wrapper sets
SITE_GOALS_SITE=<aisleprompt|specpicks> and execs this script.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from framework.core import metric_helper, goals as goals_mod


REFRESH_SCRIPT = (REPO / "agents" / "seo-data-collector" / "refresh-token.py").resolve()
OAUTH_FILE = Path(os.path.expanduser("~/.reusable-agents/seo/.oauth.json"))


# Per-site canonical config — lives here in the shared agent so we have a
# single source of truth for the goal definitions. The DB URLs come from
# env vars set by per-site wrappers.
SITE_PROFILES: dict[str, dict] = {
    "aisleprompt": {
        "host": "aisleprompt.com",
        "gsc_site_url": "sc-domain:aisleprompt.com",
        "ga4_property_id": "529023310",
        "db_env": "AISLEPROMPT_DATABASE_URL",
        # GA4 conversion event names — instacart-cart is the money event.
        # The instacart-clicks and amazon-clicks events show outbound
        # interest even when GA4 doesn't yet record a confirmed cart.
        "conversion_events": ["instacart-cart", "instacart-clicks", "amazon-clicks"],
        "page_count_sql": "SELECT COUNT(*) FROM recipe_catalog WHERE COALESCE(is_active, TRUE) = TRUE",
        "agent_id": "aisleprompt-site-goals-tracker",
    },
    "specpicks": {
        "host": "specpicks.com",
        "gsc_site_url": "sc-domain:specpicks.com",
        "ga4_property_id": "531274480",
        "db_env": "SPECPICKS_DATABASE_URL",
        "conversion_events": ["amazon-clicks", "ebay-clicks"],
        "page_count_sql": (
            "SELECT (SELECT COUNT(*) FROM products WHERE is_active = true) + "
            "(SELECT COUNT(*) FROM editorial_articles WHERE status = 'published') + "
            "(SELECT COUNT(*) FROM hardware_specs)"
        ),
        "agent_id": "specpicks-site-goals-tracker",
    },
}


def err(*a) -> None:
    print(*a, file=sys.stderr)


def get_access_token() -> str:
    out = subprocess.check_output(
        [sys.executable, str(REFRESH_SCRIPT), "--oauth-file", str(OAUTH_FILE)],
        stderr=subprocess.PIPE, timeout=60,
    ).decode().strip()
    if not out:
        raise SystemExit("refresh-token.py returned empty output")
    return out


def gsc_query(token: str, site_url: str, body: dict) -> dict:
    enc = urllib.parse.quote(site_url, safe="")
    url = f"https://www.googleapis.com/webmasters/v3/sites/{enc}/searchAnalytics/query"
    req = urllib.request.Request(
        url, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def ga4_run_report(token: str, property_id: str, body: dict) -> dict:
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    req = urllib.request.Request(
        url, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def collect_metrics(profile: dict) -> dict[str, float]:
    """Pull every metric this script tracks, returning a {goal_id: value} dict."""
    metrics: dict[str, float] = {}
    today = datetime.now(timezone.utc).date()
    start_30d = (today.replace(day=1) if False else _days_ago(today, 30)).isoformat()
    end_today = today.isoformat()

    # --- GSC: organic clicks + impressions (30d) ---
    try:
        token = get_access_token()
        gsc_resp = gsc_query(token, profile["gsc_site_url"], {
            "startDate": start_30d, "endDate": end_today,
            "dimensions": [],  # totals only
            "rowLimit": 1, "type": "web",
        })
        rows = gsc_resp.get("rows") or []
        if rows:
            metrics["goal-organic-clicks-30d"] = float(rows[0].get("clicks", 0))
            metrics["goal-organic-impressions-30d"] = float(rows[0].get("impressions", 0))
        else:
            metrics["goal-organic-clicks-30d"] = 0
            metrics["goal-organic-impressions-30d"] = 0
    except Exception as e:
        err(f"  GSC totals failed: {e}")

    # --- GA4: conversion events (30d) ---
    try:
        ga_resp = ga4_run_report(token, profile["ga4_property_id"], {
            "dateRanges": [{"startDate": start_30d, "endDate": end_today}],
            "dimensions": [{"name": "eventName"}],
            "metrics": [{"name": "eventCount"}],
        })
        events = {row["dimensionValues"][0]["value"]: int(row["metricValues"][0]["value"])
                  for row in (ga_resp.get("rows") or [])}
        total_conv = 0
        for ev in profile.get("conversion_events", []):
            n = events.get(ev, 0)
            metrics[f"goal-{_slug(ev)}-30d"] = float(n)
            total_conv += n
        metrics["goal-total-conversions-30d"] = float(total_conv)
    except Exception as e:
        err(f"  GA4 conversions failed: {e}")

    # --- DB: total active pages ---
    db_url = os.environ.get(profile["db_env"]) or _db_fallback(profile)
    if db_url:
        try:
            conn = psycopg2.connect(db_url)
            cur = conn.cursor()
            cur.execute(profile["page_count_sql"])
            metrics["goal-active-pages-count"] = float(cur.fetchone()[0])
            conn.close()
        except Exception as e:
            err(f"  DB page count failed: {e}")

    # --- GSC URL Inspection cache: indexing coverage ---
    coverage_file = Path(os.path.expanduser(
        f"~/.reusable-agents/gsc-coverage-auditor/{_short_site(profile)}-coverage.jsonl"
    ))
    if coverage_file.is_file():
        try:
            latest: dict[str, str] = {}
            with coverage_file.open() as fh:
                for raw in fh:
                    try:
                        row = json.loads(raw)
                    except Exception:
                        continue
                    url = row.get("url")
                    cs = row.get("coverageState") or ""
                    ts = row.get("inspected_at") or ""
                    prev = latest.get(url, ("", ""))
                    if ts > prev[1]:
                        latest[url] = (cs, ts)
            states: dict[str, int] = {}
            for cs, _ in latest.values():
                states[cs] = states.get(cs, 0) + 1
            total = sum(states.values()) or 1
            indexed = states.get("Submitted and indexed", 0)
            unknown = states.get("URL is unknown to Google", 0)
            crawled_not_indexed = (
                states.get("Crawled - currently not indexed", 0)
                + states.get("Crawled — currently not indexed", 0)
            )
            metrics["goal-indexed-pages-pct"] = round(100.0 * indexed / total, 2)
            metrics["goal-unknown-to-google-count"] = float(unknown)
            metrics["goal-crawled-not-indexed-count"] = float(crawled_not_indexed)
            metrics["goal-inspected-urls-total"] = float(total)
        except Exception as e:
            err(f"  coverage analysis failed: {e}")

    return metrics


def write_goal_definitions(profile: dict, agent_id: str) -> None:
    """Idempotent: write the goal schema for this site if not already present.
    Existing goals' progress_history is preserved by goals.init_goals."""
    is_aisleprompt = profile["host"] == "aisleprompt.com"
    site_label = "AislePrompt" if is_aisleprompt else "SpecPicks"

    goals = [
        # Conversion goals (the ultimate KPIs)
        {
            "id": "goal-total-conversions-30d",
            "title": f"30-day total conversion clicks ({site_label})",
            "description": (
                "Total monetization-event clicks in the last 30 days. "
                + ("Sum of instacart-cart + instacart-clicks + amazon-clicks GA4 events." if is_aisleprompt
                   else "Sum of amazon-clicks + ebay-clicks GA4 events.")
            ),
            "metric": {"name": "conversions_30d", "current": 0, "target": 1000 if is_aisleprompt else 500,
                       "direction": "increase", "unit": "events", "horizon_weeks": 12},
            "status": "active",
            "is_revenue_goal": True,
        },
        # Per-event conversion goals
        *([
            {"id": "goal-instacart-cart-30d",
             "title": "30-day Instacart cart creates",
             "description": "GA4 'instacart-cart' event count last 30 days. The money event for AislePrompt.",
             "metric": {"name": "instacart_cart_creates", "current": 0, "target": 200,
                        "direction": "increase", "unit": "events", "horizon_weeks": 12},
             "status": "active", "is_revenue_goal": True},
            {"id": "goal-instacart-clicks-30d",
             "title": "30-day Instacart button clicks",
             "description": "GA4 'instacart-clicks' event count last 30 days. Outbound-interest leading indicator.",
             "metric": {"name": "instacart_clicks", "current": 0, "target": 800,
                        "direction": "increase", "unit": "events", "horizon_weeks": 12},
             "status": "active"},
            {"id": "goal-amazon-clicks-30d",
             "title": "30-day Amazon affiliate clicks (AislePrompt kitchen)",
             "description": "GA4 'amazon-clicks' event count last 30 days. Cross-site affiliate revenue from /kitchen.",
             "metric": {"name": "amazon_clicks", "current": 0, "target": 200,
                        "direction": "increase", "unit": "events", "horizon_weeks": 12},
             "status": "active"},
        ] if is_aisleprompt else [
            {"id": "goal-amazon-clicks-30d",
             "title": "30-day Amazon affiliate clicks",
             "description": "GA4 'amazon-clicks' event count last 30 days. Primary revenue source for SpecPicks.",
             "metric": {"name": "amazon_clicks", "current": 0, "target": 1000,
                        "direction": "increase", "unit": "events", "horizon_weeks": 12},
             "status": "active", "is_revenue_goal": True},
            {"id": "goal-ebay-clicks-30d",
             "title": "30-day eBay affiliate clicks",
             "description": "GA4 'ebay-clicks' event count last 30 days. Retro-marketplace revenue for SpecPicks.",
             "metric": {"name": "ebay_clicks", "current": 0, "target": 200,
                        "direction": "increase", "unit": "events", "horizon_weeks": 16},
             "status": "active", "is_revenue_goal": True},
        ]),
        # Leading-indicator goals
        {
            "id": "goal-organic-clicks-30d",
            "title": f"30-day organic clicks ({site_label})",
            "description": "GSC organic search clicks in the last 30 days. Drives all downstream conversions.",
            "metric": {"name": "organic_clicks_30d", "current": 0,
                       "target": 5000 if is_aisleprompt else 3000,
                       "direction": "increase", "unit": "clicks", "horizon_weeks": 16},
            "status": "active",
        },
        {
            "id": "goal-organic-impressions-30d",
            "title": f"30-day organic impressions ({site_label})",
            "description": "GSC search impressions in the last 30 days. Index of overall search visibility.",
            "metric": {"name": "organic_impressions_30d", "current": 0,
                       "target": 100000 if is_aisleprompt else 50000,
                       "direction": "increase", "unit": "impressions", "horizon_weeks": 16},
            "status": "active",
        },
        # Indexing health goals
        {
            "id": "goal-indexed-pages-pct",
            "title": "% of inspected URLs indexed by Google",
            "description": "Of pages we've called URL Inspection on, what fraction are 'Submitted and indexed'. Major lever for organic traffic — currently very low, needs to climb to 60%+ to support traffic goals.",
            "metric": {"name": "indexed_pct", "current": 0, "target": 60,
                       "direction": "increase", "unit": "%", "horizon_weeks": 24},
            "status": "active",
        },
        {
            "id": "goal-unknown-to-google-count",
            "title": "Pages unknown to Google",
            "description": "URLs that Google has no record of at all. Should drop to near-zero as sitemap submission + IndexNow drive discovery.",
            "metric": {"name": "unknown_count", "current": 0, "target": 50,
                       "direction": "decrease", "unit": "pages", "horizon_weeks": 12},
            "status": "active",
        },
        {
            "id": "goal-crawled-not-indexed-count",
            "title": "Pages crawled but not indexed",
            "description": "URLs Google fetched but didn't index. Almost always content-quality blockers — routes to article-author for rewrite.",
            "metric": {"name": "crawled_not_indexed_count", "current": 0, "target": 0,
                       "direction": "decrease", "unit": "pages", "horizon_weeks": 16},
            "status": "active",
        },
        {
            "id": "goal-active-pages-count",
            "title": f"Active publishable pages ({site_label})",
            "description": (
                "Total active rows in main content tables (recipes for AislePrompt; products + articles + hardware for SpecPicks). Index of how much content is in the funnel."
                if is_aisleprompt else
                "Total active rows in products + editorial_articles + hardware_specs. Index of catalog and content depth."
            ),
            "metric": {"name": "active_pages", "current": 0,
                       "target": 60000 if is_aisleprompt else 25000,
                       "direction": "increase", "unit": "rows", "horizon_weeks": 24},
            "status": "active",
        },
    ]
    for g in goals:
        g.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        g.setdefault("progress_history", [])

    # init_goals merges with existing (preserves history)
    goals_mod.init_goals(agent_id, goals)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--site", default=os.environ.get("SITE_GOALS_SITE", ""))
    p.add_argument("--run-ts", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    p.add_argument("--no-write", action="store_true", help="just print the metrics, don't store")
    args = p.parse_args()

    site_name = args.site
    if not site_name or site_name not in SITE_PROFILES:
        raise SystemExit(f"--site must be one of: {list(SITE_PROFILES)}")
    profile = SITE_PROFILES[site_name]
    agent_id = profile["agent_id"]
    err(f"[site-goals-tracker] site={site_name} agent_id={agent_id}")

    # 1. Ensure the goal schema is in place
    try:
        write_goal_definitions(profile, agent_id)
        err(f"  ✓ goal definitions written / refreshed")
    except Exception as e:
        err(f"  ✗ goal definition write failed: {e}")

    # 2. Collect fresh metrics
    metrics = collect_metrics(profile)
    err(f"  ✓ collected {len(metrics)} metrics:")
    for k, v in sorted(metrics.items()):
        err(f"      {k:<45} = {v}")

    if args.no_write:
        return

    # 3. Record all metrics in one pass via metric_helper
    if metrics:
        metric_helper.record_many(agent_id, metrics, run_ts=args.run_ts,
                                   note="auto-collected via site-goals-tracker")
        err(f"  ✓ recorded to {agent_id}")


# --- helpers ---

def _days_ago(d, n: int):
    from datetime import timedelta
    return d - timedelta(days=n)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (s or "").lower()).strip("-")


def _short_site(profile: dict) -> str:
    return "aisleprompt" if profile["host"] == "aisleprompt.com" else "specpicks"


def _db_fallback(profile: dict) -> str:
    # Fallback to known-good DSN for these two sites — same as in sites.json
    if profile["host"] == "aisleprompt.com":
        return "postgresql://nscadmin:NscP0stgr3s!2026@nscappsdb.postgres.database.azure.com:5432/aisleprompt?sslmode=require"
    return "postgresql://nscadmin:NscP0stgr3s!2026@nscappsdb.postgres.database.azure.com:5432/specpicks?sslmode=require"


if __name__ == "__main__":
    main()
