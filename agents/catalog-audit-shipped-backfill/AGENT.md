# Catalog-Audit Shipped Backfill (`catalog-audit-shipped-backfill`)

> Closes the lifecycle of catalog-audit recs. It checks the prod DB to confirm
> that each `implemented` catalog-audit migration actually took effect, then
> flips the rec to `shipped: true`. Catalog fixes (dead recipe images, bad
> nutrition, duplicate recipes) are DB-only, so the deployer never runs for
> them and nothing else marks them shipped. This is dashboard and goal
> bookkeeping. It serves the North Star only indirectly, by keeping "did the
> catalog cleanup land?" visible. It does not change the site itself.

## At a glance

| | |
|---|---|
| Agent id | `catalog-audit-shipped-backfill` |
| Home | `reusable-agents/agents/catalog-audit-shipped-backfill/` |
| Kind | AgentBase python (fleet-wide ops agent; reconciles both sites in one run) |
| Schedule | manifest `*/30 * * * *` UTC; systemd `OnCalendar=*-*-* *:0/30:00`, `Persistent=true`, timer **enabled** |
| Entry command | `agent_run_wrapper.sh catalog-audit-shipped-backfill` → `DATABASE_URL_AISLEPROMPT=… DATABASE_URL_SPECPICKS=… PYTHONPATH=<reusable-agents> python3 agent.py` (DSNs inline in the manifest `entry_command`) |
| Category | `ops` |
| Status | live. Every run since at least 2026-09-22 reports `0/0` (no candidates); `total_shipped_flipped` is 0 |
| Docs | this runbook · [`README.md`](README.md) (quick reference) · [`SKILL.md`](SKILL.md) (Claude Desktop task stub) · origin: `agents/implementer/catalog-audit-shipped-backfill.py` (the older one-shot script this wraps) |

## What it does

`run()` in `agent.py`:

1. For each site in the hard-coded list `("aisleprompt", "specpicks")`:
   1. Connect using `DATABASE_URL_AISLEPROMPT` / `DATABASE_URL_SPECPICKS`
      (`connect_timeout=10`, autocommit so one failing verifier cannot
      poison the rest). If the env var is missing or the connection fails,
      the site is logged as `warn`/`error` and marked `skipped`.
   2. List `agents/<site>-catalog-audit-agent/runs/` in framework storage and
      take the **30 most recent** `recommendations.json` files.
   3. For every rec with `implemented: true` and no `shipped`, run
      `_verify_migration_applied()` (table below).
   4. When the verifier confirms, set `shipped: true`, `shipped_at`,
      `shipped_via: "catalog-audit-shipped-backfill"` and
      `shipped_verification` (the verifier's reason string), then write the
      file back to storage.
2. Return `success` with `"Backfilled N shipped flag(s) from M candidate(s).
   aisleprompt=f/c, specpicks=f/c [skipped: …]"`. A missing DSN or a failed
   DB connect only shows as `skipped` in the summary; the run still returns
   `success`. A storage error (list/read/write) is not caught inside `run()`,
   so it fails the run.

### Verifiers (`_verify_migration_applied`)

The rec's `check_id` (or `category`) selects a query. `ref_ids` come from
`rec.ref_ids` or `rec.migration_template.ref_ids`, coerced to int, because
older runs stored strings, which fails `integer = text` in `ANY()`.

| `check_id` | Passes when |
|---|---|
| `recipe-image-present` | none of `ref_ids` is still `is_active` in `recipe_catalog` |
| `recipe-nutrition-sanity` | none of `ref_ids` still has calories/protein/carbs/fat |
| `product-image-present` | none of `ref_ids` is still `is_active` in `kitchen_products` |
| `recipe-image-valid-url` | none of `ref_ids` still has a non-empty `image_url` |
| `recipe-video-present` | none of `ref_ids` is active with a non-empty `video_url` |
| `recipe-duplicate-source-url` | none of `ref_ids` is still active |
| `product-schema-rich-results` | always passes ("schema fix — assume shipped after commit") |
| anything else | never passes (`no verifier for check_id=…`) |

Every table-backed verifier queries **AislePrompt** tables (`recipe_catalog`,
`kitchen_products`). Six of the seven `check_id`s appear in the catalog-audit
engine's criteria map (`agents/catalog-audit-agent/agent.py`), all against
AislePrompt tables; `recipe-video-present` is not in that map. A SpecPicks rec
with any other `check_id` is checked but never flipped.

## Inputs

- Storage: `agents/aisleprompt-catalog-audit-agent/runs/*/recommendations.json`
  and `agents/specpicks-catalog-audit-agent/runs/*/recommendations.json`. The
  rec fields used are `implemented`, `shipped`, `check_id`/`category`,
  `ref_ids`, and `migration_template.ref_ids`.
- Prod DBs via `DATABASE_URL_AISLEPROMPT` and `DATABASE_URL_SPECPICKS` (read-only
  `SELECT COUNT(*)` queries).
- Upstream: `<site>-catalog-audit-agent` produces the recs. The `implementer`
  (catalog-audit dispatch, runbook `agents/implementer/CATALOG_AUDIT.md`)
  marks them `implemented`, and the site's startup migration runner applies
  the SQL at the next container boot (`initDB()` in aisleprompt
  `src/simple-server.ts`). catalog-audit dispatches skip the deployer, so
  that boot comes from some later code deploy.

## Outputs

- Rewrites the source agent's `recommendations.json` in storage, and only
  when at least one rec in that file flipped.
- Standard AgentBase artifacts: `runs/<run_ts>/progress.json` and
  `decisions.jsonl`, with `scan` / `done` / `warn` / `error` categories, and
  `state/latest.json`.
- Run summary: successful runs are queued into the daily digest addressed to
  the manifest `owner`. The routine `Backfilled 0 …` text does not match the
  framework's no-op pattern, so every run queues an entry.
- No recs, handoffs or DB writes.

## Goals & metrics

`RunResult.metrics`:

| Key | Meaning |
|---|---|
| `total_shipped_flipped` | cumulative flips, carried in `next_state` |
| `this_run_flipped` | flips this run |
| `this_run_checked` | candidate recs checked this run |

Goals from `goals.json`, as served by `GET /api/agents/catalog-audit-shipped-backfill/goals` on 2026-09-23:

| Goal id | target_metric | Current → target |
|---|---|---|
| `goal-recs-reconciled-total` | `total_shipped_flipped` | 0 → 50 recs |
| `goal-verification-throughput` | `this_run_checked` | 0 → 10 recs/run |
| `goal-flips-confirmed-per-run` | `this_run_flipped` | 0 → 5 recs/run |

The class also declares an `init_goals` entry (`shipped-lifecycle-coverage`,
target 200), but it does not appear in the served goal set.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DATABASE_URL_AISLEPROMPT` | none (site skipped) | AislePrompt DSN |
| `DATABASE_URL_SPECPICKS` | none (site skipped) | SpecPicks DSN |
| `AGENT_FORCE_RUN` | unset | framework escape hatch that bypasses the auto short-circuit |

There are no other knobs. The site list, the 30-file window and the verifier
map are code constants. Framework-first debt: the site names and
AislePrompt table names are literals in a framework-repo agent. Lift them to
config the next time this file is edited.

## Short-circuit & idempotency

- **Idempotent.** It only ever flips `shipped: false → true`, never back, and
  skips recs already shipped.
- `signals()` hashes the 30 newest catalog-audit `recommendations.json` keys
  per site, but **the short-circuit never fires**. `run()` returns a fresh
  `next_state` (`total_shipped_flipped`, `last_run_at`) that does not carry the
  framework's `_auto_signals_hash`, so the stored hash is dropped every run.
  Confirmed by `state/latest.json` (no hash key) and 50 of 50 recent runs
  doing full work. The fix, **not yet applied**, is to merge `self.state` into
  `next_state`. The runs are cheap (about 10 s, no LLM), so the cost is only
  noise.

## Running & inspecting

```bash
systemctl --user start agent-catalog-audit-shipped-backfill.service   # run now
tail -f /tmp/reusable-agents-logs/agent-catalog-audit-shipped-backfill.log
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  "http://localhost:8090/api/agents/catalog-audit-shipped-backfill/runs?limit=10"
```

There is no dry-run flag in `agent.py`. The older one-shot script
`agents/implementer/catalog-audit-shipped-backfill.py` still accepts
`--dry-run` if you need to preview flips.

## Failure modes & troubleshooting

| Symptom | Cause / action |
|---|---|
| Every run `0/0` | No catalog-audit rec in the last 30 run dirs is `implemented: true, shipped: false`. As of 2026-09-23 the AislePrompt audit writes `recommendations: []` even when its summary says "7 actionable issue(s)", because producer-history dedup (`filter_proposals_against_history`) removes issues already proposed. SpecPicks audits report 0 issues. This is a symptom, not a healthy state. The dispatched rec-001..007 never appear in recommendations.json (see the catalog-audit engine README, "Dedup and dispatch disagree"), the implementer marks them implemented 0/7, and their migrations were still absent from aisleprompt `_migrations` on 2026-09-23. This agent cannot flip anything until those are fixed. |
| `[skipped: <site>]` in the summary | The DSN env var is unset or the connection failed. Check the decisions for `connect failed`. |
| Rec checked every run but never flipped | The verifier's condition is not met (the migration did not apply), or `no verifier for check_id=…` / `no integer-castable ref_ids`. Read `shipped_verification`/decisions, then fix the migration, or add a verifier branch for a new criterion. |
| `verifier query failed: …` | The verifier SQL does not match the site DB (missing table or column). |

## Related agents

- **Upstream:** `aisleprompt-catalog-audit-agent`, `specpicks-catalog-audit-agent`
  (engine `reusable-agents: agents/catalog-audit-agent/`), and `implementer`
  (catalog-audit dispatches skip the deployer chain).
- **Sibling bookkeeping:** `deployer` marks `shipped` for code dispatches after
  a green deploy; the implementer auto-ships `pre-existing` and article-author
  recs. This agent covers the DB-only catalog-audit path.
