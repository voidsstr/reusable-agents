"""Work-type registry — maps rec types to the agent best suited to
implement them.

The seo-analyzer and other rec-producing agents call `handler_for()` at
rec-emit time to tag a rec with `handoff_target`. The implementer reads
that tag and either ships the rec itself (when target is "implementer"
or unset) or sends a handoff to the named agent.

Why this is a registry rather than `if rec.type == ...` switches: it
puts the routing decision in ONE place, makes it discoverable from the
dashboard, and lets new agents declare what they handle without
patching the analyzer.

Schema:
    rec_type → (work_type, default_agent_id)

`work_type` is the canonical noun (`internal_link_addition`,
`new_article_creation`, `body_md_edit`, `code_edit`, ...). It's separate
from the rec_type so multiple rec_types can map to the same work_type,
and so future agents can opt in via work_type tags rather than
enumerating every rec_type they accept.

`default_agent_id` is the fallback handler. Agents whose role overlaps
(e.g. article-author + progressive-improvement both edit body_md) can
override via per-site config.

Sites can override the default routing in `site.yaml`:

    handoff_routes:
      article-orphan-boost: aisleprompt-progressive-improvement-agent
      content-expansion: specpicks-article-proposal-agent

Lookup precedence: site override > default mapping > "implementer".
"""
from __future__ import annotations


# Default mapping from rec_type → (work_type, handler_agent).
#
# These are the agent IDs of the *blueprint* implementations. Per-site
# instances are resolved at call time by the analyzer's site config:
#
#   if rec_type maps to "article-proposal-agent" and site is "specpicks",
#   the analyzer looks up site.yaml's handoff_routes for an override
#   like "specpicks-article-proposal-agent" and uses that. If no override
#   is set, the rec falls through to the implementer (which has graceful
#   handling for orphan handoffs).
DEFAULT_REC_ROUTING: dict[str, tuple[str, str]] = {
    # === NEW-page creation — only thing article-author actually handles ===
    # The agent generates fresh articles from a topic + bucket. Both
    # underscore and hyphen variants since the analyzer has used both.
    "new-page-buying_guide":             ("new_article_creation", "article-proposal-agent"),
    "new-page-buying-guide":             ("new_article_creation", "article-proposal-agent"),
    "new-page-use_case":                 ("new_article_creation", "article-proposal-agent"),
    "new-page-use-case":                 ("new_article_creation", "article-proposal-agent"),
    "new-page-troubleshooting":          ("new_article_creation", "article-proposal-agent"),
    "new-page-brand":                    ("new_article_creation", "article-proposal-agent"),
    "new-page-comparison":               ("comparison_page_generation", "head-to-head-agent"),

    # === Body / template / linking edits to EXISTING pages ===
    # Implementer (LLM-driven, can read+patch markdown files, DB rows,
    # template props) handles these directly. Empty handler = "stays
    # with implementer". Previously these routed to article-author,
    # which only knows how to create NEW articles — handoffs sat
    # unactioned for days. Owner: implementer.
    "article-orphan-boost":              ("internal_link_addition", ""),
    "internal-link-add":                 ("internal_link_addition", ""),
    "internal-link-to-featured":         ("internal_link_addition", ""),
    "content-expansion":                 ("body_md_edit", ""),
    "snippet-rewrite":                   ("body_md_edit", ""),
    "title-fix":                         ("body_md_edit", ""),
    "article-template-incomplete":       ("body_md_edit", ""),
    "article-cited-sources-missing":     ("body_md_edit", ""),
    "llm-search-faq-format":             ("body_md_edit", ""),
    "faq-quality-thin":                  ("body_md_edit", ""),
    "freshness-stale-articles":          ("body_md_edit", ""),
    "trust-signal-density-thin":         ("body_md_edit", ""),
    "outbound-citations-thin":           ("body_md_edit", ""),
    "onpage-thin-content":               ("body_md_edit", ""),
    "onpage-low-internal-linking":       ("internal_link_addition", ""),
    "review-template-incomplete":        ("body_md_edit", ""),
    "body-internal-links-thin":          ("internal_link_addition", ""),
    "body-link-density-thin":            ("internal_link_addition", ""),
    "outbound-citations-missing":        ("body_md_edit", ""),
    "pros-cons-thin":                    ("body_md_edit", ""),

    # Catalog / product-data work belongs to product-hydration.
    "price-stale":                       ("price_refresh", "product-hydration-agent"),
    "product-content-incomplete":        ("product_content_hydration", "product-hydration-agent"),
    "featured-set-curation":             ("featured_set_curation", "product-hydration-agent"),

    # Catalog quality fixes (broken images, bad categorization) →
    # progressive-improvement / catalog-audit per-site.
    "catalog-broken-image":              ("quality_audit_fix", "progressive-improvement-agent"),
    "catalog-miscategorization":         ("quality_audit_fix", "progressive-improvement-agent"),
    "catalog-thin-description":          ("product_content_hydration", "product-hydration-agent"),

    # Index-submission triggers go to the indexnow-submitter (queue-driven).
    "indexnow-submit":                   ("index_submission", "indexnow-submitter"),

    # === GSC URL Inspection coverage recs (emitted by gsc-coverage-auditor
    # via seo-analyzer's _add_index_coverage_recs). Each rec_type maps to
    # the agent best suited to fix that specific coverageState. The
    # implementer (handler="") handles SSR/canonical/template fixes;
    # article-author handles content rewrites for thin pages; the
    # indexnow-submitter handles "URL is unknown to Google" by re-firing
    # bulk submission. ===
    "gsc-coverage-not-indexed":          ("body_md_edit", "article-proposal-agent"),
    "gsc-coverage-discovered":           ("internal_link_addition", ""),
    "gsc-coverage-redirect":             ("code_edit", ""),
    "gsc-coverage-unknown":              ("index_submission", "indexnow-submitter"),
    "gsc-coverage-issues":               ("schema_markup_fix", ""),
    "gsc-coverage-noindex":              ("code_edit", ""),
    "gsc-coverage-canonical-mismatch":   ("code_edit", ""),
    "gsc-coverage-soft-404":             ("code_edit", ""),

    # Everything else — code/SSR/template/schema/route work — stays with
    # the implementer. Recs for this category set handoff_target to
    # "" (or omit), which the implementer treats as "ship it yourself".
    # A non-exhaustive list of what stays at implementer:
    #   product-affiliate-tag-missing, product-jsonld-schema, robots-no-ai-allow,
    #   cwv-ttfb-slow, hreflang-*, footer-trust-links-*, breadcrumb-*,
    #   url-structure-canonical, indexing-*, schema-markup, onpage-*,
    #   collection-numberOfItems-*, sitemap-auto-regen.
}


def handler_for(
    rec_type: str,
    *,
    site_routes: dict | None = None,
) -> tuple[str, str]:
    """Return (work_type, handler_agent_id) for a rec type.

    Lookup order:
      1. site_routes[rec_type] — per-site override from site.yaml
         handoff_routes (returns "<work_type>", "<override_agent>")
      2. DEFAULT_REC_ROUTING[rec_type]
      3. ("code_edit", "") — empty handler means 'implementer ships it'

    Returns ("code_edit", "") for unknown rec types so they default to
    the implementer (the common case).
    """
    if site_routes and rec_type in site_routes:
        # Site override — work_type is inferred from the default if
        # known, else "site_specific".
        wt_default = DEFAULT_REC_ROUTING.get(rec_type, ("site_specific", ""))
        return (wt_default[0], site_routes[rec_type])
    return DEFAULT_REC_ROUTING.get(rec_type, ("code_edit", ""))


def is_implementer_work(rec_type: str, *, site_routes: dict | None = None) -> bool:
    """True iff the rec stays with the implementer (no handoff needed)."""
    _, handler = handler_for(rec_type, site_routes=site_routes)
    return not handler  # empty == implementer


def all_known_work_types() -> set[str]:
    return {wt for wt, _ in DEFAULT_REC_ROUTING.values()} | {"code_edit"}


def all_known_handlers() -> set[str]:
    """Set of generic handler-agent ids the framework knows about. Used
    by the dashboard to render the inter-agent graph + by site configs
    to validate handoff_route overrides."""
    return {h for _, h in DEFAULT_REC_ROUTING.values() if h}


# ---------------------------------------------------------------------------
# Live-state rec types — never suppressed by run-to-run dedupe
# ---------------------------------------------------------------------------
#
# Most recs describe a defect in stored content (a thin body, a missing
# schema block, a bad category). Once the implementer fixes one, a later
# run that re-detects it is noise, so producers suppress recs whose
# canonical key matches a prior run's shipped/implemented/skipped rec.
#
# Availability recs are different in kind. "This URL returned 503" is a
# re-measurement of production RIGHT NOW, not a description of a stored
# defect — so a past resolution carries no information about whether the
# condition holds today. Suppressing them trades a duplicate rec for a
# silent outage: the site 503s, the crawler notices, and the dedupe
# filter drops the rec because the same URL 503'd during an unrelated
# incident three weeks ago. That failure is an ABSENCE, which is the
# hardest kind to spot.
#
# The bind this resolves: an availability rec that a run correctly
# resolves as transient must still be markable `implemented` so the
# dispatch queue stops re-issuing it, WITHOUT that mark blinding the
# next crawl. Exempting the type from dedupe decouples the two.
#
# Producers call `is_live_state(...)` when building their handled-key
# set. Match is on the rec's `type` OR its `category` — producers differ
# in which field carries the taxonomy (the PI agent keys on category,
# the seo-analyzer on type).

DEFAULT_LIVE_STATE_REC_TYPES: frozenset[str] = frozenset({
    "broken-page",      # PI category + type: fetch error / 4xx / 5xx
    "broken-link",      # outbound or internal link that no longer resolves
    "fetch-error",      # transport-level failure (timeout, reset, DNS)
    "http-error",       # generic non-2xx status finding
    "uptime",           # explicit availability probes
    "cwv-ttfb-slow",    # latency is measured per-crawl, not stored
})

LIVE_STATE_CONFIG_KEY = "config/live-state-rec-types.json"


def live_state_rec_types(storage=None) -> frozenset[str]:
    """Rec types/categories exempt from run-to-run dedupe suppression.

    Defaults to `DEFAULT_LIVE_STATE_REC_TYPES`. When a storage backend is
    passed, an operator override at `config/live-state-rec-types.json`
    is merged in:

        {"schema_version": "1", "rec_types": ["broken-page", "..."],
         "replace": false}

    `replace: true` swaps the defaults out entirely instead of extending
    them. A missing or malformed config is ignored — the defaults are
    the safe behavior (re-propose rather than silently drop a signal).
    """
    if storage is None:
        return DEFAULT_LIVE_STATE_REC_TYPES
    try:
        doc = storage.read_json(LIVE_STATE_CONFIG_KEY)
    except Exception:
        return DEFAULT_LIVE_STATE_REC_TYPES
    if not isinstance(doc, dict):
        return DEFAULT_LIVE_STATE_REC_TYPES
    extra = doc.get("rec_types")
    if not isinstance(extra, list):
        return DEFAULT_LIVE_STATE_REC_TYPES
    cleaned = {str(t).strip().lower() for t in extra if str(t).strip()}
    if doc.get("replace") is True:
        return frozenset(cleaned)
    return DEFAULT_LIVE_STATE_REC_TYPES | frozenset(cleaned)


def is_live_state(rec: dict, *, storage=None) -> bool:
    """True iff this rec re-measures production state and so must stay
    re-detectable on every run, regardless of how a prior run resolved it.

    Checks both `type` and `category` — see the note above on why.
    """
    if not isinstance(rec, dict):
        return False
    live = live_state_rec_types(storage)
    for field in ("type", "category"):
        val = (rec.get(field) or "")
        if isinstance(val, str) and val.strip().lower() in live:
            return True
    return False
