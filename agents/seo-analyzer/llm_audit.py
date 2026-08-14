#!/usr/bin/env python3
"""LLM-driven adaptive audit pass for the SEO analyzer (phase 2b).

RECONSTRUCTED 2026-08-14. Same `.gitignore` casualty as its siblings
pull-data.py / refresh-token.py: a bare `lib/` pattern matches at ANY
depth, so agents/seo-opportunity-agent/lib/{collector,analyzer}/ was
never committed and this file lived only on the retired fleet host.
Its absence is why every analyzer run on BOTH sites logged
`LLM audit failed: No module named 'llm_audit'` and shipped
heuristic-only recommendations.

There is NO tracked ancestor (nsc-assistant/agents/_legacy-seo-
opportunity-agent/ predates the LLM pass entirely). This is written
against the CURRENT contract, derived from three tracked sources:

  1. framework/tests/test_seo_llm_audit.py — the public API + its exact
     semantics (batching, whitelist filtering, error tolerance, the
     meta-title-length→ctr-fix / schema-product-missing→schema-markup
     rec-type mappings, body capping).
  2. agents/seo-analyzer/analyzer.py §4b — the call site: keyword args
     `pages, site_label, primary_objective, ai_chat_callable,
     batch_size, adaptive_context, active_goals`, then
     `issues_to_recommendations(issues, next_id)`.
  3. agents/implementer/build-aider-invocation.py TRUSTED_REC_TYPES —
     the ~120-entry check-id catalog, itself derived from this file
     before it was lost. That is the authoritative id list; it is
     reproduced here verbatim as CHECK_CATEGORIES.

CONTRACT (public API — do not break without updating the test):
    ALL_CHECK_IDS         frozenset of every legal check_id
    CHECK_CATEGORIES      dict[category, list[check_id]] — audit groups
    CHECK_ID_TO_REC_TYPE  dict[check_id, rec_type] (canonical or page-type)
    SEO_AUDIT_CHECKLIST   the rendered whitelist injected into the prompt
    run_llm_audit(...)    -> list[issue dict]
    issues_to_recommendations(issues, next_id) -> list[rec dict]
    format_pages_for_audit(pages, cap_chars=...) -> str
    _parse_llm_json(raw)  -> list

DESIGN RULES this file must keep honouring:
  * Framework LLM path only. The caller hands us
    `ai_chat_callable = ai_providers.ai_client_for("seo-analyzer").chat`,
    so provider routing / fallback / usage-tracking stay in the
    framework. We never import a vendor SDK or shell out to a CLI.
  * BATCHED. One prompt audits N pages (default 4). A per-item loop is
    the anti-pattern CLAUDE.md explicitly forbids.
  * DEGRADE, NEVER CRASH. No LLM client, no pages, an exception mid-
    batch, or unparseable output all return/skip rather than raise —
    the analyzer's deterministic recs must still ship. A failed batch
    leaves an `llm-audit-error` breadcrumb so the failure is visible in
    the run rather than silent.
  * WHITELIST-GATED. Only check_ids in ALL_CHECK_IDS survive, so an
    SEO expert can audit exactly what the agent looks for and the
    implementer never receives an invented rec type.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# The check-id catalog — the whitelist an SEO expert audits.
#
# Grouped into the audit families the prompt presents to the LLM. This
# list IS the contract: anything outside it is dropped as hallucination.
# Keep in sync with TRUSTED_REC_TYPES in
# agents/implementer/build-aider-invocation.py.
# ---------------------------------------------------------------------------
CHECK_CATEGORIES: dict[str, list[str]] = {
    "meta": [
        "meta-title-length",
        "meta-title-keyword",
        "meta-title-brand",
        "meta-title-duplicate",
        "meta-description-missing",
        "meta-description-length",
        "meta-description-keyword",
        "meta-description-cta",
        "meta-description-duplicate",
    ],
    "headings": [
        "h1-missing",
        "h1-multiple",
        "h1-mismatch",
        "heading-hierarchy",
    ],
    "content": [
        "content-thin",
        "content-freshness-low",
        "content-keyword-stuffing",
        "body-visible-date-missing",
        "faq-quality-thin",
    ],
    "eeat": [
        "eeat-author-missing",
        "eeat-author-bio",
        "eeat-about-missing",
        "eeat-policy-missing",
        "eeat-citations-missing",
        "eeat-outbound-citation-count",
        "eeat-publish-date-missing",
        "eeat-update-date-missing",
    ],
    "schema": [
        "schema-product-missing",
        "schema-article-missing",
        "schema-faqpage-missing",
        "schema-howto-missing",
        "schema-breadcrumblist-missing",
        "schema-organization-missing",
        "schema-website-searchaction-missing",
        "schema-incomplete",
        "schema-invalid",
        "schema-deprecated",
    ],
    "indexing": [
        "indexing-robots-blocked",
        "indexing-noindex-conflict",
        "indexing-canonical-self",
        "indexing-canonical-non-2xx",
        "indexing-soft-404",
        "indexing-sitemap-404",
        "indexing-sitemap-shrank",
        "indexing-breadcrumb-parity",
        "indexing-pagination-rel",
        "indexing-hreflang-missing",
        "indexing-hreflang-asymmetric",
    ],
    "internal-linking": [
        "link-orphan",
        "link-broken",
        "link-redirect-chain",
        "link-anchor-generic",
        "link-anchor-keyword",
        "link-nofollow-internal",
        "internal-link-to-featured",
        "internal-link-graph-regression",
        "body-internal-links-thin",
        "topical-cluster-orphan",
        "footer-trust-links-missing",
    ],
    "images": [
        "image-alt-missing",
        "image-alt-empty-content",
        "image-alt-keyword-stuffing",
        "image-filename-non-descriptive",
    ],
    "core-web-vitals": [
        "cwv-render-blocking",
        "cwv-ttfb-slow",
        "cwv-ttfb-very-slow",
        "cwv-large-dom",
        "cwv-image-no-dimensions",
        "cwv-image-no-lazy",
        "cwv-image-format",
        "cwv-font-no-display",
    ],
    "mobile": [
        "mobile-viewport-missing",
        "mobile-font-small",
        "mobile-tap-targets",
        "mobile-horizontal-scroll",
    ],
    "ai-search-geo": [
        "geo-direct-answer-missing",
        "geo-faq-missing",
        "geo-statistics-missing",
        "geo-listicle-no-summary",
        "geo-author-credentials",
        "geo-llms-txt-missing",
        "llm-search-direct-answer-missing",
        "llm-search-faq-format",
        "llm-search-citation-readiness",
        "llm-search-quote-extractability",
    ],
    "conversion": [
        "cta-missing",
        "cta-weak",
        "cta-position",
        "conversion-path",
        "featured-product-pdp-improve",
    ],
    "trust": [
        "trust-signals-missing",
        "trust-signal-density-thin",
    ],
    "url-hygiene": [
        "url-deep",
        "url-non-descriptive",
        "url-trailing-slash",
        "url-uppercase",
    ],
    "article": [
        "article-author-credentials-missing",
        "article-cited-sources-missing",
        "article-datemodified-missing",
        "article-publish-update-dates",
        "article-wordcount-schema-missing",
    ],
    "product": [
        "product-schema-incomplete",
        "product-schema-rich-results",
        "product-specs-table-missing",
        "product-pros-cons-missing",
        "product-aggregate-rating-thin",
        "product-comparison-link-missing",
        "product-image-count-thin",
        "product-image-present",
        "product-image-valid-url",
        "product-brand-present",
        "product-affiliate-tag-missing",
        "product-affiliate-cta-position",
    ],
    "recipe": [
        "recipe-schema-incomplete",
        "recipe-conversational-intro-missing",
        "recipe-internal-links-thin",
        "recipe-image-present",
        "recipe-image-valid-url",
        "recipe-video-present",
        "recipe-nutrition-sanity",
        "recipe-cuisine-assigned",
        "recipe-category-assigned",
        "recipe-category-in-allowlist",
        "recipe-duplicate-source-url",
    ],
    "review": [
        "review-template-incomplete",
        "review-citations-section-missing",
        "review-buyer-persona-segmentation-missing",
        "review-specs-source-attribution-missing",
    ],
    "head-to-head": [
        "h2h-comparison-format-readiness",
        "h2h-quote-worthy-verdict-missing",
        "h2h-stale-pricing",
        "h2h-stale-pricing-product",
    ],
    "feature": [
        "feature-conversational-content",
        "feature-internal-link-cluster",
    ],
}

ALL_CHECK_IDS: frozenset[str] = frozenset(
    cid for ids in CHECK_CATEGORIES.values() for cid in ids
)

# Sentinel emitted by run_llm_audit itself when a batch blows up. It is
# deliberately NOT in ALL_CHECK_IDS (the LLM must not be able to emit
# it) and issues_to_recommendations drops it.
AUDIT_ERROR_CHECK_ID = "llm-audit-error"


# ---------------------------------------------------------------------------
# check_id -> rec_type
#
# Two mapping regimes:
#   * Page-type check ids (recipe-/product-/h2h-/article-/feature-/
#     review-) pass through UNCHANGED. The implementer dispatches on
#     those prefixes directly and they are already in TRUSTED_REC_TYPES.
#   * Everything else collapses onto a canonical analyzer rec type so
#     downstream routing (framework.core.work_types) stays small.
# ---------------------------------------------------------------------------
PAGE_TYPE_PREFIXES: tuple[str, ...] = (
    "recipe-", "product-", "h2h-", "article-", "feature-", "review-",
)

# Exact-match overrides applied BEFORE the prefix rules below.
_REC_TYPE_EXACT: dict[str, str] = {
    # "featured-…" is not "feature-…"; it is a PDP conversion rec.
    "featured-product-pdp-improve": "conversion-path",
    "conversion-path": "conversion-path",
    # llms.txt is a new static file, not a body edit.
    "geo-llms-txt-missing": "other",
    "image-filename-non-descriptive": "other",
    # Sitemap-specific indexing checks go to the sitemap path.
    "indexing-sitemap-404": "sitemap-fix",
    "indexing-sitemap-shrank": "sitemap-fix",
    # A broken link / redirect chain is a redirect fix, not a link add.
    "link-broken": "redirect-fix",
    "link-redirect-chain": "redirect-fix",
    # Linking-shaped checks that do not start with "link-"/"internal-link".
    "body-internal-links-thin": "internal-link",
    "topical-cluster-orphan": "internal-link",
    "footer-trust-links-missing": "internal-link",
    # Content-shaped checks that do not start with "content-".
    "faq-quality-thin": "content-expansion",
    "body-visible-date-missing": "content-expansion",
}

# Ordered prefix rules — first match wins, so put longer prefixes first.
_REC_TYPE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("meta-", "ctr-fix"),
    ("schema-", "schema-markup"),
    ("indexing-", "indexing-fix"),
    ("internal-link", "internal-link"),
    ("link-", "internal-link"),
    ("url-", "redirect-fix"),
    ("cwv-", "ssr-fix"),
    ("mobile-", "ssr-fix"),
    ("h1-", "ssr-fix"),
    ("heading-", "ssr-fix"),
    ("image-alt-", "ssr-fix"),
    ("cta-", "conversion-path"),
    ("eeat-", "content-expansion"),
    ("geo-", "content-expansion"),
    ("llm-search-", "content-expansion"),
    ("trust-", "content-expansion"),
    ("content-", "content-expansion"),
)


def rec_type_for_check(check_id: str) -> str:
    """Map a check_id to the rec type the implementer dispatches on."""
    cid = (check_id or "").strip()
    if cid in _REC_TYPE_EXACT:
        return _REC_TYPE_EXACT[cid]
    if cid.startswith(PAGE_TYPE_PREFIXES):
        return cid
    for prefix, rec_type in _REC_TYPE_PREFIXES:
        if cid.startswith(prefix):
            return rec_type
    return "other"


CHECK_ID_TO_REC_TYPE: dict[str, str] = {
    cid: rec_type_for_check(cid) for cid in sorted(ALL_CHECK_IDS)
}


# ---------------------------------------------------------------------------
# One-line description per check. These are what the LLM actually reads,
# so they are written as decision rules ("flag when X"), not labels.
# ---------------------------------------------------------------------------
CHECK_DESCRIPTIONS: dict[str, str] = {
    # meta
    "meta-title-length": "<title> outside 30-65 chars (too short wastes SERP space; >65 truncates)",
    "meta-title-keyword": "primary keyword absent from <title>, or buried after the brand",
    "meta-title-brand": "brand suffix missing, duplicated, or eating the front of the title",
    "meta-title-duplicate": "<title> is identical to another audited page's title",
    "meta-description-missing": "no meta description, so Google invents the snippet",
    "meta-description-length": "meta description outside 120-165 chars",
    "meta-description-keyword": "meta description omits the query the page targets",
    "meta-description-cta": "meta description is a flat summary with no click incentive",
    "meta-description-duplicate": "meta description duplicated across audited pages",
    # headings
    "h1-missing": "page renders no <h1>",
    "h1-multiple": "more than one <h1> on the page",
    "h1-mismatch": "<h1> and <title> target different topics",
    "heading-hierarchy": "heading levels skip (h1 -> h3) or restart illogically",
    # content
    "content-thin": "body word count far below what the query intent needs",
    "content-freshness-low": "content is stale for a query with recency intent",
    "content-keyword-stuffing": "unnatural keyword repetition in body or headings",
    "body-visible-date-missing": "no human-visible publish/updated date in the body",
    "faq-quality-thin": "FAQ section exists but answers are one-liners with no substance",
    # eeat
    "eeat-author-missing": "no author attributed on a page that makes claims",
    "eeat-author-bio": "author named but no credential/bio establishing expertise",
    "eeat-about-missing": "no About page linked from the page or footer",
    "eeat-policy-missing": "no editorial/affiliate-disclosure policy linked",
    "eeat-citations-missing": "factual or benchmark claims with zero sources cited",
    "eeat-outbound-citation-count": "too few authoritative outbound citations for the claim density",
    "eeat-publish-date-missing": "no publish date in markup",
    "eeat-update-date-missing": "no dateModified in markup on updated content",
    # schema
    "schema-product-missing": "product page with no Product JSON-LD",
    "schema-article-missing": "article/blog page with no Article JSON-LD",
    "schema-faqpage-missing": "page has a real Q&A block but no FAQPage JSON-LD",
    "schema-howto-missing": "step-by-step content with no HowTo JSON-LD",
    "schema-breadcrumblist-missing": "no BreadcrumbList JSON-LD on a nested page",
    "schema-organization-missing": "no Organization/Publisher JSON-LD sitewide",
    "schema-website-searchaction-missing": "no WebSite + SearchAction on the homepage",
    "schema-incomplete": "JSON-LD present but missing required/recommended fields",
    "schema-invalid": "JSON-LD malformed, wrong @type, or fails rich-result rules",
    "schema-deprecated": "uses a deprecated schema type or property",
    # indexing
    "indexing-robots-blocked": "robots.txt or meta robots blocks a page that should rank",
    "indexing-noindex-conflict": "noindex conflicts with canonical/sitemap inclusion",
    "indexing-canonical-self": "canonical missing or not self-referential where it should be",
    "indexing-canonical-non-2xx": "canonical points at a non-2xx or redirecting URL",
    "indexing-soft-404": "thin/empty page returning 200 that Google will treat as a soft 404",
    "indexing-sitemap-404": "sitemap lists a URL that does not return 200",
    "indexing-sitemap-shrank": "sitemap entry count dropped versus the prior crawl",
    "indexing-breadcrumb-parity": "visible breadcrumb and BreadcrumbList JSON-LD disagree",
    "indexing-pagination-rel": "paginated set lacks coherent pagination signals",
    "indexing-hreflang-missing": "localized page with no hreflang annotations",
    "indexing-hreflang-asymmetric": "hreflang declared without a reciprocal return tag",
    # internal linking
    "link-orphan": "page has no inbound internal links from the audited set",
    "link-broken": "internal link targets a 4xx/5xx URL",
    "link-redirect-chain": "internal link hops through one or more redirects",
    "link-anchor-generic": 'anchor text is "click here" / "read more" / a bare URL',
    "link-anchor-keyword": "anchor text over-optimised with exact-match keywords",
    "link-nofollow-internal": "internal link carries rel=nofollow, blocking equity",
    "internal-link-to-featured": "high-value/featured page under-linked from body content",
    "internal-link-graph-regression": "page lost internal links versus the prior crawl",
    "body-internal-links-thin": "body copy carries too few contextual internal links",
    "topical-cluster-orphan": "page sits outside its topical cluster with no hub link",
    "footer-trust-links-missing": "footer lacks About/Contact/Privacy/Editorial trust links",
    # images
    "image-alt-missing": "content image with no alt attribute",
    "image-alt-empty-content": "meaningful image given alt=\"\" (decorative-only marker)",
    "image-alt-keyword-stuffing": "alt text stuffed with keywords instead of describing the image",
    "image-filename-non-descriptive": "image filename is IMG_1234-style with no descriptive slug",
    # core web vitals
    "cwv-render-blocking": "render-blocking CSS/JS in <head> delaying first paint",
    "cwv-ttfb-slow": "server TTFB above the comfortable threshold",
    "cwv-ttfb-very-slow": "server TTFB bad enough to fail Core Web Vitals outright",
    "cwv-large-dom": "excessive DOM node count inflating layout cost",
    "cwv-image-no-dimensions": "img/picture without width+height, causing layout shift",
    "cwv-image-no-lazy": "below-the-fold images not lazy-loaded",
    "cwv-image-format": "images served as JPEG/PNG where WebP/AVIF is available",
    "cwv-font-no-display": "web font loaded without font-display, blocking text paint",
    # mobile
    "mobile-viewport-missing": "no responsive viewport meta tag",
    "mobile-font-small": "body copy below ~16px on mobile",
    "mobile-tap-targets": "tap targets smaller/closer than the 48px guidance",
    "mobile-horizontal-scroll": "fixed-width element forcing horizontal scroll on mobile",
    # ai search / GEO
    "geo-direct-answer-missing": "no concise direct answer in the first ~60 words for the head query",
    "geo-faq-missing": "no question-shaped section for an obviously question-shaped query",
    "geo-statistics-missing": "no concrete numbers/stats that generative engines can quote",
    "geo-listicle-no-summary": "listicle with no summary table or TL;DR block up top",
    "geo-author-credentials": "no machine-readable author credentials for AI-search attribution",
    "geo-llms-txt-missing": "site serves no /llms.txt describing content for LLM crawlers",
    "llm-search-direct-answer-missing": "answer is buried below preamble instead of stated first",
    "llm-search-faq-format": "Q&A not in a parseable question-then-answer structure",
    "llm-search-citation-readiness": "claims lack the sourcing an AI answer would need to cite the page",
    "llm-search-quote-extractability": "no self-contained quotable sentence an AI answer can lift",
    # conversion
    "cta-missing": "monetisable page with no call to action",
    "cta-weak": "CTA copy is generic and gives no reason to click",
    "cta-position": "primary CTA only appears below the fold or only once",
    "conversion-path": "path from this page to the conversion action is broken or indirect",
    "featured-product-pdp-improve": "featured PDP under-built versus the traffic it receives",
    # trust
    "trust-signals-missing": "no reviews/testimonials/guarantees/security signals present",
    "trust-signal-density-thin": "trust signals present but too sparse for the page type",
    # url hygiene
    "url-deep": "URL nested more levels deep than the site's structure warrants",
    "url-non-descriptive": "URL uses ids/params instead of a readable keyword slug",
    "url-trailing-slash": "trailing-slash handling inconsistent with the site canon",
    "url-uppercase": "URL contains uppercase characters",
    # article
    "article-author-credentials-missing": "article author has no credentials backing the topic",
    "article-cited-sources-missing": "article states facts with no cited sources",
    "article-datemodified-missing": "article JSON-LD has no dateModified",
    "article-publish-update-dates": "published/updated dates missing, conflicting, or implausible",
    "article-wordcount-schema-missing": "article JSON-LD omits wordCount",
    # product
    "product-schema-incomplete": "Product JSON-LD missing offers/price/availability/rating",
    "product-schema-rich-results": "Product JSON-LD present but fails rich-result eligibility",
    "product-specs-table-missing": "PDP has no structured specifications table",
    "product-pros-cons-missing": "PDP has no pros/cons block",
    "product-aggregate-rating-thin": "AggregateRating based on too few reviews to display",
    "product-comparison-link-missing": "PDP does not link to a comparison or alternative",
    "product-image-count-thin": "PDP carries too few product images",
    "product-image-present": "product has no image at all",
    "product-image-valid-url": "product image URL is broken, placeholder, or non-2xx",
    "product-brand-present": "product record has no brand assigned",
    "product-affiliate-tag-missing": "affiliate link missing its tracking tag (unmonetised click)",
    "product-affiliate-cta-position": "affiliate CTA placed where it will not be seen",
    # recipe
    "recipe-schema-incomplete": "Recipe JSON-LD missing ingredients/instructions/times/yield",
    "recipe-conversational-intro-missing": "recipe opens with no conversational intro for AI search",
    "recipe-internal-links-thin": "recipe body carries too few links to related recipes/kitchen pages",
    "recipe-image-present": "recipe has no hero image",
    "recipe-image-valid-url": "recipe image URL is broken, placeholder, or non-2xx",
    "recipe-video-present": "recipe has no video where the query intent expects one",
    "recipe-nutrition-sanity": "nutrition values missing or physically implausible",
    "recipe-cuisine-assigned": "recipe has no cuisine assigned",
    "recipe-category-assigned": "recipe has no category assigned",
    "recipe-category-in-allowlist": "recipe category is outside the site's allowed taxonomy",
    "recipe-duplicate-source-url": "recipe duplicates another recipe's source URL",
    # review
    "review-template-incomplete": "review missing required template sections",
    "review-citations-section-missing": "review has no citations/sources section",
    "review-buyer-persona-segmentation-missing": "review does not segment picks by buyer persona",
    "review-specs-source-attribution-missing": "spec claims in the review are unattributed",
    # head-to-head
    "h2h-comparison-format-readiness": "comparison not in a table/format engines can extract",
    "h2h-quote-worthy-verdict-missing": "no crisp self-contained verdict sentence",
    "h2h-stale-pricing": "comparison quotes prices that are out of date",
    "h2h-stale-pricing-product": "a specific product in the comparison has stale pricing",
    # feature
    "feature-conversational-content": "page lacks the conversational phrasing voice/AI search rewards",
    "feature-internal-link-cluster": "feature page not wired into a supporting link cluster",
}


def _render_checklist() -> str:
    """Render CHECK_CATEGORIES + CHECK_DESCRIPTIONS into the prompt block."""
    lines: list[str] = [
        "SEO AUDIT CHECKLIST — the COMPLETE set of issues you may report.",
        "Use the exact bracketed check_id. Do not invent categories or",
        "report anything outside it; ids outside this list are discarded.",
        "",
    ]
    for category, ids in CHECK_CATEGORIES.items():
        lines.append(f"## {category}")
        for cid in ids:
            desc = CHECK_DESCRIPTIONS.get(cid, cid.replace("-", " "))
            lines.append(f"  [{cid}] — {desc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


SEO_AUDIT_CHECKLIST: str = _render_checklist()


# ---------------------------------------------------------------------------
# Response parsing — tolerant by design
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _parse_llm_json(raw: str) -> list[dict]:
    """Extract a JSON array of issue objects from a model response.

    Models wrap output in ```json fences, prepend "Here are the issues
    I found:", or return a bare object instead of an array. All three
    are recovered here; anything genuinely unparseable returns [] so a
    malformed response costs us one batch, not the run.
    """
    if not raw or not isinstance(raw, str):
        return []
    text = raw.strip()

    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)
    # Last resort: slice from the first bracket to its matching last one.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if 0 <= start < end:
            candidates.append(text[start:end + 1])

    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
        except Exception:
            continue
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
        if isinstance(parsed, dict):
            # Tolerate {"issues": [...]} / {"findings": [...]} wrappers.
            for key in ("issues", "findings", "results", "recommendations"):
                inner = parsed.get(key)
                if isinstance(inner, list):
                    return [x for x in inner if isinstance(x, dict)]
            # A single bare issue object.
            if "check_id" in parsed:
                return [parsed]
    return []


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------
# Rendered in this order; only keys actually present are emitted, so the
# same function handles rich pages-by-type.jsonl records and the sparse
# on-demand crawl records (url/title/h1/description/canonical/body_text).
_PAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("type", "page_type"),
    ("status", "http_status"),
    ("title", "title"),
    ("description", "meta_description"),
    ("h1", "h1"),
    ("h1_count", "h1_count"),
    ("canonical", "canonical"),
    ("canonical_count", "canonical_count"),
    ("word_count", "word_count"),
    ("jsonld_types", "jsonld_types"),
    ("og_keys", "open_graph_keys"),
    ("twitter_card_keys", "twitter_card_keys"),
    ("img_count", "image_count"),
    ("img_with_alt", "images_with_alt"),
    ("internal_links", "internal_link_count"),
    ("body_relative_link_count", "body_internal_link_count"),
    ("body_relative_link_samples", "body_internal_link_samples"),
    ("faq_heading", "faq_heading"),
)


def format_pages_for_audit(pages: Iterable[dict], cap_chars: int = 2500) -> str:
    """Render page records into the prompt's PAGES block.

    `cap_chars` bounds each page's body text — that is the only field
    with unbounded size, and letting it through uncapped is what blows
    the context window on a long article.
    """
    out: list[str] = []
    for idx, page in enumerate(pages or [], start=1):
        if not isinstance(page, dict):
            continue
        url = str(page.get("url") or "")
        out.append(f"--- PAGE {idx} ---")
        out.append(f"url: {url}")
        for key, label in _PAGE_FIELDS:
            if key not in page:
                continue
            val = page.get(key)
            if val is None or val == "" or val == [] :
                continue
            if isinstance(val, (list, tuple)):
                val = ", ".join(str(v) for v in val)
            out.append(f"{label}: {val}")
        body = page.get("body_text") or page.get("body") or ""
        if body:
            body = str(body)
            truncated = len(body) > cap_chars
            body = body[:cap_chars]
            out.append("body_text: " + body + (" …[truncated]" if truncated else ""))
        out.append("")
    return "\n".join(out)


def _format_goals(active_goals: list[dict] | None) -> str:
    """Render the agent's active goals so the audit biases toward them."""
    if not active_goals:
        return ""
    lines = ["ACTIVE GOALS — prefer issues that move these metrics:"]
    for g in active_goals:
        if not isinstance(g, dict):
            continue
        title = g.get("title") or g.get("id") or ""
        if not title:
            continue
        metric = g.get("metric") or {}
        bit = f"- {title}"
        if isinstance(metric, dict) and metric.get("name"):
            bit += (
                f" (metric {metric.get('name')}: "
                f"{metric.get('current')} -> {metric.get('target')})"
            )
        lines.append(bit)
        for directive in (g.get("directives") or [])[:3]:
            lines.append(f"    * {directive}")
    return "\n".join(lines) if len(lines) > 1 else ""


_SYSTEM_PROMPT = """\
You are a senior technical SEO auditor reviewing pages from a live site.

You report ONLY issues you can prove from the page data you are given.
Every issue must quote the exact evidence from that page. If a check's
data is not present in the record, you cannot evaluate it — skip it
rather than assuming. Never guess at a page you were not shown, and
never report a URL that is not in the PAGES block.

Return a JSON array (and nothing else) where each element is:
{
  "check_id":   "<one id from the checklist, exactly as written>",
  "url":        "<the exact url from the PAGES block>",
  "severity":   "critical" | "high" | "medium" | "low",
  "confidence": <0.0-1.0, how certain you are this is really a problem>,
  "title":      "<one-line headline naming the page and the problem>",
  "rationale":  "<why this costs traffic, rankings, or conversions>",
  "evidence":   "<exact snippet/value from the page proving it>",
  "fix":        "<concrete change to make; name the element or field>"
}

Return [] if the pages are clean. An empty array is a valid, useful
answer — do not manufacture issues to fill space. Prefer a handful of
high-confidence, high-severity findings over an exhaustive list of
trivia."""


def _build_messages(
    *,
    pages_block: str,
    site_label: str,
    primary_objective: str,
    adaptive_context: str,
    active_goals: list[dict] | None,
) -> list[dict]:
    """Assemble the (system, user) message pair for one batch."""
    parts = [
        f"SITE: {site_label}",
        f"PRIMARY OBJECTIVE: {primary_objective}",
        "",
        SEO_AUDIT_CHECKLIST,
    ]
    goals_block = _format_goals(active_goals)
    if goals_block:
        parts += ["", goals_block]
    if adaptive_context:
        parts += [
            "",
            "PRIOR CHANGES ON THIS SITE — do not re-report what was already",
            "fixed; prefer categories that have not yet moved the metric:",
            adaptive_context,
        ]
    parts += ["", "PAGES TO AUDIT:", "", pages_block]
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ---------------------------------------------------------------------------
# Issue normalisation
# ---------------------------------------------------------------------------
_SEVERITIES = ("critical", "high", "medium", "low")


def _canonical_url(raw: str, batch_urls: list[str]) -> str:
    """Snap a model-reported URL back onto a URL we actually sent.

    Models shorten ("/recipes/x" for the full URL) or re-case. We match
    exactly, then by path suffix. If nothing matches we keep the raw
    value rather than dropping the finding — check_id whitelisting is
    the real hallucination guard, and silently discarding issues would
    hide genuine findings.
    """
    url = (raw or "").strip()
    if not url:
        return batch_urls[0] if batch_urls else ""
    if url in batch_urls:
        return url
    tail = url.rstrip("/").rsplit("/", 1)[-1].lower()
    if tail:
        for candidate in batch_urls:
            if candidate.rstrip("/").lower().endswith(url.rstrip("/").lower()):
                return candidate
            if candidate.rstrip("/").rsplit("/", 1)[-1].lower() == tail:
                return candidate
    return url


def _normalize_issue(raw: dict, batch_urls: list[str]) -> dict | None:
    """Validate + coerce one model-reported issue. None => discard."""
    if not isinstance(raw, dict):
        return None
    check_id = str(raw.get("check_id") or "").strip()
    # THE hallucination gate: unknown ids never reach the implementer.
    if check_id not in ALL_CHECK_IDS:
        return None

    severity = str(raw.get("severity") or "medium").strip().lower()
    if severity not in _SEVERITIES:
        severity = "medium"

    try:
        confidence = float(raw.get("confidence", 0.75))
    except (TypeError, ValueError):
        confidence = 0.75
    confidence = max(0.0, min(1.0, confidence))

    def _s(key: str, limit: int = 2000) -> str:
        val = raw.get(key)
        return str(val).strip()[:limit] if val is not None else ""

    return {
        "check_id": check_id,
        "url": _canonical_url(str(raw.get("url") or ""), batch_urls),
        "severity": severity,
        "confidence": confidence,
        "title": _s("title", 300) or f"{check_id} on page",
        "rationale": _s("rationale"),
        "evidence": _s("evidence"),
        "fix": _s("fix"),
    }


def run_llm_audit(
    *,
    pages: list[dict],
    site_label: str,
    primary_objective: str = "top5-rank",
    ai_chat_callable: Callable[..., str] | None = None,
    batch_size: int = 4,
    adaptive_context: str = "",
    active_goals: list[dict] | None = None,
    min_confidence: float = 0.5,
    body_cap_chars: int = 2500,
    max_tokens: int = 4000,
    temperature: float = 0.0,
) -> list[dict]:
    """Audit `pages` with the LLM, returning validated issue dicts.

    Batched: one prompt per `batch_size` pages, never one per page.

    Degradation ladder — none of these raise:
      * no client / no pages          -> []
      * one batch raises or times out -> `llm-audit-error` breadcrumb,
                                         remaining batches still run
      * unparseable batch output      -> that batch contributes nothing
      * hallucinated check_id         -> dropped by the whitelist
    """
    if ai_chat_callable is None:
        return []
    clean_pages = [p for p in (pages or []) if isinstance(p, dict)]
    if not clean_pages:
        return []
    batch_size = max(1, int(batch_size or 1))

    # (url, check_id) -> issue. dict preserves insertion order, so the
    # output stays in page order while higher-confidence duplicates win.
    collected: dict[tuple[str, str], dict] = {}

    for batch_no, start in enumerate(range(0, len(clean_pages), batch_size), start=1):
        batch = clean_pages[start:start + batch_size]
        batch_urls = [str(p.get("url") or "") for p in batch]
        try:
            messages = _build_messages(
                pages_block=format_pages_for_audit(batch, cap_chars=body_cap_chars),
                site_label=site_label,
                primary_objective=primary_objective,
                adaptive_context=adaptive_context,
                active_goals=active_goals,
            )
            raw = ai_chat_callable(
                messages, temperature=temperature, max_tokens=max_tokens,
            )
        except Exception as e:  # transient LLM blip, timeout, quota, ...
            print(
                f"  [llm-audit] batch {batch_no} failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            # Unique key per batch so multiple failures don't collapse.
            collected[(f"batch-{batch_no}", AUDIT_ERROR_CHECK_ID)] = {
                "check_id": AUDIT_ERROR_CHECK_ID,
                "url": batch_urls[0] if batch_urls else "",
                "severity": "low",
                "confidence": 0.0,
                "title": f"LLM audit batch {batch_no} failed",
                "rationale": f"{type(e).__name__}: {e}",
                "evidence": "",
                "fix": "",
            }
            continue

        for raw_issue in _parse_llm_json(raw):
            issue = _normalize_issue(raw_issue, batch_urls)
            if issue is None:
                continue
            if issue["confidence"] < min_confidence:
                continue
            key = (issue["url"], issue["check_id"])
            prior = collected.get(key)
            if prior is None or issue["confidence"] > prior["confidence"]:
                collected[key] = issue

    return list(collected.values())


# ---------------------------------------------------------------------------
# issue -> recommendation
# ---------------------------------------------------------------------------
def issues_to_recommendations(
    issues: list[dict],
    next_id: Callable[[], str],
) -> list[dict]:
    """Convert audit issues into recommendations.json entries.

    Field names follow shared/schemas/recommendations.schema.json so the
    reporter, the dashboard and the implementer all read them unchanged.
    `llm_check_id` is what lets a human trace a shipped rec back to the
    exact checklist entry that produced it.
    """
    recs: list[dict] = []
    for issue in issues or []:
        if not isinstance(issue, dict):
            continue
        check_id = str(issue.get("check_id") or "").strip()
        # Error breadcrumbs are tracked in the run, never shipped as work.
        if not check_id or check_id == AUDIT_ERROR_CHECK_ID:
            continue

        url = str(issue.get("url") or "")
        severity = str(issue.get("severity") or "medium").lower()
        if severity not in _SEVERITIES:
            severity = "medium"

        rationale = str(issue.get("rationale") or "").strip()
        evidence = str(issue.get("evidence") or "").strip()
        fix = str(issue.get("fix") or "").strip()
        if evidence:
            rationale = f"{rationale}\n\nEvidence: {evidence}".strip()

        outline: dict[str, Any] = {}
        if fix:
            outline["notes"] = fix

        rec: dict[str, Any] = {
            "id": next_id(),
            "type": CHECK_ID_TO_REC_TYPE.get(check_id) or rec_type_for_check(check_id),
            "priority": severity,
            "title": str(issue.get("title") or f"{check_id} on {url}")[:300],
            "rationale": rationale,
            "data_refs": [url] if url else [],
            "implementation_outline": outline,
            "implemented": False,
            "llm_check_id": check_id,
        }
        try:
            rec["llm_confidence"] = float(issue.get("confidence", 0.0))
        except (TypeError, ValueError):
            rec["llm_confidence"] = 0.0
        if evidence:
            rec["llm_evidence"] = evidence[:2000]
        recs.append(rec)
    return recs
