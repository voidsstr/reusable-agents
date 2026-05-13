"""Canonical email template — visual shell + composable sections.

Every agent in the framework that sends operator email goes through
this module so emails share one visual language. Two entry points:

  • `render_email(subject, sections, ...)` — generic email builder; takes
    a list of pre-rendered HTML sections and wraps them in the shared
    header/footer + reply-help block. Use for digests, status reports,
    confirmation emails, anything that's not a rec-list.

  • `render_recs_email(...)` (in shared.site_quality) — purpose-built
    for rec-list emails (the most common shape). Internally uses
    `render_shell()` from this module for the chrome.

Visual conventions (DO NOT diverge in agent-side code):
  • System-font stack, 14px base
  • Brand-neutral palette (slate ink + accent green)
  • Header: agent label + run timestamp + dashboard link
  • Optional sticky banner for confirmation gates
  • Recommendations table: rec-id (with rec_uid), badges, title, rationale
  • Reply-help: full syntax (rec ids, ranges, uids, bulk filters)
  • Footer: dashboard link + unsubscribe note + framework version

Rationale for centralizing here:
  • Visual consistency — operator scans one familiar layout
  • One place to fix accessibility, dark-mode, brand updates
  • One place to audit reply-syntax + uid surfacing
  • Per-deployment overrides via storage config (future) without
    touching agent code
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Tokens — ONE source of truth for the visual language
# ---------------------------------------------------------------------------

PALETTE = {
    "ink_900": "#0f172a",   # primary text
    "ink_700": "#334155",   # body text
    "ink_500": "#64748b",   # secondary text
    "ink_300": "#cbd5e1",   # divider
    "ink_100": "#f1f5f9",   # surface subtle
    "accent_700": "#15803d",   # primary accent (action)
    "accent_500": "#22c55e",
    "accent_50":  "#f0fdf4",
    "warn_500":   "#f59e0b",
    "danger_500": "#ef4444",
    "danger_50":  "#fef2f2",
    "white":      "#ffffff",
}

FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)


# ---------------------------------------------------------------------------
# Public API — agents call these
# ---------------------------------------------------------------------------

def render_shell(*,
                 title: str,
                 site_label: str = "",
                 agent_id: str = "",
                 run_ts: str = "",
                 dashboard_url: str = "",
                 banner_html: str = "",
                 sections: list[str],
                 reply_help_html: str = "",
                 footer_extra_html: str = "") -> str:
    """Wrap a list of HTML section strings in the canonical chrome.

    Use this directly when you need a non-rec email (digest summary,
    status alert, confirmation request). Use `render_recs_email`
    instead when you have a list of rec dicts.

    Args:
      title          shown in the header (e.g. "Daily catalog audit")
      site_label     site display name (e.g. "AislePrompt")
      agent_id       short agent id (shown in footer for debugging)
      run_ts         current run timestamp (shown in header)
      dashboard_url  optional link to the agent's dashboard page
      banner_html    optional sticky banner above the content
                     (e.g. confirmation-gate notice). Leave "" to skip.
      sections       pre-rendered HTML strings — concatenated in order
      reply_help_html  optional reply-syntax block (use
                       `render_reply_help()` to generate)
      footer_extra_html  optional per-agent footer addendum
    """
    p = PALETTE
    when = run_ts or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"""
    <header style="background:{p['ink_100']};border-bottom:1px solid {p['ink_300']};
                   padding:18px 24px;font-family:{FONT_STACK};color:{p['ink_900']}">
      <div style="font-size:18px;font-weight:600;line-height:1.3">{title}</div>
      <div style="font-size:13px;color:{p['ink_500']};margin-top:4px">
        {(site_label + ' · ') if site_label else ''}
        <span style="font-family:monospace;font-size:11px">{when}</span>
        {f'&nbsp;·&nbsp;<a href="{dashboard_url}" style="color:{p["accent_700"]};text-decoration:none">dashboard</a>' if dashboard_url else ''}
      </div>
    </header>
    """
    body_inner = "\n".join(sections)
    reply_block = (
        f'<div style="border-top:1px solid {p["ink_300"]};padding:18px 24px;'
        f'background:{p["ink_100"]};font-family:{FONT_STACK};font-size:13px;'
        f'color:{p["ink_700"]};line-height:1.6">{reply_help_html}</div>'
        if reply_help_html else ""
    )
    footer = f"""
    <footer style="border-top:1px solid {p['ink_300']};padding:14px 24px;
                   font-family:{FONT_STACK};font-size:11px;color:{p['ink_500']};
                   line-height:1.5">
      {footer_extra_html}
      <div style="margin-top:6px">
        <span style="font-family:monospace">{agent_id or 'reusable-agents'}</span>
        — sent by the reusable-agents framework. Reply only with the
        documented syntax above; other replies are ignored.
      </div>
    </footer>
    """
    return (
        f'<html><body style="margin:0;padding:0;background:{p["white"]};'
        f'font-family:{FONT_STACK};color:{p["ink_900"]};font-size:14px">'
        f'<div style="max-width:760px;margin:0 auto;background:{p["white"]};'
        f'border:1px solid {p["ink_300"]};border-radius:8px;overflow:hidden">'
        f'{header}'
        f'{banner_html}'
        f'<div style="padding:0">{body_inner}</div>'
        f'{reply_block}'
        f'{footer}'
        f'</div></body></html>'
    )


def render_section(*, heading: str = "",
                   subheading: str = "",
                   body_html: str = "",
                   accent: str = "") -> str:
    """One HTML section block — use as building blocks for `sections=`.

    Args:
      heading      bold title (e.g. "Findings", "Top 5 actions")
      subheading   optional dimmer subtitle below
      body_html    arbitrary HTML for the body
      accent       optional left-border colour (token name from PALETTE,
                   e.g. "accent_500" for green or "danger_500" for red)
    """
    p = PALETTE
    border = f"border-left:4px solid {p[accent]};" if accent and accent in p else ""
    return f"""
    <section style="padding:18px 24px;{border}">
      {f'<div style="font-size:15px;font-weight:600;color:{p["ink_900"]}">{heading}</div>' if heading else ''}
      {f'<div style="font-size:12px;color:{p["ink_500"]};margin-top:2px">{subheading}</div>' if subheading else ''}
      <div style="margin-top:{'10px' if heading else '0'};color:{p['ink_700']};line-height:1.55">{body_html}</div>
    </section>
    """


def render_kv_table(items: list[tuple[str, str]]) -> str:
    """Two-column key-value table — for status/digest emails."""
    p = PALETTE
    rows = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:{p["ink_500"]};'
        f'font-size:12px;vertical-align:top;white-space:nowrap">{k}</td>'
        f'<td style="padding:6px 0;color:{p["ink_900"]};font-size:13px">{v}</td></tr>'
        for k, v in items
    )
    return f'<table style="border-collapse:collapse;width:100%">{rows}</table>'


def render_reply_help(*,
                      shows_recs: bool = True,
                      auto_queued: bool = False) -> str:
    """Standard reply-syntax block. Used by every rec-list email.

    Two modes:
      • shows_recs=True, auto_queued=False  — full reply syntax
        (implement/skip/modify/merge by id, range, uid, bulk filter)
      • auto_queued=True                    — "already queued" notice
        with override-only instructions (defer/skip)
    """
    p = PALETTE
    chip = (
        f'background:{p["white"]};padding:2px 6px;border:1px solid '
        f'{p["ink_300"]};border-radius:3px;font-family:monospace;font-size:11px'
    )
    if not shows_recs:
        return ""
    if auto_queued:
        return (
            f'<b>✅ All recommendations have been auto-queued for implementation.</b>'
            f'<br><br>The implementer will pick them up shortly. Reply only to override:'
            f'<br><br>'
            f'<code style="{chip}">defer rec-001</code> &nbsp;'
            f'<code style="{chip}">skip rec-002 rec-005</code> &nbsp;'
            f'<code style="{chip}">revert rec-003</code>'
        )
    return (
        f'<b>Reply to ship recommendations.</b> Subject must stay <code>Re: …</code>.'
        f'<br><br>'
        f'<b>By rec id (most precise):</b><br>'
        f'<code style="{chip}">implement rec-001 rec-005</code> &nbsp;'
        f'<code style="{chip}">skip rec-002</code> &nbsp;'
        f'<code style="{chip}">implement rec-001 - rec-007</code>'
        f'<br><br>'
        f'<b>By globally-unique uid (unambiguous across emails):</b><br>'
        f'<code style="{chip}">implement r-a1b2c3d4</code>'
        f'<br><br>'
        f'<b>Bulk by tier or severity:</b><br>'
        f'<code style="{chip}">implement all</code> &nbsp;'
        f'<code style="{chip}">implement auto</code> &nbsp;'
        f'<code style="{chip}">implement high</code> &nbsp;'
        f'<code style="{chip}">implement critical and high</code>'
        f'<br><span style="color:{p["ink_500"]};font-size:11px">Tier filters: '
        f'<code>auto</code>, <code>review</code>, <code>experimental</code>. '
        f'Severity filters: <code>critical</code>, <code>high</code>, '
        f'<code>medium</code>, <code>low</code>. <code>all</code> matches '
        f'everything. Combine with <code>and</code> / commas.</span>'
    )


def render_email(*,
                 subject_inner: str,
                 agent_id: str,
                 request_id: str,
                 site_label: str = "",
                 run_ts: str = "",
                 dashboard_url: str = "",
                 banner_html: str = "",
                 sections: list[str],
                 reply_help_html: str = "",
                 footer_extra_html: str = "") -> tuple[str, str]:
    """Build (subject, html) for any email.

    Subject is prefixed with `[agent_id:request_id]` so the responder
    can route replies back via the existing subject-tag regex.
    Use this for non-rec emails. For rec lists, use the higher-level
    `shared.site_quality.render_recs_email` which composes a rec table
    on top of this shell.
    """
    subject = f"[{agent_id}:{request_id}] {subject_inner}"
    html = render_shell(
        title=subject_inner,
        site_label=site_label,
        agent_id=agent_id,
        run_ts=run_ts,
        dashboard_url=dashboard_url,
        banner_html=banner_html,
        sections=sections,
        reply_help_html=reply_help_html,
        footer_extra_html=footer_extra_html,
    )
    return subject, html


__all__ = [
    "PALETTE", "FONT_STACK",
    "render_shell", "render_section", "render_kv_table",
    "render_reply_help", "render_email",
]
