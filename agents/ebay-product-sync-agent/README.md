# eBay Product Sync Agent (`ebay-product-sync-agent`)

> Keeps a site's catalog stocked with live eBay listings: canonical
> `products` rows plus short-lived `ebay_listings` rows linked by foreign
> key. North Star link: **conversions**. Retro / pre-2012 hardware pages
> send buyers to eBay instead of Amazon (see the repo `CLAUDE.md`, section
> "Retro / pre-2012 hardware → eBay"). A product with no active listing has
> no working buy link, so this agent's output is what the site's eBay CTAs
> and `/api/products/:asin/ebay-listings` render.

This file is the **engine runbook** (the manifest's `runbook` is
`README.md`). Per-site wiring for the one live instance lives in
`specpicks: agents/ebay-product-sync-agent/README.md`.

## At a glance

| | |
|---|---|
| Agent id | `ebay-product-sync-agent` (class default). Scheduled runs use the per-site id from the `AGENT_ID` env var, e.g. `specpicks-ebay-product-sync-agent` |
| Home | `reusable-agents: agents/ebay-product-sync-agent/` |
| Kind | AgentBase python engine (`EbayProductSyncAgent`). **Caveat:** `main()` calls `agent.run()` directly, not `run_once()`. See [Known issues](#known-issues-verified-2026-09-23) |
| Schedule | Engine manifest has no cron and is **not registered** on its own. Instances carry the schedule (table below) |
| Entry command | `EBAY_PRODUCT_SYNC_CONFIG=<site.yaml> DATABASE_URL=<dsn> python3 /home/voidsstr/development/reusable-agents/agents/ebay-product-sync-agent/agent.py [--dry-run] [--force-remap] [--verbose]` |
| Category | `ingestion` (manifest `task_type: scheduled-cron`) |
| Status | Engine: not registered. Live through the specpicks instance |
| Confirmation flow | `schema-mapping-approval`. The first run for a site emails a mapping proposal and waits for APPROVE / EDIT / REJECT |

### Instances

| Instance id | Site config | Schedule | Timer | Latest outcome (log, 2026-09-23) |
|---|---|---|---|---|
| `specpicks-ebay-product-sync-agent` | `specpicks: agents/ebay-product-sync-agent/site.yaml` | manifest `30 * * * *` America/Detroit; systemd `OnCalendar=*-*-* *:30:00` | enabled | 12:30 EDT run: `queries=8 items=156 products=116 listings_in=5 listings_up=111 success_rate=74.36%`. The 13:30 run was still in LLM hydration timeouts when this was written |

## What it does

The steps below follow `EbayProductSyncAgent.run()` → `_run_inner()` →
`_ingest_v2()`. The file also contains a v1 single-table `_ingest()`, but
nothing calls it.

1. **Load config.** Reads the YAML at `EBAY_PRODUCT_SYNC_CONFIG`, which is required. If the YAML sets `secrets_file` (resolved relative to the YAML), it loads that file into the environment without overriding variables that are already set. It then opens a DB adapter for `destination.kind`: `postgres` (psycopg2) or `azure-sql` (pyodbc).
2. **eBay auth.** Obtains an OAuth `client_credentials` token and runs a healthcheck. Missing credentials or a failed OAuth call raises, and the run exits non-zero.
3. **Mapping (phase 1).** Reads `agents/<agent_id>/mappings/<site_id>.json` from framework storage. A mapping is used as-is when it has `approved_at` and `schema_version == "2"`. Otherwise the agent:
   - drains email replies from `agents/<agent_id>/responses-queue/` and applies the first-line commands `APPROVE`, `EDIT <json>`, `REJECT <reason>`, `CREATE NEW`, or `USE TABLES products=<t> listings=<t>`;
   - if no approved or pending proposal exists, builds a new one. It introspects the products and listings tables, samples 3 rows from each, fetches 1 sample eBay item, lists active `categories.slug` values, and asks the LLM (`MAPPING_SYS` prompt) for a v2 two-table mapping;
   - emails the proposal, writes a pending `ConfirmationRecord` (`map-<site_id>-<epoch>`), and raises `ConfirmationPending`. `main()` then prints `{"status": "awaiting_confirmation", ...}` and exits 0.
4. **DDL (phase 2, one-time).** If the mapping is not yet `ddl_applied`, the agent runs each section's `create_ddl` and sets `ddl_applied: true`.
5. **Audit.** Checks up to `audit_max_per_run` (default 200) active listings, oldest `updated_at` first, with Browse `GET /item/{ebay_item_id}`. A 404 marks the listing inactive in one batched `UPDATE`. Skipped on `--dry-run`.
6. **Plan the queries.** The plan draws on several queues:
   - **Coverage queue:** up to 40 products with zero active listings, most recently emptied first.
   - **Priority seeds:** seeds whose `category` is in `priority_categories`.
   - **Other seeds:** all remaining seeds.
   - **Inbound handoffs:** handoffs with `work_type` `ebay_fetch_for_product` or `ebay_on_demand_fetch` (`rec.product_query` + `rec.product_category`) are inserted at index 0 of the priority-seed pool. They get no slots when `priority_seeds_pct` is 0 (the code default), and the daily rotation below is applied to the pool afterwards, so a handoff is picked only if it lands inside that day's slice.

   Budget: `priority_seeds_pct` of `max_queries_per_run` goes to priority seeds, `seed_reservation_pct` of the remainder goes to other seeds, and coverage gets the rest. The seed rotation offset is `day_of_year × 7`, so every tick within one UTC day picks the same seed slice. The queues are interleaved priority → coverage → seed.
7. **Per query:**
   - **Search.** Calls Browse `item_summary/search` with `ebay_filter`, `limit=per_query_limit` (capped at 200), and `fieldgroups=EXTENDED`.
   - **Hydrate canonical products.** Pass 1 builds the product from eBay `brand` + `mpn` + `title`, with confidence 0.85 when there is an `mpn` and 0.65 without one. Pass 2 sends the remaining items to the LLM in batches of 8 (`HYDRATION_SYS` prompt, via `self.ai_chat`). Items with confidence below 0.5 are skipped and counted in `products_skipped_low_conf`.
   - **Upsert the product.** Looks up an existing product by the mapping's `match_columns`, else upserts on `key_columns`. It then builds the listing row with the product FK plus `item_end_date` from `itemEndDate`, and upserts listings on the listings `key_columns`.
8. **Reap stale listings.** Runs `ALTER TABLE … ADD COLUMN IF NOT EXISTS item_end_date`, then marks listings inactive where `item_end_date < now` or `updated_at` is older than `stale_hours`.
9. **Completion email.** Includes KPIs, catalog totals, a per-category table, samples, and errors grouped by class. It is sent to `owner_email` unless `--dry-run` is set or `owner_email` is empty. It goes through `shared.site_quality.send_via_msmtp`, and under `DIGEST_ONLY=1` (exported by `agent_run_wrapper.sh`) it is queued to `digest-queue/` instead of being sent. Every send attempt is also recorded under `outbound-emails/`.
10. **Result.** Returns `RunResult(status="success", metrics=stats)`. Scalar goal keys are added to `stats` (see Goals & metrics).

## Inputs

| Input | Detail |
|---|---|
| Site YAML | Path in `EBAY_PRODUCT_SYNC_CONFIG`. Keys read by code: `site_id`, `owner_email`, `sender_email`, `secrets_file`, `ebay.{client_id,client_secret,campaign_id}[_env]`, `ebay.env`, `ebay.marketplace_id`, `destination.{kind,dsn,products_table,table,listings_table,mode,site_constants}`, `stale_hours`, `ebay_filter`, `per_query_limit`, `max_queries_per_run`, `audit_max_per_run`, `priority_categories`, `priority_seeds_pct`, `seed_reservation_pct`, `seeds[].{category or category_slug, queries[]}` |
| Destination DB | `destination.dsn` goes through `os.path.expandvars`. Reads the products + listings tables and `categories (id, slug, is_active)` |
| eBay Browse API | `https://api.ebay.com/buy/browse/v1` (sandbox when `env: SANDBOX`). OAuth token URL `…/identity/v1/oauth2/token`. `X-EBAY-C-ENDUSERCTX` carries `affiliateCampaignId` when a campaign id is set |
| Framework storage | `agents/<agent_id>/mappings/<site_id>.json`, `agents/<agent_id>/responses-queue/`, `agents/<agent_id>/confirmations/` |
| LLM | `self.ai_chat` resolves through `framework.core.ai_providers`, where operator overrides beat the manifest. The manifest still says `claude-max-cli` / `claude-sonnet-4-6`, but the API reported `ollama-5090` / `qwen3:8b` (source: override) for the specpicks instance on 2026-09-23 |
| Inbound handoffs | `work_type` `ebay_fetch_for_product` / `ebay_on_demand_fetch`, read from `self.inbound_handoffs`. These are **never populated today** (see Known issues) |

## Outputs

| Output | Detail |
|---|---|
| Products table | Canonical rows built from the mapping `products_table` section, plus hydration fields and `site_constants`. Deduped by `match_columns` |
| Listings table | One row per eBay item (`key_columns`, FK `fk_to_product_column`), plus `item_end_date`. Stale rows are set `is_active=false` |
| Storage | The mapping doc (proposal → approved, `ddl_applied`), confirmation records, `outbound-emails/<request_id>` records, and `digest-queue/` entries when digest mode suppresses mail |
| Emails | Mapping proposal (expects a reply) and a per-run completion summary. The recipient is the site YAML's `owner_email` |
| Recs / handoffs | None emitted |
| Process result | Exit 0 with a JSON `{status, summary, metrics}` on stdout, or `awaiting_confirmation` JSON. An unhandled exception exits non-zero, and the wrapper then records `failure exited rc=N` |

## Goals & metrics

`RunResult.metrics` = the full `stats` dict plus these scalars:

| Metric key | Meaning |
|---|---|
| `sync_success_rate_pct` | `100 × products_upserted / max(items_seen, 1)` |
| `products_upserted_count` | Canonical products upserted this run |
| `errors_count` | `len(stats["errors"])`, counting search failures and per-item product upsert failures |
| `items_seen_count` | eBay items returned across all queries |

Other `stats` keys: `queries_run`, `items_seen`, `products_upserted`,
`products_skipped_low_conf`, `listings_inserted`, `listings_updated`,
`by_category`, `audit_checked`, `audit_ended`, `audit_errors`,
`coverage_targets`, `seed_pool_size`, `priority_seed_pool`, `seeds_in_plan`,
`coverage_in_plan`, `stale_listings_inactive`, `error_class_counts`, plus
up to 30 product and 30 listing samples.

Engine goals (`goals.json`; they apply only if the engine id were run directly):

| Goal id | target_metric | Target |
|---|---|---|
| `goal-sync-success-rate` | `sync_success_rate_pct` | ≥ 95 % |
| `goal-products-upserted-flow` | `products_upserted_count` | ≥ 50 products/run |
| `goal-sync-errors-zero` | `errors_count` | 0 errors/run |

The specpicks instance has its own goal set, listed in the instance README.
Because `main()` bypasses `post_run()`, **no goal receives Layer-B progress
from a run** (see Known issues).

## Configuration

| Name | Default | Meaning |
|---|---|---|
| `EBAY_PRODUCT_SYNC_CONFIG` | required | Path to the site YAML |
| `DATABASE_URL` (or any var referenced in `destination.dsn`) | none | Destination DSN, expanded from the environment |
| `AGENT_ID` | class id | Per-site id. Sets the storage prefix, which includes the mapping key |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | required | Default env names. Override with `ebay.client_id_env` / `ebay.client_secret_env` |
| `EBAY_CAMPAIGN_ID` | unset | EPN campaign for affiliate headers |
| `EBAY_ENV` | `PRODUCTION` | Used when `ebay.env` is absent |
| `EBAY_MARKETPLACE_ID` | `EBAY_US` | Used when `ebay.marketplace_id` is absent |
| `DIGEST_ONLY` | `1` (set by the wrapper) | Queues the completion and proposal emails to the digest instead of sending them |

YAML defaults in code: `stale_hours` 72, `per_query_limit` 60,
`max_queries_per_run` 80, `audit_max_per_run` 200, `priority_seeds_pct` 0.0,
`seed_reservation_pct` 0.6, `destination.listings_table` `ebay_listings`,
`destination.mode` `use-existing-products-new-listings`, `ebay_filter`
`buyingOptions:{FIXED_PRICE},conditions:{USED|NEW|REFURBISHED|FOR_PARTS_OR_NOT_WORKING},price:[5..5000],priceCurrency:USD`.
Start a new site from `config.example.yaml`. It shows the v1 single-table
`table:` form, which is still accepted.

### Mapping document (v2, stored per site)

```json
{
  "schema_version": "2",
  "mode": "use-existing-products-and-listings | use-existing-products-new-listings | create-new-tables",
  "site_id": "<site_id>",
  "products_table": {
    "name": "products", "create_ddl": null,
    "key_columns": ["site_id", "asin"], "match_columns": ["title"],
    "fields": [
      {"destination_column": "asin", "source_path": "legacyItemId", "transform": "ebay_id_prefix"},
      {"destination_column": "category_id", "from_hydration": "category_id"}
    ],
    "constants": [{"destination_column": "source", "value": "ebay"}]
  },
  "listings_table": {
    "name": "ebay_listings", "create_ddl": null,
    "key_columns": ["ebay_item_id"], "fk_to_product_column": "product_id", "fk_target": "id",
    "fields": [{"destination_column": "price", "source_path": "price.value", "transform": "parse_float"}],
    "constants": [{"destination_column": "marketplace", "value": "ebay"}]
  },
  "approved_at": "…", "approved_by": "…", "ddl_applied": true
}
```

The field values above are taken from the approved specpicks mapping. A
field with `from_hydration` takes its value from the canonical-extraction
record, and its `source_path` / `transform` are ignored.

### Transform whitelist (`mapping.py`)

| Transform | Effect |
|---|---|
| `ebay_id_prefix` | `EBAY_<legacyItemId>`, for cross-marketplace dedup |
| `parse_float` / `parse_int` | Numeric coercion with NULL fallback |
| `iso_date` | ISO 8601 pass-through |
| `feedback_pct_to_5` | Seller feedback % → 0–5 rating |
| `affiliate_url` | `itemAffiliateWebUrl`, else `itemWebUrl` |
| `image_first` | First non-empty image (image → thumbnail → additional) |
| `buying_options_csv` | CSV of `buyingOptions[]` |
| `json_dumps` | Serialize to a JSON string |
| `condition_lower` | Stripped eBay condition string. Despite the name, the code does **not** lowercase it |
| `seller_username` / `seller_feedback_score` | Nested seller fields |
| `location_country` / `location_postal` | Nested `itemLocation` fields |

## Short-circuit & idempotency

- No `signals()` override. Because `main()` calls `run()` directly, the
  framework's auto short-circuit would never apply anyway. Every tick does
  a full run.
- Upserts are idempotent on the mapping's key columns. Products dedupe by
  `match_columns` before insert.
- DDL runs once per approved mapping (`ddl_applied`). The `item_end_date`
  column add is `IF NOT EXISTS`.
- The seed rotation is deterministic per UTC day, so hourly ticks within a
  day repeat the same seed queries. Only the coverage queue and handoffs
  vary.

## Running & inspecting

```bash
# Trigger the scheduled instance (logs append to the file below)
systemctl --user start agent-specpicks-ebay-product-sync-agent.service
systemctl --user list-timers | grep ebay-product-sync

# Agent lines only. The log is dominated by Azure SDK HTTP logging.
grep -v "azure.core.pipeline\|^    '\|^Request\|^Response\|^No body\|^A body" \
  /tmp/reusable-agents-logs/agent-specpicks-ebay-product-sync-agent.log | tail -60

# Framework API (8090 is the listening port on whitebeast; auth required)
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/specpicks-ebay-product-sync-agent
```

Manual / dry run. `--dry-run` skips DB writes, the audit, the reaper, and
the completion email, but it still calls eBay and the LLM, and it still
emails a proposal if no approved mapping exists. Set `AGENT_ID` so the
instance's mapping is used. Without it, the agent reads
`agents/ebay-product-sync-agent/mappings/<site_id>.json`. For `specpicks`
that blob exists (last modified 2026-04-27) but sits in the Azure
**Archive** tier, so the read fails and storage returns nothing (checked
2026-09-23). A run without `AGENT_ID` would therefore build and email a
new proposal instead of ingesting.

```bash
set -a; . ~/.reusable-agents/secrets.env; set +a     # EBAY_* + storage vars
AGENT_ID=specpicks-ebay-product-sync-agent \
EBAY_PRODUCT_SYNC_CONFIG=/home/voidsstr/development/specpicks/agents/ebay-product-sync-agent/site.yaml \
DATABASE_URL='<specpicks DSN>' \
PYTHONPATH=/home/voidsstr/development/reusable-agents \
  python3 /home/voidsstr/development/reusable-agents/agents/ebay-product-sync-agent/agent.py --dry-run
```

`--force-remap` clears the stored mapping and emails a new proposal.
Ingestion then stops until the operator approves it.

### Onboarding a new site

1. Get eBay developer credentials (client id, secret, and an optional EPN
   campaign id). Put them in `~/.reusable-agents/secrets.env`, which is
   the unit's `EnvironmentFile`, or in a gitignored file named by
   `secrets_file`.
2. Copy `config.example.yaml` to `<site>/agents/ebay-product-sync-agent/site.yaml`.
   Set `site_id`, `owner_email`, `destination.*`, and `seeds`.
3. Write a per-site `manifest.json` whose `entry_command` sets
   `EBAY_PRODUCT_SYNC_CONFIG` and the DSN, and register it with the site
   repo's `agents/register-with-framework.sh`.
4. Start one run. It emails the mapping proposal. Reply `APPROVE`,
   `EDIT <json>`, `REJECT <reason>`, `CREATE NEW`, or
   `USE TABLES products=<t> listings=<t>`. A reply reaches the agent only
   if a responder-agent route delivers it to
   `agents/<AGENT_ID>/responses-queue/` (see Known issue 3). The next run
   promotes the approved mapping and ingests.

## Failure modes & troubleshooting

| Symptom | Cause / evidence | Action |
|---|---|---|
| `products=0 … success_rate=0.0%` with `products_skipped_low_conf == items_seen` while the run still reports success | LLM hydration failed on every batch. From 2026-09-22 23:11 to 2026-09-23 09:30 EDT every run logged `hydration batch failed: ollama unreachable at http://127.0.0.1:11434 (provider=ollama-5090)`. Pass 1 handled 0 items in every one of the 107 batches logged from 2026-09-22 23:11 to 2026-09-23 13:50 EDT (`0/30 items handled by ebay-fields`), so hydration depends entirely on the LLM | Restore the provider, or change the agent's override in `config/ai-defaults.json` |
| Run takes tens of minutes, with `hydration batch failed: generator didn't stop after throw()` every ~5 min | LLM call hit `timeout=300` (2026-09-23 13:30 run) | Same as above |
| `value too long for type character varying(500)` in `errors` | Product upsert rejected by a 500-char column (7, 13 and 7 errors in the 10:30, 11:30 and 12:30 EDT runs on 2026-09-23). The mapped `products` columns that are `varchar(500)` are `slug` (from hydration), `thumbnail_url` and `ebay_url`. Which one overflows was not identified | Inspect the failing item in `errors[]` against those three columns |
| `audit: N/N listings ended (0 errors)` on every run | On 2026-09-23 the audit reported 100 % ended (30/30, 45/45, 41/41). The approved mapping writes `ebay_item_id` from the bare `legacyItemId`, and the stored ids are bare numbers (read-only query, 2026-09-23). `get_item()` calls `/item/{id}`, but Browse `getItem` expects the RESTful `v1\|<id>\|0` form, so each call likely 404s and **every audited listing is deactivated**. *Suspected root cause, not tested against the live API.* At about 14:10 EDT on 2026-09-23, 0 of 34,535 `ebay_listings` rows were active | Verify one id by hand before trusting `audit_ended` |
| `eBay credentials not configured …` / `eBay OAuth failed: <code>` | Missing or invalid `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | Check `~/.reusable-agents/secrets.env`. The unit's `EnvironmentFile=-` tolerates a missing file silently |
| `secrets_file … not found — relying on process env` | Harmless when the vars come from `secrets.env`. It logs on every specpicks run because `specpicks/agents/ebay-product-sync-agent/.env` does not exist on whitebeast | none |
| All upserts fail after one error (`current transaction is aborted`) | Historical (2026-04-29). The code now rolls back per failure | Should not recur. If it does, check the rollback paths |
| `connection already closed` after long hydration | Azure Postgres idle timeout. The code calls `adapter.ensure_open()` before the audit, the upsert phase, the `item_end_date` ALTER, and the reaper | none |
| FK violation `products_site_id_fkey` | Historical (2026-04-30). The mapping's `site_id` constant changed from a UUID to `'ebay'` (mapping `change_log`) | Check `destination.site_constants` |
| Runs burning claude-pool calls despite an ollama override | Historical (2026-05-09 → 2026-05-11, 9,763 calls). Fixed by the resolution order in `ai_providers` (operator override beats manifest) | none |

### Known issues (verified 2026-09-23)

1. **Runs are invisible to the framework lifecycle.** `main()` calls
   `agent.run(...)` instead of `run_once()`, so `pre_run` / `post_run`
   never execute. Only the wrapper's `starting` / `success` status lines
   are written. The API shows no `recent_runs`, and goals get no Layer-B
   progress from `RunResult.metrics`.
2. **Handoffs are never drained.** With no `pre_run`,
   `self.inbound_handoffs` stays empty. On 2026-09-23,
   `agents/specpicks-ebay-product-sync-agent/handoff-queue/` held 1,831
   files dated 2026-05-04 through 2026-09-08. 1,824 are
   `ebay_fetch_for_product` handoffs (senders: `seo-implementer` 1,340,
   `specpicks-article-author-agent` 221,
   `specpicks-article-author-implementer` 103, `implementer` 89,
   `specpicks-article-proposal-agent` 71). The other 7 (2026-05-04 to
   2026-05-25) are in the Azure Archive tier and cannot be read.
   The generic `agents/ebay-product-sync-agent/handoff-queue/` also holds
   undrained handoffs (oldest dated 2026-05-12).
3. **No email-reply route.** The live responder config
   (`~/.reusable-agents/responder/config.yaml`) has no route whose
   `target_agent` is an eBay agent, so replies fall to the default
   `implementer`. The API's dispatch view (`_QUEUE_AGENT_IDS`) lists the
   generic `ebay-product-sync-agent` queue, while the instance drains
   `agents/specpicks-ebay-product-sync-agent/responses-queue/`. The one
   approved mapping was approved `by ui` (2026-04-29). This only matters if
   a new proposal goes out.

## Related agents

- **Instance:** `specpicks-ebay-product-sync-agent` (specpicks: `agents/ebay-product-sync-agent/README.md`).
- **Upstream:** `seo-implementer`, `implementer`, and the specpicks article agents send `ebay_fetch_for_product` handoffs (see Known issue 2). `responder-agent` would route mapping-approval replies, but no eBay route is configured (Known issue 3).
- **Side paths:** `specpicks/scripts/fetch-ebay-on-demand.py` is a fast on-demand fetch that reuses this dir's `ebay_client.py` and writes `ebay_listings` directly. `specpicks-ebay-counterpart-matcher` is registered separately; see its own runbook.
- **Downstream:** the SpecPicks API `/api/products/:asin/ebay-listings` (`specpicks/src/index.ts`) reads `ebay_listings` joined to `products`. `agent-metrics-collector` writes Layer-A points for the specpicks instance (see the instance README).
