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

from framework.core import metric_helper, goals as goals_mod, human_clicks, gsc_coverage


# 2026-05-11: the legacy `seo-data-collector` agent was retired in
# favor of `seo-opportunity-agent` (CLAUDE.md pipeline collapse). The
# refresh-token.py moved to the new location. Use the canonical path.
REFRESH_SCRIPT = (
    REPO / "agents" / "seo-opportunity-agent"
         / "lib" / "collector" / "refresh-token.py"
).resolve()
OAUTH_FILE = Path(os.path.expanduser("~/.reusable-agents/seo/.oauth.json"))


# The dated Safari-17 measurement note. The per-site profiles in
# config/site-goals-tracker-config.json pin it to the click goals, and
# install/seed-default-goals.sh carries the same (ts, note);
# goals.init_goals merges annotations by (ts, note), so all three must agree
# (framework/tests/test_goals_rearm.py, test_site_goals_tracker_profiles.py).
SAFARI_FLEET_NOTE = (
    "measurement correction: Safari-17 fleet removed — a Safari 17.0-17.2 bot fleet "
    "(one click per IP, 0% US) was 348 of 681 30-day and 326 of 378 7-day verified-human "
    "Amazon clicks; filtered from 2026-11-11. The step down to ~330 (30d) / ~50 (7d) "
    "is not a regression.")


# --- per-site profiles (config, not code: SHOP-22) ---
#
# Each site's profile (GSC/GA4 ids, DB env, conversion events, human-click
# filter, goal_defaults, event_goals, goal_overrides) lives in
# config/site-goals-tracker-config.json under by_site.<site>. Layers, lowest
# first: the repo file, then storage config/site-goals-tracker-config.json,
# then the JSON file named by SITE_GOALS_PROFILE_FILE (by_site or a bare
# profile for --site). Dicts deep-merge; lists replace. A new site is a new
# by_site entry, never a code branch. goal_overrides keep their own extra
# layer: the site.yaml `site_goals.goal_overrides` block (goal_overrides()).

PROFILES_CONFIG_KEY = "config/site-goals-tracker-config.json"
_REPO_PROFILES = REPO / "config" / "site-goals-tracker-config.json"


def _deep_merge(base: dict, over) -> dict:
    out = json.loads(json.dumps(base))
    if isinstance(over, dict):
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
    return out


def load_profiles(*, storage=None, read_storage: bool = False,
                  repo_file: Optional[Path] = None) -> dict[str, dict]:
    """{site: profile}. Every profile carries `site` (its by_site key) and
    `label` (display name, default the site key)."""
    try:
        doc = json.loads((repo_file or _REPO_PROFILES).read_text())
    except Exception as e:
        print(f"  site profiles: repo config unreadable: {e}", file=sys.stderr)
        doc = {}
    by_site = dict((doc or {}).get("by_site") or {})
    if read_storage:
        try:
            from framework.core.storage import get_storage
            over = (storage or get_storage()).read_json(PROFILES_CONFIG_KEY) or {}
            for site, prof in (over.get("by_site") or {}).items():
                by_site[site] = _deep_merge(by_site.get(site) or {}, prof)
        except Exception as e:
            print(f"  site profiles: storage override not applied: {e}", file=sys.stderr)
    extra = os.environ.get("SITE_GOALS_PROFILE_FILE")
    if extra:
        try:
            over = json.loads(Path(os.path.expanduser(extra)).read_text())
            for site, prof in (over.get("by_site") or {}).items():
                by_site[site] = _deep_merge(by_site.get(site) or {}, prof)
        except Exception as e:
            print(f"  site profiles: {extra} not applied: {e}", file=sys.stderr)
    out = {}
    for site, prof in by_site.items():
        if isinstance(prof, dict):
            out[site] = {**prof, "site": prof.get("site") or site,
                         "label": prof.get("label") or site}
    return out


# The repo profiles, for callers and tests that want them without a storage
# read; main() re-reads with the storage layer.
SITE_PROFILES: dict[str, dict] = load_profiles()


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

    # --- Bing Webmaster Tools: Bing's own view of the site ---
    #
    # rec growth-20260928T191200Z-02. The GA4 split above says Bing's index
    # sends the humans; this records what Bing itself reports (pages held,
    # 28d clicks/impressions, crawl errors) so those can be read as a daily
    # series. The property URL comes from the site's own config
    # (site.yaml `data_sources.bing.site_url`, see bing_site_url()); skipped
    # quietly while BING_WEBMASTER_API_KEY is unset.
    #
    # 2026-10-01 audit (bing-webmaster-not-configured): bingbot crawled
    # ~3,000 specpicks URLs a day yet fetched none of 11 new articles, and
    # nothing could say why. With the key set, GetUrlInfo is now asked about
    # every article published in the last 14 days (bing_recent_url_info()).
    site_yaml = load_site_yaml(profile)
    bing_url = bing_site_url(profile, site_yaml)
    if bing_url:
        from framework.core import bing_webmaster
        if bing_webmaster.api_key():
            try:
                bwt = bing_webmaster.collect(bing_url)
                for k in ("in_index", "clicks_28d", "impressions_28d", "crawl_errors_1d"):
                    if k in bwt.get("metrics", {}):
                        metrics[f"bing-{k.replace('_', '-')}"] = float(bwt["metrics"][k])
                if bwt.get("errors"):
                    err(f"  Bing Webmaster partial failure: {bwt['errors']}")
            except Exception as e:
                err(f"  Bing Webmaster failed: {e}")
            try:
                metrics.update(bing_recent_url_info(profile, site_yaml, bing_url))
            except Exception as e:
                err(f"  Bing GetUrlInfo for recent articles failed: {e}")
        else:
            err("  Bing Webmaster skipped: BING_WEBMASTER_API_KEY not set")
    else:
        err("  Bing Webmaster skipped: no data_sources.bing.site_url in the site's site.yaml")

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
                        for ev in hc_cfg.get("events_7d") or []:
                            try:
                                bd = human_clicks.breakdown(
                                    _conn, hc_spec, table=hc_cfg["table"],
                                    window_days=7, match=hc_events[ev])
                                metrics[f"goal-{_slug(ev)}-7d"] = float(
                                    bd[human_clicks.VERDICT_HUMAN])
                            except Exception as e:
                                _conn.rollback()
                                err(f"  7-day human-click query for {ev} failed "
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
            # Both of Google's indexed states (framework/core/gsc_coverage.py).
            indexed = sum(n for cs, n in states.items() if cs in gsc_coverage.INDEXED_STATES)
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


# --- per-goal overrides (targets, re-arm rules, annotations) ---
#
# The goal definitions below carry default targets. A site's own targets and
# re-arm rules come from data: SITE_PROFILES[site]["goal_overrides"], then the
# site.yaml block `site_goals.goal_overrides` (shared/schemas/
# site-config.schema.json). Click goals keep steering prompts after they are
# hit (framework/core/goals.py): a ratchet raises the target past the value
# instead of accomplishing it, and reopen puts an accomplished goal back to
# active when the value falls back. The tracker re-seeds every run; a
# ratcheted target survives those re-seeds (metric.seed_target).

def _override_annotations(raw, where: str) -> list[dict]:
    """Dated notes from an override: ts + note required. YAML reads an
    unquoted 2026-11-11 as a date, so ts is stored as its ISO string."""
    out: list[dict] = []
    for a in raw if isinstance(raw, list) else []:
        if not (isinstance(a, dict) and a.get("ts") and a.get("note")):
            err(f"  goal override {where}: annotation needs ts and note, ignored: {a!r}")
            continue
        ts = a["ts"].isoformat() if hasattr(a["ts"], "isoformat") else str(a["ts"])
        note = {"ts": ts, "note": str(a["note"])}
        if a.get("kind"):
            note["kind"] = str(a["kind"])
        if isinstance(a.get("value"), (int, float)) and not isinstance(a.get("value"), bool):
            note["value"] = a["value"]
        out.append(note)
    return out


def _clean_override(ov, where: str) -> dict:
    """Keep the override keys the goals schema accepts; warn on the rest, so
    a typo in a site.yaml never blocks the goal-definition refresh. null
    ratchet/reopen is kept: init_goals removes that rule."""
    if not isinstance(ov, dict):
        err(f"  goal override {where} ignored: not a mapping")
        return {}
    out: dict = {}
    for k, v in ov.items():
        if k == "target" and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
        elif k == "ratchet" and (v is None or goals_mod.ratchet_factor({"ratchet": v})):
            out[k] = None if v is None else {"factor": v["factor"]}
        elif k == "reopen" and (v is None or isinstance(v, bool)):
            out[k] = v
        elif k == "annotations":
            out[k] = _override_annotations(v, where)
        else:
            err(f"  goal override {where}.{k} ignored: {v!r}")
    return out


def goal_overrides(profile: dict, site_yaml: Optional[dict] = None) -> dict[str, dict]:
    """{goal_id: {target?, ratchet?, reopen?, annotations?}}: the profile's
    goal_overrides, then site.yaml `site_goals.goal_overrides` on top. Per key
    the site.yaml value wins; annotations from both layers are kept."""
    layers = [("profile", profile.get("goal_overrides"))]
    sg = (site_yaml or {}).get("site_goals")
    if isinstance(sg, dict):
        layers.append(("site.yaml", sg.get("goal_overrides")))
    out: dict[str, dict] = {}
    for src, layer in layers:
        if layer is None:
            continue
        if not isinstance(layer, dict):
            err(f"  {src} goal_overrides ignored: not a mapping")
            continue
        for gid, ov in layer.items():
            clean = _clean_override(ov, f"{src} {gid}")
            if not clean:
                continue
            merged = out.setdefault(str(gid), {})
            notes = merged.pop("annotations", []) + clean.pop("annotations", [])
            merged.update(clean)
            if notes:
                merged["annotations"] = notes
    return out


def _apply_goal_override(goal: dict, ov: dict) -> None:
    m = goal.setdefault("metric", {})
    for k in ("target", "ratchet", "reopen"):
        if k in ov:
            m[k] = dict(ov[k]) if isinstance(ov[k], dict) else ov[k]
    notes = list(goal.get("annotations") or [])
    seen = {(a.get("ts"), a.get("note")) for a in notes}
    for a in ov.get("annotations") or []:
        if (a["ts"], a["note"]) not in seen:
            notes.append(dict(a))
            seen.add((a["ts"], a["note"]))
    if notes:
        goal["annotations"] = notes


def write_goal_definitions(profile: dict, agent_id: str,
                           site_yaml: Optional[dict] = None) -> None:
    """Idempotent: write the goal schema for this site if not already present.
    Existing goals' progress_history is preserved by goals.init_goals.
    `site_yaml` defaults to load_site_yaml(profile); see goal_overrides()."""
    site_label = profile.get("label") or _short_site(profile)
    defaults = profile.get("goal_defaults") or {}

    def target(gid: str, fallback):
        return (defaults.get(gid) or {}).get("target", fallback)

    def text(gid: str, fallback: str) -> str:
        return (defaults.get(gid) or {}).get("description") or fallback

    goals = [
        # Conversion goals (the ultimate KPIs)
        {
            "id": "goal-total-conversions-30d",
            "title": f"30-day total conversion clicks — verified human ({site_label})",
            "description": text(
                "goal-total-conversions-30d",
                "Total monetization-event clicks in the last 30 days, bots excluded. "
                "Per event: max(GA4 event count, first-party click rows that pass "
                "framework/core/human_clicks.py)."),
            "metric": {"name": "human_conversions_30d", "current": 0,
                       "target": target("goal-total-conversions-30d", 1000),
                       "direction": "increase", "unit": "human clicks", "horizon_weeks": 12},
            "status": "active",
            "is_revenue_goal": True,
        },
        # Per-event conversion goals: the site's own (profile event_goals).
        *json.loads(json.dumps(profile.get("event_goals") or [])),
        # Leading-indicator goals
        {
            "id": "goal-organic-clicks-30d",
            "title": f"30-day organic clicks ({site_label})",
            "description": "GSC organic search clicks in the last 30 days. Drives all downstream conversions.",
            "metric": {"name": "organic_clicks_30d", "current": 0,
                       "target": target("goal-organic-clicks-30d", 3000),
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
                       "target": target("goal-ai-assistant-sessions-30d", 100),
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
                       "target": target("goal-bing-index-sessions-30d", 300),
                       "direction": "increase", "unit": "sessions", "horizon_weeks": 8},
            "status": "active",
        },
        {
            "id": "goal-organic-impressions-30d",
            "title": f"30-day organic impressions ({site_label})",
            "description": "GSC search impressions in the last 30 days. Index of overall search visibility.",
            "metric": {"name": "organic_impressions_30d", "current": 0,
                       "target": target("goal-organic-impressions-30d", 50000),
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
            "description": text("goal-active-pages-count",
                                "Total active rows in the site's main content tables "
                                "(profile page_count_sql). Index of how much content is in the funnel."),
            "metric": {"name": "active_pages", "current": 0,
                       "target": target("goal-active-pages-count", 25000),
                       "direction": "increase", "unit": "rows", "horizon_weeks": 24},
            "status": "active",
        },
    ]
    overrides = goal_overrides(profile, load_site_yaml(profile) if site_yaml is None else site_yaml)
    for gid in sorted(set(overrides) - {g["id"] for g in goals}):
        err(f"  goal override for {gid} ignored: the tracker defines no such goal")
    for g in goals:
        if g["id"] in overrides:
            _apply_goal_override(g, overrides[g["id"]])
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
    profiles = load_profiles(read_storage=True)
    if not site_name or site_name not in profiles:
        raise SystemExit(f"--site must be one of: {list(profiles)}")
    profile = profiles[site_name]
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

    # 2b. Did a recent deploy cost verified clicks on a money placement?
    run_placement_watch(site_name, profile, dry_run=args.no_write)

    if args.no_write:
        return

    # 3. Record all metrics in one pass via metric_helper
    if metrics:
        metric_helper.record_many(agent_id, metrics, run_ts=args.run_ts,
                                   note="auto-collected via site-goals-tracker")
        err(f"  ✓ recorded to {agent_id}")


def run_placement_watch(site_name: str, profile: dict, *, dry_run: bool = False) -> Optional[dict]:
    """Placement-level conversion watch (framework/core/placement_watch.py).

    Compares verified-human clicks per money placement in the 24-48 h after
    the newest judgeable deploy (the deployer's gate-state log) with the same
    hours on the 7 days before; a real drop opens one incident and sends one
    email to the site's reporter.email. The click table, placements and
    thresholds are VALUES in storage config/placement-watch-config.json: a
    site with no entry there is skipped. --no-write evaluates without
    recording or emailing. Never fails the tracker."""
    try:
        from framework.core import placement_watch
        cfg = placement_watch.site_config(site_name)
        if not cfg:
            err(f"  placement watch: no entry for this site in {placement_watch.CONFIG_KEY}; skipped")
            return None
        db_url = os.environ.get(profile["db_env"]) or _db_fallback(profile)
        if not db_url:
            err("  placement watch: no DB URL for this site; skipped")
            return None
        email = (load_site_yaml(profile).get("reporter") or {}).get("email") or {}
        # statement_timeout: a hung series query never raises, so without it
        # a slow DB would stall the whole daily tracker (the CLI sets the same).
        conn = psycopg2.connect(db_url, connect_timeout=15,
                                options="-c statement_timeout=120000")
        try:
            conn.set_session(readonly=True, autocommit=True)
            res = placement_watch.watch_site(site_name, conn=conn, cfg=cfg, email=email,
                                             profile=profile.get("agent_id", ""), dry_run=dry_run)
        finally:
            conn.close()
        for line in placement_watch.summary_lines(res):
            err(f"  placement watch: {line}")
        return res
    except Exception as e:
        err(f"  placement watch failed (non-fatal): {e}")
        return None


# --- per-site config (site.yaml) ---

def site_yaml_path(profile: dict) -> Optional[Path]:
    """The site's seo-opportunity-agent site.yaml: SITE_GOALS_SITE_YAML, else
    <repos root>/<site>/agents/seo-opportunity-agent/site.yaml (root from
    SITE_GOALS_REPOS_ROOT / INDEXNOW_SITE_REPOS_ROOT, default ~/development)."""
    explicit = os.environ.get("SITE_GOALS_SITE_YAML") or profile.get("site_yaml")
    if explicit:
        return Path(os.path.expanduser(explicit))
    root = (os.environ.get("SITE_GOALS_REPOS_ROOT")
            or os.environ.get("INDEXNOW_SITE_REPOS_ROOT")
            or os.path.expanduser("~/development"))
    return Path(root) / _short_site(profile) / "agents" / "seo-opportunity-agent" / "site.yaml"


def load_site_yaml(profile: dict) -> dict:
    """Parsed site.yaml, or {} when missing/unreadable (never raises)."""
    path = site_yaml_path(profile)
    if not path or not path.is_file():
        return {}
    try:
        import yaml
        doc = yaml.safe_load(path.read_text())
        return doc if isinstance(doc, dict) else {}
    except Exception as e:
        err(f"  site.yaml unreadable ({path}): {e}")
        return {}


def bing_site_url(profile: dict, site_yaml: dict) -> str:
    """Bing Webmaster property URL: SITE_GOALS_BING_SITE_URL, else site.yaml
    `data_sources.bing.site_url` (unless `enabled: false`), else a legacy
    profile `bing_site_url`. "" = not configured."""
    env = (os.environ.get("SITE_GOALS_BING_SITE_URL") or "").strip()
    if env:
        return env
    bing = ((site_yaml.get("data_sources") or {}).get("bing") or {})
    if isinstance(bing, dict) and bing.get("site_url") and bing.get("enabled", True) is not False:
        return str(bing["site_url"]).strip()
    return str(profile.get("bing_site_url") or "").strip()


def _real_date(v) -> bool:
    """GetUrlInfo reports "never" as /Date(-62135596800000)/ (year 1)."""
    from framework.core import bing_webmaster
    d = bing_webmaster.parse_date(v)
    return bool(d and d.year >= 2000)


def recent_article_urls(profile: dict, site_yaml: dict, *, days: int = 14,
                        limit: int = 30) -> list[str]:
    """Canonical URLs of articles published in the last `days` days, newest
    first, built from site.yaml `articles.url_template` ({slug}, {domain})."""
    arts = site_yaml.get("articles") or {}
    tmpl = arts.get("url_template") if isinstance(arts, dict) else None
    if not tmpl:
        return []
    db_url = os.environ.get(profile["db_env"]) or _db_fallback(profile)
    if not db_url:
        return []
    table = str(profile.get("articles_table") or "editorial_articles")
    if not table.replace("_", "").isalnum():
        raise ValueError(f"unsafe table name {table!r}")
    conn = psycopg2.connect(db_url, connect_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT slug FROM {table} WHERE status = 'published' AND slug IS NOT NULL "
                "   AND published_at >= NOW() - make_interval(days => %s) "
                "   AND published_at <= NOW() "
                " ORDER BY published_at DESC LIMIT %s",
                (int(days), int(limit)))
            slugs = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    domain = str(arts.get("domain") or profile.get("host") or "")
    return [tmpl.replace("{domain}", domain).replace("{slug}", str(s)) for s in slugs]


def bing_recent_url_info(profile: dict, site_yaml: dict, site_url: str) -> dict[str, float]:
    """Ask Bing (GetUrlInfo) about every article published in the last 14
    days. Returns bing-recent-articles-{checked,unknown,discovered,crawled,
    errors} and logs each URL's state. IndexNow already handles submission, so this
    only reads (no legacy URL Submission API)."""
    from framework.core import bing_webmaster
    days = int(os.environ.get("SITE_GOALS_BING_RECENT_DAYS", "14"))
    limit = int(os.environ.get("SITE_GOALS_BING_URL_INFO_MAX", "30"))
    urls = recent_article_urls(profile, site_yaml, days=days, limit=limit)
    out = {"bing-recent-articles-checked": 0.0, "bing-recent-articles-unknown": 0.0,
           "bing-recent-articles-discovered": 0.0, "bing-recent-articles-crawled": 0.0,
           "bing-recent-articles-errors": 0.0}
    for url in urls:
        info = bing_webmaster.url_info(site_url, url, timeout=20)
        if not isinstance(info, dict) or "error" in info:
            msg = str(info.get("error") if isinstance(info, dict) else info)
            if "notfound" in msg.lower().replace(" ", ""):
                # Bing has no record of the URL at all.
                out["bing-recent-articles-checked"] += 1
                out["bing-recent-articles-unknown"] += 1
                err(f"  bing url-info {url}: unknown to Bing")
                continue
            out["bing-recent-articles-errors"] += 1
            err(f"  bing url-info {url}: {msg[:200]}")
            if any(t in msg for t in ("HTTP 401", "HTTP 403", "HTTP 429",
                                      "InvalidApiKey", "ThrottleUser")):
                err("  bing url-info: auth/quota error, stopping")
                break
            continue
        out["bing-recent-articles-checked"] += 1
        discovered = _real_date(info.get("DiscoveryDate"))
        crawled = _real_date(info.get("LastCrawledDate"))
        out["bing-recent-articles-discovered"] += 1 if discovered else 0
        out["bing-recent-articles-crawled"] += 1 if crawled else 0
        err(f"  bing url-info {url}: http={info.get('HttpStatus')} "
            f"discovered={'yes' if discovered else 'no'} crawled={'yes' if crawled else 'no'}")
    return out


# --- helpers ---

def _days_ago(d, n: int):
    from datetime import timedelta
    return d - timedelta(days=n)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (s or "").lower()).strip("-")


def _short_site(profile: dict) -> str:
    """The site key (by_site entry); legacy profiles without one fall back to
    the host's first label (example.com -> example)."""
    return str(profile.get("site") or str(profile.get("host") or "").split(".")[0])


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
