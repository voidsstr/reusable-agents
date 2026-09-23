# Category Integrity Agent — engine runbook (`category-integrity-agent`)

> Removes products that are provably outside a catalogue's domain from their
> category, so buying guides stop listing slow cookers. It also hands the
> upstream cause (products filed without ever being scored) to the implementer
> as a code-fix rec. Serves **conversions / Amazon purchases** and
> buying-guide trust: an ink cartridge in a games-cartridges guide is a dead
> click and a credibility hit.

Design rationale, the precision measurements and the "why only this narrow
question" argument are in [README.md](README.md). This file is the
operational runbook.

## At a glance

| | |
|---|---|
| Engine id | `category-integrity-agent`. This is the class default; instances set `AGENT_ID`. |
| Home | `reusable-agents/agents/category-integrity-agent/` (this dir): `agent.py`, `domain.py` (pure off-domain test), `test_domain.py` |
| Kind | AgentBase python **engine**. It has no `manifest.json` of its own and is not registered; per-site instances point their `entry_command` at `agent.py`. |
| Instances | `specpicks-category-integrity-agent`: `specpicks: agents/category-integrity-agent/README.md` (manifest + `site.yaml` only). No aisleprompt instance. |
| Schedule | Set per instance. specpicks: `35 */4 * * *` America/New_York → systemd `*-*-* 0/4:35:00`, timer enabled. |
| Entry command (instance) | `AGENT_ID=<id> CATEGORY_INTEGRITY_CONFIG=<site>/agents/category-integrity-agent/site.yaml PYTHONPATH=…/reusable-agents python3 …/reusable-agents/agents/category-integrity-agent/agent.py` |
| Category | catalog (instance manifest) |
| Status | engine: not registered by design. specpicks instance: live. |
| Added | 2026-08-29 (commit `864ee54`) |

## What it does

`run()`:

1. **Load vocabulary** from the instance's `site.yaml` (`domain_vocabulary`, `foreign_vocabulary`, both lower-cased). If either list is empty → `failure` "refusing to judge". It never runs blind.
2. **Measure the pipeline gap.** Among active products that have a category, count those with `category_confidence IS NULL` ("never scored") and compute `unscored_pct`.
3. **Find off-domain rows.** Select active, categorised, **unscored**, **not** `category_validated` products (joined to `categories` for the slug), and run `domain.is_off_domain(title)` on each. A title is off-domain when its tokens contain ≥1 foreign word **and** 0 domain words. Both halves are required: foreign-only would flag "gaming chair", and domain-absent-only would flag a bare model number.
4. **De-categorise** up to `CATEGORY_MAX_FIX_PER_RUN` of them: `UPDATE products SET category_id = NULL, updated_at = NOW()`. Up to 5 examples go to the decision log. It never moves a product to a guessed category, never touches `category_validated = true`, and never deletes.
5. **Recommend the code fix.** If `unscored_pct > CATEGORY_UNSCORED_ALERT`, write one rec to storage `agents/<AGENT_ID>/runs/<run_ts>/recommendations.json`.
6. Return `success` with metrics.

## Inputs

| Source | What |
|---|---|
| `CATEGORY_INTEGRITY_CONFIG` (instance `site.yaml`) | `site`, `db_env`, `domain_vocabulary`, `foreign_vocabulary` |
| Postgres (the env var named by `db_env`, else `DATABASE_URL`) | `products` (`id, asin, title, is_active, category_id, category_confidence, category_validated`), `categories` (`id, slug`) |

## Outputs

- **DB:** `products.category_id = NULL` on de-categorised rows. The site's own categoriser (specpicks `scripts/assign-categories.ts`) can re-place them later with a real confidence.
- **Rec** (only above the alert threshold): one per run, with a fresh id `cat-<UTC ts>-001`:
  - `type: categorisation-pipeline-gap`, `priority: high`
  - `evidence: {unscored_pct, site}`
  - written to `agents/<AGENT_ID>/runs/<run_ts>/recommendations.json`

  The agent does **not** queue or dispatch it itself, even though its run summary says "queued a pipeline fix for the implementer". `backlog-dispatcher-agent` walks this instance's `runs/*/recommendations.json` (`specpicks-category-integrity-agent` is in its `PRODUCER_AGENT_IDS`) and queues unflagged recs, subject to its per-agent title dedup. This rec type is not in `framework/core/work_types.py` `DEFAULT_REC_ROUTING` (grep 2026-09-23), so `handler_for` defaults it to `code_edit` (the implementer). The first rec (`cat-20260830T033942Z-001`) was implemented; of the 121 rec files from 2026-08-30 to 2026-09-23 it is the only one flagged implemented, and none is flagged deferred or skipped. Whether the later re-emitted recs are dispatched at all was not verified.
- **RunResult:** `success`, or `failure` on empty vocabulary or an uncaught DB error. No `next_state` (state stays `{}`). No email, no handoffs.

## Goals & metrics

No goals are declared: there is no goals file, and there is no `agents/<id>/goals/active.json` for the specpicks instance (checked 2026-09-23). CLAUDE.md requires 3–7 goals per agent, so this is an open gap.

`RunResult.metrics`: `decategorised`, `unscored_pct` (0–1, 4 dp), `recs` (0 or 1).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `CATEGORY_INTEGRITY_CONFIG` | — (required) | path to the instance `site.yaml` |
| `AGENT_ID` | `category-integrity-agent` | the instance id; also the storage prefix |
| `CATEGORY_MAX_FIX_PER_RUN` | `500` | de-categorisations per tick |
| `CATEGORY_UNSCORED_ALERT` | `0.25` | unscored share that triggers the code-fix rec |
| `REUSABLE_AGENTS_REPO` | `/home/voidsstr/development/reusable-agents` | framework path |
| `DATABASE_URL` | — | fallback DSN if the `db_env` variable is unset |

Instance `site.yaml` keys: `site` (string), `db_env` (name of the DSN env var),
`domain_vocabulary`, `foreign_vocabulary`. Extend FOREIGN when a new class of
junk appears. Extend DOMAIN when a legitimate product is wrongly flagged.

## Short-circuit & idempotency

- `signals()` returns `None`, so the agent never short-circuits. The catalogue churns constantly and the check is cheap.
- Idempotent: a de-categorised row no longer matches `category_id IS NOT NULL`, and validated or scored rows are never considered.

## Running & inspecting

```bash
systemctl --user start agent-specpicks-category-integrity-agent.service
tail -f /tmp/reusable-agents-logs/agent-specpicks-category-integrity-agent.log
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/specpicks-category-integrity-agent/runs?limit=10
# unit tests for the off-domain test (reads the specpicks site.yaml vocabulary)
python3 -m pytest /home/voidsstr/development/reusable-agents/agents/category-integrity-agent/test_domain.py
```

There is no dry-run flag. To preview, run the SELECT from step 3 read-only and
apply `domain.is_off_domain` in a Python shell.

## Failure modes & troubleshooting

| Symptom | Cause / action |
|---|---|
| `failure` "site.yaml has no domain/foreign vocabulary" | One of the two lists is empty or misspelled in the instance `site.yaml`. |
| `SystemExit` "<db_env> not set" | The DSN env var named by `db_env` is missing from `~/.reusable-agents/secrets.env`, and `DATABASE_URL` is unset. |
| A legitimate product was de-categorised | Add the missing domain word to `domain_vocabulary`, then re-run the categoriser for that product. |
| Same "pipeline fix" rec every 4h | Expected while `unscored_pct` stays above the threshold (every run on 2026-09-23 wrote one, with 0 de-categorised). The first rec (`cat-20260830T033942Z-001`) shipped as specpicks commit `2d8345a` on 2026-08-29: the scraper and BD import stopped writing `category_id` without a confidence. The share measures **existing** rows, so it stays ~53% (53.3% on 2026-09-23) until those rows are re-scored. The rec has no memory of that fix. |

## Related agents

- **Instance:** `specpicks: agents/category-integrity-agent/README.md`.
- **Downstream:** `backlog-dispatcher-agent` → auto-queue → `implementer` (code-fix rec).
- **Complementary (specpicks repo):** `scripts/verify-categorizations.ts`, an "is this the *right* category" check that is report-only unless run with both `--apply` and `--i-measured-precision` (its own comment puts precision at ~21%), and `scripts/assign-categories.ts`, the scoring categoriser.
