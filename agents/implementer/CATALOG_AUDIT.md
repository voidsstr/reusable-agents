# Catalog-audit implementer runbook

You are processing catalog-audit recommendations. Each rec is a row-level
data-quality issue caught by `scripts/catalog-quality-audit.ts`. The fix
shape is **always a SQL migration** — never a code edit, never a TypeScript
change.

## Workflow

1. Read the recommendations file:
   ```
   cat $RESPONDER_RUN_DIR/recommendations.json
   ```
2. Read the site config (mostly for context — the migration writes against
   the live DB on deploy):
   ```
   cat $SEO_AGENT_CONFIG
   ```
3. For EACH rec id passed in `RESPONDER_REC_IDS`, write a NEW SQL file at
   `db/migrations/<UTC-ts>_catalog-audit-<criterion>.sql`. The criterion is
   in the rec's `id` field of the audit findings (e.g. `recipe-image-present`,
   `recipe-nutrition-sanity`). The file MUST contain:
     * Header comment naming the criterion + the row IDs it targets
     * The UPDATE/DELETE statements scoped by `id IN (...)` from
       `rec.evidence.ref_ids`
     * A trailing `-- VERIFY:` comment showing how to re-run the
       criterion's check after the migration applies
4. Also write `changes/<rec-id>.summary.md` with first line `# <rec-id> —
   <criterion>` and a `**Status:** SHIPPED` second line naming the
   migration file.

## DO NOT defer

The recs you receive list specific row IDs that need fixing right now.
The audit DETAIL field sometimes says things like "backfill-images job
should cover these but regressions happen" — **that is context, not
instruction**. The reason the rec exists is that the audit ran AFTER the
backfill and the rows are still flagged. Your job is to ship the fix.

DEFERRED summaries are only valid when:
  * Zero `evidence.ref_ids` are listed (no concrete rows to act on), AND
  * The rec text itself explicitly says "no action required"

In all other cases — including when the rec's DETAIL or rationale
mentions another job — write the SQL migration. The rows are flagged
because the other job didn't fix them; your migration is the catch-up.

## Migration patterns by criterion

| Criterion | Migration shape |
|---|---|
| `recipe-image-present` | `UPDATE recipe_catalog SET is_active = false WHERE id IN (<ids>) AND image_url IS NULL;` (deactivate so they stop showing as grey placeholders; backfill regenerates if/when scrapers return real URLs and someone manually reactivates) |
| `recipe-nutrition-sanity` | Two-step: deactivate junk titles (`is_active=false WHERE id IN (junk_ids)`) + null implausible nutrition on real recipes (`SET calories=NULL, protein=NULL, carbs=NULL, fat=NULL WHERE id IN (real_but_bad_ids)`) so enrichment regenerates them |
| `recipe-image-valid-url` | `UPDATE recipe_catalog SET image_url = NULL WHERE id IN (<ids>);` (force backfill on invalid URLs) |
| `recipe-cuisine-assigned` | If `evidence.detected_cuisine` present, UPDATE to that. Otherwise UPDATE to NULL + comment explaining manual review needed |
| `product-image-present` | `UPDATE kitchen_products SET is_active = false WHERE id IN (<ids>) AND primary_image_url IS NULL;` |
| `product-schema-rich-results` | UPDATE missing structured-data fields (brand, model, gtin) to inferred values from title — only when the inference is clean enough |

If a criterion isn't listed above, infer the migration shape from the
audit script's `check` SQL (find it in
`scripts/catalog-quality-audit.ts`). Default to `is_active=false` for
items that fail a content quality check, and `column = NULL` for items
where a backfill should regenerate the value.

## Hard rules

* Use ONLY the row IDs listed in `rec.evidence.ref_ids` — no blanket
  UPDATEs against the whole table.
* Add `WHERE is_active = true` (or equivalent) so re-runs are idempotent.
* Do NOT execute the SQL via psql/sqlite-CLI yourself — the deployer's
  migration runner applies it on deploy. Hand-execution skips the audit
  trail.
* Commit the migration file + the summary.md per rec. The wrapper
  bulk-commits at the end.
