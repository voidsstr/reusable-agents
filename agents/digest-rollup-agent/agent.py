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
    """Walk every agent's run-index.json for recent run entries."""
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
                out.append({
                    "agent": agent,
                    "site": _agent_site(agent),
                    "run_ts": entry.get("run_ts", ""),
                    "status": entry.get("status", ""),
                    "summary": (entry.get("summary") or "")[:200],
                    "ended_at": entry.get("ended_at", ""),
                    "metrics": entry.get("metrics") or {},
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
    """Read agent-doctor errors that landed in window."""
    out: list[dict] = []
    try:
        for k in s.list_prefix("agents/agent-doctor/errors/"):
            if not k.endswith(".json"):
                continue
            try:
                d = s.read_json(k) or {}
            except Exception:
                continue
            ts = _iso_to_dt(d.get("ts") or "")
            if ts is None or ts < cutoff:
                continue
            ctx = d.get("context") or {}
            out.append({
                "ts": d.get("ts") or "",
                "target": ctx.get("target_agent") or "",
                "signature": ctx.get("error_signature") or "",
                "severity": d.get("severity") or "medium",
                "message": (d.get("error_message") or "")[:160],
            })
    except Exception:
        pass
    return out


# ──────────────────────────────────────────────────────────────────────
# HTML rendering — clean grouped layout
# ──────────────────────────────────────────────────────────────────────

_DASHBOARD = "https://agents.happysky-24190067.eastus.azurecontainerapps.io"


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
    no metrics available."""
    if not seo_metrics:
        return ""
    parts: list[str] = [
        '<h2 style="font-size:15px;color:#0f172a;margin:24px 0 8px 0;'
        'border-bottom:1px solid #e2e8f0;padding-bottom:6px">'
        '🎯 SEO + Amazon-revenue readiness</h2>'
    ]
    for m in seo_metrics:
        ec = m.get("editorial_coverage", {})
        pf = m.get("pricing_freshness", {})
        cc = m.get("content_coverage", {})
        # Editorial coverage %
        total = ec.get("total_active") or 0
        in_ed = ec.get("asins_in_editorial") or 0
        coverage_pct = (100.0 * in_ed / total) if total else 0.0
        # Featured price freshness — the metric the user actually feels
        f_fresh = pf.get("featured_fresh") or 0
        f_stale = pf.get("featured_stale") or 0
        feat_total = (f_fresh or 0) + (f_stale or 0)
        feat_fresh_pct = (100.0 * f_fresh / feat_total) if feat_total else 100.0
        amazon_active = pf.get("amazon_active") or 0
        stale_24h = pf.get("stale_24h") or 0
        # Coverage gap headline color
        gap_color = "#10b981" if coverage_pct >= 5 else "#f59e0b" if coverage_pct >= 1 else "#ef4444"
        feat_color = "#10b981" if feat_fresh_pct >= 90 else "#f59e0b" if feat_fresh_pct >= 50 else "#ef4444"
        parts.append(
            f'<div style="background:#f8fafc;border:1px solid #e2e8f0;'
            f'border-radius:8px;padding:14px;margin-bottom:10px">'
            f'<div style="font-weight:600;color:#0f172a;margin-bottom:8px">'
            f'{m["label"]}</div>'
            f'<table style="border-collapse:collapse;font-size:13px;width:100%">'
            f'<tr><td style="padding:4px 8px;color:#475569">Editorial coverage</td>'
            f'<td style="padding:4px 8px;font-weight:600;color:{gap_color}">'
            f'{in_ed:,} of {total:,} active products ({coverage_pct:.2f}%)</td></tr>'
            f'<tr><td style="padding:4px 8px;color:#475569">Featured products fresh-priced (≤24h)</td>'
            f'<td style="padding:4px 8px;font-weight:600;color:{feat_color}">'
            f'{f_fresh} of {feat_total} ({feat_fresh_pct:.0f}%)</td></tr>'
            f'<tr><td style="padding:4px 8px;color:#475569">Stale-priced Amazon products (catalog-wide)</td>'
            f'<td style="padding:4px 8px">'
            f'{stale_24h:,} of {amazon_active:,} '
            f'<span style="color:#94a3b8;font-size:11px">(refreshed 6,000/day at 500/run × 12 runs)</span></td></tr>'
            f'<tr><td style="padding:4px 8px;color:#475569">Published content</td>'
            f'<td style="padding:4px 8px">'
            f'{cc.get("articles", 0)} articles · '
            f'{cc.get("guides", 0)} buying guides · '
            f'{cc.get("h2h_pairs", 0)} head-to-head pairs · '
            f'{cc.get("researched_queue", 0)} researched topics in queue</td></tr>'
            f'</table>'
            f'</div>'
        )
    return "".join(parts)


def _section_header(title: str, count: int, color: str) -> str:
    return (
        f'<h2 style="font-size:16px;color:{color};border-bottom:2px solid {color};'
        f'padding-bottom:6px;margin:28px 0 10px 0">'
        f'{title} <span style="color:#94a3b8;font-weight:normal;font-size:13px">({count})</span></h2>'
    )


def _rec_row(r: dict, *, show_url: bool = True) -> str:
    site = r.get("site") or ""
    site_chip = (
        f'<span style="font-size:10px;background:#f1f5f9;color:#64748b;'
        f'padding:1px 6px;border-radius:3px;margin-right:6px">{site}</span>'
    ) if site else ""
    url_link = (
        f'<a href="{r["public_url"]}" style="color:#2563eb;font-size:11px;'
        f'margin-left:8px">view →</a>'
    ) if show_url and r.get("public_url") else ""
    return (
        f'<tr><td style="padding:6px 8px;border-bottom:1px solid #e2e8f0">'
        f'{site_chip}<code style="font-size:11px;color:#475569">{r.get("rec_id","")}</code></td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;font-size:13px">'
        f'{r.get("title","")[:120]}{url_link}'
        f'<div style="font-size:10px;color:#94a3b8;margin-top:2px">'
        f'{r.get("agent","")}{" · " + r.get("type","") if r.get("type") else ""}</div>'
        f'</td></tr>'
    )


def _render_html(*, window_hours: int, shipped: list[dict], implemented_only: list[dict],
                 queued: list[dict], runs: list[dict], failed_runs: list[dict],
                 escalations: list[dict], suppressed_count: int,
                 seo_metrics: list[dict] | None = None) -> str:
    now = _now()
    by_site_shipped: dict[str, list[dict]] = defaultdict(list)
    for r in shipped:
        by_site_shipped[r.get("site") or "(other)"].append(r)
    by_site_implemented: dict[str, list[dict]] = defaultdict(list)
    for r in implemented_only:
        by_site_implemented[r.get("site") or "(other)"].append(r)

    parts: list[str] = [
        '<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#0f172a;line-height:1.5;max-width:920px;margin:0 auto;padding:24px;background:#fff">',
        f'<h1 style="border-bottom:1px solid #e2e8f0;padding-bottom:12px;margin:0 0 12px 0;font-size:22px;color:#0f172a">'
        f'Activity digest <span style="color:#64748b;font-weight:normal;font-size:14px">— last {window_hours}h</span></h1>',
        f'<p style="color:#475569;font-size:13px;margin:0 0 18px 0">'
        f'Through {now.strftime("%Y-%m-%d %H:%M UTC")} · '
        f'<a href="{_DASHBOARD}" style="color:#2563eb">open dashboard</a>'
        f'</p>',
        # KPI strip
        '<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin:0 0 22px 0">',
    ]
    for label, val, color in (
        ("Shipped", len(shipped), "#10b981"),
        ("Implemented", len(implemented_only), "#3b82f6"),
        ("Queued", sum(q["rec_count"] for q in queued), "#64748b"),
        ("Runs", len(runs), "#0f172a"),
        ("Failed", len(failed_runs), "#ef4444" if failed_runs else "#94a3b8"),
    ):
        parts.append(
            f'<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;'
            f'padding:10px;text-align:center">'
            f'<div style="font-size:1.6rem;font-weight:700;color:{color}">{val}</div>'
            f'<div style="font-size:.72rem;color:#64748b;text-transform:uppercase;'
            f'letter-spacing:.05em;margin-top:4px">{label}</div></div>'
        )
    parts.append("</div>")

    # ── SEO + Amazon-revenue readiness ──────────────────────────────
    seo_block = _render_seo_traffic_block(seo_metrics or [])
    if seo_block:
        parts.append(seo_block)

    # ── Shipped ─────────────────────────────────────────────────────
    if shipped:
        parts.append(_section_header("🚀 Shipped to production", len(shipped), "#10b981"))
        for site in sorted(by_site_shipped.keys()):
            items = by_site_shipped[site]
            parts.append(
                f'<h3 style="font-size:13px;color:#334155;margin:12px 0 4px 0">'
                f'{site} <span style="color:#94a3b8;font-weight:normal">· {len(items)}</span></h3>'
                f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin-bottom:6px">'
            )
            for r in sorted(items, key=lambda x: x.get("shipped_at", ""), reverse=True):
                parts.append(_rec_row(r))
            parts.append("</table>")

    # ── Implemented (no deploy step yet) ────────────────────────────
    if implemented_only:
        parts.append(_section_header("✅ Implemented (awaiting deploy or pure-DB)", len(implemented_only), "#3b82f6"))
        for site in sorted(by_site_implemented.keys()):
            items = by_site_implemented[site]
            parts.append(
                f'<h3 style="font-size:13px;color:#334155;margin:12px 0 4px 0">'
                f'{site} <span style="color:#94a3b8;font-weight:normal">· {len(items)}</span></h3>'
                f'<table style="border-collapse:collapse;width:100%;font-size:13px;margin-bottom:6px">'
            )
            for r in sorted(items, key=lambda x: x.get("implemented_at", ""), reverse=True):
                parts.append(_rec_row(r))
            parts.append("</table>")

    # ── Queued ─────────────────────────────────────────────────────
    if queued:
        parts.append(_section_header("⋯ Queued for the implementer", len(queued), "#64748b"))
        parts.append('<table style="border-collapse:collapse;width:100%;font-size:13px">')
        parts.append('<tr style="background:#f8fafc"><th style="text-align:left;padding:6px 8px">Source agent</th>'
                     '<th style="text-align:left;padding:6px 8px">Site</th>'
                     '<th style="text-align:right;padding:6px 8px">Recs</th></tr>')
        for q in queued:
            parts.append(
                f'<tr><td style="padding:6px 8px;border-bottom:1px solid #e2e8f0">'
                f'<code style="font-size:11px">{q.get("source_agent","")}</code></td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0">{q.get("site","")}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:600">{q.get("rec_count",0)}</td></tr>'
            )
        parts.append("</table>")

    # ── Runs (failures first) ──────────────────────────────────────
    if failed_runs:
        parts.append(_section_header("❌ Failed runs", len(failed_runs), "#ef4444"))
        parts.append('<table style="border-collapse:collapse;width:100%;font-size:13px">')
        for r in sorted(failed_runs, key=lambda x: x.get("ended_at", ""), reverse=True):
            parts.append(
                f'<tr><td style="padding:6px 8px;border-bottom:1px solid #fee2e2;width:40%">'
                f'<code style="font-size:11px">{r.get("agent","")}</code></td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #fee2e2">'
                f'{r.get("summary","")[:200]}'
                f'<div style="font-size:10px;color:#94a3b8">{r.get("run_ts","")[:19]}</div>'
                f'</td></tr>'
            )
        parts.append("</table>")

    # ── Doctor escalations ─────────────────────────────────────────
    if escalations:
        parts.append(_section_header("🛡 Doctor escalations", len(escalations), "#b45309"))
        parts.append('<table style="border-collapse:collapse;width:100%;font-size:13px">')
        for e in sorted(escalations, key=lambda x: x.get("ts", ""), reverse=True):
            parts.append(
                f'<tr><td style="padding:6px 8px;border-bottom:1px solid #fde68a">'
                f'<code style="font-size:11px">{e.get("target","")}</code><br>'
                f'<span style="font-size:10px;color:#92400e">sig: {e.get("signature","")[:60]}</span></td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #fde68a;font-size:12px;color:#78350f">{e.get("message","")}</td></tr>'
            )
        parts.append("</table>")

    # ── Run roster (everything that ran in the window) ─────────────
    if runs:
        parts.append(_section_header(f"⚙️ All runs in window", len(runs), "#0f172a"))
        parts.append('<details><summary style="cursor:pointer;color:#64748b;font-size:12px">show full run list</summary>'
                     '<table style="border-collapse:collapse;width:100%;font-size:12px;margin-top:8px">')
        for r in sorted(runs, key=lambda x: x.get("ended_at", ""), reverse=True)[:80]:
            color = "#10b981" if r.get("status") == "success" else "#ef4444" if r.get("status") == "failure" else "#94a3b8"
            parts.append(
                f'<tr><td style="padding:4px 8px;border-bottom:1px solid #f1f5f9">'
                f'<span style="color:{color}">●</span> <code style="font-size:11px">{r.get("agent","")}</code></td>'
                f'<td style="padding:4px 8px;border-bottom:1px solid #f1f5f9;color:#475569">{r.get("summary","")[:140]}</td>'
                f'<td style="padding:4px 8px;border-bottom:1px solid #f1f5f9;font-size:10px;color:#94a3b8;text-align:right">{r.get("ended_at","")[:19]}</td></tr>'
            )
        parts.append("</table></details>")

    # ── Suppressed-individual-emails count ─────────────────────────
    if suppressed_count > 0:
        parts.append(
            f'<p style="font-size:11px;color:#94a3b8;margin-top:24px">'
            f'<i>{suppressed_count} individual agent email{"s" if suppressed_count != 1 else ""} '
            f'were suppressed in this window and rolled into this digest. '
            f'Set DIGEST_ONLY=0 in the agent host environment to re-enable individual emails.</i>'
            f'</p>'
        )

    # ── Empty-state ────────────────────────────────────────────────
    if not (shipped or implemented_only or queued or failed_runs or escalations):
        parts.append(
            '<div style="padding:18px;background:#f8fafc;border:1px dashed #cbd5e1;'
            'border-radius:8px;text-align:center;color:#64748b;font-size:13px">'
            'Nothing notable in the last window. All agents idle or waiting on cron.'
            '</div>'
        )

    parts.append(
        '<hr style="margin:32px 0 16px;border:none;border-top:1px solid #e2e8f0">'
        '<p style="color:#94a3b8;font-size:11px;margin:0">'
        f'Generated by <code>{AGENT_ID}</code> · '
        f'<a href="{_DASHBOARD}/agents/{AGENT_ID}" style="color:#94a3b8">edit cadence in dashboard</a>'
        '</p></body></html>'
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
