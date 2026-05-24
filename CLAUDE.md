# Claude Instructions — reusable-agents framework

> ## 🎯 NORTH STAR — READ EVERY SESSION 🎯
>
> **Everything we build here exists to drive user usage of the
> configured websites.** Today that's aisleprompt.com and
> specpicks.com; new sites slot in via per-site config. We measure
> success by site-side metrics — DAU, organic clicks, indexed
> pages, conversions, returning users — *not* by code shipped, runs
> completed, or LLM tokens consumed.
>
> **Goals are the prioritization lens, not decoration.** Every agent
> declares 3–7 long-running goals at registration. Each goal has a
> `target_metric` that points at a key in `RunResult.metrics` so the
> framework auto-records progress on every run. The Goals tab in the
> dashboard is the single pane of glass for "is this agent moving the
> needle?"
>
> **The decision procedure for any new feature, refactor, or bugfix:**
>
> 1. Open the Goals tab (or `GET /api/agents/<id>/goals` and
>    `/api/agents/<id>/goals/cache`).
> 2. Find a stalled goal — target gap large, recent progress flat.
>    Bias toward goals that map directly to user-facing site metrics
>    (organic clicks, DAU, conversions, indexed pages).
> 3. Trace that goal back to the agent that owns its `target_metric`.
>    Read its `run()` and find the bottleneck — under-batching,
>    missing handoff, low cap, no LLM short-circuit, etc.
> 4. Make the smallest change that moves the metric. Verify the
>    metric actually moved on the next 1–3 runs before declaring it
>    done. **A change that doesn't tick a goal didn't happen.**
>
> **Anti-patterns to refuse on sight:**
>
> - "Refactor for cleanliness" without a specific goal it unblocks.
> - "Add a feature the user might want" without a metric it serves.
> - "Wire a new agent" before declaring goals + `target_metric`
>   bindings. (No goals → no legibility → won't survive review.)
> - "Fix a bug" by adding an `if site == "x": …` branch in framework
>   code. Lift to config; see the Framework-First Policy below.
>
> See **[`README.md` → "Goals & metrics — the north star"](README.md)**
> for the goals pipeline architecture, layer A vs layer B metric
> capture, and how `init_goals` merges work.

> ## ⚠️ AGENTBASE IS MANDATORY — READ EVERY SESSION ⚠️
>
> **Every registered agent MUST subclass
> `framework.core.agent_base.AgentBase`.** Bash-driven multi-stage
> pipelines, scripts that write `status.json` directly via
> `framework.cli.status`, and ad-hoc shapes (synthesizing run
> summaries from work-product files instead of `progress.json`) are
> forbidden.
>
> **Why this matters:** non-AgentBase agents create silent
> inconsistencies — runs invisible in the dashboard's Runs tab, no
> `run-index.json` for fast list endpoints, no per-run heartbeat,
> no auto goal-progress tracking, no auto-generated verification
> scripts. Past workarounds (e.g. `framework.cli.status` writing a
> synthetic `progress.json` on terminal states; `_list_runs_legacy`
> reading both `progress.json` and `recommendations.json`) were
> retired on 2026-05-04 once every active SEO agent moved to
> AgentBase. Don't reintroduce them.
>
> **Decision tree (apply to EVERY new agent or refactor):**
>
> 1. **"Is this work an agent?"** If it has a cron schedule, run
>    history, status, or queueable triggers → AgentBase. If it's a
>    one-shot CLI tool (e.g. `agent/tools/wake_on_lan.py`) → not an
>    agent, no manifest, doesn't go in `agents/`.
> 2. **"Is it multi-stage?"** Stages are phases inside one agent's
>    `run()` method, NOT separate registered agents. They share one
>    `run_ts`, one `progress.json`, one `run-index` entry. If the
>    cross-stage state is too coarse for one agent, lift the contract
>    into `framework/core/` as a primitive (see
>    `implementation_queue.py`, `digest_queue.py`,
>    `outbound_email.py` for examples).
> 3. **"Does my entry_command invoke `bash`?"** Only acceptable if
>    the bash is a thin env-setup wrapper that ends in
>    `exec python3 .../agent.py` (an AgentBase entrypoint). Bash
>    that orchestrates pipeline stages, writes status, or shells out
>    to multiple python scripts is the anti-pattern this rule bans.
> 4. **"Am I about to write `if has_progress_json else
>    synthesize_from_other_files`?"** STOP. The producer should
>    write `progress.json` via `AgentBase.post_run()` — fix the
>    producer, don't add a synthesis branch in the consumer.
>
> **Reference implementation — collapsed pipeline pattern.** The SEO
> opportunity agent (`agents/seo-opportunity-agent/`) is the canonical
> example of converting a bash-orchestrated pipeline to AgentBase.
> Three former agents (`seo-data-collector`, `seo-analyzer`,
> `seo-reporter`) and a per-site `run.sh` collapsed into one
> `AgentBase` subclass with three internal phases (collect → analyze
> → finalize). Their script bodies became internal modules under
> `agents/seo-opportunity-agent/lib/{collector,analyzer,reporter}/`
> (still subprocess-called by `_run_phase()` to avoid rewriting 7,500
> LOC, but no longer registered as separate agents). Read
> `agents/seo-opportunity-agent/agent.py` + `finalizer.py` before
> refactoring any other bash pipeline.
>
> **Cross-agent contracts live in `framework/core/`.** When agent A's
> output flows to agent B, the file format / queue location belongs
> in a framework primitive, not inline JSON-shape conventions:
>
> - `framework/core/implementation_queue.py` — `queue_recs()` writes
>   `agents/responder-agent/auto-queue/<request-id>.json`
> - `framework/core/digest_queue.py` — `queue()` writes
>   `digest-queue/<ts>-<hash>.json` for the digest rollup
> - `framework/core/outbound_email.py` — `record()` writes
>   `agents/<id>/outbound-emails/<request-id>.json` for the
>   Confirmations page
> - `framework/core/handoff.py` — typed inter-agent handoffs
> - `framework/core/short_circuit.py` — snapshot hashing + replay
>
> AgentBase exposes shorthand wrappers: `self.queue_recs(...)`,
> `self.queue_for_digest(...)`, `self.record_outbound(...)`. Use
> these. If you find yourself writing `self.storage.write_json(
> "agents/responder-agent/auto-queue/...", ...)` inline, you're
> bypassing the contract — call the wrapper.
>
> **Conversion backlog (must convert when next touched).** As of
> 2026-05-04 the following agents still drive work outside AgentBase
> and need to be lifted on the next change. (`responder-agent`,
> `implementer`, `seo-deployer` were converted on 2026-05-04 in the
> same session as the SEO collapse — they're AgentBase wrappers
> around their existing scripts/run.sh now. The implementer's
> `run.sh` is still the heavy-lifter inside; rewriting it is its
> own follow-up.)
>
> - `gsc-coverage-auditor` (and per-site `aisleprompt-` /
>   `specpicks-` instances)
> - `daily-briefing-calendar-agent`, `daily-status-briefing`,
>   `fix-submission-agent`, `retro-agent-orchestrator`
> - `indexnow-submitter` (and per-site instances), `aisleprompt-indexnow-bulk`,
>   `specpicks-indexnow-bulk`
> - `external-game-cataloger`, `game-library-scanner`,
>   `market-research-pipeline`, `real-estate-agent`,
>   `retro-multiplayer-refresh`, `security-scanner-pipeline`
> - `web-search`, `product-hydration-agent`, `scraper-watchdog`
>
> Pick one of these only when its functionality is being changed —
> don't open a side-quest. The framework primitives needed already
> exist; if you find a gap, add the primitive to `framework/core/`
> first, never inline.

> ## ⚠️ FRAMEWORK-FIRST POLICY — READ EVERY SESSION ⚠️
>
> **Every change MUST be evaluated for framework abstraction BEFORE
> writing the first line of code.** Ask:
>
> 1. **"Could another site or agent ever want this?"** If yes → it goes
>    in `framework/core/` or `framework/cli/` and is **configurable per
>    deployment**.
> 2. **"Is the LOGIC site-specific, or only the VALUES?"** Logic →
>    framework with knobs. Values (DSNs, brand voice, slugs) → site repo
>    or per-deployment storage config.
> 3. **"If I deploy this framework somewhere else tomorrow, would they
>    have to fork my changes to get value?"** If yes — STOP, refactor as
>    framework primitive + config now, NOT later.
>
> **Required pattern: primitive + config + extension point.** Every new
> framework feature ships as:
>
> - A reusable primitive in `framework/core/<name>.py` with no
>   site-specific assumptions
> - A storage-backed config (`config/<name>-config.json`) so each
>   deployment can override behavior without code changes
> - A manifest field (added to `framework/core/registry.py`
>   `AgentManifest`) when per-agent override matters
> - A CLI entry point in `framework/cli/<name>.py` when shell-side
>   callers (host-worker, scripts) need it
> - **Default config that ships sensible behavior out of the box** so
>   first-time users don't need to configure anything to get going
>
> **Anti-patterns that MUST be refactored on sight:**
>
> - `if site == "specpicks": ...` in framework code — extract to config
> - Hardcoded agent-id lists in `host-worker.sh`, `responder.py`,
>   anywhere in `framework/` — move to a storage config
> - Duplicate logic in two site-agent `agent.py` files — lift to
>   `framework/core/` and have both call into it
> - Site-specific tier/priority/threshold constants — config-driven
>   per deployment
> - "I'll abstract it later" — there is no later
>
> **Reference implementations of this pattern:**
>
> - [`framework/core/priority.py`](framework/core/priority.py) +
>   [`framework/cli/priority.py`](framework/cli/priority.py) +
>   `config/priority-config.json` — agent queue priority tiers
> - [`framework/core/short_circuit.py`](framework/core/short_circuit.py) —
>   skip-LLM-when-inputs-unchanged primitives
> - [`framework/core/work_types.py`](framework/core/work_types.py) +
>   per-site `handoff_routes` in `site.yaml` — rec routing
> - [`framework/core/article_mentions.py`](framework/core/article_mentions.py) —
>   article-product mention extraction (used by any site)
>
> **When in doubt, build it in the framework.** Cost of unnecessary
> abstraction: small (one extra file). Cost of duplicated site logic:
> drift, fan-out bugs, broken priority across sites.
>
> **The contract with every site (aisleprompt, specpicks, future sites):**
> sites are *config consumers*, never *code-fork targets*. A site repo
> contributes a `manifest.json` + `site.yaml` + (optional) per-site
> `prompts/*.md`. It does NOT contribute logic, branching, or
> "site-specific overrides" embedded in framework code. If a feature
> can't be expressed through (a) a storage config the framework reads,
> (b) a manifest field, or (c) a `site.yaml` knob — STOP and add the
> missing extension point to the framework before writing the feature.
> This applies to EVERY change, not just obviously-shared ones — even
> a fix for one site goes through the same gate. No site has special
> status; aisleprompt and specpicks are reference deployments, not
> privileged.

## ⚠️ ARTICLE AUTHORING — TIMELY > EVERGREEN — READ EVERY SESSION ⚠️

**Articles must hook into what's happening NOW** — seasonal moments,
holidays, trending searches — not just evergreen "Best X Guide" pages.
A `seasonal-occasion` bucket without a Memorial Day cookout guide on
Memorial Day weekend is a failure even if 50 other articles shipped
that week. Same for trending-ai on a CES launch day, or news on a
major model release. Three framework primitives back this contract:

1. **`framework/core/seasonal_calendar.py`** — anchored US calendar
   covering fixed-date (Cinco de Mayo, July 4th) AND relative-date
   (`last-weekday:monday:5` = Memorial Day, `nth-weekday:4:thursday:11`
   = Thanksgiving) holidays. Three windows: **NOW** (today ± 3d),
   **IMMINENT** (4–14d out), **UPCOMING** (15–60d out). Each occasion
   carries `recipe_keywords`, `link_categories`, `bucket_hints`,
   `audience` (food / tech / general). Article-author agents call
   `active_signal(today, audience=...)` and inject
   `build_prompt_block(...)` into the LLM user-message so the model
   sees "🔥 SHIP NOW — Memorial Day Weekend in 2d." Without this block
   the LLM defaults to evergreen titles.

2. **`framework/core/trends_signal.py`** — Google Trends RSS +
   audience-appropriate Reddit RSS (`r/cooking` for food, `r/buildapc`
   for tech, `r/gaming` for gaming). Cached 6h per agent at
   `agents/<id>/state/trends-cache.json` to avoid pounding the RSS
   feeds. Article-author agents call `fetch_trends_cached(self.storage,
   self.agent_id, audience=...)` and the `TRENDING TODAY` block in the
   prompt biases new proposals toward search demand the catalog
   doesn't yet cover.

3. **`framework/core/featured_rotation.py`** — promotes
   `editorial_articles.featured = true` for any article tagged
   `holiday:<id>` when that holiday is NOW or IMMINENT in the
   seasonal calendar. Demotes stale holiday features (so January's
   Christmas piece doesn't stay featured in May). Operator-pinned
   features (featured=true with no `holiday:` tag) are left alone.
   Runs at the end of each article-author tick via
   `cycle_homepage_features(self._db_dsn, audience=..., max_features=N)`.

4. **`framework/core/article_link_guard.py`** — enforces that every
   shipped article contains ≥5 inline `/recipes/<slug>` links and ≥2
   `/k/<slug>` links (aisleprompt defaults; specpicks is 0/0 since
   PDP recs use `/product/`). Hooked into the implementer's
   `run.sh` post-write step — articles failing the link contract are
   skipped from INSERT and re-queued with a failure addendum that
   tells the LLM exactly which slugs to add. Articles without inline
   links are content dead-ends — no internal-link equity, no
   on-session expansion, no conversion path.

**Hard rules for any new article-author / news-author / editorial
proposer:**

- ❌ Don't roll your own holiday list per agent — call
  `seasonal_calendar.active_signal()`.
- ❌ Don't roll your own RSS scraper per agent — call
  `trends_signal.fetch_trends_cached()`.
- ❌ Don't ship the proposer without a `holiday:<id>` tag rule in
  its system prompt — without it `featured_rotation` has nothing to
  cycle.
- ❌ Don't accept LLM output that omits `expected_recipe_slugs` /
  `expected_kitchen_slugs` — the implementer's link-guard will
  reject the article anyway.
- ❌ Don't loosen the link-guard minima per-site without a real
  reason. Higher floor = better SEO + revenue path.

**Article-author dedup tuning** — the title-similarity dedup *must*
exempt holiday-bearing proposals (memorial, july, thanksgiving, …) or
seasonal titles get killed as "near-dup of existing meal-prep
article" because they share the `Recipes + Shopping List` suffix
pattern. Aisleprompt's `_dedup_proposals_by_title` has the holiday
exemption; copy that pattern when adding new bucket types.

**SSR markdown renderers MUST support GFM tables.** Per-site SSR has
its own `mdToHtml`-style function. If a site's renderer only handles
headings + lists + paragraphs, every `| header | header |` table in
shipped articles renders as `<p>literal pipes</p>` — visually broken,
breaks AggregateRating + the comparison-table content the SEO audit
expects. Aisleprompt's `simple-server.ts:mdToHtml` shipped without
table support until 2026-05-24; the live `best-grilling-tools` page
was the trigger. Specpicks's `ssrRender.ts:mdToHtml` already had it.
**On every new site onboarding, render a test article with a table and
verify `<table>` appears in the SSR output before declaring done.**
Reference impl: both sites now emit `<table class="md-table">` /
`<table class="ssr-table">` with `<thead>` + `<tbody>`.

**Article hero images — `article-hero-image-curator` is the safety net.**
The article-author picks a hero image at proposal time via SearxNG
(`_search_blog_image`), but that call is best-effort — when the query
returns nothing or returns a junk image (e.g. `medbay.jpeg` for a
"grilling tools" article), the article still ships with `NULL`
hero_image_url. The `article-hero-image-curator` agent runs every
15 min, vision-verifies + replaces failed images, and mirrors winners
to Azure Blob Storage so link-rot from upstream CDNs doesn't leave the
article hero-less. Two requirements:
- Reject patterns must include `medbay`, `scifi`, `spaceship`,
  `medical`, `hospital`, NSFW words (`porn`, `xxx`, `nsfw`, `sexy`,
  `lingerie`, `nudity`, `erotic`, `fetish`), and people patterns
  (`headshot`, `portrait`, `staff`, `bio`, `profile`).
- Minimum size 50KB — anything smaller is a thumbnail / spinner /
  icon. `tiny` images render at 80×80 thumb on the homepage rail and
  look broken.
- See `framework/agents/aisleprompt/article-hero-image-curator/` for
  the SearxNG query + score chain. Bulk-fix utility:
  `/tmp/reimage-bulk.py --site <site> --table editorial_articles
  --include broken,tiny --commit`.

## ⚠️ RETRO / PRE-2012 HARDWARE → EBAY, NOT AMAZON — READ EVERY SESSION ⚠️

**Hardware released before 2012 must default to eBay buy-links, not
Amazon.** Amazon doesn't stock active listings for 20+ year-old SKUs
(GeForce 4 Ti 4600, Voodoo3, Pentium 4, Audigy 2 ZS, etc.); the used /
refurbished retro market lives on eBay. Surfacing "Buy on Amazon →"
for a 2002 GPU sends users to a dead page; the affiliate click earns
nothing and the user-trust hit is severe.

**Decision rule applied EVERYWHERE that a hardware buy-CTA renders:**

1. **Is `release_year < 2012` OR `era == 'retro'` OR slug indicates retro
   (`retro-*`, `voodoo*`, `geforce-[1-4]-*`, `pentium-*`, `audigy-*`,
   `radeon-[1-9]xxx`, etc.)?**
   → eBay search CTA via `ebay_search_url`, NEVER Amazon.

2. **Else if active Amazon listing exists (`is_active=true`, `price
   IS NOT NULL`)?**
   → Amazon affiliate CTA with `tag=specpicks-20`.

3. **Else if hardware is modern but temporarily out of stock?**
   → "Check Amazon for current price" with affiliate link (still earns
     commission on the click-through even if Amazon shows OOS).

**Where this rule must be enforced:**

- `/benchmarks/<slug>` hero CTA + FAQ "Where can I buy" answer
- `/product/<ASIN>` hero CTA (when Amazon has no live price + the
  product matches retro-era patterns)
- `/category/retro-*` editor's top pick + product cards
- `/buying-guide/retro-*` per-pick buy buttons
- Article body cross-links (when the article-author injects a product
  link to a retro SKU)
- Homepage Editor's Picks rail (no retro hardware should appear in
  the modern picks; if it does, route the buy button to eBay)

**Why this matters operationally:**

- **Revenue**: Amazon links for unavailable products = $0 commission.
  eBay's Partner Network pays commission on retro buys; we already
  have the affiliate setup.
- **User trust**: Hitting a "Buy on Amazon" CTA that lands on an empty
  Amazon search or 404 reads as a broken site.
- **SEO**: Google's product rich-result validator flags Offer JSON-LD
  pointing at unavailable products as "deceptive structured data" —
  same manual-action class as misleading prices.

**Agent enforcement:**

- `specpicks-product-hydration-agent` populates `ebay_search_url` on
  every retro product at ingest time; verify it's running:
  `systemctl --user list-timers | grep ebay-product-sync`.
- `specpicks-progressive-improvement-agent` has a `retro-hardware-no-
  ebay-cta` detection rule (added 2026-05-22) that flags any
  retro-era page rendering an Amazon CTA without an eBay alternative.
- New agents/code surfaces that render hardware buy-CTAs MUST consult
  `release_year` + `era` + slug pattern before choosing the link.

**Anti-patterns to refuse on sight:**

- Hardcoded `${SITE_DOMAIN}/dp/${asin}` Amazon URL on any benchmark /
  category / product surface without a release-year guard.
- "Buy on Amazon" CTA copy on a page with `release_year < 2012`.
- Article body autolink injecting a /product/<ASIN> link for retro
  hardware that resolves to a stub PDP with no price (route to eBay
  search by hardware name instead).

## ⚠️ PRICING INTEGRITY — NEVER PROMOTE PRODUCTS WITH INVALID PRICE ⚠️

**The site must never crown a Top Pick or buying-guide pick with a
broken or misleading price.** Audit-found incidents:

- Indian-marketplace (INR) listing showing "$659.00" on the CTA
  button while the page header said "₹659.00" — user thought USD
  until checkout.
- $0 / null-price products surfacing as Editor's Top Pick — page
  looks broken.
- $0.01 / $0.02 prices on Atari 2600 games (scraper cents/dollars
  bug).
- "Save -$50" negative discounts (original_price < price corruption).
- Foreign-marketplace listings (CAD/GBP/EUR/INR/AUD/SGD) shown in
  US-affiliate hubs — non-actionable for US-targeted users.

**The mandatory filters at every visible-pick query layer:**

```sql
WHERE p.is_active = true
  AND p.price IS NOT NULL AND p.price >= 1.0        -- no null/$0/sub-$1
  AND (p.currency IS NULL OR p.currency = 'USD')    -- US-marketplace only
  AND (p.original_price IS NULL OR p.original_price >= p.price)  -- no neg-discount
  AND COALESCE(p.category_confidence, 0) >= 0.5     -- categorically-correct
```

Already enforced in `buyingGuideFallback` + `/api/categories` LATERAL
hero. Apply the same in any NEW query that picks a "visible Top Pick"
across categories.

**Currency display at render time** — `priceLabel()` and `priceHtml()`
in `src/services/ssrRender.ts` carry a per-currency symbol map
(USD=$, CAD=CA$, GBP=£, EUR=€, JPY=¥, INR=₹, SGD=S$, BRL=R$, MXN=MX$).
Use these helpers — never hardcode `$${price.toFixed(2)}` in a CTA
or sticky-bar render. Audit will catch hardcoded `$` prefix on
non-USD products.

**Bulk catalog hygiene patterns (reuse via SQL when issues accumulate):**

```sql
-- Demote foreign-marketplace products (deactivate or mark for cleanup)
UPDATE products SET is_active = false,
       quality_flags = COALESCE(quality_flags,'[]'::jsonb)
                     || '["foreign-marketplace-YYYY-MM-DD"]'::jsonb
 WHERE is_active = true AND currency IS NOT NULL AND currency != 'USD';

-- Demote null/$0/sub-$1 priced products from Top-Pick surfaces
UPDATE products SET category_confidence = 0.3
 WHERE is_active = true
   AND (price IS NULL OR price < 1.0)
   AND COALESCE(category_confidence, 1.0) >= 0.5;

-- Clear negative-discount data corruption
UPDATE products SET category_confidence = 0.3, original_price = NULL
 WHERE original_price IS NOT NULL AND price IS NOT NULL
   AND original_price < price;

-- Clear sub-50¢ scraper bugs (cents/dollars confusion)
UPDATE products SET category_confidence = 0.3, price = NULL
 WHERE is_active = true AND price < 0.50 AND price > 0;
```

## ⚠️ EBAY API IS WIRED — USE IT FOR RETRO/PRE-2012 LISTINGS ⚠️

**The eBay developer credentials are present and the eBay sync agent
is running.** Don't tell the user "we'd need eBay API access" — we have
it. Use it.

**Credentials location:**
- `~/.reusable-agents/secrets.env` (loaded by host-worker systemd
  service via `EnvironmentFile`)
- `<specpicks-repo>/agents/ebay-product-sync-agent/.env` (gitignored;
  loaded by the ingestion agent at startup via
  `EBAY_PRODUCT_SYNC_CONFIG=...site.yaml`)
- Env vars exposed: `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`,
  `EBAY_DEV_ID`, `EBAY_ENV` (production/sandbox),
  `EBAY_MARKETPLACE_ID` (default EBAY_US), optionally
  `EBAY_CAMPAIGN_ID` for affiliate attribution
- OAuth: `framework/agents/ebay-product-sync-agent/ebay_client.py`
  handles the `client_credentials` grant against
  `https://api.ebay.com/identity/v1/oauth2/token` and caches tokens
- Browse API base: `https://api.ebay.com/buy/browse/v1`

**The agent:** `specpicks-ebay-product-sync-agent` (systemd timer,
runs at `:30` past every hour). Ingests fresh listings for 16 retro
PC hardware categories + retro consoles, handhelds, cartridges,
controllers, emulation hardware, CRT/upscalers. Inserts as
`asin = 'EBAY_<item_id>'` so they don't collide with Amazon ASINs.

**DB markers for retro products (the contract):**
- `era = 'retro'` — set on all products in `retro-*` categories +
  any product linked to `hardware_specs` with `release_year < 2012`
- `listing_preference = 'ebay'` — primary buy channel
- `ebay_search_url` — fallback search URL (auto-backfilled from title
  via `regexp_replace(title, '[^A-Za-z0-9+]', '', 'g')` when null)
- `ebay_url` — direct item URL when known (populated by the sync agent)

**Periodic bulk SQL to maintain the era/preference flags as new
products land in retro categories:**

```sql
UPDATE products SET era='retro', listing_preference='ebay'
 WHERE category_id IN (SELECT id FROM categories WHERE slug LIKE 'retro-%' OR slug='handheld-consoles')
   AND is_active=true
   AND (era IS NULL OR listing_preference IS NULL);

UPDATE products SET ebay_search_url =
       'https://www.ebay.com/sch/i.html?_nkw='
       || regexp_replace(replace(title, ' ', '+'), '[^A-Za-z0-9+]', '', 'g')
       || '&_sacat=27386'
 WHERE era='retro' AND ebay_search_url IS NULL AND title IS NOT NULL;
```

**Render-time routing — every hardware-buy CTA across SSR:**
1. `era='retro'` OR `listing_preference='ebay'` OR linked-hardware
   `release_year<2012` → eBay-primary CTA (uses `ebay_url` ||
   `ebay_search_url` || synthesized search by title)
2. Else if Amazon listing has valid USD price → Amazon-primary
3. Else → "Check Amazon for current price" affiliate link

Applied in: PDP hero (`productBody`), benchmark page hero
(`benchmarkHardwareBody`), vertical landing picks (`verticalBody`
`ssr-vpick-ctas`), article body cross-sell cards
(`testbenchArticleBody`'s inline-cross-sell strip).

## What this repo is

A self-hostable framework for running scheduled / triggered LLM agents.
Agents register from their own home repos (nsc-assistant, specpicks,
customer apps, anywhere) and inherit lifecycle, scheduling, status,
inter-agent messaging, confirmations, and a control UI from this
codebase.

**Documentation:**

- [`README.md`](README.md) — overview + quickstart + manifest format
- [`docs/architecture.md`](docs/architecture.md) — three-tier model
  (customer repos → framework → blob storage), agent lifecycle,
  storage layout, dispatch graph, end-to-end trace of one rec
- [`install/glitchtip/README.md`](install/glitchtip/README.md) —
  optional self-hosted error tracker (Sentry-API-compatible) + the
  `crash-watcher-agent` companion that closes the crash → fix → ship
  loop. Includes the mobile-SDK wiring checklist with aisleprompt as
  the worked example.
- [`docs/agents-catalog.md`](docs/agents-catalog.md) — every agent across
  every repo, categorized, with code path + manifest path + schedule
- [`docs/repo-boundaries.md`](docs/repo-boundaries.md) — **read this
  before adding code anywhere**: what goes in framework vs customer
  repo vs `~/.reusable-agents/`. Decision tree + anti-patterns
- [`framework/core/handoff.py`](framework/core/handoff.py) +
  [`framework/core/work_types.py`](framework/core/work_types.py) —
  inter-agent handoff protocol. When agent A finds work that belongs
  to agent B, A calls `send_handoff()` and the framework routes it.
  See `docs/architecture.md` § "Inter-agent handoffs" for the full
  flow. **Whenever you add a new rec_type, also add it to
  `DEFAULT_REC_ROUTING` in work_types.py.**
- [`docs/seo-onboard-new-site.md`](docs/seo-onboard-new-site.md) —
  add a new site to SEO automation in 5 steps
- [`blueprints/README.md`](blueprints/README.md) — pick the right
  blueprint when scaffolding a new agent

Read `architecture.md` once. Re-read `repo-boundaries.md` every time
you wonder "should this code live here or in the customer repo?"

## When the user asks you to create a new agent (in ANY repo)

**Always start from a blueprint.** Pick the closest one and use the
scaffold script — don't hand-roll agent files. The blueprint + scaffold
enforce conventions (kebab-case ids, manifest schema, AGENT.md section
structure, AgentBase lifecycle, capabilities declared, status/decision
protocol, confirmation gates).

### Pick a blueprint

| User's intent | Blueprint | Reference impl |
|---|---|---|
| "audit my site for issues / send me recommendations / put fixes on auto-pilot" | `site-quality-recommender` | `progressive-improvement-agent` |
| "compare my site to competitors / find feature gaps" | `site-quality-recommender` | `competitor-research-agent` |
| "one step in a pipeline (collect / analyze / report / ship)" | `pipeline-stage` | `seo-data-collector`, `seo-analyzer` |
| "poll an inbox / route replies" | `inbox-poller` | `responder-agent` |
| "apply approved recs as code edits" | `llm-code-editor` | `implementer` |
| "just run a script on a cron" | `scheduled-task` | most agents in `nsc-assistant/agents/` |

Each blueprint's `BLUEPRINT.md` has anti-patterns + integration points.
Read it before scaffolding.

### Step-by-step

1. **Confirm the agent's home repo** with the user. The agent dir lives
   inside that repo (typically under `<repo>/agents/<id>/`). If the
   repo doesn't have an `agents/` dir yet, the script creates it.

2. **Confirm core fields**:
   - `id` (kebab-case, ≤63 chars, starts with a letter)
   - One-line description
   - Category (`seo`, `research`, `fleet`, `personal`, `ops`, `misc`)
   - Schedule (cron expression — empty for manual-only agents)
   - Owner email (gets confirmation requests for dangerous actions)
   - Kind: `python` (subclass `AgentBase`) or `bash` (entry script only)

3. **Run the scaffold**:
   ```bash
   bash /home/voidsstr/development/reusable-agents/install/create-agent.sh \
       <agent-id> <repo>/agents \
       --name "<Display Name>" \
       --description "<one line>" \
       --category <category> \
       --cron "<cron-expr>" \
       --timezone "<tz>" \
       --owner "<email>" \
       --kind <python|bash>
   ```
   The script copies `_template/agent/*` into the new dir, substitutes
   placeholders, and prints next steps.

4. **Edit the generated files** to fill in the agent's actual work:
   - `AGENT.md` — flesh out *What this agent does*, *Per-run flow*, etc.
   - `agent.py` (Python) — implement `run()`. Use `self.status`,
     `self.decide`, and the `@requires_confirmation` decorator on any
     dangerous method.
   - `run.sh` (bash) — implement the work between the marker comments.
   - `manifest.json` — only edit if user wants different cron / category
     than what was passed to the scaffold.

5. **Declare goals** (REQUIRED). Every agent in the framework MUST
   declare 3-7 long-running goals it incrementally advances each run.
   Goals stick around forever; the dashboard shows them in the Goals
   tab with progress bars and a separate Accomplished section. Each
   goal has:
   - `id` (kebab-case, stable, never reused)
   - `title` + `description`
   - `metric: {name, current, target, direction, unit}` (optional but
     strongly encouraged — drives the progress bars)
   - `directives: list[str]` (what the agent should DO each run to
     advance this goal — read by the agent's LLM at run start to bias
     analysis)

   Add the seed call to `install/seed-default-goals.sh` (one section per
   agent) so re-runs don't wipe progress, OR PUT directly to
   `/api/agents/<id>/goals` after registration. Schema:
   `shared/schemas/agent-goals.schema.json`. The `run()` should call
   `record_goal_progress(agent_id, goal_id, value, run_ts=...)` at the
   end to update metrics + push progress_history.

   Without goals the agent's purpose isn't legible to the human reviewing
   the dashboard. Don't ship without them.

6. **Register with the framework**:
   ```bash
   FRAMEWORK_API_URL=http://localhost:8093 \
       bash <repo>/agents/register-with-framework.sh
   # Or use the framework's universal walker:
   FRAMEWORK_API_URL=http://localhost:8093 \
       bash /home/voidsstr/development/reusable-agents/install/register-all-from-dir.sh <repo>/agents
   ```
   Idempotent — registration upserts an existing record + re-applies
   the systemd timer. (Use port 8093 because 8090 is held by
   application-research on this dev box.)

7. **Verify**:
   - UI: http://localhost:8091/agents/<agent-id>
   - API: `curl http://localhost:8090/api/agents/<agent-id>`
   - systemd: `systemctl --user list-timers | grep agent-<agent-id>`
   - Goals tab shows the declared objectives with progress bars

8. **Commit** the new agent dir to its home repo.

### Repos in this ecosystem

| Repo | Where agents live | How to register |
|---|---|---|
| `nsc-assistant` | `agents/<id>/` | `bash scripts/register-agents.sh` |
| `specpicks` | `agents/<id>/` | `bash agents/register-with-framework.sh` |
| Customer apps inside nsc-assistant | `Customer Applications/<app>/<ver>/agents/<id>/` (when domain-specific) OR `nsc-assistant/agents/<id>/` (when shared) | nsc-assistant's register-agents.sh |

### When NOT to use the scaffold

- The user explicitly asks for a one-off / throwaway script that doesn't
  need scheduling, status, or registration. Then just write a normal
  bash/python script.
- The user is editing an existing agent — go straight to the AGENT.md
  + agent.py + manifest.json files; re-register after.

### Anti-patterns to avoid

- ❌ Hand-creating `agents/<id>/manifest.json` without using the scaffold
  (you'll miss conventions / metadata fields).
- ❌ Putting agent definitions in `~/.openclaw/...` or `/tmp/...`. Agents
  belong in their home repo, under version control.
- ❌ Hardcoding production credentials in the agent dir. Secrets live in
  the host's OS env, in `~/.<vendor>/` config files (mode 0600), or in
  Azure Key Vault references — *never* in the manifest or runbook.
- ❌ Skipping `@requires_confirmation` on production-affecting methods.
  Add the decorator, declare it in `capabilities` with
  `confirmation_required=True`, let the framework gate it.
- ❌ Calling out to the legacy `agents.lib.agent_recorder` (HTTP
  recorder living in nsc-assistant). New agents use AgentBase, which
  writes status + runs to the framework's storage backend directly.

### Outbound-email recipient policy (HARD RULE)

Every agent that sends operator email — comp-research, SEO opportunity,
PI, article-author, h2h, catalog-audit, deployer alerts, completion
emails — uses ONE address pair across the whole fleet:

```yaml
reporter:
  email:
    to: [mperry@northernsoftwareconsulting.com]
    from: automation@northernsoftwareconsulting.com
    msmtp_account: automation
```

- ❌ Do NOT add personal addresses (`perrymb@gmail.com`, etc.) to any
  agent's `reporter.email.to` list, even if the user mentions a personal
  email in conversation. The owner field on the manifest is for ownership
  attribution; outbound mail still goes to `mperry@nsc...com` only.
- ❌ Do NOT use a different `from:` (e.g. `perrymb@...`) — msmtp's
  `automation` account is the only one configured to relay through the
  framework host. Other senders fail silently.
- ✅ When you scaffold a new email-sending agent, copy the block above
  verbatim. Don't ask the user for an inbox.

This rule was set 2026-05-07 after a recipient-list expansion got
reverted — keep it tight to one canonical operator inbox.

## Implementer path-scope — keep agents in their lane

> ## ⚠️ THE IMPLEMENTER WILL ROAM IF YOU LET IT — READ EVERY SESSION ⚠️
>
> The implementer agent runs aider / claude-cli / copilot against the
> entire site repo. Without an explicit path-scope policy in the site's
> `site.yaml`, the LLM will follow whatever loose interpretation of the
> rec it has, drift into unrelated areas, and commit. Historical hit:
> SEO recs ("zero Instacart clicks last 30d — investigate funnel")
> caused the implementer to rewrite the AislePrompt **mobile** app
> (220-line ScreenHeader.tsx, 223-line auth.ts refactor) because the
> rec had no concrete `target_files` and aider chose what to "improve."
>
> **Every per-site agent's `site.yaml` MUST declare an implementer
> path-scope.** Block format:
>
> ```yaml
> implementer:
>   agent_id: seo-implementer
>   repo_path: /home/voidsstr/development/<site>
>   branch: master
>   allowed_paths:
>     - "src/**"
>     - "frontend/**"
>     - "db/migrations/**"
>     - "scripts/**"
>     - "changes/**"
>     - "*.md"
>   excluded_paths:
>     - "mobile/**"
>     - "ios-extensions/**"
>     - "android/**"
>   post_apply:
>     kick_mobile_build: false
>     kick_backend_deploy: true
>   # Per-dispatch-kind overrides. Different agents have different
>   # legitimate scopes — the crash-watcher needs mobile/, SEO must
>   # not. The implementer reads dispatch_kind from the dispatching
>   # agent and applies the matching block instead of the default.
>   scope_by_dispatch_kind:
>     crash-fix:                             # crash-watcher-agent
>       allowed_paths:
>         - "src/**"
>         - "frontend/**"
>         - "mobile/**"
>         - "ios-extensions/**"
>         - "android/**"
>       excluded_paths: []
>       post_apply:
>         kick_mobile_build: true   # auto-kick EAS on mobile fix
>         kick_backend_deploy: true
> ```
>
> **Two enforcement checkpoints:**
>
> 1. **Pre-LLM** (in `agents/implementer/build-aider-invocation.py`) —
>    any rec whose `target_files` fall outside `allowed_paths` (or hit
>    `excluded_paths`) is deferred with reason
>    `out-of-scope per site policy` and never reaches the LLM.
> 2. **Post-LLM** (in `agents/implementer/run.sh` just before
>    `git add`) — after aider finishes, every newly-touched file is
>    filtered again. Files that violate scope are `git checkout`-ed (or
>    deleted if newly created) and dropped from the commit. Catches
>    LLM drift where an in-scope rec edits an out-of-scope file as a
>    side effect.
>
> Primitive: [`framework/core/implementer_scope.py`](framework/core/implementer_scope.py).
> `ScopePolicy.from_site_config(cfg, dispatch_kind=...)` — when
> `dispatch_kind` matches a key under `scope_by_dispatch_kind`, that
> block REPLACES (not merges with) the default. fnmatch glob syntax, `**`
> matches any number of segments.
>
> **`post_apply` hooks** (all default `true` for back-compat):
> - `kick_mobile_build` — set `false` to refuse triggering EAS builds
>   even if mobile/ paths slipped through.
> - `kick_backend_deploy` — set `false` to require manual
>   `install/deploy-azure.sh` after backend edits.
>
> **Anti-patterns to refuse on sight:**
>
> - A new per-site agent's `site.yaml` ships without an `allowed_paths`
>   block. Default behavior (no policy) is "implementer may touch
>   anything." That's the bug that caused the mobile drift.
> - Adding a path to `allowed_paths` because "this one rec needs it"
>   without considering what else aider might do with that permission.
> - Site-specific `if site == "x"` branches inside `implementer_scope.py`.
>   The policy is data — the primitive stays generic.
>
> Schema: [`shared/schemas/site-quality-config.schema.json`](shared/schemas/site-quality-config.schema.json)
> → `implementer.{allowed_paths, excluded_paths, post_apply}`.

## Site-name literals in framework code — disallowed

The framework must not name specific sites (`aisleprompt`, `specpicks`,
…) anywhere in `framework/` or shipped agents. Per-site behavior flows
through three extension points:

1. **Storage configs** (`config/<feature>-config.json` in the storage
   backend) for cross-cutting overrides like priority tiers.
2. **`manifest.depends_on`** for per-agent graph edges.
3. **Wildcard templates** (`*-progressive-improvement-agent`,
   `*-competitor-research-agent`, …) in `framework/core/dependencies.py`
   that expand at query time against the registered-agents list.

If you find yourself typing `"aisleprompt-..."` in `framework/`, stop
and write a template or config knob instead. There's a regression test
([`framework/tests/test_dependencies.py::test_no_site_specific_literals_in_default_edges`](framework/tests/test_dependencies.py))
that fails CI if any site name leaks back in.

## When the user asks to edit an existing agent's behavior

1. Find the agent's home repo (check the registry: `curl
   http://localhost:8090/api/agents/<id>`; the `repo_dir` field tells
   you where it lives).
2. Edit `AGENT.md` (runbook) and/or `agent.py` (code) in that repo.
3. Re-register if `manifest.json` changed (cron, owner, category, etc.):
   ```bash
   bash <home-repo>/agents/register-with-framework.sh
   ```
4. Manifest changes that affect scheduling auto-update the systemd
   timer.
5. Trigger a fresh run if needed:
   ```bash
   curl -X POST http://localhost:8090/api/agents/<id>/trigger \
        -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"
   ```

## When the user asks to edit a per-site `site.yaml` (SEO + revenue agents)

Per-site SEO instances (`specpicks-seo-opportunity-agent`,
`aisleprompt-seo-opportunity-agent`, future sites) all share **one
JSON schema**: `shared/schemas/site-config.schema.json`. Every block
that has `additionalProperties: false` rejects unknown keys at startup.
The agent calls `load_config_from_env()` first thing and **exits
status 1 within ~1 second** when validation fails — silently in
systemd-journal terms (only "Main process exited, code=exited,
status=1/FAILURE" shows up).

**Failure signature you've seen before (2026-05-04):** commit added
`articles.url_template` to `specpicks/agents/seo-opportunity-agent/site.yaml`
without updating the schema. Both SEO agents (specpicks + aisleprompt)
broke for ~2 hours until the schema was extended. The error only
appears when running the entry command manually:

```
Config validation failed for .../site.yaml:
  Additional properties are not allowed ('url_template' was unexpected)
  at: articles
```

**Hard rule when adding any `site.yaml` field:**

1. **Add it to the schema first.** Open
   `shared/schemas/site-config.schema.json`, find the right block
   (`articles`, `data_sources`, `analyzer`, `reporter`, `implementer`,
   `deployer`, `coverage_targets`, `page_inventory`, etc.), and add
   the property under `properties` with a real `description`.
2. **Pre-commit-test by running the agent locally** before pushing:
   ```bash
   AGENT_ID=specpicks-seo-opportunity-agent \
   SEO_AGENT_CONFIG=/home/voidsstr/development/specpicks/agents/seo-opportunity-agent/site.yaml \
   DATABASE_URL='postgresql://...' \
   PYTHONPATH=/home/voidsstr/development/reusable-agents \
   python3 /home/voidsstr/development/reusable-agents/agents/seo-opportunity-agent/agent.py
   ```
   Validation errors print to stderr in the first second of output.
3. **Update both per-site `site.yaml` files at once** if the field is
   shared semantics — schema changes apply to every site instance.
4. **Update `agents/seo-opportunity-agent/README.md`** if the field
   adds a user-visible capability (link it in the
   "Configuration → most-used optional blocks" table).
5. **Re-register the manifest** only if the manifest itself changed
   (`bash <site-repo>/agents/register-with-framework.sh`). Editing
   `site.yaml` alone doesn't require re-registration — the next cron
   tick or manual trigger picks it up.

**Cron scheduling discipline.** The two SEO instances run on a
2-hour cycle with a `:15` offset between them
(`specpicks-seo-opportunity-agent: 0 */2 * * *`,
`aisleprompt-seo-opportunity-agent: 15 */2 * * *`) so they don't
race for the same LLM provider quota. **Don't put a third site at
:00 or :15.** Pick :30 or :45 when adding a new site.

**Documentation map (where to put what):**

| Change | Update |
|---|---|
| New phase rule in collector / analyzer / reporter | The phase's `lib/<phase>/README.md` + the engine's [`agents/seo-opportunity-agent/README.md`](agents/seo-opportunity-agent/README.md) (rec-type catalog) |
| New `site.yaml` field | The schema + the engine README + (if onboarding-relevant) `docs/seo-onboard-new-site.md` |
| New rec type | The rec-type catalog table in the engine README |
| New troubleshooting failure mode | Engine README's Troubleshooting section + this CLAUDE.md if it's a recurring class of mistake |
| Architecture-level change to the pipeline | `docs/agents-catalog.md` SEO section + the engine README |

**Per-site app-deploy recipes** (Azure Container Apps, Vercel,
Cloudflare Workers, Netlify) are documented in the
[`agents/seo-deployer/README.md`](agents/seo-deployer/README.md)
example sections — copy from there into a new site's `site.yaml`
under `deployer:`.

## Auto short-circuit — opt EVERY cron-driven agent in

`AgentBase.signals()` is the framework's universal short-circuit hook
(added 2026-05-11 after a retro found 16 of 18 agents were re-running
LLM-touching work on every cron tick with no input change).

**Override `signals()` on any agent that wakes on a schedule and
*sometimes* has nothing new to do.** Three lines, no risk:

```python
class MyAgent(AgentBase):
    def signals(self) -> dict | None:
        return {
            "queue_keys": sorted(self.storage.list_prefix("queue/") or []),
            "last_event_ts": (self.storage.read_json("events.json") or {}).get("ts"),
        }
```

When the hash matches the prior successful run, the framework returns
a `RunResult(short_circuited=True)` and **`run()` is never called** —
no LLM call, no API hits, no Azure list/read traffic.

Rules:
- Only include INPUTS the agent reads. Never include `now`, a tick
  count, a UUID, or anything else that changes every tick.
- Stable serialization: sort lists, round floats. (The framework calls
  `signal_hash()` which already does these for you.)
- Returning `None` opts out (default behavior — preserves status quo
  for un-converted agents).
- If you can't compute a stable hash without doing the expensive work,
  use `framework.core.short_circuit.partition_by_hash` instead and
  short-circuit per-item inside `run()`.

**Conversion status** (2026-05-11):

| Agent | signals() | Notes |
|---|---|---|
| `seo-opportunity-agent` | ✅ in-run via `should_skip` | Pre-dates the hook |
| `progressive-improvement-agent` | ✅ in-run via `should_skip` | Pre-dates the hook |
| `crash-watcher-agent` | ✅ via `signals()` | Hash = (issue_id, lastSeen) tuple list |
| `digest-rollup-agent` | ✅ via `signals()` | Hash = digest-queue file list |
| `catalog-audit-shipped-backfill` | ✅ via `signals()` | Hash = recent recommendations.json filenames |
| `catalog-audit-agent` | ✅ via `signals()` | Hash = latest findings file name + size + mtime |
| `competitor-research-agent` | ⚠️ wrong pattern | NOT signals() — competitor pages change anytime, we can't tell without crawling. Needs `partition_by_hash` INSIDE run() after the crawl, to skip LLM compare for unchanged competitor HTMLs. Separate refactor. |
| `app-store-opportunity-agent` | ⚠️ wrong pattern | Exploration agent — LLM picks new countries/queries each tick, time IS the signal. Short-circuit would freeze it. Skip. |
| `ebay-product-sync-agent` | ⚠️ needs eBay-cursor | Signal needs the eBay shop revision-id from a HEAD request. Domain-specific work; not adopting until next time the file is touched. |
| `product-hydration-agent` | ⚠️ batch-processor | Designed to do incremental batch work each tick. Better fix is to use `partition_by_hash` per-product inside run() so the LLM only runs on products whose content changed. |
| `gsc-coverage-auditor` | n/a | bash/script agent — in the AgentBase conversion backlog above |
| `indexnow-submitter` | n/a | bash agent — conversion backlog |
| `agent-metrics-collector` | n/a | script-style (no AgentBase class) — conversion backlog |
| `goals-tracker` | n/a | script-style — conversion backlog |
| `site-goals-tracker` | n/a | script-style — conversion backlog |
| `responder-agent` | n/a | Inbox-poll agent; IMAP cursor *is* the signal |
| `deployer` | n/a | Reactive dispatch, never cron-fires alone |
| `implementer` | n/a | Same |
| `agent-doctor` | n/a | Only fires on operator-incident dispatch |
| `jcode-agent` | n/a | Reactive |

**Each TODO is ~10 minutes of work.** Pick whichever you're already
touching — never open a side-quest just for short-circuit, but always
do it when you're in the file for another reason.

## When the user asks to inspect / debug an agent

- `curl http://localhost:8090/api/agents/<id>` — full detail
- `curl http://localhost:8090/api/agents/<id>/runs?limit=20` — history
- `curl http://localhost:8090/api/agents/<id>/status` — live status
- `journalctl --user -u agent-<id>.service` — systemd run logs
- UI: http://localhost:8091/agents/<id> → Storage tab — browse the
  agent's blob hierarchy directly

## Framework architecture cheat sheet

Python core under `framework/core/`:

| Module | Role |
|---|---|
| `agent_base.py` | `AgentBase` class + `RunResult` + lifecycle |
| `storage.py` | Pluggable backend (Azure Blob default, Local FS for tests) |
| `registry.py` | Master agent list at `registry/agents.json` |
| `status.py` | Live status writes + global event log |
| `messaging.py` | Inter-agent async messages |
| `confirmations.py` | `@requires_confirmation` decorator |
| `decision_log.py` | Per-run jsonl decision log |
| `context_index.py` | Date-indexed run summaries (caps next-run context budget) |
| `scheduler.py` | systemd `--user` timer/service writer |
| `release_tagger.py` | git commit + tag agent/<id>/release/<run-ts> + push |
| `email_codes.py` | Subject-tag encode/decode for routing replies |
| `guardrails.py` | `Capability` declaration dataclass |
| `mailer.py` | Outbound mailer interface (LogMailer + Graph + SMTP impls) |
| `handoff.py` + `work_types.py` | Inter-agent handoff routing |
| `resilience.py` | `safe_run`, `with_retry`, `notify_operator`, `invoke_doctor` |
| `short_circuit.py` | Skip-LLM-when-inputs-unchanged primitives — `signal_hash`, `snapshot_hash`, `should_skip`, `partition_by_hash`, `merge_findings_cache`. **Any agent that fires on a cron and only sometimes has new work to do MUST use these** rather than re-rolling its own hashing logic. |
| `ai_providers.py` | Provider/model registry — agents call `ai_client_for(agent_id)` and the framework resolves via overrides + defaults |

## LLM efficiency — required patterns for every agent

The framework provides primitives that EVERY new agent should use rather
than reinventing. Failing to use them costs tokens AND introduces drift:

1. **`framework.core.short_circuit.signal_hash` / `snapshot_hash`** —
   hash an agent's inputs (excludes timestamps, rounds floats). Use
   `should_skip(self.state, "last_signals_hash", sig)` at the top of
   `run()` to skip the LLM call when nothing has changed since the last
   successful run. Persist the hash to `next_state["last_signals_hash"]`.

2. **`framework.core.short_circuit.partition_by_hash`** — for
   per-page / per-product / per-row LLM analysis. Skips items whose
   hash matches the prior run + replays prior findings via
   `merge_findings_cache`.

3. **`framework.core.resilience.safe_run`** — wrap any external call
   (LLM, HTTP, DB) so the framework handles retries + operator-alert on
   final failure. Don't roll your own try/except retry loops.

4. **`framework.core.handoff.send_handoff`** — when your agent finds
   work that belongs to a specialist (article generation, code edit, …),
   send a handoff. Don't try to do work outside your role.

5. **Always batch LLM calls when iterating over N items.** A single
   structured-output prompt with N items is dramatically cheaper than N
   individual prompts. See `competitor-research-agent._extract_features_batched`
   for the canonical pattern.

**Rule of thumb:** if you're about to add a `for item in items: client.chat(...)`
loop, stop and ask whether you can do it in one batched call instead.

## ⚠️ DISPATCH-KIND PAUSE — OPERATOR-PAUSEABLE WORK ⚠️

**The implementer can be pinned to a subset of dispatch_kinds** so an
operator manually editing the site repo (or another agent) doesn't get
its work interleaved with framework-driven edits. Storage config:

`config/implementer-allowed-dispatch-kinds.json`:

```json
{
  "schema_version": "1",
  "allow": ["article-author", "news-author", "news-rewrite"],
  "_history": [
    {"set_at": "2026-05-24T16:35:00Z",
     "value": ["article-author", "news-author", "news-rewrite"],
     "reason": "Operator manually editing both sites — pause SEO/PI"}
  ]
}
```

When `allow` is a list, the **backlog-dispatcher** (not the implementer
itself) filters recs at dispatch time — non-matching recs are left in
their producer's run-dir and re-evaluated on the next tick. SEO opp /
PI / catalog-audit recs simply queue up until the restriction is lifted.

**To pause all non-authoring work** (typical use):

```json
{"allow": ["article-author", "news-author", "news-rewrite"]}
```

**To allow everything** (default / unrestricted):

```json
{"allow": ["*"]}
```

**Why this lives at the dispatcher, not the implementer**: cheaper +
safer. The dispatcher's per-tick walk skips disallowed recs in <1ms;
running the implementer scope just to immediately defer wastes a
systemd-run launch + the run.sh boot. Also keeps the implementer's
run-history clean (no "deferred: not in allow-list" rows polluting
the dashboard).

**Where the gate is enforced**: `agents/backlog-dispatcher-agent/agent.py`
inside the candidate-collection loop, right after the rec-handler
allowlist gate. Producer agent_id → dispatch_kind mapping is inlined
(seo-opportunity → seo, progressive-improvement → pi, article-author
→ article-author, catalog-audit → catalog-audit, comp-research →
comp-research, news-author/news-writer → news-author). Individual recs
can override via `rec.dispatch_kind`.

## ⚠️ IMAGE GENERATION — LOCAL ONLY — READ EVERY SESSION ⚠️

**All text-to-image generation MUST use the local SDXL-Turbo daemon at
`http://127.0.0.1:7861/generate`.** This is a hard rule. Paid
providers (Azure OpenAI gpt-image-1, OpenAI DALL-E, fal.ai, Replicate,
Stability, Together, etc.) are forbidden — see the 2026-05-24 incident
where Azure gpt-image-1 was burning ~$190/day at the 8-RPM quota cap.

**Why local-only**:
- Cost: ~$0.0003/img electricity vs $0.042/img paid (140× cheaper).
- Throughput: ~6,000 img/hour on the RTX 5090 vs 8 RPM Azure cap.
- Quality: SDXL-Turbo (and FLUX-schnell when wired up) produces
  food-photography output that beats the paid path for our use case.

**The daemon**: `/home/voidsstr/development/reusable-agents/services/local-image-gen/`
runs under `local-image-gen.service` systemd unit. Default port 7861,
bearer-token auth. See its `README.md` for full API + ops notes.

**The hard rules**:

1. **Any new code that needs an image MUST POST `localhost:7861/generate`.**
   No exceptions. If you need a different model for a specific use case
   (different style, higher quality), run a SECOND instance of the
   daemon with `LOCAL_IMAGE_GEN_MODEL=...` on a different port.
2. **No paid fallback.** If the daemon is down, the operation fails or
   defers (e.g. the refiller agent retries next tick). Silent failover
   to a paid API is what caused the 2026-05-24 incident.
3. **The live API (Azure-hosted simple-server.ts) does NOT generate
   images in the hot path.** It can't reach localhost. New recipes
   start with `image_url = NULL`; the dev-box-hosted refiller agent
   picks them up within 5 minutes.
4. **No `images/generations` calls to ANY hosted provider in any
   service or agent.** Grep before merging: `azureOpenAIGenerateImage`,
   `gpt-image`, `dall-e`, `dalle`, `/images/generations`,
   `AZURE_OPENAI_IMAGE_*`, `fal.ai`, `replicate.com`, `together.xyz/v1/images`.
5. **If the local daemon needs to be reached from Azure** (rare), add
   a Cloudflare tunnel ingress for `images.aisleprompt.com → localhost:7861`
   via the Cloudflare Zero Trust dashboard. DO NOT reintroduce a
   paid provider just because the tunnel is missing.

**Anti-patterns to refuse on sight**:

- ❌ `await azureOpenAIGenerateImage(...)` anywhere — function should
  not exist; the slot in simple-server.ts is a tombstone comment.
- ❌ `process.env.AZURE_OPENAI_IMAGE_*` references — those env vars are
  removed from every deployment. Referring to them is dead code.
- ❌ "Optional fallback to fal.ai/Replicate when the local daemon is
  down" — explicitly banned. Operational hygiene is to keep the daemon
  running, not to add paid escape hatches.
- ❌ Adding a new `images/generations` HTTP call to Azure OpenAI in any
  new code, even "just for testing".
- ❌ Re-creating the deleted `aisleprompt-ai-img` Azure OpenAI image
  deployment without an explicit cost review + rollback plan.

**Reference implementation**: `agents/recipe-image-refiller` →
`scripts/_refill-missing-images.ts` → `localGenerateImage()` shows the
canonical client pattern. The cookware cross-sell strip, article
hero images, and any future image flow use the SAME endpoint.

## ⚠️ ARTICLE + NEWS WRITING — OPUS-ONLY HARD REQUIREMENT ⚠️

**Every code path that GENERATES the body of an editorial article, news
piece, or long-form review MUST use Claude Opus (currently
`claude-opus-4-7`) and MUST defer rather than fall back to a smaller
model when Opus is unavailable.** This applies to:

- `article-author` agents on every site (specpicks, aisleprompt, future)
- News-rewrite / news-author code paths (e.g.
  `specpicks/scripts/rewrite-news-as-commentary.ts`)
- Any agent emitting recs with `dispatch_kind` in
  `{article-author, news-author, news-rewrite}`
- Long-form review writers, head-to-head commentary, buying-guide
  per-pick rationale generation

**The mechanism is `framework/core/required_model.py`** + the storage
config `config/required-models.json`. Resolution order:

1. `rec.required_model_tier` (per-rec override)
2. `config/required-models.json.by_dispatch_kind[<kind>]`
3. `config/required-models.json.by_agent_id[<source_agent>]`
4. None — falls through to soft `recommended_model_tier`

**The current config (in Azure blob `agents/config/required-models.json`):**

```json
{
  "by_dispatch_kind": {
    "article-author":  "opus",
    "news-author":     "opus",
    "news-rewrite":    "opus"
  },
  "by_agent_id": {
    "specpicks-article-author-agent":  "opus",
    "aisleprompt-article-author-agent": "opus",
    "specpicks-news-writer":            "opus"
  }
}
```

**Implementer behavior when Opus is unavailable:** the
`required_model_for_batch()` helper returns `("opus",
"claude-opus-4-7")`; if the implementer can't reach Opus (rate-limit,
auth failure, etc.) it SKIPS the rec with reason
`required-model-unavailable` and the rec stays in the queue. We
intentionally lose throughput rather than ship sonnet-quality prose.

**The defer happens BEFORE any fallback chain runs.** As of 2026-05-24
the implementer's `run.sh` lifts `REQUIRED_MODEL` computation to the
TOP of the dispatch flow — before the `IMPLEMENTER_FORCE_FALLBACK` /
`IMPLEMENTER_BACKEND=copilot-gpt-4.1` shortcuts. When a required-model
batch hits one of those shortcut paths (e.g. claude-pool probe
returned all-dead), the implementer writes `deferred.json` immediately
and exits cleanly. The framework code-editor chain (`jcode-copilot`,
`aider-github-copilot`, `aider-azure`, `jcode-ollama`) is NEVER
allowed to satisfy a required-model batch — those backends can't run
Opus, and shipping their output to an article body would violate the
editorial quality contract. The rec stays in the queue and the
dispatcher retries on the next tick.

**Multi-account claude-pool rotation (2026-05-24).** When you have
multiple Max accounts in different orgs (`profile-1` through
`profile-5`), one being disabled at the org level does NOT mean the
pool is dead. The `_AUTH_DEAD_PATTERNS` in
`framework/cli/claude_pool.py` recognizes the org-disabled message
("Your organization has disabled Claude subscription access") and
marks ONLY that profile dead; the pool rotates to the next profile
automatically. The implementer's probe in `run.sh` walks ALL profiles
before declaring the pool dead — a single org-disabled profile is
not a pool-wide outage. To re-enable a disabled profile, the org
admin restores Claude Code access in the Anthropic console (the
profile's auth blob is still valid — just the org-level toggle).

**Why:** prose quality matters more than throughput for any text a
human reader will judge. Sonnet drafts read as flatter, repeat
generic-CMS phrasing, and lose the differentiated voice of each site.
Opus 4.7 produces the editorial voice the sites are graded on.

**One-off scripts MUST honor this rule too.** Any new ts/py script in
`<site>/scripts/` that calls claude-cli or the framework chat client
to write article-shaped output should hard-code `claude-opus-4-7` in
the `--model` flag (NOT `claude-sonnet-4-6` or downgrade chains).
Reference impl: `specpicks/scripts/rewrite-news-as-commentary.ts`.

**Anti-patterns to refuse on sight:**

- ❌ Adding a `chat_with_fallback(...)` call to article-author code
  (the fallback chain includes sonnet + haiku).
- ❌ Hard-coding `claude-sonnet-4-6` in any article/news writer.
- ❌ Adding entries to `recommended_model_tier` for these
  dispatch_kinds (recommended is soft; required is hard — use
  required).
- ❌ Skipping `required_model_for_batch()` in a custom implementer.

## ⚠️ H2H COMMENTARY ALSO OPUS-ONLY — READ EVERY SESSION ⚠️

**Head-to-head verdict + 600-word performance prose is editorial content
at the same quality bar as articles/news.** As of 2026-05-24 the
required-models config in storage adds:

```json
"by_dispatch_kind": {
  "h2h":              "opus",
  "h2h-commentary":   "opus",
  "comparison_page_generation": "opus"
},
"by_agent_id": {
  "specpicks-head-to-head-agent": "opus"
}
```

This means the implementer skips h2h recs rather than ship sonnet-quality
verdicts. The same rule applies to any future site that adopts the
head-to-head-agent blueprint.

## Copilot-opus bridge — required-opus fallback when claude-pool is broken

**When the Claude Max profiles are unusable** (rate-limit, weekly-cap
exhaustion, or — as happened starting ~2026-05-04 — an organizational
disable of Claude Code: `"Your organization has disabled Claude
subscription access for Claude Code"`), the implementer's required-opus
deferral has a second-chance branch (added 2026-05-24, in
`agents/implementer/run.sh` around line 725) — **opt-in via
`IMPLEMENTER_COPILOT_OPUS_BRIDGE=1` in `~/.reusable-agents/secrets.env`
because as of 2026-05-24 the proxy was returning 402 quota_exceeded for
both opus AND sonnet, so the bridge would consume the deferral path
without actually shipping prose**:

1. Probe the GitHub Copilot proxy at `localhost:4141/v1/models`
2. If `claude-opus-4.7` is listed, route the `claude --print` loop
   through that proxy by setting `ANTHROPIC_BASE_URL=http://localhost:4141`
   and removing `claude-pool/bin` from PATH (so the real claude CLI
   binary is used, not the pool shim — the shim ignores
   `ANTHROPIC_BASE_URL` because it always routes to authenticated profile
   dirs).

**This is why h2h commentary stopped producing new entries between
2026-05-01 and 2026-05-24.** All 5 claude-pool profiles had
`auth_error_message: "Your organization has disabled..."`. The
implementer's required-opus gate deferred every h2h batch because
`IMPLEMENTER_FORCE_FALLBACK=1` (set in `~/.reusable-agents/secrets.env`)
plus opus-required = "no path to satisfy quality bar." The Copilot
bridge is the unblocker.

**Operator checklist when h2h / article / news writing stalls:**

```bash
# 1. Is the Copilot proxy serving opus?
curl -sf http://localhost:4141/v1/models | grep claude-opus-4.7

# 2. Is the pool still showing auth errors?
jq '."profile-1".auth_error_message' \
  ~/.reusable-agents/claude-pool/state.json

# 3. Are recs being deferred for required-model-unavailable?
find ~/.reusable-agents/data -name "deferred.json" -mtime -1 -exec cat {} \;

# 4. Is the implementer picking up the Copilot bridge?
grep -A1 "copilot-opus path available" \
  /tmp/reusable-agents-logs/agent-implementer.log | tail
```

**If Copilot proxy is dead**, start it (the user's own setup, varies),
or provision `ANTHROPIC_API_KEY` with real opus access and the regular
claude CLI will route through that instead.

**Anti-patterns to refuse on sight:**

- ❌ Removing the Copilot bridge "because claude-pool is back" — leave it
  as defense-in-depth. The bridge only fires when claude-pool is
  force-disabled.
- ❌ Hard-coding `claude-opus-4.7` model name in the bridge instead of
  reading it from the proxy's `/v1/models` list (the proxy may rename
  to `claude-opus-5.0` next year; the probe should adapt).
- ❌ Pointing `ANTHROPIC_BASE_URL` at the Copilot proxy globally (i.e.
  in systemd Environment=). That would route ALL claude calls — not
  just required-opus ones — through Copilot, defeating the
  rate-budgeting the Max profiles provide for non-opus work.

## LLM provider routing — chat vs code-editor

The framework has TWO independent LLM systems. Don't conflate them:

1. **Chat** — `framework.core.ai_providers`. Used by anything that
   calls `self.ai_client()` or `chat_with_fallback(...)`. Provider
   kinds: `copilot` (GitHub Copilot proxy, subscription billing — the
   default), `claude-cli` (Claude Max session), `anthropic`/`openai`/
   `azure_openai` (per-token API), `ollama` (free local).
   Defaults + per-agent overrides live at
   `config/ai-defaults.json` in storage; edit via the dashboard
   `/providers` page or `POST /api/providers/defaults/{set,agent-override}`.
   `chat_with_fallback` auto-walks `('copilot', 'azure_openai',
   'openai', 'anthropic', 'ollama')` on rate-limit/timeout/quota
   errors.

2. **Code editor** — `framework.core.code_editor`. Used by the
   implementer + any `llm-code-editor` blueprint. A chain of editor
   binaries (`aider`, `opencode`, `crush`, `codex`, `plandex`) each
   paired with a model (claude-sonnet-4.6 via copilot proxy is top of
   chain; gpt-4.1-mini via Azure is fallback). Configured at
   `config/code-editor-config.json` in storage. The chain runs *after*
   any agent-specific editor logic (e.g. the implementer's claude-pool
   path).

**When the user says "switch from claude to copilot/aider":**
- Chat agents → change the provider in
  `config/ai-defaults.json` (global default or per-agent override).
- Implementer → set `IMPLEMENTER_FORCE_FALLBACK=1` in its env, or
  `IMPLEMENTER_LLM=framework`, to skip the claude-pool path and go
  straight to the framework code-editor chain (which uses aider).

**Don't shell out to `claude` / `aider` / `gh copilot` directly** from
new agent code — both systems above already wrap those binaries with
live LLM stream capture, usage tracking (`config/llm-usage-*.jsonl`),
fallback chains, and dashboard visibility. Direct shell invocations
bypass all of that and create yet another duplicated provider chain.

**Where the docs are:**
- `README.md` → "LLM provider chain — chat agents + code editor" (operator-facing tables of providers, backends, env knobs)
- `agents/implementer/AGENT.md` → "## LLM driver" (implementer's specific claude-pool → framework-chain path)
- This file → routing rules + when to switch what

API service: `framework/api/app/main.py` (FastAPI). 35 routes + 2 WS
streams. Token auth via `FRAMEWORK_API_TOKEN`.

UI: `framework/ui/src/`. React + Vite + Tailwind. Iframe-friendly.

Templates: `_template/agent/` is what `create-agent.sh` clones from.

Tests: `framework/tests/` — 20 pytest cases against LocalFilesystemStorage.

## Deploying the agent dashboard to Azure — REQUIRED after every change

**Every change to `framework/api/` or `framework/ui/` MUST be deployed to
Azure as part of the same task.** Local docker compose updates only the
dev box at http://localhost:8091; the production dashboard at the
Container App FQDN keeps serving the previous build until you push.

Deploy command (from the repo root):

```bash
cd /home/voidsstr/development/reusable-agents
bash install/deploy-azure.sh
```

The script builds + pushes both images to `nscappsacr` ACR and updates
the `agents` Container App in the `nsc-apps` resource group. Default
tag is a UTC timestamp; pass an explicit tag if you need to roll back
to a specific build (`bash install/deploy-azure.sh 20260428-1620`).

When this is required:
- `framework/api/**` (FastAPI routes, lifespan, host-worker, core/*)
- `framework/ui/**` (React components, pages, styling)
- `framework/core/**` (shared modules the API imports — agent_base,
  status, ghost_reaper, llm_stream, storage, decision_log, etc.)

When this is NOT required:
- Agent code under `agents/<id>/` (host-worker re-execs each run from
  the host filesystem, so a `git pull` on the host is enough)
- Docs (`*.md`), tests, blueprints
- `_template/`, `install/` (shell scripts run on the host)

After deploying, verify the new build:

```bash
# FQDN comes from the script's final "Deploy complete" line, or:
az containerapp show -g nsc-apps -n agents --query properties.configuration.ingress.fqdn -o tsv

# Confirm it's serving the new tag
curl -sI https://<fqdn>/ | head -3
```

If the user reports "the dashboard doesn't show my change," your first
check should be: did this task include a deploy-azure.sh run?

## Per-site app deployments — pick a recipe, edit the site.yaml

When an agent commits **application code** (not just data), the
framework's deployer chains into a 5-stage pipeline so the change
ships without manual intervention:

```
test → build → push → deploy → smoke_check
```

Every stage is a shell command template. The deployer is cloud-agnostic
— whatever `bash` can do, this can deploy.

**To configure a site for deployment**:

1. Pick a recipe from `examples/deployer/`:
   - `azure-container-apps.yaml` — **active**, used by aisleprompt + specpicks
   - `azure-app-service.yaml` — Azure App Service (sample)
   - `azure-functions.yaml` — Azure Functions (sample)
   - `aws-ecs-fargate.yaml` — AWS ECS Fargate + ECR (sample)
   - `aws-lambda.yaml` — AWS Lambda container image + ECR (sample)
   - `aws-app-runner.yaml` — AWS App Runner + ECR (sample)
2. Drop the entire `deployer:` block from the recipe into the site's
   `site.yaml` (under the same key).
3. Edit `deploy.vars:` for the site (app name, resource group, image
   URI, etc.). Every key here is expanded as `{key}` in any stage's
   `cmd:`.
4. Make sure the deployer host has the cloud CLI configured (az login
   for Azure, aws configure for AWS, etc.) and docker reachable.

**Where existing sites declare their deployer**:

| Site         | Path                                                                                  |
|--------------|---------------------------------------------------------------------------------------|
| aisleprompt  | `nsc-assistant/agents/aisleprompt-seo-opportunity-agent/site.yaml`                   |
| specpicks    | `specpicks/agents/seo-opportunity-agent/site.yaml`                                   |

**When the deployer SKIPS itself** (intentional):

| Dispatch kind     | Behavior  | Why |
|-------------------|-----------|-----|
| `article-author`  | skip      | articles ship via DB INSERT — no docker build needed |
| `catalog-audit`   | skip      | DB-only fixes |
| `h2h`             | skip      | DB-only |
| `IMPLEMENTER_SKIP_DEPLOY=1`  | skip | env override (manual rollbacks, debug runs) |
| anything else     | run       | the per-batch deployer fires |

**To add a new cloud target**:

1. Copy any of the dormant recipes in `examples/deployer/` as a starting
   point.
2. The framework's `agents/seo-deployer/deployer.py` does NOT need to
   change — it just runs whatever shell commands you give it. So to
   add Kubernetes, GCP Cloud Run, Cloudflare Workers, etc., you just
   write the recipe.
3. Add it to the recipe table in `examples/deployer/README.md` and the
   table in this section.

**Don't hardcode cloud-specific logic in `deployer.py`** — every site's
target is config, not code. If you find yourself wanting a per-cloud
branch in the framework, that's a sign the recipe is missing a knob.
Add the knob to the recipe + README and keep the deployer dumb.

## Hosting note

This codebase is shared. When working in nsc-assistant, specpicks, or
customer apps, **don't refactor the framework itself** unless the user
explicitly asks. Submit framework changes as PRs to the
reusable-agents repo and pull them via `git pull` in the framework
checkout.

## ⚠️ INFRA SCRIPTS ARE THE SOURCE OF TRUTH — READ EVERY SESSION ⚠️

**Any change to deployment behavior MUST land in the deploy scripts in
the same change.** No ad-hoc CLI commands, no "I'll commit it later",
no "this is just a one-off." If you ran an `aws`, `az`, `docker`, or
`gh` command that materially changed how the app gets deployed, it
belongs in a script before you call the work done.

**Where things live:**

| Change                              | Script(s) to update                                  |
|-------------------------------------|------------------------------------------------------|
| Framework provisioning (VPC, RDS,   | `install/deploy-aws.sh` + `install/deploy-azure.sh`  |
| ECR/ACR, S3/blob, ALB, IAM, secrets,| (keep cloud-agnostic phases parallel)                |
| ECS cluster)                        |                                                      |
| Framework image build/push          | `install/deploy-aws.sh` `phase_images` +             |
| (api / ui)                          | `install/deploy-azure.sh` matching block             |
| Per-app image build/push/redeploy   | `<repo>/aws/deploy.sh` + `<repo>/azure/deploy.sh`    |
| New env var the app reads           | Both deploy scripts (env passthrough) + the          |
|                                     | Secrets Manager / Container App secret block         |
| New custom domain                   | Both `phase_dns` (AWS) and the Azure equivalent      |
| New cloud target (GCP, Cloudflare)  | New `install/deploy-<cloud>.sh` mirroring the same   |
|                                     | phase shape; no per-cloud logic in agents/framework  |

**Decision rule before every PR that touches deployment:**

1. **"Did I run a cloud CLI command that the deploy script doesn't
   already do?"** If yes, add it to the script in the same change.
2. **"If a teammate ran `bash install/deploy-aws.sh provision` on a
   fresh AWS account tomorrow, would they end up in the same state
   I did?"** If no, the script is incomplete — fix it.
3. **"If we add a new cloud, would this still work?"** All cloud-
   specific code lives in `install/deploy-<cloud>.sh` and
   `<repo>/<cloud>/deploy.sh`. Framework and agent code stays
   cloud-neutral.

**Anti-patterns to refuse on sight:**

- Manual `aws ecs update-service` (or `az containerapp update`)
  invocations not wrapped by a script in the relevant repo.
- "Workaround" commits that fix prod without updating the deployer.
- New cloud resource (e.g. SQS queue, ElastiCache, Key Vault) referenced
  in agent code without a provisioning block in
  `install/deploy-<cloud>.sh`.
- One cloud's deploy script gains a feature (e.g. ALB rules); the
  other cloud's lags. Both stay parallel — if AWS gets it, so does
  Azure (and vice-versa).
- Updating `<repo>/aws/deploy.sh` without checking that
  `<repo>/azure/deploy.sh` still works with the same env contract.

**State file contract:** `install/deploy-aws.sh provision` writes
`~/.aws-deploy/state.env` (VPC, subnets, cluster, ALB, RDS endpoint,
ECR registry). Every per-app `<repo>/aws/deploy.sh` reads that file.
Same shape on the Azure side via `install/deploy-azure.sh` (state lives
in the Azure resource group, which is itself the contract). Do not add
fields to one side without the other.

## Deploying to AWS (parallel cloud target)

`install/deploy-aws.sh` is the AWS equivalent of `install/deploy-azure.sh`.
Same idempotent shape, phased:

```
bash install/deploy-aws.sh provision   # VPC, RDS, ECR, S3, ALB, IAM, Secrets, ECS cluster
bash install/deploy-aws.sh secrets     # populate Secrets Manager entries
bash install/deploy-aws.sh images      # build + push framework api/ui images
bash install/deploy-aws.sh services    # create/update ECS task defs + services for all apps
bash install/deploy-aws.sh dns         # print CNAME records for the ALB
bash install/deploy-aws.sh all         # full pipeline
```

Per-app images (aisleprompt, specpicks, hearthnote, nsc-website,
application-research) live in their own repos under `<repo>/aws/deploy.sh`
— each script reads `~/.aws-deploy/state.env` from the framework
provisioner so it knows the cluster + service name to push to.

**Don't add AWS-specific branching to existing agents.** Everything that's
cloud-specific lives in the deploy scripts and the framework storage
backend factory. Agent code is cloud-neutral.
