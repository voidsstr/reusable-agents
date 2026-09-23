# SEO + Revenue Opportunity Agent (`seo-opportunity-agent`, shared engine)

> Tier-1 SEO recommendation producer. Each run pulls Google Search Console,
> GA4, production-DB stats, and live-site signals for one site, scores
> opportunities using deterministic rules plus an LLM page audit, and writes
> up to 12 recommendations that flow to the implementer. North Star: organic
> clicks, indexed pages, and the conversions that come from organic traffic.

This is the **operational runbook**. The deep reference (rec-type catalog,
troubleshooting history, onboarding a new site) is [`README.md`](README.md) in
this directory.

## At a glance

| | |
|---|---|
| Engine id | `seo-opportunity-agent` (class fallback; each per-site unit sets `AGENT_ID`) |
| Code | `reusable-agents: agents/seo-opportunity-agent/`: `agent.py` (AgentBase orchestrator), `finalizer.py` (phase 3), `lib/collector/{pull-data.py,refresh-token.py}`, `lib/analyzer/{analyzer.py,llm_audit.py}`, `lib/reporter/send-report.py` |
| Kind | AgentBase Python engine. This dir has **no `manifest.json`**, so it is not registered on its own. |
| Per-site instances (live) | `aisleprompt-seo-opportunity-agent`: `aisleprompt: agents/seo-opportunity-agent/`, cron `15 */2 * * *`, `OnCalendar=*-*-* 0/2:15:00`, timer enabled. `specpicks-seo-opportunity-agent`: `specpicks: agents/seo-opportunity-agent/`, cron `30 */3 * * *`, `OnCalendar=*-*-* 0/3:30:00`, timer enabled. Both use `America/Detroit`. |
| Entry command (instances) | `SEO_DISABLE_UNCHANGED_SHORTCIRCUIT=1 AGENT_ID=<id> SEO_AGENT_CONFIG=<site>/agents/seo-opportunity-agent/site.yaml PYTHONPATH=<reusable-agents> python3 <reusable-agents>/agents/seo-opportunity-agent/agent.py`, run through `framework/agent_run_wrapper.sh`. The specpicks instance also sets `DATABASE_URL` inline. |
| Category | `seo` |
| Run time | 16–29 minutes when the LLM audit runs (all runs on 2026-09-23). Most of that is the LLM audit. From about 2026-09-19 13:30 UTC to 2026-09-22, most runs took about 2 minutes and emitted only rule-pass recs (5–8 recs, none from the LLM audit), yet still reported `success`. See Failure modes. |
| Deep reference | [`README.md`](README.md) |

## What it does

`SEOOpportunityAgent.run()` loads the site config
(`shared.site_config.load_config_from_env()`, which validates it against
`shared/schemas/site-config.schema.json`). It then runs three phases under one
`run_ts`, so every artifact lands in `agents/<agent_id>/runs/<run_ts>/`.

1. **Collect** (subprocess `lib/collector/pull-data.py --agent-id --run-ts`,
   written into `data/`):
   - **Token.** Mints a Google access token through `refresh-token.py
     --oauth-file <auth.oauth_file>`.
   - **GSC** (4 raw reports: `gsc-{queries,pages,devices,countries}-90d.json`).
     A 90-day window ending 3 days ago, `rowLimit` 25000. **A GSC failure exits
     non-zero** and fails the run.
   - **GA4** (4 reports: `ga4-{summary,events,geo,traffic-sources}-28d.json`).
     A 28-day window ending yesterday. A failed report degrades to `{}`.
   - **`db-stats.json`.** Each `-- @@QUERY: <name>` block in
     `data_sources.db.queries_file` runs against the DSN in `$<dsn_env>`
     (default name `DATABASE_URL`), with a 60 s statement timeout. A missing
     queries file, unset DSN variable, or missing psycopg2 writes `{}`. A
     failing query stores `[]` for that key. A **connection** error is not
     caught, so the collector exits non-zero and the run fails.
   - **`site-signals.json`.** robots.txt (`status`, `has_sitemap`,
     `disallow_all`, `body`), plus the homepage title, meta description, H1
     count, canonical, and JSON-LD `@type`s. robots.txt is fetched without a
     User-Agent header; a failed fetch leaves `robots: {}`.
   - **`sitemap-urls.json`.** `/sitemap.xml` plus up to 100 child sitemaps,
     giving per-pattern counts for each `coverage_targets.*.sitemap_pattern`.
     `complete: false` is set when a child sitemap fetch failed or the index
     had more than 100 children. If `/sitemap.xml` itself fails, no file is
     written.
2. **Analyze** (subprocess `lib/analyzer/analyzer.py --agent-id --run-ts`):
   - Writes `snapshot.json`.
   - *Would* compare against the prior run and score that run's goals, but see
     Failure modes: the prior run is not currently found.
   - Loads "handled" rec keys from the newest 30 prior runs it can list (recs
     marked shipped, implemented, applied, or deferred). Keys exist only for
     `top5-target-page`, `ctr-fix`, `internal-link`, and `article-*` recs, so
     this filter never applies to `new-page-*`, `gsc-coverage-*`, or most
     LLM-audit types.
   - Runs the deterministic rule passes. The pass order changes in pre-traffic
     mode, when 90-day impressions are below `analyzer.pre_traffic_impr_threshold`.
   - Runs the **LLM audit**. Pages come from `data/pages-by-type.jsonl`, then
     `data/pages.jsonl`, and otherwise an on-demand crawl (homepage plus the top
     10 GSC pages, depth 1, at most 20 pages, using the
     `progressive-improvement-agent` crawler). At most
     `analyzer.max_llm_audit_pages` pages (default 30) are audited, in batches
     of 4, with the agent's active goals and past goal-change context injected
     into the prompt. Findings are checked against the 133-check whitelist in
     `llm_audit.py`. LLM recs fill the slots the rule passes left, up to the
     final `max_recs_per_run` cap. An LLM error is caught and logged
     (`LLM audit failed: …`); the run continues with rule-pass recs only.
   - Tags each rec with `work_type` and `handoff_target`
     (`framework.core.work_types.handler_for`, plus the site's `handoff_routes`
     and `site_handler_overrides`).
   - Writes `recommendations.json` (capped at `analyzer.max_recs_per_run`,
     default 12) and `goals.json`.
3. **Finalize** (`finalizer.finalize`):
   - Renders the HTML report (`lib/reporter/send-report.py:render_html`).
   - Writes `recommendations.json` to storage.
   - Queues the report to the digest (`digest-queue/<ts>-<hash>.json`, sent
     later by `digest-rollup-agent`).
   - Calls `gated_dispatch_now(cfg=cfg, …)`.
   - Records outbound only if a dispatch happened.
   - Pings `reporter.dashboard.base_url`.
   - Returns metrics.

## Inputs

| Input | Detail |
|---|---|
| GSC Search Analytics API | `data_sources.gsc.site_url` |
| GA4 Data API | `data_sources.ga4.property_id` |
| Google OAuth file | `auth.oauth_file` (both sites: `~/.reusable-agents/seo/.oauth.json`, keys `client_id`, `client_secret`, `refresh_token`) |
| Production Postgres | `data_sources.db.queries_file` plus `$<dsn_env>` |
| Live site | `robots.txt`, homepage, `sitemap.xml`, plus the LLM-audit crawl |
| GSC URL-inspection history | `$GSC_INSPECT_STATE_DIR/<site>-coverage.jsonl` (default `~/.reusable-agents/gsc-coverage-auditor/`), written by `*-gsc-coverage-auditor`. This drives the `gsc-coverage-*` recs. |
| Active goals + goal-change history | `framework.core.goals` / `goal_changes`, injected into the LLM prompt |
| Prior runs | Storage `agents/<agent_id>/runs/*/` (prior snapshots and handled-rec keys) |
| Competitor parity gaps | `~/.reusable-agents/competitor-research-agent/runs/<site>/<latest>/parity-gaps.json`, pre-traffic mode only (`_add_competitor_keyword_recs`) |

**Configured but not consumed.** The collector was reconstructed on 2026-08-14
and does **not** produce `pages-by-type.jsonl`, `articles-inventory.json`,
`repo-routes.json`, `ads-*.json`, or the derived
`gsc-{top5-targets,striking-distance,zero-click,rank-regressions}.json`. The
`page_inventory`, `articles` (apart from `url_template`, which the implementer
uses), and `data_sources.google_ads` blocks still pass schema validation, but
nothing in this pipeline reads them. The analyzer rules that depend on
those files currently emit nothing. `site-signals.json` also lacks the keys
some site-wide rules read (`robots.bots`, `robots.sitemap_directive`,
`homepage.hreflang_links`, `homepage.footer_trust_links`), so those rules are
dormant too. See the README rec-type catalog.

## Outputs

| Output | Detail |
|---|---|
| Run dir (storage) | `agents/<agent_id>/runs/<run_ts>/`: `data/*` (listed above, plus `pages.jsonl` from the audit crawl), `snapshot.json`, `recommendations.json`, `goals.json`, `goal-progress.json`, `decisions.jsonl`, `progress.json`, `context-summary.md`. The implementer adds `_ship_status.json`, `applied-recs.json`, `changes/`, `verifications/`, and more. |
| Recommendations | At most 12 per run. Each has `id` (`rec-NNN`), `type`, `priority`, `title`, `rationale`, `implementation_outline`, `data_refs`, `work_type`, and `handoff_target`. |
| Rec routing | Both sites set `auto_implement: false` (since 2026-05-13), so the finalizer does **not** dispatch. `backlog-dispatcher-agent` (every minute) walks `run-index.json`, picks up unshipped recs that are not marked duplicate/deferred/skipped, and calls `dispatch_now()` directly, which starts the `implementer`. The `auto-queue` + `auto-queue-drainer` path is only a fallback. On 2026-09-23 the dispatcher picked up each SEO run within about 1 minute of it finishing (`/tmp/reusable-agents-logs/agent-backlog-dispatcher-agent.log`). The implementer (`agents/implementer/run.sh`) sends recs that carry a `handoff_target` to `agents/<handoff_target>/handoff-queue/` instead of editing code. The framework returns **generic** ids (`article-proposal-agent`, `head-to-head-agent`, `indexnow-submitter`), and no registered agent has those ids. On 2026-09-23 those three queues held 4, 1 and 93 pending envelopes (oldest 2026-05-05) and none processed, so these handoffs currently go nowhere. Only a `site_handler_overrides` entry that maps to a real per-site id (specpicks: `head-to-head-agent` → `specpicks-head-to-head-agent`) reaches a live agent. |
| Email | One HTML report per run goes into the digest queue. The subject template is `[SEO:{site}] run {tag} — {recs_count} recs`. The recipient is `reporter.email.to` (`mperry@northernsoftwareconsulting.com` from `automation@northernsoftwareconsulting.com`). No email is sent directly (`send_run_summary_email = False`). |
| `RunResult` | `success` with summary `"N recommendations"`, or `"short-circuit: replayed N recs"`. `failure` with `"collector exited rc=N"`, `"analyzer exited rc=N"`, or `"SEO_AGENT_CONFIG not set"`. |

## Goals & metrics

The initial goals came from `install/seed-default-goals.sh` (the `SEO_GOALS`
block for both sites, plus `AP_REV_GOALS` for aisleprompt). They live in
storage and have since diverged from the script (targets edited;
`goal-recs-*` goals added outside it). The current per-site lists are in each
instance's `AGENT.md`.

**`RunResult.metrics` emitted, as verified in `run-index.json` on 2026-09-23:**
`rec_count`, `eeat_recs_count`, `cwv_recs_count`, `ai_search_recs_count`,
`internal_link_recs_count`, `schema_recs_count`, `recs_shipped_count`.

**Keys that are declared but never emitted today:**
- `_collect_north_star_metrics` would also emit `top5_keyword_count`,
  `striking_distance_query_count`, `zero_click_query_count`,
  `rank_improvements_28d` / `rank_regressions_28d`,
  `mean_position_top_queries`, `mean_ctr_pct`, `organic_*_28d`, and
  `llm_referral_sessions_28d`. It reads them from files at the run-dir root
  that the collector does not write, so goals bound to these keys never
  update.
- `eeat_recs_count`, `cwv_recs_count` and `ai_search_recs_count` have been 0
  on every run since the 2026-08 reconstruction. LLM-audit check ids are
  mapped to generic rec types (`eeat-*`/`geo-*`/`llm-search-*` become
  `content-expansion`, `cwv-*` becomes `ssr-fix`), and the deterministic
  `eeat-*`/`cwv-*` rules need `pages-by-type.jsonl`, which is not produced. The
  goals bound to them read "accomplished" (target 0) without meaning anything.
- `schema_recs_count` and `internal_link_recs_count` **do** move: they match
  the LLM rec types `schema-markup` and `internal-link` (non-zero as recently
  as 2026-09-16/18 on specpicks and 2026-09-18/23 on aisleprompt). They count
  recs in one run, not a site property.
- `recs_shipped_count` counts recs already shipped at emission time, which is
  always 0.

**GSC and revenue goals are stale.** Goals scored from the snapshot
(`gsc_90d.*`, `revenue_28d.*`) were last updated on 2026-07-28. Two things
stop them today:
1. The analyzer never finds the prior run's snapshot (see Failure modes), so
   it writes no `goal-progress.json`.
2. Even with a prior snapshot it would score the ids in the prior run's
   `goals.json`. Before the fleet-host loss that file carried the stored
   `goal-*` ids (checked in the `20260728T161501Z` run). The reconstructed
   analyzer writes only its tier goals (`pretraffic-*` / `growth-*` /
   `mature-*`), and `record_goal_progress` rejects ids that are not in the
   agent's stored goals.

## Configuration

**Environment** (names only; values live in the unit or `secrets.env`):

| Variable | Default | Meaning |
|---|---|---|
| `SEO_AGENT_CONFIG` | required | Path to the site's `site.yaml` |
| `AGENT_ID` | `seo-opportunity-agent` | Per-site id, set by the unit and the entry command |
| `SEO_DISABLE_UNCHANGED_SHORTCIRCUIT` | unset | Set to `1` in **both** entry commands, which turns off the analyzer's snapshot-replay short-circuit |
| `SEO_MIN_RERUN_HOURS` | `6` | Minimum interval for that short-circuit (only matters when it is enabled) |
| `SEO_DISABLE_HANDLED_DEDUPE` | unset | `1` skips filtering of already-handled recs |
| `SEO_DISABLE_LLM_AUDIT` | unset | `1` skips the LLM audit pass |
| `GSC_INSPECT_STATE_DIR` | `~/.reusable-agents/gsc-coverage-auditor` | Location of `<site>-coverage.jsonl` |
| `SEO_OAUTH_FILE` | `~/.reusable-agents/seo/.oauth.json` | Default for `refresh-token.py --oauth-file` |
| `SEO_AGENT_CLIENT_ID` / `SEO_AGENT_CLIENT_SECRET` | none | Fallback OAuth client pair (from `secrets.env`) |
| `GSC_READONLY` | unset | `1` requests `webmasters.readonly` instead of read-write |
| `SEO_OAUTH_NO_BROWSER` | unset | Print the consent URL instead of opening a browser (for `--bootstrap`) |
| `DIGEST_ONLY` | `1` (set by the wrapper) | Keeps the standalone reporter digest-only |
| DSN var named by `data_sources.db.dsn_env` | none | aisleprompt `DATABASE_URL_AISLEPROMPT` (from `secrets.env`); specpicks `DATABASE_URL` (inline in its manifest) |

**LLM provider:** `framework.core.ai_providers.ai_client_for("seo-analyzer")`,
which `analyzer.ai_provider` / `analyzer.ai_model` in `site.yaml` can override.
On 2026-09-23 the log shows `claude-cli` with `claude-sonnet-4-6`.

**`site.yaml` knobs read by the engine code:**
- `site.{id,domain,mode}`
- `data_sources.{gsc.site_url, gsc.default_country_filter, ga4.property_id, db.{dsn_env,queries_file}}`
- `coverage_targets.*`
- `analyzer.{max_recs_per_run, max_llm_audit_pages, pre_traffic_impr_threshold, primary_objective, coverage_target_files, ai_provider, ai_model}`
- `revenue_kpis`, `revenue_focus`
- `handoff_routes`, `site_handler_overrides`
- `reporter.{email,dashboard}`
- `auth.oauth_file`
- `runs_root` (legacy local mode only)
- `auto_implement`

`implementer.*` and `deployer.*` are read by the implementer and deployer, not
by this engine.

**Present in the site configs but read by no code.** Checked with grep across
all three repos on 2026-09-23:
- `analyzer.min_impressions_for_target`, `analyzer.us_traffic_weight`
- `analyzer.geo_signals_checklist`, `analyzer.geo_priority_pages`
- `analyzer.indexation_stall`, `analyzer.conversion_stall`
- `analyzer.archived_redirect_check`, `analyzer.llms_txt_check`
- `analyzer.ranking_recovery`, `analyzer.competitor_feature_gaps`
- the top-level `geo` block (there is no `_add_geo_recs` in the analyzer)
- `page_inventory`
- `articles`, except `articles.url_template`, which `agents/implementer/run.sh`
  reads for article URLs

The schema allows extra `analyzer.*` keys, so these load without error but
change nothing. Any **new top-level** key must be added to the schema first
(see README → Troubleshooting).

## Short-circuit & idempotency

- **`signals()` does nothing today.** The run-level hook hashes
  `inputs/page-inventory.json`, `inputs/gsc-coverage.json`, and `queue/`. None
  of these keys exist in storage (checked 2026-09-23), and `run()` returns no
  `next_state`, so the hash is never saved (`state/latest.json` has
  `state: {}`). Every tick runs in full.
- The **analyzer's snapshot-replay short-circuit** is turned off in production
  by `SEO_DISABLE_UNCHANGED_SHORTCIRCUIT=1`. Even with it on, it could not
  fire, because the prior snapshot is not found.
- **Idempotency relies on dedup:**
  - the analyzer's handled-rec filter. It is weakened by the listing cap (see
    Failure modes) and covers only a few rec types; no run in the 2026-09-23
    logs loaded any handled-rec keys,
  - AgentBase `post_run` title dedup, which marks repeated titles
    `duplicate: true` (on 2026-09-23: 4 of 12 specpicks recs and 8 of 12
    aisleprompt recs),
  - the backlog dispatcher skipping `duplicate` / `shipped` / `deferred` /
    `skipped` recs.

## Running & inspecting

```bash
systemctl --user start agent-specpicks-seo-opportunity-agent.service     # one full run
systemctl --user list-timers | grep seo-opportunity
tail -f /tmp/reusable-agents-logs/agent-specpicks-seo-opportunity-agent.log
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/specpicks-seo-opportunity-agent/runs?limit=5   # token: ~/.reusable-agents/secrets.env
```

The log is very noisy. To see just the phase lines:

```bash
grep -v -E '^\[claude-cli|BlobArchived|RequestId|^Time:|^Content:' \
  /tmp/reusable-agents-logs/agent-aisleprompt-seo-opportunity-agent.log | tail -40
```

- **Re-run one phase against an existing run:**
  `SEO_AGENT_CONFIG=<site.yaml> python3 lib/analyzer/analyzer.py --agent-id <id> --run-ts <ts>`.
  The collector takes the same flags.
- **Render the report without sending:**
  `SEO_AGENT_CONFIG=<site.yaml> python3 lib/reporter/send-report.py --agent-id <id> --run-ts <ts> --dry-run`
  (prints the HTML). Without `--agent-id` the script looks for a local run dir
  under `runs_root`, which production runs do not use.
- There is no dry-run for the whole pipeline. `AGENT_FORCE_RUN=1` bypasses the
  AgentBase short-circuit, but that short-circuit never fires today anyway.

## Failure modes & troubleshooting

| Symptom | Cause / action |
|---|---|
| `collector exited rc=1`, `token mint failed`, `invalid_grant` | The Google refresh token was revoked. While the consent screen is in "Testing" mode, tokens die after 7 days without use. Run `bash install/refresh-gsc-token.sh status` → `refresh` → `reauth` (reauth needs a browser), or `bash install/reauth-gsc.sh`, which also verifies the granted scope. There is also the `/refresh-gsc-token` skill. `oauth-heartbeat-agent`, which keeps the 7-day clock alive, is registered, but its **timer is disabled** as of 2026-09-23. |
| Run fails about 1 s after the tick with "Config validation failed" | `site.yaml` has a key that the schema rejects (`additionalProperties: false`). Add the key to `shared/schemas/site-config.schema.json` first. This happened on 2026-05-04 with `articles.url_template`. |
| Thousands of `BlobArchived` errors, and `⏭ no prior snapshot — skipping comparison` on **every** run | `RunDir._mirror_prior_runs_for_analyzer` and `_load_handled_rec_keys` enumerate prior runs with `storage.list_prefix()`. That call is capped at 10,000 keys in lexicographic order, so it returns the **oldest** runs (many already moved to the archive tier). The immediately previous run is never mirrored. The log shows it reading `goals.json` from April–May 2026 runs that are archived. As a result `comparison.json` and prior-goal scoring never run (GSC goals are stuck at their 2026-07-28 values; see Goals & metrics for the second cause), and the handled-rec filter scans stale runs. The diff and inbound-link regression rules also never have usable prior data, since only `snapshot.json` and `goals.json` are mirrored, never `data/`. `storage.list_child_prefixes()` documents the same trap (seen 2026-08-14). The fix belongs in framework code. |
| Run "succeeds" in about 2 minutes with 5–8 recs and none from the LLM audit | The LLM audit was skipped or failed; the analyzer catches that and ships rule-pass recs only. Seen on most runs from about 2026-09-19 13:30 UTC to 2026-09-22 on both sites (for example aisleprompt `20260921T101500Z`, specpicks `20260921T073000Z`: `data/pages.jsonl` was written, zero recs carry `llm_check_id`). The cause is unverified; the log for those days has rotated. Look for `LLM audit failed:` / `LLM audit skipped` in the log and check the claude-pool. |
| `collector exited rc=1` with a psycopg2 connection error | The DSN in `$<dsn_env>` is wrong, rotated, or unreachable. `psycopg2.connect()` is outside the collector's error handling, so this fails the whole run instead of degrading `db-stats.json`. |
| `[finalize] dashboard ping failed: … Connection refused` | Harmless. Both sites set `reporter.dashboard.base_url: http://localhost:8080`, and nothing listens on that port. |
| `LLM audit failed: No module named 'llm_audit'` / `collector exited rc=2` | This happened in August 2026. A bare `lib/` pattern in `.gitignore` had kept `lib/{collector,analyzer}` out of git, and the fleet-host loss deleted them. They were reconstructed on 2026-08-13/14 (`fa2cc8b`, `4cf975f`) and are now tracked. If this recurs, check `git ls-files agents/seo-opportunity-agent/lib`. |
| Every cron run showed `status=1/FAILURE` despite success | Fixed on 2026-05-05 (`bded9ab`). The cause was `cond and 0 or 1` in `main()`. |
| Same high-priority rec fires every run | Before 2026-08-18, detectors treated "unmeasured" as "broken". The rule now is that an unmeasured signal never ships a rec (`fa5548c`). If a `new-page-<type>` rec keeps firing, check that its `sitemap_pattern` matches the real URLs. |
| Recs produced but never implemented | Check whether `backlog-dispatcher-agent` is running (its log should show `persisted run_dir copy → …rundir-<id>-<run_ts>…`), `config/implementer-allowed-dispatch-kinds.json` (`allow` was `['*']` on 2026-09-23), and whether the recs are flagged `duplicate`. `auto-queue-drainer.service` matters only for the fallback path. |

## Related agents

- **Instances:** `aisleprompt: agents/seo-opportunity-agent/AGENT.md`,
  `specpicks: agents/seo-opportunity-agent/AGENT.md`.
- **Upstream:** `*-gsc-coverage-auditor` (coverage JSONL);
  `oauth-heartbeat-agent` (token keep-alive; timer disabled).
- **Downstream:** `backlog-dispatcher-agent` → `implementer` → `deployer`,
  with `auto-queue-drainer` only as the fallback; `digest-rollup-agent`
  (email); handoff targets `article-proposal-agent`, `head-to-head-agent`,
  `indexnow-submitter` (generic queues that nothing drains; see Outputs →
  Rec routing).
- **Goal owner:** `*-site-goals-tracker` owns the site-level organic goals
  that the aisleprompt `site.yaml` header points to.
- **Legacy:** `reusable-agents: agents/seo-analyzer/` is a standalone copy of
  the analyzer. It has **diverged** from `lib/analyzer/analyzer.py` (2026-08-29,
  `c2de01e` changed only the lib copy). Production runs the `lib/` copy.
