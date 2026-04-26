"""competitor-research-agent — main entrypoint.

For each configured site:
  1. Fetch homepage of our site + a few key pages → "features-ours.json"
  2. For each configured (or LLM-brainstormed) competitor: fetch homepage +
     a few key pages → ask LLM to extract their feature list →
     "features-theirs.json"
  3. Ask LLM to compare and produce parity-gap + competitive-advantage +
     ux-improvement recommendations
  4. Score, tier, validate, render email
  5. Send + (optionally) auto-dispatch tier=auto recs to implementer

Invoke:
    COMPETITOR_RESEARCH_CONFIG=path/to/site.yaml python3 agent.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# After agents/ consolidation: agent.py is at agents/competitor-research-agent/,
# so the repo root is THREE levels up.
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
    load_quality_config,
    render_recs_email,
    score_tier,
    send_via_msmtp,
    validate_recs_doc,
)

# Reuse the crawler from progressive-improvement-agent — same shape,
# same dependencies, no need to duplicate. After agents/ consolidation
# it lives under agents/progressive-improvement-agent/.
sys.path.insert(0, str(_REPO / "agents" / "progressive-improvement-agent"))
from crawler import Page, crawl  # noqa: E402

import os  # noqa: E402

AGENT_ID = "competitor-research-agent"


# ---------------------------------------------------------------------------
# LLM prompts
# ---------------------------------------------------------------------------

BRAINSTORM_COMPETITORS_SYS = """You suggest competitor websites for a given
project. Return STRICT JSON — a single array of domain strings only, no
prose, no markdown fences. Domains only — no scheme, no path. Skip
ourselves. Suggest sites that solve the same user problem (direct
competitors) plus a couple adjacent ones."""

EXTRACT_FEATURES_SYS = """You extract the discrete features a website
offers, based on a small set of pages from that site.

Return STRICT JSON — no prose, no markdown fences:
{
  "competitor": "<domain>",
  "summary": "one-sentence what they do",
  "features": [
    {"name": "...", "description": "...", "evidence_url": "...", "category": "core | adjacent | unique | gimmick"}
  ]
}

Be conservative. Only list features you can prove from the pages provided.
"""

COMPARE_SYS = """You produce a ranked list of recommendations comparing
our site against competitors.

Return STRICT JSON — a single array of recommendation objects, no prose,
no markdown fences. Each object MUST have:
{
  "category": "parity-feature | competitive-advantage | ux-improvement | content-gap | marketing-positioning | monetization | integrations | other",
  "severity": "critical | high | medium | low",
  "confidence": 0.0-1.0,
  "title": "one-line headline",
  "rationale": "why this matters",
  "competitor": "primary competitor inspiring this (empty for competitive-advantage)",
  "expected_impact": "concrete claim",
  "fix_suggestion": "what would change in our codebase",
  "tier_recommendation": "auto | review | experimental"
}

Rules:
- tier=auto only for narrow, mechanical, fully-derivable changes (e.g., "add
  JSON-LD product schema"). Default review for feature additions, default
  experimental for "what if we built X" speculation.
- De-duplicate. One rec per distinct feature, even if 4 competitors have it.
- Confidence calibrated: 0.95+ means any reasonable reader would agree.
- competitive-advantage recs MUST be speculation grounded in observed gaps,
  not just brainstorming.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_llm_json(raw: str):
    """Tolerant JSON parse — strip markdown fences, find first/last braces."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    for opener, closer in [("[", "]"), ("{", "}")]:
        i = s.find(opener)
        if i >= 0:
            j = s.rfind(closer)
            if j > i:
                s = s[i:j + 1]
                break
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _scan_codebase(codebase_cfg: dict) -> str:
    """Read a repo's representative files (READMEs, top-level docs, key
    source files) and return a concatenated text blob the LLM can analyze.

    Heuristic:
      1. Always include README.md / ARCHITECTURE.md / CHANGELOG.md if present
      2. Include feature_summary_files explicitly listed in config
      3. Include up to max_files matching include_globs (default: top-level
         .md + a sampling of source modules)
      4. Cap each file at max_chars_per_file
    """
    import fnmatch
    import os

    repo = codebase_cfg.get("repo_path", "")
    if not repo or not os.path.isdir(repo):
        return f"(no codebase at {repo!r})"

    max_files       = int(codebase_cfg.get("max_files", 30))
    max_chars       = int(codebase_cfg.get("max_chars_per_file", 5000))
    include_globs   = codebase_cfg.get("include_globs") or [
        "README.md", "*.md", "**/README.md",
        "framework/**/*.py", "src/**/*.py", "src/**/*.ts",
        "package.json", "pyproject.toml", "manifest.json",
    ]
    exclude_globs   = codebase_cfg.get("exclude_globs") or [
        "**/node_modules/**", "**/.git/**", "**/__pycache__/**",
        "**/dist/**", "**/build/**", "**/.venv/**", "**/test*/**",
    ]
    feature_summary = codebase_cfg.get("feature_summary_files") or [
        "README.md", "ARCHITECTURE.md", "CHANGELOG.md", "FEATURES.md",
    ]

    chunks: list[str] = []
    seen: set[str] = set()

    def _read(rel_path: str, prefix: str = "") -> None:
        full = os.path.join(repo, rel_path)
        if rel_path in seen or not os.path.isfile(full):
            return
        seen.add(rel_path)
        try:
            text = open(full, encoding="utf-8", errors="replace").read()[:max_chars]
        except Exception:
            return
        chunks.append(f"\n{'=' * 60}\n{prefix}{rel_path}\n{'=' * 60}\n{text}")

    # 1. Feature-summary files first — they're highest-signal
    for f in feature_summary:
        _read(f, prefix="[FEATURE SUMMARY] ")

    # 2. Then walk include_globs
    count = len(seen)
    for root, dirs, files in os.walk(repo):
        rel_root = os.path.relpath(root, repo)
        # Apply directory-level excludes
        if any(fnmatch.fnmatch(os.path.join(rel_root, d), e) or fnmatch.fnmatch(d, e)
               for d in dirs for e in exclude_globs):
            dirs[:] = [d for d in dirs
                       if not any(fnmatch.fnmatch(d, e.split("/")[0])
                                  for e in exclude_globs if e.endswith("/**"))]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), repo)
            if any(fnmatch.fnmatch(rel, e) for e in exclude_globs):
                continue
            if not any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(f, p)
                       for p in include_globs):
                continue
            if count >= max_files:
                break
            _read(rel)
            count += 1
        if count >= max_files:
            break

    return "".join(chunks) or "(no files matched the configured globs)"


def _format_pages(pages: list[Page], cap_chars: int = 1500) -> str:
    parts = []
    for p in pages:
        body = (p.body_text or "")[:cap_chars]
        parts.append(f"""
URL: {p.url}
TITLE: {p.title}
DESC: {p.description}
H1: {p.h1}
BODY: {body}
---""")
    return "".join(parts)


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class CompetitorResearchAgent(AgentBase):
    agent_id = AGENT_ID
    name = "Competitor Research Agent"
    description = (
        "Compares our site to competitors, recommends parity-gap and "
        "competitive-advantage features."
    )
    category = "research"

    capabilities = [
        declare("crawl_pages", "Fetch our site + competitor homepages",
                confirmation_required=False, risk_level="low"),
        declare("call_ai", "Call configured AI provider for feature extraction + comparison",
                confirmation_required=False, risk_level="low"),
        declare("send_recommendations_email",
                "Send the report email", confirmation_required=False,
                risk_level="low"),
        declare("dispatch_auto_recs",
                "Dispatch auto-tier recs to implementer (only if site config opts in)",
                confirmation_required=False, risk_level="medium",
                affects=["downstream-agent"]),
    ]

    def __init__(self, *args, **kwargs):
        env_id = os.environ.get("AGENT_ID")
        if env_id and env_id != AGENT_ID:
            kwargs.setdefault("agent_id", env_id)
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        path = os.environ.get("COMPETITOR_RESEARCH_CONFIG")
        if not path:
            raise SystemExit("set COMPETITOR_RESEARCH_CONFIG to a path to your site config")
        self.cfg = load_quality_config(path)
        self.run_dir = self.cfg.run_dir_for_now(self.agent_id)
        self.decide("setup",
                    f"site={self.cfg.site_id} run_dir={self.run_dir} agent_id={self.agent_id}",
                    evidence={"site": self.cfg.site_id, "agent_id": self.agent_id})

    def run(self) -> RunResult:
        cfg = self.cfg
        comp_cfg = cfg.get("competitors", {}) or {}
        analyzer_cfg = cfg.get("analyzer", {}) or {}
        crawler_cfg = cfg.get("crawler", {}) or {}

        self.status("starting", progress=0.05,
                    current_action=f"site={cfg.site_id}")

        # ── Apply user replies to prior recs ────────────────────────────────
        prior = self._most_recent_recs_path()
        applied = apply_user_responses(
            responses=self.responses, prior_recs_path=prior)
        if applied:
            self.decide("observation",
                        f"applied {len(applied)} prior-run user responses",
                        evidence={"applied": applied})

        # ── AI client ───────────────────────────────────────────────────────
        try:
            client = self.ai_client()
        except Exception as e:
            self.decide("error", f"ai_client unavailable: {e}")
            return RunResult(status="failure",
                              summary=f"AI provider not configured: {e}")

        # ── 1. Build competitor list ────────────────────────────────────────
        seeds = comp_cfg.get("seed_domains") or []
        max_comp = int(comp_cfg.get("max_competitors", 6))
        if not seeds:
            self.status("brainstorming competitors", progress=0.10)
            try:
                raw = client.chat([
                    {"role": "system", "content": BRAINSTORM_COMPETITORS_SYS},
                    {"role": "user", "content":
                        f"Our site: {cfg.domain}\nWhat we do: {cfg.what_we_do or '(not specified)'}\n"
                        f"Suggest up to {max_comp} competitor domains."},
                ], temperature=0.2, max_tokens=400)
                parsed = _parse_llm_json(raw) or []
                seeds = [d for d in parsed if isinstance(d, str)][:max_comp]
            except Exception as e:
                self.decide("error", f"competitor brainstorm failed: {e}")
                seeds = []
        seeds = [d.strip().lower().lstrip("https://").lstrip("http://").rstrip("/") for d in seeds][:max_comp]
        seeds = [d for d in seeds if d and d != cfg.domain.lower()]
        self.decide("plan",
                    f"comparing against {len(seeds)} competitor(s): {', '.join(seeds[:6])}",
                    evidence={"competitors": seeds})

        self._save_artifact("competitors.json", {
            "ours": cfg.domain, "competitors": seeds,
            "source": "config" if comp_cfg.get("seed_domains") else "llm-brainstorm",
        })

        # ── 2. Discover what WE offer — website crawl OR codebase scan ──────
        scan_mode = cfg.get("scan_mode", "website")
        if scan_mode == "codebase":
            self.status("scanning our codebase", progress=0.20)
            codebase_cfg = cfg.get("codebase", {}) or {}
            ours_text = _scan_codebase(codebase_cfg)
            self.decide("observation",
                        f"scanned codebase at {codebase_cfg.get('repo_path','?')} "
                        f"({len(ours_text)} chars)")
            ours_features = self._extract_features_from_text(
                client, cfg.domain or "ours", ours_text,
            )
            self._save_artifact("codebase-scan.txt", ours_text[:50_000])
        else:
            self.status("crawling our site", progress=0.20)
            ours_pages: list[Page] = []
            for page in crawl(
                base_url=cfg.base_url,
                seed_urls=crawler_cfg.get("seed_urls") or ["/"],
                use_sitemap=crawler_cfg.get("use_sitemap", True),
                max_depth=int(crawler_cfg.get("max_depth", 1)),
                max_pages=int(crawler_cfg.get("max_pages", 12)),
                path_excludes=crawler_cfg.get("path_excludes") or [],
                request_timeout_s=int(crawler_cfg.get("request_timeout_s", 15)),
                user_agent=crawler_cfg.get("user_agent",
                                            "reusable-agents-competitor-research/1.0"),
                throttle_ms=int(crawler_cfg.get("throttle_ms", 500)),
            ):
                if 200 <= page.status_code < 300 and page.body_text:
                    ours_pages.append(page)
            self.decide("observation",
                        f"crawled {len(ours_pages)} pages from {cfg.domain}")
            self.status("extracting our features", progress=0.30)
            ours_features = self._extract_features(client, cfg.domain, ours_pages)
        self._save_artifact("features-ours.json", ours_features)

        # ── 3. Fetch + extract competitor features ──────────────────────────
        per_comp_pages = int(comp_cfg.get("max_pages_per_competitor", 4))
        theirs_features: list[dict] = []
        for i, comp in enumerate(seeds):
            self.status(f"crawling competitor {i+1}/{len(seeds)}: {comp}",
                        progress=0.30 + (0.30 * (i / max(1, len(seeds)))))
            comp_pages: list[Page] = []
            try:
                for page in crawl(
                    base_url=f"https://{comp}",
                    seed_urls=["/", "/features", "/pricing", "/products", "/about"],
                    use_sitemap=False,
                    max_depth=0,
                    max_pages=per_comp_pages,
                    request_timeout_s=int(crawler_cfg.get("request_timeout_s", 15)),
                    user_agent="reusable-agents-competitor-research/1.0",
                    throttle_ms=int(crawler_cfg.get("throttle_ms", 500)),
                ):
                    if 200 <= page.status_code < 300 and page.body_text:
                        comp_pages.append(page)
            except Exception as e:
                self.decide("error", f"competitor {comp} crawl failed: {e}")
                continue
            if not comp_pages:
                continue
            extracted = self._extract_features(client, comp, comp_pages)
            theirs_features.append(extracted)
        self._save_artifact("features-theirs.json", theirs_features)

        # ── 4. Compare → recommendations ────────────────────────────────────
        self.status("comparing + writing recommendations", progress=0.65)
        compare_user = (
            f"Our site: {cfg.domain}\n"
            f"What we do: {cfg.what_we_do or '(not specified)'}\n\n"
            f"OUR FEATURES:\n{json.dumps(ours_features, indent=2)}\n\n"
            f"COMPETITOR FEATURES:\n{json.dumps(theirs_features, indent=2)}\n\n"
            f"Produce up to {analyzer_cfg.get('max_recs_per_run', 15)} ranked recommendations."
        )
        try:
            raw = client.chat([
                {"role": "system", "content": COMPARE_SYS},
                {"role": "user", "content": compare_user},
            ], temperature=0.2, max_tokens=3000)
        except Exception as e:
            self.decide("error", f"compare LLM call failed: {e}")
            return RunResult(status="failure", summary=f"LLM compare failed: {e}")
        raw_recs = _parse_llm_json(raw) or []
        if not isinstance(raw_recs, list):
            raw_recs = []

        # ── 5. Score, tier, validate ────────────────────────────────────────
        threshold = float(analyzer_cfg.get("auto_implement_threshold", 0.95))
        max_recs = int(analyzer_cfg.get("max_recs_per_run", 15))
        recs: list[dict] = []
        for r in raw_recs:
            if not isinstance(r, dict):
                continue
            try:
                conf = float(r.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            sev = r.get("severity", "low")
            # The LLM hints a tier_recommendation; we still gate via score_tier
            tier = score_tier(confidence=conf, severity=sev, threshold=threshold)
            # Honor explicit experimental from LLM (it's a more conservative signal)
            if r.get("tier_recommendation") == "experimental":
                tier = "experimental"
            evidence = []
            comp = r.get("competitor", "")
            if comp:
                evidence.append({"url": f"https://{comp}",
                                  "snippet": r.get("expected_impact", "")[:300]})
            recs.append({
                "category": r.get("category", "other"),
                "severity": sev,
                "confidence": round(conf, 3),
                "tier": tier,
                "title": r.get("title", "(untitled)"),
                "rationale": r.get("rationale", ""),
                "evidence": evidence,
                "competitor": comp,
                "expected_impact": r.get("expected_impact", ""),
                "implementation_outline": {
                    "approach": r.get("fix_suggestion", ""),
                },
                "implemented": False,
            })

        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        tier_order = {"auto": 0, "review": 1, "experimental": 2}
        recs.sort(key=lambda r: (
            sev_order.get(r["severity"], 9),
            tier_order.get(r["tier"], 9),
            -r["confidence"],
        ))
        recs = recs[:max_recs]
        recs = assign_rec_ids(recs)

        # ── 6. Persist + email ──────────────────────────────────────────────
        request_id = new_request_id()
        recs_doc = {
            "schema_version": "1",
            "site": cfg.site_id,
            "agent": AGENT_ID,
            "run_ts": self.run_ts,
            "competitors_analyzed": [t.get("competitor", "") for t in theirs_features],
            "summary": (
                f"Compared {cfg.label} against {len(theirs_features)} competitors. "
                f"{len(recs)} recommendations: "
                f"{sum(1 for r in recs if r['category']=='parity-feature')} parity, "
                f"{sum(1 for r in recs if r['category']=='competitive-advantage')} advantage, "
                f"{sum(1 for r in recs if r['category']=='ux-improvement')} UX. "
                f"{sum(1 for r in recs if r['tier']=='auto')} auto-eligible."
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
                from datetime import datetime as _dt, timezone as _tz
                self.storage.write_json(
                    f"agents/{self.agent_id}/outbound-emails/{request_id}.json",
                    {
                        "schema_version": "1",
                        "request_id": request_id,
                        "agent_id": self.agent_id,
                        "subject": subject,
                        "to": list(to),
                        "expects_response": True,
                        "sent_at": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                        "transport": f"msmtp:{msmtp_account}",
                        "ok": True,
                    },
                )
            else:
                self.decide("error", f"email send failed: {detail}")
        else:
            self.decide("observation",
                        "no recipient/sender configured — email-rendered.html written only")

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
                "competitors_analyzed": len(theirs_features),
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
                "competitors_used": seeds,
            },
        )

    def _extract_features_from_text(self, client, label: str, text: str) -> dict:
        """LLM-extract feature list from a codebase-scan text blob (no URLs)."""
        try:
            raw = client.chat([
                {"role": "system", "content": EXTRACT_FEATURES_SYS},
                {"role": "user", "content":
                    f"Project: {label}\n\nCODEBASE SCAN (truncated):\n{text[:60_000]}"},
            ], temperature=0.1, max_tokens=2500)
        except Exception as e:
            self.decide("error", f"feature extraction (codebase) failed: {e}")
            return {"competitor": label, "summary": "(extraction failed)",
                     "features": [], "error": str(e)}
        parsed = _parse_llm_json(raw)
        if not isinstance(parsed, dict):
            return {"competitor": label, "summary": "(parse failed)",
                     "features": [], "raw": (raw or "")[:500]}
        parsed.setdefault("competitor", label)
        parsed.setdefault("features", [])
        return parsed

    def _extract_features(self, client, domain: str, pages: list[Page]) -> dict:
        """One LLM call per site: extract feature list."""
        try:
            raw = client.chat([
                {"role": "system", "content": EXTRACT_FEATURES_SYS},
                {"role": "user", "content":
                    f"Site: {domain}\n\nPAGES:\n{_format_pages(pages)}"},
            ], temperature=0.1, max_tokens=2000)
        except Exception as e:
            self.decide("error", f"feature extraction for {domain} failed: {e}")
            return {"competitor": domain, "summary": "(extraction failed)",
                     "features": [], "error": str(e)}
        parsed = _parse_llm_json(raw)
        if not isinstance(parsed, dict):
            return {"competitor": domain, "summary": "(parse failed)",
                     "features": [], "raw": (raw or "")[:500]}
        parsed.setdefault("competitor", domain)
        parsed.setdefault("features", [])
        return parsed

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
        site_runs = self.run_dir.parent
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
    CompetitorResearchAgent().run_once()
