#!/usr/bin/env python3
"""Digest Rollup Agent — one email every 3 hours, everything in it.

Replaces the per-agent flood of confirmation/proposal/completion emails.
Each individual agent's `send_via_msmtp` call is gated by DIGEST_ONLY=1
(default) and silently dropped into `digest-queue/<ts>-<hash>.json`. This
agent runs every 3 hours, drains that queue, walks recent run state for
anything the queue missed, renders a single grouped HTML email, and
ships it with bypass_digest=True so it actually leaves the building.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

AGENT_ID = "digest-rollup-agent"
WINDOW_HOURS = 3
OWNER = "mperry@northernsoftwareconsulting.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_to_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Tolerate trailing Z and missing tz
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def _agent_site(agent_id: str) -> str:
    for prefix in ("specpicks-", "aisleprompt-", "reusable-agents-"):
        if agent_id.startswith(prefix):
            return prefix.rstrip("-")
    return ""


# ──────────────────────────────────────────────────────────────────────
# Queue scanners — read the structured event sources Azure already has
# ──────────────────────────────────────────────────────────────────────


def _drain_digest_queue(s, cutoff: datetime) -> tuple[list[dict], list[str]]:
    """Read every digest-queue/<ts>-<hash>.json. Returns (entries-in-window,
    keys-to-archive-after-send). Older entries are kept (in case the
    digest run failed and the next attempt picks them up); we trim to the
    window so old emails don't repeat forever."""
    keep: list[dict] = []
    archive_after: list[str] = []
    try:
        keys = list(s.list_prefix("digest-queue/"))
    except Exception:
        return [], []
    for k in keys:
        if not k.endswith(".json"):
            continue
        try:
            d = s.read_json(k)
        except Exception:
            continue
        if not d:
            continue
        ts = _iso_to_dt(d.get("ts", ""))
        if ts is None or ts < cutoff:
            # Keep stale entries indexed for cleanup, but skip the body
            archive_after.append(k)
            continue
        keep.append({"_key": k, **d})
        archive_after.append(k)
    return keep, archive_after


def _recent_shipped(s, cutoff: datetime) -> list[dict]:
    """Walk every recommendations.json across all agents; return rec
    items where shipped_at OR implemented_at falls in the window."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for k in s.list_prefix("agents/"):
        if not k.endswith("/recommendations.json"):
            continue
        parts = k.split("/")
        if len(parts) < 5 or parts[2] != "runs":
            continue
        agent = parts[1]
        run_ts = parts[3]
        if "rundir-" in run_ts:
            continue
        try:
            d = s.read_json(k) or {}
        except Exception:
            continue
        for r in d.get("recommendations", []) or []:
            rid = r.get("id")
            if not rid:
                continue
            ship_ts = _iso_to_dt(r.get("shipped_at") or "")
            impl_ts = _iso_to_dt(r.get("implemented_at") or "")
            best = max((t for t in (ship_ts, impl_ts) if t), default=None)
            if best is None or best < cutoff:
                continue
            key = (agent, rid, run_ts)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "agent": agent,
                "site": _agent_site(agent),
                "run_ts": run_ts,
                "rec_id": rid,
                "title": (r.get("title") or "")[:140],
                "type": r.get("type") or "",
                "shipped": bool(r.get("shipped")),
                "implemented": bool(r.get("implemented")),
                "shipped_at": r.get("shipped_at") or "",
                "implemented_at": r.get("implemented_at") or "",
                "public_url": r.get("public_url") or "",
                "skipped": bool(r.get("skipped")),
            })
    return out


def _recent_runs(s, cutoff: datetime) -> list[dict]:
    """Walk every agent's run-index.json for recent run entries.

    Filters out agent-doctor runs that touched nothing — the doctor runs
    every 15 minutes; a clean fleet means most runs report 0 investigated
    / 0 fixed / 0 escalated, and they would otherwise dominate the digest's
    "All runs" section. Drop them unless at least one of those three
    counters is ≥1, regardless of run status."""
    out: list[dict] = []
    try:
        for k in s.list_prefix("agents/"):
            if not k.endswith("/run-index.json"):
                continue
            parts = k.split("/")
            if len(parts) < 3:
                continue
            agent = parts[1]
            try:
                idx = s.read_json(k) or {}
            except Exception:
                continue
            for entry in (idx.get("recent") or [])[:30]:
                ended = _iso_to_dt(entry.get("ended_at") or entry.get("started_at") or "")
                if ended is None or ended < cutoff:
                    continue
                metrics = entry.get("metrics") or {}
                if agent == "agent-doctor":
                    activity = (
                        int(metrics.get("investigated") or 0)
                        + int(metrics.get("fixed") or 0)
                        + int(metrics.get("escalated") or 0)
                    )
                    if activity <= 0:
                        continue
                out.append({
                    "agent": agent,
                    "site": _agent_site(agent),
                    "run_ts": entry.get("run_ts", ""),
                    "status": entry.get("status", ""),
                    "summary": (entry.get("summary") or "")[:200],
                    "ended_at": entry.get("ended_at", ""),
                    "metrics": metrics,
                })
    except Exception:
        pass
    return out


def _auto_queue_pending(s) -> list[dict]:
    """Things still queued waiting for the implementer."""
    out: list[dict] = []
    try:
        for k in s.list_prefix("agents/responder-agent/auto-queue/"):
            if not k.endswith(".json"):
                continue
            try:
                d = s.read_json(k) or {}
            except Exception:
                continue
            out.append({
                "request_id": d.get("request_id", ""),
                "source_agent": d.get("source_agent", ""),
                "site": d.get("site", ""),
                "rec_count": len(d.get("rec_ids") or []),
                "queued_at": d.get("queued_at", ""),
            })
    except Exception:
        pass
    return out


def _doctor_escalations(s, cutoff: datetime) -> list[dict]:
    """Read agent-doctor escalations + fix attempts in window.

    Source of truth is `agents/agent-doctor/fixes-log.jsonl` — every
    investigation appends one line there with target, signature, recipe,
    outcome, the LLM/recipe notes (what was tried), and the raw stderr
    excerpt (what went wrong). Only `outcome == "escalated"` rows surface
    in the digest's escalations block; the rest are aggregated into the
    KPI row.

    Returns dicts with: ts, target, signature, severity, recipe,
    what_went_wrong, what_we_tried, attempt_n, trigger.
    """
    out: list[dict] = []
    try:
        raw = s.read_text("agents/agent-doctor/fixes-log.jsonl") or ""
    except Exception:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (d.get("outcome") or "").lower() != "escalated":
            continue
        ts = _iso_to_dt(d.get("ts") or "")
        if ts is None or ts < cutoff:
            continue
        notes = (d.get("notes") or "").strip()
        log_excerpt = (d.get("log_excerpt") or "").strip()
        # Pull the most user-actionable lines out of the log excerpt:
        # last non-empty 3 lines tend to be the actual error.
        log_tail = "\n".join(
            [ln for ln in log_excerpt.splitlines() if ln.strip()][-3:]
        )[:600]
        out.append({
            "ts": d.get("ts") or "",
            "target": d.get("target_agent") or "",
            "signature": d.get("error_signature") or "",
            "trigger": d.get("trigger") or "",
            "recipe": d.get("recipe_applied") or "(none)",
            "attempt_n": d.get("attempt_n") or 1,
            "what_went_wrong": log_tail or log_excerpt[:600],
            "what_we_tried": notes[:1200] or "(no recipe matched — escalated for human review)",
            "severity": "high" if d.get("trigger") == "stuck" else "medium",
        })
    # Sort newest first so the most recent escalation is the top card
    out.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return out


# ──────────────────────────────────────────────────────────────────────
# HTML rendering — tile-based layout (visual sibling of the SpecPicks
# article-author proposals email: bright accent badges, a generous KPI
# tile strip, monospace IDs, rounded cards)
# ──────────────────────────────────────────────────────────────────────

_DASHBOARD = "https://agents.happysky-24190067.eastus.azurecontainerapps.io"

# Slate palette — same tokens email_templates.py uses, kept inline so this
# module has no extra import surface.
_INK_900 = "#0f172a"
_INK_700 = "#334155"
_INK_600 = "#475569"
_INK_500 = "#64748b"
_INK_400 = "#94a3b8"
_INK_200 = "#e2e8f0"
_INK_100 = "#f1f5f9"
_INK_50 = "#f8fafc"
_GREEN = "#10b981"
_GREEN_BG = "#ecfdf5"
_GREEN_BORDER = "#6ee7b7"
_GREEN_FG = "#065f46"
_BLUE = "#3b82f6"
_BLUE_BG = "#eff6ff"
_AMBER = "#f59e0b"
_AMBER_BG = "#fef3c7"
_AMBER_FG = "#92400e"
_RED = "#ef4444"
_RED_BG = "#fee2e2"
_RED_BORDER = "#fecaca"
_RED_FG = "#991b1b"
_VIOLET = "#7c3aed"


def _tile(label: str, value: str | int, color: str, *, sub: str = "") -> str:
    """One KPI tile — big colored number, small caps label, optional
    sub-line. Matches the article-author email's stat-block visual."""
    sub_html = (
        f"<div style='font-size:10px;color:{_INK_400};margin-top:4px'>{sub}</div>"
        if sub else ""
    )
    return (
        f"<td style='background:#ffffff;border:1px solid {_INK_200};"
        f"border-radius:8px;padding:14px 12px;text-align:center;width:20%;"
        f"box-shadow:0 1px 0 rgba(15,23,42,0.02)'>"
        f"<div style='font-size:1.7rem;font-weight:700;color:{color};line-height:1'>{value}</div>"
        f"<div style='font-size:.68rem;color:{_INK_500};text-transform:uppercase;"
        f"letter-spacing:.06em;margin-top:6px;font-weight:600'>{label}</div>"
        f"{sub_html}"
        f"</td>"
    )


def _tile_row(tiles: list[str]) -> str:
    """Render KPI tiles in a single <table> row — Gmail-safe (no CSS grid).
    Spacer cells between tiles give consistent gaps without `gap:`."""
    if not tiles:
        return ""
    cells: list[str] = []
    for i, t in enumerate(tiles):
        if i > 0:
            cells.append("<td style='width:10px'></td>")
        cells.append(t)
    return (
        f"<table role='presentation' style='border-collapse:separate;"
        f"border-spacing:0;width:100%;margin:0 0 18px 0'>"
        f"<tr>{''.join(cells)}</tr></table>"
    )


def _agent_badge(agent_id: str) -> str:
    """Color-code by site (green specpicks / blue aisleprompt / violet
    framework / slate other) so the eye can group rows fast."""
    site = _agent_site(agent_id)
    palette = {
        "specpicks":      (_GREEN, "#fff"),
        "aisleprompt":    (_BLUE, "#fff"),
        "reusable-agents": (_VIOLET, "#fff"),
    }
    bg, fg = palette.get(site, (_INK_100, _INK_700))
    label = agent_id.replace(f"{site}-", "") if site else agent_id
    return (
        f"<span style='display:inline-block;background:{bg};color:{fg};"
        f"font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px;"
        f"text-transform:uppercase;letter-spacing:.04em;font-family:-apple-system,sans-serif'>"
        f"{label[:32]}</span>"
    )


def _site_chip(site: str) -> str:
    if not site:
        return ""
    return (
        f"<span style='display:inline-block;background:{_INK_100};color:{_INK_700};"
        f"font-size:10px;font-weight:600;padding:2px 8px;border-radius:3px;"
        f"text-transform:uppercase;letter-spacing:.04em;margin-right:6px'>"
        f"{site}</span>"
    )


def _handoff_metrics(s, cutoff: datetime) -> dict:
    """Read agents/*/handoffs.jsonl across all known agents and roll up
    inter-agent flow stats for the digest header. Read-only, never
    fails the digest if some agents have no log yet.

    Returns:
        {
          "total_out": int,
          "total_in": int,
          "shipped": int, "deferred": int, "rejected": int, "in_progress": int,
          "edges": [{"from": ..., "to": ..., "count": ..., "outcomes": {...}}],
          "stuck": [{"request_id":..., "from":..., "to":..., "work_type":...,
                     "age_hours": ..., "rec_id":...}],
        }
    """
    from collections import defaultdict
    out = {
        "total_out": 0, "total_in": 0,
        "shipped": 0, "deferred": 0, "rejected": 0, "in_progress": 0,
        "edges": [],
        "stuck": [],
    }
    edge_counts: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "outcomes": defaultdict(int)}
    )
    # Track handoffs by request_id so we can detect "no outcome ever logged"
    # → stuck. Indexed by (agent_id, request_id).
    inbound: dict[tuple[str, str], dict] = {}
    outcomes: dict[tuple[str, str], dict] = {}
    try:
        agent_keys = s.list_prefix("agents/")
    except Exception:
        return out
    agent_ids: set[str] = set()
    for k in agent_keys:
        # agents/<id>/...
        parts = k.split("/")
        if len(parts) >= 3:
            agent_ids.add(parts[1])
    for agent_id in agent_ids:
        raw = s.read_text(f"agents/{agent_id}/handoffs.jsonl") or ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts_str = row.get("ts") or row.get("received_at") or row.get("acted_at")
            ts_dt = _iso_to_dt(ts_str) if ts_str else None
            if not ts_dt or ts_dt < cutoff:
                # Still consider stuck-tracking via inbound below
                if row.get("direction") == "in":
                    rid = row.get("request_id")
                    if rid:
                        inbound[(agent_id, rid)] = {**row, "_seen_agent": agent_id}
                if row.get("direction") == "outcome":
                    rid = row.get("request_id")
                    if rid:
                        outcomes[(agent_id, rid)] = row
                continue
            d = row.get("direction")
            if d == "out":
                out["total_out"] += 1
                pair = (row.get("from_agent") or agent_id,
                        row.get("to_agent") or "?")
                edge_counts[pair]["count"] += 1
            elif d == "in":
                out["total_in"] += 1
                rid = row.get("request_id")
                if rid:
                    inbound[(agent_id, rid)] = {**row, "_seen_agent": agent_id}
            elif d == "outcome":
                rid = row.get("request_id")
                if rid:
                    outcomes[(agent_id, rid)] = row
                outcome = (row.get("outcome") or "").lower()
                if outcome in out:
                    out[outcome] += 1
    # Build edges list
    out["edges"] = [
        {
            "from": pair[0], "to": pair[1],
            "count": e["count"], "outcomes": dict(e["outcomes"]),
        }
        for pair, e in sorted(edge_counts.items(), key=lambda kv: -kv[1]["count"])
    ]
    # Detect stuck handoffs — inbound rows older than 24h with no
    # outcome row.
    now = _now()
    stuck_cutoff = now - timedelta(hours=24)
    for (agent_id, rid), row in inbound.items():
        outcome_row = outcomes.get((agent_id, rid))
        if outcome_row:
            # in_progress with no later "shipped"/"deferred" still counts
            # as stuck after 48h
            outc = (outcome_row.get("outcome") or "").lower()
            if outc != "in_progress":
                continue
            ts = _iso_to_dt(outcome_row.get("acted_at"))
            stuck_cutoff_ip = now - timedelta(hours=48)
            if ts and ts < stuck_cutoff_ip:
                out["stuck"].append({
                    "request_id": rid,
                    "to": agent_id,
                    "from": outcome_row.get("from_agent") or "?",
                    "work_type": outcome_row.get("work_type") or "?",
                    "age_hours": int((now - ts).total_seconds() / 3600)
                                  if ts else None,
                    "stuck_state": "in_progress-too-long",
                })
            continue
        ts = _iso_to_dt(row.get("received_at") or row.get("ts"))
        if ts and ts < stuck_cutoff:
            out["stuck"].append({
                "request_id": rid,
                "to": agent_id,
                "from": row.get("from_agent") or "?",
                "work_type": row.get("work_type") or "?",
                "rec_id": row.get("rec_id") or "",
                "age_hours": int((now - ts).total_seconds() / 3600),
                "stuck_state": "no-outcome",
            })
    return out


def _render_handoff_block(metrics: dict) -> str:
    """Render the handoff section. Skipped when nothing happened."""
    if not metrics or metrics.get("total_out", 0) == 0:
        return ""
    parts: list[str] = [_section_h2("Inter-agent handoffs", emoji="🔀")]
    stuck_n = len(metrics.get("stuck") or [])
    parts.append(_tile_row([
        _tile("Sent", metrics["total_out"], _BLUE),
        _tile("Shipped", metrics.get("shipped", 0), _GREEN),
        _tile("In progress", metrics.get("in_progress", 0), _INK_500),
        _tile("Deferred", metrics.get("deferred", 0), _AMBER),
        _tile("Stuck", stuck_n, _RED if stuck_n else _INK_400),
    ]))
    if metrics["edges"]:
        rows = []
        for e in metrics["edges"][:10]:
            outcomes_str = " · ".join(f"{k}={v}" for k, v in e["outcomes"].items()) or "—"
            rows.append(
                f'<tr>'
                f'<td style="padding:8px;border:1px solid {_INK_200};font-size:12px">'
                f'{_agent_badge(e["from"])} <span style="color:{_INK_400}">→</span> '
                f'{_agent_badge(e["to"])}</td>'
                f'<td style="padding:8px;border:1px solid {_INK_200};text-align:right;'
                f'font-weight:700;color:{_INK_900}">{e["count"]}</td>'
                f'<td style="padding:8px;border:1px solid {_INK_200};'
                f'font-size:11px;color:{_INK_600}">{outcomes_str}</td></tr>'
            )
        parts.append(
            f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 12px 0">'
            f'<thead><tr style="background:{_INK_50}">'
            f'<th style="text-align:left;padding:8px;border:1px solid {_INK_200};color:{_INK_700};font-size:12px">Flow</th>'
            f'<th style="text-align:right;padding:8px;border:1px solid {_INK_200};color:{_INK_700};font-size:12px">Count</th>'
            f'<th style="text-align:left;padding:8px;border:1px solid {_INK_200};color:{_INK_700};font-size:12px">Outcomes</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        )
    stuck = metrics.get("stuck") or []
    if stuck:
        items = "".join(
            f'<li style="margin-bottom:4px">'
            f'{_agent_badge(sk.get("from","?"))} <span style="color:{_INK_400}">→</span> '
            f'{_agent_badge(sk.get("to","?"))} · '
            f'<code style="font-size:11px">{sk.get("work_type","")}</code> · '
            f'rec=<code style="font-size:11px">{sk.get("rec_id","")}</code> · '
            f'<b>{sk.get("age_hours",0)}h</b> old · '
            f'<i style="color:{_AMBER_FG}">{sk.get("stuck_state","")}</i></li>'
            for sk in stuck[:8]
        )
        parts.append(
            f'<div style="background:{_AMBER_BG};border-left:3px solid {_AMBER};'
            f'border-radius:4px;padding:14px 16px;margin:8px 0 12px 0;'
            f'font-size:12px;color:{_AMBER_FG}">'
            f'<b>⚠ {len(stuck)} handoff(s) stuck.</b> '
            f'Routed to a specialist but no outcome (shipped / deferred / rejected) '
            f'within SLA. agent-doctor will escalate after another tick.'
            f'<ul style="margin:8px 0 0 18px;padding:0">{items}</ul></div>'
        )
    return "".join(parts)


def _seo_traffic_metrics() -> list[dict]:
    """Pull per-site SEO traffic-readiness metrics for the digest header.
    Queries each known site's DB via DATABASE_URL_<UPPER_SITE>; sites
    whose env var isn't set are skipped silently. Read-only — never
    writes anything. Failure on any site is logged + ignored, keeping
    the digest robust against DB outages.

    Currently supports SpecPicks (the affiliate-revenue-driving site).
    Extend SITE_QUERIES to add more sites in the future.
    """
    SITE_QUERIES = {
        "specpicks": {
            "env": "DATABASE_URL_SPECPICKS",
            "label": "specpicks.com",
            "amazon_revenue": True,
            "queries": {
                "editorial_coverage": """
                    WITH editorial_asins AS (
                        SELECT DISTINCT unnest(related_product_asins) AS asin
                        FROM editorial_articles
                        WHERE status='published' AND related_product_asins IS NOT NULL
                        UNION
                        SELECT DISTINCT (p->>'asin') AS asin
                        FROM buying_guides bg, jsonb_array_elements(bg.picks) p
                        WHERE bg.status='published' AND jsonb_typeof(bg.picks)='array'
                          AND (p->>'asin') IS NOT NULL AND (p->>'asin') <> ''
                    )
                    SELECT
                        (SELECT COUNT(*) FROM editorial_asins) AS asins_in_editorial,
                        (SELECT COUNT(*) FROM products WHERE is_active=TRUE) AS total_active,
                        (SELECT COUNT(*) FROM products WHERE is_featured=TRUE) AS featured
                """,
                "pricing_freshness": """
                    SELECT
                        COUNT(*) FILTER (WHERE asin IS NOT NULL AND asin <> ''
                                         AND asin !~ '^EBAY_' AND is_active=TRUE) AS amazon_active,
                        COUNT(*) FILTER (WHERE asin IS NOT NULL AND asin !~ '^EBAY_'
                                         AND is_active=TRUE
                                         AND (price_updated_at IS NULL
                                              OR price_updated_at < NOW() - INTERVAL '24 hours')) AS stale_24h,
                        COUNT(*) FILTER (WHERE is_featured=TRUE
                                         AND (price_updated_at IS NULL
                                              OR price_updated_at < NOW() - INTERVAL '24 hours')) AS featured_stale,
                        COUNT(*) FILTER (WHERE is_featured=TRUE
                                         AND price_updated_at >= NOW() - INTERVAL '24 hours') AS featured_fresh
                    FROM products
                """,
                "content_coverage": """
                    SELECT
                        (SELECT COUNT(*) FROM editorial_articles WHERE status='published') AS articles,
                        (SELECT COUNT(*) FROM buying_guides WHERE status='published') AS guides,
                        (SELECT COUNT(*) FROM editorial_topics WHERE status='researched') AS researched_queue,
                        (SELECT COUNT(*) FROM trending_comparisons) AS h2h_pairs
                """,
            },
        },
    }
    out: list[dict] = []
    try:
        import psycopg2
    except ImportError:
        return out
    for site_id, cfg in SITE_QUERIES.items():
        dsn = os.environ.get(cfg["env"], "").strip()
        if not dsn:
            continue
        try:
            with psycopg2.connect(dsn, connect_timeout=10) as conn:
                conn.set_session(readonly=True, autocommit=True)
                with conn.cursor() as cur:
                    metrics = {"site": site_id, "label": cfg["label"],
                                "amazon_revenue": cfg.get("amazon_revenue", False)}
                    for name, sql in cfg["queries"].items():
                        cur.execute(sql)
                        cols = [d[0] for d in cur.description]
                        row = cur.fetchone() or [None] * len(cols)
                        metrics[name] = dict(zip(cols, row))
                    out.append(metrics)
        except Exception as e:
            print(f"  [digest] SEO metrics for {site_id} failed: {e}",
                   file=sys.stderr)
    return out


def _render_seo_traffic_block(seo_metrics: list[dict]) -> str:
    """Render the per-site SEO traffic-readiness block. Empty string if
    no metrics available. One card per site, with a 4-tile KPI row inside."""
    if not seo_metrics:
        return ""
    parts: list[str] = [_section_h2("SEO + Amazon-revenue readiness", emoji="🎯")]
    for m in seo_metrics:
        ec = m.get("editorial_coverage", {})
        pf = m.get("pricing_freshness", {})
        cc = m.get("content_coverage", {})
        total = ec.get("total_active") or 0
        in_ed = ec.get("asins_in_editorial") or 0
        coverage_pct = (100.0 * in_ed / total) if total else 0.0
        f_fresh = pf.get("featured_fresh") or 0
        f_stale = pf.get("featured_stale") or 0
        feat_total = (f_fresh or 0) + (f_stale or 0)
        feat_fresh_pct = (100.0 * f_fresh / feat_total) if feat_total else 100.0
        amazon_active = pf.get("amazon_active") or 0
        stale_24h = pf.get("stale_24h") or 0
        gap_color = _GREEN if coverage_pct >= 5 else _AMBER if coverage_pct >= 1 else _RED
        feat_color = _GREEN if feat_fresh_pct >= 90 else _AMBER if feat_fresh_pct >= 50 else _RED
        stale_color = _GREEN if amazon_active and stale_24h / max(amazon_active, 1) < 0.2 else _AMBER

        articles = cc.get("articles", 0)
        guides = cc.get("guides", 0)
        pairs = cc.get("h2h_pairs", 0)
        queue = cc.get("researched_queue", 0)
        published_total = articles + guides + pairs

        site_tiles = _tile_row([
            _tile("Editorial coverage", f"{coverage_pct:.1f}%", gap_color,
                  sub=f"{in_ed:,} of {total:,} products"),
            _tile("Featured fresh-priced", f"{feat_fresh_pct:.0f}%", feat_color,
                  sub=f"{f_fresh} of {feat_total} ≤ 24h"),
            _tile("Amazon stale (24h)", f"{stale_24h:,}", stale_color,
                  sub=f"of {amazon_active:,} active"),
            _tile("Published content", f"{published_total:,}", _BLUE,
                  sub=f"{queue} in research queue"),
        ])
        breakdown = (
            f'<div style="font-size:12px;color:{_INK_600};padding:8px 14px 0 14px">'
            f'<b>{articles}</b> articles · <b>{guides}</b> buying guides · '
            f'<b>{pairs}</b> head-to-head pairs · '
            f'<b>{queue}</b> researched topics queued for next run'
            f'</div>'
        )
        parts.append(
            f'<div style="background:{_INK_50};border:1px solid {_INK_200};'
            f'border-radius:10px;padding:14px;margin-bottom:14px">'
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
            f'<span style="font-weight:600;color:{_INK_900};font-size:15px">{m["label"]}</span>'
            f'<span style="font-size:10px;color:{_INK_500};text-transform:uppercase;'
            f'letter-spacing:.06em;background:#fff;border:1px solid {_INK_200};'
            f'padding:2px 8px;border-radius:3px">SEO readiness</span>'
            f'</div>'
            f'{site_tiles}'
            f'{breakdown}'
            f'</div>'
        )
    return "".join(parts)


def _section_h2(title: str, *, emoji: str = "", count: int | None = None,
                accent: str = None) -> str:
    """Standard subhead — borrowed directly from the article-author email."""
    accent = accent or _INK_700
    count_html = (
        f'<span style="color:{_INK_400};font-weight:normal;font-size:13px;'
        f'margin-left:6px">({count})</span>'
    ) if count is not None else ""
    emoji_html = f"{emoji} " if emoji else ""
    return (
        f'<h2 style="margin:28px 0 10px 0;font-size:15px;font-weight:600;'
        f'color:{accent};border-bottom:1px solid {_INK_200};padding-bottom:6px">'
        f'{emoji_html}{title}{count_html}</h2>'
    )


def _rec_row(r: dict, *, show_url: bool = True) -> str:
    """Article-author-style row: monospace ID | site chip + agent badge |
    title + meta + url."""
    url_link = (
        f'<a href="{r["public_url"]}" style="color:{_BLUE};font-size:11px;'
        f'margin-left:8px;text-decoration:none">view →</a>'
    ) if show_url and r.get("public_url") else ""
    type_str = r.get("type", "")
    type_chip = (
        f'<span style="font-size:10px;color:{_INK_500};background:#fff;'
        f'border:1px solid {_INK_200};padding:1px 6px;border-radius:3px;'
        f'margin-left:6px">{type_str}</span>'
    ) if type_str else ""
    return (
        f'<tr>'
        f'<td style="padding:10px 8px;border:1px solid {_INK_200};'
        f'font-family:monospace;font-size:11px;color:{_INK_700};'
        f'vertical-align:top;background:#fff;width:1%;white-space:nowrap">'
        f'<b>{r.get("rec_id","")}</b></td>'
        f'<td style="padding:10px 8px;border:1px solid {_INK_200};'
        f'vertical-align:top;background:#fff;width:1%;white-space:nowrap">'
        f'{_agent_badge(r.get("agent",""))}</td>'
        f'<td style="padding:10px 8px;border:1px solid {_INK_200};'
        f'vertical-align:top;background:#fff;font-size:13px;color:{_INK_900}">'
        f'<div><b>{r.get("title","")[:140]}</b>{url_link}</div>'
        f'<div style="font-size:11px;color:{_INK_500};margin-top:4px">'
        f'{type_chip}</div>'
        f'</td></tr>'
    )


def _rec_table(rows_html: list[str]) -> str:
    """Wrap row HTML in the article-author-style table envelope."""
    return (
        f'<table style="border-collapse:collapse;width:100%;'
        f'font-size:13px;margin:6px 0 12px 0">'
        f'<thead><tr style="background:{_INK_50}">'
        f'<th style="text-align:left;padding:8px;border:1px solid {_INK_200};'
        f'color:{_INK_700};font-size:11px;font-weight:600;text-transform:uppercase;'
        f'letter-spacing:.04em">ID</th>'
        f'<th style="text-align:left;padding:8px;border:1px solid {_INK_200};'
        f'color:{_INK_700};font-size:11px;font-weight:600;text-transform:uppercase;'
        f'letter-spacing:.04em">Agent</th>'
        f'<th style="text-align:left;padding:8px;border:1px solid {_INK_200};'
        f'color:{_INK_700};font-size:11px;font-weight:600;text-transform:uppercase;'
        f'letter-spacing:.04em">Recommendation</th>'
        f'</tr></thead><tbody>{"".join(rows_html)}</tbody></table>'
    )


def _escalation_card(e: dict) -> str:
    """Doctor escalation as a richly-detailed card.

    Two-column body: left rail = target + signature + meta, right rail =
    'What went wrong' (raw stderr tail) and 'What we tried' (recipe +
    LLM analysis). The contrast between the two paragraphs is what makes
    the card actionable: the operator can scan one row and decide whether
    to keep the auto-fix loop running or step in."""
    target = e.get("target") or "?"
    sig = e.get("signature") or "?"
    trigger = e.get("trigger") or ""
    recipe = e.get("recipe") or "(none)"
    attempt_n = e.get("attempt_n") or 1
    ts = (e.get("ts") or "")[:19].replace("T", " ")
    sev = (e.get("severity") or "medium").lower()
    sev_color = _RED if sev == "high" else _AMBER
    sev_bg = _RED_BG if sev == "high" else _AMBER_BG
    went_wrong = (e.get("what_went_wrong") or "").replace("<", "&lt;").replace(">", "&gt;")
    we_tried = (e.get("what_we_tried") or "").replace("<", "&lt;").replace(">", "&gt;")
    trigger_chip = (
        f'<span style="font-size:10px;background:{sev_bg};color:{sev_color};'
        f'padding:2px 8px;border-radius:3px;text-transform:uppercase;'
        f'letter-spacing:.04em;font-weight:700">{trigger or sev}</span>'
    )
    return (
        f'<div style="border:1px solid {_INK_200};border-left:3px solid {sev_color};'
        f'border-radius:8px;background:#fff;margin-bottom:12px;overflow:hidden">'
        f'<div style="padding:12px 14px;background:{_INK_50};'
        f'border-bottom:1px solid {_INK_200};display:flex;align-items:center;'
        f'gap:10px;justify-content:space-between">'
        f'<div>{_agent_badge(target)} {trigger_chip} '
        f'<span style="font-size:11px;color:{_INK_500};margin-left:6px">'
        f'attempt #{attempt_n} · {ts} UTC</span></div>'
        f'</div>'
        f'<div style="padding:12px 14px;font-size:12px">'
        f'<div style="margin-bottom:6px"><span style="color:{_INK_500};'
        f'font-size:11px;text-transform:uppercase;letter-spacing:.06em;'
        f'font-weight:600">Signature</span><br>'
        f'<code style="font-size:12px;color:{_INK_900}">{sig}</code></div>'
        f'<div style="margin-bottom:10px"><span style="color:{_INK_500};'
        f'font-size:11px;text-transform:uppercase;letter-spacing:.06em;'
        f'font-weight:600">Recipe applied</span><br>'
        f'<code style="font-size:12px;color:{_INK_700}">{recipe}</code></div>'
        f'<div style="display:block;margin-top:10px">'
        f'<div style="background:{_RED_BG};border:1px solid {_RED_BORDER};'
        f'border-radius:6px;padding:10px 12px;margin-bottom:8px">'
        f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;'
        f'color:{_RED_FG};font-weight:700;margin-bottom:4px">What went wrong</div>'
        f'<pre style="margin:0;font-family:ui-monospace,monospace;font-size:11px;'
        f'color:{_RED_FG};white-space:pre-wrap;line-height:1.45">{went_wrong}</pre>'
        f'</div>'
        f'<div style="background:{_AMBER_BG};border:1px solid {_AMBER};'
        f'border-radius:6px;padding:10px 12px">'
        f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.06em;'
        f'color:{_AMBER_FG};font-weight:700;margin-bottom:4px">What the doctor tried</div>'
        f'<pre style="margin:0;font-family:ui-monospace,monospace;font-size:11px;'
        f'color:{_AMBER_FG};white-space:pre-wrap;line-height:1.45">{we_tried}</pre>'
        f'</div>'
        f'</div>'
        f'</div>'
        f'</div>'
    )


def _render_html(*, window_hours: int, shipped: list[dict], implemented_only: list[dict],
                 queued: list[dict], runs: list[dict], failed_runs: list[dict],
                 escalations: list[dict], suppressed_count: int,
                 seo_metrics: list[dict] | None = None,
                 handoff_metrics: dict | None = None) -> str:
    now = _now()
    by_site_shipped: dict[str, list[dict]] = defaultdict(list)
    for r in shipped:
        by_site_shipped[r.get("site") or "(other)"].append(r)
    by_site_implemented: dict[str, list[dict]] = defaultdict(list)
    for r in implemented_only:
        by_site_implemented[r.get("site") or "(other)"].append(r)

    queued_recs = sum(q["rec_count"] for q in queued)

    parts: list[str] = [
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Activity digest</title></head>'
        f'<body style="margin:0;padding:0;background:{_INK_100};'
        f'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;'
        f'color:{_INK_900};line-height:1.5">',
        # Outer envelope — single column, like the article-author email
        f'<div style="max-width:920px;margin:0 auto;background:#fff;'
        f'padding:28px;border-left:1px solid {_INK_200};border-right:1px solid {_INK_200}">',
        # Header
        f'<h1 style="border-bottom:1px solid {_INK_200};padding-bottom:14px;'
        f'margin:0 0 10px 0;font-size:22px;font-weight:600;color:{_INK_900}">'
        f'Activity digest '
        f'<span style="color:{_INK_500};font-weight:normal;font-size:14px">'
        f'— last {window_hours}h</span></h1>',
        f'<div style="margin-bottom:16px">'
        f'<span style="display:inline-block;background:{_INK_50};color:{_INK_700};'
        f'font-size:11px;padding:3px 10px;border-radius:3px;border:1px solid {_INK_200};'
        f'font-family:monospace">'
        f'<b>Through:</b> {now.strftime("%Y-%m-%d %H:%M UTC")}</span> '
        f'<a href="{_DASHBOARD}" style="color:{_BLUE};font-size:12px;'
        f'margin-left:8px;text-decoration:none">open dashboard →</a>'
        f'</div>',
    ]

    # Summary callout — green when there's positive activity, neutral
    # otherwise. The article-author email's "auto-queued" callout is the
    # visual anchor; we mirror that pattern here.
    if shipped or implemented_only:
        parts.append(
            f'<div style="background:{_GREEN_BG};border-left:3px solid {_GREEN};'
            f'border-radius:4px;padding:14px 16px;margin:0 0 18px 0;font-size:13px;'
            f'color:{_GREEN_FG};line-height:1.5">'
            f'<b>✅ {len(shipped)} shipped to production · '
            f'{len(implemented_only)} implemented (DB-only or awaiting deploy).</b>'
            f'<br><span style="font-size:12px;color:{_GREEN_FG}">'
            f'{queued_recs} more rec{"" if queued_recs == 1 else "s"} queued for the implementer; '
            f'{len(failed_runs)} failed run{"" if len(failed_runs) == 1 else "s"} '
            f'and {len(escalations)} doctor escalation{"" if len(escalations) == 1 else "s"} below.</span>'
            f'</div>'
        )

    # KPI tile strip — five tiles, table-based for Gmail-safe layout.
    parts.append(_tile_row([
        _tile("Shipped", len(shipped), _GREEN),
        _tile("Implemented", len(implemented_only), _BLUE),
        _tile("Queued", queued_recs, _INK_500),
        _tile("Runs", len(runs), _INK_900),
        _tile("Failed", len(failed_runs), _RED if failed_runs else _INK_400),
    ]))

    # ── SEO + Amazon-revenue readiness ─────────────────────────────
    seo_block = _render_seo_traffic_block(seo_metrics or [])
    if seo_block:
        parts.append(seo_block)

    # ── Inter-agent handoffs ───────────────────────────────────────
    handoff_block = _render_handoff_block(handoff_metrics or {})
    if handoff_block:
        parts.append(handoff_block)

    # ── Shipped ────────────────────────────────────────────────────
    if shipped:
        parts.append(_section_h2("Shipped to production", emoji="🚀",
                                  count=len(shipped), accent=_GREEN_FG))
        for site in sorted(by_site_shipped.keys()):
            items = by_site_shipped[site]
            parts.append(
                f'<div style="font-size:13px;color:{_INK_700};margin:12px 0 6px 0">'
                f'{_site_chip(site)}<b>{site}</b> '
                f'<span style="color:{_INK_400};font-weight:normal">· {len(items)}</span></div>'
            )
            rows = [
                _rec_row(r) for r in sorted(items, key=lambda x: x.get("shipped_at", ""), reverse=True)
            ]
            parts.append(_rec_table(rows))

    # ── Implemented (no deploy step yet) ───────────────────────────
    if implemented_only:
        parts.append(_section_h2("Implemented (DB-only or awaiting deploy)",
                                  emoji="✅", count=len(implemented_only),
                                  accent=_BLUE))
        for site in sorted(by_site_implemented.keys()):
            items = by_site_implemented[site]
            parts.append(
                f'<div style="font-size:13px;color:{_INK_700};margin:12px 0 6px 0">'
                f'{_site_chip(site)}<b>{site}</b> '
                f'<span style="color:{_INK_400};font-weight:normal">· {len(items)}</span></div>'
            )
            rows = [
                _rec_row(r) for r in sorted(items, key=lambda x: x.get("implemented_at", ""), reverse=True)
            ]
            parts.append(_rec_table(rows))

    # ── Queued ─────────────────────────────────────────────────────
    if queued:
        parts.append(_section_h2("Queued for the implementer",
                                  emoji="⋯", count=len(queued)))
        rows = []
        for q in queued:
            rows.append(
                f'<tr>'
                f'<td style="padding:8px;border:1px solid {_INK_200};'
                f'background:#fff;width:1%;white-space:nowrap">'
                f'{_agent_badge(q.get("source_agent",""))}</td>'
                f'<td style="padding:8px;border:1px solid {_INK_200};'
                f'background:#fff;font-size:13px;color:{_INK_700}">'
                f'{q.get("site","")}</td>'
                f'<td style="padding:8px;border:1px solid {_INK_200};'
                f'background:#fff;text-align:right;font-weight:700;color:{_INK_900}">'
                f'{q.get("rec_count",0)}</td></tr>'
            )
        parts.append(
            f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 12px 0">'
            f'<thead><tr style="background:{_INK_50}">'
            f'<th style="text-align:left;padding:8px;border:1px solid {_INK_200};'
            f'color:{_INK_700};font-size:11px;font-weight:600;text-transform:uppercase">Source</th>'
            f'<th style="text-align:left;padding:8px;border:1px solid {_INK_200};'
            f'color:{_INK_700};font-size:11px;font-weight:600;text-transform:uppercase">Site</th>'
            f'<th style="text-align:right;padding:8px;border:1px solid {_INK_200};'
            f'color:{_INK_700};font-size:11px;font-weight:600;text-transform:uppercase">Recs</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table>'
        )

    # ── Failed runs ────────────────────────────────────────────────
    if failed_runs:
        parts.append(_section_h2("Failed runs", emoji="❌",
                                  count=len(failed_runs), accent=_RED_FG))
        rows = []
        for r in sorted(failed_runs, key=lambda x: x.get("ended_at", ""), reverse=True):
            rows.append(
                f'<tr>'
                f'<td style="padding:8px;border:1px solid {_RED_BORDER};'
                f'background:{_RED_BG};width:1%;white-space:nowrap;vertical-align:top">'
                f'{_agent_badge(r.get("agent",""))}</td>'
                f'<td style="padding:8px;border:1px solid {_RED_BORDER};'
                f'background:#fff;font-size:13px;color:{_INK_900};vertical-align:top">'
                f'{r.get("summary","")[:240]}'
                f'<div style="font-size:10px;color:{_INK_400};margin-top:4px">'
                f'{r.get("run_ts","")[:19]}</div>'
                f'</td></tr>'
            )
        parts.append(
            f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 12px 0">'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )

    # ── Doctor escalations (rich cards) ────────────────────────────
    if escalations:
        parts.append(_section_h2("Doctor escalations", emoji="🛡",
                                  count=len(escalations), accent=_AMBER_FG))
        parts.append(
            f'<div style="font-size:12px;color:{_INK_500};margin:0 0 10px 0">'
            f'Each card shows the agent that broke, what its run actually '
            f'reported, and what the doctor tried before handing it back to you.'
            f'</div>'
        )
        for e in escalations:
            parts.append(_escalation_card(e))

    # ── Run roster (everything that ran in the window) ─────────────
    if runs:
        parts.append(_section_h2("All runs in window", emoji="⚙️",
                                  count=len(runs)))
        run_rows = []
        for r in sorted(runs, key=lambda x: x.get("ended_at", ""), reverse=True)[:80]:
            color = (_GREEN if r.get("status") == "success"
                     else _RED if r.get("status") == "failure" else _INK_400)
            run_rows.append(
                f'<tr>'
                f'<td style="padding:6px 8px;border-bottom:1px solid {_INK_100};'
                f'width:1%;white-space:nowrap">'
                f'<span style="color:{color};font-size:14px;line-height:1">●</span> '
                f'{_agent_badge(r.get("agent",""))}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid {_INK_100};'
                f'color:{_INK_600};font-size:12px">{r.get("summary","")[:160]}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid {_INK_100};'
                f'font-size:10px;color:{_INK_400};text-align:right;white-space:nowrap">'
                f'{r.get("ended_at","")[:19]}</td></tr>'
            )
        parts.append(
            f'<details><summary style="cursor:pointer;color:{_INK_500};'
            f'font-size:12px;padding:6px 0">show full run list ({len(runs)})</summary>'
            f'<table style="border-collapse:collapse;width:100%;font-size:12px;'
            f'margin-top:8px">{"".join(run_rows)}</table></details>'
        )

    # ── Suppressed individual emails ───────────────────────────────
    if suppressed_count > 0:
        parts.append(
            f'<p style="font-size:11px;color:{_INK_400};margin-top:24px">'
            f'<i>{suppressed_count} individual agent email'
            f'{"s" if suppressed_count != 1 else ""} '
            f'were suppressed in this window and rolled into this digest. '
            f'Set DIGEST_ONLY=0 in the agent host environment to re-enable individual emails.</i>'
            f'</p>'
        )

    # ── Empty-state ────────────────────────────────────────────────
    if not (shipped or implemented_only or queued or failed_runs or escalations):
        parts.append(
            f'<div style="padding:20px;background:{_INK_50};'
            f'border:1px dashed #cbd5e1;'
            f'border-radius:8px;text-align:center;color:{_INK_500};font-size:13px;'
            f'margin-top:8px">'
            f'Nothing notable in the last window. All agents idle or waiting on cron.'
            f'</div>'
        )

    parts.append(
        f'<hr style="margin:32px 0 16px;border:none;border-top:1px solid {_INK_200}">'
        f'<p style="color:{_INK_400};font-size:11px;margin:0">'
        f'Generated by <code>{AGENT_ID}</code> · '
        f'<a href="{_DASHBOARD}/agents/{AGENT_ID}" style="color:{_INK_400}">'
        f'edit cadence in dashboard</a>'
        f'</p>'
        f'</div></body></html>'
    )
    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Agent
# ──────────────────────────────────────────────────────────────────────


class DigestRollupAgent(AgentBase):
    agent_id = AGENT_ID
    name = "Activity Digest Rollup"
    description = (
        "Single 3-hourly summary email replacing the per-agent flood. "
        "Reads digest-queue/ + recent shipped/implemented/queued/failed "
        "across the fleet."
    )
    category = "ops"
    send_run_summary_email = False  # we ARE the summary email — don't send our own narrative

    capabilities = [
        declare("read_storage",
                "List + read JSON across the agents/ + digest-queue/ blob trees",
                confirmation_required=False, risk_level="low"),
        declare("send_digest_email",
                "Send a single consolidated summary email (bypasses DIGEST_ONLY gate)",
                confirmation_required=False, risk_level="low"),
        declare("archive_queue",
                "Delete digest-queue entries older than the rollup window",
                confirmation_required=False, risk_level="low"),
    ]

    def run(self) -> RunResult:
        now = _now()
        cutoff = now - timedelta(hours=WINDOW_HOURS)
        s = self.storage
        self.status("scanning storage for activity", progress=0.10)

        suppressed, archive = _drain_digest_queue(s, cutoff)
        self.decide("observation", f"digest-queue: {len(suppressed)} in window, {len(archive)} total to archive")

        shipped_or_impl = _recent_shipped(s, cutoff)
        shipped = [r for r in shipped_or_impl if r.get("shipped")]
        implemented_only = [r for r in shipped_or_impl if r.get("implemented") and not r.get("shipped")]
        self.decide("observation", f"recs: {len(shipped)} shipped + {len(implemented_only)} implemented-only")

        runs = _recent_runs(s, cutoff)
        failed_runs = [r for r in runs if r.get("status") == "failure"]
        self.decide("observation", f"runs: {len(runs)} total, {len(failed_runs)} failed")

        queued = _auto_queue_pending(s)
        self.decide("observation", f"queued: {sum(q['rec_count'] for q in queued)} recs across {len(queued)} dispatches")

        escalations = _doctor_escalations(s, cutoff)
        self.decide("observation", f"doctor escalations in window: {len(escalations)}")

        # Inter-agent handoff metrics — read from agents/*/handoffs.jsonl.
        # Surfaces sender→receiver counts, outcomes, and stuck handoffs.
        try:
            handoff_metrics = _handoff_metrics(s, cutoff)
            self.decide(
                "observation",
                f"handoffs in window: out={handoff_metrics.get('total_out',0)} "
                f"shipped={handoff_metrics.get('shipped',0)} "
                f"in_progress={handoff_metrics.get('in_progress',0)} "
                f"deferred={handoff_metrics.get('deferred',0)} "
                f"stuck={len(handoff_metrics.get('stuck') or [])}"
            )
        except Exception as e:
            self.decide("error", f"handoff metrics gather failed: {e}")
            handoff_metrics = {}

        # Per-site SEO readiness metrics — pulled fresh each digest so
        # the user sees coverage progress trend across the 3h windows.
        seo_metrics = _seo_traffic_metrics()
        if seo_metrics:
            self.decide(
                "observation",
                f"SEO metrics: {len(seo_metrics)} site(s) — "
                + ", ".join(
                    f"{m['site']} editorial={m.get('editorial_coverage',{}).get('asins_in_editorial',0)}/"
                    f"{m.get('editorial_coverage',{}).get('total_active',0)}"
                    for m in seo_metrics
                )
            )

        # ── Render + send ──────────────────────────────────────────
        self.status("rendering digest", progress=0.70)
        body_html = _render_html(
            window_hours=WINDOW_HOURS,
            shipped=shipped,
            implemented_only=implemented_only,
            queued=queued,
            runs=runs,
            failed_runs=failed_runs,
            escalations=escalations,
            suppressed_count=len(suppressed),
            seo_metrics=seo_metrics,
            handoff_metrics=handoff_metrics,
        )
        queued_recs = sum(q["rec_count"] for q in queued)
        subject_parts = [f"{len(shipped)} shipped"]
        if failed_runs:
            subject_parts.append(f"{len(failed_runs)} failed")
        if queued_recs:
            subject_parts.append(f"{queued_recs} queued")
        subject = f"[{AGENT_ID}] Digest · {' · '.join(subject_parts)} — last {WINDOW_HOURS}h"

        # If absolutely nothing happened AND no individual emails were
        # suppressed, skip sending altogether — no need to fire a "nothing
        # to report" email every 3 hours.
        if not (shipped or implemented_only or queued or failed_runs or escalations or suppressed):
            self.decide("observation", "nothing to report this window — skipping send")
            self._archive_queue(s, archive)
            return RunResult(
                status="success",
                summary="empty window — no email sent",
                metrics={
                    "shipped": 0, "implemented_only": 0, "queued": 0,
                    "runs": len(runs), "failed_runs": 0, "escalations": 0,
                    "suppressed": 0, "emailed": False,
                },
            )

        try:
            from shared.site_quality import send_via_msmtp  # noqa: WPS433
            ok, detail = send_via_msmtp(
                subject=subject, body_html=body_html,
                to=[OWNER],
                sender=f"NSC Agent Digest <automation@northernsoftwareconsulting.com>",
                msmtp_account="automation",
                extra_headers={"X-Reusable-Agent": AGENT_ID},
                bypass_digest=True,  # critical — we ARE the digest
            )
        except Exception as e:
            ok, detail = False, f"send failed: {e}"

        if ok:
            self.decide("action", f"digest sent ({detail})")
            self._archive_queue(s, archive)
        else:
            self.decide("error", f"digest send failed: {detail}")

        return RunResult(
            status="success" if ok else "failure",
            summary=(
                f"{len(shipped)} shipped · {len(implemented_only)} implemented · "
                f"{sum(q['rec_count'] for q in queued)} queued · "
                f"{len(failed_runs)} failed · {len(escalations)} escalations · "
                f"{len(suppressed)} individual emails rolled up"
            ),
            metrics={
                "shipped": len(shipped),
                "implemented_only": len(implemented_only),
                "queued_recs": sum(q["rec_count"] for q in queued),
                "queued_dispatches": len(queued),
                "runs": len(runs),
                "failed_runs": len(failed_runs),
                "escalations": len(escalations),
                "suppressed": len(suppressed),
                "emailed": bool(ok),
            },
        )

    def _archive_queue(self, s, keys: list[str]) -> None:
        """Move delivered + out-of-window queue entries to digest-archive/.
        Keep the archive bounded — a separate cleanup can prune later."""
        for k in keys:
            try:
                d = s.read_json(k)
                if d:
                    archive_key = k.replace("digest-queue/", "digest-archive/", 1)
                    s.write_json(archive_key, d)
                s.delete(k)
            except Exception:
                pass


def main() -> int:
    agent = DigestRollupAgent()
    result = agent.run_once()
    print(f"[{AGENT_ID}] status={result.status} summary={result.summary}")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
