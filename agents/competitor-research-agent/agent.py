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
import re
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

# Local accumulator (this dir).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _accumulator import (  # noqa: E402
    load_active, save_active, merge_run, open_proposals, stats as accum_stats,
    transition_state, proposal_id as compute_proposal_id,
)

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

EXTRACT_FEATURES_BATCH_SYS = """You extract the discrete features each
of N websites offers, based on a small set of pages crawled from each site.

Return STRICT JSON — a single object keyed by competitor domain, no prose,
no markdown fences. Schema:
{
  "<domain-1>": {
    "competitor": "<domain-1>",
    "summary": "one-sentence what they do",
    "features": [
      {"name": "...", "description": "...", "evidence_url": "...", "category": "core | adjacent | unique | gimmick"}
    ]
  },
  "<domain-2>": { ... },
  ...
}

Be conservative. Only list features you can prove from the pages provided.
A site with no usable pages should still appear in the output with
features=[] and summary="(no usable pages crawled)".
"""

COMPARE_SYS = """You produce a ranked list of FULLY-BLUEPRINTED feature
recommendations comparing our site against competitors.

The downstream consumer is an automated implementer that ships code
DIRECTLY from your blueprint — there is no human design step between you
and production. Every blueprint you emit must be complete enough that a
junior engineer could build it without asking clarifying questions.

Return STRICT JSON — a single array of recommendation objects, no prose,
no markdown fences. Each object MUST have:
{
  "category": "parity-feature | competitive-advantage | ux-improvement | content-gap | marketing-positioning | monetization | integrations | other",
  "severity": "critical | high | medium | low",
  "confidence": 0.0-1.0,
  "title": "one-line headline",
  "rationale": "why this matters for our users + revenue",
  "competitor": "primary competitor inspiring this (empty for competitive-advantage)",
  "expected_impact": "concrete claim with a metric (e.g. '+8% session length' or '+200 indexed pages')",
  "tier_recommendation": "auto | review | experimental",

  "user_story": "As a <role> I want <capability> so that <outcome>",

  "blueprint": {
    "ui_changes": ["specific component / page / element + what changes"],
    "backend_changes": ["specific service / route / handler + what's added or modified"],
    "data_model_changes": ["table / column / index additions, with types"],
    "api_endpoints": ["METHOD /path — request shape → response shape"],
    "third_party_integrations": ["service + auth approach + rate limits"],
    "edge_cases_and_failure_modes": ["specific edge case + how to handle"],
    "rollout": "feature-flag name + rollout plan + kill-switch",
    "estimated_complexity": "S (≤1 day) | M (2-5 days) | L (1-2 weeks) | XL (>2 weeks)"
  },

  "success_metrics": [
    "primary KPI + target delta",
    "guardrail metric (don't regress)"
  ],

  "fix_suggestion": "1-paragraph plain-English summary of what gets shipped — implementer reads this first to scope the change"
}

Rules:
- Every blueprint MUST have at least 2 entries in ui_changes, backend_changes,
  edge_cases_and_failure_modes. If you can't fill those, the rec isn't
  ready — don't emit it.
- tier=auto only for narrow, mechanical, fully-derivable changes (e.g., "add
  JSON-LD product schema"). Default review for feature additions, default
  experimental for "what if we built X" speculation.
- De-duplicate. One rec per distinct feature, even if 4 competitors have it.
- Confidence calibrated: 0.95+ means any reasonable reader would agree.
- competitive-advantage recs MUST be speculation grounded in observed gaps,
  not just brainstorming. If our site already has the feature in any form,
  do NOT propose it as a competitive-advantage.
- Prefer 5-10 thoroughly-blueprinted recs over 15 shallow ones. Quality > quantity.
- If a rec's blueprint can't fit in a single rec object's budget without
  cutting detail, split into multiple recs (e.g. "phase 1 — UI" + "phase 2 —
  backend") rather than truncating.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_llm_json(raw: str):
    """Tolerant JSON parse — strip markdown fences, then try {object} and
    [array] slices in order of likelihood given the leading non-whitespace
    char. Falls back to the alternative if the first slice fails to parse.

    Bug fix (2026-05-07): the prior version always tried `[` first if any
    `[` existed in the text — but the LLM almost always returns
    `{ "features": [...] }`, and slicing from the first `[` (inside the
    features array) to the last `]` gave a substring that often had a
    trailing `}` or comma left in, causing JSONDecodeError. Both
    competitor-research feature extractions silently emitted 0 features
    every run. Now we sniff the leading char and prefer that shape.
    """
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        s = s.strip()

    # Order: prefer the shape that the response actually starts with. If
    # neither slice parses, return None.
    pairs = [("{", "}"), ("[", "]")] if s.lstrip().startswith("{") else [("[", "]"), ("{", "}")]
    for opener, closer in pairs:
        i = s.find(opener)
        if i < 0:
            continue
        j = s.rfind(closer)
        if j <= i:
            continue
        candidate = s[i:j + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Try trimming trailing commas (common LLM artifact) and a
            # progressive shrink from the right in case the response was
            # truncated mid-element.
            cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
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
    send_run_summary_email = False  # already emails recommendations
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
                "Send the report email — user replies to ship recs via responder",
                confirmation_required=False, risk_level="low"),
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

    # Auto short-circuit deliberately NOT implemented here: competitor
    # pages can change at any time and we can't know without crawling
    # first. The right pattern is `partition_by_hash` *inside* run()
    # after crawling — skip the LLM compare for any competitor whose
    # HTML hash matches the prior run. Tracked as TODO in CLAUDE.md.

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

        # ── 3. Fetch + extract competitor features (BATCHED) ────────────────
        # Crawl all competitors first (no LLM), then extract features for
        # ALL of them in ONE structured-output call. Replaces the prior
        # N-call-per-run loop where each competitor cost a separate LLM
        # request — at typical max_competitors=6 + an additional comparison
        # call later, this cuts the per-run premium budget burn by ~6×.
        per_comp_pages = int(comp_cfg.get("max_pages_per_competitor", 4))
        comp_pages_by_domain: dict[str, list[Page]] = {}
        for i, comp in enumerate(seeds):
            self.status(f"crawling competitor {i+1}/{len(seeds)}: {comp}",
                        progress=0.30 + (0.20 * (i / max(1, len(seeds)))))
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
            if comp_pages:
                comp_pages_by_domain[comp] = comp_pages

        self.status("extracting all competitor features (batched LLM call)",
                    progress=0.55)
        theirs_features = self._extract_features_batched(client, comp_pages_by_domain)
        self._save_artifact("features-theirs.json", theirs_features)

        # ── 3b. Optional app-store competitor scan ──────────────────────────
        # When the product also ships as a mobile app, site.yaml can list
        # iOS / Android competitors under `app_stores:`. We pull metadata
        # from the iTunes Search API + the Google Play public store and
        # fold descriptions + screenshots into the comparison context so
        # the LLM sees app-side feature lists alongside web-side ones.
        app_store_cfg = cfg.get("app_stores") or {}
        if app_store_cfg:
            try:
                from framework.core.app_store_clients import (
                    lookup_itunes, search_play, _play_detail,
                )
                self.status("fetching app-store metadata", progress=0.58)
                country = (app_store_cfg.get("country") or "us").lower()
                ios_competitors = list(app_store_cfg.get("competitors", {}).get("ios") or [])
                android_competitors = list(app_store_cfg.get("competitors", {}).get("android") or [])
                ours_ios = app_store_cfg.get("ios_app_id")
                ours_android = app_store_cfg.get("android_package")

                ios_apps = lookup_itunes(
                    [str(x) for x in ([ours_ios] if ours_ios else []) + ios_competitors],
                    country=country,
                ) if (ours_ios or ios_competitors) else []
                android_apps: list[dict] = []
                for pkg in ([ours_android] if ours_android else []) + android_competitors:
                    if not pkg:
                        continue
                    try:
                        d = _play_detail(str(pkg), country)
                        if d:
                            android_apps.append(d)
                    except Exception:
                        continue

                # Split ours vs theirs.
                ours_apps = [a for a in (ios_apps + android_apps)
                             if (a.get("store") == "ios" and str(a.get("store_id")) == str(ours_ios)) or
                                (a.get("store") == "android" and str(a.get("store_id")) == str(ours_android))]
                theirs_apps = [a for a in (ios_apps + android_apps) if a not in ours_apps]
                self._save_artifact("app-stores-ours.json", ours_apps)
                self._save_artifact("app-stores-theirs.json", theirs_apps)
                self.decide("observation",
                            f"app-store scan: {len(ours_apps)} ours · {len(theirs_apps)} competitor app(s)")

                # Project app metadata into the same shape the comparison
                # call already understands: list of feature-summary strings.
                def _flatten(app: dict) -> list[str]:
                    out = []
                    if app.get("name"): out.append(f"App: {app['name']} ({app.get('store')})")
                    if app.get("description"):
                        desc = app["description"][:1500]
                        out.append(f"Store description: {desc}")
                    if app.get("average_rating"):
                        out.append(f"Rating: {app['average_rating']} ({app.get('review_count')} reviews)")
                    if app.get("days_since_update"):
                        out.append(f"Days since last update: {app['days_since_update']}")
                    return out
                if ours_apps:
                    ours_features.setdefault("app_store_signals", [])
                    for a in ours_apps:
                        ours_features["app_store_signals"].extend(_flatten(a))
                for a in theirs_apps:
                    domain_key = f"appstore::{a.get('store')}::{a.get('store_id')}"
                    theirs_features[domain_key] = _flatten(a)
            except Exception as e:
                self.decide("error", f"app-store scan failed: {e}")

        # ── 4. Compare → recommendations ────────────────────────────────────
        self.status("comparing + writing recommendations", progress=0.65)
        # Cap recs to keep the blueprint output budget tractable.
        # Thorough blueprints take ~600-900 tokens each; 8 recs ≈ 6500
        # tokens which fits comfortably in claude-sonnet's response time.
        max_recs = min(int(analyzer_cfg.get("max_recs_per_run", 8)), 8)

        # ── Memory: pass the accumulator's existing proposals to the LLM
        # so it doesn't re-emit recs that were already proposed (and
        # possibly already implemented or deferred by the operator).
        # Without this the agent re-generates the same 8 recs every run,
        # because the prompt only sees current-state features and has no
        # awareness that some gaps have already been filed/shipped.
        prior_accum = load_active(self.storage, self.agent_id)
        prior_summary_lines: list[str] = []
        for p in prior_accum.get("proposals", [])[:200]:
            state = p.get("state", "open")
            # Surface implemented/shipped to the LLM as "do not re-emit",
            # and open ones as "already in backlog — skip duplicates".
            title = (p.get("title") or "").strip()[:140]
            if not title:
                continue
            prior_summary_lines.append(f"  [{state}] {title}")
        prior_block = ""
        if prior_summary_lines:
            prior_block = (
                "\n\nPREVIOUSLY-PROPOSED RECOMMENDATIONS (already in our "
                "backlog or shipped — DO NOT re-emit anything substantively "
                "equivalent to these; if you would propose one of these "
                "again, SKIP it and emit a DIFFERENT gap instead):\n"
                + "\n".join(prior_summary_lines[:120])
            )

        compare_user = (
            f"Our site: {cfg.domain}\n"
            f"What we do: {cfg.what_we_do or '(not specified)'}\n\n"
            f"OUR FEATURES:\n{json.dumps(ours_features, indent=2)}\n\n"
            f"COMPETITOR FEATURES:\n{json.dumps(theirs_features, indent=2)}"
            f"{prior_block}\n\n"
            f"Produce up to {max_recs} thoroughly-blueprinted recommendations. "
            f"Quality > quantity — fewer fully-specified recs beats many shallow ones. "
            f"NEVER propose a feature substantively equivalent to anything in the "
            f"PREVIOUSLY-PROPOSED list above — pick a different gap instead."
        )
        try:
            # Use AgentBase's chat_with_fallback so claude→copilot→ollama
            # auto-falls through on timeout/rate-limit. Direct client.chat
            # calls don't get fallback. We also cap max_tokens at 6000 to
            # keep claude's response time inside its 600s CLI timeout.
            raw = self.ai_chat([
                {"role": "system", "content": COMPARE_SYS},
                {"role": "user", "content": compare_user},
            ], temperature=0.2, max_tokens=6000)
        except Exception as e:
            self.decide("error", f"compare LLM call failed (after fallback): {e}")
            return RunResult(status="failure", summary=f"LLM compare failed: {e}")
        # Persist the raw compare response for debugging — silent 0-rec
        # runs are nearly always a parse failure or token truncation.
        try:
            self._save_artifact("compare-raw.txt", raw or "")
        except Exception:
            pass
        raw_recs = _parse_llm_json(raw) or []
        # Some LLMs wrap recs in {"recommendations": [...]} — unwrap.
        if isinstance(raw_recs, dict):
            for k in ("recommendations", "recs", "items"):
                if isinstance(raw_recs.get(k), list):
                    raw_recs = raw_recs[k]
                    break
            else:
                raw_recs = []
        if not isinstance(raw_recs, list):
            raw_recs = []
        if not raw_recs:
            self.decide(
                "compare-empty",
                f"compare LLM returned no parseable recs (raw len={len(raw or '')})",
            )

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
                # Preserve the LLM's structured blueprint (ui_changes,
                # backend_changes, data_model_changes, api_endpoints, etc.)
                # so downstream consumers (implementer, dashboard) see the
                # full spec. Falls back to legacy implementation_outline.
                "user_story": r.get("user_story", ""),
                "blueprint": r.get("blueprint", {}) if isinstance(r.get("blueprint"), dict) else {},
                "success_metrics": r.get("success_metrics", []) if isinstance(r.get("success_metrics"), list) else [],
                "implementation_outline": {
                    "approach": r.get("fix_suggestion", "") or r.get("rationale", ""),
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

        # Apply any user replies that arrived since the last run (mark
        # proposals implemented/skipped/deferred in the accumulator).
        # AgentBase populates self.responses with auto-queue entries.
        self._apply_responses_to_accumulator()

        # Merge this run's new recs into the cross-run accumulator and
        # save back to storage. The accumulator is the source of truth
        # for the email body — it holds every still-open proposal across
        # all prior runs.
        accum = load_active(self.storage, self.agent_id)
        accum = merge_run(accum, new_recs=recs, site_id=cfg.site_id, run_ts=self.run_ts)
        save_active(self.storage, self.agent_id, accum)
        accum_counts = accum_stats(accum)
        self.decide(
            "accumulator",
            f"open={accum_counts['open']} implemented={accum_counts['implemented']} "
            f"deferred={accum_counts['deferred']} skipped={accum_counts['skipped']} "
            f"(this run added/refreshed {len(recs)} candidate(s))",
        )

        # Email body now includes ALL open proposals — not just this run's.
        # Re-assign rec_ids on the open list so the user can reply
        # `implement rec-001` and the responder routes to the matching
        # proposal_id (we map back via the rec_ids→proposal_id table
        # below). Persist the run's recommendations.json with the new
        # run's recs only (for run-history visibility).
        recs_doc = {
            "schema_version": "1",
            "site": cfg.site_id,
            "agent": AGENT_ID,
            "agent_id": self.agent_id,
            "run_ts": self.run_ts,
            "competitors_analyzed": [t.get("competitor", "") for t in theirs_features],
            "summary": (
                f"Compared {cfg.label} against {len(theirs_features)} competitors. "
                f"{len(recs)} new candidate(s) this run; "
                f"{accum_counts['open']} total open proposal(s) in backlog."
            ),
            "recommendations": recs,
            "accumulator_counts": accum_counts,
        }
        validate_recs_doc(recs_doc)
        self._save_artifact("recommendations.json", recs_doc)

        # Render email from the FULL open list, not just this run.
        # assign_rec_ids re-numbers in priority order so the user's
        # reply syntax (`implement rec-005`) maps cleanly. We also
        # persist the (rec_id → proposal_id) map alongside the email
        # so the responder can update the accumulator on reply.
        open_list = open_proposals(accum)
        # Cap email body to keep it readable. The full backlog is
        # always visible on the dashboard's accumulator view; the email
        # surfaces the top-N most-actionable.
        email_cap = int(
            (cfg.get("reporter") or {}).get("email", {}).get("backlog_cap", 50)
        )
        email_open_list = open_list[:email_cap]
        truncated = max(0, len(open_list) - email_cap)

        # Convert accumulator entries → rec dicts the email renderer
        # understands. They already share most fields; just ensure id
        # is unset so assign_rec_ids overwrites cleanly.
        email_recs = [dict(p) for p in email_open_list]
        for er in email_recs:
            er.pop("id", None)
        email_recs = assign_rec_ids(email_recs)

        # Build the rec_id → proposal_id map for the responder.
        rec_id_map = {r["id"]: r["proposal_id"] for r in email_recs if r.get("proposal_id")}
        self._save_artifact("rec-id-to-proposal-id.json", rec_id_map)

        truncated_note = (
            f" ({truncated} additional proposal(s) hidden — see dashboard for full backlog)"
            if truncated else ""
        )
        email_summary = (
            f"📋 {accum_counts['open']} open proposal(s) — "
            f"{len(recs)} new this run · "
            f"{accum_counts['implemented']} already implemented · "
            f"{accum_counts['deferred']} deferred · "
            f"{accum_counts['skipped']} skipped."
            f"{truncated_note} "
            f"Reply `implement rec-NNN` to ship, `skip rec-NNN` to "
            f"drop it from future emails, `defer rec-NNN` to revisit later."
        )

        subject, html = render_recs_email(
            cfg=cfg, agent_id=self.agent_id, request_id=request_id,
            recs=email_recs, summary=email_summary,
        )
        self._save_artifact("email-rendered.html", html)

        email_cfg = (cfg.get("reporter", {}) or {}).get("email") or {}
        to = email_cfg.get("to") or []
        sender = email_cfg.get("from", "")
        msmtp_account = email_cfg.get("msmtp_account", "automation")
        if to and sender:
            # Comp-research emails are first-class deliverables, NOT
            # status alerts — bypass the digest queue so the operator
            # sees them immediately rather than rolled up every 5h.
            ok, detail = send_via_msmtp(
                subject=subject, body_html=html, to=to,
                sender=sender, msmtp_account=msmtp_account,
                extra_headers={
                    "X-Reusable-Agent": self.agent_id,
                    "Reply-To": sender,
                },
                bypass_digest=True,
            )
            if ok:
                self.decide("action", f"emailed {len(to)} recipient(s): {detail}")
            else:
                self.decide("error", f"email send failed: {detail}")
            # `detail` distinguishes Graph from msmtp from digest-mode:
            #   Graph success      → "graph send_as ok"
            #   msmtp success      → "sent to N recipient(s)"
            #   digest suppression → "suppressed: digest-mode"
            # Record verbatim so the Confirmations tab can show actual transport.
            actual_transport = "graph" if "graph" in (detail or "").lower() \
                else "msmtp" if "sent to" in (detail or "") \
                else "digest" if "digest" in (detail or "").lower() \
                else "unknown"
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
                    "transport": actual_transport,
                    "transport_detail": detail or "",
                    "msmtp_account": msmtp_account,
                    "ok": bool(ok),
                },
            )
        else:
            self.decide("observation",
                        "no recipient/sender configured — email-rendered.html written only")

        # 2026-05-12: comp-research USED to be hardcoded "email-only"
        # (never auto-dispatched) because competitive-feature-gap recs
        # are judgment-heavy. That hardcode is now replaced with the
        # unified `auto_implement` gate — site.yaml is the single source
        # of truth. Default is FALSE in comp-research's site.yaml so
        # behavior is unchanged (still waits for email approval). To
        # auto-ship comp-research recs in the future, flip site.yaml's
        # `auto_implement: true`.
        rec_ids = [r["id"] for r in recs if r.get("id")]
        if rec_ids:
            from framework.core import dispatch as _dispatch
            _dispatch.gated_dispatch_now(
                cfg=cfg, agent=self,
                agent_id=self.agent_id,
                run_dir=str(self.run_dir),
                rec_ids=rec_ids,
                site=cfg.site_id,
                subject_tag="competitor-research",
            )

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
                "awaiting_user_reply": len(recs),
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
        """One LLM call per site: extract feature list. Kept for backward
        compatibility (tests + the our-site path still call this), but the
        per-competitor loop now uses _extract_features_batched."""
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

    def _extract_features_batched(
        self, client, pages_by_domain: dict[str, list[Page]],
    ) -> list[dict]:
        """ONE LLM call covering ALL competitors at once.

        Returns list[{competitor, summary, features:[...]}] in the order
        domains were requested. Falls back to per-site extraction only
        when the batched call fails completely (parse error / empty
        response).
        """
        if not pages_by_domain:
            return []
        # Build the user prompt — one section per competitor.
        sections = []
        for domain, pages in pages_by_domain.items():
            sections.append(
                f"### COMPETITOR: {domain}\n"
                f"PAGES:\n{_format_pages(pages)}"
            )
        user_prompt = (
            f"Extract features for {len(pages_by_domain)} competitor(s). "
            f"Respond with one JSON object keyed by domain, including ALL "
            f"of these domains: {list(pages_by_domain.keys())}\n\n"
            + "\n\n".join(sections)
        )
        try:
            raw = client.chat([
                {"role": "system", "content": EXTRACT_FEATURES_BATCH_SYS},
                {"role": "user", "content": user_prompt},
            ], temperature=0.1, max_tokens=4500)  # ~750 tok per competitor
        except Exception as e:
            self.decide("error",
                        f"batched feature extraction failed ({e}); "
                        f"falling back to per-competitor calls")
            return [
                self._extract_features(client, d, p)
                for d, p in pages_by_domain.items()
            ]

        parsed = _parse_llm_json(raw)
        # _parse_llm_json normalises a dict into [dict] — unwrap.
        out_map: dict[str, dict] = {}
        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                # Two shapes possible:
                #   (a) {"<domain>": {...}, "<domain2>": {...}} → unwrap
                #   (b) {"competitor": "<domain>", ...}        → use as-is
                if "competitor" in item and "features" in item:
                    out_map[item["competitor"]] = item
                else:
                    for k, v in item.items():
                        if isinstance(v, dict):
                            out_map[k] = v

        results: list[dict] = []
        missing: list[str] = []
        for domain in pages_by_domain.keys():
            entry = out_map.get(domain)
            if not isinstance(entry, dict):
                missing.append(domain)
                continue
            entry.setdefault("competitor", domain)
            entry.setdefault("features", [])
            results.append(entry)

        if missing:
            self.decide("error",
                        f"batched extract missed {len(missing)} domain(s): "
                        f"{missing}; running per-site fallback for those")
            for d in missing:
                results.append(
                    self._extract_features(client, d, pages_by_domain[d])
                )

        self.decide(
            "observation",
            f"batched feature extraction: 1 LLM call covered "
            f"{len(pages_by_domain)} competitor(s) "
            f"({len(missing)} fallbacks)",
            evidence={"total": len(pages_by_domain), "fallbacks": len(missing)},
        )
        return results

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

    def _apply_responses_to_accumulator(self) -> None:
        """Translate user replies (`implement rec-001`, `skip rec-002`,
        `defer rec-003`) into state changes on the accumulator.

        AgentBase.responses contains per-rec actions the responder-agent
        parsed from email replies. The rec_ids in those replies refer to
        the LAST email's numbered list — we translate via the
        rec-id-to-proposal-id.json artifact saved alongside that email.
        """
        responses = list(getattr(self, "responses", []) or [])
        if not responses:
            return

        # Find the most recent prior run-dir's rec_id_map artifact.
        prior_map: dict[str, str] = {}
        try:
            site_runs = self.run_dir.parent
            if site_runs.is_dir():
                for d in sorted(site_runs.iterdir(), reverse=True):
                    if not d.is_dir() or d == self.run_dir:
                        continue
                    p = d / "rec-id-to-proposal-id.json"
                    if p.is_file():
                        prior_map = json.loads(p.read_text())
                        break
        except Exception:
            pass

        if not prior_map:
            self.decide("warn",
                        f"got {len(responses)} response(s) but no prior "
                        f"rec_id→proposal_id map found — skipping accumulator update")
            return

        accum = load_active(self.storage, self.agent_id)
        applied = 0
        for resp in responses:
            action = (resp.get("action") or "").lower()
            rec_id = resp.get("rec_id") or ""
            pid = prior_map.get(rec_id)
            if not pid:
                continue
            target_state = {
                "implement": "implemented",
                "ship": "implemented",
                "skip": "skipped",
                "defer": "deferred",
            }.get(action)
            if not target_state:
                continue
            if transition_state(accum, pid, target_state,
                                reason=f"user reply '{action} {rec_id}'"):
                applied += 1
                self.decide(
                    "accumulator-state",
                    f"{rec_id} ({pid}) → {target_state}",
                )
        if applied:
            save_active(self.storage, self.agent_id, accum)
            self.decide("accumulator", f"applied {applied} state change(s) from user replies")


if __name__ == "__main__":
    CompetitorResearchAgent().run_once()
