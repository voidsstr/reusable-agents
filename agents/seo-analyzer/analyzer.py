#!/usr/bin/env python3
"""seo-analyzer — read collector's data dir, write recommendations.json + goals.

Deterministic. No LLM calls. Picks top-5 rank targets, indexing fixes,
CTR fixes, and conversion-path issues from the data files.

Usage:
  SEO_AGENT_CONFIG=my-site.yaml python3 analyzer.py
  SEO_AGENT_CONFIG=my-site.yaml python3 analyzer.py --run-ts 20260425T140245Z
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.site_config import load_config_from_env  # noqa: E402
from shared import run_files  # noqa: E402


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(p: Path) -> dict:
    if not p.is_file():
        return {}
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def build_snapshot(cfg, run_dir: Path) -> dict:
    data = run_dir / "data"
    queries_90 = _load(data / "gsc-queries-90d.json")
    pages_90 = _load(data / "gsc-pages-90d.json")
    devices = _load(data / "gsc-devices-90d.json")
    countries = _load(data / "gsc-countries-90d.json")
    summary_28 = _load(data / "ga4-summary-28d.json")
    events_28 = _load(data / "ga4-events-28d.json")
    geo_28 = _load(data / "ga4-geo-28d.json")
    sources_28 = _load(data / "ga4-traffic-sources-28d.json")
    db_stats = _load(data / "db-stats.json")

    rows = queries_90.get("rows", [])
    total_impr = sum(r.get("impressions", 0) for r in rows)
    total_clicks = sum(r.get("clicks", 0) for r in rows)
    avg_pos = sum(r["position"] for r in rows) / max(len(rows), 1) if rows else 0.0

    # Per-query positions (lowercased so goal lookups are case-insensitive)
    query_position = {}
    for r in rows:
        keys = r.get("keys") or []
        if not keys: continue
        q = str(keys[0]).strip().lower()
        if q:
            query_position[q] = {
                "position": round(r.get("position", 0.0), 2),
                "impressions": r.get("impressions", 0),
                "clicks": r.get("clicks", 0),
                "ctr": round(r.get("ctr", 0.0), 4),
            }
    # Per-page positions
    page_position = {}
    for r in pages_90.get("rows", []):
        keys = r.get("keys") or []
        if not keys: continue
        url = str(keys[0]).strip()
        if url:
            page_position[url] = {
                "position": round(r.get("position", 0.0), 2),
                "impressions": r.get("impressions", 0),
                "clicks": r.get("clicks", 0),
            }

    # Position buckets
    buckets = {"top3": 0, "top5": 0, "pos6_10": 0, "pos11_20": 0, "pos21_50": 0, "pos51plus": 0}
    for r in rows:
        p = r.get("position", 100.0)
        if p <= 3: buckets["top3"] += 1
        if p <= 5: buckets["top5"] += 1
        if 6 <= p <= 10: buckets["pos6_10"] += 1
        elif 11 <= p <= 20: buckets["pos11_20"] += 1
        elif 21 <= p <= 50: buckets["pos21_50"] += 1
        elif p > 50: buckets["pos51plus"] += 1

    snap: dict = {
        "schema_version": "1",
        "site": cfg.site_id,
        "captured_at": _now_iso(),
        "gsc_90d": {
            "total_impressions": total_impr,
            "total_clicks": total_clicks,
            "total_ctr": round(total_clicks / total_impr, 4) if total_impr else 0.0,
            "avg_position": round(avg_pos, 2),
            "num_queries": len(rows),
            "num_pages_indexed": len(pages_90.get("rows", [])),
            "by_device": {
                r["keys"][0].lower(): {
                    "impressions": r["impressions"],
                    "clicks": r["clicks"],
                    "ctr": round(r["ctr"], 4),
                    "position": round(r["position"], 2),
                } for r in devices.get("rows", [])
            },
            "by_country_top": [
                {"country": r["keys"][0], "impressions": r["impressions"], "clicks": r["clicks"]}
                for r in countries.get("rows", [])[:10]
            ],
            "query_position": query_position,
            "page_position": page_position,
            "rank_buckets": buckets,
        },
        "ga4_28d": {},
        "ga4_events_28d": {},
        "ga4_channels_28d": {},
        "revenue_28d": {},
    }

    if summary_28.get("rows"):
        r = summary_28["rows"][0]
        names = [h["name"] for h in summary_28.get("metricHeaders", [])]
        vals = r.get("metricValues", [])
        snap["ga4_28d"] = {n: vals[i]["value"] for i, n in enumerate(names) if i < len(vals)}

    for r in events_28.get("rows", []):
        name = r["dimensionValues"][0]["value"]
        count = int(r["metricValues"][0]["value"])
        users = int(r["metricValues"][1]["value"]) if len(r["metricValues"]) > 1 else 0
        snap["ga4_events_28d"][name] = {"count": count, "users": users}

    # Revenue rollup — read configured KPIs
    revenue: dict = {}
    for kpi in cfg.get("revenue_kpis", []):
        ev = kpi.get("ga4_event", "")
        if ev:
            revenue[f"{kpi['id']}_event_28d"] = snap["ga4_events_28d"].get(ev, {}).get("count", 0)
        tbl = kpi.get("db_table", "")
        if tbl and db_stats:
            # Convention: collector's DB script wrote {<table>_30d: {"last_7d", "last_30d"}}
            db_key = f"{tbl}_30d"
            if isinstance(db_stats.get(db_key), dict):
                revenue[f"{kpi['id']}_db_7d"] = db_stats[db_key].get("last_7d", 0)
                revenue[f"{kpi['id']}_db_30d"] = db_stats[db_key].get("last_30d", 0)
    snap["revenue_28d"] = revenue

    # Geo / channel breakdown
    if geo_28.get("rows"):
        total_sessions = us_sessions = 0
        for r in geo_28["rows"]:
            country = r["dimensionValues"][0]["value"]
            sessions = int(r["metricValues"][0]["value"])
            total_sessions += sessions
            if country == "United States":
                us_sessions = sessions
        snap["us_sessions_28d"] = us_sessions
        snap["total_sessions_28d"] = total_sessions
        snap["us_traffic_share"] = round(us_sessions / total_sessions, 4) if total_sessions else 0.0

    for r in sources_28.get("rows", []):
        ch = r["dimensionValues"][0]["value"]
        snap["ga4_channels_28d"][ch] = {
            "sessions": int(r["metricValues"][0]["value"]),
            "active_users": int(r["metricValues"][1]["value"]) if len(r["metricValues"]) > 1 else 0,
            "bounce": round(float(r["metricValues"][2]["value"]), 4) if len(r["metricValues"]) > 2 else 0,
        }

    return snap


# ---------------------------------------------------------------------------
# Comparison (vs prior snapshot)
# ---------------------------------------------------------------------------

def find_prior_snapshot(cfg, latest_run_ts: str) -> Optional[Path]:
    runs_dir = cfg.site_runs_dir
    if not runs_dir.is_dir():
        return None
    runs = sorted(p.name for p in runs_dir.iterdir() if p.is_dir() and p.name != "latest" and p.name != "_queue")
    if latest_run_ts in runs:
        idx = runs.index(latest_run_ts)
        if idx > 0:
            return runs_dir / runs[idx - 1] / "snapshot.json"
    return None


def build_comparison(snap: dict, prior: dict) -> dict:
    def delta(now, then):
        if isinstance(now, (int, float)) and isinstance(then, (int, float)):
            return round(now - then, 2)
        return None

    def pct(now, then):
        if isinstance(now, (int, float)) and isinstance(then, (int, float)) and then:
            return round((now - then) * 100 / then, 1)
        return None

    cmp_obj: dict = {
        "site": snap["site"],
        "current_run": snap.get("captured_at"),
        "prior_run": prior.get("captured_at") if prior else None,
        "gsc_90d": {},
        "revenue_28d": {},
    }
    for k in ("total_impressions", "total_clicks", "total_ctr", "avg_position",
              "num_queries", "num_pages_indexed"):
        c = snap["gsc_90d"].get(k); p = (prior.get("gsc_90d", {}) or {}).get(k)
        cmp_obj["gsc_90d"][k] = {"current": c, "prior": p, "delta": delta(c, p), "pct": pct(c, p)}
    for k in snap.get("revenue_28d", {}):
        c = snap["revenue_28d"].get(k); p = (prior.get("revenue_28d", {}) or {}).get(k)
        cmp_obj["revenue_28d"][k] = {"current": c, "prior": p, "delta": delta(c, p), "pct": pct(c, p)}
    cmp_obj["us_traffic_share"] = {
        "current": snap.get("us_traffic_share"), "prior": prior.get("us_traffic_share") if prior else None,
        "delta": delta(snap.get("us_traffic_share", 0), (prior or {}).get("us_traffic_share", 0)),
    }
    return cmp_obj


# ---------------------------------------------------------------------------
# Goal scoring
# ---------------------------------------------------------------------------

def resolve_metric(snap: dict, path: str):
    """Walk a dotted path; supports gsc.query_position.<query> as a special prefix."""
    if path.startswith("gsc.query_position."):
        q = path[len("gsc.query_position."):].strip().lower()
        node = snap.get("gsc_90d", {}).get("query_position", {}).get(q)
        return float(node["position"]) if node else None
    if path.startswith("gsc.page_position."):
        url = path[len("gsc.page_position."):].strip()
        node = snap.get("gsc_90d", {}).get("page_position", {}).get(url)
        return float(node["position"]) if node else None
    if path.startswith("gsc.rank_buckets."):
        bucket = path[len("gsc.rank_buckets."):]
        v = snap.get("gsc_90d", {}).get("rank_buckets", {}).get(bucket)
        return float(v) if v is not None else None
    parts = path.split(".")
    cur: Any = snap
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    try: return float(cur)
    except (TypeError, ValueError): return None


def score_prior_goals(snap: dict, prior_goals: dict) -> dict:
    if not prior_goals:
        return {"goals": []}
    out = {"goals": []}
    for g in prior_goals.get("goals", []):
        cur = resolve_metric(snap, g.get("target_metric", ""))
        result = dict(g)
        result["current"] = cur
        if cur is None or g.get("baseline") is None or g.get("target") is None:
            result["status"] = "unknown"; result["progress_pct"] = None
        else:
            span = g["target"] - g["baseline"]
            move = cur - g["baseline"]
            pct = 0.0 if span == 0 else max(0.0, min(1.0, move / span))
            result["progress_pct"] = round(pct * 100, 1)
            lower_is_better = span < 0
            if lower_is_better:
                if cur <= g["target"]: result["status"] = "achieved"
                elif pct > 0: result["status"] = "improving"
                else: result["status"] = "no-movement"
            else:
                if cur >= g["target"]: result["status"] = "achieved"
                elif pct > 0: result["status"] = "improving"
                else: result["status"] = "no-movement"
        out["goals"].append(result)
    return out


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def build_recommendations(cfg, run_dir: Path, snap: dict) -> tuple[list[dict], list[dict]]:
    """Return (recommendations, declared_goals)."""
    data = run_dir / "data"
    recs: list[dict] = []
    goals: list[dict] = []
    rec_id_counter = 1
    def next_id():
        nonlocal rec_id_counter
        rid = f"rec-{rec_id_counter:03d}"; rec_id_counter += 1; return rid

    max_recs = cfg.get("analyzer", {}).get("max_recs_per_run", 12)

    # ---- Top-5 rank targets (US-first) ----
    country = cfg["data_sources"]["gsc"].get("default_country_filter", "usa")
    country_targets = _load(data / f"gsc-{country}-top5-targets.json").get("rows", [])
    global_targets = _load(data / "gsc-top5-targets.json").get("rows", [])

    # Prefer country-specific targets, fall back to global
    primary_targets = country_targets[:5] if country_targets else global_targets[:5]
    for t in primary_targets:
        rid = next_id()
        recs.append({
            "id": rid,
            "type": "top5-target-page",
            "priority": "high" if t["tier"] == "striking" else "medium",
            "title": f"Build keyword-targeted page for \"{t['query']}\" → top-5",
            "rationale": (
                f"Currently pos {t['position']:.1f} ({t['tier']} tier), "
                f"{t['impressions']} impressions/90d, {t['clicks']} clicks. "
                f"Score: {t['score']}. A dedicated landing page targeting this exact "
                f"query in URL/title/H1/body should push it into top-5."
            ),
            "expected_impact": {
                "metric": f"gsc.query_position.{t['query']}",
                "baseline": t["position"],
                "target": 5.0,
                "horizon_weeks": 8,
            },
            "implementation_outline": {
                "files": ["src/server.ts (new SSR route)", "sitemap.xml (new entry)"],
                "json_ld": "CollectionPage",
                "internal_links_from": ["/", "/recipes (or equivalent hub page)"],
                "notes": (
                    "Target query MUST appear in URL slug, <title>, H1, and first 100 "
                    "words. Use catalog content as substrate, layer editorial framing "
                    "(intro paragraph, FAQ, related)."
                ),
            },
            "data_refs": [f"data/gsc-{country}-top5-targets.json", "data/gsc-queries-90d.json"],
            "implemented": False,
        })
        # Goal for this rec
        goals.append({
            "id": f"top5-{t['query'].replace(' ', '-')}",
            "description": f"Move \"{t['query']}\" into top-5 via dedicated landing page",
            "target_metric": f"gsc.query_position.{t['query']}",
            "baseline": t["position"],
            "target": 5.0,
            "rationale": f"Current pos {t['position']:.1f}, {t['impressions']} impr/90d. New page targets the exact query.",
            "check_by": "+8-weeks",
            "is_top5_goal": True,
            "from_rec": rid,
        })
        if len(recs) >= max_recs: break

    # ---- Zero-click queries (CTR fix) ----
    if len(recs) < max_recs:
        zero_click = _load(data / "gsc-zero-click.json").get("rows", [])
        for r in zero_click[:3]:
            if len(recs) >= max_recs: break
            keys = r.get("keys", [])
            query_or_pair = keys[0] if len(keys) == 1 else f"{keys[0]} on {keys[1]}"
            rid = next_id()
            recs.append({
                "id": rid,
                "type": "ctr-fix",
                "priority": "medium",
                "title": f"Rewrite snippet for \"{query_or_pair}\" — high-impression zero-click",
                "rationale": (
                    f"Pos {r.get('position',0):.1f} with {r.get('impressions',0)} impressions "
                    f"and 0 clicks/90d. Indexing works but the SERP snippet isn't compelling."
                ),
                "expected_impact": {
                    "metric": f"gsc.query_position.{keys[0]}",
                    "baseline": r.get("position", 0),
                    "target": r.get("position", 0),  # rank stays; clicks rise
                    "horizon_weeks": 4,
                },
                "implementation_outline": {
                    "files": ["<page that ranks for this query>"],
                    "notes": "Rewrite <title> (≤60 chars) and meta-description (≤160 chars) to better match the query intent. Add a number, a year, or a benefit phrase.",
                },
                "data_refs": ["data/gsc-zero-click.json"],
                "implemented": False,
            })

    # ---- Striking-distance (already pos 4-10, near top-3) ----
    if len(recs) < max_recs:
        striking = _load(data / "gsc-striking-distance.json").get("rows", [])
        for r in striking[:2]:
            if len(recs) >= max_recs: break
            keys = r.get("keys", [])
            if not keys: continue
            rid = next_id()
            recs.append({
                "id": rid,
                "type": "internal-link",
                "priority": "low",
                "title": f"Boost striking-distance query \"{keys[0]}\" → top-3 with internal links",
                "rationale": (
                    f"Pos {r.get('position',0):.1f}, {r.get('impressions',0)} impr/90d. Page is "
                    f"close to top-3 — additional internal links from authority pages can push it."
                ),
                "expected_impact": {
                    "metric": f"gsc.query_position.{keys[0]}",
                    "baseline": r.get("position", 0),
                    "target": 3.0,
                    "horizon_weeks": 6,
                },
                "implementation_outline": {
                    "notes": "Add 2-3 internal links from high-authority pages (homepage, category pages) with anchor text matching the query.",
                },
                "data_refs": ["data/gsc-striking-distance.json"],
                "implemented": False,
            })

    # ---- Rank regressions (alarm) ----
    if len(recs) < max_recs:
        regressions = _load(data / "gsc-rank-regressions.json").get("rows", [])
        for r in regressions[:2]:
            if r.get("position_delta", 0) < 5: break  # only meaningful drops
            if len(recs) >= max_recs: break
            rid = next_id()
            recs.append({
                "id": rid,
                "type": "indexing-fix",
                "priority": "high",
                "title": f"Investigate rank regression on {r['page'][:60]}",
                "rationale": (
                    f"Pos went from {r['prev_position']:.1f} → {r['cur_position']:.1f} "
                    f"(Δ +{r['position_delta']:.1f}). Impressions {r['prev_impressions']} → "
                    f"{r['cur_impressions']} ({r['impressions_delta_pct']:+.1f}%)."
                ),
                "expected_impact": {
                    "metric": f"gsc.page_position.{r['page']}",
                    "baseline": r["cur_position"],
                    "target": r["prev_position"],
                    "horizon_weeks": 4,
                },
                "implementation_outline": {
                    "notes": "Diff page content vs previous version. Check for: removed text, broken canonical, internal links lost, JSON-LD removed. Restore.",
                },
                "data_refs": ["data/gsc-rank-regressions.json"],
                "implemented": False,
            })

    # ---- Revenue conversion alarm (if KPIs configured + dropping) ----
    revenue = snap.get("revenue_28d", {})
    if revenue and len(recs) < max_recs:
        # Look for any KPI that dropped meaningfully (will compare in reporter)
        for kpi in cfg.get("revenue_kpis", []):
            if len(recs) >= max_recs: break
            db_30 = revenue.get(f"{kpi['id']}_db_30d", 0)
            db_7 = revenue.get(f"{kpi['id']}_db_7d", 0)
            if db_30 == 0 and db_7 == 0:
                # No conversions at all — surface it
                rid = next_id()
                recs.append({
                    "id": rid,
                    "type": "conversion-path",
                    "priority": "high",
                    "title": f"Zero {kpi['label']} in last 30d — investigate funnel",
                    "rationale": (
                        f"`{kpi['id']}` shows 0 conversions in 7d AND 30d. Either the event "
                        f"isn't firing (instrumentation issue) or there's no traffic to convert."
                    ),
                    "implementation_outline": {
                        "notes": (
                            f"Verify GA4 event {kpi.get('ga4_event','-')!r} is firing in dev. "
                            f"Check DB table {kpi.get('db_table','-')!r} for rows. "
                            f"If both empty, check whether organic traffic reached pages with "
                            f"the conversion CTA (look at ga4-organic-landing-90d.json)."
                        ),
                    },
                    "data_refs": ["data/ga4-organic-events-28d.json", f"data/ga4-events-28d.json"],
                    "implemented": False,
                })

    return recs[:max_recs], goals


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _crawl_for_audit(cfg, run_dir):
    """On-demand crawl of the site's homepage + top GSC pages + sitemap entries.

    Used by the LLM audit when the collector didn't already produce a
    pages.jsonl. Returns a list of page records (url, title, h1,
    description, canonical, body_text). Capped at 20 pages.
    """
    try:
        # Reuse the BFS crawler from progressive-improvement-agent
        sys.path.insert(0, str(_REPO_ROOT / "progressive-improvement-agent"))
        from crawler import crawl as _crawl
    except ImportError as e:
        print(f"  [llm-audit] crawler import failed: {e}", file=sys.stderr)
        return []

    domain = cfg.get("site", {}).get("domain", "")
    if not domain:
        return []
    base_url = f"https://{domain}"

    # Seed URLs: homepage + top GSC pages + sitemap roots
    seeds: list[str] = ["/"]
    gsc_pages = _load(run_dir / "data" / "gsc-pages-90d.json")
    for r in (gsc_pages.get("rows") or [])[:10]:
        url = r.get("keys", [None])[0]
        if url and url.startswith(base_url):
            seeds.append(url)

    print(f"  [llm-audit] on-demand crawl: {base_url} ({len(seeds)} seeds)",
          file=sys.stderr)
    pages = []
    for page in _crawl(
        base_url=base_url,
        seed_urls=seeds,
        use_sitemap=True,
        max_depth=1,
        max_pages=20,
        path_excludes=["/admin/*", "/api/*", "/auth/*"],
        request_timeout_s=15,
        user_agent="reusable-agents-seo-audit/1.0",
        throttle_ms=400,
    ):
        if 200 <= page.status_code < 300 and page.body_text:
            pages.append({
                "url": page.url, "title": page.title,
                "h1": page.h1, "description": page.description,
                "canonical": page.canonical, "body_text": page.body_text,
            })
    return pages


def _build_ai_chat_callable(cfg):
    """Return a `(messages, *, temperature, max_tokens) -> str` callable that
    routes through the framework's AI provider config, OR None if unavailable.

    Resolution order:
      1. cfg.analyzer.ai_provider / ai_model (per-site overrides)
      2. framework default for agent_id 'seo-analyzer'
      3. None (returns None — caller skips LLM pass)
    """
    try:
        from framework.core import ai_providers
    except ImportError:
        return None
    analyzer_cfg = cfg.get("analyzer", {}) or {}
    override_provider = analyzer_cfg.get("ai_provider", "")
    override_model = analyzer_cfg.get("ai_model", "")
    try:
        client = ai_providers.ai_client_for(
            "seo-analyzer",
            override_provider=override_provider or None,
            override_model=override_model or None,
        )
    except Exception:
        return None
    return client.chat


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-ts", default=None,
                   help="Specific run-ts dir to analyze (default: latest)")
    args = p.parse_args()

    cfg = load_config_from_env()
    if args.run_ts:
        run_dir = cfg.run_dir_for_ts(args.run_ts)
    else:
        run_dir = cfg.latest_run_dir()
        if run_dir is None:
            raise SystemExit(f"No runs for site={cfg.site_id}. Run seo-data-collector first.")
    if not run_dir.is_dir():
        raise SystemExit(f"Run dir not found: {run_dir}")
    run_ts = run_dir.name

    print(f"[analyzer] site={cfg.site_id} run_ts={run_ts}", file=sys.stderr)

    # 1. Snapshot
    snap = build_snapshot(cfg, run_dir)
    (run_dir / "snapshot.json").write_text(json.dumps(snap, indent=2))
    print(f"  ✓ snapshot.json ({snap['gsc_90d']['num_queries']} queries, "
          f"{snap['gsc_90d']['total_impressions']:,} impressions)", file=sys.stderr)

    # 2. Compare to prior
    prior_snap_path = find_prior_snapshot(cfg, run_ts)
    if prior_snap_path and prior_snap_path.is_file():
        prior = json.loads(prior_snap_path.read_text())
        cmp_obj = build_comparison(snap, prior)
        (run_dir / "comparison.json").write_text(json.dumps(cmp_obj, indent=2))
        print(f"  ✓ comparison.json (vs {prior_snap_path.parent.name})", file=sys.stderr)
    else:
        print(f"  ⏭  no prior snapshot — skipping comparison", file=sys.stderr)

    # 3. Score prior goals
    if prior_snap_path:
        prior_goals_path = prior_snap_path.parent / "goals.json"
        if prior_goals_path.is_file():
            prior_goals = json.loads(prior_goals_path.read_text())
            scored = score_prior_goals(snap, prior_goals)
            (run_dir / "goal-progress.json").write_text(json.dumps({
                "goals_declared_at": prior_snap_path.parent.name,
                "measured_at": run_ts,
                **scored,
            }, indent=2))
            print(f"  ✓ goal-progress.json ({len(scored['goals'])} goals scored)", file=sys.stderr)

    # 4. Build recommendations + declare new goals
    recs, declared_goals = build_recommendations(cfg, run_dir, snap)

    # 4b. LLM-driven adaptive audit pass — flags evolving SEO opportunities
    # the deterministic pass can't see (CWV, schema, EEAT, AI search,
    # mobile-first specifics). Disabled with SEO_DISABLE_LLM_AUDIT=1.
    if os.environ.get("SEO_DISABLE_LLM_AUDIT") != "1":
        try:
            from llm_audit import (
                run_llm_audit,
                issues_to_recommendations,
            )
            ai_chat = _build_ai_chat_callable(cfg)
            if ai_chat is not None:
                # Pull the page records the collector scraped (if any).
                # The SEO collector currently doesn't crawl pages, so we
                # do an on-demand crawl using the same crawler the
                # progressive-improvement agent uses. Caches to
                # data/pages.jsonl for re-use next run.
                pages_path = run_dir / "data" / "pages.jsonl"
                pages: list[dict] = []
                if pages_path.is_file():
                    for line in pages_path.read_text().splitlines():
                        if not line.strip():
                            continue
                        try:
                            pages.append(json.loads(line))
                        except Exception:
                            pass
                if not pages:
                    pages = _crawl_for_audit(cfg, run_dir)
                    if pages:
                        with pages_path.open("w") as f:
                            for p in pages:
                                f.write(json.dumps(p) + "\n")
                # Cap at 20 pages to keep token usage sane
                pages = pages[:20]
                if pages:
                    print(f"  → LLM audit: {len(pages)} pages", file=sys.stderr)
                    issues = run_llm_audit(
                        pages=pages,
                        site_label=cfg.site_id,
                        primary_objective=cfg.get("analyzer", {}).get(
                            "primary_objective", "top5-rank"),
                        ai_chat_callable=ai_chat,
                        batch_size=4,
                    )
                    print(f"  → LLM audit found {len(issues)} issues", file=sys.stderr)
                    next_id_count = {"i": len(recs)}
                    def _next_id():
                        next_id_count["i"] += 1
                        return f"rec-{next_id_count['i']:03d}"
                    llm_recs = issues_to_recommendations(issues, _next_id)
                    # De-dupe by (url, llm_check_id) to avoid stomping
                    seen = {(r.get("data_refs",[None])[0], r.get("llm_check_id"))
                            for r in recs if r.get("llm_check_id")}
                    for r in llm_recs:
                        key = (r.get("data_refs",[None])[0], r.get("llm_check_id"))
                        if key in seen:
                            continue
                        recs.append(r)
                        seen.add(key)
                else:
                    print(f"  → LLM audit skipped (no pages.jsonl)", file=sys.stderr)
            else:
                print("  → LLM audit skipped (no AI client available)", file=sys.stderr)
        except Exception as e:
            print(f"  → LLM audit failed: {e}", file=sys.stderr)

    summary = (
        f"{len(recs)} recommendations: "
        f"{sum(1 for r in recs if r['type'] == 'top5-target-page')} top-5 pages, "
        f"{sum(1 for r in recs if r['type'] == 'ctr-fix')} CTR fixes, "
        f"{sum(1 for r in recs if r['type'] == 'indexing-fix')} indexing fixes, "
        f"{sum(1 for r in recs if r['type'] == 'conversion-path')} conversion-path, "
        f"{sum(1 for r in recs if r.get('llm_check_id'))} from adaptive LLM audit."
    )
    run_files.write_recommendations(
        run_dir,
        site=cfg.site_id, run_ts=run_ts, mode=cfg.mode,
        summary=summary, recommendations=recs,
    )
    (run_dir / "goals.json").write_text(json.dumps({
        "site": cfg.site_id,
        "run_ts": run_ts,
        "change_summary": summary,
        "goals": declared_goals,
    }, indent=2))
    print(f"  ✓ recommendations.json ({len(recs)} recs)", file=sys.stderr)
    print(f"  ✓ goals.json ({len(declared_goals)} declared)", file=sys.stderr)

    print(str(run_dir))


if __name__ == "__main__":
    main()
