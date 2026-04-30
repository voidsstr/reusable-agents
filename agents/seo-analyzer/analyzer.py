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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
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
    # Optional ads data — surfaces in snapshot.ads if present
    ads_paid_organic = _load(data / "ads-paid-vs-organic.json")
    ads_search_terms = _load(data / "ads-search-terms.json")
    ads_keyword_perf = _load(data / "ads-keyword-perf.json")
    ads_ad_copy = _load(data / "ads-ad-copy.json")

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

    # Ads rollup (only populated if google_ads is configured)
    ads_summary = {}
    if ads_paid_organic.get("results"):
        ads_summary["paid_organic_rows"] = len(ads_paid_organic["results"])
    if ads_search_terms.get("results"):
        ads_summary["search_term_rows"] = len(ads_search_terms["results"])
    if ads_keyword_perf.get("results"):
        ads_summary["keyword_perf_rows"] = len(ads_keyword_perf["results"])
    if ads_ad_copy.get("results"):
        ads_summary["ad_copy_rows"] = len(ads_ad_copy["results"])
    if ads_summary:
        snap["ads_90d"] = ads_summary

    return snap


# ---------------------------------------------------------------------------
# Comparison (vs prior snapshot)
# ---------------------------------------------------------------------------

def _canonical_rec_key(rec: dict) -> Optional[str]:
    """Stable identity for a rec across runs — used by the dedupe filter
    so the analyzer doesn't keep re-proposing the same change every cron
    cycle. Must match the same shape between the rec we're considering
    NOW and the recs the implementer already shipped/skipped previously.

    Returns None for rec types we can't safely dedupe (defensive default —
    we'd rather re-propose than silently lose a fresh signal)."""
    typ = rec.get("type", "")
    if typ == "top5-target-page":
        target = rec.get("target") or {}
        query = target.get("query") or ""
        if not query:
            # Fall back to the rec title's quoted query if present
            import re as _re
            m = _re.search(r'for "([^"]+)"', rec.get("title") or "")
            query = m.group(1) if m else ""
        return f"top5:{query.lower().strip()}" if query else None
    if typ == "ctr-fix":
        # The URL of the page whose snippet we're rewriting is the natural key.
        url = rec.get("url", "")
        if not url:
            import re as _re
            m = _re.search(r'on (https?://\S+?)["\s,]', (rec.get("title") or "") + " ")
            url = m.group(1).rstrip('"., ') if m else ""
        return f"ctr-fix:{url}" if url else None
    if typ == "internal-link":
        import re as _re
        m = _re.search(r'"([^"]+)"', rec.get("title") or "")
        query = (m.group(1) if m else "").lower().strip()
        return f"internal-link:{query}" if query else None
    if typ in ("article-snippet-rewrite", "article-title-fix"):
        url = rec.get("url", "")
        return f"{typ}:{url}" if url else None
    if typ == "article-orphan-boost":
        # Multi-URL — use the sorted-tuple as the key so it's stable
        urls = rec.get("orphan_urls") or []
        return f"article-orphan-boost:{','.join(sorted(urls)[:5])}" if urls else None
    return None


def _load_handled_rec_keys(cfg, current_run_ts: str,
                            scan_runs: int = 30) -> set[str]:
    """Walk prior runs' recommendations.json + responses-archive to find
    every rec the implementer already shipped/implemented OR the user
    explicitly skipped/deferred. Returns a set of canonical keys to
    EXCLUDE from this run's proposals.

    Without this, the analyzer keeps proposing the same "build a page for
    cube steak recipes" rec every 3 hours even after it's been shipped —
    the operator's inbox fills with recommendations that say
    "ALREADY-IMPLEMENTED" when the implementer runs them.
    """
    handled: set[str] = set()
    try:
        from framework.core.storage import get_storage
        s = get_storage()
    except Exception:
        return handled
    # Map cfg.site_id (e.g. "aisleprompt") → likely source agent id
    site_id = cfg.site_id if hasattr(cfg, "site_id") else cfg.get("site", {}).get("id", "")
    candidate_agents = [
        f"{site_id}-seo-opportunity-agent",
    ]
    for agent_id in candidate_agents:
        try:
            keys = list(s.list_prefix(f"agents/{agent_id}/runs/"))
        except Exception:
            continue
        # Build set of run_ts (excluding the rundir-... dispatch dirs)
        run_tss: set[str] = set()
        for k in keys:
            tail = k.split(f"agents/{agent_id}/runs/", 1)[1] if "agents/" in k else ""
            if not tail or "rundir-" in tail:
                continue
            run_ts = tail.split("/", 1)[0]
            if run_ts and run_ts != current_run_ts:
                run_tss.add(run_ts)
        # Walk newest N runs
        for run_ts in sorted(run_tss, reverse=True)[:scan_runs]:
            try:
                d = s.read_json(f"agents/{agent_id}/runs/{run_ts}/recommendations.json")
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            for r in d.get("recommendations", []):
                # Skip recs that aren't actually "handled" — those are
                # still legitimately re-proposable
                handled_via_implement = (
                    r.get("shipped") or r.get("implemented")
                    or r.get("implemented_via") == "pre-existing"
                    or r.get("applied")
                )
                handled_via_skip = bool(r.get("deferred"))
                if not (handled_via_implement or handled_via_skip):
                    continue
                key = _canonical_rec_key(r)
                if key:
                    handled.add(key)
    return handled


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

def _load_repo_routes(run_dir: Path) -> list[dict]:
    """Read data/repo-routes.json (written by collector's scan_repo).
    Returns [] when the file is absent or empty."""
    rr = _load(run_dir / "data" / "repo-routes.json")
    return rr.get("routes", []) if rr else []


def _files_for_url(routes: list[dict], url: str) -> list[str]:
    """Match a URL against repo-routes patterns, return candidate file paths.

    Lightweight: regex-converts :slug / [slug] / :slug* to wildcards and
    checks if path matches. Returns "<file>:<line>" strings the implementer
    can use directly.
    """
    if not url or not routes:
        return []
    try:
        path = urlparse(url).path or url
    except Exception:
        path = url
    matches = []
    for r in routes:
        pat = r.get("url_pattern", "")
        if not pat:
            continue
        # Build regex from pattern by replacing :params + [slug] segments.
        # Apply substitutions on the unescaped pattern, then anchor.
        rx_pat = re.sub(r":[A-Za-z_][A-Za-z0-9_]*\*", "PARAMSPLAT", pat)
        rx_pat = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "PARAMSEG", rx_pat)
        rx_pat = re.sub(r"\[\.\.\.([^\]]+)\]", "PARAMSPLAT", rx_pat)
        rx_pat = re.sub(r"\[([^\]]+)\]", "PARAMSEG", rx_pat)
        rx_str = re.escape(rx_pat)
        rx_str = rx_str.replace("PARAMSPLAT", ".+")
        rx_str = rx_str.replace("PARAMSEG", "[^/]+")
        rx_str = "^" + rx_str + "/?$"
        try:
            if re.match(rx_str, path) or pat == path:
                matches.append(f"{r['file']}:{r.get('line', 1)}")
        except re.error:
            continue
    return matches[:5]


def _match_files_by_type(routes: list[dict], page_type: str) -> list[str]:
    """Heuristic: match repo-route file by page type when URL matching fails.
    Returns first-match file paths whose path contains the type keyword."""
    if not page_type:
        return []
    out = []
    for r in routes:
        f = r.get("file", "").lower()
        if page_type.replace("_", "-") in f or page_type in f:
            out.append(f"{r['file']}:{r.get('line', 1)}")
    return out[:3]


def _load_pages_by_type(data: Path) -> list[dict]:
    """Load the inventory crawler's per-page records, one JSON per line."""
    p = data / "pages-by-type.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _required_jsonld_for_type(page_type: str) -> list[str]:
    """Map a page-type bucket to the JSON-LD @types we expect.

    Modeled on studio-supplies.com:
      product/recipe → Product or Recipe + BreadcrumbList + FAQPage
      collection / shop_category → CollectionPage + BreadcrumbList
      article / blog / review → Article (or Review) + BreadcrumbList
      home → WebSite + Organization
    """
    pt = (page_type or "").lower()
    if pt in ("product", "recipe"):
        return ["Product", "BreadcrumbList"]  # FAQPage advisory
    if pt in ("collection", "category", "shop_category"):
        return ["CollectionPage", "BreadcrumbList"]
    if pt in ("article", "blog", "review", "guide"):
        return ["Article", "BreadcrumbList"]
    if pt in ("home", "homepage"):
        return ["WebSite", "Organization"]
    return []


def _add_onpage_recs(data: Path, recs: list, next_id, max_recs: int) -> None:
    """Apply the studio-supplies-derived on-page audit rules.

    Reads pages-by-type.jsonl + site-signals.json and emits up to a
    handful of summary recs. Each rec aggregates issues across many
    pages — we surface counts + 3-5 sample URLs rather than spamming
    one rec per page (the implementer can fan out from one rec).
    """
    if len(recs) >= max_recs:
        return

    pages = _load_pages_by_type(data)
    signals = _load(data / "site-signals.json")

    def _budget() -> bool:
        return len(recs) < max_recs

    def _samples(items: list[dict], n: int = 5) -> list[str]:
        return [it.get("url", "") for it in items[:n] if it.get("url")]

    # ── Per-page rules (require pages-by-type.jsonl) ─────────────────
    if pages:
        ok_pages = [p for p in pages if 200 <= int(p.get("status", 0) or 0) < 300]

        # Title length
        if _budget():
            short = [p for p in ok_pages if 0 < len(p.get("title") or "") < 30]
            long = [p for p in ok_pages if len(p.get("title") or "") > 70]
            if (short or long) and (len(short) + len(long)) >= 2:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-title-length", "priority": "medium",
                    "title": f"Tighten <title> length on {len(short) + len(long)} page(s) (target 30-65 chars)",
                    "rationale": (
                        f"{len(short)} title(s) below 30 chars (under-using SERP real estate); "
                        f"{len(long)} title(s) over 70 (Google truncates). studio-supplies pattern: "
                        f"\"<Specific Phrase> | Brand\" 30-65 chars. Sample short: "
                        f"{', '.join(_samples(short, 3))}. Sample long: {', '.join(_samples(long, 3))}."
                    ),
                    "expected_impact": {"metric": "gsc.avg_ctr", "horizon_weeks": 4},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "For each flagged URL, rewrite <title> to 30-65 chars including the primary "
                            "keyword first, brand suffix \" | Site Name\" last. Verify in /sitemap.xml "
                            "doesn't 404 after the change."
                        ),
                    },
                    "sample_urls": _samples(short, 3) + _samples(long, 3),
                    "implemented": False,
                })

        # Missing / under-length / over-length meta description
        if _budget():
            missing = [p for p in ok_pages if not (p.get("description") or "").strip()]
            short = [p for p in ok_pages if 0 < len(p.get("description") or "") < 120]
            long = [p for p in ok_pages if len(p.get("description") or "") > 165]
            tot = len(missing) + len(short) + len(long)
            if tot >= 2:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-meta-description", "priority": "medium",
                    "title": f"Fix meta description on {tot} page(s) — missing/short/long",
                    "rationale": (
                        f"missing: {len(missing)}, under 120 chars: {len(short)}, over 165: {len(long)}. "
                        f"Studio-supplies-style pages run 145-160 chars and lead with the primary keyword. "
                        f"Sample missing: {', '.join(_samples(missing, 3))}."
                    ),
                    "expected_impact": {"metric": "gsc.avg_ctr", "horizon_weeks": 4},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "Write a 145-160 char meta description per page that opens with the page's "
                            "primary keyword, includes a benefit phrase, and ends with a soft CTA "
                            "(\"Compare specs and prices.\")."
                        ),
                    },
                    "sample_urls": _samples(missing + short + long, 5),
                    "implemented": False,
                })

        # Canonical missing
        if _budget():
            no_canon = [p for p in ok_pages if not (p.get("canonical") or "").strip()]
            if len(no_canon) >= 2:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-canonical-missing", "priority": "high",
                    "title": f"Add <link rel=\"canonical\"> on {len(no_canon)} page(s)",
                    "rationale": (
                        "Pages without a canonical risk duplicate-content dilution when query "
                        "params, tracking codes, or alternate template paths cause variant URLs "
                        "to be indexed separately. studio-supplies sets canonical on every page."
                    ),
                    "expected_impact": {"metric": "indexed_pages", "horizon_weeks": 6},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "In SSR <head>, emit `<link rel=\"canonical\" href=\"https://<domain><path>\">` "
                            "where path strips query params, fragments, and trailing slashes. "
                            "Verify on each flagged URL after deploy."
                        ),
                    },
                    "sample_urls": _samples(no_canon, 5),
                    "implemented": False,
                })

        # H1 — missing or multiple
        if _budget():
            no_h1 = [p for p in ok_pages if int(p.get("h1_count", 0) or 0) == 0]
            many_h1 = [p for p in ok_pages if int(p.get("h1_count", 0) or 0) > 1]
            if (no_h1 or many_h1) and (len(no_h1) + len(many_h1)) >= 2:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-h1-issue", "priority": "medium",
                    "title": f"Fix h1 on {len(no_h1) + len(many_h1)} page(s) — exactly one h1 per page",
                    "rationale": (
                        f"{len(no_h1)} page(s) have no h1 (Google falls back to <title> "
                        f"or worse, a nav link). {len(many_h1)} have multiple h1s "
                        f"(diluted topical signal). Studio-supplies rule: 1 h1, primary keyword, "
                        f"matches user-visible page topic."
                    ),
                    "expected_impact": {"metric": "gsc.avg_position", "horizon_weeks": 6},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "Audit the SSR template. Demote redundant h1s (logo, nav) to <p>/<div>. "
                            "On pages without h1, add one near the top of <main> using the page's "
                            "primary keyword."
                        ),
                    },
                    "sample_urls": _samples(no_h1 + many_h1, 5),
                    "implemented": False,
                })

        # OG tag completeness
        if _budget():
            need = {"title", "description", "image", "url", "type"}
            incomplete = [
                p for p in ok_pages
                if not need.issubset(set(p.get("og_keys") or []))
            ]
            if len(incomplete) >= 3:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-og-incomplete", "priority": "medium",
                    "title": f"Complete Open Graph tags on {len(incomplete)} page(s)",
                    "rationale": (
                        "Open Graph drives the link preview Slack/Discord/Facebook/iMessage shows. "
                        "Pages missing og:title/description/image/url/type get a generic preview that "
                        "reduces CTR from social. Studio-supplies emits all five on every page."
                    ),
                    "expected_impact": {"metric": "social_referral_ctr", "horizon_weeks": 4},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "In SSR <head>, emit og:site_name, og:title, og:description, og:url, "
                            "og:type (website|article|product), og:image (≥1200×630). Mirror title "
                            "and meta description fallbacks when page-specific values are missing."
                        ),
                    },
                    "sample_urls": _samples(incomplete, 5),
                    "implemented": False,
                })

        # Twitter card missing
        if _budget():
            no_twitter = [p for p in ok_pages if not (p.get("twitter_card_keys") or [])]
            if len(no_twitter) >= 3:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-twitter-card-missing", "priority": "low",
                    "title": f"Add Twitter Card meta on {len(no_twitter)} page(s)",
                    "rationale": (
                        "Without twitter:card, X/Twitter renders a plain link instead of a rich "
                        "preview. Studio-supplies emits twitter:card, twitter:title, twitter:description "
                        "on every page (3 tags minimum)."
                    ),
                    "expected_impact": {"metric": "social_referral_ctr", "horizon_weeks": 4},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "Emit `<meta name=\"twitter:card\" content=\"summary_large_image\">` plus "
                            "twitter:title, twitter:description, optional twitter:site (@handle) and "
                            "twitter:image (≥1200×675)."
                        ),
                    },
                    "sample_urls": _samples(no_twitter, 5),
                    "implemented": False,
                })

        # JSON-LD schema by page type
        if _budget():
            broken = []
            for p in ok_pages:
                req = _required_jsonld_for_type(p.get("type", ""))
                have = set(p.get("jsonld_types") or [])
                missing = [t for t in req if t not in have]
                if req and missing:
                    broken.append({"url": p.get("url", ""), "type": p.get("type", ""), "missing": missing})
            if len(broken) >= 2:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-jsonld-missing", "priority": "high",
                    "title": f"Add structured-data (JSON-LD) on {len(broken)} page(s)",
                    "rationale": (
                        "Per-page-type JSON-LD is the foundation of rich SERP results — review stars, "
                        "breadcrumb trails, FAQ accordions, sitelinks. Studio-supplies emits "
                        "Product+BreadcrumbList+FAQPage on products, Article+BreadcrumbList on blogs, "
                        "WebSite+Organization on home, CollectionPage+BreadcrumbList on collections. "
                        f"Sample gaps: " + "; ".join(
                            f"{b['url']} (type={b['type']}, missing={','.join(b['missing'])})"
                            for b in broken[:3]
                        )
                    ),
                    "expected_impact": {"metric": "rich_result_ctr", "horizon_weeks": 8},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "In each page-type's SSR template, emit a `<script type=\"application/ld+json\">` "
                            "block. Use schema.org's per-type required fields (Product needs name, image, "
                            "description, brand, offers; Article needs headline, author, datePublished, "
                            "image, publisher; BreadcrumbList needs itemListElement with @id+name+position)."
                        ),
                    },
                    "sample_urls": [b["url"] for b in broken[:5]],
                    "missing_breakdown": broken[:10],
                    "implemented": False,
                })

        # FAQ schema absent on product/category-style pages (advisory — high CTR lift)
        if _budget():
            faq_targets = [
                p for p in ok_pages
                if p.get("type") in ("product", "recipe", "collection", "category", "shop_category")
                and "FAQPage" not in (p.get("jsonld_types") or [])
                and not p.get("faq_heading")
            ]
            if len(faq_targets) >= 5:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-faqpage-opportunity", "priority": "medium",
                    "title": f"Add FAQ block + FAQPage JSON-LD to {len(faq_targets)} commerce page(s)",
                    "rationale": (
                        "Studio-supplies product pages each ship a 10-question FAQ rendered as both "
                        "an accordion in the body AND FAQPage JSON-LD. This produces rich-snippet "
                        "expandable Q&A under the SERP listing — measurable CTR lift on commercial "
                        "queries. The agent can generate Qs from product specs/category."
                    ),
                    "expected_impact": {"metric": "gsc.avg_ctr", "horizon_weeks": 8},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "Add a `faqs: [{question, answer}]` field on each product/category. "
                            "Render in body as <details><summary>Q</summary><p>A</p></details>. Also "
                            "emit FAQPage JSON-LD with mainEntity[*].@type=Question, .name=Q, "
                            ".acceptedAnswer.@type=Answer, .acceptedAnswer.text=A. Aim for 5-10 Q&As."
                        ),
                    },
                    "sample_urls": _samples(faq_targets, 5),
                    "implemented": False,
                })

        # Image alt text rate
        if _budget():
            offenders = [
                p for p in ok_pages
                if int(p.get("img_count", 0) or 0) >= 4
                and (int(p.get("img_with_alt", 0) or 0) / max(int(p.get("img_count", 1)), 1)) < 0.85
            ]
            if len(offenders) >= 3:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-images-alt-rate", "priority": "medium",
                    "title": f"Lift image alt-text coverage on {len(offenders)} page(s) (target ≥90%)",
                    "rationale": (
                        "Image alt is required for accessibility AND helps Google Images rank — a "
                        "real organic-discovery channel. Studio-supplies hits ~95% alt coverage on "
                        "product pages with descriptive captions ('Apple Watch Ultra titanium ocean band') "
                        "rather than filenames."
                    ),
                    "expected_impact": {"metric": "google_images_referrals", "horizon_weeks": 6},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "For each <img>, emit a descriptive alt that includes the product/topic "
                            "name + attribute (color/size/era). Empty alt='' is acceptable for purely "
                            "decorative images; missing alt is not. Auto-generate from `${product.name} "
                            "${product.color || ''}` in the SSR template."
                        ),
                    },
                    "sample_urls": _samples(offenders, 5),
                    "implemented": False,
                })

        # Thin content
        if _budget():
            thin = [
                p for p in ok_pages
                if p.get("type") not in ("home", "homepage")
                and 0 < int(p.get("word_count", 0) or 0) < 250
            ]
            if len(thin) >= 3:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-thin-content", "priority": "medium",
                    "title": f"Expand thin-content pages — {len(thin)} page(s) under 250 words",
                    "rationale": (
                        "Thin pages rarely rank for competitive queries. Studio-supplies product "
                        "pages run ~1500 words (description + FAQ + cross-references); category "
                        "pages run 500+. Add a buying-guide intro paragraph, a 5-Q FAQ, and 3-5 "
                        "cross-links to related products."
                    ),
                    "expected_impact": {"metric": "gsc.avg_position", "horizon_weeks": 8},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "implementation_outline": {
                        "approach": (
                            "On each flagged URL, add: (1) a 100-word intro lede, (2) a 5-question "
                            "FAQ block (also wired to FAQPage JSON-LD), (3) 3 cross-links to "
                            "related items in the same category."
                        ),
                    },
                    "sample_urls": _samples(thin, 5),
                    "implemented": False,
                })

        # Internal linking density
        if _budget():
            sparse = [
                p for p in ok_pages
                if int(p.get("internal_links", 0) or 0) < 10
                and p.get("type") not in ("home", "homepage")
            ]
            if len(sparse) >= 3:
                rid = next_id()
                recs.append({
                    "id": rid, "type": "onpage-low-internal-linking", "priority": "low",
                    "title": f"Add internal links to {len(sparse)} sparsely-linked page(s)",
                    "rationale": (
                        "Pages with under 10 internal links pass minimal authority and are harder "
                        "for Google to discover. Studio-supplies product pages average 50+ internal "
                        "links via the global nav, breadcrumbs, related-products carousel, and footer."
                    ),
                    "expected_impact": {"metric": "indexed_pages", "horizon_weeks": 6},
                    "data_refs": ["data/pages-by-type.jsonl"],
                    "sample_urls": _samples(sparse, 5),
                    "implemented": False,
                })

    # ── Site-wide rules (require site-signals.json) ───────────────────
    robots = (signals.get("robots") or {}) if isinstance(signals, dict) else {}
    bots = robots.get("bots") or {}

    # robots.txt missing AI-crawler explicit allows
    if _budget() and bots:
        ai_crit = ["GPTBot", "ClaudeBot", "PerplexityBot", "OAI-SearchBot", "Bingbot"]
        missing_crit = [b for b in ai_crit if not bots.get(b, {}).get("declared")]
        if len(missing_crit) >= 2:
            rid = next_id()
            recs.append({
                "id": rid, "type": "robots-no-ai-allow", "priority": "high",
                "title": f"Add explicit robots.txt allow for {len(missing_crit)} AI crawler(s)",
                "rationale": (
                    "AI search engines (ChatGPT, Claude, Perplexity, Google AI Overviews, Bing "
                    "Copilot) are a fast-growing referral channel. Without explicit User-agent "
                    "blocks, some bots fall back to conservative crawl patterns or skip the site "
                    "entirely. Studio-supplies emits a per-bot Allow:/ block for "
                    "GPTBot, OAI-SearchBot, ChatGPT-User, PerplexityBot, Perplexity-User, "
                    "ClaudeBot, anthropic-ai, claude-web, Google-Extended, Applebot-Extended, "
                    "Bingbot, Amazonbot, Bytespider, Meta-ExternalAgent. "
                    f"Currently missing: {', '.join(missing_crit)}."
                ),
                "expected_impact": {"metric": "ai_referral_traffic", "horizon_weeks": 12},
                "data_refs": ["data/site-signals.json"],
                "implementation_outline": {
                    "approach": (
                        "Append to robots.txt a per-bot block for each AI crawler:\n"
                        "  User-agent: GPTBot\n  Disallow: /admin\n  Disallow: /cart\n  Allow: /\n\n"
                        "Repeat for ClaudeBot, PerplexityBot, OAI-SearchBot, Bingbot. Each block "
                        "is independent (User-agent groups don't inherit from *)."
                    ),
                },
                "missing_bots": missing_crit,
                "implemented": False,
            })

    # robots.txt missing Sitemap directive
    if _budget() and robots and robots.get("sitemap_directive") is False:
        rid = next_id()
        recs.append({
            "id": rid, "type": "robots-no-sitemap", "priority": "high",
            "title": "Add `Sitemap:` directive to robots.txt",
            "rationale": (
                "Without a Sitemap: line in robots.txt, crawlers must discover the sitemap by "
                "convention (/sitemap.xml). Explicit declaration improves discovery latency and "
                "ensures named bots (which don't inherit from *) see it."
            ),
            "expected_impact": {"metric": "indexed_pages", "horizon_weeks": 4},
            "data_refs": ["data/site-signals.json"],
            "implementation_outline": {
                "approach": "Append `Sitemap: https://<domain>/sitemap.xml` once at the bottom of robots.txt.",
            },
            "implemented": False,
        })

    # Homepage missing WebSite or Organization JSON-LD
    if _budget():
        home = (signals.get("homepage") or {}) if isinstance(signals, dict) else {}
        types = set(home.get("jsonld_types") or [])
        missing = [t for t in ("WebSite", "Organization") if t not in types]
        if missing:
            rid = next_id()
            recs.append({
                "id": rid, "type": "home-jsonld-missing", "priority": "high",
                "title": f"Add {' + '.join(missing)} JSON-LD to homepage",
                "rationale": (
                    "WebSite + Organization on the homepage is what Google uses to build the "
                    "knowledge-panel sitelinks search box and the brand sitelinks shown for "
                    "navigational queries. Studio-supplies emits both at the root."
                ),
                "expected_impact": {"metric": "branded_serp_features", "horizon_weeks": 8},
                "data_refs": ["data/site-signals.json"],
                "implementation_outline": {
                    "approach": (
                        "Add to homepage <head>:\n"
                        "  <script type=\"application/ld+json\">{\n"
                        "    \"@context\":\"https://schema.org\",\n"
                        "    \"@type\":\"WebSite\",\n"
                        "    \"name\":\"<Site Name>\",\n"
                        "    \"url\":\"https://<domain>/\",\n"
                        "    \"potentialAction\":{\"@type\":\"SearchAction\",\n"
                        "      \"target\":\"https://<domain>/search?q={search_term_string}\",\n"
                        "      \"query-input\":\"required name=search_term_string\"}\n"
                        "  }</script>\n\n"
                        "And an Organization block with name, url, logo, sameAs (social profiles)."
                    ),
                },
                "missing": missing,
                "implemented": False,
            })


def build_recommendations(cfg, run_dir: Path, snap: dict,
                            handled_keys: Optional[set] = None) -> tuple[list[dict], list[dict]]:
    """Return (recommendations, declared_goals).

    handled_keys: optional set of canonical rec keys (per _canonical_rec_key)
    that have already been shipped or skipped in prior runs. Recs whose
    key is in this set are dropped before being added to the output.
    Caller usually loads it via _load_handled_rec_keys; pass None to
    disable the dedupe filter (e.g. for testing).
    """
    handled_keys = handled_keys or set()
    skipped_already_handled = 0
    def _keep(rec: dict) -> bool:
        nonlocal skipped_already_handled
        key = _canonical_rec_key(rec)
        if key and key in handled_keys:
            skipped_already_handled += 1
            return False
        return True
    data = run_dir / "data"
    recs: list[dict] = []
    goals: list[dict] = []
    rec_id_counter = 1
    def next_id():
        nonlocal rec_id_counter
        rid = f"rec-{rec_id_counter:03d}"; rec_id_counter += 1; return rid

    max_recs = cfg.get("analyzer", {}).get("max_recs_per_run", 12)
    repo_routes = _load_repo_routes(run_dir)

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

    # ---- Ads: paid-organic gap (queries paid wins, organic loses) ----
    if len(recs) < max_recs:
        paid_org = _load(data / "ads-paid-vs-organic.json").get("results", [])
        # Index our query positions for lookup
        q_pos = snap.get("gsc_90d", {}).get("query_position", {})
        # Score: ads_clicks high, organic_clicks low, organic position > 10
        gap_candidates = []
        for row in paid_org:
            # SDK + REST shapes both have nested metrics + dimensions
            term = ""
            try:
                term = (
                    row.get("paid_organic_search_term_view", {}).get("search_term")
                    or row.get("paidOrganicSearchTermView", {}).get("searchTerm")
                    or ""
                )
            except Exception:
                continue
            if not term:
                continue
            metrics = row.get("metrics", {}) or {}
            ad_clicks = int(metrics.get("clicks", 0) or 0)
            org_clicks = int(metrics.get("organic_clicks", metrics.get("organicClicks", 0)) or 0)
            ad_impr = int(metrics.get("impressions", 0) or 0)
            term_l = term.strip().lower()
            organic_pos = q_pos.get(term_l, {}).get("position", 100.0)
            if ad_clicks >= 3 and organic_pos > 10 and org_clicks < ad_clicks:
                gap_candidates.append({
                    "term": term, "ad_clicks": ad_clicks, "ad_impressions": ad_impr,
                    "organic_clicks": org_clicks, "organic_position": organic_pos,
                })
        gap_candidates.sort(key=lambda r: -r["ad_clicks"])
        for cand in gap_candidates[:3]:
            if len(recs) >= max_recs: break
            rid = next_id()
            recs.append({
                "id": rid,
                "type": "paid-organic-gap",
                "priority": "high",
                "title": f"Paid wins, organic loses for \"{cand['term']}\" — close the gap",
                "rationale": (
                    f"Ads pulled {cand['ad_clicks']} clicks ({cand['ad_impressions']} impr) for "
                    f"\"{cand['term']}\" in 90d while organic only got {cand['organic_clicks']} "
                    f"clicks at pos {cand['organic_position']:.1f}. We're paying for traffic we "
                    f"could rank for organically."
                ),
                "expected_impact": {
                    "metric": f"gsc.query_position.{cand['term']}",
                    "baseline": cand["organic_position"],
                    "target": 10.0,
                    "horizon_weeks": 8,
                },
                "implementation_outline": {
                    "files": _match_files_by_type(repo_routes, "page") or
                             [r["file"] for r in repo_routes[:3]],
                    "notes": (
                        f"Build/optimize an organic landing page targeting \"{cand['term']}\". "
                        f"Use the ad copy from ads-ad-copy.json that converts for this term as "
                        f"the page title/H1 starting point."
                    ),
                },
                "data_refs": ["data/ads-paid-vs-organic.json", "data/ads-ad-copy.json"],
                "implemented": False,
            })

    # ---- Ads: ad-copy headline winner (port to organic) ----
    if len(recs) < max_recs:
        ad_copy = _load(data / "ads-ad-copy.json").get("results", [])
        # Find top-CTR ads
        top_ads = []
        for row in ad_copy:
            metrics = row.get("metrics", {}) or {}
            ctr = float(metrics.get("ctr", 0) or 0)
            if ctr <= 0.05:  # only meaningfully good CTRs
                continue
            ad = (row.get("ad_group_ad", {}) or row.get("adGroupAd", {})).get("ad", {}) or {}
            rsa = ad.get("responsive_search_ad") or ad.get("responsiveSearchAd") or {}
            heads = rsa.get("headlines", [])
            head_texts = []
            for h in heads:
                if isinstance(h, dict):
                    head_texts.append(h.get("text", ""))
                else:
                    head_texts.append(str(h))
            if head_texts:
                top_ads.append({"ctr": ctr, "headlines": head_texts[:5]})
        top_ads.sort(key=lambda a: -a["ctr"])
        if top_ads:
            best = top_ads[0]
            if len(recs) < max_recs:
                rid = next_id()
                head_str = " | ".join(h for h in best["headlines"] if h)[:160]
                recs.append({
                    "id": rid,
                    "type": "ad-copy-headline-winner",
                    "priority": "medium",
                    "title": f"Port top-CTR ad headline to organic title/H1 ({best['ctr']*100:.1f}% CTR)",
                    "rationale": (
                        f"Ad copy with CTR {best['ctr']*100:.1f}% uses headlines: {head_str}. "
                        f"These phrases are proven click-magnets — port them into <title>/<h1> on "
                        f"the matching organic page."
                    ),
                    "implementation_outline": {
                        "files": [r["file"] for r in repo_routes[:5] if "title" not in r.get("emits", [])],
                        "notes": "Identify the organic page that ranks for the same query the ad targets, then update its <title> and H1 to mirror the high-CTR ad headline pattern.",
                    },
                    "data_refs": ["data/ads-ad-copy.json"],
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

    # ---- Article SEO boost (when articles-inventory.json exists) ----
    # The collector's pull_site_articles step joins every published article's
    # URL with GSC pages-90d and tags each one with boost_signals. Surface
    # the most actionable ones as recs. Capped at 5 per run so they don't
    # crowd out keyword + CTR work.
    inv = _load(data / "articles-inventory.json")
    if inv and inv.get("articles"):
        articles = inv["articles"]
        # Bucket by signal, sort each bucket by impact heuristic
        snippet_candidates = sorted(
            [a for a in articles if "high-impressions-low-ctr" in (a.get("boost_signals") or [])],
            key=lambda a: -a["gsc"]["impressions"],
        )
        title_fix_candidates = sorted(
            [a for a in articles if "high-position-zero-clicks" in (a.get("boost_signals") or [])],
            key=lambda a: a["gsc"]["position"],
        )
        orphan_candidates = sorted(
            [a for a in articles if "orphan-no-gsc-data" in (a.get("boost_signals") or [])],
            key=lambda a: a["title"],
        )
        article_recs_added = 0
        ARTICLE_RECS_MAX = 5

        for a in snippet_candidates[:2]:
            if article_recs_added >= ARTICLE_RECS_MAX:
                break
            rid = next_id()
            recs.append({
                "id": rid,
                "type": "article-snippet-rewrite",
                "priority": "high",
                "title": f"Rewrite SERP snippet for \"{a['title'][:60]}\" — {a['gsc']['impressions']} impr, CTR {a['gsc']['ctr']*100:.2f}%",
                "rationale": (
                    f"Article at {a['url']} gets {a['gsc']['impressions']} impressions/90d at "
                    f"avg position {a['gsc']['position']:.1f} but only {a['gsc']['clicks']} clicks "
                    f"(CTR {a['gsc']['ctr']*100:.2f}%). The page is being shown but not clicked — "
                    f"the SERP title and meta-description aren't earning the click. Rewrite both "
                    f"with a more compelling hook + benefit framing."
                ),
                "expected_impact": {
                    "metric": f"gsc.url_ctr.{a['url']}",
                    "baseline": float(a["gsc"]["ctr"]),
                    "target": 0.04, "horizon_weeks": 4,
                },
                "data_refs": ["data/articles-inventory.json", "data/gsc-pages-90d.json"],
                "url": a["url"],
                "slug": a["slug"],
                "implemented": False,
            })
            article_recs_added += 1

        for a in title_fix_candidates[:2]:
            if article_recs_added >= ARTICLE_RECS_MAX:
                break
            rid = next_id()
            recs.append({
                "id": rid,
                "type": "article-title-fix",
                "priority": "high",
                "title": f"Fix title for \"{a['title'][:60]}\" — pos {a['gsc']['position']:.1f}, 0 clicks",
                "rationale": (
                    f"Article at {a['url']} ranks at avg position {a['gsc']['position']:.1f} "
                    f"with {a['gsc']['impressions']} impressions but ZERO clicks. Top-10 "
                    f"placement should produce SOME traffic — the title is failing to match "
                    f"intent. Audit the title vs the queries it's appearing for and rewrite."
                ),
                "expected_impact": {
                    "metric": f"gsc.url_clicks.{a['url']}",
                    "baseline": 0, "target": 5, "horizon_weeks": 4,
                },
                "data_refs": ["data/articles-inventory.json", "data/gsc-pages-90d.json"],
                "url": a["url"],
                "slug": a["slug"],
                "implemented": False,
            })
            article_recs_added += 1

        # 1 orphan rec — articles with no GSC data at all need internal-link boost
        if orphan_candidates and article_recs_added < ARTICLE_RECS_MAX:
            sample = orphan_candidates[:5]
            rid = next_id()
            recs.append({
                "id": rid,
                "type": "article-orphan-boost",
                "priority": "medium",
                "title": f"Boost {len(orphan_candidates)} orphan article(s) with internal links + sitemap",
                "rationale": (
                    f"{len(orphan_candidates)} published articles have no GSC data — Google "
                    f"likely hasn't indexed them, or they have ZERO impressions. Most common "
                    f"cause: no internal links pointing in + missing from sitemap.xml. "
                    f"Sample slugs: " + ", ".join(s["slug"] for s in sample[:5])
                ),
                "expected_impact": {
                    "metric": "articles_with_gsc_impressions",
                    "baseline": len(articles) - len(orphan_candidates),
                    "target": len(articles),
                    "horizon_weeks": 8,
                },
                "data_refs": ["data/articles-inventory.json"],
                "orphan_urls": [s["url"] for s in sample[:10]],
                "implemented": False,
            })
            article_recs_added += 1

    # ---- On-page audit (studio-supplies playbook) ----
    # Rules read pages-by-type.jsonl + site-signals.json (written by the
    # data-collector). Modeled on the high-converting studio-supplies.com
    # SEO patterns: comprehensive head metadata, JSON-LD per page type,
    # FAQ blocks, robots.txt with explicit AI-crawler allows, etc.
    if len(recs) < max_recs:
        try:
            _add_onpage_recs(data, recs, next_id, max_recs)
        except Exception as e:
            print(f"  [onpage] rule eval failed: {e}", file=sys.stderr)

    # Filter out any rec whose canonical key matches a previously
    # shipped/skipped rec. This prevents the analyzer from re-proposing
    # the same recommendations every cron cycle (every 3h) — without it,
    # the implementer keeps seeing "build keyword page for cube steak
    # recipes" even after the first run shipped it; claude correctly
    # marks it implemented_via=pre-existing but the operator's inbox
    # fills with stale-feeling proposals.
    if handled_keys:
        before = len(recs)
        recs = [r for r in recs if _canonical_rec_key(r) not in handled_keys]
        skipped_already_handled = before - len(recs)
        if skipped_already_handled:
            print(f"  ✓ filtered out {skipped_already_handled} rec(s) already handled "
                  f"in prior runs (shipped/implemented/skipped)", file=sys.stderr)

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
        sys.path.insert(0, str(_REPO_ROOT / "agents" / "progressive-improvement-agent"))
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
    p.add_argument("--agent-id", default=None,
                   help="Framework orchestrator agent id. Routes run-dir reads/writes "
                        "through Azure under agents/<agent-id>/runs/<run-ts>/. Required "
                        "with --run-ts when using the new Azure-backed flow.")
    args = p.parse_args()

    cfg = load_config_from_env()

    # Azure-backed mode: orchestrator pins (--agent-id, --run-ts).
    if args.agent_id and args.run_ts:
        _main_azure(cfg, args.agent_id, args.run_ts)
        return
    if args.agent_id and not args.run_ts:
        raise SystemExit("--agent-id requires --run-ts")

    # Legacy local-FS mode.
    if args.run_ts:
        run_dir = cfg.run_dir_for_ts(args.run_ts)
    else:
        run_dir = cfg.latest_run_dir()
        if run_dir is None:
            raise SystemExit(f"No runs for site={cfg.site_id}. Run seo-data-collector first.")
    if not run_dir.is_dir():
        raise SystemExit(f"Run dir not found: {run_dir}")
    run_ts = run_dir.name

    print(f"[analyzer] site={cfg.site_id} run_ts={run_ts} (legacy local-fs)", file=sys.stderr)
    _run_analyzer(cfg, run_dir, run_ts)


def _main_azure(cfg, agent_id: str, run_ts: str) -> None:
    """Azure-backed mode: materialize the run dir from Azure into a tempdir
    structured as <tmp>/<site>/<run_ts>/ so analyzer.find_prior_snapshot()
    keeps working. Sync back on exit."""
    from framework.core.run_dir import RunDir
    rd = RunDir(agent_id, run_ts, site=cfg.site_id)
    print(f"[analyzer] site={cfg.site_id} run_ts={run_ts} agent_id={agent_id} (azure)",
          file=sys.stderr)
    with rd.tempdir(structured_parent=True, skip_globs=["data/page-cache/*"]) as td:
        # td = <tmp>/<site>/<run_ts>/   so site_runs_dir = <tmp>/<site>
        # and runs_root = <tmp>. Patch cfg so cfg.site_runs_dir resolves to td.parent.
        cfg["runs_root"] = str(td.parent.parent)
        _run_analyzer(cfg, td, run_ts)
    print(rd.handle)


def _run_analyzer(cfg, run_dir, run_ts: str) -> None:
    """The analyzer body — pathlib-based, used by both legacy and Azure modes."""
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
            # Mirror each scored metric into the canonical goals/active.json
            # progress_history so the dashboard's time-series + UI status pills
            # reflect the latest measurement (without this the metric in the
            # registry stays at baseline forever).
            try:
                from framework.core import goals as _goals_mod
                _agent_id = (cfg.get("reporter", {}) or {}).get(
                    "dashboard", {}).get("agent_id") or f"{cfg.site_id}-seo-opportunity-agent"
                for sg in scored.get("goals", []):
                    if sg.get("current") is None:
                        continue
                    try:
                        _goals_mod.record_goal_progress(
                            _agent_id, sg["id"], float(sg["current"]),
                            run_ts=run_ts,
                            note=f"scored from snapshot ({sg.get('status','')})",
                        )
                    except Exception as _e:
                        print(f"  [warn] record_goal_progress({sg.get('id')}): {_e}",
                              file=sys.stderr)
            except Exception as e:
                print(f"  [warn] progress_history sync failed: {e}", file=sys.stderr)

    # 4. Build recommendations + declare new goals
    # Load handled-rec keys from prior runs so we don't re-propose recs
    # that have already been shipped, implemented, or explicitly skipped
    # by the operator. Disable with SEO_DISABLE_HANDLED_DEDUPE=1.
    handled_keys: set = set()
    if os.environ.get("SEO_DISABLE_HANDLED_DEDUPE") != "1":
        try:
            handled_keys = _load_handled_rec_keys(cfg, run_ts)
            if handled_keys:
                print(f"  ✓ loaded {len(handled_keys)} handled-rec keys from prior runs",
                      file=sys.stderr)
        except Exception as e:
            print(f"  [warn] handled-rec dedupe load failed: {e}", file=sys.stderr)
    recs, declared_goals = build_recommendations(cfg, run_dir, snap,
                                                  handled_keys=handled_keys)
    # Repo routes are useful in the LLM-audit post-processing below too
    repo_routes = _load_repo_routes(run_dir)

    # 4b. LLM-driven adaptive audit pass — flags evolving SEO opportunities
    # the deterministic pass can't see (CWV, schema, EEAT, AI search,
    # mobile-first specifics). Disabled with SEO_DISABLE_LLM_AUDIT=1.
    # NOTE: max_recs is local to build_recommendations() — re-load here
    # for the LLM-audit branch's own dedupe + budget logic. Without this,
    # the audit fails with "name 'max_recs' is not defined" mid-run and
    # produces 0 final recs.
    max_recs = int(cfg.get("analyzer", {}).get("max_recs_per_run", 12))
    if os.environ.get("SEO_DISABLE_LLM_AUDIT") != "1":
        try:
            from llm_audit import (
                run_llm_audit,
                issues_to_recommendations,
            )
            ai_chat = _build_ai_chat_callable(cfg)
            if ai_chat is not None:
                # Pull the page records.
                # Source preference order:
                #  1. data/pages-by-type.jsonl  — page-type inventory crawl
                #     (driven by site.yaml page_inventory). Each row carries
                #     a `type` field that activates page-type-specific LLM
                #     checks (recipe-*, product-*, h2h-*, ...).
                #  2. data/pages.jsonl          — legacy: cached from a prior
                #     on-demand crawl.
                #  3. on-demand BFS crawl       — fallback, top-10 GSC pages.
                #
                # The cap is `analyzer.max_llm_audit_pages` (default 30) to
                # bound LLM cost.
                max_audit_pages = int(
                    cfg.get("analyzer", {}).get("max_llm_audit_pages", 30)
                )
                pages_by_type_path = run_dir / "data" / "pages-by-type.jsonl"
                pages_path = run_dir / "data" / "pages.jsonl"
                pages: list[dict] = []
                if pages_by_type_path.is_file():
                    for line in pages_by_type_path.read_text().splitlines():
                        if not line.strip():
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        # Skip non-2xx records; they have no body to audit
                        if rec.get("status") and not (
                            200 <= rec["status"] < 300 or rec["status"] == 304
                        ):
                            continue
                        pages.append(rec)
                if not pages and pages_path.is_file():
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
                # Cap to bound cost (default 30, configurable per-site)
                pages = pages[:max_audit_pages]
                if pages:
                    print(f"  → LLM audit: {len(pages)} pages", file=sys.stderr)
                    # Adaptive context: load past goal-changes for this site
                    # so the LLM can learn from what's been implemented and
                    # which categories are/aren't moving the metric.
                    adaptive_ctx = ""
                    active_goals: list[dict] = []
                    try:
                        from framework.core import goal_changes as _gc
                        from framework.core import goals as _goals_mod
                        _src = (cfg.get("reporter", {}) or {}).get(
                            "dashboard", {}).get("agent_id") or f"{cfg.site_id}-seo-opportunity-agent"
                        adaptive_ctx = _gc.adaptive_context_block(
                            _src, site=cfg.site_id, horizon=30,
                        )
                        active_goals = _goals_mod.read_active_goals(_src)
                        if adaptive_ctx:
                            print(f"  → adaptive context: {len(adaptive_ctx)} chars from past changes",
                                  file=sys.stderr)
                        if active_goals:
                            print(f"  → injecting {len(active_goals)} active goals",
                                  file=sys.stderr)
                    except Exception as e:
                        print(f"  → adaptive context unavailable: {e}", file=sys.stderr)
                    issues = run_llm_audit(
                        pages=pages,
                        site_label=cfg.site_id,
                        primary_objective=cfg.get("analyzer", {}).get(
                            "primary_objective", "top5-rank"),
                        ai_chat_callable=ai_chat,
                        batch_size=4,
                        adaptive_context=adaptive_ctx,
                        active_goals=active_goals,
                    )
                    print(f"  → LLM audit found {len(issues)} issues", file=sys.stderr)
                    next_id_count = {"i": len(recs)}
                    def _next_id():
                        next_id_count["i"] += 1
                        return f"rec-{next_id_count['i']:03d}"
                    llm_recs = issues_to_recommendations(issues, _next_id)
                    # Wire repo-routes into implementation_outline.files so
                    # the implementer + the human reading the email get a
                    # concrete file:line target instead of guessing.
                    if repo_routes:
                        for r in llm_recs:
                            url = (r.get("data_refs") or [""])[0]
                            files = _files_for_url(repo_routes, url)
                            if not files:
                                # Fall back to page-type heuristic from llm_check_id
                                cid = r.get("llm_check_id", "")
                                pt = cid.split("-", 1)[0] if "-" in cid else ""
                                files = _match_files_by_type(repo_routes, pt)
                            if files:
                                outline = r.setdefault("implementation_outline", {})
                                outline.setdefault("files", []).extend(files)
                    # De-dupe by (url, llm_check_id) to avoid stomping.
                    # Reserve up to half the total budget for LLM audit recs.
                    max_llm_recs = max(max_recs // 2, max_recs - len(recs))
                    seen = {(r.get("data_refs",[None])[0], r.get("llm_check_id"))
                            for r in recs if r.get("llm_check_id")}
                    llm_added = 0
                    for r in llm_recs:
                        if llm_added >= max_llm_recs:
                            break
                        key = (r.get("data_refs",[None])[0], r.get("llm_check_id"))
                        if key in seen:
                            continue
                        recs.append(r)
                        seen.add(key)
                        llm_added += 1
                    # Final hard cap in case strategic recs were already at budget
                    recs = recs[:max_recs]
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
        f"{sum(1 for r in recs if r['type'] == 'paid-organic-gap')} paid-organic gaps, "
        f"{sum(1 for r in recs if r['type'] == 'ad-copy-headline-winner')} ad-copy ports, "
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
