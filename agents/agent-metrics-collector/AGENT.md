# Agent Metrics Collector (`agent-metrics-collector`)

> Once a day, back-fills goal metrics for about 19 agents that do not record
> their own. It computes each agent's numbers from its latest run's
> `recommendations.json`, site DB counts, GSC coverage JSONL and host logs,
> then writes them to the goals store, so those agents' Goals-tab bars and
> the `goals-tracker` digest are not flat. It moves no site metric itself.
> Its job is to keep per-agent progress legible, which the North Star
> decision procedure depends on.

## At a glance

| | |
|---|---|
| Agent id | `agent-metrics-collector` |
| Code | `reusable-agents/agents/agent-metrics-collector/agent.py` (this dir holds the engine only; there is no manifest here) |
| Registered instance | nsc-assistant `agents/agent-metrics-collector/`: `manifest.json`, `run.sh`, `goals.json`, `SKILL.md` and a short `AGENT.md` (read-only reference; its "11:00 UTC" schedule note is stale). Its `run.sh` runs `python3 $RA_REPO/agents/agent-metrics-collector/agent.py "$@"`. |
| Kind | Plain Python script behind a bash wrapper, **not AgentBase**. No `progress.json`, run-index or `RunResult`. Status comes only from `agent_run_wrapper.sh`. |
| Schedule | manifest `0 11 * * *`, `timezone: UTC`. Timer `OnCalendar=*-*-* 11:0:00` has no zone suffix, so it fires at **11:00 America/Detroit** (15:00:09Z on 2026-09-23). Enabled, `Persistent=true`. Ordering still holds on the local clock: after the overnight per-site runs, before `goals-tracker` (12:00) and the site-goals-trackers (13:00). |
| Entry | `bash /home/voidsstr/development/nsc-assistant/agents/agent-metrics-collector/run.sh` |
| Category | ops |
| Status | Live, but degraded. The 2026-09-23 run recorded 26 metrics across 16 agents, and **every DB-derived metric failed** (see Failure modes). |

## What it does

`main()`:

1. Walks `AGENT_METRIC_FNS`, a dict of agent id → metric function with 19
   entries. `--only a,b` restricts the walk.
2. For each agent it calls the function to get a `{goal_id: value}` dict.
   An exception or an empty dict is logged and skipped.
3. Unless `--dry-run` is set, it calls
   `metric_helper.record_many(agent_id, metrics, run_ts=<now>, note="auto-collected by agent-metrics-collector")`.
   That appends `agents/<id>/goals/progress/<goal>.jsonl`, updates
   `metric.current` for goal ids that exist in `agents/<id>/goals/active.json`,
   and updates `agents/<id>/goals/timeseries-cache.json`. Keys with no goal
   definition land only in the JSONL and the cache.
4. Prints `recorded N metrics across M agents`.

### Metric functions

| Target agent(s) | Keys written | Source |
|---|---|---|
| `{aisleprompt,specpicks}-progressive-improvement-agent` | `goal-issues-found-per-run`, `goal-issues-fixed-30d`, `goal-quality-score-trend` | latest `recommendations.json` rec count; recs flagged implemented/shipped in 30 days; DB % of active rows with an image (plus `category_confidence ≥ 0.5` for specpicks) |
| `{…}-competitor-research-agent` | `goal-competitor-pages-analyzed-30d` (rec count × 5, a rough estimate), `goal-content-gaps-found` | latest `recommendations.json` |
| `{…}-article-proposal-agent` | `goal-articles-published-30d`, `goal-articles-indexed-pct` | `editorial_articles` published in 30 days; `~/.reusable-agents/gsc-coverage-auditor/<site>-coverage.jsonl`, filtered to `/reviews/`, `/blog/` and `/articles/` URLs |
| `{…}-catalog-audit-agent` | `goal-broken-records-fixed-30d`, `goal-catalog-health-pct` | shipped recs; DB health % |
| `aisleprompt-kitchen-scraper` | `goal-recipes-scraped-30d`, `goal-scrape-success-rate` | `recipe_catalog` created in 30 days; the success rate is a **hardcoded 90.0** |
| `specpicks-scraper-watchdog` | `goal-watchdog-incidents-7d`, `goal-scrape-coverage-pct` | counts lines from the last 7 days in the last 2,000 lines of `/tmp/reusable-agents-logs/agent-specpicks-scraper-watchdog.log` that start with `──` and contain stale, incident or error; coverage is a **hardcoded 100.0** |
| `{…}-seo-opportunity-agent` | `goal-recs-shipped-30d`, `goal-recs-emitted-per-run` | run dirs in storage |
| `specpicks-benchmark-research-agent` | `goal-benchmarks-added-30d`, `goal-hardware-coverage-pct` | `gaming_benchmarks`, `ai_benchmarks`, `synthetic_benchmarks`, `hardware_specs` |
| `specpicks-ebay-product-sync-agent` | `goal-ebay-products-synced-30d`, `goal-sync-success-rate` | `ebay_listings.last_synced_at`; the success rate is a **hardcoded 95.0** |
| `specpicks-product-hydration-agent` | `goal-products-refreshed-30d`, `goal-freshness-pct` | `products.updated_at` |
| `specpicks-head-to-head-agent` | `goal-comparisons-published-30d` | `trending_comparisons.discovered_at`, `is_active` |
| `aisleprompt-user-growth-strategist` | `goal-strategy-recs-per-run`, `goal-strategy-recs-implemented-30d` | run dirs in storage |
| `responder-agent` | `goal-responder-dispatches-30d`, `goal-responder-uptime-pct` | `agents/responder-agent/auto-queue-processed/` keys starting `r-`; uptime is a **hardcoded 100.0** |
| `agent-doctor` | `goal-doctor-checks-7d`, `goal-doctor-issues-found-7d` | **both hardcoded** (7 and 0) |

The "latest run" and "shipped in 30 days" helpers list
`agents/<id>/runs/` in storage and read `recommendations.json`. They read
up to 5 runs for the latest-run count and up to 200 runs for the shipped
count.

Treat the hardcoded placeholders as noise, not signal. The functions name
sites and tables literally, which breaks the framework's no-site-literals
rule. The intended retirement path is written at the top of `agent.py`:
when an agent gains native `metric_helper` / `RunResult.metrics`
recording, delete its entry here.

## Inputs

- Shared storage (Azure Blob): `agents/<id>/runs/*/recommendations.json`,
  `agents/responder-agent/auto-queue-processed/`.
- Site DBs, through `db(site)`. It resolves `AISLEPROMPT_DATABASE_URL` /
  `SPECPICKS_DATABASE_URL`, then a JSON map in `DATABASE_URLS`, then
  `DATABASE_URL`.
- `~/.reusable-agents/gsc-coverage-auditor/<site>-coverage.jsonl`.
- `/tmp/reusable-agents-logs/agent-specpicks-scraper-watchdog.log`.

## Outputs

Goal points only, under `agents/<target-id>/goals/…` for each target agent.
It sends no email, queues no recs, and does not record metrics for itself.

## Goals & metrics

Goals come from nsc-assistant `agents/agent-metrics-collector/goals.json`
(seeded 2026-08-18) and are mirrored in storage:

| Goal id | `target_metric` | Target | Current (2026-09-23) |
|---|---|---|---|
| `goal-agents-backfilled-daily` | `n_agents` | 19 | 0 |
| `goal-metrics-recorded-daily` | `n_metrics` | 60 | 0 |
| `goal-backfill-freshness` | `n_metrics` | 1 | 0 |

These have **never recorded progress**. `n_agents` / `n_metrics` exist
only as local variables printed at the end of a run. The script is not
AgentBase, so no `RunResult.metrics` is auto-tracked, and it never calls
`record_many` for its own id. The goals file itself says "Metric binding
unverified". To fix it, record `{"n_agents": …, "n_metrics": …}`-bound
goal values at the end of `main()`, or convert to AgentBase.

## Configuration

| Knob | Default | Meaning |
|---|---|---|
| `--only` | all | comma-separated agent ids |
| `--dry-run` | off | compute and print, write nothing |
| `AISLEPROMPT_DATABASE_URL`, `SPECPICKS_DATABASE_URL`, `DATABASE_URLS`, `DATABASE_URL` | unset | DSN lookup chain (above) |
| `STORAGE_BACKEND` + Azure storage envs | from `secrets.env` | goals store |
| `RA_REPO`, `METRICS_COLLECTOR_LOG` (wrapper) | framework path; `/tmp/reusable-agents-metrics-collector.log` | engine location and log file |

## Short-circuit & idempotency

There is no short-circuit. Every run re-reads the run dirs and re-queries
the DBs. It is not idempotent within a day: a second run appends a second
point to every series.

## Running & inspecting

```bash
python3 /home/voidsstr/development/reusable-agents/agents/agent-metrics-collector/agent.py --dry-run --only agent-doctor   # compute + print only
systemctl --user start agent-agent-metrics-collector.service                                                          # real run
grep -v 'BlobArchived\|RequestId\|^Time:\|^Content:\|^ErrorCode' /tmp/reusable-agents-metrics-collector.log | tail -60
```

`/tmp/reusable-agents-logs/agent-agent-metrics-collector.log` stays empty,
because the wrapper redirects all output to `METRICS_COLLECTOR_LOG`. The
wrapper also ends in `|| echo "(agent-metrics-collector failed)"`, so
systemd always reports success.

## Failure modes & troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No DB DSN resolved for site 'aisleprompt'/'specpicks'` for every DB metric (seen in the 2026-09-23 run: progressive-improvement quality, article counts, catalog health, kitchen-scraper, benchmark, eBay, hydration and h2h all failed; 3 agents recorded nothing) | `~/.reusable-agents/secrets.env` defines `DATABASE_URL_AISLEPROMPT` / `DATABASE_URL_SPECPICKS`, but `db()` only looks for `<SITE>_DATABASE_URL`, `DATABASE_URLS` and `DATABASE_URL`. `site-goals-tracker` hit the same mismatch on 2026-05-07 and added a fallback (`_db_fallback`). This collector needs the same fix. |
| Hundreds of `BlobArchived … This operation is not permitted on an archived blob` lines (770 lines, 385 failed reads, on 2026-09-23) | The shipped-in-30-days scan reads up to 200 old run dirs, including archive-tier blobs from run dirs dated 2026-04-26 to 2026-05-25. The reads fail harmlessly, but they are slow and noisy. Stopping at the 30-day cutoff by run-dir name would avoid them. |
| Placeholder values look healthy (100% uptime, 95% sync success) | They are constants, not measurements. |
| Its own goals stay at 0 | See *Goals & metrics*. |

## Related agents

- **Writes goals for:** the 19 agents in `AGENT_METRIC_FNS` (table above).
- **Downstream:** `goals-tracker` (the 12:00 daily goals digest reads
  these caches).
- **Sibling:** `site-goals-tracker`, which records site-level KPIs such as
  organic clicks and conversions. This agent records per-agent technical
  metrics. Engine runbook: `agents/site-goals-tracker/AGENT.md`.
- **Upstream data:** the gsc-coverage-auditors (coverage JSONL), and each
  target agent's run dirs.
