# `agents/`: index

This index covers every directory under `reusable-agents/agents/`. Each entry
says what kind of thing the directory is, whether and when it runs, what it
does, and where its runbook is.

**Audited 2026-09-23** against:
- the framework registry (`GET /api/agents` on `127.0.0.1:8090`, 85 entries);
- the 69 `agent-*.timer` units on the fleet host (67 enabled, 2 disabled);
- each manifest's `entry_command` and each unit's `ExecStart`;
- per-agent runbook audits.

Status lines are snapshots. For live state, use the dashboard or
`GET /api/agents/<id>`.

**Conventions**
- **Schedules are host-local time (America/Detroit).** `framework/core/scheduler.py`
  turns `cron_expr` into `OnCalendar=` and ignores the manifest `timezone`.
  The times below are the effective `OnCalendar` values, host drop-ins included.
- **Kinds:**
  - **Framework agent:** scheduled from this repo.
  - **Engine:** code here, run only by per-site instance directories in other repos.
  - **Chained:** dispatched by another agent and never timer-driven.
  - **Blueprint / not an agent / archived:** self-explanatory.
- **Paths to other repos** are relative to `/home/voidsstr/development/`.
  Links in this file are relative to `reusable-agents/agents/`.

## Contents

1. [Framework agents](#1-framework-agents)
2. [Chained agents (not scheduled)](#2-chained-agents-not-scheduled)
3. [Shared engines and their per-site instances](#3-shared-engines-and-their-per-site-instances)
4. [Blueprints, templates, and non-agents](#4-blueprints-templates-and-non-agents)
5. [Archived](#5-archived)
6. [Scheduled agents that live in other repos](#6-scheduled-agents-that-live-in-other-repos)
7. [Adding and registering an agent](#7-adding-and-registering-an-agent)

---

## 1. Framework agents

These agents work across the whole fleet rather than for one site. Each row
is one registered id with its own timer.

| Dir | Schedule | Status (2026-09-23) | What it does | Runbook |
|---|---|---|---|---|
| `agent-doctor` | Every 5 h at :00 (00/05/10/15/20). Also invoked on demand through `resilience.invoke_doctor` / host-worker. | Live. The 16:47Z run investigated 1 run, fixed 0, escalated 1. The fixes-log holds 2,712 entries since 2026-08-24, none with outcome `fixed`. | Self-healer. Drains resilience incidents, polls `/api/agents` for failed or stuck runs, classifies log signatures, then applies a narrow recipe (token refresh, stale-lock cleanup, transient no-op) or a `claude --print` diagnosis. Escalates through `notify_operator`. | [AGENT.md](agent-doctor/AGENT.md) |
| `app-store-opportunity-agent` | Daily 14:00 | Live. Both 2026-09-23 runs (03:48Z and the scheduled 18:27Z) succeeded with 279 open opportunities, 2 new. In each run all 3 blueprint generations failed on claude-cli errors (claude-pool auth failover, "max turns", 600 s timeouts). No handler exists for "implement opp-NNN" replies. | Scouts the iOS App Store and Google Play for popular low-rated apps and regional gaps. Accumulates them in storage, writes build blueprints for the top picks, and emails a ranked list. Instance of the `app-store-opportunity-finder` blueprint. | [AGENT.md](app-store-opportunity-agent/AGENT.md). `README.md` and `*-spec.md` in this dir are an app spec, not agent docs. |
| `authority-agent` | Daily 13:00 | Live, but its input (`gsc_crawl_progress`) has been frozen since 2026-07-28. | Reads the SpecPicks indexation snapshot, ranks the 40 most citable published articles, and emails a link-building worklist of the top 10. It writes nothing to the DB. | [AGENT.md](authority-agent/AGENT.md) |
| `backlog-dispatcher-agent` | Every minute | Live. Its log from 2026-09-23 03:11Z to 18:51Z holds 941 ticks: 868 found nothing, 44 were throttled, 29 dispatched 1–10 recs. `config/implementer-allowed-handlers.json` is an archived blob, so each non-throttled tick logs an error and no handler restriction applies. | Walks recent run dirs of 13 hard-coded producer ids (`PRODUCER_AGENT_IDS`) for recs that are not yet shipped, deferred, skipped or backed off. Dispatches them with `dispatch_now()` within claude-pool/copilot capacity caps, and kills stuck scopes. | [README.md](backlog-dispatcher-agent/README.md) |
| `catalog-audit-shipped-backfill` | Every 30 min | Live, but every run since 2026-09-22 has found 0 candidates. | Reads each site's latest catalog-audit `recommendations.json` files, checks in the prod DB that each migration took effect (AislePrompt criteria only), and flips verified recs to `shipped`. | [AGENT.md](catalog-audit-shipped-backfill/AGENT.md) |
| `digest-rollup-agent` | Daily 07:30, set by the host drop-in `agent-digest-rollup-agent.timer.d/10-daily.conf` (2026-09-15) | Live. **The manifest and registry still say `16 */5 * * *` with `enabled=false`.** Re-registering the manifest as-is stops and disables the timer. `WINDOW_HOURS=3` is hard-coded while the timer is daily. | Sends one consolidated operator email (Graph first, msmtp fallback) covering shipped recs, the auto-queue, failures, doctor escalations and the queued `digest-queue/` mail, then archives the queue. | [README.md](digest-rollup-agent/README.md) |
| `oauth-heartbeat-agent` | None. Timer disabled; the manifest's `37 8 * * *` is inactive. | Retired 2026-09-04 (commit `c924f0a`) when the OAuth consent screen was published to production. The registry entry still says `enabled=true`. | Minted Google OAuth tokens daily to keep a Testing-mode refresh token alive. | [README.md](oauth-heartbeat-agent/README.md) |
| `responder-agent` | Every 2 min | Firing but **inert since host standup (2026-08-13)**. `~/.reusable-agents/responder/config.yaml` is still byte-identical to `config.example.yaml` (`imap.example.com`), so every tick fails DNS yet reports success. | Polls the automation IMAP inbox, parses operator replies (implement, skip, merge or modify by rec id or range), writes `agents/<target>/responses-queue/`, and spawns the implementer when a route matches. | [AGENT.md](responder-agent/AGENT.md) |
| `agent-metrics-collector` | Daily 11:00. Registered through `nsc-assistant/agents/agent-metrics-collector/run.sh`, which runs this dir's `agent.py`. | Degraded. Every DB-derived metric fails because the code reads `<SITE>_DATABASE_URL` while `secrets.env` defines `DATABASE_URL_<SITE>`. Its own goals have never recorded progress. | Plain Python, not AgentBase. Back-fills goal metrics for the agents in `AGENT_METRIC_FNS` from storage run dirs, site DB counts and GSC coverage via `metric_helper.record_many`. Some values are hard-coded placeholders. | [AGENT.md](agent-metrics-collector/AGENT.md) |
| `goals-tracker` | Daily 12:00. Registered through `nsc-assistant/agents/goals-tracker/run.sh`. | Live. The 2026-09-23 digest covered 55 agents, 222 goals and 37 stale metrics. Its own goals are never recorded. | Plain Python, not AgentBase. Sends a daily HTML email rolling up every enabled agent's goals (trend, sparkline, stale alert) directly through Graph, bypassing the digest gate. `last-digest.html` is generated output. | [AGENT.md](goals-tracker/AGENT.md) |

## 2. Chained agents (not scheduled)

| Dir | How it runs | Status (2026-09-23) | What it does | Runbook |
|---|---|---|---|---|
| `implementer` | On demand, as transient `systemd-run --user --scope` units named `agent-dispatch-implementer-<site>-<ts>`. Spawned by `framework.core.dispatch.dispatch_now` (backlog-dispatcher, producers) and by `auto-queue-drainer.service`. Registry: `enabled=false`, `runnable_modes=["chained"]`, no timer. | Live. The claude-pool `profile-4` is auth-dead and needs an operator re-login. | The fleet's LLM editor, a ~3,400-line `run.sh` wrapped by `agent.py`. Applies rec batches through the claude-pool first, then the framework code-editor chain. **Required-model batches (article, news, H2H) defer instead of downgrading.** Code recs are committed and chained to the deployer; article, H2H and catalog-audit recs become DB rows. | [README.md](implementer/README.md). `AGENT.md` is the LLM prompt; `ARTICLE_AUTHOR.md`, `H2H.md` and `CATALOG_AUDIT.md` are per-kind prompts. |
| `deployer` | Chained from `implementer/run.sh` after a batch that commits code. Runtime id is `seo-deployer`. Registry id `deployer` has `enabled=false` and no timer. | Live via the implementer. The `seo-deployer` run-index keeps the last 50 runs, which span 2026-09-19 to 09-23: 30 success, 20 failure. 16 of the failures are `deploy blocked` (the test gate). | `run.sh` is a shim that execs the AgentBase wrapper `agent.py` (id `seo-deployer`), which drives `deployer.py`. Runs the site's `deployer:` block: test → build → push → deploy (with revision post-check) → smoke check. Then pushes, tags `release/<site>/NNNN` and marks the batch's recs shipped. It is skipped for `article-author`, `catalog-audit` and `h2h` dispatches, when `IMPLEMENTER_SKIP_DEPLOY=1` is set, or when the batch made no commits. | [AGENT.md](deployer/AGENT.md) |

## 3. Shared engines and their per-site instances

An **engine** is AgentBase (or plain Python) code in this repo with no
schedule of its own. A **per-site instance** is a directory in a site repo that
holds `manifest.json` plus `site.yaml` (or just a thin `run.sh`). Its
`entry_command` points back at the engine's `agent.py`. The engine runbook
describes the code; the instance runbook describes that site's config and
current behaviour.

### Engines

| Engine dir | Registry state | What it does | Engine runbook |
|---|---|---|---|
| `catalog-audit-agent` | Registry entry with `enabled=false` and no timer. | Runs a site's own catalog-audit script, converts findings (AislePrompt JSON or SpecPicks image-mismatch CSV) into `rec-NNN` recs, emails them through the digest gate, and dispatches via `gated_dispatch_now` (kind `catalog-audit`). | [README.md](catalog-audit-agent/README.md) |
| `category-integrity-agent` | No manifest; not registered. | Nulls `category_id` on unscored products whose titles match foreign-domain words (500 per run at most). Emits a `categorisation-pipeline-gap` rec when more than 25% of categorised products were never scored. | [AGENT.md](category-integrity-agent/AGENT.md) |
| `competitor-research-agent` | Blueprint manifest (`is_blueprint`); not registered (404). | Crawls our site and competitors' sites, extracts feature lists with local ollama, asks claude-cli for blueprinted parity, advantage, UX and content-gap recs, keeps a cross-run proposal accumulator, and emails the top open proposals (bypassing the digest). Crawler and compare fixes landed in `7d760fd` (2026-09-23 17:59Z) and no scheduled run had confirmed them at audit time. | [AGENT.md](competitor-research-agent/AGENT.md) |
| `ebay-product-sync-agent` | Registry entry with `enabled=true` but an empty cron, so no timer. | Searches the eBay Browse API and upserts canonical `products` plus short-lived `ebay_listings`, with LLM canonicalisation over an operator-approved mapping. Then audits and reaps stale listings. `main()` calls `run()` directly, bypassing `run_once()`. | [README.md](ebay-product-sync-agent/README.md) |
| `feedback-triage-agent` | No manifest; not registered. | Atomically claims `new` feedback rows and classifies them with deterministic regex rules in `triage.py` (no LLM). Emits `user-feedback-defect` recs for backlog-dispatcher. | [AGENT.md](feedback-triage-agent/AGENT.md) |
| `gsc-coverage-auditor` | No manifest; not registered. `run.sh` and `submit-sitemap.py` are legacy. | AgentBase wrapper around `inspect.py`. Resubmits sitemaps, inspects the 150 least-recently inspected URLs via GSC URL Inspection, and appends `coverageState` to a local JSONL that the SEO analyzer reads. | [AGENT.md](gsc-coverage-auditor/AGENT.md) |
| `indexnow-submitter` | No manifest here. A legacy registry entry `indexnow-submitter` (repo_dir `nsc-assistant/agents/indexnow-submitter`, `enabled=false`, last run 2026-08-25) is still present. `submit.sh`, `sites.json` and `queue-publish.py` are legacy. | AgentBase wrapper around `submit.ts`. POSTs watermarked DB, static and sitemap-delta URLs to api.indexnow.org, checks canonical-vs-sitemap coverage, then HEAD-checks and GSC-resubmits sitemaps. The URL universe comes from each site's `agents/seo-config/site-indexnow.json`. | [AGENT.md](indexnow-submitter/AGENT.md) |
| `product-hydration-agent` | Blueprint manifest; not registered (404). | Refreshes stale Amazon prices (creators, paapi or brightdata) and hydrates product description, pros/cons, FAQ and SEO meta with one opus call per product. Optionally runs a site `is_featured` script. | [AGENT.md](product-hydration-agent/AGENT.md) |
| `progressive-improvement-agent` | Blueprint manifest; not registered (404). | BFS-crawls a site with `crawler.py` (also imported by competitor-research and seo-opportunity), LLM-audits changed pages in batches against a base prompt plus the site's `qa_detection_rules`, and writes ranked `recommendations.json`. Miscategorisations are handed off to `<site>-catalog-audit-agent`. | [AGENT.md](progressive-improvement-agent/AGENT.md) |
| `search-demand-agent` | No manifest; not registered (404). | Deterministic, no LLM. Distils 28 days of GSC and GA4 data into template winners, steer topics, zero-coverage and strike-distance queries, and hot `/vs/` pairs, published to `framework/demand-signal/<site>.json`. | [AGENT.md](search-demand-agent/AGENT.md) |
| `seo-opportunity-agent` | No manifest; not registered. | The SEO and revenue engine (collect → analyze → finalize, with phase bodies in `lib/`). Pulls GSC, GA4, DB and live-site signals, runs rule passes plus an LLM page audit, writes up to 12 recs, and queues an HTML report to the digest. The reference collapsed pipeline named in `CLAUDE.md`. | [AGENT.md](seo-opportunity-agent/AGENT.md); rec-type catalog in [README.md](seo-opportunity-agent/README.md) |
| `shelf-audit-agent` | Manifest with `enabled=false` and no cron; not registered (404). | Crawls a site 3 levels deep plus JSON shelf endpoints, checks every reachable product against the Amazon Creators API (price drift, image, stock, title), and emits one rec per issue type. When `dispatch_findings` is true it dispatches up to 3 high-severity recs per run. | [README.md](shelf-audit-agent/README.md) |
| `site-goals-tracker` | No manifest; not registered under its own id. | Plain Python, not AgentBase. Refreshes a site's goal definitions, then records GSC 30-day clicks and impressions, GA4 sessions, conversion clicks, active page count and GSC coverage via `metric_helper.record_many`. Site values are hard-coded in `SITE_PROFILES`. | [AGENT.md](site-goals-tracker/AGENT.md) |

### Per-site instances

| Engine | Registered id | Instance dir | Schedule | Status (2026-09-23) | Instance runbook |
|---|---|---|---|---|---|
| catalog-audit-agent | `aisleprompt-catalog-audit-agent` | `aisleprompt/agents/catalog-audit-agent` | Every 5 h at :02 | Live. Re-dispatches the same 7 recs every tick (4 duplicate migration commits on 2026-09-23). | `README.md` |
| catalog-audit-agent | `specpicks-catalog-audit-agent` | `specpicks/agents/specpicks-catalog-audit-agent` | Daily 08:30 | Live but blind: claude-pool auth failures turn almost every image verdict into `unsure`, so each run reports clean. | `README.md` |
| category-integrity-agent | `specpicks-category-integrity-agent` | `specpicks/agents/category-integrity-agent` | Every 4 h at :35 | Live. 0 de-categorised; 53.3% of the catalogue never scored; the pipeline-gap rec is re-emitted every run. | `README.md` |
| competitor-research-agent | `aisleprompt-competitor-research-agent` | `aisleprompt/agents/competitor-research-agent` | Every 5 h at :04 | Degraded 2026-09-18..23: 1 of 8 competitors crawled, last run a failure. `7d760fd` was not yet confirmed. The backlog holds 2,194 open proposals. | `README.md` |
| competitor-research-agent | `specpicks-competitor-research-agent` | `specpicks/agents/competitor-research-agent` | Daily 06:30 | Degraded 2026-09-18..23: 0 competitors crawled. `7d760fd` was not yet confirmed. | `README.md` |
| ebay-product-sync-agent | `specpicks-ebay-product-sync-agent` | `specpicks/agents/ebay-product-sync-agent` | Hourly at :30 | Live. The 12:30 EDT run synced 116 products; the 12 runs before it got 0 while ollama was unreachable. | `README.md` |
| feedback-triage-agent | `aisleprompt-feedback-triage-agent` | `aisleprompt/agents/feedback-triage-agent` | Every 20 min | Live; 0 reports claimed. No goals declared. | `README.md` |
| feedback-triage-agent | `specpicks-feedback-triage-agent` | `specpicks/agents/feedback-triage-agent` | Every 20 min | Live; 0 claimed; has never produced a rec. No goals declared. | `README.md` |
| gsc-coverage-auditor | `aisleprompt-gsc-coverage-auditor` | `aisleprompt/agents/gsc-coverage-auditor` (thin `run.sh`) | Daily 11:30 | Live. Indexed 0.04% of 5,003 inspected URLs. | Symlinks to the engine `AGENT.md` |
| gsc-coverage-auditor | `specpicks-gsc-coverage-auditor` | `nsc-assistant/agents/specpicks-gsc-coverage-auditor` (thin `run.sh`) | Daily 12:00 | Live. | Symlinks to the engine `AGENT.md` |
| indexnow-submitter | `aisleprompt-indexnow-submitter` | `aisleprompt/agents/indexnow-submitter` (thin `run.sh`) | Every 15 min | Live. About 274 URLs per tick with 0 failed. | Symlinks to the engine `AGENT.md` |
| indexnow-submitter | `aisleprompt-indexnow-bulk` | `aisleprompt/agents/indexnow-bulk` (`INDEXNOW_BULK=1`) | Daily 09:00 | Live. About 197k URLs per run with 0 failed. | `AGENT.md` |
| indexnow-submitter | `specpicks-indexnow-submitter` | `nsc-assistant/agents/specpicks-indexnow-submitter` | Every 5 h at :18 | Live. | Symlinks to the engine `AGENT.md` |
| indexnow-submitter | `specpicks-indexnow-bulk` | `nsc-assistant/agents/specpicks-indexnow-bulk` | Daily 10:00 | Live. | Symlinks to the engine `AGENT.md` |
| product-hydration-agent | `specpicks-product-hydration-agent` | `specpicks/agents/product-hydration-agent` (thin `run.sh`) | Every 2 h at :15 | `partial_failure` on every run 2026-09-19..23. Most runs hydrate 72–78 of 80 products (the 2026-09-23 10:15Z run managed 44), but 0 prices are refreshed because BrightData returns HTTP 401 (per the instance runbook). | `AGENT.md` |
| progressive-improvement-agent | `aisleprompt-progressive-improvement-agent` | `aisleprompt/agents/progressive-improvement-agent` | Every 2 h at :45 | Live. 40 pages, 6 recs, quality score 85. | `README.md` |
| progressive-improvement-agent | `specpicks-progressive-improvement-agent` | `specpicks/agents/progressive-improvement-agent` | Daily 05:30 | Live. 80 pages, 15 recs, quality score 81.25, about 82 min per run. | `README.md` |
| search-demand-agent | `specpicks-search-demand-agent` | `specpicks/agents/search-demand-agent` (thin `run.sh`) | 05:35 and 17:35 | Live. The 09:35Z run found 20 topics with 4 uncovered. Feeds `specpicks-article-proposal-agent`. | `AGENT.md` |
| seo-opportunity-agent | `aisleprompt-seo-opportunity-agent` | `aisleprompt/agents/seo-opportunity-agent` | Every 2 h at :15 | Live. Handoffs to generic handler ids are dead-lettered. | `AGENT.md` |
| seo-opportunity-agent | `specpicks-seo-opportunity-agent` | `specpicks/agents/seo-opportunity-agent` | Every 3 h at :30 | Live. `site.yaml` has no implementer `allowed_paths`. | `AGENT.md` |
| shelf-audit-agent | `aisleprompt-shelf-audit-agent` | `aisleprompt/agents/shelf-audit-agent` | Every 8 h at :55 | Live, report-only (`dispatch_findings: false`). No goals declared. | `README.md` |
| shelf-audit-agent | `specpicks-shelf-audit-agent` | `specpicks/agents/shelf-audit-agent` | Every 8 h at :25 | Live, dispatching since 2026-08-30. 7 of the last 50 runs failed with "shelf discovery found no products". | `README.md` |
| site-goals-tracker | `aisleprompt-site-goals-tracker` | `aisleprompt/agents/site-goals-tracker` (thin `run.sh`) | Daily 13:00 | Live. Recorded 17 metrics. | `AGENT.md` |
| site-goals-tracker | `specpicks-site-goals-tracker` | `nsc-assistant/agents/specpicks-site-goals-tracker` (thin `run.sh`) | Daily 13:00 | Live. | `AGENT.md` |

**Config-only site directories that engines read:**
- `aisleprompt/agents/seo-config/` and `specpicks/agents/seo-config/`: each holds
  `site-indexnow.json`, the URL universe used by `indexnow-submitter` and
  `gsc-coverage-auditor`. They are not agents; each has a `README.md`.

## 4. Blueprints, templates, and non-agents

| Dir / path | What it is | Runbook |
|---|---|---|
| `jcode-agent` | Blueprint-flagged AgentBase wrapper around one `jcode run --json` task. Not registered (404), never run, and `jcode` is not installed on the fleet host. Its success path passes `output=` to `RunResult`, which has no such field, so a successful run would report failure. Separate from the implementer's `jcode-*` code-editor backends. | [AGENT.md](jcode-agent/AGENT.md) |
| `seo-analyzer` | Not an agent: a legacy standalone copy of the SEO analyzer phase with no manifest and no live caller. The only reference is `specpicks/agents/seo-opportunity-agent/run.sh`, which the systemd unit does not use (it runs the engine's `agent.py` directly). As of 2026-09-23 it differs from `seo-opportunity-agent/lib/analyzer/` by one docstring. | [README.md](seo-analyzer/README.md) |
| [`../_template/agent/`](../_template/agent/) | The scaffold that `install/create-agent.sh` copies: `manifest.json`, `AGENT.md`, `SKILL.md`, `README.md`, `requirements.txt`, `run.sh` and `agent.py.template`. | n/a |
| [`../blueprints/`](../blueprints/README.md) | Seven agent patterns. `blueprints/README.md` indexes five of them (site-quality-recommender, pipeline-stage, inbox-poller, llm-code-editor, scheduled-task). The other two are `app-store-opportunity-finder` (reference: `app-store-opportunity-agent`) and `competitor-research-with-accumulator` (reference: `competitor-research-agent`). | Each has a `BLUEPRINT.md` |

Manifests flagged `metadata.is_blueprint: true` are skipped by
`install/register-all-from-dir.sh`. In this directory that means
`competitor-research-agent`, `deployer`, `implementer`, `jcode-agent`,
`product-hydration-agent` and `progressive-improvement-agent`. Some of them
still have older registry entries; see the tables above.

## 5. Archived

| Dir | What it was | State |
|---|---|---|
| `_archive/reusable-agents-competitor-research-agent` | A "self-improvement" instance of `competitor-research-agent` in codebase mode. It compared this repo with workflow platforms (n8n, Temporal, Airflow, …). | Moved to `_archive/` on 2026-05-13 (commit `ab2fc5e`). The manifest has `enabled=false` and no cron. A registry entry with the same id is still present (`enabled=false`, cron `26 */5 * * *`, repo_dir pointing at the pre-archive path); it has no timer. See its [README.md](_archive/reusable-agents-competitor-research-agent/README.md). |

`register-all-from-dir.sh` only looks one level down, and `_archive/` has no
`manifest.json` at that level, so nothing in it is re-registered.

## 6. Scheduled agents that live in other repos

Of the 69 `agent-*.timer` units on the fleet host (2026-09-23), **34 run
code from this directory**: the framework agents in §1 and the instances in §3.
The other 35 run site-specific code that lives, with its runbooks, in the site
repos:

| Repo | Count | Registered ids |
|---|---|---|
| `aisleprompt/agents/` | 12 | `aisleprompt-` + article-hero-image-curator, article-proposal-agent, conversion-optimizer, kitchen-instacart-backfill, kitchen-scraper, promo-curator-agent, recipe-generator-agent, recipe-image-archiver, recipe-image-refiller, recipe-image-verifier, user-growth-strategist; plus `trending-recipe-discovery` (no site prefix) |
| `specpicks/agents/` | 21 | `specpicks-` + ai-news-aggregator, amazon-catalog-freshness, amazon-price-verifier, article-hero-image-curator, article-proposal-agent, benchmark-research-agent, competitor-gap-consumer, content-accuracy-auditor, ebay-counterpart-matcher, head-to-head-agent, internal-link-densifier, news-hero-image-curator, newsletter-digest-sender, reddit-reply-queue, schema-fix-specialist, scraper-watchdog (timer disabled), site-functional-tests, stale-content-watcher, trending-topic-pipeline, user-growth-strategist, youtube-review-agent |
| `nsc-assistant/agents/` | 2 | market-research-pipeline, sessions-save (every 5 min via `OnUnitActiveSec`, not a cron) |

For a live list, run `systemctl --user list-timers --all | grep agent-`, or
`curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://127.0.0.1:8090/api/agents`.
[`../docs/agents-catalog.md`](../docs/agents-catalog.md) is the fleet-wide
catalog: one row per timer for all 69, grouped by function, re-verified
2026-09-23.

## 7. Adding and registering an agent

**Where it goes**
- Fleet-wide behaviour, not tied to one site: a new dir here.
- Site-specific logic: `<site>/agents/<id>/` in the site repo.
- Another site using an existing engine: a per-site instance dir in that site's
  repo with `manifest.json` and `site.yaml`. Its `entry_command` sets the
  engine's config env var (for example `SEO_AGENT_CONFIG` or
  `PROGRESSIVE_IMPROVEMENT_CONFIG`) and runs the engine's `agent.py`.
- Site differences belong in config (`site.yaml`, storage config, manifest
  fields), never in `if site == ...` branches. See
  [`../docs/repo-boundaries.md`](../docs/repo-boundaries.md) and `../CLAUDE.md`.

**Scaffold**

Pick a pattern from [`../blueprints/README.md`](../blueprints/README.md), then:

```bash
bash /home/voidsstr/development/reusable-agents/install/create-agent.sh \
    <agent-id> <repo>/agents --name "<Display Name>" \
    --description "<one line>" --category <seo|research|fleet|personal|ops|misc> \
    --cron "<cron-expr>" --timezone "<tz>" --owner "<email>" --kind python
```

Every registered agent must subclass `framework.core.agent_base.AgentBase`
(`../CLAUDE.md`). `--kind bash` is only for a thin env wrapper that ends in
`exec python3 .../agent.py`. Declare 3–7 goals, each with a `target_metric`,
before shipping (`install/seed-default-goals.sh` or
`PUT /api/agents/<id>/goals`).

**Register**

```bash
set -a; . ~/.reusable-agents/secrets.env; set +a   # exports FRAMEWORK_API_URL (:8090) and FRAMEWORK_API_TOKEN
# one agent
bash install/register-agent.sh <agent-dir>
# every agent dir under a parent
bash install/register-all-from-dir.sh <repo>/agents
```

- **Load the token first.** Without `FRAMEWORK_API_TOKEN` the API answers 401
  and `register-agent.sh` exits 1. (An unauthenticated `GET /api/agents`
  returned 401 on 2026-09-23.) `register-agent.sh` sends `Authorization:
  Bearer` only when `FRAMEWORK_API_TOKEN` is set, and exits 1 on any response
  other than HTTP 200. `register-all-from-dir.sh` counts each failure and exits
  non-zero at the end. `~/.bashrc` and `~/.profile` do not source
  `secrets.env`, so a fresh login shell has no token until you run the `set -a`
  line.
- On the fleet host the framework API listens on `127.0.0.1:8090`
  (`reusable-agents-api.service`), and `secrets.env` points `FRAMEWORK_API_URL`
  there. `register-agent.sh` also defaults to `http://localhost:8090`. Some
  older scripts and docs default to `:8093`, but nothing listens on 8093 on
  this host.
- **What registration does:** `POST /api/agents/register` stores the manifest in
  `registry/agents.json` and `agents/<id>/manifest.json`, and embeds
  `runbook.md`, `skill.md` and `readme.md`. If `cron_expr` and `entry_command`
  are both set, it writes `~/.config/systemd/user/agent-<id>.{service,timer}`.
  The service runs `framework/agent_run_wrapper.sh <id> <entry_command>` with
  `EnvironmentFile=-~/.reusable-agents/secrets.env` and logs to
  `/tmp/reusable-agents-logs/agent-<id>.log`. The timer is then enabled, or,
  when `enabled=false`, stopped and disabled.
- **What `register-all-from-dir.sh` skips:** subdirs with no `manifest.json`;
  `lib`, `_legacy-*`, `_template`, `tests` and `*.bak`; and manifests with
  `metadata.is_blueprint`.
- **Order matters:** duplicate ids are last-write-wins. `agent-doctor` and
  `responder-agent` also have manifests in `nsc-assistant/agents/` with an
  empty cron, so register this repo **last**. `install/standup-fleet-host.sh
  register` uses the order aisleprompt, specpicks, nsc-assistant, reusable-agents.
- **Host drop-ins:** re-registering rewrites the unit files. It leaves
  `*.timer.d/` drop-ins alone, but `enabled=false` in the manifest disables the
  timer anyway (see `digest-rollup-agent` above).

**Verify**

```bash
set -a; . ~/.reusable-agents/secrets.env; set +a   # exports FRAMEWORK_API_URL (:8090) and FRAMEWORK_API_TOKEN
systemctl --user list-timers --all | grep agent-<id>
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://127.0.0.1:8090/api/agents/<id>
tail -n 50 /tmp/reusable-agents-logs/agent-<id>.log
```

A clean `systemctl start` does not prove the run worked. Check the run's
output (`progress.json`, recs, DB impact), not just the exit status.
