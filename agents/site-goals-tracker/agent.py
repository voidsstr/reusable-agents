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
  Outbound-click goals count VERIFIED HUMAN clicks only (profile
  `human_clicks` → framework/core/human_clicks.py); the raw first-party row
  count is kept as the raw-<event>-30d diagnostic metric.

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

from framework.core import metric_helper, goals as goals_mod, human_clicks


# 2026-05-11: the legacy `seo-data-collector` agent was retired in
# favor of `seo-opportunity-agent` (CLAUDE.md pipeline collapse). The
# refresh-token.py moved to the new location. Use the canonical path.
REFRESH_SCRIPT = (
    REPO / "agents" / "seo-opportunity-agent"
         / "lib" / "collector" / "refresh-token.py"
).resolve()
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
        # First-party fallback, authoritative for outbound clicks. See
        # CONVERSION_SQL_NOTE below.
        "conversion_sql": {
            "amazon-clicks": ("SELECT COUNT(*) FROM kitchen_click_events "
                              "WHERE source = 'amazon' "
                              "AND created_at > now() - interval '30 days'"),
            "instacart-clicks": ("SELECT COUNT(*) FROM kitchen_click_events "
                                 "WHERE source = 'instacart' "
                                 "AND created_at > now() - interval '30 days'"),
        },
        # Verified-human filter (framework/core/human_clicks.py). The goal
        # value for these events is the HUMAN count; conversion_sql above is
        # kept only as the raw-* diagnostic. See HUMAN_CLICKS_NOTE.
        "human_clicks": {
            "table": "kitchen_click_events",
            "spec": {"time_col": "created_at", "referer_col": "referer",
                     "bot_flag_col": "is_bot"},
            "events": {"amazon-clicks": {"source": "amazon"},
                       "instacart-clicks": {"source": "instacart"}},
        },
        "page_count_sql": "SELECT COUNT(*) FROM recipe_catalog WHERE COALESCE(is_active, TRUE) = TRUE",
        "agent_id": "aisleprompt-site-goals-tracker",
    },
    "specpicks": {
        "host": "specpicks.com",
        "gsc_site_url": "sc-domain:specpicks.com",
        "ga4_property_id": "531274480",
        "db_env": "SPECPICKS_DATABASE_URL",
        "conversion_events": ["amazon-clicks", "ebay-clicks"],
        # The site's delegated listener (frontend/src/utils/affiliateClickTracking.ts)
        # sends `amazon_click` / `ebay_click`. The goal names use the hyphenated
        # form, so the GA4 lookup never matched either one and the GA4 side of
        # these goals has always been 0.
        "ga4_event_aliases": {
            "amazon-clicks": ["amazon_click"],
            "ebay-clicks": ["ebay_click"],
        },
        "conversion_sql": {
            "amazon-clicks": ("SELECT COUNT(*) FROM outbound_clicks "
                              "WHERE target = 'amazon' "
                              "AND clicked_at > now() - interval '30 days'"),
            "ebay-clicks": ("SELECT COUNT(*) FROM outbound_clicks "
                            "WHERE target = 'ebay' "
                            "AND clicked_at > now() - interval '30 days'"),
        },
        "human_clicks": {
            "table": "outbound_clicks",
            "spec": {"time_col": "clicked_at", "referer_col": "source_page",
                     "country_col": "country", "bot_flag_col": "is_bot"},
            "events": {"amazon-clicks": {"target": "amazon"},
                       "ebay-clicks": {"target": "ebay"}},
        },
        "page_count_sql": (
            "SELECT (SELECT COUNT(*) FROM products WHERE is_active = true) + "
            "(SELECT COUNT(*) FROM editorial_articles WHERE status = 'published') + "
            "(SELECT COUNT(*) FROM hardware_specs)"
        ),
        "agent_id": "specpicks-site-goals-tracker",
    },
}


# GA4 sessionSource substrings for engines that serve Bing's index
# ("bing", "cn.bing.com", "ca.search.yahoo.com", …) or retrieve from it.
BING_INDEX_SOURCES = ("bing", "duckduckgo", "ecosia", "yahoo", "chatgpt.com", "copilot")


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

    # Fetch the access token ONCE up front. If this fails, both GSC and
    # GA4 calls below are short-circuited gracefully. Previously the
    # token was minted inside the GSC try-block, which meant a
    # refresh-token failure left `token` unbound for the GA4 block →
    # UnboundLocalError reported as `cannot access local variable
    # 'token' where it is not associated with a value`.
    token = None
    try:
        token = get_access_token()
    except Exception as e:
        err(f"  access-token mint failed (GSC + GA4 will be skipped): {e}")

    # --- GSC: organic clicks + impressions (30d) ---
    if token:
        try:
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

    # --- GA4: AI Assistant channel sessions (30d) ---
    #
    # rec growth-20260916T001200Z-03. AI Assistant is GA4's channel group for
    # referrals from ChatGPT / Perplexity / Gemini and friends, and on
    # aisleprompt it is the highest-intent human channel on the site: measured
    # 2026-09-16, 33 sessions with 32 of them engaged, against Organic Search's
    # 37/30. It was not tracked here at all, so the one channel worth building
    # on had no daily series and no goal.
    #
    # Read straight off sessionDefaultChannelGroup rather than any raw
    # activeUsers total. Direct carries 32,562 sessions against ~70 real human
    # ones — that is the Tencent-Cloud bot fleet, and any goal keyed off an
    # unfiltered GA4 user count is tracking bots, not traction. Filtering to a
    # named channel sidesteps that entirely.
    if token:
        try:
            ch_resp = ga4_run_report(token, profile["ga4_property_id"], {
                "dateRanges": [{"startDate": start_30d, "endDate": end_today}],
                "dimensions": [{"name": "sessionDefaultChannelGroup"}],
                "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}],
                "dimensionFilter": {"filter": {
                    "fieldName": "sessionDefaultChannelGroup",
                    "stringFilter": {"matchType": "EXACT", "value": "AI Assistant"},
                }},
            })
            ch_rows = ch_resp.get("rows") or []
            # No rows means the channel genuinely had no sessions in the
            # window, which is a real zero and should be recorded as one.
            metrics["goal-ai-assistant-sessions-30d"] = float(
                ch_rows[0]["metricValues"][0]["value"]) if ch_rows else 0.0
            metrics["ga4-ai-assistant-engaged-sessions-30d"] = float(
                ch_rows[0]["metricValues"][1]["value"]) if ch_rows else 0.0
        except Exception as e:
            err(f"  GA4 AI Assistant channel failed: {e}")

    # --- GA4: search + AI sessions split by index (30d) ---
    #
    # rec growth-20260924T141200Z-01. GSC only sees Google, and on aisleprompt
    # Google sent 2 organic sessions in the 30 days to 2026-09-24 while bing,
    # duckduckgo, ecosia, yahoo (all Bing-backed) and chatgpt.com (retrieves
    # from Bing) sent ~137. goal-organic-clicks-30d therefore measures the
    # channel that isn't delivering. This records the one that is.
    if token:
        try:
            src_resp = ga4_run_report(token, profile["ga4_property_id"], {
                "dateRanges": [{"startDate": start_30d, "endDate": end_today}],
                "dimensions": [{"name": "sessionSource"}],
                "metrics": [{"name": "sessions"}],
                "dimensionFilter": {"filter": {
                    "fieldName": "sessionDefaultChannelGroup",
                    "inListFilter": {"values": ["Organic Search", "AI Assistant"]},
                }},
                "limit": 100,
            })
            bing_n = google_n = 0
            for row in src_resp.get("rows") or []:
                src = row["dimensionValues"][0]["value"].lower()
                n = int(row["metricValues"][0]["value"])
                if "google" in src:
                    google_n += n
                elif any(k in src for k in BING_INDEX_SOURCES):
                    bing_n += n
            metrics["goal-bing-index-sessions-30d"] = float(bing_n)
            metrics["ga4-google-search-ai-sessions-30d"] = float(google_n)
        except Exception as e:
            err(f"  GA4 search-source split failed: {e}")

    # --- Conversions (30d): GA4, corrected by first-party click tables ---
    #
    # CONVERSION_SQL_NOTE. GA4 alone reports these as ZERO, and that zero is
    # wrong. Outbound buy-links are server-side 302 redirects (/k/:slug and
    # friends), so no browser JS ever runs on that hop and no GA4 event
    # fires. Measured 2026-08-14: GA4 said 0 amazon-clicks on both sites
    # while the first-party tables held 1,260 aisleprompt clicks in 30d and
    # 98,132 specpicks outbound clicks all-time (81,837 amazon / 16,295
    # ebay).
    #
    # That false zero is worse than a missing metric: every monetisation
    # goal read 0/target, so the whole affiliate side of the North Star
    # looked dead and any optimisation keyed off it was reasoning from
    # fiction.
    #
    # The redirect handler writes the click row itself, so the DB is the
    # AUTHORITATIVE source here; GA4 can only undercount. Take the larger of
    # the two per event, and keep both under distinct metric keys so the gap
    # stays visible rather than silently papered over.
    # The human-click spec (per-site columns + storage overrides) is resolved
    # once: the first-party filter below uses all of it, and the GA4 event
    # count uses its `exclude_countries` so both sides of max(GA4, DB) drop
    # the same traffic (Singapore headless fleet by default). GA4's countryId
    # dimension is ISO-3166 alpha-2, the same codes the spec carries.
    hc_cfg = profile.get("human_clicks") or {}
    hc_spec = (human_clicks.resolve_spec(hc_cfg.get("spec") or {},
                                         profile=profile.get("agent_id", ""))
               if hc_cfg else None)
    ga_excluded = sorted({str(c).upper() for c in
                          ((hc_spec or {}).get("exclude_countries") or []) if c})

    events: dict[str, int] = {}
    if token:
        try:
            ga_body = {
                "dateRanges": [{"startDate": start_30d, "endDate": end_today}],
                "dimensions": [{"name": "eventName"}],
                "metrics": [{"name": "eventCount"}],
            }
            if ga_excluded:
                ga_body["dimensionFilter"] = {"notExpression": {"filter": {
                    "fieldName": "countryId",
                    "inListFilter": {"values": ga_excluded},
                }}}
            ga_resp = ga4_run_report(token, profile["ga4_property_id"], ga_body)
            events = {row["dimensionValues"][0]["value"]: int(row["metricValues"][0]["value"])
                      for row in (ga_resp.get("rows") or [])}
        except Exception as e:
            err(f"  GA4 conversions failed: {e}")

    # HUMAN_CLICKS_NOTE (2026-09-24). The first-party click tables log every
    # hit on the redirect endpoint, crawlers included. Counting raw rows
    # reported 21,258 SpecPicks "Amazon clicks" in 30d when the verified
    # human number was 0 — 99.3% carried the site's own is_bot verdict and
    # the rest were a stale-UA fleet, Alibaba/Tencent cloud IPs (incl. the
    # Singapore headless fleet) and one Sogou spider. Goals now count ONLY
    # rows that pass framework/core/human_clicks.py; the raw count survives
    # as the `raw-<event>-30d` diagnostic so the bot share stays visible.
    #
    # If the human query fails we record NOTHING for that event rather than
    # fall back to the raw count: a missing day is honest, a bot number
    # dressed as a goal value is what this replaced.
    first_party: dict[str, int] = {}
    raw_first_party: dict[str, int] = {}
    human_failed: set[str] = set()
    conv_sql = profile.get("conversion_sql") or {}
    hc_events = hc_cfg.get("events") or {}
    if conv_sql or hc_events:
        _db_url = os.environ.get(profile["db_env"]) or _db_fallback(profile)
        if _db_url:
            try:
                _conn = psycopg2.connect(_db_url)
                try:
                    for ev, sql in conv_sql.items():
                        try:
                            with _conn.cursor() as _cur:
                                _cur.execute(sql)
                                raw_first_party[ev] = int(_cur.fetchone()[0])
                        except Exception as e:
                            _conn.rollback()
                            err(f"  raw first-party query for {ev} failed: {e}")
                    if hc_events:
                        for ev, match in hc_events.items():
                            try:
                                bd = human_clicks.breakdown(
                                    _conn, hc_spec, table=hc_cfg["table"],
                                    window_days=30, match=match)
                                first_party[ev] = int(bd[human_clicks.VERDICT_HUMAN])
                                err(f"  {ev}: {first_party[ev]} verified human of "
                                    f"{bd['_total']} rows — "
                                    + ", ".join(f"{k}={v}" for k, v in bd.items()
                                                if k not in ("_total",)))
                            except Exception as e:
                                _conn.rollback()
                                human_failed.add(ev)
                                err(f"  human-click query for {ev} failed "
                                    f"(goal not recorded this run): {e}")
                    # Events with no human spec keep the legacy raw count.
                    for ev, n in raw_first_party.items():
                        if ev not in hc_events:
                            first_party[ev] = n
                finally:
                    _conn.close()
            except Exception as e:
                err(f"  first-party conversion query failed: {e}")
                human_failed.update(hc_events)

    total_conv = 0
    for ev in profile.get("conversion_events", []):
        if ev in human_failed:
            continue
        ga_n = int(events.get(ev, 0)) + sum(
            int(events.get(alias, 0))
            for alias in (profile.get("ga4_event_aliases") or {}).get(ev, []))
        fp_n = int(first_party.get(ev, 0))
        n = max(ga_n, fp_n)
        metrics[f"goal-{_slug(ev)}-30d"] = float(n)
        if ev in first_party:
            metrics[f"ga4-{_slug(ev)}-30d"] = float(ga_n)
            metrics[f"firstparty-{_slug(ev)}-30d"] = float(fp_n)
            if fp_n > ga_n:
                err(f"  {ev}: first-party DB {fp_n} > GA4 {ga_n} "
                    f"(server-side redirect not seen by GA4) — using DB")
        if ev in raw_first_party and ev in hc_events:
            metrics[f"raw-{_slug(ev)}-30d"] = float(raw_first_party[ev])
        total_conv += n
    if not human_failed:
        metrics["goal-total-conversions-30d"] = float(total_conv)

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
            "title": f"30-day total conversion clicks — verified human ({site_label})",
            "description": (
                "Total monetization-event clicks in the last 30 days, bots excluded. "
                + ("Sum of GA4 instacart-cart + verified human instacart-clicks + amazon-clicks." if is_aisleprompt
                   else "Sum of verified human amazon-clicks + ebay-clicks.")
                + " Per event: max(GA4 event count, first-party click rows that pass "
                  "framework/core/human_clicks.py — no site bot verdict, no headless/"
                  "automation UA, no stale-browser fingerprint, no datacenter IP, no "
                  "Singapore headless traffic, on-site referer, <=20 clicks/IP/day)."
            ),
            "metric": {"name": "human_conversions_30d", "current": 0,
                       "target": 1000 if is_aisleprompt else 1100,
                       "direction": "increase", "unit": "human clicks", "horizon_weeks": 12},
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
             "title": "30-day Instacart button clicks — verified human",
             "description": "Verified human Instacart button clicks, last 30 days: kitchen_click_events rows "
                            "that pass framework/core/human_clicks.py (bots, headless UAs, stale-browser "
                            "fingerprints, datacenter IPs excluded), or the GA4 event count if higher. "
                            "Outbound-interest leading indicator. Raw row count is the raw-instacart-clicks-30d metric.",
             "metric": {"name": "human_instacart_clicks_30d", "current": 0, "target": 800,
                        "direction": "increase", "unit": "human clicks", "horizon_weeks": 12},
             "status": "active"},
            {"id": "goal-amazon-clicks-30d",
             "title": "30-day Amazon affiliate clicks — verified human (AislePrompt kitchen)",
             "description": "Verified human Amazon affiliate clicks, last 30 days: kitchen_click_events rows "
                            "that pass framework/core/human_clicks.py (bots, headless UAs, stale-browser "
                            "fingerprints, datacenter IPs excluded), or the GA4 event count if higher. "
                            "Cross-site affiliate revenue from /kitchen. Raw row count is the raw-amazon-clicks-30d metric.",
             "metric": {"name": "human_amazon_clicks_30d", "current": 0, "target": 200,
                        "direction": "increase", "unit": "human clicks", "horizon_weeks": 12},
             "status": "active"},
        ] if is_aisleprompt else [
            # Target 900/30d = 30 verified human clicks/day (reset 2026-09-24
            # from 1000 raw events). The raw count it replaced was 21,258 in
            # 30d with 0 verified humans — see HUMAN_CLICKS_NOTE.
            {"id": "goal-amazon-clicks-30d",
             "title": "30-day Amazon affiliate clicks — verified human (target 30/day)",
             "description": "Verified human Amazon affiliate clicks, last 30 days. Counts outbound_clicks "
                            "rows that pass framework/core/human_clicks.py: not flagged is_bot, no headless/"
                            "automation UA (HeadlessChrome, puppeteer, playwright, selenium, curl, python…), "
                            "no stale-browser fingerprint, no datacenter IP (incl. Tencent/Alibaba/Huawei "
                            "Singapore ranges), no Singapore traffic, an on-site referer, and <=20 clicks per "
                            "IP per day — or the GA4 amazon_click count if higher. Primary revenue source for "
                            "SpecPicks. The raw row count (~99% bots) is the raw-amazon-clicks-30d metric.",
             "metric": {"name": "human_amazon_clicks_30d", "current": 0, "target": 900,
                        "direction": "increase", "unit": "human clicks", "horizon_weeks": 26},
             "status": "active", "is_revenue_goal": True},
            {"id": "goal-ebay-clicks-30d",
             "title": "30-day eBay affiliate clicks — verified human",
             "description": "Verified human eBay affiliate clicks, last 30 days (same human_clicks.py filter "
                            "as the Amazon goal), or the GA4 ebay_click count if higher. Retro-marketplace "
                            "revenue for SpecPicks. Raw row count is the raw-ebay-clicks-30d metric.",
             "metric": {"name": "human_ebay_clicks_30d", "current": 0, "target": 200,
                        "direction": "increase", "unit": "human clicks", "horizon_weeks": 16},
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
            "id": "goal-ai-assistant-sessions-30d",
            "title": f"30-day AI Assistant sessions ({site_label})",
            "description": "GA4 sessions whose channel group is 'AI Assistant' — "
                           "ChatGPT / Perplexity / Gemini referrals. Highest engagement "
                           "rate of any human channel on the site; the GEO/AI-search "
                           "thesis in docs/seo-growth-strategy.md is measured here.",
            "metric": {"name": "ai_assistant_sessions_30d", "current": 0,
                       "target": 50 if is_aisleprompt else 100,
                       "direction": "increase", "unit": "sessions", "horizon_weeks": 8},
            "status": "active",
        },
        {
            "id": "goal-bing-index-sessions-30d",
            "title": f"30-day search + AI sessions via Bing's index ({site_label})",
            "description": "GA4 Organic Search + AI Assistant sessions whose source is "
                           "bing / duckduckgo / ecosia / yahoo / chatgpt.com / copilot — "
                           "every engine that serves or retrieves from Bing's index. GSC "
                           "cannot see any of these.",
            "metric": {"name": "ga4_sessions_bing_plus_chatgpt", "current": 0,
                       "target": 400 if is_aisleprompt else 300,
                       "direction": "increase", "unit": "sessions", "horizon_weeks": 8},
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
    """Resolve the site DB DSN tolerantly across naming conventions.

    Primary is profile['db_env'] (e.g. SPECPICKS_DATABASE_URL), checked by
    the caller. This fallback also accepts the reversed convention
    DATABASE_URL_<SITE> (as set in ~/.reusable-agents/secrets.env) and a
    generic DATABASE_URL, so a rename of one form doesn't silently drop the
    DB-derived active-pages-count metric (regression seen 2026-05-07: env was
    DATABASE_URL_SPECPICKS but the tracker only looked for SPECPICKS_DATABASE_URL).
    """
    primary = profile.get("db_env", "")
    site = primary.replace("_DATABASE_URL", "").replace("DATABASE_URL_", "").strip("_")
    candidates = ([f"DATABASE_URL_{site}"] if site else []) + ["DATABASE_URL"]
    for name in candidates:
        v = os.environ.get(name)
        if v:
            return v
    return ""


if __name__ == "__main__":
    main()
