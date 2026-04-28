# product-hydration-agent — runbook

## Purpose

Replaces request-time AI content generation (the legacy
`contentGenerator.ts` / `ai-content.ts` services that called
Ollama / OpenAI per HTTP request). Content is now precomputed once
per product per `stale_after_days` window and stored in DB columns
the runtime reads directly. Routes that previously generated content
on demand now read from DB and return HTTP 202 if a row hasn't
been hydrated yet — telling the caller "ask again after the next
hydration run".

## How a run works

1. Read `PRODUCT_HYDRATION_CONFIG` env var → load `site.yaml`.
2. Read `DATABASE_URL` (or whatever `database.url_env` names) → connect.
3. SELECT products WHERE `is_active = TRUE` AND
   ([any requested content_type column IS NULL] OR `hydrated_at IS NULL`
   OR `hydrated_at < NOW() - stale_after_days`),
   filtered by `site_id_filter`, ORDER BY `selection_priority` DESC NULLS LAST,
   LIMIT `batch_size`.
4. Per product: build a user prompt with product context (title /
   brand / price / rating / features / category / raw description) +
   `requested_content_types`. Send to Claude with the system prompt
   from `prompts/hydrate_product_system.md`.
5. Validate the parsed JSON per type (e.g. pros_cons must have lists,
   faq must have at least one Q/A pair). Build the SET clause of
   only the columns that validated.
6. UPDATE the row, stamp `hydrated_at` + `hydration_model`. Commit.
7. Honor `max_runtime_minutes`: stop NEW work but finish the in-flight
   product. Write partial results.
8. Email digest only if (`send_only_when_failures: true` AND failures > 0)
   OR weekly status (Monday).

## Resilience

- DB connection / SELECT / UPDATE wrapped with `@with_retry(3, 1.5)`.
- Claude calls wrapped with `@with_retry(2, 2.0)` against
  `subprocess.TimeoutExpired` + IO errors.
- Per-product failures are recorded but never abort the run.
- Unrecoverable failures (DB down, all-products-failed) call
  `notify_operator(...)` which records the incident and (rate-limited)
  emails the operator.

## Schema requirements

Each per-site DB needs these columns added to `products`:

```sql
ALTER TABLE products
    ADD COLUMN IF NOT EXISTS pros_cons JSONB,
    ADD COLUMN IF NOT EXISTS faq JSONB,
    ADD COLUMN IF NOT EXISTS seo_meta JSONB,
    ADD COLUMN IF NOT EXISTS hydrated_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS hydration_model VARCHAR(50);
```

`description` is reused — the agent writes plain text into the
existing `description TEXT` column.

## Testing locally

```bash
PRODUCT_HYDRATION_CONFIG=/tmp/site.yaml \
DATABASE_URL='postgresql://user:pass@localhost/specpicks' \
STORAGE_BACKEND=local \
AGENT_STORAGE_LOCAL_PATH=/tmp/ra-data \
python3 agent.py
```

Set `hydration.batch_size: 1` in site.yaml for a quick smoke test.
