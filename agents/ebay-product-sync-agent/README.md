# ebay-product-sync-agent

Reusable framework agent that ingests live eBay Browse API listings into
any destination product database. Postgres and Azure SQL are supported
out of the box. The agent uses Claude (via the user's Max subscription
through the framework's `claude-cli` provider) to propose an eBay → DB
column mapping on first run, emails it to the operator for approval, and
then uses the approved mapping for all subsequent ingestion runs.

## Why this exists

eBay listings are short-lived. Auctions end, fixed-price items sell out,
sellers de-list. A static scrape goes stale within hours. This agent
keeps a destination product catalog continuously up to date with current
listings, marks anything not refreshed in `stale_hours` as inactive, and
preserves an audit log of every run.

It's also a generalization of the ad-hoc scrapers many sites end up
building. Define a list of search queries per category once, point the
agent at your destination DB and your eBay credentials, approve the
mapping email, and let the cron drain the queue every hour.

## Operator setup

### 1. Get eBay credentials

Sign up at [developer.ebay.com](https://developer.ebay.com), create an
application, and grab:
- `EBAY_CLIENT_ID`
- `EBAY_CLIENT_SECRET`
- `EBAY_CAMPAIGN_ID` (optional — eBay Partner Network id for affiliate tagging)

Set them in the environment that runs the agent. The framework's UI also
accepts them as per-agent secrets if you'd rather not put keys in
shell-rc.

### 2. Create a site config

Copy `config.example.yaml` into your site repo (typically
`<your-site>/agents/ebay-product-sync-agent/site.yaml`) and customize:

- `site_id` — unique key for your tenant. The approved mapping is keyed
  on this so multiple sites can share the same agent installation.
- `owner_email` — gets the schema-mapping approval email.
- `destination.kind` — `postgres` or `azure-sql`.
- `destination.dsn` — env-expandable DSN.
- `destination.table` — the table to ingest into. Schema-qualifiable
  (`"shop.products"`).
- `destination.site_constants` — values merged into every row. For
  multi-tenant DBs use this to inject the right `site_id` UUID.
- `seeds[]` — category + queries. The agent runs each query through the
  Browse API, applies the mapping, and upserts the items. If your DB
  has a `categories` table with matching slugs, the agent fills
  `category_id` automatically.
- `stale_hours` — listings not refreshed within this window get
  `is_active = false`. 72h is a reasonable default.

### 3. Register the agent

```bash
# In the framework repo
cd /path/to/reusable-agents
docker compose up -d           # start the framework + host worker
bash install/register-all-from-dir.sh /path/to/your-site/agents
```

### 4. First run — generates the mapping proposal

```bash
EBAY_PRODUCT_SYNC_CONFIG=/path/to/your-site/agents/ebay-product-sync-agent/site.yaml \
EBAY_CLIENT_ID=... EBAY_CLIENT_SECRET=... \
DATABASE_URL=postgres://... \
  python3 /path/to/reusable-agents/agents/ebay-product-sync-agent/agent.py
```

Output: `{"status": "awaiting_confirmation", "confirmation_id": "map-..."}`.
The agent emailed the operator with the proposal HTML.

### 5. Approve the mapping

The email shows every destination column, the proposed eBay source path,
the transform, and any default. Reply with one of:

- `APPROVE` — store the mapping and start ingesting on the next run.
- `REJECT <reason>` — discard. The next run regenerates a fresh proposal.
- `EDIT <json>` — paste a corrected JSON mapping; the agent uses
  yours instead of Claude's. Useful when you want to tweak a transform
  or add a constant.

The framework's responder agent picks up the reply and writes a
`ConfirmationRecord` with `state=approved`. Re-run the sync agent and
it'll promote the proposal to the approved mapping (in
`agents/ebay-product-sync-agent/mappings/<site_id>.json`) and proceed
to ingestion.

### 6. Ongoing runs (cron)

Every cron tick:
1. Loads the approved mapping from storage.
2. Walks the seed queries (cap = `max_queries_per_run`).
3. Calls the Browse API per query.
4. Applies the mapping to each item.
5. Upserts into the destination table using `key_columns` from the mapping.
6. Marks rows whose `updated_at` is older than `stale_hours` as inactive.
7. Logs run stats to the framework's status store.

## Mapping JSON shape

```json
{
  "schema_version": "1",
  "site_id": "specpicks",
  "destination_table": "products",
  "key_columns": ["site_id","asin"],
  "approved_at": "2026-04-27T18:30:00Z",
  "approved_by": "ops@example.com",
  "fields": [
    {"destination_column": "asin", "source_path": "legacyItemId", "transform": "ebay_id_prefix"},
    {"destination_column": "title", "source_path": "title"},
    {"destination_column": "price", "source_path": "price.value", "transform": "parse_float"},
    {"destination_column": "main_image_url", "source_path": "image.imageUrl", "transform": "image_first"},
    {"destination_column": "affiliate_url", "source_path": "itemAffiliateWebUrl", "transform": "affiliate_url"},
    {"destination_column": "condition", "source_path": "condition", "transform": "condition_lower"},
    {"destination_column": "seller_username", "source_path": "seller.username", "transform": "seller_username"}
  ],
  "constants": [
    {"destination_column": "source", "value": "ebay"},
    {"destination_column": "is_active", "value": true}
  ]
}
```

### Transform whitelist

| Transform | Effect |
|---|---|
| `ebay_id_prefix` | `EBAY_<legacyItemId>` — for cross-marketplace dedup |
| `parse_float` / `parse_int` | numeric coercion with NULL fallback |
| `iso_date` | pass-through ISO 8601 |
| `feedback_pct_to_5` | seller feedback % → 0-5 rating |
| `affiliate_url` | use `itemAffiliateWebUrl` else `itemWebUrl` |
| `image_first` | first non-empty image (image → thumbnail → additional) |
| `buying_options_csv` | CSV of `buyingOptions[]` (FIXED_PRICE, AUCTION) |
| `json_dumps` | serialize to JSON string |
| `condition_lower` | lowercase eBay condition string |
| `seller_username` / `seller_feedback_score` | nested seller fields |
| `location_country` / `location_postal` | nested itemLocation fields |

## CLI flags

```bash
# Dry run — fetch, propose, but don't write to the destination DB.
python3 agent.py --dry-run

# Discard the stored mapping and regenerate a fresh proposal.
python3 agent.py --force-remap

# Verbose logging.
python3 agent.py --verbose
```

## Architecture

```
   ┌──────────────────────────────────────────────────────┐
   │                  cron / manual run                   │
   └────────────────────────┬─────────────────────────────┘
                            │
                            ▼
   ┌──────────────────────────────────────────────────────┐
   │  EbayProductSyncAgent.run()                          │
   │                                                      │
   │  PHASE 1 — load or propose mapping                   │
   │    storage.read_json(mappings/<site>.json)           │
   │    if approved: → PHASE 2                            │
   │    if pending:  → ConfirmationPending (exit clean)   │
   │    else:                                             │
   │      • introspect_table()                            │
   │      • sample_rows()                                 │
   │      • ebay.search() one item                        │
   │      • Claude → mapping JSON                         │
   │      • email proposal                                │
   │      • write_confirmation(state=pending)             │
   │      • ConfirmationPending                           │
   │                                                      │
   │  PHASE 2 — ingest                                    │
   │    for (cat, query) in seeds:                        │
   │      items = ebay.search()                           │
   │      rows  = [apply_mapping(it) for it in items]     │
   │      adapter.upsert_rows(rows)                       │
   │    mark_stale_inactive(table, stale_hours)           │
   └────────────────────┬───────────┬─────────────────────┘
                        ▼           ▼
                 ┌──────────────┐  ┌──────────────────┐
                 │  Postgres    │  │  Azure SQL       │
                 │  (psycopg2)  │  │  (pyodbc)        │
                 └──────────────┘  └──────────────────┘
```

## Files

- `agent.py` — main entry, AgentBase subclass.
- `db_adapter.py` — `PostgresAdapter` + `AzureSqlAdapter` with
  `introspect_table`, `sample_rows`, `upsert_rows`.
- `ebay_client.py` — Python OAuth + Browse API client.
- `mapping.py` — apply-mapping + transform whitelist.
- `manifest.json` — framework registration.
- `config.example.yaml` — copy-paste template.
- `requirements.txt` — psycopg2 + pyodbc + PyYAML.
