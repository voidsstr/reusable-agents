"""Finalize phase — formerly the standalone `seo-reporter`.

Reads `agents/<agent_id>/runs/<run_ts>/recommendations.json`, renders the
per-run HTML report, and fans it out via the framework primitives:

  - `framework.core.digest_queue.queue(...)` — periodic digest rollup
  - `framework.core.implementation_queue.queue_recs(...)` — responder
    auto-queue (recs flow to the implementer)
  - `framework.core.outbound_email.record(...)` — Confirmations page
  - dashboard ping (informational; no run record)

This is `_finalize()` of the collapsed `seo-opportunity-agent`. Imports
the existing `render_html` from `agents/seo-reporter/send-report.py`
unchanged — when seo-reporter is deleted in Phase 4 the renderer moves
into this module.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional


def finalize(agent, *, cfg, run_ts: str, run_dir: Path) -> dict:
    """Return summary dict: {summary, short_circuited, metrics, request_id}.

    Args:
      agent     the AgentBase instance — used for self.queue_recs /
                queue_for_digest / record_outbound + self.decide
      cfg       loaded site config (cfg.site_id, cfg.get('reporter'), ...)
      run_ts    canonical run-ts for this run
      run_dir   local Path the run-dir was unpacked to (so we can call
                the existing render_html unchanged)
    """
    # Reuse the existing renderer until Phase 4 absorbs it into this
    # file. Both the parent dir name (`seo-reporter`) and the file name
    # (`send-report.py`) have hyphens so we have to load by path.
    import importlib.util
    send_report_path = (
        Path(__file__).resolve().parent / "lib" / "reporter" / "send-report.py"
    )
    spec = importlib.util.spec_from_file_location("_seo_reporter_send", send_report_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)

    subject, html = mod.render_html(cfg, run_dir, run_ts=run_ts)
    print(f"[finalize] {subject}", file=sys.stderr)

    # Read recs to get rec_ids + count for the queue/record calls
    import json as _json
    recs_path = run_dir / "recommendations.json"
    recs_doc = {}
    if recs_path.exists():
        try:
            recs_doc = _json.loads(recs_path.read_text())
        except Exception:
            pass
    recs = recs_doc.get("recommendations") or []
    rec_ids = [r["id"] for r in recs if r.get("id")]
    short_circuited = bool((recs_doc.get("metadata") or {}).get("short_circuited"))

    site_id = getattr(cfg, "site_id", "") or ""
    email_cfg = (cfg.get("reporter") or {}).get("email") or {}
    to_list = list(email_cfg.get("to", []) or [])
    sender = email_cfg.get("from", "") or ""

    # 1) Digest queue — replaces the per-run email
    try:
        agent.queue_for_digest(
            subject=subject, body_html=html, to=to_list,
            sender=sender, site=site_id,
            extra_headers={"X-Reusable-Agent-Site": site_id},
        )
        agent.decide("action", f"queued report to digest ({len(html)} chars)")
    except Exception as e:
        agent.decide("warning", f"digest-queue failed (non-fatal): {e}")

    # 2) Direct dispatch — fire the implementer right now (no responder hop).
    # Gated by site.yaml `auto_implement: true|false`. Default True
    # (auto-ship). gated_dispatch_now() returns None when the flag is
    # false, in which case the responder's IMAP-reply path takes over
    # after the user replies to the email above.
    request_id = ""
    if rec_ids:
        try:
            from framework.core import dispatch as _dispatch
            # cfg is a SiteQualityConfig wrapper; gated_dispatch_now
            # only needs `.get("auto_implement")`, which works on dict
            # AND on the wrapper. Pass cfg through directly.
            handle = _dispatch.gated_dispatch_now(
                cfg=cfg,
                agent=agent,
                agent_id=agent.agent_id,
                run_dir=str(run_dir),
                rec_ids=rec_ids,
                action="implement",
                site=site_id,
                subject_tag="seo",
            )
            if handle is None:
                # auto_implement=false → email is the trigger.
                # request_id stays empty (no dispatch happened).
                # gated_dispatch_now already logged the "awaiting reply"
                # decision via agent.decide.
                request_id = ""
            else:
                request_id = handle.request_id
                label = ("auto-queue fallback" if handle.fell_back_to_queue
                         else "direct dispatch")
                agent.decide("action",
                             f"dispatched {len(rec_ids)} recs to implementer "
                             f"via {label} (request_id={request_id})")
        except Exception as e:
            agent.decide("warning", f"dispatch failed (non-fatal): {e}")
    else:
        agent.decide("observation", "no recs to dispatch")

    # 3) Record outbound — for the Confirmations page
    if request_id:
        try:
            body_hash = hashlib.sha1(html.encode("utf-8", "replace")).hexdigest()
            agent.record_outbound(
                request_id=request_id, subject=subject,
                body_hash=body_hash, body_excerpt=html,
                to=to_list, expects_response=False,
            )
        except Exception as e:
            agent.decide("warning", f"record_outbound failed (non-fatal): {e}")

    # 4) Dashboard ping — best-effort, informational
    _ping_dashboard(cfg, subject)

    summary = (
        f"short-circuit: replayed {len(rec_ids)} recs"
        if short_circuited
        else f"{len(rec_ids)} recommendations"
    )

    # North-star metrics — surface every GSC + GA4 signal already
    # collected in this run as RunResult.metrics keys so goals like
    # `goal-top5-keywords`, `goal-ctr-baseline`, `goal-zero-indexing-issues`
    # auto-track on the dashboard via Layer-B capture. Without this,
    # those goals stay flatlined at 0 forever even when ranking improves.
    north_star = _collect_north_star_metrics(run_dir)

    # Per-category rec counts — drive the SEO checklist goals (eeat,
    # cwv, ai-search-readiness, internal-linking, etc). Each is the
    # count of recs in the corresponding rule category. The analyzer's
    # rule taxonomy uses prefixed rec_type strings like
    # `eeat-author-bio-missing`, `cwv-largest-contentful-paint`,
    # `llm-search-direct-answer-missing`, `internal-link-anchor-thin`.
    per_category = _category_rec_counts(recs)

    metrics = {"rec_count": len(rec_ids), **north_star, **per_category}

    return {
        "summary": summary,
        "short_circuited": short_circuited,
        "metrics": metrics,
        "request_id": request_id,
    }


def _category_rec_counts(recs: list) -> dict:
    """Bucket the run's recs by SEO checklist category prefix. Each
    count maps to a north-star goal:
      - eeat_recs_count          → goal-eeat-baseline
      - cwv_recs_count           → goal-cwv-pass
      - ai_search_recs_count     → goal-ai-search-readiness
      - internal_link_recs_count → goal-internal-linking
      - schema_recs_count        → goal-schema-coverage (alt)
      - recs_shipped_count       → goal-recs-shipped-30d (cumulative;
                                    Layer-A cache aggregates over time)
    """
    out = {"eeat_recs_count": 0, "cwv_recs_count": 0,
           "ai_search_recs_count": 0, "internal_link_recs_count": 0,
           "schema_recs_count": 0, "recs_shipped_count": 0}
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        rt = (r.get("type") or r.get("rec_type") or "").lower()
        # Counts are INVERSE indicators — lower is better. Direction
        # on the goal record is `decrease` for these. Cap a clean
        # bucket at 0 explicitly (not None) so progress series renders.
        if rt.startswith("eeat-"): out["eeat_recs_count"] += 1
        if rt.startswith(("cwv-", "core-web-vitals-")): out["cwv_recs_count"] += 1
        if rt.startswith(("llm-search-", "ai-search-", "geo-")): out["ai_search_recs_count"] += 1
        if "internal-link" in rt or rt.startswith("orphan-"): out["internal_link_recs_count"] += 1
        if rt.startswith(("schema-", "ld-json-")) or "json-ld" in rt: out["schema_recs_count"] += 1
        if r.get("shipped") is True or r.get("implemented") is True:
            out["recs_shipped_count"] += 1
    # Convert to float for consistent metric typing
    return {k: float(v) for k, v in out.items()}


def _collect_north_star_metrics(run_dir: Path) -> dict:
    """Read GSC + GA4 artifacts the collector dropped in run_dir and
    return a flat dict of metric keys → values for goal auto-tracking.

    All metrics here are POINT-IN-TIME measurements of search/citation
    visibility (not rate counters), so each cron tick updates them and
    the Goals tab shows real trend over time.

    Keys (with the goal that binds them):
      - top5_keyword_count               → goal-top5-keywords
      - striking_distance_query_count    → goal-top5-keywords (alt)
      - zero_click_query_count           → goal-zero-click-queries-converted
      - mean_position_top_queries        → goal-rank-trend
      - mean_ctr_pct                     → goal-ctr-baseline
      - rank_improvements_28d            → goal-rank-trend
      - rank_regressions_28d             → goal-rank-trend (negative signal)
      - indexed_pages_count              → goal-zero-indexing-issues
      - organic_sessions_28d             → goal-organic-traffic
      - organic_conversions_28d          → goal-monthly-revenue (proxy)
      - llm_referral_sessions_28d        → goal-llm-traffic
    """
    import json as _json
    m: dict = {}

    def _read(name: str):
        p = run_dir / name
        if not p.is_file():
            return None
        try:
            return _json.loads(p.read_text())
        except Exception:
            return None

    # GSC: top-5 + striking-distance + zero-click counts
    top5 = _read("gsc-top5-targets.json")
    if isinstance(top5, list):
        m["top5_keyword_count"] = len(top5)
    elif isinstance(top5, dict):
        items = top5.get("items") or top5.get("queries") or []
        if isinstance(items, list):
            m["top5_keyword_count"] = len(items)

    striking = _read("gsc-striking-distance.json")
    if isinstance(striking, list):
        m["striking_distance_query_count"] = len(striking)
    elif isinstance(striking, dict):
        items = striking.get("items") or striking.get("queries") or []
        if isinstance(items, list):
            m["striking_distance_query_count"] = len(items)

    zclick = _read("gsc-zero-click.json")
    if isinstance(zclick, list):
        m["zero_click_query_count"] = len(zclick)
    elif isinstance(zclick, dict):
        items = zclick.get("items") or zclick.get("queries") or []
        if isinstance(items, list):
            m["zero_click_query_count"] = len(items)

    # GSC: rank movement
    imp = _read("gsc-rank-improvements.json")
    if isinstance(imp, list):
        m["rank_improvements_28d"] = len(imp)
    reg = _read("gsc-rank-regressions.json")
    if isinstance(reg, list):
        m["rank_regressions_28d"] = len(reg)

    # GSC: position distribution — derive avg position + CTR baseline
    pages_28d = _read("gsc-pages-28d.json")
    if isinstance(pages_28d, dict):
        rows = pages_28d.get("rows") or pages_28d.get("items") or []
        if rows and isinstance(rows, list):
            positions = [r.get("position") for r in rows if isinstance(r, dict) and r.get("position") is not None]
            if positions:
                m["mean_position_top_queries"] = round(sum(positions) / len(positions), 2)
            # CTR baseline — weighted by impressions
            total_clicks = sum((r.get("clicks") or 0) for r in rows if isinstance(r, dict))
            total_impr = sum((r.get("impressions") or 0) for r in rows if isinstance(r, dict))
            if total_impr > 0:
                m["mean_ctr_pct"] = round(100.0 * total_clicks / total_impr, 2)
                m["organic_impressions_28d"] = int(total_impr)
                m["organic_clicks_28d"] = int(total_clicks)

    # GA4: organic sessions + conversions (revenue proxy)
    ga_summary = _read("ga4-organic-summary-28d.json")
    if isinstance(ga_summary, dict):
        # Shapes vary by collector version — try a few common keys
        sessions = ga_summary.get("sessions") or ga_summary.get("organic_sessions") or ga_summary.get("total_sessions")
        if sessions is not None:
            try:
                m["organic_sessions_28d"] = int(sessions)
            except Exception:
                pass
        conv = ga_summary.get("conversions") or ga_summary.get("organic_conversions")
        if conv is not None:
            try:
                m["organic_conversions_28d"] = int(conv)
            except Exception:
                pass

    # LLM referral traffic (the north-north-star) — referrers like
    # chat.openai.com, claude.ai, perplexity.ai, gemini.google.com.
    # ga4-source-28d.json shape: rows of {source, sessions}.
    ga_source = _read("ga4-organic-source-28d.json")
    if isinstance(ga_source, dict):
        rows = ga_source.get("rows") or ga_source.get("items") or []
        llm_hosts = ("chatgpt.com", "chat.openai.com", "claude.ai", "perplexity.ai",
                     "gemini.google.com", "you.com", "bing.com/chat", "copilot.microsoft.com")
        llm_sessions = 0
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            src = (r.get("source") or r.get("session_source") or "").lower()
            if any(h in src for h in llm_hosts):
                try:
                    llm_sessions += int(r.get("sessions") or 0)
                except Exception:
                    pass
        m["llm_referral_sessions_28d"] = llm_sessions

    return m


def _ping_dashboard(cfg, subject: str) -> None:
    dash = (cfg.get("reporter") or {}).get("dashboard") or {}
    base = (dash.get("base_url") or "").rstrip("/")
    aid = dash.get("agent_id") or ""
    if not base or not aid:
        return
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"{base}/api/agents/{aid}", method="GET"),
            timeout=5,
        ).read()
    except Exception as e:
        print(f"[finalize] dashboard ping failed: {e}", file=sys.stderr)
