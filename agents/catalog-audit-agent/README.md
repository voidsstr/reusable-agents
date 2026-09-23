# Catalog Audit Agent (`catalog-audit-agent`) — shared engine

> Runs a site's own catalog-quality audit script, turns its findings into
> standard `rec-NNN` recommendations and dispatches them to the implementer,
> so broken or misleading catalog rows (no image, wrong category, wrong
> product image, implausible nutrition) get fixed or deactivated. It serves
> **indexed pages** and **conversions**. Both per-site manifests declare
> `target_metric: goal-indexed-pages-pct`.

This directory is the **engine**. It is never scheduled itself. The
scheduled agents are the per-site instances listed below. Each one is a
`manifest.json` plus a `site.yaml` that runs this `agent.py`.

## At a glance

| | |
|---|---|
| Agent id | `catalog-audit-agent` (engine). Instances: `aisleprompt-catalog-audit-agent`, `specpicks-catalog-audit-agent` |
| Home | `reusable-agents/agents/catalog-audit-agent/` (`agent.py`, `goals.json`, `manifest.json`) |
| Kind | AgentBase python (`CatalogAuditAgent`). Shared engine for the per-site instances |
| Schedule | None. The manifest sets `enabled: false` and has no `cron_expr`. There is no systemd unit. It **is** in the framework registry (API returns it with `enabled=False`, checked 2026-09-23) |
| Entry command | Per instance: `CATALOG_AUDIT_CONFIG=<site.yaml> python3 /home/voidsstr/development/reusable-agents/agents/catalog-audit-agent/agent.py` |
| Category | `research` |
| Status | Engine only. Not scheduled. Both instances are live (see below) |
| Instance runbooks | aisleprompt: `aisleprompt/agents/catalog-audit-agent/README.md`. specpicks: `specpicks/agents/specpicks-catalog-audit-agent/README.md` |
| Downstream runbook | `reusable-agents/agents/implementer/CATALOG_AUDIT.md` (how the implementer applies these recs) |

### Instances (checked 2026-09-23)

| Instance | Config | Cron (manifest, America/Detroit) | systemd `OnCalendar` | `findings_format` |
|---|---|---|---|---|
| `aisleprompt-catalog-audit-agent` | `aisleprompt/agents/catalog-audit-agent/site.yaml` | `2 */5 * * *` | `*-*-* 0/5:2:00` (enabled) | `aisleprompt-catalog-audit` |
| `specpicks-catalog-audit-agent` | `specpicks/agents/specpicks-catalog-audit-agent/site.yaml` | `30 8 * * *` | `*-*-* 8:30:00` (enabled) | `specpicks-image-csv` |

> The engine `manifest.json` description still says the instances live in
> `nsc-assistant/agents/…`. That is stale. The aisleprompt instance moved to
> the aisleprompt repo on 2026-05-12 (aisleprompt commit `e1b5a2c6`), and
> no catalog-audit dir remains in nsc-assistant.

## What it does

`run()` runs these phases in order. Each instance runs them with its own
`agent_id`. The unit sets `AGENT_ID=<instance id>` in its environment, and
`__init__` prefers it over the class id.

0. **setup**: reads `CATALOG_AUDIT_CONFIG`. If the variable is unset or the
   file is missing, the agent exits (`SystemExit`). Next it runs
   `shared.site_quality.load_quality_config()`, which validates against
   `shared/schemas/site-quality-config.schema.json` (`site:` is required).
   Last it creates the local run dir
   `<runs_root or ~/.reusable-agents/<agent_id>/runs>/<site_id>/<UTC-ts>/`.
1. **Audit**: runs `audit.command` (an argv list) through `subprocess.run`
   with timeout `audit.timeout_s` (default 1800). A non-zero rc or an
   exception is logged as an `error` decision, and the run **continues**
   with whatever findings file already exists. If there is no command, the
   agent uses the existing findings file.
2. **Load findings**. With `aisleprompt-catalog-audit` it reads the newest
   `*.json` in `audit.findings_path`. With `specpicks-image-csv` it reads the
   newest file that matches `audit.findings_glob`, parsed with
   `csv.DictReader`. If nothing is found, it returns `RunResult(failure,
   "no findings file found")`.
3. **Convert** with `CONVERTERS[findings_format]`, capped at `audit.max_recs`
   (default 30). Zero recs means a **success** with the summary "audit found
   0 actionable issues — clean run" and `metrics={"recs_total": 0}`. Nothing
   is emailed or dispatched.
   - `aisleprompt-catalog-audit`: this converter handles one rec per
     criterion that has `count > 0`.
     - **Evidence.** Up to 5 sample row ids from the audit become
       `ref_ids`/`evidence`. A criterion whose samples carry no `id` is
       **skipped**, and stderr prints `[catalog-audit-recs] skipped N
       criteria with empty sample`.
     - **Scoring.** Confidence is 0.95, or 0.85 for `*valid-url*`. The tier
       comes from `score_tier(threshold=0.92)`. `goal_ids` tags are
       inferred from the criterion name.
     - **Migration templates.** Six criteria carry a `migration_template`
       with ready SQL (`sql_with_ids`): `recipe-image-present`,
       `recipe-image-valid-url`, `recipe-nutrition-sanity`,
       `recipe-duplicate-source-url`, `product-image-present` and
       `product-schema-rich-results`. The last one is a manual-review
       comment only, with no UPDATE.
     - **Affected URLs.** For `recipe_catalog` / `kitchen_products`
       templates, `affected_urls` gets
       `https://aisleprompt.com/recipes/<id>` or `/products/<id>`. These
       site URL patterns are hardcoded in the engine.
   - `specpicks-image-csv`: this converter handles one rec per CSV row where
     `verdict == "mismatch"`. Each rec gets category `image-name-mismatch`,
     check `product-image-matches-name`, severity `high`, confidence 0.9 and
     tier `review`. Rows with `unsure` or `match` are ignored.
4. **Rank and persist**. Recs are sorted by severity, then by confidence,
   and `assign_rec_ids()` numbers them.
   - **User replies.** Replies are applied to the previous run's local
     `recommendations.json` via `apply_user_responses`, from
     `self.responses`. AgentBase loads those from
     `agents/<agent_id>/responses-queue/`.
   - **Dedup, tagging, save.** Producer-history dedup runs through
     `filter_proposals_against_history`, which reads
     `agents/<agent_id>/state/emitted-titles.json`. Recs are then stamped
     with `stamp_implementer_safety(dispatch_kind="catalog-audit")`.
     Finally the doc is validated, and `recommendations.json` is written
     locally and to storage.
5. **Email and dispatch**. `render_recs_email(auto_queued=True)` produces
   `email-rendered.html`, and `send_via_msmtp()` tries Graph first, then
   msmtp, behind the digest gate.
   - **If the send returns ok**:
     - The outbound record is written to
       `agents/<agent_id>/outbound-emails/<request_id>.json`.
     - `framework.core.dispatch.gated_dispatch_now(subject_tag="catalog-audit")`
       dispatches the rec ids to the implementer.
   - **Dispatch sits inside the send-ok branch.** If `reporter.email.to`
     or `from` is missing, or the send fails, `gated_dispatch_now` is not
     called (step 6 still runs).
6. **Legacy auto-tier message**. If `auto_implement` is truthy,
   `dispatch_auto_recs()` also writes the `tier=auto` rec ids to
   `agents/<implementer.agent_id or "implementer">/responses-queue/<ts>-auto-<agent_id>.json`.
   Its count becomes the `auto_dispatched` metric. On 2026-09-23 these
   messages ended up in `agents/implementer/responses-archive/` (moved
   there by AgentBase `pre_run` when the implementer runs). Neither
   `agents/implementer/agent.py` nor `run.sh` reads the drained
   responses, so the message is archived without being acted on (code
   reading).
7. It returns `RunResult(success)` with the metrics listed below and
   `next_state={last_run_ts, last_request_id, site_id}`.

## Inputs

| Input | Where |
|---|---|
| Site config | `CATALOG_AUDIT_CONFIG` → the instance's `site.yaml` |
| Findings file | Written by the site's audit script: aisleprompt `audit-history/<YYYY-MM-DD>.json` (UTC date), specpicks `audit-product-images-<ISO-ts>.csv` |
| Site DB | Only via the audit script. `audit.command` sets `DATABASE_URL` for the script. The engine itself opens no DB connection |
| Prior replies | `agents/<agent_id>/responses-queue/` (AgentBase) + previous local `recommendations.json` |
| Dedup history | `agents/<agent_id>/state/emitted-titles.json` |

## Outputs

| Output | Where |
|---|---|
| Run artifacts | Local `<run_dir>/{recommendations.json,email-rendered.html}`. Storage `agents/<agent_id>/runs/<run_ts>/…` (plus AgentBase `progress.json`, decisions, etc.) |
| Email | `send_via_msmtp`. The engine does **not** pass `bypass_digest`. The wrapper sets `DIGEST_ONLY=1` by default, so the mail is queued to `digest-queue/` ("suppressed: digest-mode"). If `DIGEST_ONLY=1` and `DIGEST_DISABLED=1`, it is dropped instead. Both paths return ok, so dispatch still happens |
| Outbound record | `agents/<agent_id>/outbound-emails/<request_id>.json` (`kind: email-recommendations`) |
| Implementer dispatch | `dispatch.gated_dispatch_now` → `dispatch_now`. Observed 2026-09-23: kind `catalog-audit` is treated as data-only ("bypassing site lock"). The run dir is copied to `/tmp/reusable-agents-logs/dispatch-rundirs/`, and a transient `agent-dispatch-implementer-<site>-<ts>` unit is spawned |
| What the implementer does | Uses `agents/implementer/CATALOG_AUDIT.md`, which asks for `db/migrations/<UTC-ts>_catalog-audit-<criterion>.sql` + `changes/<rec-id>.summary.md` in the site repo. Observed names on 2026-09-23 also carry the rec number, e.g. `db/migrations/20260923T140300Z_catalog-audit-rec001-recipe-category-assigned.sql`. It **skips the deployer chain** for `catalog-audit` dispatches. `catalog-audit-shipped-backfill` (every 30 min) later checks the prod DB and flips `shipped` |
| Implementer config | `dispatch_now` sets `RESPONDER_SITE` but not `SEO_AGENT_CONFIG`, so `implementer/run.sh` derives its config from `reusable-agents/examples/sites/<site>.yaml` (the 2026-09-23 aisleprompt dispatch logged `config=…/examples/sites/aisleprompt.yaml`). The instance `site.yaml` `implementer.allowed_paths` / `excluded_paths` are therefore **not** the scope applied to these dispatches, and the `examples/sites/*.yaml` files declare no `allowed_paths` |
| RunResult | `success` for clean runs and normal runs. `failure` when there is no findings file, or on an exception, which triggers AgentBase's automatic `agent-doctor` invocation |

**`RunResult.metrics` keys:** `recs_total`, `recs_critical`, `recs_high`,
`applied_responses`, `auto_dispatched`. A clean run emits only
`recs_total`.

## Goals & metrics

Engine `goals.json` (seeded 2026-08-18, commit `84f6b7a`):

| Goal id | `target_metric` | Target |
|---|---|---|
| `goal-critical-findings-zero` | `recs_critical` | 0 (decrease) |
| `goal-findings-auto-dispatched` | `auto_dispatched` | 5 recs/run (increase) |
| `goal-catalog-issue-count-shrink` | `recs_total` | 0 (decrease) |

The per-site instances carry their own goals in storage. These are not
`goals.json` files. See each instance runbook. `agent-metrics-collector`
also computes `goal-broken-records-fixed-30d` and `goal-catalog-health-pct`
for `<site>-catalog-audit-agent` (`m_catalog_audit()`).

## Configuration

**Environment**

| Name | Default | Meaning |
|---|---|---|
| `CATALOG_AUDIT_CONFIG` | none (required) | Path to the instance `site.yaml` |
| `AGENT_ID` | class id `catalog-audit-agent` | Set by the systemd unit to the instance id |
| `DIGEST_ONLY` | `1` (set by `framework/agent_run_wrapper.sh`) | `1` queues the email to the digest instead of sending it |
| `DIGEST_DISABLED` | unset | `1` drops digest-queued mail (see `shared/site_quality.py:maybe_queue_to_digest`) |
| `AGENT_FORCE_RUN` | unset | `1` bypasses the auto short-circuit for one run |

**`site.yaml` keys the engine reads**

| Key | Default | Notes |
|---|---|---|
| `site.id` / `site.domain` / `site.label` / `site.what_we_do` | none | `site` is required by the schema, and `site` has `additionalProperties: false` |
| `audit.command` | none | argv list, e.g. `["bash","-c","cd <repo> && … npx tsx scripts/<audit>.ts"]` |
| `audit.findings_format` | `aisleprompt-catalog-audit` | Must be a key of `CONVERTERS`. Anything else exits (`SystemExit`) |
| `audit.findings_path` | none | Directory holding `*.json` (aisleprompt format) |
| `audit.findings_glob` | none | Glob for CSVs (specpicks format) |
| `audit.max_recs` | 30 | Cap per run |
| `audit.timeout_s` | 1800 | Audit subprocess timeout |
| `reporter.email.{to,from,subject_template,msmtp_account}` | `msmtp_account`: `automation` | `reporter` has `additionalProperties: false` (only `email` is allowed). `subject_template` is formatted with `{site}`, `{label}`, `{tag}`, `{recs_count}` only, and the `[<agent_id>:<request_id>]` prefix is added automatically. Both instances' templates contain `{agent_id}`/`{request_id}`, which raises `KeyError`, so the default `<agent_id> — <site> — <tag>` is used (seen in the 2026-09-23 outbound records) |
| `auto_implement` | `gated_dispatch_now` treats missing as **true**. `dispatch_auto_recs` treats missing as false | Both instances set `true` |
| `implementer.agent_id` | `implementer` | The only `implementer` key the engine reads (target of the legacy step-6 message). The other `implementer.*` keys are not what the dispatched implementer applies; see "Implementer config" above |
| `runs_root` | `~/.reusable-agents/<agent_id>/runs` | Local run-dir root |

`audit:` is not in the schema, but the schema's top level allows unknown
keys, so it validates.

A minimal `site.yaml` shape (current schema, not the pre-2026-05 flat
`site_id:` form):

```yaml
site:
  id: example
  domain: example.com
  label: Example
  what_we_do: "…"
audit:
  command: ["bash", "-c", "cd /path/to/repo && npx tsx scripts/<audit>.ts"]
  findings_format: aisleprompt-catalog-audit   # or specpicks-image-csv
  findings_path: /path/to/repo/audit-history   # (or findings_glob for CSV)
  max_recs: 30
  timeout_s: 1800
reporter:
  email:
    to: [mperry@northernsoftwareconsulting.com]
    from: "<Label> Catalog Audit <automation@northernsoftwareconsulting.com>"
    subject_template: "Catalog Audit — {label} — {tag}"   # [agent_id:request_id] is prefixed automatically
    msmtp_account: automation
implementer:
  repo_path: /path/to/repo
  branch: master
  allowed_paths: ["db/migrations/**", "changes/**"]   # CLAUDE.md requires a scope (see "Implementer config")
auto_implement: true
```

### Adding a new audit format

To add a format, write a `_findings_to_recs_<fmt>()` converter and register
it in `CONVERTERS`. Then add a loader branch in **both** `run()` step 2 and
`signals()`. Both switch on `findings_format` explicitly.

## Short-circuit & idempotency

- `signals()` hashes `{site, fmt, latest findings file name, size, mtime}`.
  **In practice it never skips a run.** Two things cause this:
  - It is evaluated *before* `run()` re-executes `audit.command`, which
    rewrites the findings file each tick.
  - `run()` returns a `next_state` without the `_auto_signals_hash` key.
    `post_run` persists `next_state` wholesale, so the hash is not carried
    to the next run. On 2026-09-23 the aisleprompt instance's
    `state/latest.json` held only `last_request_id`, `last_run_ts` and
    `site_id`.
- Producer-history dedup (`emitted-titles.json`) filters only the
  `recommendations.json` document. The email, the dispatched rec ids and
  the metrics all use the **pre-dedup** list. See Failure modes.

## Running & inspecting

```bash
# run an instance now (real audit + real dispatch — there is no dry-run flag)
systemctl --user start agent-aisleprompt-catalog-audit-agent.service
systemctl --user start agent-specpicks-catalog-audit-agent.service

# logs
tail -f /tmp/reusable-agents-logs/agent-<instance-id>.log

# framework API (8090 on this host; requires the bearer token)
curl -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://localhost:8090/api/agents/<instance-id>
curl -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://localhost:8090/api/agents/<instance-id>/goals

# local run artifacts
ls ~/.reusable-agents/<instance-id>/runs/<site>/
```

The systemd unit's exit status is `success` whenever the Python process
exits 0. The run's own status line in the log (`success` / `failure`) is
the real outcome.

## Failure modes & troubleshooting

- **The same batch is re-dispatched every tick (verified 2026-09-23,
  aisleprompt).**
  - **What happened.** Every run that day (03:11Z off-schedule, then
    04:02Z, 09:02Z, 14:02Z) produced the same 7 recs. Each dispatch committed another copy of the
    same 7 migration files to aisleprompt: `d6cabc0e`, `ec6c0a8a`,
    `5410e2b3`, `bab7c5e4`.
  - **What the implementer reported.** It logged `implemented 0/7` and
    `shipped=0 unverified=7`. Its own summary said "The recs keep firing
    because the earlier migrations are still pending deployment".
  - **Why.** The flagged rows are unchanged until the migrations reach the
    DB, and `catalog-audit` dispatches skip the deployer
    (`agents/implementer/run.sh`: "catalog-audit dispatch — skipping deployer
    chain").
  - **How aisleprompt applies migrations.** It applies them at server boot.
    `initDB()` in `src/simple-server.ts` runs every top-level
    `db/migrations/*.sql` file that is not yet in `_migrations`, in sorted
    order. It records each failure in `_migration_failures`. `startWithRetry()`
    retries `initDB()` up to 30 times, 5 s apart. `Dockerfile.azure` does
    `COPY db ./db`, so the files ship inside the image. A committed
    catalog-audit migration therefore reaches prod only when the next image
    deploy boots. That deploy has to come from some other, code-changing
    dispatch.
  - **Observed state (read-only query, 2026-09-23 about 19:09Z).**
    - No catalog-audit file dated 2026-09-21..23 was in `_migrations`. That
      covers the 37 such files in `release/aisleprompt/0226` and the 44 in
      the working tree. The release is commit `7edf354b` (10:44 EDT), tagged
      10:50 EDT, image `20260923-1445`, and it already holds the 09:02Z and
      14:03Z files.
    - The last catalog-audit apply was 2026-09-03 15:43Z, and
      `_migration_failures` was empty.
    - The stall is not specific to catalog-audit. 764 of the 3,429 `.sql`
      files in the working tree were not in `_migrations`. Only two rows were
      added after 2026-09-03: one on 2026-09-22 14:32Z and one on 2026-09-23
      13:27Z. Each was a single file, while hundreds of files that sort
      earlier stayed pending, so neither looks like a boot pass.
  - **Why the boots did not apply them is unverified.** Boot logs were not
    checked. One code path fits the evidence. Suppose a migration file opens
    its own `BEGIN` and then fails. The connection is left in an aborted
    transaction, so the catch block's `_migration_failures` insert also fails
    (it has no `ROLLBACK`). Then the next file's `SELECT 1 FROM _migrations`,
    which sits outside that `try`, throws and aborts `initDB()`. Each retry
    stops at the same file, and nothing is recorded. An empty
    `_migration_failures` therefore does not prove that no migration failed.
  - **Check it:**
    `SELECT name, applied_at FROM _migrations WHERE name LIKE '%catalog-audit%' ORDER BY applied_at DESC LIMIT 10`
  - **rec007 (recipe-video-present).** aisleprompt commit `dac677db`
    (2026-09-23 09:46 EDT) replaced the rec007 backlog with one set-based
    migration, `20260923T140000Z_rec007-video-source-setbased-backlog.sql`.
    That file is in `_migrations`, applied 2026-09-23 13:27Z. The commit also
    moved the 18 superseded rec007 batch files to `db/migrations/_quarantine/`.
    The runner does not recurse into that folder; see its `README.md`. The
    agent kept emitting rec007 id-list files after the commit
    (`20260923T140306Z_…` and `20260923T190207Z_…`).
- **Dedup and dispatch disagree (verified 2026-09-23).**
  - **What happened.** The `recommendations.json` of all four 2026-09-23
    runs (local, and the dispatched run-dir copy for 14:02Z) held **0**
    recs, because dedup dropped all 7 titles. Yet `rec-001..rec-007` were
    still dispatched each time.
  - **Why.** Steps 5–6 and the metrics use the unfiltered `recs` list.
- **Clean runs that aren't clean (specpicks).** The converter counts only
  `mismatch` rows. When the audit's LLM calls fail, every row comes back
  `unsure` and the run reports "0 actionable issues". See the specpicks
  instance runbook: nearly every CSV from 2026-09-02 to 2026-09-23 is mostly `unsure`.
- **`no findings file found` → failure.** Either the audit script never
  wrote a file, or `findings_path` / `findings_glob` is wrong. The failure
  auto-invokes `agent-doctor`.
- **Config rejected at setup.** A `ValueError: config invalid at …` comes
  from schema validation. Unknown keys under `site` or `reporter` fail.
- **Criteria silently skipped.** A criterion whose audit samples carry no
  row `id` produces no rec. Fix the audit script so it emits ids for that
  check.

## Related agents

- **Upstream**: the site audit scripts. aisleprompt uses
  `scripts/catalog-quality-audit.ts`. specpicks uses
  `scripts/audit-product-images.ts`.
- **Downstream**:
  - `implementer`, via `framework.core.dispatch` with runbook
    `CATALOG_AUDIT.md`.
  - `backlog-dispatcher-agent`, which lists both instances in
    `PRODUCER_AGENT_IDS` and dispatches their still-pending recs. It skips
    recs marked shipped, implemented, deferred, duplicate or skipped.
  - `catalog-audit-shipped-backfill` (`*/30`), which verifies migrations
    landed.
  - `responder-agent`, which handles email replies.
- **Sibling engines on the same `shared/site_quality.py` helpers**:
  `progressive-improvement-agent`, `competitor-research-agent`.
