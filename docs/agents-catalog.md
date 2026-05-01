# Agents Catalog

Every agent registered with the framework, organized by category. For each
agent: what it does, where the code lives, where the manifest lives, and
what cadence it runs on.

If you're adding a new agent, see [`../blueprints/README.md`](../blueprints/README.md).

## Categories

- [Framework infrastructure](#framework-infrastructure) — agents that run the framework itself
- [SEO + revenue (reusable engine)](#seo--revenue-reusable-engine) — the per-site SEO pipeline
- [SEO + revenue (per-site instances)](#seo--revenue-per-site-instances) — site-specific SEO drivers
- [Content authoring](#content-authoring) — article + comparison generation
- [Catalog + product data](#catalog--product-data) — product hydration, eBay sync, audit
- [Site quality (recommend + auto-implement)](#site-quality-recommend--auto-implement) — progressive improvement
- [Research](#research) — competitor + benchmark + market research
- [Personal](#personal) — daily briefings, real estate, travel, etc.
- [Fleet (retro-PC infrastructure)](#fleet-retro-pc-infrastructure)

---

## Framework infrastructure

These keep the framework itself healthy. All live in
`reusable-agents/agents/`.

| Agent | Code | Schedule | What it does |
|---|---|---|---|
| `agent-doctor` | `agents/agent-doctor/` | every 5 min | Watches the framework API for failed / stuck agents. Classifies error signatures and applies known remediations (kill stuck systemd unit, requeue trigger, escalate to human). |
| `digest-rollup-agent` | `agents/digest-rollup-agent/` | `0 */3 * * *` | Drains `digest-queue/` (every per-agent email gets queued there when `DIGEST_ONLY=1`), groups by site + agent, sends ONE summary email to the operator. The user normally sees this single email instead of dozens of per-agent ones. |
| `responder-agent` | `agents/responder-agent/` | every 1 min | Polls IMAP for replies + drains `agents/responder-agent/auto-queue/` (which the SEO reporter and other auto-shipping agents drop into). Dispatches batches to the implementer via `systemd-run`. |
| `host-worker` (not an agent — systemd unit) | `install/host-worker.sh` | continuous | Polls `/tmp/agent-trigger-queue/` and execs the registered `entry_command` for "Run now" triggers. Runs ON the host because containers can't write systemd timers. |

## SEO + revenue (reusable engine)

The SEO chain is split into five pure-pipeline-stage agents that
together implement the collector → analyzer → reporter → implementer
→ deployer flow. Each is reusable across every site — site-specific
behavior comes entirely from the per-site `site.yaml`. None has its
own cron schedule; they're chained from the per-site
`seo-opportunity-agent` wrappers.

| Agent | Code | What it does |
|---|---|---|
| `seo-data-collector` | `agents/seo-data-collector/pull-data.py` | Pulls 90d Google Search Console + GA4 data, optionally Google Ads. Crawls the sitemap, samples per page-type (regex from `site.yaml.page_inventory`), fetches HTML for the analyzer's audit pass. Pulls article inventory (slug+title+optional body_md) from the site's DB. Optionally runs SQL queries from `db-queries.sql`. Output: standardized run-dir layout (`gsc-*.json`, `ga4-*.json`, `pages-by-type.jsonl`, `articles-inventory.json`, `db-stats.json`). |
| `seo-analyzer` | `agents/seo-analyzer/analyzer.py` | Deterministic Python. Reads collector outputs + applies the rule passes below, then a final adaptive LLM audit pass. Output: `recommendations.json` with monotonically-numbered rec ids. **Rule passes** (each opt-out via `site.yaml` flags, all run when present): striking-distance, zero-click, indexing-fix, conversion-path, paid-organic-gap, ad-copy-headline-winner, content-gap (`new-page-*` against `coverage_targets`), schema completeness, on-page (title/meta/H1/thin-content/internal-link), pros/cons density, outbound citations, body-link density, article-template completeness, **affiliate-tag leak**, **article × featured-product mention attribution** (`revenue_focus`), **TTFB perf** (`cwv-ttfb-slow` at 600ms / 1500ms), **freshness** (`dateModified` coverage on articles), **FAQ quality** (≥3 Qs, ≥20-word avg answer), **hreflang completeness**, **footer trust-links** (privacy/terms/contact/about/affiliate), **breadcrumb parity** (visible vs JSON-LD count), **trust-signal density** (warranty/guarantee/authorized keyword count), **collection numberOfItems** schema, **revenue-focus PDP audit** (featured-PDP weakness + `priority_boost` multipliers), **diff alarms** (sitemap-shrank, schema regression vs prior run), **competitor keyword gaps**. |
| `seo-reporter` | `agents/seo-reporter/send-report.py` | Renders an HTML digest of recommendations + comparisons + goal-progress, sends via the framework mailer (gated through `DIGEST_ONLY=1` so it lands in the rollup queue). ALSO writes `agents/responder-agent/auto-queue/<request-id>.json` so the responder ships every rec without waiting for a reply. |
| `implementer` | `agents/implementer/run.sh` | LLM-driven code editor. Receives `RESPONDER_REC_IDS=<csv>` + `RESPONDER_RUN_DIR=<run-ts>` from the responder, resolves `DATABASE_URL` per-site (from `DATABASE_URL_<UPPER_SITE>` env), routes through claude-pool, drives `claude --print` against `AGENT.md` to apply the edits. Commits + tags `agent/<id>/release/<run-ts>` and optionally chains to seo-deployer. |
| `seo-deployer` | `agents/seo-deployer/run.sh` | Pluggable deploy pipeline. Runs configured test → build → push → deploy → smoke-check commands per `site.yaml.deployer`. Hard gates on test or smoke-check failure. Stays a no-op when `deployer: null` (e.g. SpecPicks ships via Azure Container Apps using its own scripts). |

## SEO + revenue (per-site instances)

These are thin per-site wrappers. Each manifest's `entry_command`
exports `SEO_AGENT_CONFIG=<path-to-site.yaml>` and runs the four-stage
chain in series. The framework registers each as a distinct agent id
so the dashboard tracks runs separately.

| Agent | Manifest | site.yaml | Schedule |
|---|---|---|---|
| `specpicks-seo-opportunity-agent` | `specpicks/agents/seo-opportunity-agent/` | same dir | `30 */3 * * *` (every 3h at :30) |
| `aisleprompt-seo-opportunity-agent` | `nsc-assistant/agents/aisleprompt-seo-opportunity-agent/` | same dir | `0 */3 * * *` (every 3h at :00) |

A new site adds a third entry. See
[`seo-onboard-new-site.md`](seo-onboard-new-site.md) for the 5-step
process.

## Content authoring

| Agent | Code | Per-site manifests | Schedule | What it does |
|---|---|---|---|---|
| `specpicks-article-author-agent` | `specpicks/agents/article-author-agent/` (one-off, not yet generalized) | same dir | `45 */2 * * *` | Daily Claude-driven proposals for new SpecPicks articles + buying guides. Reads GSC zero-click + quick-wins, the editorial_topics queue, recently-published articles, the buying-guides backlog, the **featured-products set** (drives revenue focus), and trending topics that match featured products. Auto-queues approved proposals to the implementer (via the same auto-queue path as SEO recs). |
| `head-to-head-agent` (specpicks) | `specpicks/agents/head-to-head-agent/` | same dir | varies | Generates `<a>-vs-<b>` comparison content across all category pairs. Auto-queues to implementer. |

Article-author and head-to-head are currently SpecPicks-only because
their topic models are coupled to the SpecPicks DB schema. Generalizing
them to the reusable-agents repo (with per-site `site.yaml` driving
table names + categories) is a candidate refactor.

## Catalog + product data

| Agent | Code | Per-site manifest | Schedule | What it does |
|---|---|---|---|---|
| `specpicks-product-hydration-agent` | reusable: `agents/product-hydration-agent/` | `specpicks/agents/product-hydration-agent/` | `15 */2 * * *` | Every 2h: refreshes Amazon-sourced product pricing via BrightData (default — falls back to PA-API once SpecPicks qualifies for Associates), then runs Claude-driven content hydration (description, pros/cons, FAQ, SEO meta) on the highest-priority products needing it. Final phase recomputes the `is_featured` set via `select-featured.py` so the home-page Editor's Choice section stays fresh. |
| `specpicks-ebay-product-sync-agent` | reusable: `agents/ebay-product-sync-agent/` | `specpicks/agents/ebay-product-sync-agent/` | `30 * * * *` | Hourly: pulls live eBay Browse API listings, runs Claude to map raw listing fields → product schema. **First run requires schema-mapping approval** (only `confirmation_flow.kind=schema-mapping-approval` agent in the suite — bad mapping ruins the catalog). Subsequent runs are full auto-queue. |
| `specpicks-catalog-audit-agent` | reusable: `agents/catalog-audit-agent/` | `specpicks/agents/specpicks-catalog-audit-agent/` | varies | Runs `scripts/catalog-quality-audit.ts`, surfaces six classes of catalog defects, auto-queues fix recs to the implementer. |
| `aisleprompt-catalog-audit-agent` | reusable: `agents/catalog-audit-agent/` | `nsc-assistant/agents/aisleprompt-catalog-audit-agent/` | varies | Same engine, AislePrompt's pantry/recipe DB. |
| `aisleprompt-kitchen-scraper` | `nsc-assistant/agents/aisleprompt-kitchen-scraper/` (one-off) | same dir | `*/30 * * * *` | Pulls fresh recipe / kitchen-product data into AislePrompt's catalog. |
| `specpicks-scraper-watchdog` | `specpicks/agents/scraper-watchdog/` | same dir | `*/5 * * * *` | Health-checks the SpecPicks main scraper Docker container, restarts it if it died. |

## Site quality (recommend + auto-implement)

The progressive-improvement-agent is a category of agent shaped by the
`site-quality-recommender` blueprint: crawl-or-query a site → LLM
analysis → emit ranked recs → auto-queue every rec to the implementer.

| Agent | Code | Per-site manifest | Schedule | What it does |
|---|---|---|---|---|
| `aisleprompt-progressive-improvement-agent` | reusable: `agents/progressive-improvement-agent/` | `nsc-assistant/agents/aisleprompt-progressive-improvement-agent/` | daily | Daily quality audit of aisleprompt.com — checks UX/UI, accessibility, SEO basics, content gaps. |
| `aisleprompt-user-growth-strategist` | reusable: `agents/progressive-improvement-agent/` (specialized prompt) | `nsc-assistant/agents/aisleprompt-user-growth-strategist/` | daily | Specialized prompt focusing on user growth + conversion (signup flow, onboarding, retention). |
| `aisleprompt-competitor-research-agent` | reusable: `agents/competitor-research-agent/` | `nsc-assistant/agents/aisleprompt-competitor-research-agent/` | daily | Daily comparison of aisleprompt vs competitor grocery/recipe apps (Plan to Eat, Mealime, Paprika, AnyList). Emits feature-gap + UX recs. |

## Research

| Agent | Code | Schedule | What it does |
|---|---|---|---|
| `reusable-agents-competitor-research-agent` | `reusable-agents/agents/reusable-agents-competitor-research-agent/` | `0 7 * * 1` (weekly) | Self-improvement: this framework analyzes itself vs n8n / Temporal / Airflow / Prefect / Inngest, emits feature-parity recs that get auto-queued to the implementer running against the framework's own repo. |
| `specpicks-competitor-research-agent` | `specpicks/agents/competitor-research-agent/` | varies | Daily competitor comparison for SpecPicks vs Wirecutter, RTINGS, PCPartPicker, etc. |
| `specpicks-benchmark-research-agent` | `specpicks/agents/benchmark-research-agent/` | varies | Pulls fresh GPU/CPU benchmark data. |
| `external-game-cataloger` | `nsc-assistant/agents/external-game-cataloger/` | `0 5 * * 0` (weekly) | Catalogs new game releases relevant to the retro/PC fleet. |
| `game-library-scanner` | `nsc-assistant/agents/game-library-scanner/` | `0 6 * * *` | Scans the SMB share for new game installers. |
| `market-research-pipeline` | `nsc-assistant/agents/market-research-pipeline/` | `*/30 * * * *` | The big one — 24-stage pipeline from idea → blueprint → app generation → testing → deploy. Independent docs in [`agents/market-research-pipeline/README.md`](../../nsc-assistant/agents/market-research-pipeline/README.md). |

## Personal

| Agent | Manifest | Schedule | What it does |
|---|---|---|---|
| `daily-briefing-calendar-agent` | `nsc-assistant/agents/daily-briefing-calendar-agent/` | `30 7 * * *` | Morning brief: today's calendar + weather + traffic. |
| `daily-status-briefing` | `nsc-assistant/agents/daily-status-briefing/` | manual | On-demand briefing across all agent activity. |
| `email-monitor` | `nsc-assistant/agents/email-monitor/` | `*/15 * * * *` | Watches the operator inbox for items needing personal attention. |
| `real-estate-agent` | `nsc-assistant/agents/real-estate-agent/` | `0 7 * * *` | Daily check on saved property searches. |
| `travel-miami` / `travel-fontainebleau` | `nsc-assistant/agents/travel-*` | varies | Travel research agents (deprecated — slated for removal). |
| `web-search` | `nsc-assistant/agents/web-search/` | manual | On-demand SearxNG-backed search agent. |
| `indexnow-submitter` | `nsc-assistant/agents/indexnow-submitter/` | manual | Submits new URLs to IndexNow on demand. |

## Fleet (retro-PC infrastructure)

These are tightly coupled to the retro-PC fleet at NSC and live in
`nsc-assistant/agents/`. They are NOT generalizable.

| Agent | Schedule | What it does |
|---|---|---|
| `retro-multiplayer-refresh` | `30 5 * * *` | Re-applies favorites scripts on every retro PC for Q3/Q2/UT99/UT2004. |
| `retro-agent-orchestrator` | manual | Coordinates fleet-wide operations on the four Win XP machines. |
| `security-scanner-pipeline` | `*/10 * * * *` | Continuous security scan against fleet endpoints. |
| `fix-submission-agent` | `*/2 * * * *` | Picks up customer-app fix proposals from the dashboard queue and runs them through the fix pipeline. |

---

## Quick lookup — "where does X live?"

| You want to | Look here |
|---|---|
| Edit reusable agent code | `reusable-agents/agents/<id>/` |
| Edit a per-site manifest / config | `<customer-repo>/agents/<id>/` |
| See an agent's run history | `http://localhost:8091/agents/<id>` (or the Azure FQDN) |
| Read an agent's runbook | `<dir>/AGENT.md` |
| Read a blueprint | `reusable-agents/blueprints/<name>/BLUEPRINT.md` |
| Re-register agents after editing manifests | `bash <repo>/agents/register-with-framework.sh` (specpicks) or `bash scripts/register-agents.sh` (nsc-assistant) |

## Schedule overview (cron + frequency)

```
* * * * *           responder-agent                     ← every minute
*/2 * * * *         fix-submission-agent
*/5 * * * *         agent-doctor
*/5 * * * *         specpicks-scraper-watchdog
*/10 * * * *        security-scanner-pipeline
*/15 * * * *        email-monitor
*/30 * * * *        market-research-pipeline
*/30 * * * *        aisleprompt-kitchen-scraper
30 * * * *          specpicks-ebay-product-sync-agent   ← hourly at :30
15 */2 * * *        specpicks-product-hydration-agent   ← every 2h at :15
45 */2 * * *        specpicks-article-author-agent      ← every 2h at :45
0 */3 * * *         aisleprompt-seo-opportunity-agent
0 */3 * * *         digest-rollup-agent                 ← every 3h at :00
30 */3 * * *        specpicks-seo-opportunity-agent
30 5 * * *          retro-multiplayer-refresh           ← daily 05:30
0 6 * * *           game-library-scanner
0 7 * * *           real-estate-agent
30 7 * * *          daily-briefing-calendar-agent
0 7 * * 1           reusable-agents-competitor-research-agent ← weekly Mon 7am
0 5 * * 0           external-game-cataloger             ← weekly Sun 5am
```

All times in `America/Detroit` unless the manifest says otherwise.
