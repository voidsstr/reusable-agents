# Shelf Audit Agent (`shelf-audit-agent`, shared engine)

> Checks **what the site shows** against **what Amazon says**. It crawls each
> site the way a visitor would, collects the products that can be reached from
> the homepage, and verifies each one against the Amazon Creators API.
> North Star: correct prices and images on the pages people and Google actually
> see, which protects Amazon conversions. A stale price also ends up in the
> page's JSON-LD `offer`, so Google indexes the wrong price.

Every other catalog agent picks its targets from the database. This one picks
them from the *rendered site*: the homepage plus 3 levels of links, and the
JSON endpoints behind client-rendered shelves. Products that are neither
featured nor linked from an article no longer slip through.

## At a glance

| | |
|---|---|
| Engine id | `shelf-audit-agent` (class fallback; each unit sets `AGENT_ID` to the per-site id) |
| Code | `reusable-agents: agents/shelf-audit-agent/` (`agent.py` = AgentBase subclass, `shelf.py` = crawl/compare primitives, `requirements.txt` = PyYAML, psycopg2-binary, Pillow) |
| Kind | AgentBase Python engine. This dir holds the engine only: its `manifest.json` has `enabled: false` and no `entry_command`/`cron_expr`. It is **not registered** and has no timer. |
| Per-site instances (live) | `specpicks-shelf-audit-agent` (`specpicks: agents/shelf-audit-agent/`, cron `25 */8 * * *`), `aisleprompt-shelf-audit-agent` (`aisleprompt: agents/shelf-audit-agent/`, cron `55 */8 * * *`). Both are `America/New_York` in the manifest. The systemd `OnCalendar` values are `0/8:25` and `0/8:55` in host-local time (America/Detroit), and both timers are enabled. The two start times are staggered so the instances do not hit the Amazon API at the same moment. |
| Entry command (instances) | `SHELF_AUDIT_CONFIG=<site>/agents/shelf-audit-agent/site.yaml PYTHONPATH=<reusable-agents> python3 <reusable-agents>/agents/shelf-audit-agent/agent.py`, run through `framework/agent_run_wrapper.sh` |
| Category | `research` |
| Runbook | This file is the engine runbook (manifest `runbook: README.md`). The per-site `README.md` files cover only instance values. |
| History | Added 2026-08-26 (`9406cf2`). The dispatch gate was added the same day (`76df0dd`). specpicks turned dispatch on 2026-08-30 (`specpicks: 295350e`). |

## Why this exists

These numbers come from crawling both sites on 2026-08-26 and checking 2,277
products against the live Amazon API:

- specpicks' price refresher (`specpicks-amazon-price-verifier`) treats a
  product as "user-visible" when `is_featured OR asin = ANY(article.related_product_asins)`.
  Of the **974** ASINs that could actually be reached on the site, **464 matched
  neither condition**. They sat in the refresher's tier-3 tail, and **408** of
  them had prices more than a week old or no price at all.
- **aisleprompt had no Amazon price verification at all.** 533 of 1,367 surfaced
  products had drifted prices. The worst case showed **$48.02** for a product
  Amazon sold at **$14.88**.
- The stale price also lands in the product page's JSON-LD `offer`, so Google
  indexes it too, not just visitors.

A database-side guess at what is "user-visible" cannot find these. Crawling can.

## What it does (`ShelfAuditAgent.run()`)

1. **Load config.** `SHELF_AUDIT_CONFIG` must point at the site's `site.yaml`.
   Without it the agent raises `SystemExit`.
2. **Discover the shelf** (`_discover`).
   - `shelf.crawl()` walks the site breadth-first from `origin` to `max_depth`
     with a separate page budget per depth (`per_depth`) and 16 workers. It
     follows only same-host links (query strings dropped) and collects ASINs from
     any `/dp/<ASIN>` pattern in the HTML. `seeds` from the config, if given,
     go into the queue at depth 1.
   - `shelf.crawl_api_surface()` fetches each URL in `api_endpoints` and keeps
     every `products[]`/`items[]` entry that has a `slug`, keyed by slug.
   - Each part is logged as an `observation` decision.
   - If neither the crawl nor the API returned anything, the run ends with
     **`status=failure`**: "shelf discovery found no products — crawl or
     api_endpoints config is wrong".
3. **Load DB rows** (`_shelf_rows`). The agent runs `shelf_query` against the
   DSN in the env var named by `dsn_env`. Parameters are bound according to how
   many `%s` placeholders the query has: `(asins)` or `(asins, slugs)`. Only
   rows that have an `asin` become targets.
4. **Cap the batch.** If there are more targets than `max_verify_per_run`
   (default 400), they are sorted by `price_updated_at` (oldest first) and cut to
   the cap. Successive runs therefore sweep the whole shelf.
5. **Look up Amazon** (`_amazon_lookup`). The agent calls
   `framework.core.amazon_creators.AmazonCreatorsClient.get_items` in batches of
   10 ASINs, sleeping `SHELF_AUDIT_SLEEP` seconds (default 1.0) between batches.
   - Each batch gets up to 4 attempts. `CreatorsThrottled` sleeps with
     exponential backoff; other errors sleep and retry, and on the 4th failure
     every ASIN in the batch is recorded as `api:<error>`.
   - An ASIN that Amazon reports as an error, that fails to parse, or that is
     simply not returned is recorded in `amazon_errors` rather than dropped.
     Exception: a batch that is throttled on all 4 attempts is skipped without
     being recorded anywhere.
6. **Compare** (`shelf.compare_row`), for each target Amazon returned:

   | Issue tag | Rule | Severity |
   |---|---|---|
   | `price_drift` | \|site − Amazon\| / Amazon > 2% (`PRICE_TOLERANCE`) | high |
   | `image_mismatch` | Both image URLs are normalized to the `._AC_SX679_.jpg` rendition, then compared with a 16×16 average hash. Flagged when the Hamming distance is > 12 of 256 bits (`IMAGE_THRESHOLD`). Only runs when `check_images` is on and both URLs exist. | high |
   | `missing_title` | DB title is empty | high |
   | `out_of_stock_on_amazon` | Amazon `in_stock` is `False` | medium |
   | `site_no_price` | Amazon has a price, DB does not | medium |
   | `image_missing` | DB image is empty | medium |
   | `amazon_no_price` | Amazon returned no price | low |
   | `missing_brand` | DB brand is empty | low |
   | *(unreachable)* | ASIN is in `amazon_errors` | medium |

7. **Build recs** (`_to_recs`).
   - Findings are grouped into one rec per issue tag, largest group first. Each
     rec has id `shelf-<run_ts>-<kind>`, title `"N shelf products: <kind>"`,
     `severity`, `priority`, `tier`, a rationale with 5 example ASINs,
     `expected_impact`, and `affected_asins` (at most 200).
   - `tier` is `lever` for high-severity kinds (`price_drift`, `image_mismatch`,
     `missing_title`). Everything else is `smaller`.
   - A separate rec, `shelf-<run_ts>-unreachable`, lists ASINs Amazon would not
     return. These are candidates for deactivation.
8. **Dispatch, optional** (`_maybe_dispatch`). This step is off unless the
   site's `dispatch_findings` is `true`. When on, it takes recs whose `tier` is
   in `dispatch_tiers` (default `lever`), keeps at most `max_dispatch` of them
   (default 3), and calls `framework.core.dispatch.gated_dispatch_now(...,
   action="implement", subject_tag="shelf-audit")`. The implementer is started
   right away as a transient systemd unit, `agent-dispatch-implementer-<site>-<ts>`.
9. **Save artifacts and return** a `RunResult`. See Outputs below.

## Inputs

| Source | Detail |
|---|---|
| Live site HTML | `origin`, crawled to `max_depth` (both sites use 3) with `per_depth` budgets. The User-Agent is `Mozilla/5.0 (compatible; shelf-audit-agent/1.0; +https://specpicks.com)` for both sites. Only `text/html` or JSON responses are read. |
| JSON shelf endpoints | `api_endpoints`. Required for client-rendered shelves: on aisleprompt, crawling 982 pages found zero ASINs in the HTML when this was built (2026-08-26); the `20260923T125509Z` run found 14 in HTML versus 1,429 products from the API. |
| Production Postgres | `shelf_query` (per site), using the DSN from the env var named by `dsn_env` |
| Amazon Creators API | `framework.core.amazon_creators` (`CreatorsConfig.from_env()`). Calls are charged to the `shelf-audit` line of the shared daily budget (`config/amazon-creators-budget.json`, ledger under `config/amazon-creators-usage/<date>/`) |
| Host secrets | `~/.reusable-agents/secrets.env`, loaded through the unit's `EnvironmentFile=` |

## Outputs

| Output | Where |
|---|---|
| `recommendations.json` | Storage key `agents/<agent_id>/runs/<run_ts>/recommendations.json` (`schema_version`, `site`, `agent_id`, `run_ts`, `summary`, `recommendations[]`). A copy is also written to disk at `~/.reusable-agents/<agent_id>/runs/<run_ts>/`. |
| `shelf-findings.json` | Same two locations. Holds `crawled_pages`, `surfaced_asins`, `api_products`, `verified`, `amazon_errors`, and per-product `findings[]`. |
| Implementer dispatch | Only when `dispatch_findings: true`. The run-dir copy is saved under `/tmp/reusable-agents-logs/dispatch-rundirs/` and the implementer log at `/tmp/reusable-agents-logs/dispatch-implementer-<site>-<ts>.log`. Shelf-audit agents are **not** in `backlog-dispatcher-agent`'s `PRODUCER_AGENT_IDS`, so this in-run dispatch is the only way their findings reach the implementer. |
| DB writes | **None by the agent itself.** Fixes to catalog data happen only through the implementer when dispatch is on. |
| Email | The agent sends nothing itself. It does not turn off AgentBase's default run summary (`send_run_summary_email`), so a successful run's summary is queued to the daily digest and a failed run's summary is sent immediately. The addressee is the manifest `owner`, which for both instances is the site name (`specpicks` / `aisleprompt`), not an email address; where that mail ends up is unverified. |
| `RunResult.status` | `success` whenever discovery found products, even when every product has issues. `failure` when discovery found nothing. Uncaught exceptions are recorded as failure by AgentBase. |

## Goals & metrics

- **No goals are declared.** On 2026-09-23, `GET /api/agents/<id>/goals`
  returned `goals: []` for both instances. CLAUDE.md requires 3–7 goals, so this
  is an open gap. A natural `target_metric` would be `products_with_issues`
  (direction: decrease).
- `RunResult.metrics` keys emitted on every successful run: `pages_crawled`,
  `products_verified` (ASINs Amazon returned), `products_with_issues`,
  `amazon_unreachable`.

## Configuration

**Per-site `site.yaml`** (`SHELF_AUDIT_CONFIG`). This file is not checked
against a schema.

| Key | Default in code | Meaning |
|---|---|---|
| `site_id` | `""` | Written to recs and passed to dispatch as `site` |
| `origin` | required | Crawl root |
| `max_depth` | 3 | Crawl depth |
| `per_depth` | `[null, 400, 500, 500]` | Page budget for each depth; `null` means unlimited |
| `seeds` | `[]` | Extra URLs queued at depth 1 |
| `api_endpoints` | `[]` | JSON shelf URLs (see Inputs) |
| `dsn_env` | required | Name of the env var that holds the DSN |
| `shelf_query` | required | Must return `asin, slug, title, brand, db_price_cents, db_image, price_updated_at` |
| `max_verify_per_run` | 400 | Maximum ASINs sent to Amazon per run, stalest-priced first |
| `check_images` | true | `false` skips image fetching and hashing (cheaper). `image_missing` is still reported. |
| `dispatch_findings` | false | Master switch for sending findings to the implementer. Default is off because the fix rewrites **live catalog data**, and observed drifts range from −95% to +279%. |
| `dispatch_tiers` | `lever` | Comma-separated rec tiers that may be dispatched |
| `max_dispatch` | 3 | Maximum **recs** (issue buckets) dispatched per run, not products |

**Environment:**

| Variable | Default | Meaning |
|---|---|---|
| `SHELF_AUDIT_CONFIG` | none (required) | Path to `site.yaml` |
| `AGENT_ID` | `shelf-audit-agent` | Set per site by the systemd unit |
| `REUSABLE_AGENTS_REPO` | `/home/voidsstr/development/reusable-agents` | Framework import path |
| `SHELF_AUDIT_SLEEP` | `1.0` | Seconds to sleep between 10-ASIN Amazon batches |
| `AMAZON_CREATORS_CLIENT_ID`, `AMAZON_CREATORS_CLIENT_SECRET`, `AMAZON_CREATORS_PARTNER_TAG`, `AMAZON_CREATORS_MARKETPLACE` | none | Amazon Creators API credentials, read by `CreatorsConfig.from_env()` |
| `DATABASE_URL_SPECPICKS` / `DATABASE_URL_AISLEPROMPT` | none | The env var named by `dsn_env` (from `secrets.env`) |
| `STORAGE_BACKEND`, `AZURE_STORAGE_*` | set by the unit | Where artifacts are stored |

> **Caveat: the "second gate" is not wired.** The site.yaml comments and the
> `_maybe_dispatch` docstring say `gated_dispatch_now()` "still honours
> `auto_implement` as a second approval gate". In practice `_maybe_dispatch`
> calls it **without** `cfg=`, so `auto_implement` is treated as `True`. Setting
> `auto_implement: false` in a shelf-audit `site.yaml` has no effect.
> `dispatch_findings` is the only switch that actually works.

## No producer-history dedup, on purpose

This agent is a **monitor**, not a proposer. Its rec titles
("N shelf products: image mismatch") stay the same from run to run. When
`filter_proposals_against_history()` was applied, it suppressed 5 of 6 buckets
on the second run, collapsing 257 real findings into 1 rec. Recurring findings
must show up again every run. **Do not add that filter back.**

One thing to know about this: `AgentBase.post_run()` also runs a framework-wide
title dedup pass (added 2026-05-15). It marks recurring bucket titles
`duplicate: true` in the *stored* `recommendations.json`. On 2026-09-23 all 6
recs in aisleprompt's `20260923T125509Z` run were flagged this way. Dispatch is
not affected, because `_maybe_dispatch` runs inside `run()` before `post_run`.
On specpicks' `20260923T122500Z` run the stored file carried no `duplicate`
flags and the two dispatched recs were stamped `shipped`. The likely reason is
that the implementer wrote its own copy back afterwards (not verified).
Dashboard rec counts for aisleprompt may still show these recs as duplicates.

## Two traps the code guards against

**Comparing images by URL or image ID does not work.** Amazon serves the same
product under several image IDs. Two renditions of the *same* ID (`._SL500_`
vs `._AC_SX679_`) differ by 117 of 256 hash bits. Raw comparison reported about
99% of images as "wrong". After normalizing both URLs to one rendition, the
measured mismatch rate on a 200-product sample per site was **10.6%
(aisleprompt) / 14.0% (specpicks)**. `shelf.normalize_image()` is not optional.

**Budget pages per depth, not with one global cap.** specpicks' homepage alone
links 408 pages. A 400-page global budget was used up entirely at depth 1, and
depths 2–3 were never visited. Hence `per_depth: [null, 400, 500, 500]`.

## Short-circuit & idempotency

- `signals()` returns the constant `{"ready": True}`. It currently does
  nothing, because `run()` returns no `next_state`, so the signal hash is never
  saved. On 2026-09-23 the `state` in `agents/<id>/state/latest.json` was `{}`
  for both instances, and every tick runs in full.
  **Do not "fix" this by saving `next_state`.** With a constant signal, every
  run after the first would short-circuit and the monitor would stop running.
  Give `signals()` a real input first (for example, the shelf ASIN set), or
  remove it.
- Without dispatch, re-running is safe because the agent only reads. The
  stalest-first cap means consecutive runs cover different products.

## Running & inspecting

```bash
systemctl --user start agent-specpicks-shelf-audit-agent.service      # one run now
systemctl --user list-timers | grep shelf-audit
tail -f /tmp/reusable-agents-logs/agent-specpicks-shelf-audit-agent.log
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/specpicks-shelf-audit-agent        # token lives in ~/.reusable-agents/secrets.env
ls ~/.reusable-agents/specpicks-shelf-audit-agent/runs/                 # local artifact copies
```

There is no dry-run flag. To run with dispatch off, set `dispatch_findings:
false` in the site's yaml.

Typical run time on 2026-09-23 was about 4–10 minutes per instance. Most of
that is the crawl.

## Failure modes & troubleshooting

| Symptom | Cause / action |
|---|---|
| `failure` about 1 s after start: "shelf discovery found no products" | `shelf.fetch()` swallows every exception and returns `""`, so an unreachable or non-HTML homepage (and failed API endpoints) produces an empty crawl. specpicks hit this on its 00:25 local-time tick on 2026-09-10, 11, 12, 15, 17, 18 and 21. The other ticks those days succeeded. **Root cause unverified.** Check whether the site was reachable at that time before changing the crawl config. |
| "0 products verified" and a large `unreachable` rec | The Amazon client could not be used. Check the `AMAZON_CREATORS_*` names in `secrets.env`. After 4 failed attempts, every ASIN in the batch is recorded as `api:<error>`. |
| aisleprompt reports no products | The `api_endpoints` list is stale (the category set changed), or the `/api/kitchen/products` response shape changed. The code expects `products[]` or `items[]` entries with a `slug`. |
| `image_mismatch` never fires | Pillow is missing (`ahash` returns `None`, so the verdict is "unknown"), or `check_images: false`. |
| Findings never get fixed | `dispatch_findings: false` (aisleprompt, as of 2026-09-23), or the implementer deferred them. Check `dispatch-implementer-<site>-*.log`. |

## Related agents

- **Instances:** `specpicks: agents/shelf-audit-agent/README.md`,
  `aisleprompt: agents/shelf-audit-agent/README.md`.
- **Downstream:** `implementer` (dispatched directly). On specpicks, its fixes
  reset hydration fields so that `specpicks-product-hydration-agent` fetches the
  data again, for example `specpicks: bdb346a` on 2026-09-23.
- **Siblings (DB-driven):** `specpicks-amazon-price-verifier` (tiered price
  refresh), `*-catalog-audit-agent`, `*-product-hydration-agent`.
- **Framework primitives used:** `framework/core/amazon_creators.py`,
  `framework/core/dispatch.py` (`gated_dispatch_now` / `dispatch_now`).
