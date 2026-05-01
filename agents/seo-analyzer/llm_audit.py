"""LLM-driven adaptive SEO audit pass for seo-analyzer.

Centers on a specific, verifiable checklist of SEO best practices that
evolve with search-engine behavior. The prompt enumerates each check
explicitly so an SEO expert can validate that the agent is looking for
the right things.

Why LLM in this loop:
  - Search-engine signals are a moving target (Core Web Vitals weights,
    AI search citations, EEAT thresholds, schema-markup support all shift)
  - A deterministic analyzer can ONLY flag things its code knows about;
    the LLM pass surfaces emerging opportunities the deterministic pass
    misses
  - LLM still operates on REAL data (page HTML + GSC stats + GA4 events)
    not vibes — every rec must cite evidence from the inputs

The deterministic pass (analyzer.py) handles things we KNOW are right:
top-5 keyword targets, indexing-error counts, conversion-path drops.
This LLM pass handles things that REQUIRE judgment: "is this title
under-optimized?" "is the schema markup complete for this content
type?" "is this content thin for an EEAT-sensitive query?"

The two passes' recs are merged + de-duplicated by analyzer.py.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any


# ---------------------------------------------------------------------------
# The audit checklist — explicit, expert-verifiable
# ---------------------------------------------------------------------------
#
# Every category listed here MUST be a concrete, observable check the LLM
# can verify from page HTML / structured data / GSC stats. No vibes,
# no "improve user experience". Each check has a short label that the
# user (or an SEO expert reviewing the agent's behavior) can audit.

SEO_AUDIT_CHECKLIST = """
You are an SEO expert auditing pages from a website. Use the data
provided (page HTML excerpts + GSC + GA4 stats) to flag issues that
match THIS SPECIFIC CHECKLIST. Do not invent categories outside it.

═════════════════════════════════════════════════════════════════════
TITLE + META DESCRIPTION (every page)
─────────────────────────────────────────────────────────────────────
[meta-title-length]            Title tag is 30-60 chars (truncated if longer)
[meta-title-keyword]           Primary keyword appears in title (front-loaded ideal)
[meta-title-brand]             Brand name in title (end position)
[meta-description-length]      Description 120-160 chars
[meta-description-cta]         Description has an action verb / value prop
[meta-description-keyword]     Description includes the primary query keyword
[meta-description-missing]     <meta name=\"description\"> missing entirely
[meta-title-duplicate]         Same <title> as another page on the site
[meta-description-duplicate]   Same description as another page

HEADINGS + CONTENT STRUCTURE
─────────────────────────────────────────────────────────────────────
[h1-missing]                   Page has no <h1>
[h1-multiple]                  Page has more than one <h1>
[h1-mismatch]                  H1 doesn't reflect page intent / contradicts title
[heading-hierarchy]            Headings skip levels (h1 → h3 with no h2)
[content-thin]                 Body text < 300 words on a page that should rank
[content-keyword-stuffing]     Same keyword phrase repeated suspiciously often

STRUCTURED DATA / SCHEMA.ORG
─────────────────────────────────────────────────────────────────────
[schema-product-missing]       Product page with no Product JSON-LD
[schema-article-missing]       Blog post with no Article JSON-LD
[schema-faqpage-missing]       FAQ-style page with no FAQPage schema
[schema-howto-missing]         How-to/recipe with no HowTo or Recipe schema
[schema-breadcrumblist-missing] Page in deep nav with no BreadcrumbList
[schema-organization-missing]  Site has no Organization schema (homepage)
[schema-website-searchaction-missing] Site has no SearchAction schema
[schema-incomplete]            JSON-LD present but missing required fields
[schema-invalid]               JSON-LD has syntax errors
[schema-deprecated]            Using a deprecated schema property

E-E-A-T SIGNALS (Experience / Expertise / Authority / Trust)
─────────────────────────────────────────────────────────────────────
[eeat-author-missing]          Article with no visible author byline
[eeat-author-bio]              Author byline present but no linked bio
[eeat-publish-date-missing]    No visible publish date
[eeat-update-date-missing]     Old content with no \"updated\" date
[eeat-citations-missing]       Claims without sources / outbound citations
[eeat-about-missing]           No About / Contact page reachable in 1 click
[eeat-policy-missing]          No privacy / refund / policy pages

INTERNAL LINKING
─────────────────────────────────────────────────────────────────────
[link-orphan]                  Page has no inbound internal links
[link-anchor-generic]          Anchor text is \"click here\" / \"read more\" / \"learn more\"
[link-anchor-keyword]          Internal anchor text could be keyword-optimized
[link-broken]                  Internal link 404s (only flag if observed in crawl)
[link-redirect-chain]          Internal link goes through 2+ redirects
[link-nofollow-internal]       Internal link has rel=\"nofollow\" (usually wrong)

MOBILE-FIRST + UX
─────────────────────────────────────────────────────────────────────
[mobile-viewport-missing]      No viewport meta tag
[mobile-tap-targets]           Tap targets < 48x48px close together
[mobile-font-small]            Body font-size < 16px (mobile readability)
[mobile-horizontal-scroll]     Page requires horizontal scroll on phone

CORE WEB VITALS / PERFORMANCE
─────────────────────────────────────────────────────────────────────
[cwv-render-blocking]          Synchronous JS / non-async CSS in <head>
[cwv-image-no-dimensions]      <img> without width/height (causes CLS)
[cwv-image-no-lazy]            Below-fold image without loading=\"lazy\"
[cwv-image-format]             Image is JPEG/PNG when WebP/AVIF would be smaller
[cwv-font-no-display]          @font-face without font-display:swap
[cwv-large-dom]                DOM > ~1500 nodes (LCP penalty)
[cwv-ttfb-slow]                TTFB > 600ms on a SSR route (Lighthouse-equivalent threshold)
[cwv-ttfb-very-slow]           TTFB > 1500ms — page feels broken; almost certainly hurting Core Web Vitals INP

CRAWLABILITY + INDEXING
─────────────────────────────────────────────────────────────────────
[indexing-noindex-conflict]    Page has noindex but also in sitemap
[indexing-canonical-self]      Canonical missing or pointing wrong (self-canonical recommended)
[indexing-canonical-non-2xx]   Canonical URL returns non-2xx
[indexing-sitemap-404]         Sitemap entry returns 404
[indexing-sitemap-shrank]      Current sitemap has lost ≥30 URLs vs prior run (alarm — broken pipeline?)
[indexing-robots-blocked]      Important page blocked by robots.txt
[indexing-pagination-rel]      Paginated series without prev/next or canonical
[indexing-soft-404]            Real page returning 200 but with thin/error content
[indexing-hreflang-missing]    Multi-locale site emits no hreflang link tag on this page
[indexing-hreflang-asymmetric] Page declares hreflang for X but the X page has no return-link to this page
[indexing-breadcrumb-parity]   Visible breadcrumb labels don't match BreadcrumbList JSON-LD itemListElement names
[indexing-itemlist-numberOfItems-missing]  ItemList / CollectionPage schema lacks numberOfItems (Google carousel ineligible)

URL STRUCTURE
─────────────────────────────────────────────────────────────────────
[url-non-descriptive]          URL has session ids / query strings instead of slug
[url-deep]                     Path depth > 4 levels for important content
[url-uppercase]                URL has uppercase chars (case-sensitivity issues)
[url-trailing-slash]           Inconsistent trailing-slash treatment

IMAGES
─────────────────────────────────────────────────────────────────────
[image-alt-missing]            <img> without alt attribute
[image-alt-empty-content]      Content image with alt=\"\" (decorative-only OK; flag content imgs)
[image-alt-keyword-stuffing]   alt text repeats target keyword unnaturally
[image-filename-non-descriptive] File named DSC_1234.jpg / image-1.png

AI SEARCH / GENERATIVE ENGINE OPTIMIZATION (GEO)
─────────────────────────────────────────────────────────────────────
[geo-direct-answer-missing]    No 1-paragraph direct answer near top of long-form page
[geo-faq-missing]              Page about a topic has no FAQ section / FAQPage schema
[geo-listicle-no-summary]      Listicle without scannable summary at top
[geo-llms-txt-missing]         Site has no /llms.txt at root
[geo-author-credentials]       AI-search prefers byline + credentials proof
[geo-statistics-missing]       Authoritative content lacks data / citations

CONVERSION-RELEVANT (revenue-tracking sites)
─────────────────────────────────────────────────────────────────────
[cta-missing]                  Page about a product/service with no CTA
[cta-weak]                     CTA text is generic (\"submit\") not specific (\"start free trial\")
[cta-position]                 Primary CTA below the fold on revenue page
[trust-signals-missing]        Conversion page lacks reviews / testimonials / guarantees
[trust-signal-density-thin]    Conversion page has fewer than 3 trust-signal phrases visible (guarantee/warranty/authorized/verified/return-policy/etc.)
[footer-trust-links-missing]   Footer doesn't carry privacy/terms/contact/about/affiliate-disclosure (FTC + E-E-A-T baseline)
[content-freshness-low]        ≥70% of content pages haven't been updated in 90+ days (site-wide signal — stale catalogs lose rank)
[faq-quality-thin]             FAQPage schema present but has fewer than 3 questions or average answer < 20 words (LLM-citation sweet spot is 5 Q × 50-80-word answers)
[featured-product-pdp-improve] On a revenue_focus site, a featured PDP has any of: thin content, missing schema, or low body-internal-link count
[internal-link-to-featured]    Featured product PDP receives no inbound internal links from buying-guide/comparison/category pages on the same site
[h2h-stale-pricing-product]    Product page's visible price is stale relative to DB price (>14 days drift)

LLM-SEARCH READINESS (apply to ALL pages — generative-engine optimization)
─────────────────────────────────────────────────────────────────────
[llm-search-direct-answer-missing]  No extractable single paragraph at top answering the page's primary query
[llm-search-citation-readiness]     Fewer than 3 specific quantitative claims (numbers, dates, comparisons) — LLMs prefer citable facts
[llm-search-quote-extractability]   No memorable single-sentence verdict / conclusion (LLMs love quote-worthy passages)
[llm-search-faq-format]             No Q&A formatted content (FAQ schema or H2/H3 question-form headers)

PAGE-TYPE: RECIPE (only when the input record has type=recipe)
─────────────────────────────────────────────────────────────────────
[recipe-schema-incomplete]          Recipe JSON-LD missing yield, prepTime, cookTime, totalTime, recipeIngredient[], recipeInstructions[], nutrition, image, author, datePublished
[recipe-internal-links-thin]        No links to sibling recipes in same category, no breadcrumb
[recipe-conversational-intro-missing]  No 2-3 sentence intro answering \"what is this dish, why make it\"

PAGE-TYPE: PRODUCT (only when the input record has type=product)
─────────────────────────────────────────────────────────────────────
[product-schema-incomplete]         Product JSON-LD missing brand, mpn/sku, offers.price, offers.priceCurrency, aggregateRating, review[]
[product-comparison-link-missing]   No link to head-to-head pages featuring this product
[product-affiliate-cta-position]    Amazon CTA below the fold (hurts conversion)
[product-pros-cons-missing]         No "Pros & Cons" / "👍 / 👎" block on a product page (LLMs literally extract these for product summaries; studio-supplies emits one on every product)
[product-image-count-thin]          Product page with fewer than 3 product images (Google's product-rich-result minimum)
[product-aggregate-rating-thin]     Product schema with aggregateRating.reviewCount < 5 — Google won't show stars in SERP unless review count crosses a fuzzy threshold
[product-specs-table-missing]       No structured spec table / list on a product page (every studio-supplies product has explicit Specifications section)
[product-affiliate-tag-missing]     Outbound Amazon link without ?tag=<id> on a product page (lost commission)

PAGE-TYPE: HEAD-TO-HEAD / VS (only when type=head_to_head)
─────────────────────────────────────────────────────────────────────
[h2h-comparison-format-readiness]   No spec table, no winner verdict, no buy-advice block
[h2h-quote-worthy-verdict-missing]  No single-sentence \"X wins for Y, Z wins for W\" line
[h2h-stale-pricing]                 Prices in HTML > 14 days old vs DB current price

PAGE-TYPE: ARTICLE / BLOG / REVIEW (only when type=article OR type=review)
─────────────────────────────────────────────────────────────────────
[article-author-credentials-missing]  No author byline with bio link
[article-publish-update-dates]        Missing or future-dated publish/update date
[article-cited-sources-missing]       Fewer than 3 outbound citations
[article-wordcount-schema-missing]    Article JSON-LD lacks wordCount field (signal Google + LLMs use to weight)
[article-datemodified-missing]        Article JSON-LD has datePublished but no dateModified (freshness signal)
[review-template-incomplete]          Review-style article missing canonical sections — methodology / specs / independent-testing / owners-say / strengths / limitations / who-should-buy / alternatives / sources. Studio-supplies' template hits all 8.
[review-buyer-persona-segmentation-missing]  Review without "Who Should Buy It / Who Should Skip" or equivalent buyer-persona segmentation (LLM-search loves this — direct answer to "is this for me?")
[review-specs-source-attribution-missing]    Specs heading without source attribution like "(per <manufacturer> and <test-site>)" — tells LLMs the data has provenance
[review-citations-section-missing]    Review/article without distinct "Sources & Citations" / "References" section with ≥3 outbound links
[eeat-outbound-citation-count]        Review or authoritative content with fewer than 3 outbound links to non-affiliate, non-social authoritative domains
[body-visible-date-missing]           No human-visible publish or update date in body (LLM-search uses these as a freshness signal independent of schema)
[body-internal-links-thin]            Fewer than 5 internal links inside <main>/<article> body (excludes nav + footer). Studio-supplies has 8+ in-content internal links per product page

PAGE-TYPE: FEATURE / APP-PAGE (only when type=feature)
─────────────────────────────────────────────────────────────────────
[feature-conversational-content]    Page is mostly UI, no prose explaining the feature for SEO/LLM
[feature-internal-link-cluster]     Feature page doesn't link to sibling features
═════════════════════════════════════════════════════════════════════

For EACH issue found, return a JSON object:
{
  "check_id": "<one of the bracketed ids above>",
  "url": "<page URL>",
  "severity": "critical | high | medium | low",
  "confidence": 0.0-1.0,
  "title": "one-line headline",
  "rationale": "why this matters for SEO + which Google guideline",
  "evidence": "exact snippet from the page proving the issue",
  "fix": "concrete change — what to add/edit, with example markup if applicable"
}

Return STRICT JSON (an array). No markdown fences, no preamble.

If a page is fully clean for the checks above, do not invent issues.
Empty output is valid.

Calibrate confidence carefully: 0.95+ means another SEO expert would
look at the evidence and immediately agree. 0.5-0.7 means probable but
worth a human verifying. <0.5 means speculation — typically only for
GEO checks where best practices are still emerging.
"""

# Mapping check_id → SEO recommendation type (for downstream
# tooling — implementer template selection, dashboard grouping, etc.)
CHECK_ID_TO_REC_TYPE = {
    # title/meta
    "meta-title-length": "ctr-fix", "meta-title-keyword": "ctr-fix",
    "meta-title-brand": "ctr-fix", "meta-description-length": "ctr-fix",
    "meta-description-cta": "ctr-fix", "meta-description-keyword": "ctr-fix",
    "meta-description-missing": "ctr-fix",
    "meta-title-duplicate": "ctr-fix", "meta-description-duplicate": "ctr-fix",
    # headings/content
    "h1-missing": "ssr-fix", "h1-multiple": "ssr-fix", "h1-mismatch": "ssr-fix",
    "heading-hierarchy": "ssr-fix",
    "content-thin": "content-expansion", "content-keyword-stuffing": "content-expansion",
    # schema
    "schema-product-missing": "schema-markup", "schema-article-missing": "schema-markup",
    "schema-faqpage-missing": "schema-markup", "schema-howto-missing": "schema-markup",
    "schema-breadcrumblist-missing": "schema-markup",
    "schema-organization-missing": "schema-markup",
    "schema-website-searchaction-missing": "schema-markup",
    "schema-incomplete": "schema-markup", "schema-invalid": "schema-markup",
    "schema-deprecated": "schema-markup",
    # eeat
    "eeat-author-missing": "content-expansion", "eeat-author-bio": "content-expansion",
    "eeat-publish-date-missing": "ssr-fix", "eeat-update-date-missing": "ssr-fix",
    "eeat-citations-missing": "content-expansion",
    "eeat-about-missing": "internal-link", "eeat-policy-missing": "internal-link",
    # links
    "link-orphan": "internal-link", "link-anchor-generic": "internal-link",
    "link-anchor-keyword": "internal-link",
    "link-broken": "redirect-fix", "link-redirect-chain": "redirect-fix",
    "link-nofollow-internal": "internal-link",
    # mobile/cwv
    "mobile-viewport-missing": "ssr-fix", "mobile-tap-targets": "ssr-fix",
    "mobile-font-small": "ssr-fix", "mobile-horizontal-scroll": "ssr-fix",
    "cwv-render-blocking": "ssr-fix", "cwv-image-no-dimensions": "ssr-fix",
    "cwv-image-no-lazy": "ssr-fix", "cwv-image-format": "ssr-fix",
    "cwv-font-no-display": "ssr-fix", "cwv-large-dom": "ssr-fix",
    "cwv-ttfb-slow": "ssr-fix", "cwv-ttfb-very-slow": "ssr-fix",
    # indexing
    "indexing-noindex-conflict": "indexing-fix", "indexing-canonical-self": "indexing-fix",
    "indexing-canonical-non-2xx": "indexing-fix", "indexing-sitemap-404": "sitemap-fix",
    "indexing-sitemap-shrank": "sitemap-fix",
    "indexing-robots-blocked": "indexing-fix", "indexing-pagination-rel": "indexing-fix",
    "indexing-soft-404": "indexing-fix",
    "indexing-hreflang-missing": "indexing-fix",
    "indexing-hreflang-asymmetric": "indexing-fix",
    "indexing-breadcrumb-parity": "schema-markup",
    "indexing-itemlist-numberOfItems-missing": "schema-markup",
    # url/images
    "url-non-descriptive": "ssr-fix", "url-deep": "ssr-fix",
    "url-uppercase": "ssr-fix", "url-trailing-slash": "redirect-fix",
    "image-alt-missing": "ssr-fix", "image-alt-empty-content": "ssr-fix",
    "image-alt-keyword-stuffing": "ssr-fix", "image-filename-non-descriptive": "ssr-fix",
    # GEO / AI search
    "geo-direct-answer-missing": "content-expansion",
    "geo-faq-missing": "schema-markup",
    "geo-listicle-no-summary": "content-expansion",
    "geo-llms-txt-missing": "ssr-fix",
    "geo-author-credentials": "content-expansion",
    "geo-statistics-missing": "content-expansion",
    # cta
    "cta-missing": "conversion-path", "cta-weak": "conversion-path",
    "cta-position": "conversion-path", "trust-signals-missing": "conversion-path",
    "trust-signal-density-thin": "conversion-path",
    "footer-trust-links-missing": "conversion-path",
    "content-freshness-low": "content-expansion",
    "faq-quality-thin": "schema-markup",
    "featured-product-pdp-improve": "product-affiliate-cta-position",
    "internal-link-to-featured": "internal-link",
    "h2h-stale-pricing-product": "product-schema-incomplete",
    # LLM-search readiness (apply everywhere)
    "llm-search-direct-answer-missing": "content-expansion",
    "llm-search-citation-readiness": "content-expansion",
    "llm-search-quote-extractability": "content-expansion",
    "llm-search-faq-format": "schema-markup",
    # Page-type checks — rec.type is the check_id itself (open enum)
    "recipe-schema-incomplete": "recipe-schema-incomplete",
    "recipe-internal-links-thin": "recipe-internal-links-thin",
    "recipe-conversational-intro-missing": "recipe-conversational-intro-missing",
    "product-schema-incomplete": "product-schema-incomplete",
    "product-comparison-link-missing": "product-comparison-link-missing",
    "product-affiliate-cta-position": "product-affiliate-cta-position",
    "product-pros-cons-missing": "content-expansion",
    "product-image-count-thin": "schema-markup",
    "product-aggregate-rating-thin": "schema-markup",
    "product-specs-table-missing": "content-expansion",
    "product-affiliate-tag-missing": "conversion-path",
    "h2h-comparison-format-readiness": "h2h-comparison-format-readiness",
    "h2h-quote-worthy-verdict-missing": "h2h-quote-worthy-verdict-missing",
    "h2h-stale-pricing": "h2h-stale-pricing",
    "article-author-credentials-missing": "article-author-credentials-missing",
    "article-publish-update-dates": "article-publish-update-dates",
    "article-cited-sources-missing": "article-cited-sources-missing",
    "article-wordcount-schema-missing": "schema-markup",
    "article-datemodified-missing": "schema-markup",
    "review-template-incomplete": "content-expansion",
    "review-buyer-persona-segmentation-missing": "content-expansion",
    "review-specs-source-attribution-missing": "content-expansion",
    "review-citations-section-missing": "content-expansion",
    "eeat-outbound-citation-count": "content-expansion",
    "body-visible-date-missing": "ssr-fix",
    "body-internal-links-thin": "internal-link",
    "feature-conversational-content": "feature-conversational-content",
    "feature-internal-link-cluster": "feature-internal-link-cluster",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_llm_json(raw: str) -> list[dict]:
    """Tolerant JSON-array parse — strip markdown fences, find brackets."""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    i = s.find("[")
    if i < 0:
        i = s.find("{")
    if i < 0:
        return []
    j = max(s.rfind("]"), s.rfind("}"))
    if j <= i:
        return []
    s = s[i:j + 1]
    try:
        out = json.loads(s)
    except json.JSONDecodeError:
        return []
    if isinstance(out, dict):
        return [out]
    if isinstance(out, list):
        return [x for x in out if isinstance(x, dict)]
    return []


def format_pages_for_audit(pages: list[dict], cap_chars: int = 2000) -> str:
    """Render a small batch of page records for the audit prompt.

    If a page record has a `type` field (set by the page-inventory crawl
    in seo-data-collector), it's included so the LLM can apply the
    page-type-specific checks (recipe-*, product-*, h2h-*, etc.).
    """
    parts = []
    for p in pages:
        body = (p.get("body_text") or p.get("content") or "")[:cap_chars]
        page_type = p.get("type", "")
        type_line = f"TYPE: {page_type}\n" if page_type else ""
        parts.append(
            f"\nURL: {p.get('url','?')}\n"
            f"{type_line}"
            f"TITLE: {p.get('title','')}\n"
            f"DESCRIPTION: {p.get('description','')}\n"
            f"H1: {p.get('h1','')}\n"
            f"CANONICAL: {p.get('canonical','')}\n"
            f"BODY (truncated):\n{body}\n"
            "---"
        )
    return "".join(parts)


def run_llm_audit(
    *,
    pages: list[dict],
    site_label: str,
    primary_objective: str = "top5-rank",
    ai_chat_callable=None,
    batch_size: int = 4,
    adaptive_context: str = "",
    active_goals: list[dict] | None = None,
) -> list[dict]:
    """Send pages through the LLM audit; return raw issue list.

    `ai_chat_callable` is a function `(messages, *, temperature, max_tokens) -> str`.
    If None, returns []. Caller wires this to `framework.core.ai_providers.ai_client_for(...)`.

    Each returned issue has the schema specified in SEO_AUDIT_CHECKLIST.
    The caller is responsible for converting these into the SEO
    recommendation schema (see `issues_to_recommendations`).

    `adaptive_context` (optional) is rendered output of
    `framework.core.goal_changes.adaptive_context_block(...)` — a summary
    of which past recs the user implemented and how each goal's metric
    moved. The LLM uses it to de-prioritize categories that haven't been
    moving the needle and double down on what worked.

    `active_goals` (optional) is the list of long-running goals from
    `agents/<id>/goals/active.json`. Injected so the LLM can prioritize
    findings that advance current goals + suggest goal updates when the
    landscape shifts.
    """
    if ai_chat_callable is None or not pages:
        return []

    out: list[dict] = []
    user_preamble = (
        f"Site: {site_label}\n"
        f"Primary SEO objective: {primary_objective}\n\n"
    )
    if active_goals:
        user_preamble += "ACTIVE GOALS (your recommendations should advance these):\n"
        for g in active_goals:
            metric = g.get("metric") or {}
            user_preamble += (
                f"  - {g.get('id','')}: {g.get('title','')} "
                f"[{metric.get('name','?')}: current={metric.get('current','?')} "
                f"target={metric.get('target','?')}]\n"
            )
        user_preamble += "\n"
    if adaptive_context:
        user_preamble += adaptive_context + "\n\n"

    for i in range(0, len(pages), batch_size):
        batch = pages[i:i + batch_size]
        prompt = user_preamble + "PAGES TO AUDIT:\n" + format_pages_for_audit(batch)
        try:
            raw = ai_chat_callable(
                [
                    {"role": "system", "content": SEO_AUDIT_CHECKLIST},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=3000,
            )
        except Exception as e:
            # Continue with the next batch — partial output is better than none
            out.append({
                "check_id": "llm-audit-error",
                "url": batch[0].get("url", ""),
                "severity": "low",
                "confidence": 0.0,
                "title": "LLM audit batch failed",
                "rationale": str(e)[:300],
                "evidence": "",
                "fix": "Retry next run; check ai-providers config.",
            })
            continue
        parsed = _parse_llm_json(raw)
        # Filter to only the bracketed check_ids we know about
        for issue in parsed:
            cid = issue.get("check_id", "")
            if cid not in CHECK_ID_TO_REC_TYPE and cid != "llm-audit-error":
                # Reject hallucinated check ids — we want the SEO expert
                # to be able to audit a fixed list
                continue
            out.append(issue)

    return out


def issues_to_recommendations(
    issues: list[dict], next_id_fn, *, kind_default: str = "ssr-fix"
) -> list[dict]:
    """Convert raw audit issues to the SEO recommendation schema."""
    recs: list[dict] = []
    for issue in issues:
        check_id = issue.get("check_id", "")
        if check_id == "llm-audit-error":
            continue
        rec_type = CHECK_ID_TO_REC_TYPE.get(check_id, kind_default)
        rec = {
            "id": next_id_fn(),
            "type": rec_type,
            "priority": _severity_to_priority(issue.get("severity", "low")),
            "title": issue.get("title", check_id),
            "rationale": issue.get("rationale", ""),
            "implementation_outline": {
                "notes": issue.get("fix", ""),
            },
            "data_refs": [issue.get("url", "")],
            "implemented": False,
            # LLM-specific tracking so we can audit which checks fired
            "llm_check_id": check_id,
            "llm_confidence": issue.get("confidence", 0),
            "llm_evidence": issue.get("evidence", ""),
        }
        recs.append(rec)
    return recs


def _severity_to_priority(sev: str) -> str:
    return {
        "critical": "critical", "high": "high",
        "medium": "medium", "low": "low",
    }.get(sev.lower(), "medium")


# Useful constants exposed for goals + dashboard
ALL_CHECK_IDS = sorted(CHECK_ID_TO_REC_TYPE.keys())
CHECK_CATEGORIES = {
    "title-meta": [c for c in ALL_CHECK_IDS if c.startswith(("meta-",))],
    "headings-content": [c for c in ALL_CHECK_IDS if c.startswith(("h1-", "heading-", "content-"))],
    "schema": [c for c in ALL_CHECK_IDS if c.startswith("schema-")],
    "eeat": [c for c in ALL_CHECK_IDS if c.startswith("eeat-")],
    "links": [c for c in ALL_CHECK_IDS if c.startswith("link-")],
    "mobile-cwv": [c for c in ALL_CHECK_IDS if c.startswith(("mobile-", "cwv-"))],
    "indexing": [c for c in ALL_CHECK_IDS if c.startswith("indexing-")],
    "url-images": [c for c in ALL_CHECK_IDS if c.startswith(("url-", "image-"))],
    "ai-search-geo": [c for c in ALL_CHECK_IDS if c.startswith("geo-")],
    "conversion": [c for c in ALL_CHECK_IDS if c.startswith(("cta-", "trust-"))],
    "llm-search-readiness": [c for c in ALL_CHECK_IDS if c.startswith("llm-search-")],
    "page-type-recipe": [c for c in ALL_CHECK_IDS if c.startswith("recipe-")],
    "page-type-product": [c for c in ALL_CHECK_IDS if c.startswith("product-")],
    "page-type-h2h": [c for c in ALL_CHECK_IDS if c.startswith("h2h-")],
    "page-type-article": [c for c in ALL_CHECK_IDS if c.startswith("article-")],
    "page-type-feature": [c for c in ALL_CHECK_IDS if c.startswith("feature-")],
    "page-type-review": [c for c in ALL_CHECK_IDS if c.startswith("review-")],
    "body-content-signals": [c for c in ALL_CHECK_IDS if c.startswith("body-")],
}
