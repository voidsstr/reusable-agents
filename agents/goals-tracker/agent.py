"""Goals Tracker — daily email digest of every agent's goals + metrics.

Agent-agnostic. Walks the registry, reads each agent's goals/active.json
+ timeseries-cache.json, and renders an HTML email with:

  • Per-site KPI roll-up (organic clicks, impressions, conversions trend)
  • Per-agent goal table with baseline / current / target / trend arrow
    + an inline SVG sparkline showing last 30 measurements per goal
  • "Stale metrics" alert section listing agents whose latest progress
    point is older than the staleness threshold (default 30h — runs once
    a day so up-to-24h gaps are normal; >30h means the daily run failed).

Outputs:
  • HTML email to mperry@northernsoftwareconsulting.com via msmtp
    [automation]
  • The same HTML written to <run-dir>/digest.html for inspection
  • A summary line on stdout

Run:
  python3 agent.py [--no-email] [--debug]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from framework.core import registry, goals as goals_mod, metric_helper
from framework.core.storage import get_storage


# Config
TO_EMAIL = os.environ.get("GOALS_TRACKER_TO", "mperry@northernsoftwareconsulting.com")
FROM_EMAIL = os.environ.get("GOALS_TRACKER_FROM", "automation@northernsoftwareconsulting.com")
# Send path: prefer Microsoft Graph /sendMail (works through tenant SMTP-auth
# disable), fall back to msmtp[automation] if Graph fails, then msmtp[personal].
GRAPH_OAUTH_FILE = os.environ.get(
    "GOALS_TRACKER_OAUTH_FILE",
    os.path.expanduser("~/.reusable-agents/responder/.oauth.json"),
)
MSMTP_ACCOUNT = os.environ.get("GOALS_TRACKER_MSMTP_ACCOUNT", "automation")
STALE_HOURS = int(os.environ.get("GOALS_TRACKER_STALE_HOURS", "30"))
SUBJECT_TPL = os.environ.get("GOALS_TRACKER_SUBJECT", "[Goals Tracker] {date} — {n_agents} agents, {n_goals} goals, {n_stale} stale")


def err(*a) -> None:
    print(*a, file=sys.stderr)


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def collect_all_agents() -> list[dict]:
    """Pull every registered + ENABLED agent's goal data into a unified shape.
    Disabled agents are filtered out — their goals don't represent active
    work and would otherwise pile up in the stale-agents alert."""
    storage = get_storage()
    agents = registry.list_agents(storage=storage)
    out: list[dict] = []
    for a in agents:
        if not getattr(a, "enabled", True):
            continue
        agent_id = a.id
        active = storage.read_json(goals_mod.goals_key(agent_id)) or {}
        goals = active.get("goals", []) or []
        cache = metric_helper.read_cache(agent_id, storage=storage) or {}
        cache_goals = cache.get("goals") or {}
        # Build per-goal metrics
        goal_rows: list[dict] = []
        for g in goals:
            gid = g.get("id", "")
            if not gid:
                continue
            metric = g.get("metric") or {}
            cache_entry = cache_goals.get(gid) or {}
            points = cache_entry.get("points") or []
            baseline = points[0].get("value") if points else None
            current = cache_entry.get("latest_value")
            if current is None:
                current = metric.get("current")
            target = metric.get("target")
            direction = metric.get("direction", "increase")
            unit = metric.get("unit", "")
            latest_ts = cache_entry.get("latest_ts") or ""
            stale = _is_stale(latest_ts)
            # Trend: last value vs first
            trend = None
            if baseline is not None and current is not None and baseline != 0:
                trend = round(100.0 * (current - baseline) / abs(baseline), 1)
            elif baseline is not None and current is not None:
                trend = current - baseline
            goal_rows.append({
                "id": gid,
                "title": g.get("title", gid),
                "description": g.get("description", ""),
                "status": g.get("status", "active"),
                "metric_name": metric.get("name", ""),
                "current": current,
                "baseline": baseline,
                "target": target,
                "direction": direction,
                "unit": unit,
                "horizon_weeks": metric.get("horizon_weeks"),
                "is_revenue_goal": bool(g.get("is_revenue_goal")),
                "n_points": len(points),
                "latest_ts": latest_ts,
                "stale": stale,
                "trend_pct": trend,
                "points": points,  # for sparkline
            })
        if not goal_rows:
            continue
        # Agent-level "reported today" check
        latest_ts_overall = max((g["latest_ts"] for g in goal_rows if g["latest_ts"]), default="")
        agent_stale = _is_stale(latest_ts_overall) if latest_ts_overall else True
        out.append({
            "id": agent_id,
            "name": a.name,
            "category": a.category,
            "site": (a.metadata or {}).get("site", ""),
            "enabled": a.enabled,
            "cron_expr": a.cron_expr,
            "n_goals": len(goal_rows),
            "n_stale_goals": sum(1 for g in goal_rows if g["stale"]),
            "n_revenue_goals": sum(1 for g in goal_rows if g["is_revenue_goal"]),
            "n_accomplished": sum(1 for g in goal_rows if g["status"] == "accomplished"),
            "latest_ts_overall": latest_ts_overall,
            "agent_stale": agent_stale,
            "goals": goal_rows,
        })
    return out


def _is_stale(latest_ts: str) -> bool:
    """True if the latest progress point is older than STALE_HOURS."""
    if not latest_ts:
        return True
    try:
        ts = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
    except Exception:
        return True
    return (now() - ts) > timedelta(hours=STALE_HOURS)


# ---------------------------------------------------------------------------
# Rendering — inline SVG sparklines + HTML email
# ---------------------------------------------------------------------------

def render_sparkline(points: list[dict], width: int = 120, height: int = 30,
                     direction: str = "increase") -> str:
    """Inline SVG sparkline. Renders the last 30 points; green if trend
    matches desired direction, red if opposite, grey if flat or single point."""
    if not points:
        return f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg"></svg>'
    values = [p.get("value") for p in points[-30:] if p.get("value") is not None]
    if len(values) < 2:
        # Single point — render dot
        return (f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
                f'<circle cx="{width/2}" cy="{height/2}" r="3" fill="#94a3b8"/></svg>')
    vmin = min(values); vmax = max(values)
    span = max(vmax - vmin, 1e-9)
    pad = 3
    pts = []
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad) * i / (len(values) - 1)
        y = (height - pad) - (height - 2 * pad) * (v - vmin) / span
        pts.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(pts)
    # Choose color by trend matching desired direction
    delta = values[-1] - values[0]
    if delta == 0:
        color = "#94a3b8"
    elif (delta > 0 and direction == "increase") or (delta < 0 and direction == "decrease"):
        color = "#16a34a"  # green — improving
    else:
        color = "#dc2626"  # red — worsening
    # End-point dot for visual reference
    last_x, last_y = pts[-1].split(",")
    return (
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" '
        f'style="vertical-align:middle">'
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="2.5" fill="{color}"/>'
        f'</svg>'
    )


def fmt_value(v: Optional[float], unit: str = "") -> str:
    """Format a metric value with proper spacing between number and unit.
    Examples:  75, "clicks"  → "75 clicks"   (space before word units)
               99.5, "%"     → "99.5%"        (no space before %)
               1234, ""      → "1,234"        (thousands separator, no unit)
    """
    if v is None:
        return "—"
    # Compact short numbers; thousands-separated for >= 1000
    if abs(v - round(v)) < 0.001:
        n = int(round(v))
        s = f"{n:,}" if abs(n) >= 1000 else f"{n}"
    else:
        s = f"{v:,.2f}"
    if not unit:
        return s
    if unit == "%":
        return f"{s}%"
    # Word unit — space (use non-breaking so number+unit don't wrap)
    return f"{s} {unit}"


def fmt_trend(trend_pct: Optional[float], direction: str) -> str:
    if trend_pct is None:
        return '<span style="color:#94a3b8">—</span>'
    if trend_pct == 0:
        return '<span style="color:#94a3b8">flat</span>'
    is_improving = (trend_pct > 0 and direction == "increase") or (trend_pct < 0 and direction == "decrease")
    color = "#16a34a" if is_improving else "#dc2626"
    arrow = "↑" if trend_pct > 0 else "↓"
    return f'<span style="color:{color};font-weight:600">{arrow} {abs(trend_pct):.1f}%</span>'


def fmt_progress(current: Optional[float], target: Optional[float], direction: str) -> str:
    if current is None or target is None or target == 0:
        return '<span style="color:#94a3b8">—</span>'
    if direction == "decrease":
        # Lower is better. Progress = (start - current) / (start - target). We don't
        # have start here, so just show "current vs target" as a delta.
        if current <= target:
            return '<span style="color:#16a34a;font-weight:600">✓ at target</span>'
        return f'<span style="color:#475569">{current:.0f} → {target:.0f}</span>'
    pct = 100.0 * current / target
    if pct >= 100:
        return '<span style="color:#16a34a;font-weight:600">✓ accomplished</span>'
    color = "#16a34a" if pct >= 75 else "#475569" if pct >= 25 else "#dc2626"
    return f'<span style="color:{color}">{pct:.0f}% to target</span>'


def render_email_html(agents: list[dict]) -> str:
    """Render the full HTML digest. Responsive design — single column on
    mobile (<600px), 2-column site cards on desktop. Email-client safe:
    inline styles only, table-based layout for Outlook compat, font stack
    falls through to system fonts, numbers use tabular-nums for alignment.
    """
    today = now().strftime("%A, %B %d, %Y")
    today_short = now().strftime("%Y-%m-%d")

    n_total = len(agents)
    n_stale_agents = sum(1 for a in agents if a["agent_stale"])
    n_total_goals = sum(a["n_goals"] for a in agents)
    n_stale_goals = sum(a["n_stale_goals"] for a in agents)
    n_accomplished = sum(a["n_accomplished"] for a in agents)
    n_revenue = sum(a["n_revenue_goals"] for a in agents)

    # Per-site agent grouping
    agents_by_site: dict[str, list[dict]] = {}
    for a in agents:
        site = a["site"] or "shared"
        agents_by_site.setdefault(site, []).append(a)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>Goals Tracker — {today_short}</title>
<style>
@media only screen and (max-width:640px) {{
  .gt-container {{ width:100% !important; padding:16px !important; }}
  .gt-kpi-grid {{ display:block !important; }}
  .gt-kpi-card {{ display:block !important; width:100% !important; margin-bottom:12px !important; }}
  .gt-summary-stats td {{ display:block !important; width:100% !important; padding:8px 0 !important; border:none !important; }}
  .gt-agent-table {{ font-size:13px !important; }}
  .gt-hide-mobile {{ display:none !important; }}
  .gt-stack-mobile {{ display:block !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;color:#0f172a;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden">
  Goals Tracker · {today_short} · {n_total} agents · {n_total_goals} goals · {n_stale_agents} need attention
</div>

<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f1f5f9;padding:32px 16px">
<tr><td align="center">

<table role="presentation" cellpadding="0" cellspacing="0" border="0" class="gt-container" style="width:680px;max-width:680px;background:#ffffff;border-radius:12px;box-shadow:0 1px 3px rgba(15,23,42,0.06),0 4px 12px rgba(15,23,42,0.04)">

<!-- HEADER -->
<tr><td style="padding:32px 40px 8px">
  <div style="font-size:13px;color:#64748b;letter-spacing:0.04em;text-transform:uppercase;font-weight:600;margin-bottom:8px">Goals Tracker</div>
  <h1 style="margin:0 0 4px;font-size:24px;line-height:1.25;color:#0f172a;font-weight:700;letter-spacing:-0.01em">{today}</h1>
  <p style="margin:4px 0 0;color:#64748b;font-size:14px;line-height:1.5">
    Daily snapshot of every active agent's goals and metrics.
  </p>
</td></tr>

<!-- SUMMARY STATS BAR -->
<tr><td style="padding:24px 40px 0">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" class="gt-summary-stats" style="width:100%;border-collapse:collapse;background:#f8fafc;border-radius:10px">
    <tr>
      <td style="padding:16px;text-align:center;border-right:1px solid #e2e8f0">
        <div style="font-size:24px;font-weight:700;color:#0f172a;line-height:1.2">{n_total}</div>
        <div style="font-size:11px;color:#64748b;letter-spacing:0.04em;text-transform:uppercase;margin-top:2px">Agents</div>
      </td>
      <td style="padding:16px;text-align:center;border-right:1px solid #e2e8f0">
        <div style="font-size:24px;font-weight:700;color:#0f172a;line-height:1.2">{n_total_goals}</div>
        <div style="font-size:11px;color:#64748b;letter-spacing:0.04em;text-transform:uppercase;margin-top:2px">Goals</div>
      </td>
      <td style="padding:16px;text-align:center;border-right:1px solid #e2e8f0">
        <div style="font-size:24px;font-weight:700;color:#16a34a;line-height:1.2">{n_accomplished}</div>
        <div style="font-size:11px;color:#64748b;letter-spacing:0.04em;text-transform:uppercase;margin-top:2px">Accomplished</div>
      </td>
      <td style="padding:16px;text-align:center;border-right:1px solid #e2e8f0">
        <div style="font-size:24px;font-weight:700;color:#f59e0b;line-height:1.2">{n_revenue}</div>
        <div style="font-size:11px;color:#64748b;letter-spacing:0.04em;text-transform:uppercase;margin-top:2px">Revenue Goals</div>
      </td>
      <td style="padding:16px;text-align:center">
        <div style="font-size:24px;font-weight:700;color:{'#dc2626' if n_stale_agents else '#16a34a'};line-height:1.2">{n_stale_agents}</div>
        <div style="font-size:11px;color:#64748b;letter-spacing:0.04em;text-transform:uppercase;margin-top:2px">Need Attention</div>
      </td>
    </tr>
  </table>
</td></tr>

<!-- SITE KPI CARDS -->
{render_site_kpi_cards(agents)}

<!-- STALE ALERT -->
{render_stale_alert(agents)}

<!-- AGENT GROUPS -->
{''.join(render_site_section(site, agents_by_site[site]) for site in sorted(agents_by_site.keys()))}

<!-- FOOTER -->
<tr><td style="padding:24px 40px 32px;border-top:1px solid #f1f5f9">
  <p style="margin:0;color:#94a3b8;font-size:12px;line-height:1.6">
    Generated {now().strftime('%Y-%m-%d %H:%M UTC')} ·
    <a href="https://agents.happysky-24190067.eastus.azurecontainerapps.io" style="color:#475569;text-decoration:none">View dashboard</a>
    <br>
    <span style="color:#cbd5e1">Sent daily at 7:00 AM Detroit · automation@northernsoftwareconsulting.com</span>
  </p>
</td></tr>

</table>

</td></tr>
</table>
</body>
</html>"""


def render_site_kpi_cards(agents: list[dict]) -> str:
    """Two big cards (one per site) showing the headline conversion + traffic
    metrics. Stack on mobile via class gt-kpi-card. Numbers are big, units
    are small + grey, sparklines underneath."""
    SITE_TRACKERS = {
        "aisleprompt-site-goals-tracker": {"label": "AislePrompt", "accent": "#4f46e5", "kpi_goal_id": "goal-instacart-cart-30d", "kpi_label": "Instacart cart creates"},
        "specpicks-site-goals-tracker":   {"label": "SpecPicks", "accent": "#f59e0b", "kpi_goal_id": "goal-amazon-clicks-30d", "kpi_label": "Amazon clicks"},
    }
    HEADLINE = [
        ("goal-organic-clicks-30d",      "Organic clicks (30d)"),
        ("goal-organic-impressions-30d", "Organic impressions (30d)"),
        ("goal-total-conversions-30d",   "Total conversions (30d)"),
        ("goal-indexed-pages-pct",       "Pages indexed by Google"),
    ]
    cards = []
    for agent_id, meta in SITE_TRACKERS.items():
        ag = next((a for a in agents if a["id"] == agent_id), None)
        if not ag:
            continue
        # Hero KPI (the big number at the top of the card)
        hero = next((g for g in ag["goals"] if g["id"] == meta["kpi_goal_id"]), None)
        hero_html = ""
        if hero:
            hero_value = fmt_value(hero["current"], hero["unit"])
            hero_html = f"""
            <div style="margin-bottom:16px">
              <div style="font-size:13px;color:#64748b;font-weight:500">{meta['kpi_label']} (30d)</div>
              <div style="font-size:32px;font-weight:700;color:#0f172a;line-height:1.1;margin-top:4px;letter-spacing:-0.02em">{hero_value}</div>
              <div style="margin-top:6px;font-size:13px">
                {fmt_trend(hero['trend_pct'], hero['direction'])} &nbsp;·&nbsp;
                <span style="color:#94a3b8">target {fmt_value(hero['target'], hero['unit'])}</span>
              </div>
            </div>"""
        # Smaller stats
        rows = []
        for gid, label in HEADLINE:
            g = next((g for g in ag["goals"] if g["id"] == gid), None)
            if not g:
                continue
            spark = render_sparkline(g["points"], width=88, height=24, direction=g["direction"])
            current_html = fmt_value(g['current'], g['unit'])
            rows.append(f"""
            <tr>
              <td style="padding:8px 0;border-top:1px solid #e2e8f0;color:#64748b;font-size:13px;width:55%">{label}</td>
              <td style="padding:8px 0;border-top:1px solid #e2e8f0;font-size:13px;font-weight:600;color:#0f172a;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap">{current_html}</td>
              <td style="padding:8px 0 8px 12px;border-top:1px solid #e2e8f0;text-align:right;width:90px">{spark}</td>
            </tr>""")
        cards.append(f"""
        <td valign="top" class="gt-kpi-card" style="width:50%;padding:0 6px">
          <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:20px 22px;border-top:3px solid {meta['accent']}">
            <div style="font-size:11px;color:{meta['accent']};font-weight:700;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:10px">{meta['label']}</div>
            {hero_html}
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse">
              {''.join(rows)}
            </table>
          </div>
        </td>""")
    if not cards:
        return ""
    return f"""
<tr><td style="padding:24px 40px 0">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" class="gt-kpi-grid" style="width:100%;border-collapse:separate;border-spacing:0">
    <tr>{''.join(cards)}</tr>
  </table>
</td></tr>"""


def render_stale_alert(agents: list[dict]) -> str:
    """Compact alert above the agent list. Dismisses to a green "all good"
    box if nothing's stale."""
    stale = [a for a in agents if a["agent_stale"]]
    if not stale:
        return f"""
<tr><td style="padding:24px 40px 0">
  <div style="background:#f0fdf4;border-left:3px solid #16a34a;padding:14px 18px;border-radius:6px">
    <div style="font-size:14px;color:#15803d;font-weight:600">All agents reporting fresh metrics</div>
    <div style="font-size:13px;color:#475569;margin-top:2px">No agents older than {STALE_HOURS}h.</div>
  </div>
</td></tr>"""
    rows = []
    for a in sorted(stale, key=lambda x: x["latest_ts_overall"] or ""):
        last_short = a["latest_ts_overall"][:10] if a["latest_ts_overall"] else "never"
        rows.append(f"""
        <tr>
          <td style="padding:6px 12px 6px 0;font-size:13px;color:#7c2d12;font-family:'SF Mono',Menlo,Consolas,monospace;font-weight:500">{a['id']}</td>
          <td style="padding:6px 12px;font-size:12px;color:#7c2d12;font-family:'SF Mono',Menlo,Consolas,monospace">{a['cron_expr'] or '—'}</td>
          <td style="padding:6px 0;font-size:12px;color:#7c2d12;text-align:right;white-space:nowrap">last: {last_short}</td>
        </tr>""")
    return f"""
<tr><td style="padding:24px 40px 0">
  <div style="background:#fef2f2;border-left:3px solid #dc2626;padding:16px 20px;border-radius:6px">
    <div style="font-size:14px;color:#991b1b;font-weight:600;margin-bottom:4px">{len(stale)} agent{'s' if len(stale)!=1 else ''} need investigation</div>
    <div style="font-size:13px;color:#475569;margin-bottom:12px">Latest metric older than {STALE_HOURS} hours — expected daily updates.</div>
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse">
      {''.join(rows)}
    </table>
  </div>
</td></tr>"""


def render_site_section(site: str, site_agents: list[dict]) -> str:
    """One section per site (specpicks / aisleprompt / shared). Heading with
    site name + count, then each agent as a row card."""
    site_agents = sorted(site_agents, key=lambda x: (not x["agent_stale"] is False,  # stale first? no — fresh first
                                                       -x["n_revenue_goals"], x["id"]))
    blocks = [render_agent_card(a) for a in site_agents]
    site_label = {"aisleprompt": "AislePrompt", "specpicks": "SpecPicks", "shared": "Shared / Cross-site"}.get(site, site.title())
    return f"""
<tr><td style="padding:32px 40px 0">
  <h2 style="margin:0 0 4px;font-size:13px;color:#64748b;letter-spacing:0.06em;text-transform:uppercase;font-weight:700">{site_label}</h2>
  <div style="margin:0 0 16px;font-size:13px;color:#94a3b8">{len(site_agents)} agent{'s' if len(site_agents)!=1 else ''}</div>
  {''.join(blocks)}
</td></tr>"""


def render_agent_card(ag: dict) -> str:
    """Per-agent card: header row (id, badges, summary) + goals table."""
    badge_color = {
        "seo": "#4f46e5", "research": "#0891b2", "fleet": "#16a34a",
        "personal": "#a855f7", "ops": "#f59e0b", "misc": "#64748b",
    }.get(ag["category"] or "misc", "#64748b")

    # Status pill
    if ag["agent_stale"]:
        last = ag["latest_ts_overall"][:10] if ag["latest_ts_overall"] else "never"
        status_pill = f'<span style="background:#fef2f2;color:#991b1b;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:600;border:1px solid #fecaca;letter-spacing:0.02em">stale · last {last}</span>'
        bg_color = "#fffbfb"
        border_color = "#fecaca"
    else:
        status_pill = ''
        bg_color = "#ffffff"
        border_color = "#e2e8f0"

    cat_badge = f'<span style="background:{badge_color};color:#fff;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:600;letter-spacing:0.04em;text-transform:uppercase">{ag["category"] or "misc"}</span>'

    # Goal rows — sort revenue/accomplishment first
    goals_sorted = sorted(ag["goals"], key=lambda g: (not g["is_revenue_goal"], g["status"] != "active", g["id"]))
    goal_rows = []
    for i, g in enumerate(goals_sorted):
        spark = render_sparkline(g["points"], width=110, height=24, direction=g["direction"])
        title = g["title"]
        markers = []
        if g["is_revenue_goal"]:
            markers.append('<span style="color:#f59e0b;font-size:11px;margin-right:4px;font-weight:600" title="Revenue goal">★</span>')
        if g["status"] == "accomplished":
            title = f'<span style="text-decoration:line-through;color:#94a3b8">{title}</span>'
            markers.append('<span style="color:#16a34a;margin-left:4px;font-size:13px" title="Accomplished">✓</span>')

        # Color-code current value: bold dark; stale = grey
        current_str = fmt_value(g['current'], g['unit'])
        baseline_str = fmt_value(g['baseline'], g['unit']) if g['baseline'] is not None else '<span style="color:#cbd5e1">—</span>'
        target_str = fmt_value(g['target'], g['unit']) if g['target'] is not None else '<span style="color:#cbd5e1">—</span>'

        border_top = "border-top:1px solid #f1f5f9;" if i > 0 else ""
        goal_rows.append(f"""
        <tr>
          <td style="padding:10px 12px 10px 0;{border_top}font-size:13px;line-height:1.4;color:#0f172a;vertical-align:top">
            {''.join(markers)}{title}
          </td>
          <td style="padding:10px 8px;{border_top}font-size:13px;color:#64748b;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;vertical-align:top">{baseline_str}</td>
          <td style="padding:10px 8px;{border_top}font-size:13px;font-weight:600;color:#0f172a;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;vertical-align:top">{current_str}</td>
          <td class="gt-hide-mobile" style="padding:10px 8px;{border_top}font-size:13px;color:#64748b;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;vertical-align:top">{target_str}</td>
          <td class="gt-hide-mobile" style="padding:10px 8px;{border_top}font-size:13px;text-align:right;white-space:nowrap;vertical-align:top">{fmt_trend(g['trend_pct'], g['direction'])}</td>
          <td class="gt-hide-mobile" style="padding:10px 0 10px 8px;{border_top}text-align:right;vertical-align:top">{spark}</td>
        </tr>""")

    return f"""
<div style="background:{bg_color};border:1px solid {border_color};border-radius:10px;padding:18px 22px;margin-bottom:14px">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;margin-bottom:10px">
    <tr>
      <td style="vertical-align:middle">
        <span style="font-family:'SF Mono',Menlo,Consolas,monospace;font-size:14px;font-weight:600;color:#0f172a">{ag['id']}</span>
      </td>
      <td style="vertical-align:middle;text-align:right;white-space:nowrap">
        {status_pill} {cat_badge}
      </td>
    </tr>
  </table>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" class="gt-agent-table" style="width:100%;border-collapse:collapse">
    <thead>
      <tr>
        <th style="padding:6px 12px 6px 0;text-align:left;font-size:11px;color:#94a3b8;font-weight:600;letter-spacing:0.04em;text-transform:uppercase;font-weight:600">Goal</th>
        <th style="padding:6px 8px;text-align:right;font-size:11px;color:#94a3b8;letter-spacing:0.04em;text-transform:uppercase;font-weight:600">Baseline</th>
        <th style="padding:6px 8px;text-align:right;font-size:11px;color:#94a3b8;letter-spacing:0.04em;text-transform:uppercase;font-weight:600">Current</th>
        <th class="gt-hide-mobile" style="padding:6px 8px;text-align:right;font-size:11px;color:#94a3b8;letter-spacing:0.04em;text-transform:uppercase;font-weight:600">Target</th>
        <th class="gt-hide-mobile" style="padding:6px 8px;text-align:right;font-size:11px;color:#94a3b8;letter-spacing:0.04em;text-transform:uppercase;font-weight:600">Trend</th>
        <th class="gt-hide-mobile" style="padding:6px 0 6px 8px;text-align:right;font-size:11px;color:#94a3b8;letter-spacing:0.04em;text-transform:uppercase;font-weight:600">30d</th>
      </tr>
    </thead>
    <tbody>{''.join(goal_rows)}</tbody>
  </table>
</div>"""


def _UNUSED_render_site_kpi_rollup(agents: list[dict]) -> str:
    """Highlight the top site-level KPI metrics across both sites' site-goals-tracker agents."""
    SITE_TRACKERS = {
        "aisleprompt-site-goals-tracker": "AislePrompt",
        "specpicks-site-goals-tracker": "SpecPicks",
    }
    HEADLINE_GOALS = [
        ("goal-organic-clicks-30d", "Organic clicks (30d)"),
        ("goal-organic-impressions-30d", "Organic impressions (30d)"),
        ("goal-total-conversions-30d", "Total conversions (30d)"),
        ("goal-instacart-cart-30d", "Instacart cart creates (30d)"),
        ("goal-amazon-clicks-30d", "Amazon clicks (30d)"),
        ("goal-ebay-clicks-30d", "eBay clicks (30d)"),
        ("goal-indexed-pages-pct", "% indexed by Google"),
    ]
    cards = []
    for agent_id, site_label in SITE_TRACKERS.items():
        ag = next((a for a in agents if a["id"] == agent_id), None)
        if not ag:
            continue
        # Pull the highlight metrics
        rows = []
        for gid, label in HEADLINE_GOALS:
            g = next((g for g in ag["goals"] if g["id"] == gid), None)
            if not g or g["current"] is None:
                continue
            sparkline = render_sparkline(g["points"], width=80, height=22, direction=g["direction"])
            unit = g["unit"]
            rows.append(f"""
            <tr>
                <td style="padding:6px 12px 6px 0;color:#475569;font-size:.86rem;white-space:nowrap">{label}</td>
                <td style="padding:6px 12px;font-weight:600;color:#0f172a;font-size:.92rem;text-align:right">{fmt_value(g['current'], unit)}</td>
                <td style="padding:6px 12px;font-size:.85rem;text-align:right;white-space:nowrap">{fmt_trend(g['trend_pct'], g['direction'])}</td>
                <td style="padding:6px 0;text-align:right">{sparkline}</td>
            </tr>""")
        if rows:
            cards.append(f"""
            <div style="flex:1;min-width:380px;background:#f8fafc;border-radius:10px;padding:16px 20px;border-left:4px solid {'#4f46e5' if 'AislePrompt' in site_label else '#f59e0b'}">
                <div style="font-weight:700;font-size:1rem;color:#0f172a;margin-bottom:6px">{site_label}</div>
                <table style="border-collapse:collapse;width:100%">{''.join(rows)}</table>
            </div>""")
    if not cards:
        return ""
    return f"""
    <h2 style="margin:0 0 12px;font-size:1.1rem;color:#0f172a">Site KPI rollup</h2>
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px">
        {''.join(cards)}
    </div>"""


def _UNUSED_render_stale_section(agents: list[dict]) -> str:
    stale = [a for a in agents if a["agent_stale"]]
    if not stale:
        return f"""
        <div style="margin:24px 0;background:#dcfce7;border-left:4px solid #16a34a;padding:14px 18px;border-radius:6px">
            <b style="color:#15803d">✓ All agents reporting fresh metrics.</b>
            <span style="color:#475569;font-size:.88rem"> No agents flagged stale ({STALE_HOURS}h threshold).</span>
        </div>"""
    rows = []
    for a in sorted(stale, key=lambda x: x["latest_ts_overall"] or ""):
        last = a["latest_ts_overall"][:19] if a["latest_ts_overall"] else "(never)"
        cron = a["cron_expr"] or "(no cron)"
        rows.append(f"""
        <tr style="border-bottom:1px solid #fee2e2">
            <td style="padding:6px 12px 6px 0;font-family:ui-monospace,monospace;font-size:.82rem;color:#991b1b">{a['id']}</td>
            <td style="padding:6px 12px;color:#7c2d12;font-size:.85rem">{cron}</td>
            <td style="padding:6px 12px;color:#7c2d12;font-size:.85rem;font-family:ui-monospace,monospace">{last}</td>
            <td style="padding:6px 0;color:#7c2d12;font-size:.85rem;text-align:right">{a['n_goals']} goal(s)</td>
        </tr>""")
    return f"""
    <div style="margin:24px 0;background:#fef2f2;border-left:4px solid #dc2626;padding:14px 18px;border-radius:6px">
        <b style="color:#991b1b">⚠ {len(stale)} agent(s) flagged for investigation</b>
        <span style="color:#7c2d12;font-size:.88rem"> — latest metric older than {STALE_HOURS}h, expected daily updates.</span>
        <table style="margin-top:10px;width:100%;border-collapse:collapse">
            <thead>
                <tr style="border-bottom:2px solid #fecaca">
                    <th style="padding:6px 12px 6px 0;text-align:left;font-size:.78rem;color:#7c2d12;text-transform:uppercase;letter-spacing:.04em">Agent</th>
                    <th style="padding:6px 12px;text-align:left;font-size:.78rem;color:#7c2d12;text-transform:uppercase;letter-spacing:.04em">Cron</th>
                    <th style="padding:6px 12px;text-align:left;font-size:.78rem;color:#7c2d12;text-transform:uppercase;letter-spacing:.04em">Latest report</th>
                    <th style="padding:6px 0;text-align:right;font-size:.78rem;color:#7c2d12;text-transform:uppercase;letter-spacing:.04em">Goals</th>
                </tr>
            </thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>"""


def _UNUSED_render_agent_block(ag: dict) -> str:
    badge_color = {"seo": "#4f46e5", "research": "#0891b2", "fleet": "#16a34a",
                   "personal": "#a855f7", "ops": "#f59e0b", "misc": "#64748b"}.get(ag["category"] or "misc", "#64748b")
    badge = f'<span style="background:{badge_color};color:#fff;padding:2px 8px;border-radius:10px;font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em">{ag["category"] or "misc"}</span>'
    stale_chip = ""
    if ag["agent_stale"]:
        last = ag["latest_ts_overall"][:10] if ag["latest_ts_overall"] else "never"
        stale_chip = f'<span style="background:#fef2f2;color:#991b1b;padding:2px 8px;border-radius:10px;font-size:.7rem;font-weight:600;border:1px solid #fecaca">⚠ stale (last: {last})</span>'

    goal_rows = []
    for g in sorted(ag["goals"], key=lambda x: (not x["is_revenue_goal"], x["id"])):
        spark = render_sparkline(g["points"], width=100, height=24, direction=g["direction"])
        title_html = g["title"]
        if g["is_revenue_goal"]:
            title_html = f'💰 {title_html}'
        if g["status"] == "accomplished":
            title_html = f'<span style="text-decoration:line-through;color:#94a3b8">{title_html}</span> ✓'
        n_pts = g["n_points"]
        pts_label = f'{n_pts} pt' if n_pts == 1 else f'{n_pts} pts'
        goal_rows.append(f"""
        <tr>
            <td style="padding:6px 12px 6px 0;font-size:.85rem;color:#0f172a;line-height:1.3">{title_html}</td>
            <td style="padding:6px 8px;font-size:.85rem;color:#475569;text-align:right;white-space:nowrap;font-family:ui-monospace,monospace">{fmt_value(g['baseline'], g['unit'])}</td>
            <td style="padding:6px 8px;font-size:.9rem;font-weight:600;color:#0f172a;text-align:right;white-space:nowrap;font-family:ui-monospace,monospace">{fmt_value(g['current'], g['unit'])}</td>
            <td style="padding:6px 8px;font-size:.85rem;color:#475569;text-align:right;white-space:nowrap;font-family:ui-monospace,monospace">{fmt_value(g['target'], g['unit'])}</td>
            <td style="padding:6px 8px;text-align:right;white-space:nowrap">{fmt_trend(g['trend_pct'], g['direction'])}</td>
            <td style="padding:6px 8px;text-align:right;white-space:nowrap">{fmt_progress(g['current'], g['target'], g['direction'])}</td>
            <td style="padding:6px 8px;text-align:right">{spark}</td>
            <td style="padding:6px 0;text-align:right;font-size:.75rem;color:#94a3b8">{pts_label}</td>
        </tr>""")

    return f"""
    <div style="margin:14px 0;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;{'background:#fef2f2;border-color:#fecaca;' if ag['agent_stale'] else 'background:#fff;'}">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
            <code style="font-family:ui-monospace,monospace;font-size:.95rem;font-weight:600;color:#0f172a">{ag['id']}</code>
            {badge}
            {stale_chip}
            <span style="color:#94a3b8;font-size:.78rem;margin-left:auto">
                {ag['n_goals']} goal{'s' if ag['n_goals']!=1 else ''}
                {f"· {ag['n_revenue_goals']} 💰" if ag['n_revenue_goals'] else ''}
                {f"· {ag['n_accomplished']} ✓" if ag['n_accomplished'] else ''}
            </span>
        </div>
        <table style="border-collapse:collapse;width:100%">
            <thead>
                <tr style="border-bottom:1px solid #f1f5f9">
                    <th style="padding:4px 12px 4px 0;text-align:left;font-size:.7rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em">Goal</th>
                    <th style="padding:4px 8px;text-align:right;font-size:.7rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em">Baseline</th>
                    <th style="padding:4px 8px;text-align:right;font-size:.7rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em">Current</th>
                    <th style="padding:4px 8px;text-align:right;font-size:.7rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em">Target</th>
                    <th style="padding:4px 8px;text-align:right;font-size:.7rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em">Trend</th>
                    <th style="padding:4px 8px;text-align:right;font-size:.7rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em">Progress</th>
                    <th style="padding:4px 8px;text-align:right;font-size:.7rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em">Spark</th>
                    <th style="padding:4px 0;text-align:right;font-size:.7rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em">N</th>
                </tr>
            </thead>
            <tbody>{''.join(goal_rows)}</tbody>
        </table>
    </div>"""


# ---------------------------------------------------------------------------
# Email send
# ---------------------------------------------------------------------------

def send_email(html: str, to_addr: str, from_addr: str, subject: str) -> bool:
    """Send via Microsoft Graph API (preferred — tenant-level SMTP auth is
    disabled). Falls back to msmtp[automation] if Graph fails, then
    msmtp[personal] if that also fails. Each path tries to send AS the
    configured FROM_EMAIL (automation@nsc).
    """
    # Path 1: Graph /users/{mailbox}/sendMail with shared-mailbox scope
    if Path(GRAPH_OAUTH_FILE).is_file():
        try:
            return _send_via_graph(html, to_addr, from_addr, subject)
        except Exception as e:
            err(f"[goals-tracker] Graph send failed: {e} — falling back to msmtp")

    # Path 2: msmtp[automation] (XOAUTH2 — broken at the M365 tenant level today
    # but kept as a fallback in case it gets re-enabled)
    if _send_via_msmtp(html, to_addr, from_addr, subject, account=MSMTP_ACCOUNT):
        return True

    # Path 3: last-resort personal Gmail (visible reroute — From: header
    # still says automation@nsc but envelope sender is perrymb@gmail.com)
    err("[goals-tracker] falling back to msmtp[personal] — From header preserved as automation@nsc")
    return _send_via_msmtp(html, to_addr, from_addr, subject, account="personal")


def _send_via_graph(html: str, to_addr: str, from_addr: str, subject: str) -> bool:
    """POST /users/{from_addr}/sendMail with the digest as HTML body."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mint_token",
        str(REPO / "agents" / "responder-agent" / "mint-token.py"),
    )
    mt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mt)
    token, _user, _prov = mt.mint_access_token(
        Path(GRAPH_OAUTH_FILE),
        scope_override="offline_access https://graph.microsoft.com/Mail.Send.Shared",
    )
    import json as _j, urllib.request as _ur
    body = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": to_addr}}],
            "from": {"emailAddress": {"address": from_addr}},
            "sender": {"emailAddress": {"address": from_addr}},
        },
        "saveToSentItems": "false",
    }
    url = (
        f"https://graph.microsoft.com/v1.0/users/"
        f"{urllib_quote(from_addr)}/sendMail"
    )
    req = _ur.Request(
        url, method="POST", data=_j.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    _ur.urlopen(req, timeout=30).read()
    err(f"[goals-tracker] sent to {to_addr} as {from_addr} via Graph API")
    return True


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


def _send_via_msmtp(html: str, to_addr: str, from_addr: str, subject: str, *, account: str) -> bool:
    import email.message, uuid
    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4()}@goals-tracker>"
    msg.set_content("Your email client doesn't support HTML. Open the attached digest.html.")
    msg.add_alternative(html, subtype="html")
    try:
        proc = subprocess.run(
            ["msmtp", "-a", account, to_addr],
            input=msg.as_bytes(), capture_output=True, timeout=60,
        )
        if proc.returncode != 0:
            err(f"[goals-tracker] msmtp[{account}] failed (rc={proc.returncode}): "
                f"{proc.stderr.decode('utf-8','replace')[:200]}")
            return False
        err(f"[goals-tracker] sent to {to_addr} via msmtp[{account}]")
        return True
    except FileNotFoundError:
        err("[goals-tracker] msmtp not installed")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--no-email", action="store_true", help="render only, don't send")
    p.add_argument("--out", default=str(HERE / "last-digest.html"),
                   help="where to write a copy of the rendered HTML")
    p.add_argument("--to", default=TO_EMAIL)
    args = p.parse_args()

    err(f"[goals-tracker] collecting goals + metrics from registry…")
    agents = collect_all_agents()
    err(f"[goals-tracker] {len(agents)} agents with at least 1 goal")

    n_total = len(agents)
    n_goals = sum(a["n_goals"] for a in agents)
    n_stale = sum(1 for a in agents if a["agent_stale"])

    html = render_email_html(agents)

    # Save a copy for inspection
    try:
        Path(args.out).write_text(html)
        err(f"[goals-tracker] wrote digest to {args.out} ({len(html)} chars)")
    except Exception as e:
        err(f"[goals-tracker] could not write {args.out}: {e}")

    subject = SUBJECT_TPL.format(
        date=now().strftime("%Y-%m-%d"),
        n_agents=n_total, n_goals=n_goals, n_stale=n_stale,
    )

    if args.no_email:
        err(f"[goals-tracker] --no-email: skipped sending. Subject would be: {subject}")
        print(f"OK render-only: {n_total} agents, {n_goals} goals, {n_stale} stale")
        return

    ok = send_email(html, args.to, FROM_EMAIL, subject)
    if ok:
        print(f"OK sent: {n_total} agents, {n_goals} goals, {n_stale} stale")
    else:
        print(f"FAIL send: {n_total} agents, {n_goals} goals, {n_stale} stale")
        sys.exit(1)


if __name__ == "__main__":
    main()
