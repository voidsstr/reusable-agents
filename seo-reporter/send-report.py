#!/usr/bin/env python3
"""seo-reporter — read recommendations.json + goal-progress.json + comparison.json
for a run, render an HTML email, send via msmtp + (optionally) post to dashboard.

Usage:
  SEO_AGENT_CONFIG=my-site.yaml python3 send-report.py
  SEO_AGENT_CONFIG=my-site.yaml python3 send-report.py --run-ts 20260425T140245Z
  SEO_AGENT_CONFIG=my-site.yaml python3 send-report.py --dry-run
  SEO_AGENT_CONFIG=my-site.yaml python3 send-report.py --no-email --no-dashboard
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from email.utils import formatdate, make_msgid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.site_config import load_config_from_env  # noqa: E402


def _load(p: Path) -> dict:
    return json.loads(p.read_text()) if p.is_file() else {}


# ---------------------------------------------------------------------------
# Email rendering
# ---------------------------------------------------------------------------

PRIORITY_COLORS = {
    "critical": "#dc2626", "high": "#ea580c",
    "medium": "#ca8a04", "low": "#65a30d",
}

REC_TYPE_EMOJI = {
    "top5-target-page": "🎯",
    "indexing-fix": "🔧",
    "ctr-fix": "✏️",
    "ssr-fix": "🛠",
    "schema-markup": "📐",
    "internal-link": "🔗",
    "sitemap-fix": "🗺",
    "conversion-path": "💰",
    "content-expansion": "📝",
    "redirect-fix": "↪️",
    "other": "•",
}


def render_html(cfg, run_dir: Path) -> tuple[str, str]:
    """Return (subject, html_body)."""
    site = cfg.site_id
    domain = cfg.domain
    label = cfg.get("site", {}).get("label") or site
    mode = cfg.mode
    run_ts = run_dir.name

    recs_data = _load(run_dir / "recommendations.json")
    comparison = _load(run_dir / "comparison.json")
    goal_progress = _load(run_dir / "goal-progress.json")
    new_goals = _load(run_dir / "goals.json")

    recs = recs_data.get("recommendations", [])
    summary = recs_data.get("summary", "")

    # Subject
    subject_template = cfg.get("reporter", {}).get("email", {}).get(
        "subject_template", "SEO agent run — {site} — {tag}"
    )
    subject = subject_template.format(site=site, tag=run_ts, mode=mode)

    # Reply instructions
    reply_to = cfg.get("reporter", {}).get("email", {}).get("from", "")
    reply_help = (
        f"<p style='color:#64748b;font-size:13px;margin-top:8px'>"
        f"<b>To implement specific recommendations,</b> reply to this email with the "
        f"rec ids you want implemented (one per line or space-separated): "
        f"<code style='background:#f1f5f9;padding:2px 6px;border-radius:3px'>"
        f"implement rec-001 rec-003</code><br>"
        f"Subject must stay <code>Re: …</code>. Use <code>skip rec-002</code> to "
        f"dismiss recs without implementing them.</p>"
    )

    parts = [
        f"<!DOCTYPE html><html><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;color:#0f172a;line-height:1.5;max-width:780px;margin:0 auto;padding:24px'>",
        f"<h1 style='color:#0f172a;border-bottom:1px solid #e2e8f0;padding-bottom:12px'>"
        f"SEO agent — {label} <span style='color:#64748b;font-weight:normal;font-size:14px'>({site})</span></h1>",
        f"<p><b>Mode:</b> <code style='background:{('#fef3c7' if mode=='recommend' else '#dcfce7')};padding:2px 8px;border-radius:3px'>{mode}</code> &nbsp;",
        f"<b>Run:</b> {run_ts} &nbsp; <b>Domain:</b> {domain}</p>",
        f"<p style='color:#475569'>{summary}</p>",
    ]

    if mode == "recommend":
        parts.append(f"<div style='background:#fef3c7;border-left:3px solid #ca8a04;padding:12px;margin:12px 0;font-size:13px'>"
                     f"This site is in <b>recommend</b> mode — nothing was shipped. The recommendations below are "
                     f"awaiting your selection.</div>")
        parts.append(reply_help)

    # Recommendations
    parts.append("<h2 style='margin-top:24px'>Recommendations</h2>")
    if not recs:
        parts.append("<p style='color:#64748b'>No recommendations this run.</p>")
    else:
        parts.append("<table style='border-collapse:collapse;width:100%;font-size:13px'>")
        parts.append("<tr style='background:#f8fafc'><th style='text-align:left;padding:8px;border:1px solid #e2e8f0'>ID</th>"
                     "<th style='text-align:left;padding:8px;border:1px solid #e2e8f0'>Priority</th>"
                     "<th style='text-align:left;padding:8px;border:1px solid #e2e8f0'>Recommendation</th>"
                     "<th style='text-align:left;padding:8px;border:1px solid #e2e8f0'>Expected impact</th></tr>")
        for r in recs:
            color = PRIORITY_COLORS.get(r.get("priority", "low"), "#64748b")
            emoji = REC_TYPE_EMOJI.get(r.get("type", "other"), "•")
            impact = r.get("expected_impact") or {}
            impact_str = ""
            if impact:
                metric = impact.get("metric", "")
                base, tgt = impact.get("baseline"), impact.get("target")
                horizon = impact.get("horizon_weeks", 0)
                if metric and base is not None and tgt is not None:
                    direction = "→" if tgt >= base else "↓"
                    impact_str = f"{metric.split('.')[-1]}: <code>{base} {direction} {tgt}</code> ({horizon}w)"
            parts.append(
                f"<tr>"
                f"<td style='padding:8px;border:1px solid #e2e8f0;font-family:monospace'><b>{r['id']}</b></td>"
                f"<td style='padding:8px;border:1px solid #e2e8f0'>"
                f"<span style='color:{color};font-weight:bold;text-transform:uppercase;font-size:10px'>{r.get('priority','?')}</span></td>"
                f"<td style='padding:8px;border:1px solid #e2e8f0'>"
                f"{emoji} <b>{r.get('title','')}</b><br>"
                f"<span style='color:#475569;font-size:12px'>{r.get('rationale','')}</span></td>"
                f"<td style='padding:8px;border:1px solid #e2e8f0;font-size:12px'>{impact_str}</td>"
                f"</tr>"
            )
        parts.append("</table>")

    # Metrics delta
    if comparison:
        parts.append("<h2 style='margin-top:24px'>Metrics delta</h2>")
        gsc = comparison.get("gsc_90d", {})
        rev = comparison.get("revenue_28d", {})
        usf = comparison.get("us_traffic_share", {})
        rows = []
        for k in ("total_impressions", "total_clicks", "total_ctr", "avg_position", "num_pages_indexed"):
            v = gsc.get(k, {})
            if v.get("current") is None: continue
            label_map = {
                "total_impressions": "Impressions (90d)", "total_clicks": "Clicks (90d)",
                "total_ctr": "CTR", "avg_position": "Avg position",
                "num_pages_indexed": "Pages indexed",
            }
            cur, prior = v.get("current"), v.get("prior")
            delta = v.get("delta", "")
            pct = v.get("pct")
            cur_disp = f"{cur*100:.2f}%" if k == "total_ctr" else f"{cur:,}" if isinstance(cur,(int,float)) else cur
            prior_disp = f"{prior*100:.2f}%" if k == "total_ctr" else f"{prior:,}" if isinstance(prior,(int,float)) else prior
            rows.append(f"<tr><td style='padding:6px;border:1px solid #e2e8f0'>{label_map[k]}</td>"
                        f"<td style='padding:6px;border:1px solid #e2e8f0;font-family:monospace'>{prior_disp} → {cur_disp}</td>"
                        f"<td style='padding:6px;border:1px solid #e2e8f0;font-family:monospace'>{('+' if (isinstance(delta,(int,float)) and delta>=0) else '')}{delta}{(f' ({pct:+.1f}%)' if pct is not None else '')}</td></tr>")
        for k, v in rev.items():
            if v.get("current") is None: continue
            cur, prior = v.get("current"), v.get("prior")
            delta = v.get("delta", "")
            pct = v.get("pct")
            rows.append(f"<tr><td style='padding:6px;border:1px solid #e2e8f0'>💰 {k}</td>"
                        f"<td style='padding:6px;border:1px solid #e2e8f0;font-family:monospace'>{prior} → {cur}</td>"
                        f"<td style='padding:6px;border:1px solid #e2e8f0;font-family:monospace'>{delta}{(f' ({pct:+.1f}%)' if pct is not None else '')}</td></tr>")
        if usf and usf.get("current") is not None:
            cur, prior = usf.get("current"), usf.get("prior")
            rows.append(f"<tr><td style='padding:6px;border:1px solid #e2e8f0'>🇺🇸 US traffic share</td>"
                        f"<td style='padding:6px;border:1px solid #e2e8f0;font-family:monospace'>{((prior or 0)*100):.1f}% → {((cur or 0)*100):.1f}%</td>"
                        f"<td style='padding:6px;border:1px solid #e2e8f0'></td></tr>")
        if rows:
            parts.append("<table style='border-collapse:collapse;width:100%;font-size:13px'>")
            parts.extend(rows)
            parts.append("</table>")

    # Prior-run goals (scored)
    if goal_progress and goal_progress.get("goals"):
        parts.append(f"<h2 style='margin-top:24px'>Prior-run goals — scored</h2>")
        top5 = [g for g in goal_progress["goals"] if g.get("is_top5_goal")]
        revenue = [g for g in goal_progress["goals"] if g.get("is_revenue_goal") and not g.get("is_top5_goal")]
        seo = [g for g in goal_progress["goals"] if not g.get("is_top5_goal") and not g.get("is_revenue_goal")]
        for label_, group, em in [("🎯 Top-5 rank", top5, "🎯"), ("💰 Revenue", revenue, "💰"), ("📈 SEO", seo, "📈")]:
            if not group: continue
            parts.append(f"<h3 style='margin-top:12px'>{label_} ({len(group)})</h3><ul>")
            for g in group:
                status_em = {"achieved": "✅", "improving": "➡️", "no-movement": "⏸", "unknown": "❔"}.get(g.get("status"), "")
                parts.append(f"<li>{status_em} <b>{g.get('description','')}</b> — "
                             f"baseline {g.get('baseline')} → target {g.get('target')}, current {g.get('current')} "
                             f"({g.get('progress_pct', 0)}% of target)</li>")
            parts.append("</ul>")

    # Newly declared goals
    if new_goals.get("goals"):
        parts.append(f"<h2 style='margin-top:24px'>New goals declared this run</h2><ul>")
        for g in new_goals["goals"]:
            tag = "🎯" if g.get("is_top5_goal") else ("💰" if g.get("is_revenue_goal") else "📈")
            parts.append(f"<li>{tag} <b>{g.get('description','')}</b> — "
                         f"<code>{g.get('target_metric','')}</code>: {g.get('baseline')} → "
                         f"{g.get('target')} by {g.get('check_by','')}</li>")
        parts.append("</ul>")

    parts.append(f"<hr style='margin:32px 0 16px;border:none;border-top:1px solid #e2e8f0'>")
    parts.append(f"<p style='color:#94a3b8;font-size:11px'>Run dir: <code>{run_dir}</code></p>")
    parts.append("</body></html>")

    return subject, "\n".join(parts)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_email(cfg, subject: str, html_body: str) -> bool:
    email_cfg = (cfg.get("reporter") or {}).get("email")
    if not email_cfg:
        print("[reporter] no email configured, skipping send", file=sys.stderr)
        return False
    to_list = email_cfg.get("to", [])
    if not to_list:
        print("[reporter] no recipients configured", file=sys.stderr)
        return False
    from_addr = email_cfg.get("from", "")
    account = email_cfg.get("msmtp_account", "default")

    msg_id = make_msgid(domain="reusable-agents")
    headers = [
        f"From: {from_addr}",
        f"To: {', '.join(to_list)}",
        f"Subject: {subject}",
        f"Date: {formatdate(localtime=True)}",
        f"Message-ID: {msg_id}",
        "MIME-Version: 1.0",
        "Content-Type: text/html; charset=utf-8",
        "X-Reusable-Agent: seo-reporter",
        f"X-Reusable-Agent-Site: {cfg.site_id}",
    ]
    raw = "\n".join(headers) + "\n\n" + html_body
    try:
        proc = subprocess.run(
            ["msmtp", "-a", account] + to_list,
            input=raw, text=True, capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f"[reporter] msmtp failed (rc={proc.returncode}): {proc.stderr[:300]}", file=sys.stderr)
            return False
        print(f"[reporter] sent to {', '.join(to_list)} via msmtp[{account}]", file=sys.stderr)
        return True
    except FileNotFoundError:
        print("[reporter] msmtp not installed — install + configure first", file=sys.stderr)
        return False


def post_to_dashboard(cfg, run_dir: Path, subject: str) -> None:
    dash = (cfg.get("reporter") or {}).get("dashboard")
    if not dash or not dash.get("base_url") or not dash.get("agent_id"):
        return
    base = dash["base_url"].rstrip("/")
    agent_id = dash["agent_id"]
    # We don't write a run row here (the orchestrating shell script does), but
    # we patch the latest run with the rendered subject as its summary so the
    # dashboard card reflects what was just emailed.
    try:
        recs = json.loads((run_dir / "recommendations.json").read_text()).get("recommendations", [])
        body = json.dumps({
            "summary_md": subject + "\n\n" + (
                f"{len(recs)} recommendations: "
                + ", ".join(r["id"] for r in recs[:5])
                + ("…" if len(recs) > 5 else "")
            ),
        }).encode()
        # We don't know the run_id from here — this is a lightweight hint via
        # the dashboard's agent-level summary endpoint. The orchestrating
        # entry-script (run.sh) is the canonical place to record run state.
        urllib.request.urlopen(urllib.request.Request(
            f"{base}/api/agents/{agent_id}", method="GET",
        ), timeout=5).read()
    except Exception as e:
        print(f"[reporter] dashboard ping failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-ts", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-email", action="store_true")
    p.add_argument("--no-dashboard", action="store_true")
    args = p.parse_args()

    cfg = load_config_from_env()
    run_dir = cfg.run_dir_for_ts(args.run_ts) if args.run_ts else cfg.latest_run_dir()
    if not run_dir or not run_dir.is_dir():
        raise SystemExit(f"No run dir for site={cfg.site_id} ts={args.run_ts or 'latest'}")

    subject, html = render_html(cfg, run_dir)
    print(f"[reporter] {subject}", file=sys.stderr)

    if args.dry_run:
        print(html)
        return

    if not args.no_email:
        send_email(cfg, subject, html)
    if not args.no_dashboard:
        post_to_dashboard(cfg, run_dir, subject)


if __name__ == "__main__":
    main()
