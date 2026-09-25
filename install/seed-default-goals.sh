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
# The analyzer now (2026-06-01) loads canonical goals via the framework
# storage backend, merges them into per-run goals.json, and scores any goal
# whose `target_metric` resolves via resolve_metric() against the live
# snapshot. Goals WITHOUT target_metric still surface in the dashboard but
# never get scored. Add target_metric to anything you want auto-tracked.
SEO_GOALS='{"goals":[
  {"id":"goal-impressions-90d","title":"Grow GSC impressions / 90d (top of organic funnel)",
   "description":"Total impressions across all queries in the last 90 days. The top of the organic funnel — every click + conversion downstream depends on this growing. Pre-traffic threshold 100/90d; growth-tier 1k+; mature-tier 10k+.",
   "metric":{"name":"gsc_total_impressions_90d","current":0,"target":10000,"direction":"increase","unit":"impressions","horizon_weeks":12},
   "target_metric":"gsc_90d.total_impressions",
   "directives":["bias toward publishing net-new pages that target real queries","prefer high-volume keyword targets (existing impr > 5) over zero-data speculation"]},
  {"id":"goal-organic-clicks-90d","title":"Generate organic clicks (impressions → traffic)",
   "description":"Total GSC clicks in 90d. The bridge between SERP visibility and on-site engagement. Improvements come from CTR fixes + ranking improvements + new high-intent pages.",
   "metric":{"name":"gsc_total_clicks_90d","current":0,"target":500,"direction":"increase","unit":"clicks","horizon_weeks":16},
   "target_metric":"gsc_90d.total_clicks",
   "directives":["prioritize title/H1 fixes for pages ranking top-10 with 0 clicks","emit article-title-fix recs first"]},
  {"id":"goal-organic-ctr-90d","title":"Improve organic CTR (clicks per impression)",
   "description":"Aggregate GSC CTR. 2% is the minimum healthy baseline for buying-guide / review content; sub-1% indicates a snippet problem.",
   "metric":{"name":"gsc_total_ctr_90d","current":0,"target":0.02,"direction":"increase","unit":"ratio","horizon_weeks":16},
   "target_metric":"gsc_90d.total_ctr",
   "directives":["emit ctr-fix recs for any page ranking ≤ 10 with CTR < 0.5%","port high-CTR ad headlines to organic <title> + H1 when ads-ad-copy data is available"]},
  {"id":"goal-top5-bucket-90d","title":"Grow queries ranking in top-5 (high-CTR tier)",
   "description":"Count of unique queries where GSC reports avg position ≤ 5 in the 90d window. Top-5 ranks deliver ~30-40% CTR vs ~5% for positions 6-10 — highest-leverage ranking metric.",
   "metric":{"name":"gsc_rank_bucket_top5","current":0,"target":50,"direction":"increase","unit":"queries","horizon_weeks":16},
   "target_metric":"gsc_90d.rank_buckets.top5",
   "directives":["emit top5-target-page recs for queries at pos 6-15 with ≥ 5 impr/90d","prefer striking-distance work over deep-tier speculation"]},
  {"id":"goal-pages-indexed-90d","title":"Grow Google-indexed page count",
   "description":"Distinct URLs returning GSC impressions in 90d (proxy for indexed-page count). Sites need >300 indexed pages to enter striking-distance for long-tail terms.",
   "metric":{"name":"gsc_pages_indexed_90d","current":0,"target":800,"direction":"increase","unit":"pages","horizon_weeks":24},
   "target_metric":"gsc_90d.num_pages_indexed",
   "directives":["emit article-orphan-boost recs for any indexed page with 0 internal links","emit new-page-* recs to close content-coverage gaps the analyzer identifies"]},
  {"id":"goal-amazon-clicks-30d","title":"Amazon affiliate click-throughs / 30d (REVENUE GATEWAY)",
   "description":"Outbound clicks to Amazon affiliate links in last 28d (GA4 event count). Primary affiliate revenue path — every click ~$0.05-2.00 commission depending on category + conversion. Zero clicks = either no traffic OR CTAs not visible/clickable.",
   "metric":{"name":"amazon_outbound_clicks_28d","current":0,"target":20,"direction":"increase","unit":"clicks","horizon_weeks":12},
   "target_metric":"revenue_28d.amazon-clicks_event_28d",
   "directives":["emit product-affiliate-tag-missing recs CRITICAL when untagged Amazon links exist","emit conversion-path recs when 30d count is 0 + organic traffic > 100/30d","prefer fixing CTA visibility on top-traffic pages over creating new pages"]},
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

# ── seo-opportunity-agent — aisleprompt-only conversion goals ───────────────
# Site-specific revenue paths only AislePrompt has (Instacart cart creates
# from grocery flow, sign-ups for retention). SpecPicks revenue is
# Amazon-only and already covered by the shared goal-amazon-clicks-30d.
AP_REV_GOALS='{"goals":[
  {"id":"goal-instacart-cart-30d","title":"Instacart cart creates / 30d (PRIMARY REVENUE PATH)",
   "description":"GA4 event count of instacart_cart_create in last 28d. THE primary revenue path for AislePrompt — every cart create is a commissionable conversion (~5-15% on grocery cart value).",
   "metric":{"name":"instacart_cart_creates_28d","current":0,"target":100,"direction":"increase","unit":"events","horizon_weeks":12},
   "target_metric":"revenue_28d.instacart-carts_event_28d",
   "directives":["emit conversion-path recs CRITICAL if 7d count is 0 with non-zero 30d","emit pantry-aware-grocery-list rec when competitor analysis shows pantry-subtract is gating cart-create conversion","prefer CTA-prominence fixes on top-landing recipe pages"]},
  {"id":"goal-sign-ups-30d","title":"User sign-ups / 30d (retention / LTV foundation)",
   "description":"GA4 event count of sign_up_complete in last 28d. Sign-ups drive saved-recipe + saved-cart retention — directly correlated with repeat Instacart conversion.",
   "metric":{"name":"signups_completed_28d","current":0,"target":50,"direction":"increase","unit":"events","horizon_weeks":16},
   "target_metric":"revenue_28d.sign-ups_event_28d",
   "directives":["emit conversion-path rec if sign-up CTA is not on top-landing pages","prefer sign-up CTA placement A/B tests over net-new sign-up flows"]}
]}'
put_goals "aisleprompt-seo-opportunity-agent" "$AP_REV_GOALS"

# ── aisleprompt — AI-visibility + verified-human outcome goals (2026-09-25) ─
# The aisleprompt SEO agent is the one agent that feeds its goals into its
# LLM prompt, and its goal set rewarded emitting recs ("recs per run",
# "geo/cwv/eeat violations = 0 accomplished") while goal-amazon-clicks-30d
# read a GA4 event that reports ~0. Bind goals to what the site measures:
# verified-human Amazon clicks (data_sources.db.human_clicks →
# revenue_28d.amazon-clicks_db_30d), AI referrals and AI answer-time fetches
# (db-queries blocks ai_referrals_30d / ai_live_fetches_30d → db.<block>.<col>
# metrics). Status-only entries retire an existing goal and keep its history
# (framework/core/goals.py init_goals).
AP_AI_GOALS='{"goals":[
  {"id":"goal-amazon-clicks-30d","title":"Verified-human Amazon clicks / 30d (REVENUE)",
   "description":"Amazon affiliate clicks by real people in the last 30 days: kitchen_click_events rows that pass framework/core/human_clicks.py (site bot flag, headless/stale-browser UAs, datacenter IPs, no referer, velocity). Same definition as the site-goals-tracker goal. It used to read the GA4 kitchen_click event, which reported 0-1 while people were clicking.",
   "metric":{"name":"human_amazon_clicks_30d","current":135,"target":200,"direction":"increase","unit":"clicks","horizon_weeks":12},
   "target_metric":"revenue_28d.amazon-clicks_db_30d",
   "directives":["prefer fixes on the pages that already produce human Amazon clicks and on the /blog/best-* guides AI assistants fetch","one clear Amazon link with the affiliate tag and rel=\"sponsored nofollow\" where the reader decides; never show a price that is not fresh"]},
  {"id":"goal-ai-referrals-30d","title":"Visits sent by AI assistants / 30d",
   "description":"Visits referred from ChatGPT, Perplexity, Copilot or Claude in the last 30 days (ai_traffic_log kind=referral; db-queries block ai_referrals_30d).",
   "metric":{"name":"ai_referrals_30d","current":26,"target":75,"direction":"increase","unit":"visits","horizon_weeks":16},
   "target_metric":"db.ai_referrals_30d.last_30d",
   "directives":["work on the pages AI assistants already fetch and cite (homepage, /blog/best-* guides) before net-new pages","state the answer or verdict in the first screen of text; keep structured data honest and matching visible text"]},
  {"id":"goal-ai-fetched-pages-30d","title":"Pages AI assistants fetch while answering / 30d",
   "description":"Distinct pages fetched by ChatGPT-User, Perplexity-User and Claude-User in the last 30 days, spoof-filtered (db-queries block ai_live_fetches_30d). A page an assistant fetches while answering a user is a page it can cite.",
   "metric":{"name":"ai_fetched_pages_30d","current":260,"target":500,"direction":"increase","unit":"pages","horizon_weeks":16},
   "target_metric":"db.ai_live_fetches_30d.distinct_pages_30d",
   "directives":["keep AI-fetched pages fast and indexable (no noindex, no soft-404)","never trade honesty (prices, availability, authorship, testing claims) for visibility"]},
  {"id":"goal-recs-emitted-per-run","status":"abandoned"},
  {"id":"goal-recs-shipped-30d","status":"abandoned"},
  {"id":"goal-schema-coverage","status":"abandoned"},
  {"id":"goal-eeat-baseline","status":"abandoned"},
  {"id":"goal-cwv-pass","status":"abandoned"},
  {"id":"goal-ai-search-readiness","status":"abandoned"},
  {"id":"goal-internal-linking","status":"abandoned"}
]}'
put_goals "aisleprompt-seo-opportunity-agent" "$AP_AI_GOALS"

# aisleprompt PI: the two goals that counted its own output.
AP_PI_RETIRE='{"goals":[
  {"id":"goal-issues-found-per-run","status":"abandoned"},
  {"id":"goal-issues-fixed-30d","status":"abandoned"}
]}'
put_goals "aisleprompt-progressive-improvement-agent" "$AP_PI_RETIRE"

# aisleprompt user-growth-strategist: mirror the site outcomes it steers by
# (agent.py _mirror_site_goals copies the site-goals-tracker's values) instead
# of counting its own memo recs.
AP_GROWTH_GOALS='{"goals":[
  {"id":"goal-amazon-clicks-30d","title":"Verified-human Amazon clicks / 30d",
   "description":"Mirrored each run from aisleprompt-site-goals-tracker goal-amazon-clicks-30d (framework/core/human_clicks.py). The memo is judged by whether this moves.",
   "metric":{"name":"human_amazon_clicks_30d","current":135,"target":200,"direction":"increase","unit":"clicks","horizon_weeks":12},
   "target_metric":"human_amazon_clicks_30d",
   "directives":["every lever names the AI-landed or human-click pages it works on and its expected change in verified-human Amazon clicks"]},
  {"id":"goal-ai-assistant-sessions-30d","title":"AI-assistant sessions / 30d",
   "description":"Mirrored each run from aisleprompt-site-goals-tracker goal-ai-assistant-sessions-30d (GA4 AI Assistant channel).",
   "metric":{"name":"ai_assistant_sessions_30d","current":26,"target":50,"direction":"increase","unit":"sessions","horizon_weeks":12},
   "target_metric":"ai_assistant_sessions_30d",
   "directives":["prefer levers on the pages AI assistants already fetch and refer people from"]},
  {"id":"goal-strategy-recs-per-run","status":"abandoned"},
  {"id":"goal-strategy-recs-implemented-30d","status":"abandoned"}
]}'
put_goals "aisleprompt-user-growth-strategist" "$AP_GROWTH_GOALS"

# ── seo-opportunity-agent — specpicks outcome goals (2026-09-25) ────────────
# SpecPicks has ~0 Google clicks; its measured demand is AI assistants.
# Bind goals to what the site actually measures — AI referral landings
# (data_sources.ai_traffic → db-stats ai_referrals_30d) and verified-human
# Amazon clicks (data_sources.db.human_clicks → amazon_human_clicks_30d) —
# and retire the goals that rewarded emitting recs or read a GA4 event this
# site never sends. Status-only entries retire an existing goal and keep its
# history (framework/core/goals.py init_goals).
SP_SEO_GOALS='{"goals":[
  {"id":"goal-ai-referral-landings-30d","title":"AI-assistant referral landings / 30d",
   "description":"People who reached the site from a link in ChatGPT, Perplexity, Claude, Copilot or Gemini (ai_traffic_log kind=referral, status 200), rolling 30 days. The LLM audit starts from the pages these land on (analyzer.audit_seed_queries).",
   "metric":{"name":"ai_referral_landings_30d","current":121,"target":250,"direction":"increase","unit":"landings","horizon_weeks":12},
   "target_metric":"db.ai_referrals_30d.last_30d",
   "directives":["audit the pages in ai_landed_pages first: make each answer its query in the first screen, load fast, and link the right product","never trade honesty (prices, availability, authorship, testing claims) for visibility"]},
  {"id":"goal-human-amazon-clicks-30d","title":"Verified-human Amazon clicks / 30d",
   "description":"Amazon affiliate click-outs framework/core/human_clicks.py classifies as a person; outbound_clicks is ~99% crawlers, so the raw row count is not the metric. 0 on 2026-09-25.",
   "metric":{"name":"human_amazon_clicks_30d","current":0,"target":30,"direction":"increase","unit":"clicks","horizon_weeks":12},
   "target_metric":"db.amazon_human_clicks_30d.last_30d",
   "directives":["on AI-landed pages make the tagged Amazon link (eBay for pre-2012 hardware) the obvious next step, showing a price only when it is under 24h old"]},
  {"id":"goal-recs-emitted-per-run","status":"abandoned"},
  {"id":"goal-recs-shipped-30d","status":"abandoned"},
  {"id":"goal-amazon-clicks-30d","status":"abandoned"}
]}'
put_goals "specpicks-seo-opportunity-agent" "$SP_SEO_GOALS"

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
