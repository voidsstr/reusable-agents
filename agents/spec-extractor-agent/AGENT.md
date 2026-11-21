# Spec Extractor — runbook

**Engine:** `agents/spec-extractor-agent` (AgentBase, Python). **Per-site
instances** are config-only directories in the site repos
(`<site repo>/agents/spec-extractor/`: `manifest.json` + `site.yaml` +
goals). This engine's own manifest is `enabled: false` with no cron: it is
a template and is never scheduled itself.

## What it does

A catalog page is only as good as its numbers, and they must be our own:
the Amazon IP License forbids storing Amazon text, so specs come from
manufacturers. This engine fetches manufacturer pages, FAQ pages and manual
PDFs politely, extracts specs deterministically, keeps one provenance row
per value, projects accepted values into typed spec tables, and then
derives compatibility edges and runtime estimates from them. It is
niche-agnostic: the fields, extractors, brands, rules, connectors and
rulesets are profiles in the two primitives' storage configs; every table
and column is in the site's `site.yaml`.

Built on:

| Primitive | Used for |
|---|---|
| `framework.core.spec_sources` | robots.txt, per-host rate limit, conditional GETs, content hashes, documents (HTML / PDF / text), per-brand deterministic extractors, the batched cheap-model fallback, consolidation + provenance, the adjudication queue (Amazon always refused) |
| `framework.core.compat_graph` | versioned compatibility rules (cold Voc at -40 °C, current, connectors, manufacturer lists) → `compat_edges` |
| `framework.core.short_circuit` | `partition_by_hash` (per model) and `stable_hash` signatures |

## One run: sources → extract → validate → compat → runtime

1. **sources.** A source row is due when it was never fetched, its last
   check is older than `sources.refetch_hours`, it was extracted with an
   older config fingerprint, or fetched content was never extracted.
   Failed sources back off exponentially (`retry_base_hours` doubling to
   `retry_max_hours`); a gone page (404/410) is retried after
   `gone_retry_hours`; a refused URL (Amazon, or a redirect to it) is never
   fetched again while its URL is unchanged; a deferred host (busy, or a
   Crawl-delay over the cap) is simply tried next run. Order: urgent
   (never fetched / config changed / not extracted), then the models
   `sources.priority_sql` returns, then the oldest check; hosts are
   interleaved. Caps: `max_fetches_per_run`, `max_run_minutes`.
2. **extract.** Only sources whose content hash or extraction fingerprint
   changed are parsed (`spec_sources.needs_extraction`): the candidate
   values are stored on the source row, so unchanged content is never
   parsed again. With `llm.enabled` (off by default), one batched
   cheap-model pass (10 items per call, `CLAUDE_POOL_RESERVE=2`) asks only
   for fields the deterministic pass missed; every answer needs a verbatim
   quote that states the value. Answers are kept with the source: a
   re-extraction of UNCHANGED content (a new extractor config) keeps them,
   and when the LLM config changes every field the source's answers covered
   is asked again; an answer is replaced only when a new pass on that
   source completes, field by field. A pool-reserve or provider deferral
   keeps the earlier answers (nothing blanks) and is retried next run.
   Changed content drops them (they quoted the old text) and asks again.
3. **validate.** First the adjudicator spends today's budget on queued
   conflicts (off by default; at most 0.1 h/day, a strong model may only
   choose among sourced candidates, fields marked `adjudicate: human` wait
   for an operator). Then every model whose INPUTS changed is consolidated
   (`partition_by_hash` over its sources' candidates, operator values,
   adjudication decisions and config; unchanged models are revisited every
   `consolidate.revisit_after_runs` runs). A model's `spec_values`, typed
   row and status are written in one transaction and only when they differ.
   New conflicts are enqueued; conflicts that vanished are retired. A model
   whose write fails is rolled back, reported, and retried next run; the
   others proceed.
4. **compat** (`compat.enabled`). Nodes are the accepted values of every
   model of the rulesets' kinds; fit records come from `compat.fit_lists`
   (a published "compatible with ..." text field resolved through
   `compat_graph.ModelIndex`, never guessed) and the optional
   `compat.fit_records` table. Full rebuild on the first run, a config or
   rule-version change, or every `full_rebuild_every_runs` runs; otherwise
   only pairs touching changed models: a model whose values changed, and
   both ends of every fit record that appeared, changed or was WITHDRAWN (a
   fit-records row deleted, a model dropped from a published list). Inside
   that part every row that no longer yields a stored edge is deleted on
   the same run, evaluated or not (a `manufacturer_list`-only ruleset no
   longer evaluates a pair whose record is gone); rows of models that left
   the catalog are deleted. Skipped when nothing changed. Batched by A-side
   models (`batch_size`) so memory stays bounded. A full rebuild also
   deletes the engine's rows of rulesets the profile has DISABLED
   (`compat.purge_orphaned: disabled`, the default: disabling a ruleset
   withdraws its verdicts on the next run); rows of rulesets the profile
   does not know are only counted (`compat_orphaned_rows`) unless
   `purge_orphaned: all`.

   **Manual rows.** `compat_graph` protects every `confidence = 'manual'`
   row. The engine writes such rows itself from fit records of origin
   `manual` (an operator's statement, still held to the safety checks), so
   it records each one it wrote in its state (`compat.owned_manual`: key →
   hash of verdict, via, rule version and inputs hash) and releases it once
   it no longer derives that row: the record was withdrawn, a model left
   the catalog, or the ruleset was disabled. Releasing is a guarded DELETE
   in the same transaction as the write plan, so the edge that now decides
   the pair (the manufacturer's record, the computed rules) replaces it on
   the same run (`compat_manual_released`). A manual row the engine did not
   write (an operator's own `compat_edges` row) or one an operator edited
   after the engine wrote it is never touched. If the state blob is lost,
   the next (full) run adopts every manual row it derives identically
   again; a record withdrawn while the state was lost leaves its row to the
   operator.
5. **runtime** (`runtime.enabled`). For every spec-complete model of
   `runtime.kinds` and every load preset: usable energy = the measured
   field, else rated × `rated_factor` with the interval rated ×
   `rated_interval` (niche values, no engine default: e.g. 0.842 and
   [0.75, 0.925] for portable power stations); hours = usable / draw, where
   draw is energy_per_day / 24, else power × duty cycle, else power. A
   preset is estimated only when the model's continuous output covers its
   running power (no claim otherwise); `peak_ok` is NULL when the preset has
   no surge or the model's peak is unknown. Rows are replaced per model only
   when they differ; rows of models that left the scope (retired, another
   kind, a kind no longer estimated) are deleted, so `runtime_models_live`
   counts the current catalog only.

One run per instance at a time: a session advisory lock in the site DB
(`database.lock_key`, default `spec-extractor:<agent id>`); a second run
exits immediately with `skipped_locked`. `signals()` returns None on
purpose (elapsed time is an input: sources fall due); a run with nothing to
do reports `short_circuited`.

The lock is also what keeps one fetching process per instance: the
per-host interval, robots Crawl-delay and 429/503 backoff live in one
process's `PoliteFetcher`, so two processes would each fetch a host at the
full rate. `--dry-run` fetches live, so it takes the same lock. It needs
the instance id (`--agent-id <site>-spec-extractor`, the cron unit's
`AGENT_ID`) or `database.lock_key` (exit 2 otherwise: the engine id would
name a different lock), and exits 3 without fetching while a run holds it.
A cron tick that starts while a dry run holds the lock is skipped
(`skipped_locked`), so its due sources wait for the next tick (about 12 h).
Another site's instance locks in its own site DB and does not exclude this
one: before two instances share hosts or a fetch cache dir, host slots must
be shared across processes.

* **Forcing a run.** `AGENT_FORCE_RUN=1` (`short_circuit.force_run_requested`)
  bypasses the in-run short-circuits: every source fetched this run is
  re-extracted, every model re-consolidated (writes stay diff-based) and
  compat fully rebuilt. Sources still fall due by age (politeness is not a
  short-circuit). `ENGINE_VERSION` (spec_extractor_pipeline.py) is part of
  every model's input hash: bump it with a change to how this engine
  consolidates, and every model is consolidated again on the next run.
* **A dropped connection.** Between phases (after the up-to-40-minute fetch
  phase, after the LLM pass, before compat and runtime) the connection is
  probed (`framework.core.pg_conn.is_open`); a dropped one is replaced and
  the run lock taken again on the new session (`db_reconnects`). If another
  run took the lock in between, this run stops.
* **A run that raises** returns `failure` with its previous state merged
  with the state of the phases that completed (model hashes, compat
  signatures), never an empty one: the next run does not re-consolidate
  every model or rebuild compat from scratch.

## TABLE CONTRACT

The site's schema does not exist when the engine is written, so the engine
assumes NO names: every table and column below is named in `site.yaml`
(logical name → column; `null` = the table has no such column). The
reference implementation is [`fixtures/schema.sql`](fixtures/schema.sql);
the tests run against it. WHERE fragments are trusted config (no `;`,
literal `%` doubled) and see these aliases: models `m`, products `p`,
presets `p`, fit records `f`.

### `tables.models` — read (relation), status written (write_table)

| logical | required | meaning |
|---|---|---|
| `id` | yes | model id (any type; `spec_values.model_id` etc. reference it) |
| `kind` | yes | product kind; only kinds under `kinds:` are extracted |
| `brand` | no | brand value, mapped through `brands.map` to a spec_sources brand slug (falls back to the source URL's host) |
| `name` | no | display name (logs, LLM prompts, fit-list resolution) |
| `model_number` | no | fit-list resolution |
| `aliases` | no | text[] or comma-separated text (fit-list resolution) |

`relation` may be a view (e.g. joining the brand slug). `where` scopes the
catalog (e.g. `m.lifecycle <> 'retired'`). `status_columns` (all optional)
are written on `write_table` (default `relation`): `status` text
(`complete` | `partial` | `conflict` | `missing` | `unsourced`),
`completeness` numeric 0-1 (accepted required fields / required fields),
`checked_at` timestamptz (set when the model is consolidated).

### `tables.sources` — rows seeded by the site, bookkeeping by the engine

| logical | required | meaning |
|---|---|---|
| `id` | yes | source row id |
| `model_id` | yes | the model this document describes |
| `kind` | yes | `manufacturer-page` \| `manual-pdf` \| `manufacturer-faq` \| `measured-test` \| `cert-listing` \| `manual-entry` |
| `url` | yes | the document; also the provenance identity of its values (redirects are recorded in the state, not in provenance) |
| `publisher` | no | shown with the citation (default: the URL host) |
| `fetched_at` | yes | timestamptz: when the content the stored values came from was fetched (written) |
| `content_sha256` | yes | text: that content's hash (written) |
| `state` | yes | **jsonb, engine-owned** (default column name `fetch_state`) |

Required shape: `UNIQUE (model_id, url)`. Only kinds in
`sources.fetch_kinds` are fetched (default: manufacturer-page, manual-pdf,
manufacturer-faq, cert-listing); `manual-entry` and `measured-test` rows
exist to be cited by operator values. Deleting a row removes its values
(`ON DELETE CASCADE` on `spec_values.source_id` is recommended).

The `state` jsonb (do not edit by hand; clearing it to `{}` forces a fresh
fetch and extraction of that row):

```json
{"v": 1, "status": "ok|failed|gone|blocked|refused", "last_result": "<FetchResult.status>",
 "reason": "...", "attempted_at": "...", "failures": 0, "next_attempt_at": null,
 "fetch": {<FetchResult.to_record(): url, final_url, http_status, etag, last_modified, content_sha256, ...>},
 "final_url": "...", "brand": "<resolved slug>", "fingerprint": "<extraction config fingerprint>",
 "extracted_sha": "<content the candidates came from>", "extracted_at": "...",
 "candidates": [<spec_sources Candidate dicts + rank, deterministic and LLM>],
 "candidates_sha": "...", "errors": [...], "flags": [...],
 "llm": {"sig": "...", "at": "...", "items": 0|1, "candidates": n},
 "refused_url": "<only when refused>"}
```

### `tables.values` — engine output, plus operator rows

| logical | required | meaning |
|---|---|---|
| `id` | yes (for operator status updates) | row id |
| `model_id`, `field` | yes | |
| `value_num`, `value_text`, `unit` | yes | value in the field's canonical unit (dimensions: value_text `a×b×c`) |
| `source_id` | yes | the `spec_sources` row (NULL allowed only on operator rows) |
| `extractor` | yes | `deterministic` \| `haiku` \| `opus-adjudicated` \| `manual` |
| `extractor_id` | yes | engine rows always set it; see below |
| `confidence` | yes | numeric |
| `status` | yes | `accepted` \| `conflict` \| `rejected` \| `superseded` |
| `snippet`, `qualifier`, `conflict_id` | no | provenance detail when mapped |

**Who owns which row.** An *operator row* is `extractor = 'manual'` with
`extractor_id` NULL, empty or starting with `manual-entry`: an operator
enters it (a hand-checked or measured value, citing a `spec_sources` row).
Operator rows are read as manual candidates (they always win
consolidation), are never deleted or changed by the engine except their
`status` (e.g. `rejected` when out of the field's range). An operator row
for a field outside its kind's configured field set is not consolidated
(not range-checked, status left as entered) but, if `accepted`, is still
read by the compat and runtime phases: add the field to the kind's profile
to have it validated. Every other row is
engine output, replaced per model when the consolidated result changes. An
operator's *adjudication decision* is stored as an engine row with
`extractor = 'manual'` and `extractor_id = 'manual:<extractor>'`.

### `tables.products` — read, metrics only (optional)

`id`, `model_id` required; `kind` optional (else the model's kind);
`where` (e.g. `p.is_active`). When set, `primary_spec_complete_pct`
counts products (pages) instead of models.

### `kinds.<kind>.typed` — one typed row per model (optional)

`table`, `key` (default `model_id`; needs a UNIQUE constraint),
`columns: {field: column}` (values in the field's canonical unit; booleans
as boolean, dimensions as numeric[]), optional `completeness` (numeric) and
`updated_at` (timestamptz, set when the row changes). Fields that are not
accepted (missing or in conflict) are written as NULL. When a model's kind
changes, its row in another configured kind's typed table is deleted with
the next write (`typed_deletes`).

### compat (optional)

* edges: `framework.core.compat_graph`'s writer config (`compat.graph.writer`
  or the storage profile; default table `compat_edges`, shape:
  `compat_graph.reference_ddl()`).
* `compat.fit_records`: `a`, `b`, `relation` required; `verdict`, `origin`
  (`manufacturer` | `manual`), `config` (jsonb), `via`, `source_url`,
  `source_kind`, `publisher`, `source_id`, `statement` optional.
  Manufacturer records need a non-Amazon source of kind manufacturer-page,
  manual-pdf, manufacturer-faq or cert-listing. A `manual` record outranks
  manufacturer records and the computed rules (safety checks still apply)
  and yields a `confidence = 'manual'` edge the engine owns: deleting the
  record withdraws the edge on the next run (see compat above). The row
  key is `(a, b, relation)` per ruleset.
* `compat.connectors`: optional connector table (`id` required; `label`,
  `family`, `max_a`, `max_v`) merged into the profile's taxonomy.

### runtime (optional)

* `runtime.presets`: `id`, `power_w` (average running W) required;
  `peak_w` (surge W), `duty_cycle` (0-1), `energy_per_day_wh` optional;
  `where` (e.g. `NOT p.medical`). Column defaults are the logical names:
  map yours (e.g. `power_w: avg_w`).
* `runtime.output`: `model_id`, `preset_id`, `basis`, `usable_energy`,
  `hours_est`, `hours_low`, `hours_high`, `peak_ok` required, `computed_at`
  optional; unique per (model_id, preset_id). Column defaults are the
  logical names (e.g. map `usable_energy: usable_wh`, `peak_ok: surge_ok`).

## Configuration

* **site.yaml block** `spec_extractor:` — template:
  [`site.example.yaml`](site.example.yaml); schema:
  [`spec-extractor-config.schema.json`](spec-extractor-config.schema.json)
  (unknown keys fail the run in its first second).
* **Niche profiles** — `config/spec-sources-config.json`
  (`profiles.<name>`: fields, rules, extractor sets, brands; worked
  examples: `framework/tests/fixtures/spec_sources/profiles/`) and
  `config/compat-graph-config.json` (`profiles.<name>`: connectors, policy,
  rules, rulesets). The block selects them: `spec_sources.profile`,
  `kinds.<kind>.spec_sources.profile`, `compat.graph.profile`.
* **Per kind:** `fields` (subset of the profile's), `required` (what makes
  that kind spec-complete), `typed`. Fetching, Amazon refusal and
  adjudication settings come from the common `spec_sources` layer only (one
  fetcher serves every kind, so the per-host interval holds across kinds).
  A primary kind (`metrics.primary_kinds`, or every kind when that is
  empty) must have required fields (config problem otherwise: its
  completeness would be 1.0 for every model); another kind without them
  only counts as complete once it has a source or an operator value.
* **`database.dsn_env`** is the NAME of the env var holding the DSN
  (`A-Z`, `0-9`, `_`); a pasted connection string is rejected without
  being echoed. The DSN never reaches a run record, framework storage, a
  log or the CLI: a connect failure (also a reconnect between phases) is
  reported as `cannot connect to the site DB ($NAME): <class>: <detail>`,
  where a malformed DSN keeps no driver text at all (libpq echoes the
  connection string, a mis-encoded password included), every other driver
  message loses its quoted segments, and every fragment of the password
  is replaced by `<redacted>`. In a URI DSN, percent-encode special
  characters of the password (`%` → `%25`, `@` → `%40`, `/` → `%2F`).
* **Switches without a commit** (storage config):
  `config/spec-sources-config.json` `by_agent_id.<id>.llm.enabled=false` /
  `.adjudication.enabled=false`; compat ruleset
  `by_site.<site>.rulesets.<id>.enabled=false` (the config change triggers a
  full rebuild on the next run, which deletes that ruleset's edges the
  engine wrote, those from manual fit records included; an operator's own
  `compat_edges` rows stay).
* **Bot identity:** set `spec_sources.fetch.user_agent` to a UA containing
  `SpecSourcesBot` and a contact URL on the site.

## Operator tasks

* **Add sources.** Insert `spec_sources` rows (model, kind, url,
  publisher). The next run fetches them first.
* **Enter a hand-checked or measured value.** Insert a `spec_values` row
  with `extractor = 'manual'`, `extractor_id` NULL, `status = 'accepted'`,
  citing a `spec_sources` row (kind `manual-entry` or `measured-test` with
  the citation URL). It wins over extracted values on the next run.
* **Settle a conflict.** Conflicts wait in the queue at
  `agents/<agent id>/spec-adjudication/queue.json` (framework storage):
  ```python
  from framework.core import spec_sources as ss
  from framework.core.storage import get_storage
  q = ss.AdjudicationQueue(ss.StorageQueueStore(get_storage(),
          "agents/<agent id>/spec-adjudication/queue.json"))
  for c in q.pending(): print(c.conflict_id, c.model_key, c.field,
                              [(i, x.value_repr(), x.source.url) for i, x in enumerate(c.candidates)])
  q.add_operator_decision("<conflict id>", <candidate index or None>, "why")
  ```
  The next run applies the decision (and, for a decided required field,
  completes the model and its compat edges).
* **State or withdraw a fit.** Insert a `compat.fit_records` row with
  `origin = 'manual'` (and a `statement`): the next run writes the edge,
  held to the safety checks. Delete the row to withdraw it: the next run
  deletes the edge, or replaces it with what the manufacturer's records or
  the computed rules now say. A `compat_edges` row written by hand with
  `confidence = 'manual'` is the operator's alone: the engine never edits
  or deletes it (a manual fit record for the same pair still replaces it),
  and editing an engine-written manual row by hand makes it the operator's.
* **Re-extract one source:** set its state column to `'{}'`.
  **Re-extract everything for a brand:** change the brand's extractor
  config (the fingerprint changes; bodies come from the fetch cache, no
  re-download within `min_refetch_h`).
* **Roll out brand by brand:** `brands.only: [<slug>, ...]`.

## Metrics and goals

Goals ([`goals.json`](goals.json)), each bound to a `RunResult.metrics`
key: `primary_spec_complete_pct` ≥ 45, `open_spec_conflicts` ≤ 20,
`runtime_models_live` ≥ 300, `amazon_sourced_values` = 0,
`sources_unhealthy_pct` ≤ 10.

Other keys: `models_total`, `catalog_models_complete|conflict|partial|missing`
(the whole catalog, from the stored rows), `models_unchanged`,
`models_to_consolidate`, `models_consolidated`, `models_written`,
`models_complete|partial|conflict|missing|unsourced` (this run's
consolidations), `models_write_failed`, `sources_body_requests`, `sources_due|attempted|changed|
unchanged|extracted|extraction_skipped|failed|gone|blocked|refused|deferred|
left_for_next_run`, `source_fetch_failed_pct`, `documents_failed`,
`candidates_extracted`, `extractor_errors`, `llm_items|calls|candidates|
rejected|deferred`, `adjudication_decisions`, `adjudication_spent_s`,
`conflicts_enqueued|retired|queue_pending|needs_human`, `values_inserted|
deleted|dropped`, `typed_upserts`, `status_updates`,
`primary_models_total|complete`, `primary_products_total|complete`,
`compat_*` (graph metrics summed over batches, plus `compat_upserts|deletes|
unchanged|protected`, `compat_mode_full|incremental`, `compat_skipped`,
`compat_fit_records`, `compat_fit_names_unresolved`, `compat_orphaned_rows`,
`compat_purged`, `compat_manual_released`), `runtime_models_estimated`, `runtime_rows_written|deleted`,
`runtime_models_removed`, `runtime_presets`, `typed_deletes`,
`llm_candidates_kept`, `db_reconnects`.

## Per-site instance

```json
{
  "id": "<site>-spec-extractor",
  "name": "<Site> Spec Extractor",
  "category": "research",
  "cron_expr": "10 4,16 * * *",
  "timezone": "America/Detroit",
  "entry_command": "SPEC_EXTRACTOR_CONFIG=<site repo>/agents/spec-extractor/site.yaml python3 /home/voidsstr/development/reusable-agents/agents/spec-extractor-agent/agent.py",
  "owner": "framework"
}
```

Seed the goals from [`goals.json`](goals.json) under the instance id.

## CLI (no run record)

```bash
A=agents/spec-extractor-agent/agent.py
python3 $A --config     --site-yaml <site.yaml>    # resolved settings + per-kind fields / required
python3 $A --print-sql  --site-yaml <site.yaml>    # the contract SQL
python3 $A --plan       --site-yaml <site.yaml>    # READ-ONLY session: due sources and why
python3 $A --dry-run    --site-yaml <site.yaml> --agent-id <site>-spec-extractor --model-id 42
                        # LIVE polite fetch + extract + consolidate on a READ-ONLY session:
                        # no DB / queue / LLM writes; holds the instance's run lock
```

`--spec-config-file` / `--compat-config-file` read local JSON instead of
the storage configs. Exit codes: 0 done; 1 config problems, the site DB
unreachable or a failed dry run; 2 invalid config, or `--dry-run` without
the instance id (`--agent-id`, `$AGENT_ID` or `database.lock_key`); 3
`--dry-run` while a run holds the instance's lock (nothing fetched; try
again after the run).

## Safety

* Never parses or stores Amazon text: Amazon URLs, redirects to Amazon and
  Amazon-looking pages are refused by the primitive; the writer drops any
  row whose source is Amazon as a second line (`amazon_sourced_values`).
* robots.txt is always honoured; one fetcher enforces the per-host
  interval within its process, and the run lock keeps one fetching process
  per instance (a cron run or a `--dry-run`); failures back off.
* Never blanks data: a failed, gone or blocked source keeps its last good
  values; only a refused (Amazon) source loses them.
* LLM use is off unless the site enables it, and then only with verbatim
  quotes, the pool reserve, and a provider without tools.
* Compat verdicts are conservative by the primitive's code floors
  (-40 °C, NEC 690.7(A), ≥ 5 % headroom, one adapter at most).

## Troubleshooting

| Symptom | Look at |
|---|---|
| run fails in its first second | `config problems` / `config invalid` in the summary: schema, unknown profile, a typed column for a field the kind lacks, a UA without the robots token |
| `sources_unhealthy_pct` rising | `spec_sources.<state>->>'status'` and `reason`; gone pages need new URLs |
| a model stays `partial` | the missing required field: add a label / brand extractor to the profile, or an operator value |
| `open_spec_conflicts` stuck | `conflicts_needs_human`: operator decisions (see above) |
| no compat edges for a model | its rulesets' fields (`compat_edges_unknown` counts pairs with missing inputs; a field in conflict counts as missing) |
| edges of a disabled ruleset still in the table | they are deleted on the next full rebuild (the config change triggers one); rows of an unknown ruleset need `compat.purge_orphaned: all` |
| an edge stays after its fit record was deleted | a `confidence = 'manual'` row the engine does not own: written by hand, edited by hand after the engine wrote it, or its record withdrawn while the agent state was lost (`compat.owned_manual` in the state). It is the operator's to delete |
| `cannot connect to the site DB ($NAME): ProgrammingError: not a valid connection string or URI` | the DSN in `$NAME` does not parse (the driver's text is withheld because it echoes the DSN): usually an unencoded special character in a URI password (`%` → `%25`, `@` → `%40`, `/` → `%2F`); test it with `psql "$NAME"` |
| an engine fix should reach every model now | bump `ENGINE_VERSION`, or run once with `AGENT_FORCE_RUN=1` |
| `models_write_failed` > 0 | the problem text names the model and the DB error (often a typed column type) |

## Tests

`framework/tests/test_spec_extractor_agent.py` — offline: fake HTTP over
the real F9 fixture documents, a fake chat, a throwaway postgres:16
container loaded with `fixtures/schema.sql` (skips without docker / the
local image; `F17_TEST_PG_DSN` may name an admin conninfo of an existing
throwaway server).
