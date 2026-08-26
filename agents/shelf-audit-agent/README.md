# shelf-audit-agent

Verifies **what the site shows** against **what Amazon says**.

Every other catalog agent picks its targets from the database. This one picks
them from the *rendered site*: it crawls the homepage and 3 levels down (plus
the JSON endpoints behind client-rendered shelves), collects the products a
visitor can actually reach, and checks each one against the Amazon Product API.

## Why this exists

Measured 2026-08-26 by crawling both sites and verifying 2,277 products
against the live Amazon API:

- specpicks' price refresher approximates "user-visible" as
  `is_featured OR asin = ANY(article.related_product_asins)`. Of **974** ASINs
  actually reachable on the site, **464 matched neither** — they sit in the
  refresher's tier-3 tail, and **408** of those had prices over a week old or
  never set.
- **aisleprompt had no Amazon price verification at all.** 533 of 1,367
  surfaced products had drifted prices; worst case the site showed **$48.02**
  for a product Amazon sells at **$14.88**.
- The stale price also lands in the product page's JSON-LD `offer`, so it is
  what Google indexes, not just what a visitor sees.

A DB-side proxy for "user-visible" cannot find these. Crawling can.

## Checks

| check | how |
|---|---|
| price drift | site price vs Amazon price, >2% tolerance |
| image accuracy | perceptual hash, **both URLs normalized to one rendition** |
| availability | Amazon `in_stock` false while still on the shelf |
| hydration | missing title / brand |
| unbuyable | ASIN surfaced on site but Amazon won't return it |

### Two traps this encodes

**Image comparison by URL or image-ID does not work.** Amazon serves the same
product under several image IDs, and two renditions of the *same* id
(`._SL500_` vs `._AC_SX679_`) differ by 117 of 256 hash bits. Raw comparison
reported ~99% of images "wrong"; the measured rate after normalizing both
URLs to one rendition is **10.6% (aisleprompt) / 14.0% (specpicks)** on a
200-product sample per site. `shelf.normalize_image()` is not optional.

**Per-depth crawl budgets, not a global page cap.** specpicks' homepage alone
links 408 pages, so a 400-page global budget was spent entirely at depth 1 and
depths 2–3 were never visited. `per_depth: [null, 400, 500, 500]`.

## No producer-history dedup — on purpose

This is a **monitor**, not a proposer. Its rec titles
("N shelf products: image mismatch") have a constant signature run to run, so
`filter_proposals_against_history()` suppressed 5 of 6 buckets on the second
run — 257 real findings collapsed to 1 rec. Recurring findings must re-surface
every run. Do not add that filter back.

## Config (per site)

`SHELF_AUDIT_CONFIG=<site>/agents/shelf-audit-agent/site.yaml`

| key | meaning |
|---|---|
| `origin` / `max_depth` / `per_depth` | crawl scope |
| `api_endpoints` | JSON shelves for client-rendered sites (**required** for aisleprompt — crawling 982 of its pages yields zero ASINs in HTML) |
| `dsn_env` | env var holding the DSN |
| `shelf_query` | returns `asin, slug, title, brand, db_price_cents, db_image, price_updated_at`; bound with `(asins)` or `(asins, slugs)` by placeholder count |
| `max_verify_per_run` | Amazon call budget; **stalest-priced first**, so consecutive runs sweep the whole shelf |
| `check_images` | set false to skip image fetching (cheaper) |

## Schedule

`specpicks` 25 */8, `aisleprompt` 55 */8 — offset so they don't contend for
the Amazon API.
