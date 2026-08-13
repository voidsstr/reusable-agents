"""implementer_safety — per-rec routing hint for the dispatcher.

Tells the dispatcher which implementer backend a rec is safe for.
The dispatcher consumes this in `_classify_rec_backend()`; producers
can either set it explicitly on each rec OR let the classifier infer
from the rec's `type` / `check_id` / `dispatch_kind`.

Three canonical values:

  STRUCTURED   — Safe for gpt-4.1 via aider/jcode-copilot. Well-
                 bounded, structured edits with no design judgment:
                   - DB migrations + row updates
                   - Taxonomy / category fixes
                   - Schema.org JSON-LD additions
                   - Image URL validation / deactivation
                   - SQL allowlist / blocklist updates
                 What it CAN'T be trusted with: anything that wholesale
                 rewrites human-facing copy, deletes UI sections, or
                 changes visual hierarchy.

  DESIGN_JUDGMENT — Requires claude. Touches user-facing copy, layout
                 decisions, or visual hierarchy. Examples:
                   - Hero/landing-page copy
                   - "How it works" sections
                   - Meta description rewrites with brand voice
                   - Anything where "reduce duplicate content" might
                     mean "selectively trim" vs "gut a section"
                 gpt-4.1 has reliably damaged these (2026-05-13).

  LONG_FORM    — Requires claude (opus preferred). Content generation
                 over multi-K-word output:
                   - article-author body writing
                   - head-to-head editorial comparisons
                 Needs voice cohesion and long-context handling that
                 only opus-4-7 reliably provides.

Schema:
  recs.json[]["implementer_safety"]: "structured" | "design_judgment" | "long_form"

When the field is MISSING the classifier infers from `check_id`,
`type`, and `dispatch_kind`. Producers that want stricter routing
than the inferrer can stamp the field explicitly.

Drain policy (when claude is down):
  - structured → dispatch via copilot-gpt-4.1 channel
  - design_judgment / long_form → defer to next tick (wait for claude)
"""
from __future__ import annotations

# Canonical safety tags. Treat as enum.
STRUCTURED = "structured"
DESIGN_JUDGMENT = "design_judgment"
LONG_FORM = "long_form"

ALL = (STRUCTURED, DESIGN_JUDGMENT, LONG_FORM)

# Backend each tag routes to.
BACKEND_FOR_TAG = {
    STRUCTURED: "copilot-gpt-4.1",
    DESIGN_JUDGMENT: "claude",
    LONG_FORM: "claude",
}


# check_id substrings that imply STRUCTURED. Conservative — only patterns
# we've verified gpt-4.1 ships cleanly. Add patterns ONLY after a clean
# end-to-end test on real recs.
_STRUCTURED_CHECK_ID_PATTERNS = (
    "recipe-category",
    "recipe-image",
    "product-image",
    "product-brand",
    "image-name-mismatch",
    "image-mismatch",
    "schema-product-missing",
    "schema-faqpage-missing",
    "schema-breadcrumblist-missing",
    "schema-recipe-incomplete",
    "indexnow",
    "sitemap-",
    "canonical-url",
    "robots-meta",
)

# rec.type substrings that imply STRUCTURED.
_STRUCTURED_TYPE_PATTERNS = (
    "catalog-audit",
    "recipe-category",
    "product-image",
    "image-name-mismatch",
)

# dispatch_kind values that imply STRUCTURED at the kind level.
_STRUCTURED_DISPATCH_KINDS = frozenset({"catalog-audit"})

# dispatch_kind values that ALWAYS require LONG_FORM (claude).
_LONG_FORM_DISPATCH_KINDS = frozenset({"article-author", "h2h"})


def safety_tag(rec: dict, dispatch_kind: str = "") -> str:
    """Return the safety tag for this rec. Reads explicit
    `rec.implementer_safety` if present + valid; otherwise infers.

    Default for unmatched recs is DESIGN_JUDGMENT (the safer default —
    routes to claude, gets human-quality output, never damages UI).
    """
    explicit = (rec or {}).get("implementer_safety")
    if explicit in ALL:
        return explicit

    if dispatch_kind in _LONG_FORM_DISPATCH_KINDS:
        return LONG_FORM
    if dispatch_kind in _STRUCTURED_DISPATCH_KINDS:
        return STRUCTURED

    check_id = ((rec or {}).get("check_id") or "").lower()
    rec_type = ((rec or {}).get("type") or
                (rec or {}).get("rec_type") or "").lower()
    if any(p in check_id for p in _STRUCTURED_CHECK_ID_PATTERNS):
        return STRUCTURED
    if any(p in rec_type for p in _STRUCTURED_TYPE_PATTERNS):
        return STRUCTURED
    if "article-author-proposal" in rec_type or "head-to-head" in rec_type:
        return LONG_FORM

    # Default: design_judgment. Routes to claude. Keeps gpt-4.1 conservative.
    return DESIGN_JUDGMENT


def backend_for(rec: dict, dispatch_kind: str = "") -> str:
    """Return implementer backend ('claude' | 'copilot-gpt-4.1')
    based on this rec's safety tag."""
    return BACKEND_FOR_TAG[safety_tag(rec, dispatch_kind)]


# ──────────────────────────────────────────────────────────────────
# Model-tier recommendation (Opus / Sonnet / Haiku)
#
# The dispatch backend decides claude vs gpt-4.1; this layer decides
# WHICH Claude model when claude is the chosen backend. Three tiers:
#
#   opus    — high stakes: landing-page edits, monetization components,
#             critical-severity recs, complex multi-file refactors. Spends
#             ~5× more per token but the marginal cost of a worse answer
#             is high (regression, cascade drift).
#   sonnet  — default. Most SEO copy, schema, internal-link, ARIA work.
#   haiku   — mechanical / structured work that can be checked by
#             pattern: image-name-mismatch, indexnow re-submit, simple
#             schema-faq adds. Saves cost; falls forward to sonnet
#             automatically if the cheap tier produces low-quality output.
#
# Resolves at *batch* level: when N recs ship together, pick the highest
# tier any rec in the batch requires (most expensive wins).
# ──────────────────────────────────────────────────────────────────

MODEL_TIER_OPUS = "opus"
MODEL_TIER_SONNET = "sonnet"
MODEL_TIER_HAIKU = "haiku"

# Concrete Claude model IDs per tier. Keep these in sync with the
# implementer's claude-pool fallback chain in run.sh.
MODEL_FOR_TIER = {
    MODEL_TIER_OPUS:   "claude-opus-5",
    MODEL_TIER_SONNET: "claude-sonnet-4-6",
    MODEL_TIER_HAIKU:  "claude-haiku-4-5",
}

# File-path substrings that elevate a rec to OPUS regardless of other
# signals. These are user-facing high-leverage surfaces where a wrong
# edit cascades (the 2026-05-13 MarketingPage drift, today's Instacart
# CTA regression).
_HIGH_STAKES_PATH_PATTERNS = (
    "MarketingPage",
    "FeaturesPage",
    "HomePage",
    "HeroSection",
    "InstacartDescribeToCart",
    "PaywallModal",
    "/landing/",
    "STATIC_SSR_PAGES",  # the simple-server.ts SSR-page registry
    "robots.txt",
    "sitemap",            # sitemap config (not generated XML)
    "site.yaml",          # config touch
    "schema.sql",
    "_initial_schema",    # DB migration
)

# rec.type substrings that elevate to OPUS regardless of other signals.
_OPUS_TYPE_PATTERNS = (
    "monetization",
    "parity-feature",       # comp-research strategic — multi-file builds
    "competitive-advantage",
    "conversion-path",      # funnel/instrumentation work
    "complexity-l",         # explicitly marked complex by the producer
    "new-page-",            # new article/page creation
)


def recommended_model_tier(
    rec: dict,
    *,
    dispatch_kind: str = "",
) -> str:
    """Return the Claude model tier (opus/sonnet/haiku) for this rec.

    Decision order:
      1. STRUCTURED tag (from `safety_tag`) → haiku (cheapest works)
      2. High-stakes path / type → opus
      3. severity=critical, confidence>=0.9 → opus
      4. Otherwise → sonnet
    """
    if not isinstance(rec, dict):
        return MODEL_TIER_SONNET

    tag = safety_tag(rec, dispatch_kind)
    if tag == STRUCTURED:
        return MODEL_TIER_HAIKU

    rec_type = (rec.get("type") or rec.get("rec_type") or "").lower()
    if any(p in rec_type for p in _OPUS_TYPE_PATTERNS):
        return MODEL_TIER_OPUS

    target_files = rec.get("target_files") or rec.get("affected_files") or []
    if isinstance(target_files, str):
        target_files = [target_files]
    for f in target_files:
        f_lower = str(f or "").lower()
        for pat in _HIGH_STAKES_PATH_PATTERNS:
            if pat.lower() in f_lower:
                return MODEL_TIER_OPUS

    if (rec.get("severity") or "").lower() == "critical":
        return MODEL_TIER_OPUS

    try:
        if float(rec.get("confidence", 0)) >= 0.9 and (rec.get("expected_impact") or rec.get("goal_ids")):
            return MODEL_TIER_OPUS
    except (TypeError, ValueError):
        pass

    return MODEL_TIER_SONNET


def recommended_model_for_batch(
    recs: list[dict],
    *,
    dispatch_kind: str = "",
) -> tuple[str, str]:
    """For a multi-rec batch dispatched together, return (tier, model_id)
    picked by 'most expensive wins'.

    Rationale: the implementer runs a single claude-cli session over all
    N recs. If ONE rec needs Opus, the whole batch needs Opus — we can't
    downgrade mid-session.
    """
    if not recs:
        return MODEL_TIER_SONNET, MODEL_FOR_TIER[MODEL_TIER_SONNET]
    # Order matters: opus > sonnet > haiku
    rank = {MODEL_TIER_OPUS: 2, MODEL_TIER_SONNET: 1, MODEL_TIER_HAIKU: 0}
    best_tier = MODEL_TIER_HAIKU
    for r in recs:
        t = recommended_model_tier(r, dispatch_kind=dispatch_kind)
        if rank[t] > rank[best_tier]:
            best_tier = t
    return best_tier, MODEL_FOR_TIER[best_tier]


def stamp_safety(recs: list[dict], dispatch_kind: str = "") -> int:
    """Producer-side helper: stamp `implementer_safety` on every rec
    that doesn't already have a valid tag. Returns # stamped.

    Use in a producer's run() right after generating proposals, before
    persisting recommendations.json. Lets downstream tooling (dashboard
    bucket, dispatcher, reporter) read the tag without re-inferring.

        from framework.core.implementer_safety import stamp_safety
        stamp_safety(recs_doc["recommendations"], dispatch_kind="pi")
    """
    n = 0
    for r in recs or []:
        if not isinstance(r, dict):
            continue
        if r.get("implementer_safety") in ALL:
            continue
        r["implementer_safety"] = safety_tag(r, dispatch_kind)
        n += 1
    return n
