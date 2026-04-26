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
     to the configured implementer (default seo-implementer) by writing
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

class ProgressiveImprovementAgent(AgentBase):
    agent_id = AGENT_ID
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
        for i in range(0, len(valid_pages), batch_size):
            batch = valid_pages[i:i + batch_size]
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
            self.status(f"analyzed {min(i + batch_size, len(valid_pages))}/{len(valid_pages)} pages",
                        progress=0.55 + min(0.30, (i / max(1, len(valid_pages))) * 0.30))

        # ── 4. Score, tier, cap ─────────────────────────────────────────────
        threshold = float(analyzer_cfg.get("auto_implement_threshold", 0.95))
        max_recs = int(analyzer_cfg.get("max_recs_per_run", 15))
        recs: list[dict] = []
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
