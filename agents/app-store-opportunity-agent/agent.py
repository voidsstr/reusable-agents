"""app-store-opportunity-agent — main entrypoint.

Per run:
  1. Search iOS App Store + Google Play for the configured queries.
  2. Apply eligibility filters (review count, rating range, stale-update,
     publisher exclusions).
  3. Ask the LLM (one batched call per ≤8 apps) to score build_complexity
     1-5, revenue_potential, decline_thesis, v2 pitch, feature gaps.
  4. Compute deterministic opportunity_score + rank_signals.
  5. Apply user replies from the prior email (`pursue oppN`, `pass oppN`).
  6. Merge into accumulator at agents/<id>/opportunities/active.json.
  7. Render and send the operator email with the top-N open opportunities,
     sorted by (revenue_potential / build_complexity) desc.

Invoke:
    APP_STORE_OPPORTUNITY_CONFIG=path/to/config.yaml python3 agent.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo root on sys.path
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Local lib
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _accumulator import (  # noqa: E402
    load_active, save_active, merge_run, open_opportunities,
    stats as accum_stats, transition_state, opportunity_id,
)
from lib import app_stores  # noqa: E402
from lib.scoring import opportunity_score  # noqa: E402

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402
from framework.core.email_codes import new_request_id  # noqa: E402

try:
    from framework.core.ai_providers import ai_client_for, chat_with_fallback  # noqa: E402
except ImportError:  # local dev fallback
    ai_client_for = None  # type: ignore
    chat_with_fallback = None  # type: ignore

import yaml  # type: ignore


_BLUEPRINT_SYS = """You are a senior product architect generating a complete
plan-mode build spec for a competing 'better v2' app.

The output is a SET OF FILES that will be written to storage at
agents/<agent>/blueprints/<opportunity-id>/. The implementer reads them
and ships a polished, end-to-end application — React Native frontend
(iOS + Android), Node.js + Postgres backend, Docker for delivery.

You produce the spec; the implementer writes the code. Be detailed
enough that a junior developer with the spec + the implementer's LLM
could ship a working app without further questions.

Design principles to bake into every spec:
  • UX FIRST — modern, calm, purposeful interactions. Read the
    competitor app's flaws (low rating, listed feature_gaps,
    decline_thesis) and design AROUND them. Bias to native iOS / iOS
    HIG and Material 3 conventions; avoid skeuomorphism.
  • Pixel polish — typography hierarchy, generous spacing, soft
    shadows, considered colour palette. Light + dark mode from day one.
  • Accessibility by default — WCAG AA contrast, dynamic type, screen
    reader labels, hit targets ≥44pt.
  • Onboarding < 30s to "first value" — no email walls before showing
    the user something useful.
  • Engagement loop — name the daily/weekly habit and design 3 retention
    triggers (notification, streak, social).
  • Monetization plan — free tier + paid plan; specify price points
    benchmarked to the category.

Return STRICT JSON of this exact shape:
{
  "files": {
    "README.md":              "<entry-point file index + 'how to read this blueprint'>",
    "product-spec.md":        "<vision, target user, MVP feature list (5-9 bullets), v2/v3 ladder, non-goals, success metrics>",
    "ux-research.md":         "<analysis of the target competitor + 2-3 named adjacent apps; what to copy, what to fix; user-complaint themes from public reviews>",
    "ux-spec.md":             "<colour palette (5-7 hex), type scale, key screens (onboarding, home, detail, settings) each with: layout, components, primary action, micro-copy)>",
    "tech-stack.md":          "<frontend (RN+Expo+TypeScript+state mgmt), backend (Node+Express+Postgres+migrations), infra (Docker compose for dev, Container Apps for prod), CI (GH Actions), observability>",
    "api-spec.md":             "<REST endpoint catalog with method/path/auth/request body/response shape>",
    "data-model.md":          "<Postgres tables: name, columns + types, FKs, indexes; ERD in mermaid>",
    "implementation-plan.md": "<file tree of the repo; ordered milestones (M1 onboarding, M2 core loop, M3 monetization); commit-by-commit ordering for the implementer>"
  }
}

Return ONLY the JSON object. No prose, no markdown fence, no commentary.
Each file's content should be production-ready Markdown (or JSON for the
ERD if you embed JSON), not placeholder text.
"""


_BLUEPRINT_USER_TPL = """## Reference application architecture (CANONICAL — follow this)
{reference_arch}

## Target app (the competitor we're displacing)
{app}

## Tech stack baseline (must align with the reference architecture above)
{stack}

## Adjacent apps in the same category (use for UX research)
{peers}

Generate the complete blueprint for the v2 of this app. Be specific —
no placeholders. Lean into what the competitor's users actually
complain about (low rating + feature_gaps + decline_thesis above).
The implementation-plan.md you produce MUST scaffold the file tree
defined in the reference architecture (frontend/, mobile/, src/,
prisma/, packages/shared/, agents/, scripts/, tests/, azure/, plus
the standard Dockerfiles and docker-compose.yml).
"""


_PLAN_SYS = """You are the search planner for an app-store-opportunity scout.
Each run, you choose WHERE to look — countries, genres, search queries,
and regional-gap pairs — based on what the accumulator has already
discovered and which goals are lagging.

The hunt brief is fixed:
  • Find apps that are POPULAR (large userbase, many reviews) but
    LOW-RATED (visible user friction, ≤4.0 stars), in domains where
    a small team can plausibly build a better v2.
  • Bonus: find apps popular in some countries but ABSENT in others
    (regional gaps — same need, no competitor in those markets).

Avoid rediscovering apps already in the accumulator; bias toward
new genres/countries/queries.

Output STRICT JSON with this shape:
{
  "countries":        [<2-letter ISO codes; pick 1-3 markets to sweep
                        top charts in, e.g. ["us", "gb", "de"]>],
  "genre_ids":        [<iTunes genre ids to focus on this run; 3-6 ids
                        from the catalog the user provides>],
  "search_queries":   [<3-7 keyword queries that target niches
                        unlikely to chart but with real demand,
                        e.g. "expense splitter", "habit chain">],
  "gap_check": {
    "popular_in":     [<2-3 countries to scan for popular apps>],
    "missing_in":     [<2-4 countries to check absence in;
                        markets you suspect are underserved>]
  },
  "rationale":        "<one sentence: why this plan moves the needle>"
}
"""

_PLAN_USER_TPL = """## Accumulator state (compact summary)
{accum_summary}

## Goal progress
{goal_summary}

## iTunes genre catalog (id → name)
{genres}

## Country pool
You may pick from any 2-letter ISO storefront code Apple supports.
Suggested sources for popularity: us, gb, de, fr, jp, kr, br, in, mx, au, ca.
Suggested underserved markets to check absence in: br, mx, in, id, ph, pl, tr, vn, eg, ng, za.

## Last 3 runs' top picks (avoid rediscovery)
{recent_top_picks}

Return ONLY the JSON object, no prose, no markdown fence.
"""

_LLM_PROMPT = """You are evaluating apps for a competing-product opportunity scan.
For each app below, respond with a JSON array of objects (one per app, in order),
each with:
  store_id:           <copied verbatim>
  build_complexity:   integer 1-5 — 1 trivial, 5 huge. Consider:
                      external integrations, regulated domain, real-time/sync,
                      specialized hardware (BLE, CV, AR), marketplace/network
                      effects, content-production cost.
  complexity_drivers: 1-3 short bullets explaining the score
  revenue_potential:  one of "low", "medium", "high", "very_high"
                      ("high" = $5k-50k MRR plausible; "very_high" = $50k+).
  revenue_thesis:     1 sentence on monetization angle
  decline_thesis:     1 sentence on why this app is vulnerable now
  feature_gaps:       2-4 short bullets on what users complain about / want
  v2_pitch:           1 sentence pitch for the "better v2"
  moats_to_take:      1-3 short bullets on what would make our v2 sticky
Return ONLY the JSON array. No prose, no markdown fence.

Apps:
{apps}
"""


def _load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _slim_for_llm(app: dict) -> dict:
    desc = (app.get("description") or "").strip()
    if len(desc) > 1500:
        desc = desc[:1500] + "…"
    return {
        "store_id": app.get("store_id"),
        "store": app.get("store"),
        "name": app.get("name"),
        "publisher": app.get("publisher"),
        "category": app.get("category"),
        "rating": app.get("average_rating"),
        "reviews": app.get("review_count"),
        "version": app.get("current_version"),
        "last_updated_at": app.get("last_updated_at"),
        "days_since_update": app.get("days_since_update"),
        "price": app.get("price"),
        "description": desc,
    }


_REV_LABELS = {"low", "medium", "high", "very_high"}


class AppStoreOpportunityAgent(AgentBase):
    agent_id = "app-store-opportunity-agent"

    def run(self, **kwargs) -> RunResult:
        cfg_path = os.environ.get("APP_STORE_OPPORTUNITY_CONFIG", "config.yaml")
        if not Path(cfg_path).exists():
            cfg_path_local = Path(__file__).parent / cfg_path
            if cfg_path_local.exists():
                cfg_path = str(cfg_path_local)
        cfg = _load_yaml(cfg_path)
        self.status("collecting", current_action=f"config={cfg_path}", progress=0.05)

        # ---- 1. LLM-planned discovery ----
        # Each run, the LLM picks countries/genres/queries to scan based
        # on accumulator state + goal progress. This concentrates budget
        # on uncharted areas instead of re-sweeping the same US top-100.
        from framework.core.app_store_clients import (  # noqa: E402
            sweep_itunes_top_charts, discover_country_gaps, ITUNES_GENRES,
        )
        max_apps = int((cfg.get("discovery") or {}).get("max_apps_per_run", 200))
        per_query = int((cfg.get("search") or {}).get("results_per_query", 25))

        # Pre-load the accumulator early so the planner sees prior state.
        early_accum = load_active(self.storage, self.agent_id)
        plan = self._plan_search(early_accum, cfg)
        self._save_artifact("search-plan.json", plan)
        self.status("collecting", current_action=f"plan: {plan.get('rationale','')[:60]}", progress=0.1)

        countries = plan.get("countries") or ["us"]
        genre_ids = plan.get("genre_ids") or list(ITUNES_GENRES.keys())[:8]
        queries   = plan.get("search_queries") or []
        gap_cfg   = plan.get("gap_check") or {}

        discovered: dict[str, dict] = {}

        # 1a. Top-charts sweep in the planned countries + genres
        for country in countries:
            self.status("collecting",
                        current_action=f"top-charts {country} ({len(genre_ids)} genres)",
                        progress=0.15)
            for app in sweep_itunes_top_charts(
                country=country, kinds=("free", "grossing"),
                overall_limit=50, genre_ids=genre_ids, per_genre_limit=25,
            ):
                discovered.setdefault(f"ios::{app['store_id']}", app)
        self.decide("observation",
                    f"top-charts collected {len(discovered)} unique apps "
                    f"across countries={countries} genres={genre_ids}")

        # 1b. LLM-suggested keyword searches (niche depth)
        for q in queries:
            for country in countries:
                self.status("collecting", current_action=f"search:itunes:{q[:30]}",
                            progress=0.25)
                for app in app_stores.search_itunes(q, country=country, limit=per_query):
                    discovered.setdefault(f"ios::{app['store_id']}", app)
                try:
                    for app in app_stores.search_play(q, country=country, limit=per_query):
                        discovered.setdefault(f"android::{app['store_id']}", app)
                except Exception as e:
                    self.decide("observation",
                                f"play scrape failed for {q!r}/{country}: {e}")

        # 1c. Regional-gap discovery — apps popular in N countries, absent from M.
        gap_apps: list[dict] = []
        if gap_cfg.get("popular_in") and gap_cfg.get("missing_in"):
            self.status("collecting",
                        current_action=f"gap-check {gap_cfg.get('popular_in')} → {gap_cfg.get('missing_in')}",
                        progress=0.30)
            try:
                gap_apps = discover_country_gaps(
                    popular_in_countries=list(gap_cfg["popular_in"]),
                    missing_in_countries=list(gap_cfg["missing_in"]),
                    overall_limit=30, per_genre_limit=15,
                    genre_ids=genre_ids,
                )
                for app in gap_apps:
                    discovered.setdefault(f"ios::{app['store_id']}", app)
                self.decide("observation",
                            f"regional-gap discovery: {len(gap_apps)} apps popular in "
                            f"{gap_cfg['popular_in']} but absent from "
                            f"{gap_cfg['missing_in']}")
            except Exception as e:
                self.decide("error", f"regional-gap discovery failed: {e}")

        all_apps = list(discovered.values())
        self.decide("observation", f"discovered {len(all_apps)} unique apps before filters")

        # ---- 2. Eligibility filters ----
        # HARD low-rating gate: every candidate MUST be rated below
        # `max_average_rating` (default 4.0). Top-rated apps are
        # deliberately excluded — they're hard to replace and rarely have
        # a v2 angle that survives execution. The decline_signal in
        # scoring then disambiguates between "low rated everywhere" and
        # "low rated AND stale AND highly-reviewed" (the strongest signal).
        #
        # Two-tier review-count model on top of the rating gate:
        #   • neglected — moderate userbase + stale update
        #   • popular   — large userbase (regardless of update cadence)
        f = cfg.get("filters", {}) or {}
        excl_pub = {p.lower() for p in f.get("exclude_publishers", []) or []}
        excl_kw = [k.lower() for k in f.get("exclude_keywords", []) or []]

        # GLOBAL — applies to every tier.
        max_rating_global = float(f.get("max_average_rating",
                                        f.get("global_max_rating", 4.0)))
        min_rating_global = float(f.get("min_average_rating", 0.0))

        # Tier 1: "neglected" — moderate userbase + stale update
        neg_min_reviews = int(f.get("neglected_min_reviews",
                                    f.get("min_review_count", 500)))
        neg_min_stale   = int(f.get("neglected_min_days_since_update",
                                    f.get("min_days_since_update", 180)))
        # Tier 2: "popular" — huge userbase override; staleness ignored
        pop_min_reviews = int(f.get("popular_min_reviews", 50000))

        def in_excludes(app: dict) -> bool:
            if (app.get("publisher") or "").lower() in excl_pub:
                return True
            blob = (app.get("name", "") + " " + app.get("description", "")).lower()
            if any(k in blob for k in excl_kw):
                return True
            return False

        def passes(app: dict) -> bool:
            if in_excludes(app):
                return False
            rating = float(app.get("average_rating") or 0)
            # Hard rating gate — top-rated apps are excluded fleet-wide.
            if rating > max_rating_global or rating < min_rating_global:
                return False
            reviews = int(app.get("review_count") or 0)
            stale = int(app.get("days_since_update") or 0)
            # neglected tier
            if reviews >= neg_min_reviews and stale >= neg_min_stale:
                return True
            # popular tier — large userbase regardless of update cadence
            if reviews >= pop_min_reviews:
                return True
            return False

        eligible = [a for a in all_apps if passes(a)][:max_apps]
        self.decide("observation", f"{len(eligible)} apps eligible (of {len(all_apps)})")
        self.status("analyzing", current_action=f"{len(eligible)} eligible", progress=0.3)

        # ---- 3. LLM analysis (batched) ----
        analyzed = self._llm_analyze(eligible, cfg)

        # ---- 4. Score ----
        weights = (cfg.get("scoring") or {}).get("weights") or {
            "decline_signal": 0.30, "install_base": 0.25,
            "monetization": 0.25, "simplicity": 0.20,
        }
        for app in analyzed:
            score, parts = opportunity_score(app, weights)
            app["opportunity_score"] = round(score, 4)
            app["rank_signals"] = {k: round(v, 4) for k, v in parts.items()}

        # ---- 5. Apply user replies BEFORE merge ----
        accum = load_active(self.storage, self.agent_id)
        self._apply_responses(accum)

        # ---- 6. Merge into accumulator ----
        accum = merge_run(accum, new_apps=analyzed, run_ts=self.run_ts)

        # Cap open list — auto-defer the lowest-scored beyond max_open
        max_open = int(cfg.get("max_open_opportunities", 200))
        all_open = open_opportunities(accum)
        if len(all_open) > max_open:
            for o in all_open[max_open:]:
                transition_state(accum, o["opportunity_id"], "passed",
                                 reason=f"auto-deferred above cap of {max_open}")

        # ---- 6b. Generate build blueprints for top opportunities ----
        # For the highest-ROI opportunities that don't yet have a blueprint,
        # call the LLM to produce a detailed plan-mode set of documents
        # the implementer can build from. Files are written to storage at
        #   agents/<id>/blueprints/<opp-id>/...
        # and the accumulator entry is tagged with blueprint metadata.
        bp_cfg = cfg.get("blueprints") or {}
        bp_max_per_run = int(bp_cfg.get("max_per_run", 3))
        bp_regen_days = int(bp_cfg.get("regenerate_after_days", 90))
        if bp_max_per_run > 0:
            self.status("blueprinting", progress=0.78)
            self._cached_accum = accum   # used by _blueprint_prompt for peer context
            self._generate_blueprints(accum, cfg, bp_max_per_run, bp_regen_days)

        save_active(self.storage, self.agent_id, accum)
        counts = accum_stats(accum)
        self.status("emailing", current_action=f"{counts['open']} open", progress=0.85)

        # ---- 7. Email ----
        request_id = new_request_id()
        email_sent = self._email_top(cfg, accum, analyzed, counts, request_id)

        # Count regional-gap finds in the open backlog so the goal
        # tracker can surface progress on the country-gap goal.
        gap_count = sum(
            1 for o in open_opportunities(accum)
            if int(o.get("gap_score") or 0) > 0
            and len(o.get("gap_absent_in") or []) >= 2
        )
        return RunResult(
            status="success",
            metrics={
                "open_opportunities": counts["open"],
                "low_complexity_wins": sum(
                    1 for o in open_opportunities(accum)
                    if int(o.get("build_complexity") or 5) <= 2
                    and o.get("revenue_potential") in ("high", "very_high")
                ),
                "country_gap_finds": gap_count,
                "discovered_this_run": len(eligible),
                "obsoleted_this_run": counts["obsolete"],
                "email_sent": 1 if email_sent else 0,
            },
            summary=(
                f"{counts['open']} open opps · "
                f"{len(eligible)} new this run · "
                f"email_sent={email_sent}"
            ),
        )

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _save_artifact(self, name: str, content) -> None:
        """Write a per-run artifact to framework storage."""
        key = f"agents/{self.agent_id}/runs/{self.run_ts}/{name}"
        try:
            if isinstance(content, (dict, list)):
                self.storage.write_json(key, content)
            else:
                self.storage.write_text(key, str(content))
        except Exception as e:
            self.decide("observation", f"_save_artifact({name}) failed: {e}")

    # ------------------------------------------------------------------
    def _plan_search(self, accum: dict, cfg: dict) -> dict:
        """Ask the LLM to choose where to search this run.

        Reads accumulator state + the agent's goal progress and returns
        a structured plan: countries, genres, queries, and a gap-check
        pair. Falls back to a sensible default if the LLM is
        unreachable so the agent still runs.
        """
        from framework.core.app_store_clients import ITUNES_GENRES

        # Compact accumulator snapshot
        items = accum.get("opportunities") or []
        states: dict[str, int] = {}
        country_counts: dict[str, int] = {}
        genre_counts: dict[str, int] = {}
        for it in items:
            states[it.get("state", "open")] = states.get(it.get("state", "open"), 0) + 1
            country_counts[it.get("country", "?")] = country_counts.get(it.get("country", "?"), 0) + 1
            genre_counts[it.get("category", "?")] = genre_counts.get(it.get("category", "?"), 0) + 1
        accum_summary = (
            f"total={len(items)} states={states} "
            f"by_country={dict(sorted(country_counts.items(), key=lambda x: -x[1])[:8])} "
            f"by_genre_label={dict(sorted(genre_counts.items(), key=lambda x: -x[1])[:8])}"
        )

        # Goal progress (best effort — the framework primitive may not
        # be wired everywhere yet)
        goal_summary = "weekly_qualifying_finds, monthly_low_complexity_wins, country_gap_finds"
        try:
            from framework.core.goals import load_agent_goal_progress  # type: ignore
            gp = load_agent_goal_progress(self.storage, self.agent_id) or {}
            goal_summary = json.dumps(
                {g: {"current": v.get("current"), "target": v.get("target")}
                 for g, v in (gp.get("goals") or {}).items()}, indent=2
            )[:400]
        except Exception:
            pass

        # Recent top picks — last 3 runs' top-3 by opportunity_score
        recent_top: list[str] = []
        for it in sorted(items, key=lambda x: -float(x.get("opportunity_score") or 0))[:15]:
            recent_top.append(f"  - {it.get('name','?')} ({it.get('country','?')}) "
                              f"★{it.get('average_rating','?')} bc={it.get('build_complexity','?')} "
                              f"rev={it.get('revenue_potential','?')}")
        recent_top_picks = "\n".join(recent_top) if recent_top else "(no prior picks)"

        prompt = _PLAN_USER_TPL.format(
            accum_summary=accum_summary,
            goal_summary=goal_summary,
            genres="\n".join(f"  {gid}: {gname}" for gid, gname in ITUNES_GENRES.items()),
            recent_top_picks=recent_top_picks,
        )

        if chat_with_fallback is None:
            self.decide("observation", "ai_providers unavailable — using fallback plan")
            return self._fallback_plan()

        try:
            # chat_with_fallback returns (text, client_used) tuple
            text, _client = chat_with_fallback(
                agent_id=self.agent_id,
                messages=[
                    {"role": "system", "content": _PLAN_SYS},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=1500,
            )
        except Exception as e:
            self.decide("observation", f"plan LLM call failed: {e} — using fallback plan")
            return self._fallback_plan()

        try:
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(),
                          flags=re.MULTILINE)
            start = text.find("{"); end = text.rfind("}")
            plan = json.loads(text[start:end + 1])
        except Exception as e:
            self.decide("observation", f"plan parse failed: {e} — using fallback plan")
            return self._fallback_plan()

        # Sanity: clamp sizes so a runaway plan can't blow LLM budget.
        plan["countries"]      = (plan.get("countries") or [])[:3]
        plan["genre_ids"]      = (plan.get("genre_ids") or [])[:6]
        plan["search_queries"] = (plan.get("search_queries") or [])[:7]
        gc = plan.get("gap_check") or {}
        gc["popular_in"]       = (gc.get("popular_in") or [])[:3]
        gc["missing_in"]       = (gc.get("missing_in") or [])[:4]
        plan["gap_check"]      = gc
        return plan

    def _fallback_plan(self) -> dict:
        # Conservative default if the LLM is offline. Keeps the agent
        # still useful but doesn't try to be clever.
        return {
            "countries": ["us", "gb", "de"],
            "genre_ids": [6007, 6013, 6015, 6024, 6012, 6017],  # Productivity/Health/Finance/Shopping/Lifestyle/Education
            "search_queries": [],
            "gap_check": {
                "popular_in": ["us", "gb"],
                "missing_in": ["br", "mx", "in"],
            },
            "rationale": "fallback plan — LLM planner unreachable",
        }

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _generate_blueprints(self, accum: dict, cfg: dict,
                             max_per_run: int, regen_days: int) -> None:
        """Generate detailed build blueprints for top-ranked opportunities.

        Selection: open items sorted by `revenue_potential / build_complexity`
        (the email's same ROI sort). Skip items that already have a recent
        blueprint (within `regen_days`).

        Each blueprint is a SET of files written to:
          agents/<agent_id>/blueprints/<opp-id>/
            - README.md             entry-point + manifest
            - product-spec.md       vision, target user, MVP feature list
            - ux-research.md        competitor analysis, UX patterns to adopt
            - ux-spec.md            visual language, key screens, components
            - tech-stack.md         React Native + backend + docker
            - api-spec.md           REST endpoints
            - data-model.md         DB schema
            - implementation-plan.md  file tree + ordered build steps
            - plan-mode.json        machine-readable bundle for implementer

        The accumulator entry is updated with:
          • blueprint:               { path, generated_at, generated_run_ts,
                                       file_count }
          • build_status:            "blueprint-ready" (used by the email
                                       renderer + responder)
        """
        if chat_with_fallback is None:
            self.decide("observation", "ai_providers unavailable — skipping blueprints")
            return

        rev_score = {"very_high": 4, "high": 3, "medium": 2, "low": 1, "": 0}
        def _roi(it):
            bc = max(1, int(it.get("build_complexity") or 5))
            return rev_score.get(it.get("revenue_potential", ""), 0) / bc

        # Skip items whose blueprint is fresh enough.
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        regen_cutoff = now - timedelta(days=regen_days)

        candidates = [it for it in accum.get("opportunities") or []
                      if it.get("state", "open") == "open"]
        candidates.sort(key=lambda it: -_roi(it))

        targeted = []
        for it in candidates:
            bp = it.get("blueprint") or {}
            generated_at = bp.get("generated_at")
            if generated_at:
                try:
                    g = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
                    if g > regen_cutoff:
                        continue   # blueprint still fresh
                except Exception:
                    pass
            targeted.append(it)
            if len(targeted) >= max_per_run:
                break

        if not targeted:
            self.decide("observation", "no opportunities qualify for blueprinting this run")
            return

        # Stack used for the build (overridable in config).
        bp_stack = cfg.get("blueprints", {}).get("tech_stack") or {
            "frontend": "React Native (TypeScript) with Expo for delivery",
            "backend":  "Node.js + Express on Postgres, deployed via Docker (mirrors aisleprompt's stack)",
            "infra":    "Docker compose for local dev, Azure Container Apps for production",
            "auth":     "OAuth + JWT sessions",
            "ci":       "GitHub Actions: lint, type-check, test, build, push image",
        }

        for idx, it in enumerate(targeted):
            self.status("blueprinting",
                        current_action=f"{idx+1}/{len(targeted)}: {it.get('name','?')[:40]}",
                        progress=0.78 + 0.05 * (idx / max(1, len(targeted))))
            self._generate_one_blueprint(it, bp_stack)

    def _generate_one_blueprint(self, item: dict, stack: dict) -> None:
        """Single LLM call → produces all blueprint files for one opportunity."""
        opp_id = item.get("opportunity_id")
        if not opp_id:
            return

        bp_dir = f"agents/{self.agent_id}/blueprints/{opp_id}"

        prompt = self._blueprint_prompt(item, stack)
        try:
            text, _client = chat_with_fallback(
                agent_id=self.agent_id,
                messages=[
                    {"role": "system", "content": _BLUEPRINT_SYS},
                    {"role": "user",   "content": prompt},
                ],
                max_tokens=8000,
            )
        except Exception as e:
            self.decide("error", f"blueprint LLM call failed for {opp_id}: {e}")
            return

        # Parse the LLM's JSON-encoded file map.
        try:
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(),
                          flags=re.MULTILINE)
            start = text.find("{"); end = text.rfind("}")
            payload = json.loads(text[start:end + 1])
        except Exception as e:
            self.decide("error", f"blueprint parse failed for {opp_id}: {e}")
            try:
                self.storage.write_text(f"{bp_dir}/blueprint-raw.txt", text or "")
            except Exception:
                pass
            return

        files = payload.get("files") or {}
        if not isinstance(files, dict) or not files:
            self.decide("error", f"blueprint payload had no files for {opp_id}")
            return

        # Write each file the LLM produced.
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        written: list[str] = []
        for fname, content in files.items():
            # Sanitize the filename — only allow basename + nested subdirs.
            safe = re.sub(r"[^A-Za-z0-9_./-]", "", str(fname)).lstrip("/")
            if not safe or ".." in safe:
                continue
            try:
                if safe.endswith(".json") and isinstance(content, (dict, list)):
                    self.storage.write_json(f"{bp_dir}/{safe}", content)
                else:
                    self.storage.write_text(f"{bp_dir}/{safe}",
                                            content if isinstance(content, str)
                                            else json.dumps(content, indent=2))
                written.append(safe)
            except Exception as e:
                self.decide("error", f"blueprint write {bp_dir}/{safe} failed: {e}")

        # Machine-readable manifest the implementer reads first.
        manifest = {
            "schema_version":   "1",
            "opportunity_id":   opp_id,
            "app_name":         item.get("name", ""),
            "store_url":        item.get("store_url", ""),
            "category":         item.get("category", ""),
            "country":          item.get("country", ""),
            "build_complexity": item.get("build_complexity"),
            "revenue_potential": item.get("revenue_potential"),
            "v2_pitch":         item.get("v2_pitch", ""),
            "feature_gaps":     item.get("feature_gaps", []),
            "moats_to_take":    item.get("moats_to_take", []),
            "tech_stack":       stack,
            "files":            written,
            "generated_at":     now_iso,
            "generated_run_ts": self.run_ts,
            "rec_type":         "app-build-from-blueprint",  # implementer cue
        }
        self.storage.write_json(f"{bp_dir}/manifest.json", manifest)

        # Update the accumulator entry so the email knows this is shippable.
        item["blueprint"] = {
            "path":             bp_dir,
            "generated_at":     now_iso,
            "generated_run_ts": self.run_ts,
            "file_count":       len(written),
            "manifest_key":     f"{bp_dir}/manifest.json",
        }
        item["build_status"] = "blueprint-ready"

        self.decide("action",
                    f"blueprint written for {item.get('name','?')}: "
                    f"{len(written)} file(s) at {bp_dir}")

    def _blueprint_prompt(self, item: dict, stack: dict) -> str:
        # Pull adjacent accumulator items in the same category as competitor
        # context. The LLM uses these for "UX research" — what works in the
        # space, what users complain about.
        peers = []
        try:
            same_cat = [
                p for p in (self._cached_accum or {}).get("opportunities", [])
                if p.get("category") == item.get("category")
                and p.get("opportunity_id") != item.get("opportunity_id")
            ][:8]
            for p in same_cat:
                peers.append({
                    "name": p.get("name"),
                    "rating": p.get("average_rating"),
                    "reviews": p.get("review_count"),
                    "feature_gaps": p.get("feature_gaps") or [],
                    "decline_thesis": p.get("decline_thesis", ""),
                })
        except Exception:
            pass

        # Inject the reference architecture doc so the LLM grounds its
        # spec in our actual stack. Read once, cache on instance.
        if not hasattr(self, "_ref_arch_cache"):
            ref_path = Path(__file__).resolve().parent.parent.parent / \
                "docs" / "reference-app-architecture.md"
            try:
                self._ref_arch_cache = ref_path.read_text()
            except Exception:
                self._ref_arch_cache = ""

        return _BLUEPRINT_USER_TPL.format(
            app=json.dumps({
                "name": item.get("name"),
                "publisher": item.get("publisher"),
                "category": item.get("category"),
                "country":  item.get("country"),
                "rating":   item.get("average_rating"),
                "reviews":  item.get("review_count"),
                "version":  item.get("current_version"),
                "days_since_update": item.get("days_since_update"),
                "description": (item.get("description") or "")[:1500],
                "feature_gaps": item.get("feature_gaps") or [],
                "decline_thesis": item.get("decline_thesis", ""),
                "v2_pitch": item.get("v2_pitch", ""),
                "moats_to_take": item.get("moats_to_take") or [],
            }, indent=2),
            stack=json.dumps(stack, indent=2),
            peers=json.dumps(peers, indent=2),
            reference_arch=self._ref_arch_cache[:12000],  # cap so prompt fits
        )

    # ------------------------------------------------------------------
    def _llm_analyze(self, apps: list[dict], cfg: dict) -> list[dict]:
        if not apps:
            return []
        if chat_with_fallback is None:
            self.decide("observation", "ai_providers unavailable — skipping LLM, defaults applied")
            for a in apps:
                a.setdefault("build_complexity", 3)
                a.setdefault("revenue_potential", "medium")
            return apps

        BATCH = 8
        results_by_id: dict[str, dict] = {}
        for i in range(0, len(apps), BATCH):
            batch = apps[i:i + BATCH]
            prompt = _LLM_PROMPT.format(
                apps=json.dumps([_slim_for_llm(a) for a in batch], indent=2)
            )
            self.status("analyzing",
                        current_action=f"llm batch {i // BATCH + 1}",
                        progress=0.3 + 0.4 * (i / max(1, len(apps))))
            try:
                text, _client = chat_with_fallback(
                    agent_id=self.agent_id,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4000,
                )
            except Exception as e:
                self.decide("observation", f"llm batch {i // BATCH + 1} failed: {e}")
                continue
            parsed = self._parse_llm_array(text)
            for item in parsed:
                sid = str(item.get("store_id") or "")
                if sid:
                    results_by_id[sid] = item

        out = []
        for a in apps:
            sid = str(a.get("store_id") or "")
            llm = results_by_id.get(sid) or {}
            try:
                a["build_complexity"] = max(1, min(5, int(llm.get("build_complexity") or 3)))
            except Exception:
                a["build_complexity"] = 3
            rp = (llm.get("revenue_potential") or "medium").lower()
            a["revenue_potential"] = rp if rp in _REV_LABELS else "medium"
            for f in ("complexity_drivers", "revenue_thesis", "decline_thesis",
                      "feature_gaps", "v2_pitch", "moats_to_take"):
                if f in llm:
                    a[f] = llm[f]
            out.append(a)
        return out

    def _parse_llm_array(self, text: str) -> list[dict]:
        if not text:
            return []
        # Strip markdown fences if any.
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
        # Find first '[' / last ']'.
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        try:
            arr = json.loads(text[start:end + 1])
            return arr if isinstance(arr, list) else []
        except Exception:
            return []

    # ------------------------------------------------------------------
    def _apply_responses(self, accum: dict) -> None:
        """Read the most recent prior run's opp-id-map.json + responses;
        translate `pursue opp-NNN` / `pass opp-NNN` into accumulator
        state changes."""
        responses = getattr(self, "responses", None) or []
        if not responses:
            return
        # Find prior opp-id-map.json (rec_id → opportunity_id from last run).
        prior_map: dict[str, str] = {}
        try:
            run_keys = sorted(
                self.storage.list_prefix(f"agents/{self.agent_id}/runs/"),
                reverse=True,
            )
            for k in run_keys:
                if k.endswith("/opp-id-map.json"):
                    prior_map = self.storage.read_json(k) or {}
                    break
        except Exception:
            pass
        if not prior_map:
            return
        action_map = {
            "pursue": "pursued", "build": "pursued", "ship": "pursued",
            "pass": "passed", "skip": "passed", "no": "passed",
        }
        for r in responses:
            text = (r.get("body") or r.get("text") or "").lower()
            for m in re.finditer(r"(pursue|build|ship|pass|skip|no)\s+(opp-\d+)", text):
                action, rid = m.group(1), m.group(2)
                oid = prior_map.get(rid)
                if oid:
                    transition_state(accum, oid, action_map[action],
                                     reason=f"operator-reply: {action}")

    # ------------------------------------------------------------------
    def _email_top(self, cfg: dict, accum: dict, this_run: list[dict],
                   counts: dict, request_id: str) -> bool:
        rep = cfg.get("reporter", {}).get("email", {}) or {}
        cap = int(rep.get("backlog_cap", 40))
        to_list = rep.get("to") or ["mperry@northernsoftwareconsulting.com"]
        from_addr = rep.get("from") or "automation@northernsoftwareconsulting.com"
        msmtp_account = rep.get("msmtp_account") or "automation"
        subject_tmpl = rep.get("subject_template") or "App Store Opportunities — {date}"

        all_open = open_opportunities(accum)
        top = all_open[:cap]
        truncated = max(0, len(all_open) - cap)

        # Build rec-id → opp-id map for the responder.
        opp_map = {f"opp-{i+1:03d}": o["opportunity_id"] for i, o in enumerate(top)}
        self.storage.write_json(
            f"agents/{self.agent_id}/runs/{self.run_ts}/opp-id-map.json", opp_map)

        subject = subject_tmpl.format(date=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        html = self._render_html(top, counts, this_run_count=len(this_run),
                                 truncated=truncated, request_id=request_id)
        try:
            self.storage.write_text(
                f"agents/{self.agent_id}/runs/{self.run_ts}/email-rendered.html", html)
        except Exception:
            pass

        sent = False
        detail = ""
        try:
            from shared.site_quality import send_via_msmtp  # type: ignore
            ok, detail = send_via_msmtp(
                subject=subject, body_html=html, to=to_list,
                sender=from_addr, msmtp_account=msmtp_account,
                extra_headers={
                    "X-Reusable-Agent": self.agent_id,
                    "Reply-To": from_addr,
                },
                bypass_digest=True,
            )
            sent = bool(ok)
        except Exception as e:
            detail = f"send_via_msmtp exception: {e}"
            self.decide("observation", detail)
            sent = False
        # Disambiguate which transport actually fired (see comp-research
        # for the rationale).
        actual_transport = "graph" if "graph" in (detail or "").lower() \
            else "msmtp" if "sent to" in (detail or "") \
            else "digest" if "digest" in (detail or "").lower() \
            else "unknown"
        try:
            from datetime import datetime as _dt, timezone as _tz
            self.storage.write_json(
                f"agents/{self.agent_id}/outbound-emails/{request_id}.json",
                {
                    "schema_version": "1",
                    "request_id": request_id,
                    "agent_id": self.agent_id,
                    "subject": subject,
                    "to": list(to_list),
                    "expects_response": True,
                    "sent_at": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                    "transport": actual_transport,
                    "transport_detail": detail or "",
                    "msmtp_account": msmtp_account,
                    "ok": sent,
                },
            )
        except Exception as e:
            self.decide("observation", f"outbound-record write failed: {e}")
        return sent

    def _render_html(self, top: list[dict], counts: dict, *,
                     this_run_count: int, truncated: int,
                     request_id: str) -> str:
        rev_badge = {"very_high": "🔥", "high": "💰", "medium": "•", "low": "·"}
        rows = []
        bp_ready = 0
        for i, o in enumerate(top, start=1):
            rid = f"opp-{i:03d}"
            rev = o.get("revenue_potential", "medium")
            bc = int(o.get("build_complexity") or 3)
            bp = o.get("blueprint") or {}
            bp_status = ""
            if bp.get("path"):
                bp_ready += 1
                bp_status = (f"<br><span style='display:inline-block;padding:2px 6px;"
                             f"border-radius:3px;background:#e8f5e9;color:#1b5e20;"
                             f"font-size:11px;font-weight:600;'>📋 BLUEPRINT READY · "
                             f"{bp.get('file_count', '?')} files</span>")
            rows.append(f"""
            <tr style="border-top:1px solid #eee">
              <td style="padding:8px;vertical-align:top"><b>{rid}</b>{bp_status}</td>
              <td style="padding:8px;vertical-align:top">
                <a href="{(o.get('store_url') or '#')}">{(o.get('name') or '?')}</a>
                <br><span style="color:#888">{o.get('store','?')} · {o.get('publisher','?')}</span>
              </td>
              <td style="padding:8px;vertical-align:top">
                ★ {o.get('average_rating','?')} ({o.get('review_count','?')} rev)<br>
                <span style="color:#888">v{o.get('current_version','?')} ·
                {o.get('days_since_update','?')}d stale</span>
              </td>
              <td style="padding:8px;vertical-align:top">{rev_badge.get(rev,'·')} {rev}<br>
                <span style="color:#888">complexity {bc}/5</span>
              </td>
              <td style="padding:8px;vertical-align:top">
                <i>{(o.get('v2_pitch') or '')}</i><br>
                <span style="color:#666;font-size:90%">{(o.get('decline_thesis') or '')}</span>
              </td>
            </tr>""")
        truncated_note = (
            f"<p style='color:#888'>{truncated} more open opportunity(ies) hidden — "
            f"see the dashboard for the full backlog.</p>" if truncated else "")
        bp_help = (f"<p><b>📋 {bp_ready} item(s) have build blueprints.</b> "
                   f"Reply <code>implement opp-NNN</code> on a blueprint-ready item "
                   f"to queue the v2 build (the implementer reads the blueprint and "
                   f"scaffolds a full React Native + Node + Postgres app per the "
                   f"reference architecture).</p>" if bp_ready else "")
        return f"""<html><body style="font-family:system-ui,sans-serif">
<h2>App Store Opportunity Scan</h2>
<p><b>{counts['open']}</b> open · {this_run_count} new this run ·
{counts['pursued']} pursued · {counts['passed']} passed ·
{counts['obsolete']} obsoleted.</p>
{bp_help}
<p>Reply <code>pursue opp-NNN</code> to greenlight building, or
<code>pass opp-NNN</code> to drop from future emails.</p>
<table style="border-collapse:collapse;width:100%;font-size:14px">
<thead><tr style="background:#f5f5f5">
<th style="text-align:left;padding:8px">ID</th>
<th style="text-align:left;padding:8px">App</th>
<th style="text-align:left;padding:8px">Signals</th>
<th style="text-align:left;padding:8px">Opportunity</th>
<th style="text-align:left;padding:8px">Thesis</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody></table>
{truncated_note}
<p style="color:#aaa;font-size:11px">request_id={request_id}</p>
</body></html>"""


if __name__ == "__main__":
    AppStoreOpportunityAgent().run_once()
