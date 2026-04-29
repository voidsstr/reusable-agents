"""Shared email-template primitives for the reusable-agents framework.

The visual reference is the SEO recommendations email — clean
white-on-light-gray, borderless 780px column, monospace request-id chip,
mode badge in the header, status callout boxes by severity. This module
exposes the building blocks; each agent's email module composes them
into its own message.

Why this exists:
  Before: 4+ senders (seo-reporter, completion_email, agent-doctor
  weekly-digest, claude-pool outage email, render_recs_email in shared/
  site_quality.py) each rolled their own HTML — gradual visual drift.
  After: shared header/footer/section primitives so every framework
  email looks like a member of the same family.

Design tokens:
  Color tokens follow the Tailwind slate palette already used in the
  dashboard UI so the emails feel consistent with the dashboard:
    ink-900 #0f172a  ink-800 #1e293b  ink-500 #64748b  ink-300 #cbd5e1
    bg-page #ffffff  bg-subtle #f1f5f9  bg-callout #f8fafc
    accent-50 #eff6ff  accent-700 #1d4ed8
    success-bg #ecfdf5  success-border #6ee7b7  success-fg #065f46
    warn-bg #fef3c7    warn-border #fde68a    warn-fg #92400e
    error-bg #fee2e2   error-border #fecaca   error-fg #991b1b

Public functions:
  page(title_label, sublabel, request_id, mode, body_parts)
    -> wrap one or more body_parts in the standard <html> envelope.
  header_bar(title_label, sublabel="", mode="", run_id="")
    -> top of every email
  request_id_chip(request_id)
    -> the small monospace box that identifies the run for replies
  callout(severity, title, body_html)
    -> tinted box (severity ∈ "success"/"info"/"warn"/"error"/"neutral")
  table(headers, rows, *, zebra=False, monospace_first=False)
    -> standardized table; rows is a list[list[str|html]]
  section_h2(text, color=None)
    -> standard subhead
  metric_row(label, value, *, mono=False)
    -> for the "Run details" trailing block

Caller flow:

    from framework.core.email_templates import page, header_bar, callout, table

    parts = [
        header_bar("SEO agent — AislePrompt", sublabel="aisleprompt.com",
                   mode="recommend", run_id="20260429T133000Z"),
        callout("success", "12 recommendations auto-queued",
                "Reply only to override (defer/skip/revert)."),
        section_h2("Recommendations"),
        table(
            ["ID", "Priority", "Recommendation", "Expected impact"],
            rows,
            monospace_first=True,
        ),
    ]
    html = page(title="SEO agent — AislePrompt", request_id=req_id,
                body_parts=parts)
"""
from __future__ import annotations

from typing import Iterable, Optional


# Color tokens — match the dashboard's Tailwind slate palette.
INK_900 = "#0f172a"
INK_800 = "#1e293b"
INK_700 = "#334155"
INK_600 = "#475569"
INK_500 = "#64748b"
INK_400 = "#94a3b8"
INK_300 = "#cbd5e1"
INK_200 = "#e2e8f0"
INK_100 = "#f1f5f9"
INK_50 = "#f8fafc"
ACCENT_50 = "#eff6ff"
ACCENT_700 = "#1d4ed8"
SUCCESS_BG = "#ecfdf5"
SUCCESS_BORDER = "#6ee7b7"
SUCCESS_FG = "#065f46"
WARN_BG = "#fef3c7"
WARN_BORDER = "#fde68a"
WARN_FG = "#92400e"
ERROR_BG = "#fee2e2"
ERROR_BORDER = "#fecaca"
ERROR_FG = "#991b1b"
INFO_BG = "#dbeafe"
INFO_BORDER = "#bfdbfe"
INFO_FG = "#1e40af"


_FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif"


def page(*, title: str, request_id: str = "", body_parts: Iterable[str]) -> str:
    """Wrap body parts in the standard email envelope.

    Returns a complete <html> document. Single-column 780px max width,
    safe-area inset top/bottom padding so it renders well in Gmail/Outlook
    + iOS mail without horizontal scroll.
    """
    body_html = "\n".join(body_parts)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
</head>
<body style="margin:0;padding:0;background:{INK_100};font-family:{_FONT};color:{INK_900};line-height:1.5">
<div style="max-width:780px;margin:0 auto;background:#ffffff;padding:24px;border-left:1px solid {INK_200};border-right:1px solid {INK_200}">
{body_html}
</div>
</body>
</html>"""


def header_bar(title: str, *, sublabel: str = "", mode: str = "",
               run_id: str = "") -> str:
    """Top of every email — title, optional small subtitle, mode badge,
    run id badge. Mode badge is amber for 'recommend' and green for
    'auto-implement' to match the dashboard's pill colors."""
    parts = [
        f"<h1 style='color:{INK_900};border-bottom:1px solid {INK_200};padding-bottom:12px;margin:0 0 12px 0;font-size:22px;font-weight:600'>"
        f"{_html_escape(title)}"
        + (f" <span style='color:{INK_500};font-weight:normal;font-size:14px'>({_html_escape(sublabel)})</span>" if sublabel else "")
        + "</h1>"
    ]
    chips = []
    if mode:
        bg = WARN_BG if mode == "recommend" else SUCCESS_BG
        fg = WARN_FG if mode == "recommend" else SUCCESS_FG
        chips.append(
            f"<span style='display:inline-block;background:{bg};color:{fg};font-size:11px;padding:3px 8px;border-radius:3px;margin-right:6px'>"
            f"<b>Mode:</b> {_html_escape(mode)}</span>"
        )
    if run_id:
        chips.append(
            f"<span style='display:inline-block;background:{INK_50};color:{INK_700};font-size:11px;padding:3px 8px;border-radius:3px;margin-right:6px;font-family:monospace;border:1px solid {INK_200}'>"
            f"<b>Run:</b> {_html_escape(run_id)}</span>"
        )
    if chips:
        parts.append(f"<div style='margin-bottom:14px'>{''.join(chips)}</div>")
    return "\n".join(parts)


def request_id_chip(request_id: str) -> str:
    """Small monospace identifier the operator can quote-cite when
    replying. Matches the SEO email's request-id box."""
    if not request_id:
        return ""
    return (
        f"<div style='display:inline-block;font-size:11px;color:{INK_500};"
        f"background:{INK_100};border:1px solid {INK_200};border-radius:4px;"
        f"padding:4px 10px;margin-bottom:12px;font-family:monospace'>"
        f"<b>Request id:</b> <code style='color:{INK_900}'>{_html_escape(request_id)}</code>"
        f"</div>"
    )


_CALLOUT_PALETTE = {
    "success": (SUCCESS_BG, SUCCESS_BORDER, SUCCESS_FG),
    "info":    (INFO_BG, INFO_BORDER, INFO_FG),
    "warn":    (WARN_BG, WARN_BORDER, WARN_FG),
    "warning": (WARN_BG, WARN_BORDER, WARN_FG),
    "error":   (ERROR_BG, ERROR_BORDER, ERROR_FG),
    "danger":  (ERROR_BG, ERROR_BORDER, ERROR_FG),
    "neutral": (INK_50, INK_200, INK_700),
}


def callout(severity: str, title: str, body_html: str = "") -> str:
    """Tinted box — header line + optional body. Severity drives color."""
    bg, border, fg = _CALLOUT_PALETTE.get(severity, _CALLOUT_PALETTE["neutral"])
    title_html = f"<b>{_html_escape(title)}</b>" if title else ""
    body = f"<br><span style='color:{fg};font-size:12px;line-height:1.5'>{body_html}</span>" if body_html else ""
    return (
        f"<div style='background:{bg};border-left:3px solid {border};color:{fg};"
        f"padding:14px 16px;margin:12px 0;font-size:13px;line-height:1.5;border-radius:4px'>"
        f"{title_html}{body}"
        f"</div>"
    )


def section_h2(text: str, color: str = None) -> str:
    color = color or INK_700
    return (
        f"<h2 style='margin:24px 0 8px 0;font-size:15px;font-weight:600;"
        f"color:{color};border-bottom:1px solid {INK_200};padding-bottom:6px'>"
        f"{_html_escape(text)}</h2>"
    )


def table(headers: list[str], rows: list[list[str]], *,
          zebra: bool = False, monospace_first: bool = False) -> str:
    """Standardized table. Cells may contain pre-rendered HTML.
    `monospace_first=True` mono-fies the first column (rec ids etc.)."""
    th = "".join(
        f"<th style='text-align:left;padding:8px;border:1px solid {INK_200};"
        f"background:{INK_50};color:{INK_700};font-size:12px;font-weight:600'>"
        f"{_html_escape(h)}</th>"
        for h in headers
    )
    body_rows = []
    for i, row in enumerate(rows):
        bg = (INK_50 if (zebra and i % 2 == 1) else "#ffffff")
        cells = []
        for j, cell in enumerate(row):
            mono = monospace_first and j == 0
            style = f"padding:8px;border:1px solid {INK_200};font-size:13px;color:{INK_800};background:{bg}"
            if mono:
                style += ";font-family:monospace"
            cells.append(f"<td style='{style}'>{cell}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        f"<table style='border-collapse:collapse;width:100%;font-size:13px;margin:6px 0'>"
        f"<thead><tr>{th}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        f"</table>"
    )


def metric_row(label: str, value: str, *, mono: bool = False) -> str:
    """For the 'Run details' style key-value list at the bottom of an
    email. Caller wraps these in a <table>."""
    val_style = "font-family:monospace" if mono else ""
    return (
        f"<tr>"
        f"<td style='padding:4px 12px 4px 0;color:{INK_500};font-size:12px'>{_html_escape(label)}</td>"
        f"<td style='padding:4px 0;color:{INK_900};font-size:13px;{val_style}'>{value}</td>"
        f"</tr>"
    )


def footer_dashboard_link(dashboard_base: str, agent_id: str) -> str:
    """Standard 'View in dashboard' link."""
    if not dashboard_base or not agent_id:
        return ""
    href = f"{dashboard_base.rstrip('/')}/agents/{agent_id}"
    return (
        f"<div style='color:{INK_500};font-size:12px;margin-top:18px;border-top:1px solid {INK_200};padding-top:12px'>"
        f"<a href='{href}' style='color:{ACCENT_700};text-decoration:none'>"
        f"View this agent in the dashboard →</a>"
        f"</div>"
    )


def priority_chip(priority: str) -> str:
    """Render a priority/severity as a colored chip."""
    p = (priority or "").lower()
    palette = {
        "critical": (ERROR_BG, ERROR_FG),
        "must":     (ERROR_BG, ERROR_FG),
        "high":     ("#fff7ed", "#9a3412"),  # orange
        "medium":   (WARN_BG, WARN_FG),
        "should":   (WARN_BG, WARN_FG),
        "low":      (INFO_BG, INFO_FG),
        "could":    (INFO_BG, INFO_FG),
    }
    bg, fg = palette.get(p, (INK_100, INK_700))
    return (
        f"<span style='background:{bg};color:{fg};font-size:10px;"
        f"font-weight:600;padding:2px 8px;border-radius:3px;text-transform:uppercase'>"
        f"{_html_escape(p)}</span>"
    )


def _html_escape(s: str) -> str:
    """Lightweight HTML escape — we don't want to depend on `html` here
    since most callers pass pre-rendered fragments. Apply this only to
    plain user-provided strings (titles, labels)."""
    if s is None:
        return ""
    return (str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;"))
