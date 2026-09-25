# Site Goals Tracker — shared engine (`agents/site-goals-tracker`)

> Once a day, records each site's canonical North Star numbers (organic
> clicks + impressions, AI-assistant sessions, affiliate/Instacart outbound
> clicks, indexing coverage, active page count) into the framework goals
> store, so the Goals tab, the `goals-tracker` digest and the manifests
> whose `target_metric` names one of these goal ids have a real series.

This directory is the **engine only**: a single `agent.py` with no manifest.
It is not registered under its own id. Two per-site instances run it:

| Instance id | Wrapper (entry command) | Home repo |
|---|---|---|
| `aisleprompt-site-goals-tracker` | `aisleprompt/agents/site-goals-tracker/run.sh` → `agent.py --site=aisleprompt` | aisleprompt (instance runbook: aisleprompt `agents/site-goals-tracker/AGENT.md`) |
| `specpicks-site-goals-tracker` | `nsc-assistant/agents/specpicks-site-goals-tracker/run.sh` → `agent.py --site=specpicks` | nsc-assistant |

Both instance dirs also hold an `agent.py` symlink to this file; the
wrappers call the engine through `$RA_REPO/agents/site-goals-tracker/agent.py`.

The module docstring in `agent.py` is out of date. It says both wrappers
live in nsc-assistant and set `SITE_GOALS_SITE`. The aisleprompt wrapper
moved to the aisleprompt repo on 2026-05-12 (aisleprompt commit e1b5a2c6),
and both wrappers now pass `--site=`.

## At a glance

| | |
|---|---|
| Kind | Plain Python script, **not** an AgentBase subclass. No `progress.json`, no run-index, no `signals()`. The only run status is the start/finish status written by `framework/agent_run_wrapper.sh`. |
| Schedule | Instances: manifest `cron_expr: "0 13 * * *"`, `timezone: "UTC"`. The systemd timers use `OnCalendar=*-*-* 13:0:00` with no zone suffix, so they fire at **13:00 host-local (America/Detroit)**. On 2026-09-23 the runs started at 17:00:09Z (13:00 EDT). `Persistent=true`, so a missed run fires at boot. The extra run on 2026-09-23 started 03:10:48Z, 13 s after the host booted (03:10:35Z), which fits that catch-up. |
| Why 13:00 | After the GSC coverage auditors (`aisleprompt-gsc-coverage-auditor` 11:30, `specpicks-gsc-coverage-auditor` 12:00, same local clock), so the coverage JSONL is fresh. |
| Entry | `python3 agents/site-goals-tracker/agent.py --site=<aisleprompt\|specpicks> [--run-ts TS] [--no-write]` |
| Category | seo |
| Status | Live via both instances (timers enabled, last run 2026-09-23 17:00Z, both recorded) |

## What it does

`main()` in `agent.py`:

1. **Resolve the site profile.** `--site` (or env `SITE_GOALS_SITE`) must be a
   key of `SITE_PROFILES` (`aisleprompt`, `specpicks`); anything else exits
   with an error. The profile holds the GSC property, GA4 property id, DB env
   var name, conversion events, first-party click SQL, page-count SQL and the
   instance `agent_id`.
2. **Write goal definitions** (`write_goal_definitions` → `goals.init_goals`).
   Idempotent merge: existing goals keep `progress_history`, `current` and
   `status`; title, description, target, direction, unit and horizon are
   refreshed from code. Goals not in the list are kept.
3. **Collect metrics** (`collect_metrics`):
   - Mint one OAuth access token via
     `agents/seo-opportunity-agent/lib/collector/refresh-token.py --oauth-file ~/.reusable-agents/seo/.oauth.json`.
     If minting fails, every GSC and GA4 step is skipped (DB and coverage
     steps still run).
   - **GSC** Search Analytics totals, last 30 days, `type: web` →
     organic clicks + impressions.
   - **GA4** sessions + engaged sessions where `sessionDefaultChannelGroup`
     is exactly `AI Assistant` (ChatGPT / Perplexity / Gemini referrals).
     Added 2026-09-15 (commit b5fa2ea) because unfiltered GA4 user counts
     are dominated by bot traffic.
   - **Conversions**: GA4 `eventCount` by `eventName`, plus first-party
     click counts from the site DB. For each conversion event the goal value
     is `max(GA4, first-party)`. Why: outbound buy links are server-side 302
     redirects, so GA4 never sees them. On 2026-08-14, GA4 reported 0 while
     the DB held thousands of clicks (commit 70593c5). SpecPicks' GA4 events
     are named `amazon_click` / `ebay_click`. They are summed through
     `ga4_event_aliases` (commit 5abd5ef, 2026-09-16).
   - **Verified human clicks only (2026-09-24).** The first-party side is
     no longer a raw row count. The click tables log every hit on the redirect
     endpoint, so on 2026-09-24 specpicks showed 21,258 Amazon "clicks" in 30d
     with **0** verified humans. Each profile's `human_clicks` block (table,
     column names, per-event match) is passed to
     `framework/core/human_clicks.py`. That module drops rows the site flagged
     `is_bot`, headless/automation UAs, stale-browser fingerprints (a Chromium
     or Firefox major more than 16 releases old), datacenter IPs (including
     Tencent, Alibaba and Huawei Singapore ranges), `exclude_countries`
     (default `SG`), clicks with no on-site referer, scanner referers and
     more than 20 clicks per IP per day. The GA4 event query drops the same
     `exclude_countries` through `countryId`. If the human query fails, that
     event is not recorded for the run; it never falls back to the raw count.
     Thresholds are overridable without a deploy via storage
     `config/human-click-filter-config.json` (`defaults` / `by_profile.<agent-id>`).
     To see the breakdown by hand, run `python3 -m framework.cli.human_clicks --help`.
   - **Active pages**: one `COUNT(*)` query against the site DB.
   - **Indexing coverage**: reads
     `~/.reusable-agents/gsc-coverage-auditor/<site>-coverage.jsonl` (written
     by the gsc-coverage-auditor), keeps the latest `coverageState` per URL,
     and computes % indexed, unknown-to-Google and crawled-not-indexed counts.
4. **Record** all metrics in one `metric_helper.record_many(agent_id, …,
   note="auto-collected via site-goals-tracker")` call, unless you pass
   `--no-write`, which only prints them. `--no-write` is checked *after*
   step 2, so the goal definitions are still written to storage.

## Inputs

| Source | What | Where configured |
|---|---|---|
| GSC Search Analytics API | 30d clicks/impressions | `SITE_PROFILES[site].gsc_site_url` (`sc-domain:<host>`) |
| GA4 Data API `runReport` | AI Assistant sessions; event counts | `SITE_PROFILES[site].ga4_property_id` |
| OAuth refresh token | shared GSC+GA4 grant | `~/.reusable-agents/seo/.oauth.json` (hardcoded `OAUTH_FILE`) |
| aisleprompt DB | `kitchen_click_events` (`source` = `amazon` / `instacart`, `created_at` 30d); `recipe_catalog` active rows | DSN from `AISLEPROMPT_DATABASE_URL`, else `DATABASE_URL_AISLEPROMPT`, else `DATABASE_URL` |
| specpicks DB | `outbound_clicks` (`target` = `amazon` / `ebay`, `clicked_at` 30d); `products` active + `editorial_articles` published + `hardware_specs` | DSN from `SPECPICKS_DATABASE_URL`, else `DATABASE_URL_SPECPICKS`, else `DATABASE_URL` |
| GSC URL Inspection cache | per-URL `coverageState` + `inspected_at` | `~/.reusable-agents/gsc-coverage-auditor/<site>-coverage.jsonl` |

On whitebeast the systemd units source `~/.reusable-agents/secrets.env`, which
defines the `DATABASE_URL_<SITE>` form. The fallback in `_db_fallback()`
exists because of that naming mismatch. It was added after a regression on
2026-05-07, when active-pages-count silently disappeared.

## Outputs

All writes go through `framework.core.metric_helper` to shared storage (Azure Blob):

| Key | Content |
|---|---|
| `agents/<instance-id>/goals/active.json` | goal definitions; `metric.current` updated each run |
| `agents/<instance-id>/goals/progress/<metric-key>.jsonl` | one appended point per run per metric key |
| `agents/<instance-id>/goals/timeseries-cache.json` | last 200 points per key, which the dashboard reads |

The engine sends no email, queues no recs and makes no handoffs.

Metric keys recorded per run (17 for aisleprompt, 16 for specpicks on 2026-09-23):

| Key | Meaning |
|---|---|
| `goal-organic-clicks-30d`, `goal-organic-impressions-30d` | GSC totals |
| `goal-ai-assistant-sessions-30d`, `ga4-ai-assistant-engaged-sessions-30d` | GA4 AI Assistant channel |
| `goal-<event>-30d` | `max(GA4, first-party)` per conversion event |
| `ga4-<event>-30d`, `firstparty-<event>-30d` | the two sides of the max. `firstparty-*` is the VERIFIED HUMAN count for events with a `human_clicks` spec. Only emitted for events that have first-party data, so the gap stays visible |
| `raw-<event>-30d` | unfiltered first-party row count (bots included), kept as a diagnostic only |
| `goal-total-conversions-30d` | sum of the `goal-<event>-30d` values |
| `goal-active-pages-count` | page-count SQL |
| `goal-indexed-pages-pct`, `goal-unknown-to-google-count`, `goal-crawled-not-indexed-count`, `goal-inspected-urls-total` | coverage JSONL |

`ga4-*`, `firstparty-*`, `raw-*` and `goal-inspected-urls-total` have no goal
definition. They appear only in the progress JSONL and the timeseries cache.

## Goals & metrics

Defined in code (`write_goal_definitions`). The current values are as
recorded on 2026-09-23 at 17:00Z.

| Goal id | aisleprompt target | specpicks target | Direction | aisleprompt current | specpicks current |
|---|---|---|---|---|---|
| `goal-total-conversions-30d` (revenue, verified human) | 1,000 | 1,100 | increase | 150 | 12 |
| `goal-instacart-cart-30d` (revenue, aisleprompt only) | 200 | — | increase | 0 | — |
| `goal-instacart-clicks-30d` (aisleprompt only, verified human) | 800 | — | increase | 15 | — |
| `goal-amazon-clicks-30d` (verified human) | 200 | 900 = 30/day (revenue) | increase | 135 | 11 |
| `goal-ebay-clicks-30d` (revenue, specpicks only, verified human) | — | 200 | increase | — | 1 |
| `goal-organic-clicks-30d` | 5,000 | 3,000 | increase | 0 | 2 |
| `goal-ai-assistant-sessions-30d` | 50 | 100 | increase | 28 | 141 |
| `goal-organic-impressions-30d` | 100,000 | 50,000 | increase | 21 | 773 |
| `goal-indexed-pages-pct` | 60 | 60 | increase | 0.04 | 29.79 |
| `goal-unknown-to-google-count` | 50 | 50 | decrease | 3,520 | 1,671 |
| `goal-crawled-not-indexed-count` | 0 | 0 | decrease | 1,456 | 404 |
| `goal-active-pages-count` | 60,000 | 25,000 | increase | 141,191 | 152,398 |

`goal-instacart-cart-30d` has no first-party SQL, so it is GA4-only and reads 0.

The five click goals above were re-based on verified human clicks on
2026-09-25 (the 2026-09-23 figures were raw rows: 21,401 / 22,840 specpicks,
5,442 / 6,261 aisleprompt). Their progress series therefore drops sharply at
that point: the definition changed, not the traffic. They had been marked
`accomplished` on bot counts and were reopened (`reopened_at` /
`reopened_reason` on each goal). The specpicks conversion values are the GA4
side of the max: GA4 saw 11 `amazon_click` events while 0 first-party rows
passed the filter.

These goal ids are the canonical `target_metric` vocabulary. See
`framework/core/registry.py` → `AgentManifest.target_metric`. The
`goals-tracker` digest uses `goal-instacart-cart-30d` (aisleprompt) and
`goal-amazon-clicks-30d` (specpicks) as each site's headline KPI.

**Caveat: `accomplished` is sticky.** `metric_helper.record_many` flips
`status` to `accomplished` the first time a value meets the target and never
flips it back, and `init_goals` does not reset it. On 2026-09-23 the aisleprompt
`goal-unknown-to-google-count` (3,520 against a target of 50) and
`goal-crawled-not-indexed-count` (1,456 against 0) both still read
`accomplished`. For health, read `metric.current` rather than `status`.

## Configuration

| Knob | Default | Meaning |
|---|---|---|
| `--site` / `SITE_GOALS_SITE` | none (required) | picks the `SITE_PROFILES` entry |
| `--run-ts` | now (UTC) | run_ts stamped on every recorded point |
| `--no-write` | off | print the metrics and skip `record_many`; goal definitions are still refreshed (step 2) |
| `AISLEPROMPT_DATABASE_URL` / `SPECPICKS_DATABASE_URL` | unset | primary DSN env per site |
| `DATABASE_URL_<SITE>`, `DATABASE_URL` | unset | fallback DSN envs (see `_db_fallback`) |
| `RA_REPO`, `SITE_GOALS_LOG` (wrapper `run.sh`) | framework repo path; `/tmp/reusable-agents-site-goals.log` | where the wrapper finds this engine and where it appends output |

Site values (GSC/GA4 property ids, SQL, targets) are hardcoded in
`SITE_PROFILES` and `write_goal_definitions`. Onboarding a third site
means editing this file. That breaks the framework-first rule in CLAUDE.md
and is known debt. The fix is to lift the profile into per-site config.

## Short-circuit & idempotency

There is no short-circuit. Each run calls the APIs and the DB again. The runs
are cheap: one token mint, 3 Google API calls (GSC totals, GA4 channel, GA4
events) and 3 SQL counts, with no LLM.
Re-running on the same day appends another point to each series. It does not
replace the earlier point. Goal-definition writes are idempotent merges.

## Running & inspecting

```bash
# Metrics dry run: prints metrics, skips record_many. Still refreshes the
# goal definitions in storage. A bare shell lacks secrets.env, so DB metrics
# are skipped unless DATABASE_URL_<SITE> is exported first.
python3 /home/voidsstr/development/reusable-agents/agents/site-goals-tracker/agent.py --site=aisleprompt --no-write

# Real run through systemd (writes to the goals store)
systemctl --user start agent-aisleprompt-site-goals-tracker.service
systemctl --user start agent-specpicks-site-goals-tracker.service

# Output: both wrappers append here, NOT to the per-unit log
tail -80 /tmp/reusable-agents-site-goals.log
```

The per-unit logs `/tmp/reusable-agents-logs/agent-<site>-site-goals-tracker.log`
stay empty, because each `run.sh` redirects everything into `SITE_GOALS_LOG`.
Each run in that log starts with a line like
`── <UTC ts> — site-goals-tracker site=<site> ──`. The two instances fire at
the same minute, so their lines interleave.

Goal data: `GET /api/agents/<instance-id>/goals` on the framework API
(`http://localhost:8090`, bearer `FRAMEWORK_API_TOKEN`), or the Goals tab.

## Failure modes & troubleshooting

| Symptom | Cause / fix |
|---|---|
| systemd reports success but no new points appear | The wrappers end in `\|\| echo "(site-goals-tracker failed)"`, so the unit always exits 0. Check `/tmp/reusable-agents-site-goals.log` for that line or for a traceback. |
| `access-token mint failed (GSC + GA4 will be skipped)` | The shared OAuth grant expired or was revoked. Re-mint it with the `refresh-gsc-token` skill (`install/fix-gsc-now.sh`). The DB and coverage metrics still record. |
| `goal-active-pages-count` missing | No DSN resolved. Check that `secrets.env` has `DATABASE_URL_<SITE>`. |
| `first-party DB N > GA4 M … using DB` | Expected. Server-side redirects are invisible to GA4. |
| Coverage goals missing | The `<site>-coverage.jsonl` file is absent. Check the gsc-coverage-auditor. |
| `cannot access local variable 'token'` (historical) | Fixed. The token is now minted once, before both the GSC and GA4 blocks. |

## Related agents

- **Upstream:** `aisleprompt-gsc-coverage-auditor` and
  `specpicks-gsc-coverage-auditor` write the coverage JSONL.
  `seo-opportunity-agent`'s `refresh-token.py` mints the token.
- **Downstream:** `goals-tracker` (daily goals digest; reads both instances'
  `active.json` + timeseries cache). Every agent whose manifest
  `target_metric` names one of these goal ids. The growth strategists
  (`aisleprompt-user-growth-strategist`, `specpicks-user-growth-strategist`)
  target `goal-total-conversions-30d`.
- **Sibling:** `agent-metrics-collector` records *per-agent* technical
  metrics. This engine records *site-level* KPIs.
