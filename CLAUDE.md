# Claude Instructions — reusable-agents framework

> ## 🏗️ HOST STANDUP — `/setup-fleet-host` (read before "why is nothing running?")
>
> **The fleet host is the machine that EXECUTES agents.** Azure holds state +
> the read-only dashboard; the host holds the timers, the framework API that
> writes them, the host-worker, the drainer, and `~/.reusable-agents/`. When
> the host is gone the sites still serve 200 — the only symptoms are absences
> (publish volume 0, no IndexNow, decaying organic traffic). **Diagnose "is
> there a host at all?" before diagnosing individual agents.**
>
> Runbook: `.claude/skills/setup-fleet-host/SKILL.md` · narrative + operator
> checklist: `docs/fleet-host-standup.md` · scripts:
> `install/standup-fleet-host.sh` (phases: preflight repos deps secrets api
> register spine verify | all) and `install/recover-credentials.sh`
> (harvest | status | backup).
>
> **Current host (2026-08-13 →):** `whitebeast`, WSL2 Ubuntu 24.04, repos at
> `/home/voidsstr/development` on **ext4**. Not `/mnt/c` (9p, ~95× slower on
> small writes), not a NAS (no POSIX locking; one blip fails every agent).
> The path is a CONTRACT — every manifest `entry_command`, every registry
> `repo_dir`, and `scheduler.py`'s `EnvironmentFile=` hardcode it.
>
> **Non-obvious invariants** (full list in the skill §4): `secrets.env` values
> MUST be single-quoted (an Azure conn-string's `;` truncates on shell source);
> node from nvm is invisible to systemd — PATH + NODE_PATH come from the global
> drop-in `~/.config/systemd/user/service.d/10-fleet-path.conf`; register agents
> from the REPOS (the blob registry drifts), with the framework repo LAST
> because duplicate ids are last-write-wins; a clean `systemctl start` is NOT
> proof a run worked (`EnvironmentFile=-` tolerates a missing file).
>
> Standup ends where KTLO begins — hand off as soon as `verify` is green.

> ## 🛰️ 24/7 on-call skill — `/keep-the-lights-on` (KTLO)
>
> **The one skill for "keep the agents/sites running, fix things while I'm
> away, improve them toward their goals."** Runbook:
> `.claude/skills/keep-the-lights-on/SKILL.md`; overview + re-auth runbooks
> + optimization workflow: `docs/keep-the-lights-on.md`. Invoke when the
> operator says "keep the lights on", "monitor <system>", "keep them running
> 24/7", "you're on call for <system>", "why is publish volume zero", or when
> a prior loop wakes itself via `ScheduleWakeup` (re-invoke the SAME args every
> tick to sustain the loop).
>
> **Three live systems** (registry: `.claude/skills/keep-the-lights-on/systems/registry.yaml`):
> `aisleprompt` (site) · `specpicks` (site) · `retro-chat` (daemon —
> `retro-chat-daemon.service`, health = active AND actively discovering, not a
> hung zombie). One combined session can keep all three lit: one status box
> per system, one `ScheduleWakeup` at the shortest cadence needed. A new
> session starts KTLO by invoking the skill and naming the systems.
>
> The skill carries: the token-frugal two-layer tick (cheap shell/SQL every
> tick, LLM only on a break or the improvement window); the Opus-only authoring
> + framework-first quality contract; the auto-fix playbook; the **incident
> playbook library** (§11 — disk-full/ENOSPC fleet recovery, claude-pool
> re-auth, GSC weekly-token expiry, deploy-gate `:4001` fixture, kitchen-scraper
> timeout, transients); daily Playwright `@smoke` verification; email+in-session
> escalation with de-dupe; and the improvement cycle ("a change that doesn't
> move a goal's metric didn't happen"). State persists in
> `agents/keep-the-lights-on/<system>/` so a fresh session resumes without
> double-alerting.
>
> **Standing operator-action items** (agent can't do — sudo/interactive auth;
> current list lives in each system's `incidents.json`): re-auth the claude-pool
> (`python3 -m framework.cli.claude_pool login-help`), GSC token
> (`install/fix-gsc-now.sh`), 265G ollama-dup disk reclaim.

> ## 🎯 NORTH STAR — READ EVERY SESSION 🎯
>
> Everything we build drives user usage of the configured sites (today
> aisleprompt.com + specpicks.com; new sites slot in via per-site
> config). Success = site metrics — DAU, organic clicks, indexed pages,
> conversions, returning users — *not* code shipped, runs completed, or
> tokens consumed.
>
> **Goals are the prioritization lens.** Every agent declares 3–7
> long-running goals at registration; each goal's `target_metric` points
> at a key in `RunResult.metrics` so the framework auto-records progress
> on every run. The Goals tab is the single pane of glass for "is this
> agent moving the needle?"
>
> **Decision procedure for any feature / refactor / bugfix:**
> 1. Open the Goals tab (or `GET /api/agents/<id>/goals` + `/goals/cache`).
> 2. Find a stalled goal (large target gap, flat recent progress); bias
>    toward goals that map to user-facing site metrics.
> 3. Trace it to the agent owning its `target_metric`; read `run()`, find
>    the bottleneck (under-batching, missing handoff, low cap, no
>    short-circuit).
> 4. Make the smallest change that moves the metric; verify it moved over
>    1–3 runs. **A change that doesn't tick a goal didn't happen.**
>
> Refuse on sight: "refactor for cleanliness" with no goal it unblocks;
> "add a feature the user might want" with no metric it serves; wiring an
> agent before declaring goals + `target_metric` bindings; "fix a bug" via
> an `if site == "x":` branch in framework code (lift to config).
>
> See **README.md → "Goals & metrics — the north star"** for the pipeline,
> layer A vs B metric capture, and how `init_goals` merges.

> ## ⚠️ AGENTBASE IS MANDATORY — READ EVERY SESSION ⚠️
>
> Every registered agent MUST subclass
> `framework.core.agent_base.AgentBase`. Bash-orchestrated multi-stage
> pipelines, scripts writing `status.json` via `framework.cli.status`, and
> synthesizing run summaries from work-product files instead of
> `progress.json` are forbidden — they produce runs invisible in the
> dashboard, no `run-index.json`, no heartbeat, no auto goal-progress, no
> verification scripts. (Synthesis workarounds were retired 2026-05-04;
> don't reintroduce them.)
>
> **Decision tree (every new agent or refactor):**
> 1. **Is it an agent?** Cron / run-history / status / triggers →
>    AgentBase. One-shot CLI tool (e.g. `agent/tools/wake_on_lan.py`) →
>    not an agent, no manifest, not in `agents/`.
> 2. **Multi-stage?** Stages are phases inside one `run()`, sharing one
>    `run_ts` / `progress.json` / run-index entry — NOT separate agents.
>    If cross-stage state is too coarse, lift the contract into
>    `framework/core/` as a primitive.
> 3. **entry_command invokes `bash`?** Only OK if it's a thin env wrapper
>    ending in `exec python3 .../agent.py`. Bash that orchestrates stages,
>    writes status, or shells to multiple scripts is banned.
> 4. **About to write `if has_progress_json else synthesize`?** STOP — fix
>    the producer to write `progress.json` via `post_run()`.
>
> **Reference — collapsed pipeline:** `agents/seo-opportunity-agent/`
> (read `agent.py` + `finalizer.py`). Three former agents + a per-site
> `run.sh` collapsed into one AgentBase subclass with phases collect →
> analyze → finalize; phase bodies live under
> `lib/{collector,analyzer,reporter}/` (subprocess-called by
> `_run_phase()`).
>
> **Cross-agent contracts live in `framework/core/`** — use the AgentBase
> wrappers, never inline the storage path:
> - `implementation_queue.py` → `queue_recs()` / `self.queue_recs(...)`
> - `digest_queue.py` → `queue()` / `self.queue_for_digest(...)`
> - `outbound_email.py` → `record()` / `self.record_outbound(...)`
> - `handoff.py` (typed handoffs), `short_circuit.py` (snapshot hashing +
>   replay)
>
> **Conversion backlog** (lift to AgentBase only when next changed — no
> side-quests): `gsc-coverage-auditor` (+ per-site), `daily-briefing-
> calendar-agent`, `daily-status-briefing`, `fix-submission-agent`,
> `retro-agent-orchestrator`, `indexnow-submitter` (+ per-site + bulk),
> `external-game-cataloger`, `game-library-scanner`,
> `market-research-pipeline`, `real-estate-agent`,
> `retro-multiplayer-refresh`, `security-scanner-pipeline`, `web-search`,
> `product-hydration-agent`, `scraper-watchdog`. (`responder-agent`,
> `implementer`, `seo-deployer` converted 2026-05-04 — AgentBase wrappers;
> the implementer's `run.sh` is still the heavy-lifter inside.)

> ## ⚠️ FRAMEWORK-FIRST POLICY — READ EVERY SESSION ⚠️
>
> Evaluate every change for framework abstraction BEFORE the first line:
> 1. Could another site/agent want this? Yes → `framework/core/` or
>    `framework/cli/`, configurable per deployment.
> 2. Is the LOGIC site-specific, or only the VALUES? Logic → framework
>    with knobs. Values (DSNs, brand voice, slugs) → site repo /
>    per-deployment config.
> 3. Would another deployment have to fork to get value? Yes → refactor as
>    primitive + config NOW, not later.
>
> **Required pattern — primitive + config + extension point.** Every new
> framework feature ships as: a generic primitive in
> `framework/core/<name>.py`; a storage-backed `config/<name>-config.json`
> override; a manifest field (in `registry.py` `AgentManifest`) when
> per-agent override matters; a `framework/cli/<name>.py` entry when shell
> callers need it; and sensible default config out of the box.
>
> Refactor on sight: `if site == "specpicks":` in framework code;
> hardcoded agent-id lists in `host-worker.sh` / `responder.py` / anywhere
> in `framework/`; duplicate logic across two site `agent.py`s;
> site-specific tier/priority/threshold constants; "I'll abstract it later."
>
> Reference impls: `framework/core/priority.py` + `framework/cli/priority.py`
> + `config/priority-config.json`; `framework/core/short_circuit.py`;
> `framework/core/work_types.py` + per-site `handoff_routes`;
> `framework/core/article_mentions.py`.
>
> **Contract with every site:** sites are config consumers, never
> code-fork targets — they contribute `manifest.json` + `site.yaml` +
> optional `prompts/*.md`, never logic/branching embedded in framework
> code. If a feature can't be expressed via (a) a storage config, (b) a
> manifest field, or (c) a `site.yaml` knob, add the missing extension
> point first. Applies to EVERY change, even one-site fixes. No site is
> privileged; aisleprompt and specpicks are reference deployments.

## Article authoring — timely > evergreen

Articles must hook into what's happening NOW (seasonal moments, holidays,
trending searches), not just evergreen "Best X Guide" pages. A
`seasonal-occasion` bucket with no Memorial Day cookout guide on Memorial
Day weekend is a failure even if 50 other articles shipped. Four
primitives back this:

1. **`framework/core/seasonal_calendar.py`** — anchored US calendar:
   fixed-date (Cinco de Mayo, July 4th) + relative-date
   (`last-weekday:monday:5`=Memorial Day, `nth-weekday:4:thursday:11`=
   Thanksgiving). Windows NOW (±3d), IMMINENT (4–14d), UPCOMING (15–60d).
   Each occasion carries `recipe_keywords`, `link_categories`,
   `bucket_hints`, `audience` (food/tech/general). Authors call
   `active_signal(today, audience=...)` and inject `build_prompt_block(...)`
   into the LLM user message — without it the LLM defaults to evergreen.
2. **`framework/core/trends_signal.py`** — Google Trends RSS + audience
   Reddit RSS (r/cooking, r/buildapc, r/gaming). Cached 6h at
   `agents/<id>/state/trends-cache.json`. Authors call
   `fetch_trends_cached(self.storage, self.agent_id, audience=...)`; the
   `TRENDING TODAY` block biases proposals toward uncovered demand.
3. **`framework/core/featured_rotation.py`** — promotes
   `editorial_articles.featured=true` for articles tagged `holiday:<id>`
   when that holiday is NOW/IMMINENT; demotes stale holiday features;
   leaves operator-pinned (featured, no `holiday:` tag) alone. Runs
   end-of-tick via `cycle_homepage_features(self._db_dsn, audience=...,
   max_features=N)`.
4. **`framework/core/article_link_guard.py`** — every shipped article
   needs ≥5 inline `/recipes/<slug>` + ≥2 `/k/<slug>` links (aisleprompt
   defaults; specpicks 0/0, uses `/product/`). Hooked into implementer
   `run.sh` post-write: failing articles are skipped from INSERT and
   re-queued with a failure addendum naming the slugs to add. No inline
   links = content dead-end (no link equity, no expansion, no conversion).

Hard rules for any new article/news/editorial proposer:
- ❌ Don't roll your own holiday list — call `seasonal_calendar.active_signal()`.
- ❌ Don't roll your own RSS scraper — call `trends_signal.fetch_trends_cached()`.
- ❌ Don't ship without a `holiday:<id>` tag rule in the system prompt —
  else `featured_rotation` has nothing to cycle.
- ❌ Don't accept LLM output missing `expected_recipe_slugs` /
  `expected_kitchen_slugs` — the link-guard rejects it anyway.
- ❌ Don't loosen link-guard minima per-site without a real reason.

**Dedup tuning:** title-similarity dedup MUST exempt holiday-bearing
proposals (memorial, july, thanksgiving, …) or seasonal titles get killed
as near-dups of meal-prep articles (shared `Recipes + Shopping List`
suffix). Aisleprompt's `_dedup_proposals_by_title` has the exemption —
copy it for new bucket types.

**SSR renderers MUST support GFM tables.** If a site's `mdToHtml` only
handles headings/lists/paragraphs, `| header |` tables render as
`<p>literal pipes</p>` — breaks AggregateRating + comparison-table
content. On every new-site onboarding, render a test article with a table
and verify `<table>` in the SSR output. Both sites emit
`<table class="md-table">` / `<table class="ssr-table">` with thead+tbody.

**Hero images — `article-hero-image-curator` is the safety net.** Authors
pick a hero via SearxNG (`_search_blog_image`) best-effort; on miss/junk
the article ships with NULL `hero_image_url`. The curator runs every
15 min, vision-verifies + replaces, mirrors winners to Azure Blob.
Requirements: reject `medbay`/`scifi`/`spaceship`/`medical`/`hospital`,
NSFW (`porn`/`xxx`/`nsfw`/`sexy`/`lingerie`/`nudity`/`erotic`/`fetish`),
people (`headshot`/`portrait`/`staff`/`bio`/`profile`); min size 50KB
(smaller = thumbnail/icon, renders broken at 80×80). See
`framework/agents/aisleprompt/article-hero-image-curator/`. Bulk fix:
`/tmp/reimage-bulk.py --site <site> --table editorial_articles --include broken,tiny --commit`.

## Retro / pre-2012 hardware → eBay, not Amazon

Hardware released before 2012 defaults to eBay buy-links, not Amazon.
Amazon doesn't stock active listings for 20+ year-old SKUs (GeForce 4 Ti
4600, Voodoo3, Pentium 4, Audigy 2 ZS); the used/refurb retro market lives
on eBay. A "Buy on Amazon" CTA on a 2002 GPU lands on a dead page — $0
commission, broken-site trust hit, and Google flags the Offer JSON-LD as
deceptive structured data.

**Render-time routing — every hardware-buy CTA across SSR:**
1. `release_year<2012` OR `era='retro'` OR `listing_preference='ebay'` OR
   retro slug (`retro-*`, `voodoo*`, `geforce-[1-4]-*`, `pentium-*`,
   `audigy-*`, `radeon-[1-9]xxx`) → eBay CTA (`ebay_url` ||
   `ebay_search_url` || synthesized search by title). NEVER Amazon.
2. Else active Amazon listing (`is_active=true`, `price IS NOT NULL`) →
   Amazon affiliate CTA (`tag=specpicks-20`).
3. Else modern but OOS → "Check Amazon for current price" affiliate link
   (still earns on the click-through).

Enforce at: PDP hero (`productBody`), benchmark hero
(`benchmarkHardwareBody`) + FAQ "where to buy", vertical landing picks
(`verticalBody` `ssr-vpick-ctas`), `/category/retro-*` picks+cards,
`/buying-guide/retro-*` buttons, article body cross-sell
(`testbenchArticleBody`), homepage Editor's Picks rail.

**eBay API is wired — use it; don't tell the user "we'd need eBay access."**
Credentials in `~/.reusable-agents/secrets.env` (host-worker
`EnvironmentFile`) + `<specpicks>/agents/ebay-product-sync-agent/.env`
(gitignored, via `EBAY_PRODUCT_SYNC_CONFIG`). Env: `EBAY_CLIENT_ID`,
`EBAY_CLIENT_SECRET`, `EBAY_DEV_ID`, `EBAY_ENV`, `EBAY_MARKETPLACE_ID`
(default EBAY_US), optional `EBAY_CAMPAIGN_ID`. OAuth client_credentials
grant + token cache: `framework/agents/ebay-product-sync-agent/ebay_client.py`;
Browse API base `https://api.ebay.com/buy/browse/v1`.

Agent `specpicks-ebay-product-sync-agent` (systemd timer, `:30` hourly)
ingests 16 retro PC-hardware categories + retro consoles/handhelds/
cartridges/controllers/emulation/CRT-upscalers as `asin='EBAY_<item_id>'`.
Verify: `systemctl --user list-timers | grep ebay-product-sync`.

DB markers (the contract): `era='retro'` (retro-* categories + any product
linked to `hardware_specs.release_year<2012`); `listing_preference='ebay'`
(primary channel); `ebay_search_url` (fallback, auto-backfilled from
title); `ebay_url` (direct item URL when known). Enforcement:
`specpicks-product-hydration-agent` populates `ebay_search_url` at ingest;
`specpicks-progressive-improvement-agent` has rule
`retro-hardware-no-ebay-cta` (2026-05-22) flagging retro pages with an
Amazon CTA + no eBay alternative.

Maintain flags as new retro products land:
```sql
UPDATE products SET era='retro', listing_preference='ebay'
 WHERE category_id IN (SELECT id FROM categories WHERE slug LIKE 'retro-%' OR slug='handheld-consoles')
   AND is_active=true AND (era IS NULL OR listing_preference IS NULL);

UPDATE products SET ebay_search_url =
       'https://www.ebay.com/sch/i.html?_nkw='
       || regexp_replace(replace(title,' ','+'),'[^A-Za-z0-9+]','','g') || '&_sacat=27386'
 WHERE era='retro' AND ebay_search_url IS NULL AND title IS NOT NULL;
```

Refuse on sight: hardcoded `${SITE_DOMAIN}/dp/${asin}` without a
release-year guard; "Buy on Amazon" copy on a `release_year<2012` page;
article autolink to a retro `/product/<ASIN>` stub with no price (route to
eBay search by name instead).

## Pricing integrity — never promote a product with an invalid price

Never crown a Top Pick / buying-guide pick with a broken or misleading
price. Incidents: INR listing showing "$659.00" on the CTA while the
header said "₹659.00"; $0/null-price Editor's Top Pick; $0.01 Atari 2600
games (cents/dollars bug); "Save -$50" negative discounts;
foreign-marketplace (CAD/GBP/EUR/INR/AUD/SGD) listings in US-affiliate hubs.

Mandatory filters at every visible-pick query layer (already in
`buyingGuideFallback` + `/api/categories` LATERAL hero — apply to any new
Top-Pick query):
```sql
WHERE p.is_active = true
  AND p.price IS NOT NULL AND p.price >= 1.0        -- no null/$0/sub-$1
  AND (p.currency IS NULL OR p.currency = 'USD')    -- US-marketplace only
  AND (p.original_price IS NULL OR p.original_price >= p.price)  -- no neg-discount
  AND COALESCE(p.category_confidence, 0) >= 0.5     -- categorically-correct
```

Currency display: use `priceLabel()` / `priceHtml()` in
`src/services/ssrRender.ts` (symbol map USD=$, CAD=CA$, GBP=£, EUR=€,
JPY=¥, INR=₹, SGD=S$, BRL=R$, MXN=MX$). Never hardcode
`$${price.toFixed(2)}` — the audit flags hardcoded `$` on non-USD products.

Bulk catalog hygiene (reuse when issues accumulate):
```sql
-- foreign-marketplace → deactivate + flag
UPDATE products SET is_active=false,
       quality_flags = COALESCE(quality_flags,'[]'::jsonb) || '["foreign-marketplace-YYYY-MM-DD"]'::jsonb
 WHERE is_active=true AND currency IS NOT NULL AND currency!='USD';
-- null/$0/sub-$1 → demote
UPDATE products SET category_confidence=0.3
 WHERE is_active=true AND (price IS NULL OR price<1.0) AND COALESCE(category_confidence,1.0)>=0.5;
-- negative discount → clear
UPDATE products SET category_confidence=0.3, original_price=NULL
 WHERE original_price IS NOT NULL AND price IS NOT NULL AND original_price<price;
-- sub-50¢ scraper bug → clear
UPDATE products SET category_confidence=0.3, price=NULL
 WHERE is_active=true AND price<0.50 AND price>0;
```

## What this repo is

A self-hostable framework for scheduled/triggered LLM agents. Agents
register from their home repos (nsc-assistant, specpicks, customer apps)
and inherit lifecycle, scheduling, status, inter-agent messaging,
confirmations, and a control UI.

Docs:
- `README.md` — overview + quickstart + manifest format
- `docs/architecture.md` — three-tier model (customer repos → framework →
  blob storage), lifecycle, storage layout, dispatch graph, end-to-end rec
  trace. Read once.
- `docs/repo-boundaries.md` — what goes in framework vs customer repo vs
  `~/.reusable-agents/`. Re-read whenever you wonder "framework or
  customer repo?"
- `docs/agents-catalog.md` — every agent, categorized, with code/manifest
  path + schedule
- `docs/keep-the-lights-on.md` — the 24/7 on-call (KTLO) runbook: starting a
  session, the 3 live systems (aisleprompt/specpicks/retro-chat), incident
  playbook library, GSC + claude-pool re-auth runbooks, and the
  agent-optimization-toward-goals workflow with worked examples
- `docs/seo-onboard-new-site.md` — add a site to SEO automation in 5 steps
- `blueprints/README.md` — pick a blueprint when scaffolding
- `install/glitchtip/README.md` — optional self-hosted error tracker +
  `crash-watcher-agent` (crash → fix → ship loop), with mobile-SDK wiring
  checklist
- `framework/core/handoff.py` + `work_types.py` — inter-agent handoff
  protocol (A calls `send_handoff()`, framework routes to B); see
  architecture.md § "Inter-agent handoffs". **New rec_type → also add to
  `DEFAULT_REC_ROUTING` in work_types.py.**

## Creating a new agent (in ANY repo)

Always start from a blueprint + the scaffold script — don't hand-roll
agent files. They enforce conventions (kebab-case ids, manifest schema,
AGENT.md structure, AgentBase lifecycle, capabilities, status/decision
protocol, confirmation gates). Read the blueprint's `BLUEPRINT.md` for
anti-patterns + integration points.

| Intent | Blueprint | Reference |
|---|---|---|
| audit site / send recs / auto-fix | `site-quality-recommender` | `progressive-improvement-agent` |
| compare to competitors / find gaps | `site-quality-recommender` | `competitor-research-agent` |
| one pipeline step (collect/analyze/report/ship) | `pipeline-stage` | `seo-data-collector`, `seo-analyzer` |
| poll an inbox / route replies | `inbox-poller` | `responder-agent` |
| apply approved recs as code edits | `llm-code-editor` | `implementer` |
| run a script on a cron | `scheduled-task` | most `nsc-assistant/agents/` |

Steps:
1. Confirm the home repo (agent dir at `<repo>/agents/<id>/`).
2. Confirm core fields: `id` (kebab-case, ≤63 chars, starts with a
   letter), one-line description, category (`seo`/`research`/`fleet`/
   `personal`/`ops`/`misc`), cron (empty for manual-only), owner email,
   kind (`python`=AgentBase / `bash`=entry script).
3. Scaffold:
   ```bash
   bash /home/voidsstr/development/reusable-agents/install/create-agent.sh \
       <agent-id> <repo>/agents --name "<Display Name>" \
       --description "<one line>" --category <category> \
       --cron "<cron-expr>" --timezone "<tz>" --owner "<email>" --kind <python|bash>
   ```
4. Edit generated files: `AGENT.md` (runbook); `agent.py` (implement
   `run()`; use `self.status`, `self.decide`, `@requires_confirmation` on
   dangerous methods) or `run.sh` (between markers); `manifest.json` (only
   for cron/category changes).
5. **Declare goals (REQUIRED).** 3–7 long-running goals, each with `id`
   (kebab-case, stable, never reused), `title`+`description`,
   `metric:{name,current,target,direction,unit}` (drives progress bars),
   `directives:list[str]` (what to DO each run; read by the LLM at run
   start). Seed via `install/seed-default-goals.sh` (so re-runs don't wipe
   progress) or PUT `/api/agents/<id>/goals`. Schema
   `shared/schemas/agent-goals.schema.json`. `run()` ends with
   `record_goal_progress(agent_id, goal_id, value, run_ts=...)`. No goals →
   purpose isn't legible → don't ship.
6. Register (idempotent — upserts + re-applies the systemd timer; port
   8093 because 8090 is held by application-research):
   ```bash
   FRAMEWORK_API_URL=http://localhost:8093 bash <repo>/agents/register-with-framework.sh
   # or the universal walker:
   FRAMEWORK_API_URL=http://localhost:8093 bash /home/voidsstr/development/reusable-agents/install/register-all-from-dir.sh <repo>/agents
   ```
7. Verify: UI http://localhost:8091/agents/<id>; `curl
   http://localhost:8090/api/agents/<id>`; `systemctl --user list-timers |
   grep agent-<id>`; Goals tab shows the objectives.
8. Commit the agent dir to its home repo.

Repos: `nsc-assistant` (`agents/<id>/`, register via
`scripts/register-agents.sh`); `specpicks` (`agents/<id>/`,
`agents/register-with-framework.sh`); customer apps inside nsc-assistant
(`Customer Applications/<app>/<ver>/agents/<id>/` when domain-specific,
else `nsc-assistant/agents/<id>/`).

Skip the scaffold only for: an explicit one-off/throwaway script (no
scheduling/status/registration); editing an existing agent (go straight to
AGENT.md + agent.py + manifest.json, re-register after).

Anti-patterns: hand-creating `manifest.json` (misses metadata); agent defs
in `~/.openclaw/` or `/tmp/` (belong in the home repo under VCS); hardcoded
prod credentials (use OS env, `~/.<vendor>/` mode 0600, or Key Vault refs);
skipping `@requires_confirmation` on prod-affecting methods; calling legacy
`agents.lib.agent_recorder` (use AgentBase).

**Outbound-email recipient policy (HARD RULE).** Every agent that emails
the operator uses ONE pair, fleet-wide:
```yaml
reporter:
  email:
    to: [mperry@northernsoftwareconsulting.com]
    from: automation@northernsoftwareconsulting.com
    msmtp_account: automation
```
❌ Never add personal addresses (perrymb@gmail.com etc.) to
`reporter.email.to`, even if mentioned in conversation — the manifest
`owner` is for attribution; mail still goes only to `mperry@nsc…com`.
❌ Never change `from:` — msmtp's `automation` is the only configured
relay; others fail silently. Copy this block verbatim; don't ask for an
inbox. (Set 2026-05-07 after a recipient-list expansion was reverted.)

## Implementer queue — UNIFIED on Azure Blob (no local FS)

Single source of truth for every queued rec, host-wide:

```
Azure container `agents` (nscagentstorage.blob.core.windows.net)
  agents/responder-agent/auto-queue/              ← pending recs
  agents/responder-agent/auto-queue-processed/    ← history (post-ship)
  agents/implementer/runs/<run-ts>/               ← per-dispatch artifacts
```

Every host-worker service runs with `STORAGE_BACKEND=azure`. The local
filesystem dirs `~/.reusable-agents/data/agents/responder-agent/auto-queue/`
and `.../storage/agents/responder-agent/auto-queue/` are **DEPRECATED**
legacy from before the 2026-05-11 cutover. They each carry a `README.md`
explaining the deprecation. Stranded items from the cutover are at
`~/.reusable-agents/.local-stranded-archive/` (cleaned 2026-06-09 after the
queue unification audit).

**Decision rule — anything queue-touching:**
1. Read/write via `framework.core.storage.get_storage()`. NEVER os.path
   into `~/.reusable-agents/data/` directly. The storage backend factory
   honors `STORAGE_BACKEND=azure|local` so the same code works in dev +
   prod + tests.
2. The DRAIN side is the `auto-queue-drainer.service` daemon — a single
   long-running process (`python3 -m framework.cli.auto_queue_drainer
   --interval 15 --idle-backoff 60`). It reads exclusively from the Azure
   path above. Don't spawn parallel drainers; the responder_drain_lock
   primitive handles concurrent attempts but it's wasted process churn.
3. The PRODUCE side is `framework.core.dispatch.dispatch_now()` OR
   writing a JSON file via `framework.core.storage.write_json` to
   `agents/responder-agent/auto-queue/<request-id>.json`. Reporter scripts,
   `requeue-deferred.py`, and the per-agent reporter modules already do
   this correctly.
4. Refuse on sight: new code that touches `~/.reusable-agents/data/agents/`
   directly; a second drainer script ("just for this one rec type"); any
   queue file written outside Azure when `STORAGE_BACKEND=azure`.

### Implementer cadence — event-driven, not cron

| Component | Role | Cadence |
|---|---|---|
| `agent-<site>-article-proposal-agent.timer` | producer (writes recs) | **8h** (00:20 / 08:20 / 16:20 EDT for aisleprompt; :45 offsets for specpicks) |
| `agent-backlog-dispatcher-agent.timer` | producer-side queue feeder | every 1 min |
| `auto-queue-drainer.service` | drain daemon → fires implementer | **15s when busy, 60s when idle (>5min)** |
| `agent-implementer.service` | edits code per rec | spawned on demand by drainer |
| `agent-responder-agent.timer` | email-reply path → manual dispatches | every 15 min |

The implementer does NOT run on a cron. It only runs when the drainer
finds work. With drainer + producers all stopped, the implementer is
silent. With them running and the queue empty, the drainer ticks every
60s but does zero LLM work — checking queue size is `O(1)` blob list.

Inspect queue size (operator-grade one-liner):
```bash
STORAGE_BACKEND=azure AZURE_STORAGE_CONNECTION_STRING="$(systemctl --user show \
  reusable-agents-host-worker.service --property=Environment -o cat | \
  tr ' ' '\n' | grep -oP 'AZURE_STORAGE_CONNECTION_STRING="?\K[^"]+')" \
  python3 -c "
import sys; sys.path.insert(0,'/home/voidsstr/development/reusable-agents')
from framework.core.storage import get_storage
s = get_storage()
print('pending  :', len(s.list_prefix('agents/responder-agent/auto-queue/')))
print('processed:', len(s.list_prefix('agents/responder-agent/auto-queue-processed/')))
"
```

Stop the pipeline (lets the Claude pool cool):
```bash
systemctl --user stop auto-queue-drainer.service \
                       agent-backlog-dispatcher-agent.timer \
                       agent-responder-agent.timer
```
Resume:
```bash
systemctl --user enable --now auto-queue-drainer.service \
                                agent-backlog-dispatcher-agent.timer \
                                agent-responder-agent.timer
```

### Prioritization — how recs are ordered in the auto-queue

Defined in `framework/core/priority.py`. Re-evaluated on **every drain tick**
so newly-arrived high-tier items always jump ahead of waiting low-tier items.

**Default tier ladder** (operator-overridable via storage config
`config/priority-config.json` — currently empty so defaults apply):

| Tier | Label | Agents |
|---|---|---|
| **1** | SEO + ranking signals | `*-seo-opportunity-agent`, `*-progressive-improvement-agent`, `*-competitor-research-agent`, `seo-implementer`, `seo-analyzer` |
| **2** | AislePrompt content | `aisleprompt-article-proposal-agent`, `aisleprompt-head-to-head-agent` |
| **3** | SpecPicks content | `specpicks-article-proposal-agent`, `specpicks-head-to-head-agent` |
| **4** | Research / catalog hygiene | `*-catalog-audit-agent`, `*-product-hydration-agent`, `*-benchmark-research-agent`, `*-ebay-product-sync-agent`, etc. |
| **5** | Ops / housekeeping (default) | `agent-doctor`, `digest-rollup-agent`, `*-scraper-watchdog` |

Sort key: **`(tier_int, run_ts, key)`** — lower tier first, oldest first within tier.

**Override per-agent:** push `manifest.priority_tier: N` in the agent's
manifest, OR push a `config/priority-config.json` override naming the
agent under the desired tier number.

### Dynamic adjustments at drain time (2026-06-09)

The drainer applies two LIVE adjustments on top of the static tier:

**1. Pool-aware demotion** — `priority.effective_tier_with_pool_pressure()`
   When the claude-pool has no opus headroom (every authenticated profile
   is rate-limited until later than NOW + 15 min), any rec with
   `required_model=opus` is **demoted to tier 9** (sinks below everything
   else). This stops opus-required article batches from blocking
   non-opus T1 SEO + T4 catalog work behind them. As soon as ANY profile
   has opus available, the demotion lifts on the next drain tick.

**2. Per-site starvation rebalance** — `priority.site_starvation_boost()`
   Queries `editorial_articles` count per site over last 7d (cached 5
   min). If one site has 0–1 articles while the peer has >10, the
   starved site's tier gets a **−2 boost** (jumps ahead). 2–5 articles
   while peer has >20 → **−1 boost**. The final tier is capped at 1
   (never displaces top-priority SEO).

**3. Per-rec defer-backoff** — `framework.core.defer_backoff`
   Recs that defer (e.g. `required-model-unavailable`) get exponential
   backoff: 1m → 5m → 30m → 2h → 6h → 12h (cap). The backlog-dispatcher
   calls `defer_backoff.should_skip(rec_id, agent_id)` before adding a
   rec to the dispatch candidates; in-cooldown recs are skipped (no
   re-queue, no log spam). On success the dispatcher should call
   `defer_backoff.record_success(rec_id, agent_id)` to clear the cooldown.
   The implementer's required-model-unavailable defer path calls
   `record_defer()` automatically. Storage:
   `framework/defer-backoff/<source_agent_id>.json`.

**Operator overrides for the dynamic layer:**
- Force-clear a stuck defer cooldown: `python3 -c "from framework.core
  import defer_backoff; defer_backoff.reset_all('aisleprompt-article-proposal-agent')"`
- Disable pool-aware demotion: set `POOL_OPUS_GRACE_S=999999` in the
  drainer's environment (in `framework/cli/auto_queue_drainer.py` env)
- Bypass starvation boost: pass an empty `sites=()` tuple to
  `site_starvation_boost()` — or unset `DATABASE_URL_<site>` envs

**Refuse on sight:**
- A new agent that re-implements tier logic inline (use `tier_for_agent`)
- A code path that re-queues a deferred rec without checking
  `defer_backoff.should_skip()` first
- Hardcoded `if site == "aisleprompt"` priority boosts in framework
  code (use `priority-config.json` or the starvation primitive)

## Implementer path-scope — keep agents in their lane

The implementer runs aider/claude-cli/copilot against the whole site repo.
Without a path-scope in `site.yaml`, the LLM drifts and commits unrelated
changes — a past SEO rec ("zero Instacart clicks — investigate funnel")
made it rewrite the AislePrompt **mobile** app because the rec had no
`target_files`.

Every per-site agent's `site.yaml` MUST declare an implementer scope:
```yaml
implementer:
  agent_id: seo-implementer
  repo_path: /home/voidsstr/development/<site>
  branch: master
  allowed_paths: ["src/**", "frontend/**", "db/migrations/**", "scripts/**", "changes/**", "*.md"]
  excluded_paths: ["mobile/**", "ios-extensions/**", "android/**"]
  post_apply: {kick_mobile_build: false, kick_backend_deploy: true}
  # Per-dispatch-kind overrides REPLACE (not merge) the default block.
  scope_by_dispatch_kind:
    crash-fix:                        # crash-watcher needs mobile/, SEO must not
      allowed_paths: ["src/**", "frontend/**", "mobile/**", "ios-extensions/**", "android/**"]
      excluded_paths: []
      post_apply: {kick_mobile_build: true, kick_backend_deploy: true}
```

Two checkpoints: (1) **Pre-LLM** in `build-aider-invocation.py` — recs
whose `target_files` fall outside `allowed_paths` (or hit
`excluded_paths`) are deferred `out-of-scope per site policy`, never reach
the LLM. (2) **Post-LLM** in `run.sh` before `git add` — newly-touched
out-of-scope files are `git checkout`-ed (or deleted if new) and dropped
from the commit; catches drift where an in-scope rec edits an out-of-scope
file as a side effect.

Primitive `framework/core/implementer_scope.py`;
`ScopePolicy.from_site_config(cfg, dispatch_kind=...)` (a matching
`scope_by_dispatch_kind` key REPLACES the default; fnmatch globs, `**`=any
segments). `post_apply` hooks default `true`: set `kick_mobile_build` /
`kick_backend_deploy` to `false` to refuse EAS builds / require manual
`install/deploy-azure.sh`. Schema:
`shared/schemas/site-quality-config.schema.json` →
`implementer.{allowed_paths,excluded_paths,post_apply}`.

Refuse on sight: a per-site `site.yaml` shipping without `allowed_paths`
(default = "may touch anything" — the mobile-drift bug); adding a path
because "this one rec needs it"; `if site == "x"` branches inside
`implementer_scope.py` (policy is data, the primitive stays generic).

## Site-name literals in framework code — disallowed

The framework must not name specific sites (`aisleprompt`, `specpicks`, …)
anywhere in `framework/` or shipped agents. Per-site behavior flows
through: (1) storage configs `config/<feature>-config.json`; (2)
`manifest.depends_on` for per-agent graph edges; (3) wildcard templates
(`*-progressive-improvement-agent`, …) in `framework/core/dependencies.py`
that expand at query time. Typing `"aisleprompt-…"` in `framework/` → write
a template/config knob instead. Regression test
`framework/tests/test_dependencies.py::test_no_site_specific_literals_in_default_edges`
fails CI on a leak.

## Editing an existing agent's behavior

1. Find the home repo: `curl http://localhost:8090/api/agents/<id>` →
   `repo_dir`.
2. Edit `AGENT.md` and/or `agent.py`.
3. Re-register only if `manifest.json` changed:
   `bash <home-repo>/agents/register-with-framework.sh` (scheduling
   changes auto-update the timer).
4. Trigger if needed: `curl -X POST
   http://localhost:8090/api/agents/<id>/trigger -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"`.

## Editing a per-site `site.yaml` (SEO + revenue agents)

Per-site SEO instances share one schema
`shared/schemas/site-config.schema.json`. Blocks with
`additionalProperties:false` reject unknown keys at startup — the agent
calls `load_config_from_env()` and exits status 1 in ~1s (systemd shows
only "code=exited, status=1/FAILURE"). (2026-05-04: adding
`articles.url_template` without updating the schema broke both SEO agents
for ~2h.)

**Hard rule for any new `site.yaml` field:**
1. **Add it to the schema first** — `site-config.schema.json`, the right
   block (`articles`/`data_sources`/`analyzer`/`reporter`/`implementer`/
   `deployer`/`coverage_targets`/`page_inventory`/…), with a real
   `description`.
2. Pre-commit test locally — validation errors print to stderr in the
   first second:
   ```bash
   AGENT_ID=specpicks-seo-opportunity-agent \
   SEO_AGENT_CONFIG=/home/voidsstr/development/specpicks/agents/seo-opportunity-agent/site.yaml \
   DATABASE_URL='postgresql://...' PYTHONPATH=/home/voidsstr/development/reusable-agents \
   python3 /home/voidsstr/development/reusable-agents/agents/seo-opportunity-agent/agent.py
   ```
3. Update both per-site `site.yaml` files if the field is shared semantics.
4. Update `agents/seo-opportunity-agent/README.md` if it adds a
   user-visible capability.
5. Re-register only if the manifest itself changed (editing `site.yaml`
   alone doesn't — next tick picks it up).

**Cron discipline:** the two SEO instances run 2-hourly offset `:15`
(specpicks `0 */2 * * *`, aisleprompt `15 */2 * * *`) to avoid racing for
LLM quota. A third site → `:30` or `:45`, not `:00`/`:15`.

**Docs map:** new phase rule → that phase's `lib/<phase>/README.md` +
engine `agents/seo-opportunity-agent/README.md` (rec-type catalog); new
field → schema + engine README + (if onboarding) `docs/seo-onboard-new-site.md`;
new rec type → rec-type catalog; new failure mode → engine README
Troubleshooting (+ this file if recurring); pipeline architecture change →
`docs/agents-catalog.md` SEO section + engine README. App-deploy recipes
(Azure Container Apps, Vercel, Cloudflare, Netlify): copy from
`agents/seo-deployer/README.md` into the site's `deployer:` block.

## Auto short-circuit — opt every cron-driven agent in

`AgentBase.signals()` is the universal short-circuit hook (added 2026-05-11
after a retro found 16/18 agents re-running LLM work each tick with no
input change). Override it on any agent that wakes on a schedule and
*sometimes* has nothing new:
```python
def signals(self) -> dict | None:
    return {
        "queue_keys": sorted(self.storage.list_prefix("queue/") or []),
        "last_event_ts": (self.storage.read_json("events.json") or {}).get("ts"),
    }
```
When the hash matches the prior successful run, the framework returns
`RunResult(short_circuited=True)` and `run()` never executes — no LLM/API/
Azure traffic.

Rules: include only INPUTS the agent reads (never `now`, a tick count, or a
UUID); stable serialization (sort lists, round floats — `signal_hash()`
does this); return `None` to opt out; if you can't hash without doing the
expensive work, use `short_circuit.partition_by_hash` per-item inside
`run()`.

Status (2026-05-11): **✅ done** — `seo-opportunity-agent` +
`progressive-improvement-agent` (in-run `should_skip`), `crash-watcher-agent`,
`digest-rollup-agent`, `catalog-audit-shipped-backfill`,
`catalog-audit-agent` (via `signals()`). **⚠️ needs the per-item pattern
instead** — `competitor-research-agent` + `product-hydration-agent`
(`partition_by_hash` inside `run()` after crawl / per product). **⚠️ skip** —
`app-store-opportunity-agent` (exploration; time IS the signal),
`ebay-product-sync-agent` (needs an eBay revision-id HEAD). **n/a**
(inbox/reactive, or in the AgentBase backlog) — `responder-agent`,
`deployer`, `implementer`, `agent-doctor`, `jcode-agent`,
`gsc-coverage-auditor`, `indexnow-submitter`, `agent-metrics-collector`,
`goals-tracker`, `site-goals-tracker`. Each TODO is ~10 min — do it when
you're already in the file, never as a side-quest.

## Inspect / debug an agent

- `curl http://localhost:8090/api/agents/<id>` — full detail;
  `…/runs?limit=20` — history; `…/status` — live status
- `journalctl --user -u agent-<id>.service` — run logs
- UI http://localhost:8091/agents/<id> → Storage tab — browse the blob
  hierarchy

## Framework architecture cheat sheet

`framework/core/`:

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
| `mailer.py` | Outbound mailer (LogMailer + Graph + SMTP impls) |
| `handoff.py` + `work_types.py` | Inter-agent handoff routing |
| `resilience.py` | `safe_run`, `with_retry`, `notify_operator`, `invoke_doctor` |
| `short_circuit.py` | Skip-LLM-when-unchanged: `signal_hash`, `snapshot_hash`, `should_skip`, `partition_by_hash`, `merge_findings_cache` |
| `ai_providers.py` | Provider/model registry — `ai_client_for(agent_id)` resolves via overrides + defaults |

API: `framework/api/app/main.py` (FastAPI, 35 routes + 2 WS streams, token
auth via `FRAMEWORK_API_TOKEN`). UI: `framework/ui/src/` (React + Vite +
Tailwind, iframe-friendly). Templates: `_template/agent/` (cloned by
create-agent.sh). Tests: `framework/tests/` (20 pytest cases vs
LocalFilesystemStorage).

## LLM efficiency — required patterns

Use these primitives, don't reinvent (reinventing costs tokens + drifts):
1. `short_circuit.signal_hash` / `snapshot_hash` — hash inputs (excludes
   timestamps, rounds floats); `should_skip(self.state,
   "last_signals_hash", sig)` at the top of `run()`, persist to
   `next_state["last_signals_hash"]`.
2. `short_circuit.partition_by_hash` — per-page/product/row analysis;
   skips unchanged items + replays prior findings via `merge_findings_cache`.
3. `resilience.safe_run` — wrap external calls (LLM/HTTP/DB); framework
   handles retries + operator-alert. Don't roll your own retry loops.
4. `handoff.send_handoff` — when work belongs to a specialist, hand it off;
   don't work outside your role.
5. Always batch LLM calls over N items — one structured-output prompt for N
   items beats N prompts. Canonical:
   `competitor-research-agent._extract_features_batched`. If you're about to
   write `for item in items: client.chat(...)`, stop and batch.

## Dispatch-kind pause — operator-pauseable work

The implementer can be pinned to a subset of dispatch_kinds so an operator
(or another agent) editing the site repo doesn't get framework edits
interleaved. Storage config `config/implementer-allowed-dispatch-kinds.json`:
```json
{"schema_version": "1", "allow": ["article-author", "news-author", "news-rewrite", "h2h"]}
```
When `allow` is a list, the **backlog-dispatcher** (not the implementer)
skips non-matching recs at dispatch time — they stay in the producer's
run-dir and re-evaluate next tick (SEO/PI/catalog-audit simply queue until
lifted). The authoring set is `article-author` (blog posts), `news-author`
(syndicated rewrites), `news-rewrite` (re-angled), `h2h` (head-to-head) —
all Opus-bound. To pause non-authoring work use that set; to unrestrict,
`{"allow": ["*"]}`.

Why at the dispatcher, not the implementer: skipping is <1ms vs wasting a
systemd launch + run.sh boot to immediately defer, and it keeps the
implementer run-history clean. Enforced in
`agents/backlog-dispatcher-agent/agent.py` (candidate-collection loop,
after the rec-handler allowlist). Producer→dispatch_kind mapping is inlined
(seo-opportunity→seo, progressive-improvement→pi, article-author→
article-author, catalog-audit→catalog-audit, comp-research→comp-research,
news-author/news-writer→news-author); recs can override via
`rec.dispatch_kind`.

## Image generation — local only

All text-to-image generation MUST POST the local SDXL-Turbo daemon
`http://127.0.0.1:7861/generate`. Paid providers (Azure OpenAI
gpt-image-1, DALL-E, fal.ai, Replicate, Stability, Together) are forbidden
(2026-05-24: Azure gpt-image-1 burned ~$190/day at the 8-RPM cap). Local is
~140× cheaper (~$0.0003 vs $0.042/img), ~6,000 img/h on the RTX 5090, and
quality beats the paid path. Daemon: `services/local-image-gen/` under
`local-image-gen.service`, port 7861, bearer-token auth (see its README).

Hard rules:
1. New image code MUST POST `localhost:7861/generate`. Need a different
   model? Run a SECOND daemon instance with `LOCAL_IMAGE_GEN_MODEL=...` on
   another port.
2. No paid fallback — if the daemon is down the op fails/defers (refiller
   retries next tick). Silent failover caused the incident.
3. The live API (Azure simple-server.ts) can't reach localhost — new
   recipes start `image_url=NULL`; the dev-box refiller fills within 5 min.
4. No `images/generations` to any hosted provider. Grep before merging:
   `azureOpenAIGenerateImage`, `gpt-image`, `dall-e`, `dalle`,
   `/images/generations`, `AZURE_OPENAI_IMAGE_*`, `fal.ai`, `replicate.com`,
   `together.xyz/v1/images`.
5. To reach the daemon from Azure (rare): add a Cloudflare tunnel
   `images.aisleprompt.com → localhost:7861`. Don't reintroduce a paid
   provider because the tunnel is missing.

Refuse on sight: `azureOpenAIGenerateImage(...)` (tombstoned);
`process.env.AZURE_OPENAI_IMAGE_*` (dead); "optional fallback to
fal.ai/Replicate"; any new `images/generations` call "just for testing";
re-creating the deleted `aisleprompt-ai-img` Azure deployment without a
cost review.

Reference: `agents/recipe-image-refiller` →
`scripts/_refill-missing-images.ts` → `localGenerateImage()`. Cookware
cross-sell, article heroes, and future flows use the same endpoint.

> ## ⚠️ ARTICLE / NEWS / H2H WRITING — OPUS-ONLY — READ EVERY SESSION ⚠️
>
> Every code path that GENERATES an editorial body — article, news,
> head-to-head verdict/prose, long-form review, buying-guide per-pick
> rationale — MUST use Claude Opus (`claude-opus-4-7`) and MUST defer
> rather than fall back to a smaller model when Opus is unavailable. Prose
> quality outweighs throughput for anything a human reader judges; sonnet
> drafts read flatter and lose each site's voice. Applies to
> `dispatch_kind` in `{article-author, news-author, news-rewrite, h2h,
> h2h-commentary, comparison_page_generation}`, to article-author agents on
> every site, news-rewrite/author paths (e.g.
> `specpicks/scripts/rewrite-news-as-commentary.ts`), and the head-to-head
> agent.
>
> **Mechanism:** `framework/core/required_model.py` + storage config
> `config/required-models.json`. Resolution: (1) `rec.required_model_tier`
> → (2) `by_dispatch_kind[<kind>]` → (3) `by_agent_id[<source_agent>]` →
> (4) soft `recommended_model_tier`. Current config:
> ```json
> {
>   "by_dispatch_kind": {
>     "article-author": "opus", "news-author": "opus", "news-rewrite": "opus",
>     "h2h": "opus", "h2h-commentary": "opus", "comparison_page_generation": "opus"
>   },
>   "by_agent_id": {
>     "specpicks-article-proposal-agent": "opus", "aisleprompt-article-proposal-agent": "opus",
>     "specpicks-news-writer": "opus", "specpicks-head-to-head-agent": "opus"
>   }
> }
> ```
>
> **Implementer behavior:** `required_model_for_batch()` returns
> `("opus","claude-opus-4-7")`; if Opus is unreachable the implementer
> writes `deferred.json` (reason `required-model-unavailable`) and the rec
> stays queued. The defer happens BEFORE any fallback chain — `run.sh`
> lifts `REQUIRED_MODEL` to the top, ahead of `IMPLEMENTER_FORCE_FALLBACK`
> / `IMPLEMENTER_BACKEND=copilot-gpt-4.1`. The framework code-editor chain
> (`jcode-copilot`, `aider-github-copilot`, `aider-azure`, `jcode-ollama`)
> is NEVER allowed to satisfy a required-model batch — those backends can't
> run Opus.
>
> **One-off scripts honor this too:** any new `<site>/scripts/*.{ts,py}`
> writing article-shaped output hard-codes `claude-opus-4-7` in `--model`
> (not sonnet/downgrade chains). Reference:
> `specpicks/scripts/rewrite-news-as-commentary.ts`.
>
> **Multi-account claude-pool rotation:** with multiple Max accounts
> (`profile-1`..`profile-5`), one org-level disable is NOT a pool outage.
> `_AUTH_DEAD_PATTERNS` in `framework/cli/claude_pool.py` recognizes "Your
> organization has disabled Claude subscription access" and marks ONLY that
> profile dead; the pool rotates. The implementer's probe in `run.sh` walks
> ALL profiles before declaring the pool dead. Re-enable: the org admin
> restores Claude Code access in the Anthropic console (the auth blob stays
> valid).
>
> **Copilot-opus bridge — fallback when the pool is broken** (rate-limit,
> weekly-cap, or org-disable). Opt-in via
> `IMPLEMENTER_COPILOT_OPUS_BRIDGE=1` in `~/.reusable-agents/secrets.env`
> (opt-in because as of 2026-05-24 the proxy returned 402 quota_exceeded
> for both opus and sonnet, so it would consume the deferral path without
> shipping prose). When enabled, the implementer (run.sh ~line 725) probes
> `localhost:4141/v1/models`; if `claude-opus-4.7` is listed, it routes the
> `claude --print` loop through the proxy by setting
> `ANTHROPIC_BASE_URL=http://localhost:4141` and removing `claude-pool/bin`
> from PATH (the pool shim ignores `ANTHROPIC_BASE_URL`). This is why h2h
> commentary stalled 2026-05-01..05-24: all 5 profiles were org-disabled +
> `IMPLEMENTER_FORCE_FALLBACK=1` + opus-required = no path. If the Copilot
> proxy is dead, start it or provision `ANTHROPIC_API_KEY` with real opus
> access.
>
> Operator checklist when authoring stalls:
> ```bash
> curl -sf http://localhost:4141/v1/models | grep claude-opus-4.7          # proxy serving opus?
> jq '."profile-1".auth_error_message' ~/.reusable-agents/claude-pool/state.json   # pool auth errors?
> find ~/.reusable-agents/data -name deferred.json -mtime -1 -exec cat {} \;       # recs deferred?
> grep -A1 "copilot-opus path available" /tmp/reusable-agents-logs/agent-implementer.log | tail  # bridge picked up?
> ```
>
> Refuse on sight: a `chat_with_fallback(...)` call in article-author code
> (chain includes sonnet+haiku); hardcoded `claude-sonnet-4-6` in any
> writer; adding these dispatch_kinds to `recommended_model_tier` (soft)
> instead of required; skipping `required_model_for_batch()` in a custom
> implementer; removing the Copilot bridge "because the pool is back"
> (leave it as defense-in-depth — fires only when the pool is
> force-disabled); hardcoding the bridge model name instead of reading
> `/v1/models`; pointing `ANTHROPIC_BASE_URL` at the proxy globally (routes
> ALL claude calls, defeating Max rate-budgeting for non-opus work).

## LLM provider routing — chat vs code-editor

Two independent systems; don't conflate:
1. **Chat** — `framework.core.ai_providers`. Used by `self.ai_client()` /
   `chat_with_fallback(...)`. Kinds: `copilot` (GitHub Copilot proxy,
   subscription — default), `claude-cli` (Max session),
   `anthropic`/`openai`/`azure_openai` (per-token), `ollama` (free local).
   Defaults + per-agent overrides at `config/ai-defaults.json` (edit via
   the `/providers` page or `POST /api/providers/defaults/{set,agent-override}`).
   `chat_with_fallback` walks `(copilot, azure_openai, openai, anthropic,
   ollama)` on rate-limit/timeout/quota.
2. **Code editor** — `framework.core.code_editor`. Used by the implementer
   + the `llm-code-editor` blueprint. Chain of editor binaries (`aider`,
   `opencode`, `crush`, `codex`, `plandex`) each paired with a model
   (claude-sonnet-4.6 via copilot proxy top; gpt-4.1-mini via Azure
   fallback). Config `config/code-editor-config.json`. Runs *after*
   agent-specific editor logic (e.g. the implementer's claude-pool path).

"Switch from claude to copilot/aider": chat agents → change
`config/ai-defaults.json` (global or per-agent); implementer →
`IMPLEMENTER_FORCE_FALLBACK=1` or `IMPLEMENTER_LLM=framework` to skip the
claude-pool path and use the framework chain.

Don't shell out to `claude` / `aider` / `gh copilot` directly from new
agent code — both systems wrap those binaries with stream capture, usage
tracking (`config/llm-usage-*.jsonl`), fallback chains, and dashboard
visibility. Docs: README.md → "LLM provider chain";
`agents/implementer/AGENT.md` → "LLM driver"; this file → routing rules.

## Deploying the dashboard to Azure — REQUIRED after framework changes

Every change to `framework/api/`, `framework/ui/`, or `framework/core/`
(shared modules the API imports — agent_base, status, ghost_reaper,
llm_stream, storage, decision_log) MUST be deployed in the same task —
local docker compose only updates http://localhost:8091; production keeps
the old build until you push.
```bash
cd /home/voidsstr/development/reusable-agents && bash install/deploy-azure.sh
```
Builds + pushes both images to `nscappsacr` ACR and updates the `agents`
Container App in `nsc-apps`. Default tag = UTC timestamp; pass an explicit
tag to roll back (`bash install/deploy-azure.sh 20260428-1620`). **NOT
required** for: agent code under `agents/<id>/` (host re-execs from disk —
`git pull` on the host suffices), docs, tests, blueprints, `_template/`,
`install/`. Verify:
```bash
az containerapp show -g nsc-apps -n agents --query properties.configuration.ingress.fqdn -o tsv
curl -sI https://<fqdn>/ | head -3
```
If "the dashboard doesn't show my change," first check: did this task run
deploy-azure.sh?

## Per-site app deployments — pick a recipe, edit site.yaml

When an agent commits application code (not just data), the deployer chains
`test → build → push → deploy → smoke_check`. Each stage is a shell-command
template; the deployer is cloud-agnostic (whatever bash can do). To
configure a site:
1. Pick a recipe from `examples/deployer/`: `azure-container-apps.yaml`
   (**active**, aisleprompt+specpicks), `azure-app-service.yaml`,
   `azure-functions.yaml`, `aws-ecs-fargate.yaml`, `aws-lambda.yaml`,
   `aws-app-runner.yaml` (samples).
2. Drop the whole `deployer:` block into the site's `site.yaml`.
3. Edit `deploy.vars:` (app name, resource group, image URI) — each key
   expands as `{key}` in any stage's `cmd:`.
4. Ensure the host has the cloud CLI configured (az login / aws configure)
   and docker reachable.

Existing declarations: aisleprompt →
`nsc-assistant/agents/aisleprompt-seo-opportunity-agent/site.yaml`;
specpicks → `specpicks/agents/seo-opportunity-agent/site.yaml`.

Deployer skips (intentional): `article-author`, `catalog-audit`, `h2h`
(DB-only, no docker build), `IMPLEMENTER_SKIP_DEPLOY=1` (manual/debug);
everything else runs.

New cloud target: copy a dormant recipe — `deployer.py` doesn't change (it
just runs the shell commands), so K8s / GCP Cloud Run / Cloudflare Workers
= just a recipe. Add it to `examples/deployer/README.md` + the recipe list
above. Don't hardcode cloud-specific logic in `deployer.py` — a wanted
per-cloud branch means the recipe is missing a knob.

## Git workflow — commit straight to main, no feature branches

The operator works `main`-first. The default Claude Code rule ("if on the
default branch, branch first") is **overridden** for this repo and for
the two sibling site repos (aisleprompt, specpicks):

- **Do NOT create a feature branch** unless the operator explicitly
  asks ("branch this", "open a PR for…", "isolate in a worktree", etc.).
- Default action when the operator says "commit" or "merge": `git add` +
  `git commit` on whatever branch you're currently on (almost always
  `main`).
- `git push` only when explicitly asked. The operator pulls + reviews
  locally before pushing.
- If you find yourself already on a feature branch from an earlier
  session and the operator says "commit", finish the work on that
  branch + ask whether to merge or push — don't silently switch.

Why: the three repos are tightly coupled (framework rename touches site
code; site cron changes are noticed by the framework reporter), so a
branch-per-change creates merge friction. The operator reviews via
`git log -p` + reverts unwanted commits with `git reset` / `git revert`.

Refuse on sight: `git checkout -b` "to be safe"; opening a PR without
being asked; pushing without being asked.

## Hosting note

This codebase is shared. When working in nsc-assistant, specpicks, or
customer apps, don't refactor the framework unless explicitly asked —
submit framework changes as PRs to reusable-agents and `git pull` in the
framework checkout.

## Infra scripts are the source of truth

Any change to deployment behavior MUST land in the deploy scripts in the
same change — no ad-hoc CLI, no "commit later." If you ran an
`aws`/`az`/`docker`/`gh` command that materially changed how the app
deploys, it belongs in a script first.

| Change | Script(s) to update |
|---|---|
| Framework provisioning (VPC, RDS, ECR/ACR, S3/blob, ALB, IAM, secrets, ECS cluster) | `install/deploy-aws.sh` + `install/deploy-azure.sh` (keep phases parallel) |
| Framework image build/push (api/ui) | `deploy-aws.sh` `phase_images` + `deploy-azure.sh` matching block |
| Per-app image build/push/redeploy | `<repo>/aws/deploy.sh` + `<repo>/azure/deploy.sh` |
| New env var the app reads | both deploy scripts (passthrough) + the Secrets Manager / Container App secret block |
| New custom domain | AWS `phase_dns` + the Azure equivalent |
| New cloud target (GCP, Cloudflare) | new `install/deploy-<cloud>.sh`, same phase shape; no per-cloud logic in agents/framework |

Decision rule before any deploy-touching PR: (1) ran a cloud CLI command
the script doesn't already do? add it; (2) would a teammate running
`deploy-aws.sh provision` on a fresh account reach the same state? if not,
fix the script; (3) cloud-specific code lives only in
`install/deploy-<cloud>.sh` + `<repo>/<cloud>/deploy.sh`.

Refuse on sight: manual `aws ecs update-service` / `az containerapp update`
not wrapped by a script; "workaround" commits fixing prod without updating
the deployer; a new cloud resource referenced in agent code without a
provisioning block; one cloud's script gaining a feature the other lacks
(keep AWS+Azure parallel); editing `<repo>/aws/deploy.sh` without checking
`<repo>/azure/deploy.sh` holds the same env contract.

State contract: `deploy-aws.sh provision` writes `~/.aws-deploy/state.env`
(VPC, subnets, cluster, ALB, RDS endpoint, ECR registry); every
`<repo>/aws/deploy.sh` reads it. Azure state lives in the resource group.
Don't add fields to one side without the other.

## Deploying to AWS (parallel cloud target)

`install/deploy-aws.sh` mirrors `deploy-azure.sh`, phased:
```bash
bash install/deploy-aws.sh provision   # VPC, RDS, ECR, S3, ALB, IAM, Secrets, ECS cluster
bash install/deploy-aws.sh secrets     # populate Secrets Manager
bash install/deploy-aws.sh images      # build + push framework api/ui images
bash install/deploy-aws.sh services    # create/update ECS task defs + services
bash install/deploy-aws.sh dns         # print ALB CNAMEs
bash install/deploy-aws.sh all         # full pipeline
```
Per-app images (aisleprompt, specpicks, hearthnote, nsc-website,
application-research) live in `<repo>/aws/deploy.sh`, each reading
`~/.aws-deploy/state.env` from the framework provisioner. Don't add
AWS-specific branching to agents — cloud-specific code stays in the deploy
scripts + the framework storage backend factory; agent code is
cloud-neutral.
