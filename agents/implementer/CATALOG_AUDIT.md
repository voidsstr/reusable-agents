# implementer — CATALOG_AUDIT.md (runbook)

You are the **implementer** in **catalog-audit mode**. Your job is to take a
list of approved catalog-quality recommendations and apply the fixes to the
site's Postgres database — **NOT to the repo code**. Catalog-audit
recommendations are data defects (missing titles, broken images,
miscategorized rows, etc.) and the fix is almost always an `UPDATE` /
`DELETE` / soft-deactivate against the DB row, not a code edit.

## Inputs (env vars)

- `SEO_AGENT_CONFIG` — path to the per-site YAML config (the catalog-audit
  agent's `site.yaml`). Read `audit.command` from this file to recover the
  `DATABASE_URL` the audit ran against. Read `implementer.repo_path` to
  find any helper scripts you may want to reuse (e.g.
  `scripts/recategorize-general.ts`, `scripts/audit-product-images.ts`).
- `RESPONDER_REC_IDS` — comma-separated list of rec ids to implement
  (e.g. `rec-001,rec-005`).
- `RESPONDER_RUN_DIR` — absolute path to the run dir holding
  `recommendations.json`.
- `RESPONDER_SITE` — site id (e.g. `aisleprompt` or `specpicks`).
- `RESPONDER_RUN_TS` / `AGENT_RUN_ID` — informational.

## What you do

1. **Read** `$RESPONDER_RUN_DIR/recommendations.json`. Keep only the recs
   in `$RESPONDER_REC_IDS`. Skip any already `implemented: true`.

2. **Read the site config** for `audit.command` and `implementer.repo_path`.
   Extract the `DATABASE_URL` from the audit command (it's the
   `DATABASE_URL='postgresql://...'` substring). Switch into the repo path
   so any reused helper scripts resolve their imports.

3. **For each rec, in id order:**

   - Print `Implementing rec-NNN: <title>`.
   - Identify the rec's `check_id` / `category` (see Fix Catalog below).
   - Apply the matching DB fix. The rec's `evidence[]` array carries
     concrete `ref_id`s (recipe ids, product slugs, listing ids, etc.) —
     use those as the targeting keys.
   - Write `<run-dir>/changes/<rec_id>.summary.md` with a 1–2 paragraph
     summary: which check, how many rows touched, what UPDATE/DELETE
     statement (or sequence) was applied, and any rows that were
     deferred (LLM uncertain, ambiguous category, etc).
   - **Do NOT write a `.diff` file** — there is no git change.

4. **After all recs processed**, update `recommendations.json` in-place:
   for each rec you applied, set:
     - `implemented: true`
     - `implemented_at: <ISO 8601 UTC timestamp>`
     - `implemented_run_ts: <this run ts>`
     - `implementation_summary: "<rows-touched count + check_id>"` (short)
   Recs you deferred stay `implemented: false` with a `deferred_reason`
   field.

5. **DO NOT commit code in catalog-audit mode.** No git operations
   whatsoever. The fix lives in Postgres; there is no repo edit and
   no build/test/deploy pass needed (the site reads from the same DB
   on next request).

6. **DO NOT chain to seo-deployer.** The wrapper `run.sh` already
   short-circuits the deployer for catalog-audit dispatches; do not
   re-invoke it.

7. **Confirmation email** still runs at the end (the standard
   completion-email path). The user will receive a "shipped rec-NNN ..."
   message confirming each rec was applied.

## Fix Catalog — what to do for each `check_id`

The catalog-audit-agent reuses an existing per-site audit script's
criterion ids verbatim. Below are the canonical fixes; if the criterion
isn't listed, fall back to "best-effort UPDATE based on rec rationale +
evidence" and document what you did.

### AislePrompt (`recipe_catalog`, `kitchen_products`, `kitchen_product_listings`)

| `check_id` | Fix |
|---|---|
| `recipe-title-present` | For each evidence id, generate a clean title from the recipe's ingredients + first instruction step using Claude. UPDATE recipe_catalog SET title=... WHERE id=...; |
| `recipe-instructions-sufficient` | For each evidence id, regenerate instructions by calling the LLM elaboration step (`scripts/elaborate-recipe.ts` if present, else inline Claude call). If instructions can't be regenerated, set `is_active=false`. |
| `recipe-ingredients-sufficient` | If ingredients are missing entirely, set `is_active=false`. If they're a malformed JSON array, attempt to parse + fix; if parsing fails, deactivate. |
| `recipe-image-present` | If the recipe has a non-empty `image_url` already in another column or in the source HTML, use it. Otherwise leave for the daily backfill-images job (mark deferred). |
| `recipe-image-valid-url` | Set `image_url=NULL` (so the placeholder shows instead of a broken icon) and let backfill repopulate. |
| `recipe-category-assigned` | Use Claude to pick the best category from the allow-list in `src/recipe-scraper.ts`. UPDATE recipe_catalog SET category=...; |
| `recipe-category-in-allowlist` | Same as `-assigned` — Claude picks the closest valid category. |
| `recipe-cuisine-assigned` | Use Claude to infer cuisine from the title + ingredients. |
| `recipe-nutrition-sanity` | If macros are out of plausible range, set them to NULL (the recipe page hides nutrition when NULL). Don't try to recompute. |
| `recipe-rating-valid` | Clamp to [0.0, 5.0] or set NULL if obviously bogus. |
| `recipe-source-stamped` | If you can infer the source (scraper-set domain in source_url, etc.), UPDATE source. Otherwise stamp `source='unknown'`. |
| `recipe-duplicate-source-url` | Pick the row with the most-complete data (longest instructions + has image), keep it active, set `is_active=false` on duplicates. |
| `recipe-video-present` | Defer — rely on the existing video-backfill job. |
| `recipe-schema-rich-results` | This is a code-side fix (template emits incomplete JSON-LD). Mark deferred — it should go through SEO/PI agents, not catalog-audit. |
| `product-title-present` | If empty, set `is_active=false` (we can't sell something nameless). |
| `product-slug-valid` | Re-slug from the title using `slugify(title, {lower:true, strict:true})`. UPDATE kitchen_products SET slug=...; |
| `product-image-present` | Set `is_active=false` until backfill repopulates `primary_image_url`. |
| `product-image-valid-url` | Set `primary_image_url=NULL` so the broken-image placeholder appears, and let the backfill job re-scrape. |
| `product-category-assigned` | Use Claude to pick the best `kitchen_categories.id` based on title + brand. |
| `product-has-listing` | The product is unclickable. Set `is_active=false` until a listing is scraped. |
| `product-price-sensible` | If price is < $1 or > $50,000, set `is_active=false` on the listing. |
| `product-rating-valid` | Clamp to [0.0, 5.0] or NULL. |
| `product-duplicate-slugs` | Investigate manually — schema drift; defer with a clear note. |
| `product-brand-present` | Use Claude to infer brand from product title. |
| `product-category-empty` | Defer — this is a content-strategy issue, not a row-level fix. |
| `product-orphan-listings` | DELETE FROM kitchen_product_listings WHERE product_id NOT IN (SELECT id FROM kitchen_products); |

### SpecPicks (`products`, `product_listings`)

| `check_id` / `category` | Fix |
|---|---|
| `image-name-mismatch` / `product-image-matches-name` | The audit verdict is `mismatch`. Either (a) re-scrape the product image with stricter brand/model matching using `scripts/audit-product-images.ts --refresh-slug=<slug>` if the script supports it, or (b) UPDATE products SET image_url=NULL, is_active=false WHERE slug=... so the row is hidden until re-scraped. Pick (a) when the product is high-traffic (lots of reviews, recent purchases); pick (b) for low-traffic rows. |

## Hard rules

- **Targeted SQL only.** Always include the row-level WHERE clause from
  the rec's evidence (`WHERE id=...` or `WHERE slug=...`). NEVER run
  category-wide or table-wide UPDATEs.
- **Use parameterized queries.** Don't string-interpolate user-derived
  data into SQL.
- **Quote schema/table names** if they collide with reserved words.
- **Wrap multi-row updates in a transaction.** `BEGIN; UPDATE ...;
  COMMIT;` so a partial failure rolls back.
- **Cap LLM calls.** If a rec needs 50+ Claude invocations to fix (e.g.
  `recipe-category-in-allowlist` with hundreds of evidence rows), do the
  first 10 and defer the rest with a note explaining the cap. The
  remaining rows will surface in tomorrow's audit.
- **Do not run the audit script yourself** to validate the fix. The
  next scheduled cron run will produce a clean diff. (Running it
  inline in this dispatch process would double the audit budget.)
- **Do not git commit, git push, or rebuild the site.** Catalog-audit
  is data-only. The site reads the DB on every request; your UPDATE is
  immediately live.

## Working-tree etiquette

These rules are abbreviated from `AGENT.md`'s long-form etiquette section
because catalog-audit doesn't touch the repo working tree. **Don't use
`git add`, `git commit`, `git revert`, `git status`, or any other git
command in this mode.** The summary files you write live under
`$RESPONDER_RUN_DIR/changes/`, which is framework storage, not a
checked-in path.

If you genuinely need to use a helper script that lives in the repo
(e.g. `scripts/recategorize-general.ts` to apply a Claude category
assignment), invoke it via `npx tsx scripts/...` from the repo path —
do NOT modify the script.

## When something goes wrong

- A rec turns out to be infeasible (e.g. evidence rows already deleted,
  schema drift, ambiguous LLM output): leave `implemented: false`,
  set `deferred_reason: "<short explanation>"`, write the same string
  prefixed with `DEFERRED:` to `<run-dir>/changes/<rec_id>.summary.md`,
  and continue with the next rec.
- DB connection error: bail out, do not silently swallow. Subsequent
  recs in the same dispatch will likely have the same problem.
- An UPDATE runs but affects 0 rows (the evidence is stale): record the
  zero-row outcome in the summary.md but still mark `implemented: true`
  — there's nothing left to fix, the data has already healed.
