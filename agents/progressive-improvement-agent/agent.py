"""progressive-improvement-agent — main entrypoint.

Subclasses AgentBase. Each run:
  1. Drains user replies from the previous run's email and writes their
     choices back into the prior recommendations.json.
  2. Crawls the configured site (BFS, capped depth + page count).
  3. Asks the configured AI provider (self.ai_client()) to flag issues
     batch by batch.
  4. Scores each issue + assigns an auto/review/experimental tier.
  5. Renders a recommendations email with rec-id reply syntax, sends it
     via the framework's mailer (LogMailer in dev — record-only).
  6. If site config has auto_implement: true, dispatches tier=auto recs
     to the configured implementer (default implementer) by writing
     to its responses-queue.

Invoke:
    PROGRESSIVE_IMPROVEMENT_CONFIG=path/to/site.yaml python3 agent.py

Or via the framework Run-now button (host-worker exec).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Make framework + shared importable regardless of cwd. After the
# agents/ consolidation, the repo root is two levels up.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.email_codes import new_request_id  # noqa: E402
from framework.core.guardrails import declare  # noqa: E402

from shared.site_quality import (  # noqa: E402
    apply_user_responses,
    assign_rec_ids,
    dispatch_auto_recs,
    load_quality_config_from_env,
    render_recs_email,
    score_tier,
    send_via_msmtp,
    validate_recs_doc,
)

from crawler import Page, crawl  # noqa: E402


AGENT_ID = "progressive-improvement-agent"


# ---------------------------------------------------------------------------
# LLM analysis
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM = """You are a website-quality auditor. You review pages for
inconsistencies, broken or outdated content, miscategorized items, duplicate
content, layout/accessibility issues, and content errors.

You MUST return STRICT JSON — no prose before or after, no markdown fences.
Return a JSON array of issue objects matching this schema:
{
  "url": string,
  "category": "incorrect-categorization" | "broken-page" | "outdated-content" | "duplicate-content" | "missing-content" | "layout-issue" | "accessibility" | "performance" | "content-error" | "other",
  "severity": "critical" | "high" | "medium" | "low",
  "confidence": number (0.0-1.0),
  "title": string (one-line headline),
  "rationale": string,
  "evidence_snippet": string (exact quote from the page),
  "fix_suggestion": string
}
Only flag issues you can prove from the provided page data. If you find
nothing actionable, return [].
"""


def _format_pages_for_prompt(pages: list[Page], what_we_do: str) -> str:
    parts = []
    if what_we_do:
        parts.append(f"SITE PURPOSE: {what_we_do}\n")
    parts.append("PAGES TO REVIEW:\n")
    for p in pages:
        body_excerpt = (p.body_text or "")[:1500]
        parts.append(f"""
URL: {p.url}
STATUS: {p.status_code}{(' (ERROR: ' + p.error + ')') if p.error else ''}
TITLE: {p.title}
DESCRIPTION: {p.description}
H1: {p.h1}
CANONICAL: {p.canonical}
BODY (truncated):
{body_excerpt}
---""")
    return "".join(parts)


def _parse_llm_json(raw: str) -> list[dict]:
    """LLMs sometimes wrap JSON in fences or add a preamble. Be tolerant."""
    s = (raw or "").strip()
    # Strip markdown fences
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    # Find first [ or {
    for opener, closer in [("[", "]"), ("{", "}")]:
        i = s.find(opener)
        if i >= 0:
            j = s.rfind(closer)
            if j > i:
                s = s[i:j + 1]
                break
    try:
        out = json.loads(s)
    except json.JSONDecodeError:
        return []
    if isinstance(out, dict):
        return [out]
    if isinstance(out, list):
        return [x for x in out if isinstance(x, dict)]
    return []


# NOTE: per-item hash short-circuit utilities live in
# framework/core/short_circuit.py — partition_by_hash + merge_findings_cache.
# Import + use them in run() rather than defining a local copy.
from framework.core.short_circuit import merge_findings_cache as _merge_findings_cache  # noqa: E402


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
# Goal mapping — maps the LLM's chosen rec.category onto one or more of the
# long-running goals seeded for this agent. Drives goal_changes logging when
# a user implements the rec.
_CATEGORY_GOAL_MAP: dict[str, list[str]] = {
    "broken-page":              ["goal-zero-broken-pages"],
    "incorrect-categorization": ["goal-zero-miscategorized-products"],
    "duplicate-content":        ["goal-zero-duplicate-content"],
    "outdated-content":         ["goal-content-freshness"],
    "missing-content":          ["goal-content-freshness"],
    "accessibility":            ["goal-accessibility-baseline"],
    # No mapping for: layout-issue, performance, content-error, other → []
}


def _category_to_goal_ids(category: str) -> list[str]:
    return _CATEGORY_GOAL_MAP.get(category, [])


# ---------------------------------------------------------------------------
# Run-to-run dedupe — once a rec is shipped/skipped, future PI runs should
# not re-detect the same issue and re-queue it. Without this, a fix that
# lands in production (e.g. "trash can miscategorized as AI accelerator")
# keeps re-appearing on every PI run as a different rec-NNN, even though
# the underlying data is corrected. The canonical key is content-based
# (category + page URL + first 60 chars of normalized title), so a re-run
# that finds the same issue on the same page collapses to the same key.
# Mirrors seo-analyzer._canonical_rec_key (analyzer.py:205).

import re as _re_pi


def _canonical_rec_key(rec: dict) -> Optional[str]:
    """Stable hash-key for de-duping PI recs across runs. Returns None if
    we can't compute a reliable key (rec gets re-proposed in that case)."""
    cat = (rec.get("category") or "").strip().lower()
    title = (rec.get("title") or "").strip().lower()
    if not cat or not title:
        return None
    # Pull first URL out of evidence[].url if present
    evid = rec.get("evidence") or []
    url = ""
    if isinstance(evid, list) and evid:
        url = (evid[0].get("url") or "") if isinstance(evid[0], dict) else ""
    # Strip query + fragment for stable matching
    if url:
        url = url.split("#", 1)[0].split("?", 1)[0]
    # Normalize title: lowercase, collapse whitespace, drop trailing punctuation
    norm_title = _re_pi.sub(r'\s+', ' ', _re_pi.sub(r'[^a-z0-9 ]+', '', title))[:60]
    return f"{cat}|{url}|{norm_title}"


def _load_handled_rec_keys(
    agent_id: str, current_run_ts: str, storage,
    horizon_runs: int = 30,
) -> set[str]:
    """Walk the most recent N prior runs' recommendations.json and collect
    canonical keys for recs that were shipped/implemented OR explicitly
    skipped. Future runs won't re-propose those same issues."""
    runs_prefix = f"agents/{agent_id}/runs/"
    seen_runs: set[str] = set()
    try:
        for k in storage.list_prefix(runs_prefix):
            tail = k[len(runs_prefix):]
            if "/" not in tail:
                continue
            run_id = tail.split("/", 1)[0]
            if run_id == current_run_ts:
                continue
            seen_runs.add(run_id)
    except Exception:
        return set()
    sorted_runs = sorted(seen_runs, reverse=True)[:horizon_runs]
    handled: set[str] = set()
    for run_id in sorted_runs:
        try:
            doc = storage.read_json(f"{runs_prefix}{run_id}/recommendations.json")
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for r in doc.get("recommendations", []) or []:
            if not (r.get("shipped") or r.get("implemented") or r.get("skipped")):
                continue
            key = _canonical_rec_key(r)
            if key:
                handled.add(key)
    return handled


# ---------------------------------------------------------------------------

class ProgressiveImprovementAgent(AgentBase):
    agent_id = AGENT_ID
    send_run_summary_email = False  # emails ranked recommendations directly
    name = "Progressive Improvement Agent"
    description = (
        "Crawls configured sites top-down, identifies quality issues "
        "(broken pages, duplicates, miscategorization, outdated content), "
        "and emails ranked recommendations with rec-id reply syntax."
    )
    category = "seo"

    capabilities = [
        declare("crawl_pages", "BFS-crawl one configured site",
                confirmation_required=False, risk_level="low"),
        declare("call_ai", "Call configured AI provider for issue analysis",
                confirmation_required=False, risk_level="low"),
        declare("send_recommendations_email",
                "Send the report email to the configured recipient",
                confirmation_required=False, risk_level="low"),
        declare("dispatch_auto_recs",
                "Dispatch auto-tier recs to the implementer agent (only if site config opts in)",
                confirmation_required=False, risk_level="medium",
                affects=["downstream-agent"]),
    ]

    def __init__(self, *args, **kwargs):
        # Per-site instances pass AGENT_ID via the host-worker so this code
        # can be driven by aisleprompt-progressive-improvement-agent or
        # specpicks-progressive-improvement-agent without colliding storage.
        env_id = os.environ.get("AGENT_ID")
        if env_id and env_id != AGENT_ID:
            kwargs.setdefault("agent_id", env_id)
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        self.cfg = load_quality_config_from_env("PROGRESSIVE_IMPROVEMENT_CONFIG")
        self.run_dir = self.cfg.run_dir_for_now(self.agent_id)
        self.decide("setup",
                    f"site={self.cfg.site_id} run_dir={self.run_dir} agent_id={self.agent_id}",
                    evidence={"site": self.cfg.site_id, "agent_id": self.agent_id})

    def run(self) -> RunResult:
        cfg = self.cfg
        crawler_cfg = cfg.get("crawler", {}) or {}
        analyzer_cfg = cfg.get("analyzer", {}) or {}

        self.status("starting", progress=0.05,
                    current_action=f"site={cfg.site_id}")

        # ── 0. Surface any inbound handoffs from other agents ──────────────
        # framework.core.work_types routes catalog-broken-image,
        # catalog-miscategorization, and any cfg.handoff_routes entries here.
        # AgentBase.pre_run() already drained the queue; we just need to
        # log them in the decision stream so the LLM analyzer pass sees
        # them as priority work and the digest reports the inter-agent flow.
        if self.inbound_handoffs:
            self.decide(
                "observation",
                f"received {len(self.inbound_handoffs)} inbound handoff(s) "
                f"from: {sorted({h.get('from_agent','?') for h in self.inbound_handoffs})}",
                evidence={
                    "rec_types": sorted({(h.get('rec') or {}).get('type','?')
                                          for h in self.inbound_handoffs}),
                    "request_ids": [h.get('request_id') for h in self.inbound_handoffs[:10]],
                },
            )

        # ── 1. Apply any pending user replies from the prior run ────────────
        prior_recs = self._most_recent_recs_path()
        applied = apply_user_responses(
            responses=self.responses,
            prior_recs_path=prior_recs,
        )
        if applied:
            self.decide("observation",
                        f"applied {len(applied)} user response(s) to prior recs",
                        evidence={"applied": applied})

        # ── 2. Crawl ────────────────────────────────────────────────────────
        self.status("crawling", progress=0.15,
                    current_action=f"BFS from {cfg.base_url}")
        pages: list[Page] = []
        pages_jsonl = self.run_dir / "pages.jsonl"
        with pages_jsonl.open("w") as f:
            for page in crawl(  # noqa: B020 — using both `f` and outer `page`

                base_url=cfg.base_url,
                seed_urls=crawler_cfg.get("seed_urls") or ["/"],
                use_sitemap=crawler_cfg.get("use_sitemap", True),
                max_depth=int(crawler_cfg.get("max_depth", 2)),
                max_pages=int(crawler_cfg.get("max_pages", 30)),
                path_excludes=crawler_cfg.get("path_excludes") or [],
                request_timeout_s=int(crawler_cfg.get("request_timeout_s", 15)),
                user_agent=crawler_cfg.get("user_agent",
                                           "reusable-agents-quality-crawler/1.0"),
                throttle_ms=int(crawler_cfg.get("throttle_ms", 500)),
            ):
                pages.append(page)
                f.write(json.dumps(page.to_dict()) + "\n")
                if len(pages) % 5 == 0:
                    self.status(f"crawled {len(pages)} pages",
                                progress=0.15 + min(0.35, len(pages) * 0.01))
        # Also write the full pages.jsonl to framework storage so the dashboard
        # can surface it in the per-run drill-down.
        self.storage.write_text(
            f"agents/{self.agent_id}/runs/{self.run_ts}/pages.jsonl",
            "\n".join(json.dumps(p.to_dict()) for p in pages),
        )
        self.decide("observation",
                    f"crawled {len(pages)} pages "
                    f"({sum(1 for p in pages if 200 <= p.status_code < 300)} ok)",
                    evidence={"pages": len(pages)})

        # ── 3. LLM analysis (batched) ───────────────────────────────────────
        self.status("analyzing", progress=0.55, current_action="LLM batches")
        batch_size = int(analyzer_cfg.get("batch_size", 5))
        try:
            client = self.ai_client()
        except Exception as e:
            self.decide("error", f"ai_client unavailable: {e}")
            return RunResult(status="failure",
                              summary=f"AI provider not configured: {e}",
                              metrics={"pages_crawled": len(pages)})

        # Adaptive context — pull recent goal-changes for this site so the
        # LLM can de-prioritize patterns that haven't been moving the metric
        # and double down on what worked. Empty string when there's no
        # history yet (first run).
        try:
            from framework.core import goal_changes as _gc
            adaptive_block = _gc.adaptive_context_block(
                self.agent_id, site=cfg.site_id, horizon=30, storage=self.storage,
            )
        except Exception:
            adaptive_block = ""
        if adaptive_block:
            self.decide("observation",
                        f"injected adaptive context ({len(adaptive_block)} chars) into LLM prompt")

        raw_issues: list[dict] = []
        valid_pages = [p for p in pages if 200 <= p.status_code < 300 and p.body_text]
        # Also surface fetch errors as broken-page issues directly (no LLM needed)
        for p in pages:
            if p.error or (p.status_code and not (200 <= p.status_code < 400)):
                raw_issues.append({
                    "url": p.url,
                    "category": "broken-page",
                    "severity": "critical" if p.status_code in (0, 500, 502, 503) else "high",
                    "confidence": 0.99,
                    "title": f"Broken page: {p.url} returned {p.status_code or 'fetch error'}",
                    "rationale": p.error or f"HTTP {p.status_code}",
                    "evidence_snippet": (p.error or f"status={p.status_code}")[:200],
                    "fix_suggestion": "Investigate the route handler / build error / upstream dependency.",
                })

        # ── Page-hash short-circuit ────────────────────────────────────────
        # Skip the LLM analysis for pages whose body hash matches what we
        # saw on the last successful run AND for which no recommendations
        # came back unresolved. On a stable site at 2h cadence this short
        # -circuits ~80% of LLM batches. We carry the result-by-hash map
        # forward in next_state so cached findings persist.
        prior_hashes = (self.state.get("last_seen_hashes") or {}) if isinstance(self.state, dict) else {}
        prior_findings = (self.state.get("last_findings_by_hash") or {}) if isinstance(self.state, dict) else {}
        # Cap on how often we revisit the same hash regardless — guards against
        # findings going stale + makes sure `recs_review` recs eventually
        # re-emerge if the site reverts a fix that we tracked.
        revisit_after_runs = int(analyzer_cfg.get("revisit_unchanged_after_runs", 6))  # ≈ 12h at 2h cadence — surfaces regressions faster
        revisit_counter = (self.state.get("hash_revisit_counter") or {}) if isinstance(self.state, dict) else {}

        cached_pages: list[Page] = []
        fresh_pages: list[Page] = []
        for p in valid_pages:
            h = p.body_hash or ""
            if not h:
                fresh_pages.append(p)
                continue
            seen_count = int(revisit_counter.get(p.url, 0))
            if prior_hashes.get(p.url) == h and seen_count < revisit_after_runs:
                cached_pages.append(p)
            else:
                fresh_pages.append(p)

        # Replay cached findings into raw_issues (still get scored + ranked)
        replayed = 0
        for p in cached_pages:
            for issue in (prior_findings.get(p.body_hash) or []):
                if isinstance(issue, dict):
                    # Rebind URL — same content might live at a different URL
                    issue = dict(issue)
                    issue.setdefault("url", p.url)
                    raw_issues.append(issue)
                    replayed += 1

        if cached_pages:
            self.decide(
                "observation",
                f"page-hash short-circuit: {len(cached_pages)}/{len(valid_pages)} "
                f"pages unchanged since last run; replayed {replayed} cached findings, "
                f"sending {len(fresh_pages)} fresh page(s) to LLM",
                evidence={
                    "cached": len(cached_pages),
                    "fresh": len(fresh_pages),
                    "replayed_findings": replayed,
                },
            )

        # Track which findings each LLM call produced for which page hash —
        # so the next run's short-circuit can replay them.
        new_findings_by_hash: dict[str, list[dict]] = {}
        new_hashes: dict[str, str] = {p.url: (p.body_hash or "") for p in valid_pages}
        new_revisit_counter: dict[str, int] = {}
        for p in valid_pages:
            prev = int(revisit_counter.get(p.url, 0))
            if p in cached_pages:
                new_revisit_counter[p.url] = prev + 1
            else:
                new_revisit_counter[p.url] = 0  # reset on fresh analysis

        for i in range(0, len(fresh_pages), batch_size):
            batch = fresh_pages[i:i + batch_size]
            user_prompt = _format_pages_for_prompt(batch, cfg.what_we_do)
            try:
                system_prompt = ANALYSIS_SYSTEM
                if adaptive_block:
                    system_prompt = (
                        ANALYSIS_SYSTEM
                        + "\n\n--- ADAPTIVE CONTEXT (recent work + outcomes) ---\n"
                        + adaptive_block
                    )
                raw = client.chat(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.1,
                    max_tokens=2000,
                )
            except Exception as e:
                self.decide("error", f"LLM batch {i // batch_size} failed: {e}")
                continue
            parsed = _parse_llm_json(raw)
            self.decide("observation",
                        f"batch {i // batch_size}: {len(parsed)} issues from {len(batch)} pages")
            raw_issues.extend(parsed)
            # Index findings by the originating page's body_hash so the
            # next run can replay them when the page hasn't changed.
            for issue in parsed:
                if not isinstance(issue, dict):
                    continue
                url = issue.get("url") or ""
                # Match the issue back to one of the batched pages by URL
                for bp in batch:
                    if bp.url == url and bp.body_hash:
                        new_findings_by_hash.setdefault(bp.body_hash, []).append(issue)
                        break
            self.status(f"analyzed {min(i + batch_size, len(fresh_pages))}/{len(fresh_pages)} fresh pages "
                        f"({len(cached_pages)} cached)",
                        progress=0.55 + min(0.30, (i / max(1, len(fresh_pages))) * 0.30))

        # ── 4. Score, tier, cap ─────────────────────────────────────────────
        threshold = float(analyzer_cfg.get("auto_implement_threshold", 0.95))
        max_recs = int(analyzer_cfg.get("max_recs_per_run", 15))

        # Dedupe against shipped/implemented/skipped recs from prior runs so
        # the agent stops re-detecting the same issue on every cycle.
        # Disable with PI_DISABLE_HANDLED_DEDUPE=1.
        handled_keys: set[str] = set()
        if os.environ.get("PI_DISABLE_HANDLED_DEDUPE") != "1":
            try:
                handled_keys = _load_handled_rec_keys(
                    self.agent_id, self.run_ts, self.storage,
                )
                if handled_keys:
                    self.decide("observation",
                                f"loaded {len(handled_keys)} handled-rec keys from prior runs")
            except Exception as e:
                self.decide("observation", f"handled-rec dedupe load failed: {e}")

        recs: list[dict] = []
        skipped_dupe = 0
        for issue in raw_issues:
            try:
                conf = float(issue.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            sev = issue.get("severity", "low")
            tier = score_tier(confidence=conf, severity=sev, threshold=threshold)
            evidence = []
            if issue.get("url"):
                evidence.append({
                    "url": issue["url"],
                    "snippet": (issue.get("evidence_snippet") or "")[:300],
                })
            cat = issue.get("category", "other")
            # Probe canonical key BEFORE building the full rec so we can skip
            # cheaply. Build a stub with the fields _canonical_rec_key reads.
            stub = {"category": cat,
                    "title": issue.get("title", ""),
                    "evidence": evidence}
            ckey = _canonical_rec_key(stub)
            if ckey and ckey in handled_keys:
                skipped_dupe += 1
                continue
            recs.append({
                "category": cat,
                "severity": sev,
                "confidence": round(conf, 3),
                "tier": tier,
                "title": issue.get("title", "(untitled)"),
                "rationale": issue.get("rationale", ""),
                "evidence": evidence,
                "implementation_outline": {
                    "approach": issue.get("fix_suggestion", ""),
                },
                "implemented": False,
                # goal_ids: which long-running goals this rec advances. Used by
                # the responder to log goal-changes when the user implements,
                # which feeds adaptive_context_block on the next analyzer pass.
                "goal_ids": _category_to_goal_ids(cat),
            })

        if skipped_dupe:
            self.decide("observation",
                        f"deduped {skipped_dupe} rec(s) already shipped/skipped in prior runs")

        # Sort: severity (critical→low), then confidence desc, then tier (auto first)
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tier_order = {"auto": 0, "review": 1, "experimental": 2}
        recs.sort(key=lambda r: (
            sev_order.get(r["severity"], 9),
            tier_order.get(r["tier"], 9),
            -r["confidence"],
        ))
        recs = recs[:max_recs]
        recs = assign_rec_ids(recs)

        self.status("scoring + tiering done", progress=0.85,
                    current_action=f"{len(recs)} recs ({sum(1 for r in recs if r['tier']=='auto')} auto)")

        # ── 5. Persist + email ──────────────────────────────────────────────
        request_id = new_request_id()
        recs_doc = {
            "schema_version": "1",
            "site": cfg.site_id,
            "agent": AGENT_ID,
            "run_ts": self.run_ts,
            "pages_analyzed": len(pages),
            "summary": (
                f"Reviewed {len(pages)} pages on {cfg.label}. "
                f"Found {len(recs)} issues "
                f"(critical={sum(1 for r in recs if r['severity']=='critical')}, "
                f"high={sum(1 for r in recs if r['severity']=='high')}, "
                f"medium={sum(1 for r in recs if r['severity']=='medium')}, "
                f"low={sum(1 for r in recs if r['severity']=='low')}). "
                f"{sum(1 for r in recs if r['tier']=='auto')} are auto-eligible."
            ),
            "recommendations": recs,
        }
        validate_recs_doc(recs_doc)
        self._save_artifact("recommendations.json", recs_doc)

        subject, html = render_recs_email(
            cfg=cfg, agent_id=self.agent_id, request_id=request_id,
            recs=recs, summary=recs_doc["summary"],
            auto_queued=bool(recs),
        )
        self._save_artifact("email-rendered.html", html)

        email_cfg = (cfg.get("reporter", {}) or {}).get("email") or {}
        to = email_cfg.get("to") or []
        sender = email_cfg.get("from", "")
        msmtp_account = email_cfg.get("msmtp_account", "automation")
        if to and sender:
            ok, detail = send_via_msmtp(
                subject=subject, body_html=html, to=to,
                sender=sender, msmtp_account=msmtp_account,
                extra_headers={
                    "X-Reusable-Agent": self.agent_id,
                    "Reply-To": sender,
                },
            )
            if ok:
                self.decide("action", f"emailed {len(to)} recipient(s) via msmtp/{msmtp_account}")
                # Persist outbound-email metadata so the responder can route replies
                self.storage.write_json(
                    f"agents/{self.agent_id}/outbound-emails/{request_id}.json",
                    {
                        "schema_version": "1",
                        "request_id": request_id,
                        "agent_id": self.agent_id,
                        "subject": subject,
                        "to": list(to),
                        "expects_response": True,
                        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "transport": f"msmtp:{msmtp_account}",
                        "ok": True,
                    },
                )
                # ── Auto-queue every rec for implementation ─────────────
                # Same pattern as seo-reporter: drop a trigger file the
                # responder picks up on its next tick (every 60s) and
                # fans out to the implementer. Email becomes informational
                # — reply with `defer rec-NNN` to override.
                if recs:
                    self._write_auto_queue(request_id=request_id, recs=recs)
            else:
                self.decide("error", f"email send failed: {detail}")
        else:
            self.decide("observation",
                        "no recipient/sender configured — email-rendered.html written only")

        # ── 5b. Update goal metrics + close out prior goal-changes ──────────
        # Each recs.category translates back to a goal metric — broken-page
        # count, miscategorized count, etc. Compute now-current values from
        # the issues we found, update the goal's current metric, and walk the
        # change log to fill metric_after on past dispatches.
        try:
            self._measure_and_update_goals(pages=pages, recs=recs)
        except Exception as e:
            self.decide("error", f"goal-metric update failed: {e}")

        # ── 6. Auto-dispatch tier=auto recs (if opted in) ───────────────────
        dispatched = dispatch_auto_recs(
            cfg=cfg, agent_id=AGENT_ID, recs=recs, storage=self.storage,
        )
        if dispatched:
            self.decide("action",
                        f"auto-dispatched {len(dispatched)} recs to implementer",
                        evidence={"rec_ids": dispatched})

        # ── 7. Record outcomes for inbound handoffs ─────────────────────────
        # Mark every drained handoff as in_progress when this run produced
        # at least one rec covering the same rec_type — same fuzzy-match
        # logic as the article-author agent. The implementer's post-commit
        # hook will later promote these to "shipped" when the rec actually
        # lands in code.
        try:
            from framework.core.handoff import record_handoff_outcome
            HANDOFF_RECTYPE_TO_REC_CATEGORY = {
                # rec.type from analyzer → category prefixes the PI rec
                # writer emits when handling that class of work
                "catalog-broken-image": ("catalog-quality",),
                "catalog-miscategorization": ("catalog-quality",),
                "catalog-thin-description": ("catalog-quality", "content"),
                "quality_audit_fix": ("catalog-quality", "content", "ux"),
                "onpage-thin-content": ("content",),
                "onpage-low-internal-linking": ("content", "internal-link"),
                "article-orphan-boost": ("internal-link", "content"),
            }
            produced_categories = {
                str(r.get("category") or r.get("issue_class") or "")
                for r in (recs or [])
            }
            for h in (self.inbound_handoffs or []):
                rid = h.get("request_id")
                if not rid:
                    continue
                rec_type = (h.get("rec") or {}).get("type") or ""
                relevant_prefixes = HANDOFF_RECTYPE_TO_REC_CATEGORY.get(rec_type, ())
                covered = any(
                    any(cat.startswith(p) for p in relevant_prefixes)
                    for cat in produced_categories
                )
                if covered:
                    record_handoff_outcome(
                        agent_id=self.agent_id,
                        request_id=rid, outcome="in_progress",
                        outcome_detail=(
                            f"PI run produced rec(s) covering rec.type={rec_type}"
                        ),
                        downstream_run_ts=self.run_ts,
                    )
                else:
                    record_handoff_outcome(
                        agent_id=self.agent_id,
                        request_id=rid, outcome="deferred",
                        outcome_detail=(
                            f"no rec produced this run matched rec.type={rec_type}"
                        ),
                        downstream_run_ts=self.run_ts,
                    )
        except Exception as e:
            self.decide("error", f"handoff outcome record failed: {e}")

        self.status("done", progress=1.0, state="success")
        return RunResult(
            status="success",
            summary=recs_doc["summary"],
            metrics={
                "pages_crawled": len(pages),
                "recs_total": len(recs),
                "recs_auto": sum(1 for r in recs if r["tier"] == "auto"),
                "recs_review": sum(1 for r in recs if r["tier"] == "review"),
                "recs_experimental": sum(1 for r in recs if r["tier"] == "experimental"),
                "applied_responses": len(applied),
                "auto_dispatched": len(dispatched),
            },
            next_state={
                "last_run_ts": self.run_ts,
                "last_request_id": request_id,
                "site_id": cfg.site_id,
                # Page-hash short-circuit cache: per-URL body_hash from THIS
                # run + findings keyed by hash. Next run consults these to
                # skip the LLM for unchanged pages and replay the cached
                # findings into the rec pipeline.
                "last_seen_hashes": new_hashes,
                "last_findings_by_hash": _merge_findings_cache(
                    prior_findings, new_findings_by_hash, new_hashes
                ),
                "hash_revisit_counter": new_revisit_counter,
            },
        )

    def _measure_and_update_goals(self, *, pages: list, recs: list[dict]) -> None:
        """Compute current metric values from this run's findings, persist
        them on the goals doc, and fill metric_after on past goal-changes.

        Mapping: each goal-id has a `metric.name` that we know how to count
        from our run state.
        """
        from framework.core import goals as _goals_mod, goal_changes as _gc

        # Counts derived from this run
        broken_count = sum(1 for p in pages
                           if (p.error or (p.status_code and not (200 <= p.status_code < 400))))
        miscat_count = sum(1 for r in recs if r.get("category") == "incorrect-categorization")
        dup_count    = sum(1 for r in recs if r.get("category") == "duplicate-content")
        stale_count  = sum(1 for r in recs if r.get("category") in ("outdated-content", "missing-content"))
        a11y_count   = sum(1 for r in recs if r.get("category") == "accessibility")
        metric_now: dict[str, float] = {
            "broken_pages": broken_count,
            "miscategorized_count": miscat_count,
            "duplicate_count": dup_count,
            "stale_count": stale_count,
            "accessibility_violations": a11y_count,
        }

        active = _goals_mod.read_active_goals(self.agent_id, storage=self.storage)
        if not active:
            return
        changed = False
        for g in active:
            metric = g.get("metric") or {}
            name = metric.get("name")
            if name in metric_now:
                value = float(metric_now[name])
                if metric.get("current") != value:
                    metric["current"] = value
                    changed = True
                # Fill metric_after on prior goal-changes for this goal
                try:
                    _gc.update_post_change_metrics(
                        self.agent_id, goal_id=g["id"],
                        current_value=value,
                        measurement_run_ts=self.run_ts,
                        storage=self.storage,
                    )
                except Exception:
                    pass
        if changed:
            _goals_mod.write_goals_doc(self.agent_id, active, storage=self.storage)
            self.decide("observation",
                        f"goal metrics updated: {metric_now}")

    def _write_auto_queue(self, *, request_id: str, recs: list[dict]) -> None:
        """Direct-dispatch the rec batch to the implementer via
        framework.core.dispatch. Site-level lock; retries on transient
        failures; falls back to writing agents/responder-agent/auto-queue/
        if all retries exhaust (transitional). Email becomes informational
        — recipient replies only to override (defer rec-NNN)."""
        try:
            from framework.core import dispatch
            rec_ids = [r["id"] for r in recs if r.get("id")]
            handle = dispatch.dispatch_now(
                agent_id=self.agent_id, run_dir=str(self.run_dir),
                rec_ids=rec_ids, action="implement",
                site=self.cfg.site_id, subject_tag="progressive-improvement",
                request_id=request_id,
            )
            label = "auto-queue fallback" if handle.fell_back_to_queue else "direct dispatch"
            self.decide("action",
                        f"dispatched {len(rec_ids)} rec(s) to implementer via {label} "
                        f"(request_id={request_id})")
        except Exception as e:
            self.decide("error", f"dispatch failed: {e}")

    def _save_artifact(self, name: str, content) -> None:
        """Write an artifact to BOTH local disk (for human inspection) AND
        framework storage (so the dashboard's per-run drill-down can list +
        render it)."""
        storage_key = f"agents/{self.agent_id}/runs/{self.run_ts}/{name}"
        disk = self.run_dir / name
        if isinstance(content, (dict, list)):
            disk.write_text(json.dumps(content, indent=2))
            self.storage.write_json(storage_key, content)
        else:
            text = str(content)
            disk.write_text(text)
            self.storage.write_text(storage_key, text)

    def _most_recent_recs_path(self) -> Path | None:
        """Find the most recent prior run's recommendations.json for this site."""
        site_runs = self.run_dir.parent  # runs_root/<site>/
        if not site_runs.is_dir():
            return None
        candidates = sorted([d for d in site_runs.iterdir()
                             if d.is_dir() and d != self.run_dir])
        for d in reversed(candidates):
            p = d / "recommendations.json"
            if p.is_file():
                return p
        return None


if __name__ == "__main__":
    ProgressiveImprovementAgent().run_once()
