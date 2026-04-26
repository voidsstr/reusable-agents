#!/usr/bin/env bash
# Seed default goals for the framework's well-known agents. Idempotent —
# existing progress is preserved (init_goals merges by goal id).
#
# Usage:
#   FRAMEWORK_API_URL=http://localhost:8093 bash install/seed-default-goals.sh

set -euo pipefail

API="${FRAMEWORK_API_URL:-http://localhost:8090}"
TOKEN="${FRAMEWORK_API_TOKEN:-}"
AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

put_goals() {
    local agent="$1"
    local body="$2"
    curl -fsS -X PUT "$API/api/agents/$agent/goals" \
        -H "Content-Type: application/json" "${AUTH[@]}" \
        -d "$body" > /dev/null
    echo "  ✓ $agent"
}

echo "Seeding goals for framework agents…"

# ── progressive-improvement-agent (per-site) ────────────────────────────────
PI_GOALS='{"goals":[
  {"id":"goal-zero-broken-pages","title":"Drive broken-page count to 0",
   "description":"Every URL on the site returns 2xx with valid HTML. Re-crawls daily; flags + auto-tier-fixes broken routes.",
   "metric":{"name":"broken_pages","current":0,"target":0,"direction":"decrease","unit":"pages","horizon_weeks":4},
   "directives":["flag every non-2xx response as critical","auto-tier any rec with confidence >= 0.95 + severity in {critical,high}"]},
  {"id":"goal-zero-miscategorized-products","title":"Eliminate miscategorized content",
   "description":"Products / articles tagged into wrong categories. Critical for catalog SEO + UX.",
   "metric":{"name":"miscategorized_count","current":0,"target":0,"direction":"decrease","unit":"items","horizon_weeks":8},
   "directives":["check product/article category against title + description + body","cite specific URLs as evidence"]},
  {"id":"goal-zero-duplicate-content","title":"Zero duplicate content across pages",
   "description":"Pages with near-identical titles, descriptions, or body text. Hurts SEO and confuses users.",
   "metric":{"name":"duplicate_groups","current":0,"target":0,"direction":"decrease","unit":"groups","horizon_weeks":8},
   "directives":["dedupe by hash of body_text + title","group near-duplicates and recommend canonical/redirect"]},
  {"id":"goal-content-freshness","title":"Surface stale/outdated content",
   "description":"Content with dates older than 12 months that should refresh, or references to deprecated things.",
   "metric":{"name":"stale_pages","current":0,"target":0,"direction":"decrease","unit":"pages","horizon_weeks":12},
   "directives":["flag any page mentioning years more than 18 months past","prefer modify over skip for content recs"]},
  {"id":"goal-accessibility-baseline","title":"WCAG-AA baseline accessibility",
   "description":"All images have alt text, headings are ordered, forms have labels, links are descriptive.",
   "metric":{"name":"a11y_violations","current":0,"target":0,"direction":"decrease","unit":"issues","horizon_weeks":12},
   "directives":["scan for missing alt= attrs","scan for h1->h3 jumps","scan for unlabeled form fields"]}
]}'
for a in aisleprompt-progressive-improvement-agent specpicks-progressive-improvement-agent; do
    put_goals "$a" "$PI_GOALS"
done

# ── competitor-research-agent (per-site) ────────────────────────────────────
CR_GOALS='{"goals":[
  {"id":"goal-feature-parity","title":"Reach feature parity with top competitors",
   "description":"Catalog every feature competitors have that we don'\''t. Recommend the highest-leverage gaps to close first.",
   "metric":{"name":"parity_gap_count","current":0,"target":0,"direction":"decrease","unit":"features","horizon_weeks":24},
   "directives":["track which competitor recs the user accepts vs skips","de-prioritize categories the user repeatedly skips"]},
  {"id":"goal-unique-advantages","title":"Surface 1+ defensible competitive advantage per quarter",
   "description":"Recommend features no competitor has yet. tier=experimental by default; user can promote to review.",
   "metric":{"name":"unique_advantages_proposed","current":0,"target":4,"direction":"increase","unit":"per year","horizon_weeks":52},
   "directives":["lean into unique-category ideas, not parity","avoid suggesting things 3+ competitors already have"]},
  {"id":"goal-ux-improvements","title":"Steady stream of UX improvements",
   "description":"Onboarding/conversion/retention patterns competitors use that we should adopt.",
   "metric":{"name":"ux_recs_shipped","current":0,"target":12,"direction":"increase","unit":"shipped","horizon_weeks":52},
   "directives":["focus on top-of-funnel UX","prefer mobile-first patterns"]},
  {"id":"goal-competitor-coverage","title":"Cover the relevant competitor set",
   "description":"Configured competitor list reflects the actual market. Re-curate as space evolves.",
   "metric":{"name":"competitors_analyzed","current":0,"target":8,"direction":"increase","unit":"per run","horizon_weeks":4},
   "directives":["log competitors that came up via brainstorm but aren'\''t in seed_domains","flag competitors that 404 or pivot"]}
]}'
for a in aisleprompt-competitor-research-agent specpicks-competitor-research-agent; do
    put_goals "$a" "$CR_GOALS"
done

# ── seo-opportunity-agent ───────────────────────────────────────────────────
SEO_GOALS='{"goals":[
  {"id":"goal-top5-keywords","title":"Rank in top-5 for high-intent keywords",
   "description":"Drive average GSC position to <=5 for the top 20 high-intent queries per site.",
   "metric":{"name":"top5_keyword_count","current":0,"target":20,"direction":"increase","unit":"keywords","horizon_weeks":24},
   "directives":["prioritize queries with 50+ impr/30d AND position 6-15","build a target page if one doesn'\''t exist"]},
  {"id":"goal-monthly-revenue","title":"Grow MoM affiliate revenue by 20% per quarter",
   "description":"Conversion-focused recs (CTAs, internal links, schema) compound traffic into revenue.",
   "metric":{"name":"mom_revenue_growth","current":0,"target":20,"direction":"increase","unit":"%","horizon_weeks":12},
   "directives":["weight conversion-path recs higher when affiliate traffic is flat","cite revenue_28d in the rationale"]},
  {"id":"goal-zero-indexing-issues","title":"Zero indexing issues in GSC",
   "description":"Every URL we want indexed IS indexed. No soft-404s, no canonical conflicts.",
   "metric":{"name":"indexing_issues","current":0,"target":0,"direction":"decrease","unit":"issues","horizon_weeks":4},
   "directives":["surface noindex/canonical conflicts as critical","fix sitemap entries that 404","check the indexing-* checklist (canonical-self, sitemap-404, robots-blocked, soft-404, pagination-rel)"]},
  {"id":"goal-ctr-baseline","title":"Average CTR >=3% across high-impression queries",
   "description":"For queries with 100+ impressions, CTR should be at-or-above industry baseline.",
   "metric":{"name":"avg_ctr_pct","current":0,"target":3,"direction":"increase","unit":"%","horizon_weeks":12},
   "directives":["rewrite titles + descriptions for low-CTR / high-impression queries","use power words + numbers","run the meta-* checks (title length, keyword, brand, description length/CTA)"]},
  {"id":"goal-schema-coverage","title":"Schema.org coverage on every page",
   "description":"Every product page has Product schema, every article has Article, every FAQ has FAQPage, etc. Track count of schema-* findings dropping over time.",
   "metric":{"name":"schema_violations","current":0,"target":0,"direction":"decrease","unit":"issues","horizon_weeks":12},
   "directives":["run the schema-* checks (product, article, faqpage, howto, breadcrumblist, organization, searchaction, incomplete, invalid, deprecated)","prioritize high-traffic pages first"]},
  {"id":"goal-eeat-baseline","title":"E-E-A-T signals on all editorial content",
   "description":"Articles have author byline + bio + publish/update dates + citations. Critical for Google'\''s helpful-content + AI-search ranking.",
   "metric":{"name":"eeat_violations","current":0,"target":0,"direction":"decrease","unit":"issues","horizon_weeks":16},
   "directives":["run eeat-* checks (author-missing, author-bio, publish-date-missing, update-date-missing, citations-missing, about-missing, policy-missing)","cite specific guidelines from Google'\''s Search Quality Rater guidelines"]},
  {"id":"goal-cwv-pass","title":"Core Web Vitals pass on every page",
   "description":"LCP < 2.5s, INP < 200ms, CLS < 0.1. Confirmed CWV ranking factor since 2021; weight has grown.",
   "metric":{"name":"cwv_violations","current":0,"target":0,"direction":"decrease","unit":"issues","horizon_weeks":8},
   "directives":["run cwv-* + mobile-* checks (render-blocking, image dimensions, lazy loading, modern formats, font-display, large DOM, viewport, tap targets, font size)"]},
  {"id":"goal-ai-search-readiness","title":"AI search citation readiness (GEO)",
   "description":"Generative search engines (Perplexity, ChatGPT search, Google AI Overviews) cite authoritative content with clear direct answers + citations + author credentials. AI-search citations are the new top-of-funnel.",
   "metric":{"name":"geo_violations","current":0,"target":0,"direction":"decrease","unit":"issues","horizon_weeks":24},
   "directives":["run geo-* checks (direct-answer-missing, faq-missing, listicle-no-summary, llms-txt-missing, author-credentials, statistics-missing)","add an llms.txt at site root"]},
  {"id":"goal-internal-linking","title":"Internal-link health",
   "description":"No orphan pages, no generic anchor text, no broken/redirect-chained internal links. Compounds topic authority.",
   "metric":{"name":"link_violations","current":0,"target":0,"direction":"decrease","unit":"issues","horizon_weeks":12},
   "directives":["run link-* checks (orphan, anchor-generic, anchor-keyword, broken, redirect-chain, nofollow-internal)"]}
]}'
for a in aisleprompt-seo-opportunity-agent specpicks-seo-opportunity-agent; do
    put_goals "$a" "$SEO_GOALS"
done

# ── responder-agent ─────────────────────────────────────────────────────────
RESP_GOALS='{"goals":[
  {"id":"goal-zero-stuck-replies","title":"Zero unrouted user replies",
   "description":"Every reply to an outbound recs email gets routed within 1 minute.",
   "metric":{"name":"unrouted_replies","current":0,"target":0,"direction":"decrease","unit":"replies","horizon_weeks":4},
   "directives":["log every parse failure with the raw subject","auto-retry transient IMAP errors"]},
  {"id":"goal-fast-routing-latency","title":"Median routing latency <60s",
   "description":"From inbox arrival to dispatch in the target agent'\''s response queue.",
   "metric":{"name":"median_route_latency_s","current":60,"target":60,"direction":"decrease","unit":"seconds","horizon_weeks":4},
   "directives":["measure timestamp delta from email Date header to dispatch ts"]}
]}'
put_goals "responder-agent" "$RESP_GOALS"

echo ""
echo "Done. View at:  http://localhost:8091/agents/<id> (Goals tab)"
