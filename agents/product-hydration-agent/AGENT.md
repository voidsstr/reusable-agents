# Product Hydration Agent — engine (`product-hydration-agent`)

> Precomputes product-page copy (description, pros/cons, FAQ, SEO meta) with
> Claude and keeps Amazon price/offer data fresh, so product pages render
> complete, indexable content and valid Offer data without any request-time
> LLM call. North Star: **indexed pages + organic clicks** (thin/empty PDPs
> don't rank) and **Amazon conversions** (a stale or broken price loses the
> click-through).

This is the shared **engine**. It is never scheduled itself; per-site
instances run it. Overview / "why" doc: [README.md](README.md).

## At a glance

| | |
|---|---|
| Agent id | `product-hydration-agent` (engine); runs as the per-site id set in `AGENT_ID` |
| Home | `reusable-agents: agents/product-hydration-agent/` |
| Kind | AgentBase python engine (`ProductHydrationAgent`); manifest has `metadata.is_blueprint: true`, `enabled: false` |
| Schedule | none — `cron_expr: ""`. `install/register-all-from-dir.sh` skips blueprint manifests, and `GET /api/agents/product-hydration-agent` returns 404 (not registered) |
| Entry command | `python3 agent.py` with `PRODUCT_HYDRATION_CONFIG` + the DSN env var set (instances exec it via their `run.sh`) |
| Category | research |
| Status | engine only. Live instance: `specpicks-product-hydration-agent` (every 2h at :15, timer enabled). No aisleprompt instance exists as of 2026-09-23 |
| Instance runbook | `specpicks: agents/product-hydration-agent/AGENT.md` |

## What it does

`run()` in `agent.py`, in order:

1. **Setup.** Loads the YAML at `PRODUCT_HYDRATION_CONFIG`. Reads the DSN from
   the env var named by `database.url_env` (default `DATABASE_URL`). Loads
   `prompts/hydrate_product_system.md`. Creates a local run dir
   `<runs_root>/<site.id>/<run_ts>/`. Missing config, DSN or prompt →
   `SystemExit`.
2. **DB connect.** Uses `@with_retry(3, 1.5)`. On failure it calls
   `notify_operator(severity=high)` and returns `failure`.
3. **Phase 0: Amazon price refresh** (skipped when `price_refresh.provider`
   is unset or `none`). When there is no `price_refresh:` block, a legacy
   `amazon_paapi: {enabled: true}` block is treated as `provider: paapi`.
   - **Candidates**: `products` rows that are `is_active`, whose `asin`
     matches `^[A-Z0-9]{10}$`, and whose `price_updated_at` is NULL or
     older than `price_freshness_hours`. At most `max_refresh_per_run` rows.
     With `prioritize_featured_and_linked: true`, rows are tiered: 0 =
     `is_featured`, 1 = ASIN in a published `editorial_articles.related_product_asins`
     or `buying_guides.picks[].asin`, 2 = everything else. Within a tier,
     rows are sorted by `review_count DESC`.
   - **Providers**: `creators` (Amazon Creators API via
     `framework/core/amazon_creators.py`; its calls are charged to the
     `hydration` line of the shared Creators daily budget,
     `config/amazon-creators-budget.json`), `paapi` (`paapi_client.py`,
     SigV4 PA-API v5) and `brightdata` (`brightdata_client.py`, Bright Data
     Amazon Products dataset, about $0.001 per ASIN estimated).
   - **Automatic fallback**: if the configured provider is `paapi` or
     `brightdata` and *its credentials are absent from env*, the run
     switches to `creators` when Creators credentials exist. The switch is
     logged as a decision. A provider whose key is present but rejected
     (for example an expired key) does **not** trigger the fallback.
   - **Per row write** (`_persist_provider_refresh`, committed per row):
     `price`, `original_price`, `currency`, `availability`, `is_prime`,
     `rating`, `review_count`, `main_image_url`, `amazon_url`, `brand` (only
     if empty), `raw_amazon_data` (source, fetched_at, image dims, features,
     compliance flags), `price_updated_at`, `last_fetched_at`. If the refresh
     has no list price and the stored `original_price` is below the new
     price, `original_price` is cleared. Without this, stale list prices
     leave negative "Save -$X" discounts (a Creators run once raised
     catalog-wide negative discounts from 277 to 314).
   - **Misses** (not in response, ItemNotAccessible, no data):
     `_stamp_miss` sets `price_updated_at = NOW()`, increments
     `fetch_attempts` and records `fetch_error`, so the row ages out of the
     stale queue and isn't retried every tick.
   - A Creators auth failure (`CreatorsUnavailable`) stops the phase and
     counts the remaining ASINs as failed. Any other exception is recorded
     and the run continues.
4. **Phase 1: content hydration.**
   - A defensive `conn.rollback()` clears any aborted transaction left by
     Phase 0.
   - **Candidates**: active `products` (optionally `site_id =
     site_id_filter`) where any requested content column is NULL, or
     `hydrated_at` is NULL, or `hydrated_at` is older than
     `stale_after_days`. Sorted by `<selection_priority> DESC NULLS LAST`,
     limited to `batch_size`. The row joins `categories` for
     `category_slug`.
   - **Per product**: it works out which types are still NULL; a stale row
     with every column filled regenerates all of them. It builds a JSON
     product context (title, asin, brand, category, price, rating,
     review_count, up to 10 features, and a raw description trimmed to 600
     chars), then runs `claude --print --model <claude.model> --max-turns N
     --append-system-prompt <prompt> --dangerously-skip-permissions` with a
     per-call timeout. The call uses `@with_retry(2, 2.0)` on timeout/IO
     errors, and each call is mirrored to the dashboard Live LLM stream.
   - **Parse + validate** each type (see table below). Only the types that
     pass are written. `hydrated_at = NOW()` and `hydration_model =
     'claude-<model>'` are stamped, with one commit per product.
   - Once `max_runtime_minutes` has elapsed, no new product is started.
5. **Phase 2: featured-set refresh.** If `FEATURED_SELECT_SCRIPT` names an
   existing file, the agent runs it with `DATABASE_URL=<dsn>` (120s timeout).
   The result goes into the decision log only (`featured set refreshed:
   <last stdout line>`, or an `error` decision on non-zero rc). It is not
   written to `results.json`. The script is site-specific and lives in the
   instance dir.
6. **Coverage stats, artifacts, digest email, RunResult** (see Outputs).

If Phase 1 selects no candidates, the run returns early with `success`
right after writing artifacts: Phase 2 and the digest email are skipped
in that case.

### Validation rules (`CONTENT_SPEC` validators)

The system prompt asks for exactly 5 pros, 5 cons and 5 FAQ pairs. The
validators are more lenient. They trim bad output and keep what is usable
rather than rejecting the row:

| Type | Column | Accepts | Trims / flags |
|---|---|---|---|
| `description` | `description` TEXT | any non-empty string | whitespace |
| `pros_cons` | `pros_cons` JSONB | dict with at least 1 pro or con | max 5 each; each bullet cut to 100 chars |
| `faq` | `faq` JSONB | list with at least 1 `{question, answer}` pair | drops incomplete pairs |
| `seo_meta` | `seo_meta` JSONB | any of title / meta_description / keywords | title cut to 60, meta to 160, keywords to 7; adds `short_meta_description: true` when meta < 120 chars |

## Inputs

- **DB tables**: `products` (read + write), `categories` (slug join),
  `editorial_articles` + `buying_guides` (tier-1 price priority, read only).
  Phase 1 reads these columns: `id, site_id, asin, title, brand, price, rating,
  review_count, features, description, category_id, pros_cons, faq,
  seo_meta, hydrated_at`.
- **Config**: the instance `site.yaml` (see Configuration).
- **Prompt**: `prompts/hydrate_product_system.md` (strict output schema plus
  the Amazon Associates and Google guideline notes).
- **External**: `claude` CLI on PATH. Under `framework/agent_run_wrapper.sh`
  this is the claude-pool shim when `~/.reusable-agents/claude-pool/bin/claude`
  exists and `CLAUDE_POOL` is not `0`. Also Amazon Creators API, PA-API v5,
  or the Bright Data datasets API, depending on the provider.

## Outputs

- **DB writes** (`products`): hydration columns `description`,
  `pros_cons`, `faq`, `seo_meta`, `hydrated_at`, `hydration_model`; price
  columns listed in Phase 0; `fetch_attempts` / `fetch_error` on misses. The
  instance's featured script also writes `is_featured`.
- **Framework storage** `agents/<agent_id>/runs/<run_ts>/`:
  `results.json` (totals, per-type success/failure, success_rate,
  avg_seconds_per_product, coverage, `amazon_paapi` provider summary),
  `goal-progress.json` (the `goals:` strings from site.yaml plus current
  coverage %), `llm-output.txt` (raw Claude output sample, 50 KB or less),
  `context-summary.md`, `hydration-log.jsonl` (one line per product).
  Each file is mirrored to the local run dir.
- **Email digest**: `_maybe_send_digest` via `shared.site_quality.send_via_msmtp`.
  It sends when there are failures (with `send_only_when_failures: true`,
  the default), on every run (with `false`), and always on Mondays (UTC).
  `agent_run_wrapper.sh` exports `DIGEST_ONLY=1` by default, so the message
  goes to the digest queue rather than straight out.
  `send_run_summary_email = False` (no AgentBase summary mail).
- **Operator alerts** (`notify_operator`, severity high): DB connect failure,
  candidate-selection failure, or every product failing.
- **No recs, no handoffs, no queue writes.**
- **RunResult status**: `failure` = DB connect or candidate-select error, or
  failures > 0 with zero hydrated and zero partial. `partial_failure` = any
  product failed. `success` = everything else, including an empty queue
  ("catalog already fully hydrated within freshness window"). Price-refresh
  failures never change the status.
  `next_state = {last_run_ts, site_id_filter}`. The empty-queue early return
  sets no `next_state`, and its metrics are the raw totals keys plus
  `catalog_coverage_pct`.

### `RunResult.metrics` keys

`total_products` (queued), `hydrated`, `partial`, `failed`,
`skipped_already_fresh`, `claude_calls`, `claude_total_seconds`,
`catalog_coverage_pct` (% of active products with *all* requested columns
non-NULL), `stale_pct`, `prices_refreshed`, `prices_failed`,
`amazon_paapi_batches`, `amazon_paapi_skipped_reason`, `compliance_pass`,
`compliance_fail`, `per_type_<type>`, `per_type_failed_<type>`.

The `amazon_paapi_*` metric names, the run summary text ("Refreshed N Amazon
prices via PA-API") and the email headings say "PA-API" whatever the
provider actually was. Check `results.json → amazon_paapi.provider` for the
real one.

## Goals & metrics

The engine declares no goals. Each instance binds its goals to the metric
keys above. See the instance runbook for the current goals and values.

## Configuration

### Env vars

| Name | Default | Meaning |
|---|---|---|
| `PRODUCT_HYDRATION_CONFIG` | required | path to the instance `site.yaml` |
| `DATABASE_URL` (or the name in `database.url_env`) | required | Postgres DSN |
| `AGENT_ID` | class id `product-hydration-agent` | per-site id (set by the systemd unit) |
| `FEATURED_SELECT_SCRIPT` | unset (Phase 2 skipped) | site-specific featured-set script |
| `AMAZON_CREATORS_CLIENT_ID` / `_CLIENT_SECRET` / `_PARTNER_TAG` / `_MARKETPLACE` | unset | Creators API credentials (all three of id/secret/tag required) |
| `AMAZON_PAAPI_ACCESS_KEY` / `_SECRET_KEY` / `_ASSOCIATE_TAG` | unset | PA-API credentials (env names are overridable in site.yaml) |
| `BRIGHTDATA_API_KEY` | unset | Bright Data key (name overridable via `price_refresh.brightdata_api_key_env`) |
| `DIGEST_ONLY` | `1` (wrapper) | `1` queues the digest email instead of sending it |
| `STORAGE_BACKEND` | per host | framework storage backend |

### `site.yaml` keys read by the engine (code defaults)

| Key | Default |
|---|---|
| `site.id` | `default` (used in local run-dir path + email subject) |
| `database.url_env` / `database.site_id_filter` | `DATABASE_URL` / none |
| `hydration.content_types` | all four of `description, pros_cons, faq, seo_meta` (unknown values cause `SystemExit`) |
| `hydration.batch_size` / `max_runtime_minutes` / `stale_after_days` | 25 / 30 / 90 |
| `hydration.selection_priority` | `review_count` (allowed: `review_count, rating, sales_rank, price, id`) |
| `claude.model` / `max_turns` / `per_call_timeout_s` | `opus` / 10 / 300 |
| `price_refresh.provider` | unset (skip). Values: `creators`, `paapi`, `brightdata`, `none` |
| `price_refresh.price_freshness_hours` / `max_refresh_per_run` | 24 / 200 |
| `price_refresh.prioritize_featured_and_linked` | false |
| `price_refresh.creators_batch_size` / `paapi_batch_size` / `brightdata_batch_size` | 10 / 10 / 100 |
| `price_refresh.brightdata_poll_interval_s` / `brightdata_poll_timeout_s` | 10 / 600 |
| `price_refresh.paapi_region` / `marketplace` / `paapi_throttle_per_second` / `associate_tag` | `us-east-1` / `www.amazon.com` / 1.0 / from env |
| `email.to` / `from` / `msmtp_account` / `subject_template` / `send_only_when_failures` | none (no email) / none / `automation` / `[HYDRATION:{site}] {n} products hydrated — {date}` (`{p}` = prices refreshed) / true |
| `goals.*` | free-text strings copied into `goal-progress.json` |
| `runs_root` | `~/.reusable-agents/product-hydration-agent/runs` |

The engine does **not** read `amazon_seo:` or `google_seo:` blocks, and it
does not emit `json_ld`, even though the specpicks `site.yaml` declares
those blocks. The compliance thresholds are hardcoded in
`_compliance_flags()`: title 80–200 chars, image long side 1000 px or more,
brand, main image, price. `config.example.yaml` is the annotated template.

## DB schema requirements

Each site DB needs these on `products`:

```sql
ALTER TABLE products
    ADD COLUMN IF NOT EXISTS pros_cons JSONB,
    ADD COLUMN IF NOT EXISTS faq JSONB,
    ADD COLUMN IF NOT EXISTS seo_meta JSONB,
    ADD COLUMN IF NOT EXISTS hydrated_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS hydration_model VARCHAR(50);
```

`description` reuses the existing TEXT column. The price phase also needs
`price_updated_at`, `last_fetched_at`, `raw_amazon_data` (JSONB),
`fetch_attempts`, `fetch_error`, `is_featured`, `original_price`, `currency`,
`availability`, `is_prime`, `main_image_url`, `amazon_url` (all present in
the specpicks schema). The specpicks migration for the hydration columns is
`db/migrations/024_product_hydration_columns.sql`.

## Short-circuit & idempotency

- No `signals()` override, so every tick does real work. The candidate
  queries themselves are the idempotency guard: fresh rows aren't selected,
  and misses are stamped so they age out.
- Each product and each price row commits on its own, so a crash loses at
  most one in-flight row.
- The featured script is idempotent: it sets and clears `is_featured`
  against the computed set.

## Running & inspecting

Run the engine only through an instance (see the instance runbook for the
systemd unit). For a local smoke test against a scratch DB:

```bash
# test.yaml: copy config.example.yaml, set hydration.batch_size: 1 and
# price_refresh.provider: none (otherwise Phase 0 hits the Amazon provider)
PRODUCT_HYDRATION_CONFIG=/tmp/test.yaml \
DATABASE_URL='postgresql://user:pass@localhost/<db>' \
STORAGE_BACKEND=local AGENT_STORAGE_LOCAL_PATH=/tmp/ra-data \
python3 /home/voidsstr/development/reusable-agents/agents/product-hydration-agent/agent.py
```

There is no dry-run flag. This **writes** to whatever DB the DSN points at.

## Failure modes & troubleshooting

| Symptom | Cause / evidence | Action |
|---|---|---|
| `prices_refreshed=0`, `prices_failed≈max_refresh_per_run` every run; decisions show `BD HTTP 401 … Token expired` | Bright Data key present but expired. Because the key *exists*, the Creators fallback doesn't fire (specpicks, every run since at least 2026-08-24) | Renew `BRIGHTDATA_API_KEY`, or switch the instance to `price_refresh.provider: creators` (Creators credentials are provisioned on the fleet host) |
| `skipped_reason: "… credentials not in env"` | provider creds absent and no Creators fallback available | provision the `AMAZON_CREATORS_*` env names in `~/.reusable-agents/secrets.env` |
| Burst of `db write failed: connection already closed`; the log shows `_persist_product attempt n/4 failed: InterfaceError` | Postgres connection dropped mid-run. `with_retry` retries on the *same* closed connection, so every remaining product fails, and coverage stats fail too (`catalog_coverage_pct` records 0.0). Seen on specpicks run `20260923T101500Z`: first `OperationalError: SSL connection has been closed unexpectedly` at 10:35:50Z, then 30 `db_write_failed` rows (34 failures in total) | Transient: the next tick reconnects. Recurring: investigate DB-side idle/connection limits (unverified cause) |
| A few `claude returned non-JSON or empty output` / `validation_failed` rows per run | LLM output unusable for that product. Typical: 1–8 per 80-product run | None needed unless the rate climbs. Check `llm-output.txt` |
| All products fail → status `failure` + operator alert | claude CLI unavailable (pool exhausted / auth) or DB write path broken | check claude-pool health (`python3 -m framework.cli.claude_pool status`; re-auth commands via `… claude_pool login-help`), then DB |
| `current transaction is aborted` cascade | a failed SQL left the transaction aborted (fixed 2026-05-01: rollback after every persist failure, plus a defensive rollback before candidate selection) | should not recur. If it does, find the un-rolled-back path |

## Related agents

- **Instance**: `specpicks-product-hydration-agent`
  (`specpicks: agents/product-hydration-agent/`).
- **Downstream consumers**: the specpicks runtime reads the hydrated
  columns. `src/routes/products.ts` and `src/routes/content.ts` return
  HTTP 202 `pending` for an unhydrated product. SSR
  (`src/services/ssrRender.ts`) reads `products.faq`; it does not
  reference `pros_cons` or `seo_meta` by name (its pros/cons block reads
  `pros`/`ai_pros` fields). `is_featured` feeds the homepage "Editor's
  Choice" picks and the `specpicks-article-proposal-agent` prompt.
- **Adjacent**: `specpicks-ebay-product-sync-agent` (retro/eBay listings);
  `agent-metrics-collector` computes DB-side freshness metrics for the
  instance's goals.
- **Shared primitive**: `framework/core/amazon_creators.py` (Creators API
  client).
