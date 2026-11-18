# Amazon Price Refresh — Runbook (engine)

> Keeps Amazon prices, availability and display data fresh on the pages
> buyers and AI assistants actually use, for any **cache-model** site, through
> the official Creators API only. Writes each fresh answer into the site's
> Amazon cache table, never blanks data on a failed run, and NULLs every
> Amazon-sourced value before any of it has been stored for 24 hours.
> North-star link: a page with a fresh, quotable price shows an Offer and
> converts; a page without one does not.

## At a glance

| | |
|---|---|
| Engine id | `amazon-price-refresh-agent` (manifest `enabled: false`, no cron: never scheduled itself) |
| Home | `reusable-agents/agents/amazon-price-refresh-agent/` |
| Kind | AgentBase python, a thin wrapper over `framework.core.price_refresh_priority` (what to re-check, in what order) and `framework.core.amazon_price_refresh.refresh_prices()` (getItems ≤10 ASINs a call, budget, classification) |
| Instances | Config-only dirs in site repos: `<site repo>/agents/price-refresh/{manifest.json, site.yaml, AGENT.md}`, id `<site>-price-refresh` |
| Config | `$PRICE_REFRESH_CONFIG` = `<site.yaml>[#dotted.key]`, block `price_refresh` (schema: `price-refresh-config.schema.json`, template: `site.example.yaml`) |
| Creators budget line | the instance's own consumer (default `<site>-price-refresh`) in `config/amazon-creators-budget.json` |
| Writes | the site DB's cache table (default `amazon_item_cache`) only. Never `products`, never deactivates anything, never a price-history table |
| Status | Built 2026-11-17 (F11). Not registered: per-site instances are registered at the launch-agent step (A1) |

## What a run does

1. **Load the config** (`price_refresh_settings.py`): the site's block over the
   engine defaults, validated by the JSON schema (unknown keys fail) and by
   semantic checks (SQL identifiers, no bare `%` in SQL that runs with named
   params, a required `schedule.cron_period_hours`, a purge horizon that keeps
   stored data under 24h, a partner tag, an explicit resource list that covers
   the enabled cache columns, the demand module's own config problems). The
   site and DSN env var fall back to `framework.core.site_registry`. An
   invalid config fails in about a second with every problem listed and no
   DB or API traffic.
2. **Lock**: one run per instance at a time (`framework.core.locks.FileLock`);
   a run that finds the lock held exits as a no-op.
3. **Size the run**: `min(creators.max_calls_per_run, the consumer's remaining
   daily share)` calls × 10 ASINs.
4. **Rank** due ASINs with `price_refresh_priority` over the site's catalog
   view (`demand_priority.catalog.table`, e.g. `v_refresh_catalog` =
   `products LEFT JOIN amazon_item_cache`; `catalog.fresh_cols` defaults to the
   cache's `fetched_at`): every demand group in order (verified-human clicks,
   assistant fetches, pick ASINs, ...) past its gate, then the long tail past
   its gate; never-checked first, then oldest first. A failing signal read
   empties only its group. The **demand-ranking off switch**
   (`config/price-refresh-priority-config.json` `by_agent_id.<id>.enabled=false`)
   falls back to plain oldest-first over the same view; it does **not** stop
   the refresh or the purge. The ranking's read transaction is then closed,
   so the session is never idle in a transaction across a Creators call (the
   LWA token alone can take minutes), and a connection that died meanwhile
   fails the run before any call is spent.
5. **Sweep** with `refresh_prices()` through a raw-capturing client proxy (the
   cache needs the title and image of the same answer the classifier saw).
   Each answered batch is upserted at once: one statement, one commit,
   deadlocks and serialization failures retried. A batch that fails on a
   value (an encoding, numeric or constraint error that only some answers
   carry) is re-written row by row: the rows that still fail are skipped and
   counted (`poison_rows`, with a warning naming the ASINs) and keep their
   previous values, so one bad answer never stops the sweep or the purge. A
   batch whose every row fails is the table's fault: the sweep fails.
   `not_found` answers, which NULL a whole row, are held until the sweep has
   finished.
6. **If the sweep succeeded** (Creators answered at least one call, nothing
   raised, no abort, no persistent 429): write the held `not_found` rows unless
   the not_found guard trips, then **purge**: NULL every API column of every
   row whose answer is older than the purge horizon, including rows the
   catalog no longer lists, **except the rows this run asked about and could
   not refresh** (its failed batches, ASINs the answer left out, answers that
   could not be stored) while they are still inside the quotable window: for
   those rows the run failed, and a failed run never blanks what a visitor can
   still see. The next run re-asks them (they are due and oldest). The purge
   returns every key it NULLed with its age, so the run counts exactly what it
   blanked. Otherwise (the sweep failed) nothing more is written and the purge
   waits for the next successful run.
7. **Metrics** (read-only; a failing read never fails the run): fresh-priced
   share of indexable pages inside the quotable window, per-tier freshness,
   the backlog still due, due catalog rows the purge NULLed while still
   displayable, rows still holding Amazon data past the purge horizon (other
   than the spared ones) and past 24h, today's Creators usage against the
   share.
8. **Status**: `failure` when ASINs were due and Creators answered no call
   (every batch rejected, or a 429 from the first call), when the sweep or a
   write failed, when the not_found guard tripped, or when the purge was
   skipped while rows already hold Amazon data past 24h. A partial run that
   answered some batches and then stopped early or was throttled is a
   `success` with `purge_skipped=1`.

### Outcomes and what is written

| Outcome (`refresh_prices`) | Cache row after the run |
|---|---|
| `priced` | every enabled API column from this answer: price, list price, `USD`, availability (+type), condition, buy-box flag, `map_hidden=false`, title, image URL + dims, detailPageURL (+ browse node ids, product group where the site opted in); `outcome='priced'`, `fetched_at=NOW()`, `consumer` |
| `unavailable` (out of stock) | same, but price, list price and currency NULL; Amazon's availability text and type kept |
| `no_offer` (no New offer; MAP-hidden) | same, price NULL, availability text, availability type and buy-box flag NULL (a used listing's "Only 2 left" / `IN_STOCK_SCARCE` / buy box must not sit by a NULL price); condition kept (it says why). MAP-hidden (`map_hidden=true`: Amazon withholds a New buy-box price under MAP) keeps the type and the buy-box flag, because that item is buyable |
| `non_usd` | same, price NULL, currency = the foreign currency; availability text, type and buy-box flag NULL |
| `not_found` (per-ASIN error) | every API column NULL, `outcome='not_found'`, `fetched_at=NOW()`; written only after a clean sweep and only if the not_found guard holds |
| asked but not refreshed: `missing` (silently omitted), `error` (failed batch), an answer whose values cannot be stored (`poison_rows`) | **nothing**: the row keeps its previous values. This run's purge **spares** it while it is still inside the quotable window (`spared_rows`); the next run re-asks it, and its purge removes it if it is still unrefreshed by then (it is past the window). One already past the window is purged now (`purged_unrefreshed_rows`; nobody sees it) |
| untried (the budget ran out, an abort, a throttle) | **nothing**: the row keeps its previous values until a successful run's purge finds it past the horizon. A due row the budget never reached and that is still displayable when purged is counted in `blanked_due_rows` (the gate check keeps one run between falling due and the horizon, so this means the budget is short) |

A row's API columns always come from the single answer stamped in
`fetched_at`, so the row's age is the age of every value in it. That is what
makes the purge exact. **Display must key on `outcome`** and the quotable
window, never on `availability_type` or `is_buybox_winner` alone.

## Binding rules (do not relax)

1. **A failed or partial run never blanks data.** Nothing is written for an
   ASIN Amazon did not answer. A Creators failure before the first answer
   (the 2026-11-11 LWA token timeout that left ~3.8k pages of the reference
   site unpriced for ~6h) writes nothing at all, and neither does a run whose
   every getItems call was rejected. A run that answered some batches and not
   others is a failed run for the rows it could not refresh: its purge spares
   them while they are still inside the quotable window (rule 2). Metric
   `blanked_on_failed_run` counts API values NULLed during an unsuccessful
   sweep plus unrefreshed rows NULLed while still displayable, read from the
   ages the purge itself returns, and must stay 0.
2. **The purge runs only after a successful run** and NULLs EVERY API column
   older than the **purge horizon**: title, image URL and dims,
   detailPageURL, price, list price, currency, availability and its type,
   condition, buy-box flag, MAP-hidden flag (and browse nodes and product
   group where a site opted in), plus `cache.extra_purge_columns`. Only the
   ASIN and our own `outcome`, `fetched_at` and `consumer` stay. A
   budget-capped or budget-spent run is a successful run (normal operation):
   rows past the horizon are purged. **The spare:** the rows the run asked
   about and could not refresh (failed batch, omitted ASIN, unstorable
   answer) are left alone while their answer is inside the quotable window;
   the next run re-asks them and purges them if they are still unrefreshed.
   Like amendment 10's failed run, a spared row can stay stored past 24h
   until a run purges it: normally the next one (~24.5h at worst on the 4h
   cron); a row the next run again cannot refresh while still inside the
   window is spared once more.
3. **The horizon keeps storage under 24h, not the threshold.** The Amazon
   license caches non-image content for at most 24h, and the purge runs once
   per run: a row just under the horizon at one purge is removed only at the
   next one, a cron period later and possibly a whole run later within that
   run. So `cache.purge.max_age_hours: auto` is 24h minus
   `schedule.cron_period_hours` minus `schedule.max_run_minutes`: **19.5h on
   the 4-hourly cron** with 30-minute runs, and the oldest any unspared value
   can get is 24h. A larger explicit value fails validation (24h on a 4h cron
   would store data up to ~28.5h). The cost: a row nothing re-checks before
   the horizon (~19.5h) is blanked then, while it could still have been shown
   until ~23.8h. That is why every gate must fall due at least one run before
   the horizon (rule 7): then a row the budget misses when it falls due is
   re-asked by the next run first (oldest first) before the purge reaches it.
   A run whose purge comes later than `max_run_minutes` after its start logs
   a warning.
4. **The not_found guard.** When more than `not_found_guard.max_share` (0.5)
   of at least `min_answered` (20) answers in a run are `not_found`, that is an
   integration fault (tag, account, API), not a catalog event: the not_found
   rows are not written, the purge is skipped and the run fails so it is
   escalated.
5. **The site's own partner tag** (`creators.partner_tag` or
   `creators.partner_tag_env`) is required: requests and the stored
   detailPageURL must never carry another site's tag.
6. **No price history** (Associates Participation 6(y)): no history table, no
   "lowest ever", no alerts. **No deactivation**: the engine never touches
   `products`. **Browse nodes and product group stay out of the cache** unless
   a site opts in (license review: derive the category once into your own
   table; product group is commission analytics, not advertising).
7. **Gates re-check inside the quotable window, and a run before the purge
   horizon** (amendments 2 and 10). The window is
   `demand_priority.display_hours` minus `metrics.max_html_cache_age_s` (the
   kit's `maxHtmlCacheAgeSeconds`; 24h − 660 s ≈ 23.82h at its defaults). A
   row is re-checked at up to `(floor(gate / P) + 1) × P` hours (rows are
   answered minutes after a tick: the off-by-minutes effect the reference
   verifier measured) plus one run, plus `P` per failed run. Two checks:
   - **Quotable window**: that age, after the tolerated failed runs, must stay
     inside the window. On the 4-hourly cron the 12h demand groups and the
     15.5h long tail re-check at ~16.5h, and at ~20.5h after one failed run
     (`tolerate_failed_runs: 1`, `long_tail_tolerate_failed_runs: 1`).
   - **Purge slack**: the first re-check (no failed run) must land at or
     before the purge horizon, or a row that run cannot answer (the budget
     runs out) is NULLed by its own purge while still displayable. **A 16h
     long tail on the 4h cron fails this**: first re-check ~20.5h against the
     19.5h horizon (and ~24.5h after one failed run). Any gate at least a run
     under 16h passes: **15.5h re-checks at ~16h**, giving a run of slack, at
     about 25% more long-tail calls than ~20h re-checks; so does a 2h cron
     (horizon 21.5h). The draft's 22h long tail re-checks at ~24.5h with no
     failure at all, and its rows would be purged before they ever fall due.
   Violations are logged as warnings (each names the fix) and counted in
   `gate_buffer_violations`.

   **Operator decision (open):** amendment 10 binds "16h on the 4-hourly
   cron". It sized the cadence as `ceil(gate / P) × P` = 16h; the refresh
   actually re-checks a 16h gate at ~20h, past the 19.5h horizon that the
   24h license limit forces on a 4h purge cadence. The template ships
   **15.5h**, the ~16h cadence the amendment intended, within the review's
   accepted "16-18h or less". The alternatives are a 2h cron, or 16h with
   first-due budget misses blanked (they are reported as `blanked_due_rows`,
   and the gate check warns every run). Confirm before the instance is
   registered.

## Config (the site's `price_refresh` block)

The full, validated template is [`site.example.yaml`](site.example.yaml).
Keys: `site`; `database.{dsn_env, statement_timeout_ms, connect_timeout_s,
write_retries, write_retry_backoff_s, application_name}`;
`creators.{consumer, partner_tag | partner_tag_env, marketplace, resources,
expected_currency, max_calls_per_run, max_consecutive_errors, min_interval_s}`;
`schedule.{cron_period_hours (required), max_run_minutes, tolerate_failed_runs,
long_tail_tolerate_failed_runs}`; `demand_priority` (the
`price_refresh_priority` site config, passed verbatim: `catalog`, `sources`,
`groups`, `long_tail`, `display_hours`); `cache.{table, key_col,
fetched_at_col, outcome_col, consumer_col, columns, casts,
extra_purge_columns, max_text_chars, purge.max_age_hours ('auto'),
not_found_guard.{max_share, min_answered}}`; `metrics.{indexable_sql,
min_quotable_price, max_html_cache_age_s, tier_freshness}`.

`creators.resources: auto` requests the classifier's `PRICE_RESOURCES` plus
what the enabled columns need (`itemInfo.title`, `images.primary.large`; plus
`browseNodeInfo.browseNodes` / `itemInfo.classifications` only for a site
that maps `browse_node_ids` / `product_group` to a column, and getItems'
acceptance of the browse-node resource is still unverified). An explicit list
must cover the enabled columns. A column mapped to `null` is neither
requested, written nor purged.

### The cache table and catalog view the engine expects (site migration)

Every API column must be NULLable (the purge writes NULL). Column names are
configurable through `cache.columns`; the defaults are:

```sql
CREATE TABLE amazon_item_cache (
  asin text PRIMARY KEY,                       -- our key (unique)
  title text, image_url text, image_w integer, image_h integer,
  detail_page_url text,
  price numeric(12,2), list_price numeric(12,2), currency text,
  availability text, availability_type text, condition text,
  is_buybox_winner boolean, map_hidden boolean,
  outcome text,                                -- ours: priced | unavailable | no_offer | not_found | non_usd
  fetched_at timestamptz,                      -- ours: when the answer arrived
  consumer text                                -- ours: the Creators budget line
);
CREATE INDEX ON amazon_item_cache (fetched_at); -- the purge and the stale-row metric filter on it
CREATE VIEW v_refresh_catalog AS               -- demand_priority.catalog.table
  SELECT p.asin, p.is_active, c.fetched_at, c.outcome
    FROM products p LEFT JOIN amazon_item_cache c ON c.asin = p.asin;
```

Opt-in only (map them in `cache.columns`, and they are purged like the rest):
`browse_node_ids text[]` (cast `cache.casts.browse_node_ids`, default
`text[]`) and `product_group text`. If another writer fills an Amazon-sourced
column of this table, list it in `cache.extra_purge_columns`.

Display reads should go through a view that exposes a row only while
`fetched_at` is within the quotable window (24h minus the longest HTML cache
age); the purge is the storage backstop, not the display gate.

## Metrics and goals

`goals.json` declares seven goals; seed them under the instance id. Their
`target_metric`s are keys of `RunResult.metrics`:

| Goal | Metric | Target |
|---|---|---|
| Indexable pages show a fresh price | `fresh_priced_pct` (0-100; outcome priced, ≥ $1, inside the quotable window, over `metrics.indexable_sql` or the catalog) | ≥ 90 |
| No Amazon price held past 24h | `stale_price_rows` (rows holding a price past the purge horizon after the purge step, spared rows excluded: 0 after every successful run) | 0 |
| Demand pages fresh | `demand_fresh_pct` (clicked / assistant / pick tiers, quotable window) | ≥ 98 |
| A failed or partial run never blanks | `blanked_on_failed_run` (values NULLed by an unsuccessful sweep, plus rows the run could not refresh that its purge NULLed while still displayable; from the purge's own RETURNING ages) | 0 |
| Due pages re-checked before the purge | `blanked_due_rows` (catalog rows the purge NULLed while still displayable: due rows no run answered before the horizon, e.g. the budget ran out) | 0 |
| Inside the Creators share | `creators_calls_vs_share` | ≤ 1.0 |
| Few throttles | `creators_throttled_today` (ledger, this consumer) | ≤ 50 |

Other metrics: `candidates`, `candidates_<tier>`, `budget_calls`,
`budget_left_at_start`, `api_calls`, `refreshed`, `unavailable`,
`out_of_stock`, `no_offer`, `not_found`, `non_usd`, `map_hidden`, `missing`,
`batch_errors`, `failed_batch_asins`, `unrefreshed_asins` (asked but not
refreshed: failed batches, omitted, unstorable), `throttled`, `aborted`,
`untried`, `written_rows`, `write_retries`, `poison_rows`,
`not_found_written`, `not_found_held`, `purged_rows`, `spared_rows`,
`purged_unrefreshed_rows` (unrefreshed rows already past the quotable
window, purged), `purge_skipped`, `purge_offset_s`, `purge_horizon_hours`,
`quotable_hours`, `fresh_checked_pct`, `indexable_pages`,
`fresh_priced_pages`, `fresh_pct_<tier>`, `due_backlog` and
`due_backlog_<tier>` (rows still due after the run, including those past the
run's ranking limit, which `untried` cannot see), `stale_api_rows`,
`api_rows_past_24h` (an actual breach of the 24h limit), `demand_ranking`,
`demand_members`, `demand_signal_errors`, `gate_buffer_violations`,
`creators_calls_today`, `creators_share_calls`,
`creators_calls_all_consumers_today`, `hours_since_last_purge`, `elapsed_s`.
State carried between runs: `last_purge_at`, `last_successful_sweep_at`.

The engine opts out of the input-hash short-circuit (`signals()` returns
None): prices drift on Amazon's side and the purge must run every successful
tick, so elapsed time is the input.

## Operator CLI (no run record)

```bash
cd /home/voidsstr/development/reusable-agents
A=agents/amazon-price-refresh-agent/agent.py
python3 $A --config    --site-yaml <site.yaml>         # resolved settings + demand config; no DB, no API
python3 $A --print-sql --site-yaml <site.yaml>         # the upsert / purge / metric SQL for review
python3 $A --plan      --site-yaml <site.yaml> --limit 50   # read-only DB session: budget, tiers, candidates
python3 $A --dry-run   --site-yaml <site.yaml> --asins B0..,B0..  # LIVE getItems (charged to the consumer), NO DB writes
```
`--plan` and `--dry-run` need the site DSN env var (and Creators credentials
for `--dry-run`) loaded: `set -a; . ~/.reusable-agents/secrets.env; set +a`.
`--dry-run` exits 1 when any getItems call failed (and prints the error), 0
otherwise. Every mode honours `--agent-id`.

## First-run checklist (before the instance is registered)

1. The site migration created the cache table and catalog view above
   (`--print-sql` shows the exact statements the engine will run).
2. `config/amazon-creators-budget.json` names the instance's consumer with a
   share (without one it gets `default_share`, 400 calls/day at the defaults).
3. The partner tag is the site's own and its env var (if used) is in
   `secrets.env`.
4. `--dry-run --asins <3 known ASINs>` exits 0: confirms the credential, the
   tag and that getItems accepts the resource set (an invalid resource name
   fails every batch with HTTP 400; the dry run then exits 1 with Amazon's
   message, and a live run would fail without writing anything).
5. `--config`: `purge_horizon_hours` and `worst_case_stored_age_hours` (≤ 24)
   match the instance's cron. `--plan`: the tiers fill, the gate check is
   clean (`gate_buffer` empty: in particular no gate whose first re-check
   lands past the purge horizon), the first candidates look right. Settle
   the long-tail gate decision in rule 7 first.
6. Register the instance; read its first `progress.json` (written rows, purge,
   fresh-priced share), not just a green systemd unit.

## When something breaks

| Symptom | Meaning | Action |
|---|---|---|
| `failure`, "Creators unavailable before any answer" | LWA token / network / auth failed (CreatorsUnavailable) | Nothing was written or purged; the next run retries. Persistent: check credentials, `AssociateNotEligible` 403 |
| `failure`, "Creators answered none of the getItems calls" | every batch was rejected (e.g. HTTP 400 on a resource name) or the first call got a persistent 429 | Nothing written or purged. Read the quoted error: fix `creators.resources` or the request; for 429s see the throttle goal |
| `failure`, "price-refresh config invalid" | schema / semantic problem | Fix the site.yaml; every problem is listed |
| `failure`, "not_found guard tripped" | most answers were per-ASIN errors | Check the partner tag and the account; do not raise the guard to make it pass |
| `failure`, "sweep FAILED (write: ...)" | a cache write failed for every row of a batch (constraint, type) or for a non-value reason | `--print-sql` vs the table definition; batches before it are kept |
| `poison_rows > 0` (warning names the ASINs) | those answers carry a value the table cannot store (overflow, a CHECK, encoding) | The rows keep their previous values (the purge spares them while still displayable) and are purged once a run finds them past the quotable window; widen the column or relax the CHECK if it recurs |
| `failure`, "the purge failed" | e.g. a NOT NULL API column | Make every API column NULLable |
| `failure`, "cache row(s) hold Amazon data older than 24h" | purges have been skipped (partial or throttled runs) long enough for rows to pass the license limit, or a spared row's next run was late or skipped its purge | Find why runs stop early (summary, `aborted`, `throttled`); the next clean run purges |
| `purge_skipped=1` on a success | the sweep stopped early or was throttled after answering some batches | Rows past the horizon wait for the next clean run; watch `stale_price_rows` and `api_rows_past_24h` |
| `spared_rows > 0` | the run asked about rows past the horizon and could not refresh them (failed batches, omitted ASINs, unstorable answers); their values were kept | Normal after a stray failed batch: the next run re-asks them. Recurring: read `batch_errors` and the last error, `missing`, `poison_rows` |
| `blanked_due_rows > 0` (warning names it) | due catalog rows reached the purge horizon with no run answering them: they lost their price, title and image while still displayable | `gate_buffer_violations` first (a gate with no run of slack, e.g. a 16h tail on a 4h cron); then `due_backlog` vs `budget_calls` × 10: raise the consumer's share or shrink the catalog |
| `blanked_on_failed_run > 0` (warning "an engine regression") | values were NULLed by a failed run, or rows the run could not refresh were NULLed while displayable | Stop the instance (registry `enabled=false` or its timer) and fix the gating or the spare |
| warning "the purge ran N min into the run" | runs take longer than `schedule.max_run_minutes` | Raise `max_run_minutes` (it lowers the purge horizon) or shorten the run (`max_calls_per_run`) |
| `gate_buffer_violations > 0` | a gate re-checks past the quotable window after its tolerated failed runs, first re-checks past the purge horizon (no run of slack), or falls due only after the horizon | Apply the fix the warning names: lower the gate (at least a run under the next multiple of the period, e.g. 15.5h on a 4h cron) or shorten the cron |
| `fresh_priced_pct` low, `fresh_checked_pct` high | pages are checked but unbuyable on Amazon | A catalog problem, not a refresh one |
| `budget_calls < max_calls_per_run` every run | the share is too small for the catalog | Raise the consumer's share (keep the shares summing to ≤ 1.0) |

To stop an instance (an incident), set `enabled=false` on its registry entry
(dashboard, or `PUT /api/agents/<id>`) or stop its systemd timer. The storage
setting `by_agent_id.<id>.enabled=false` only switches demand ranking off.

## Provenance

Lifted from the reference site's `amazon-price-verifier` (the first site's
Creators price refresh) as a config-driven engine, per the affiliate-site
build design (F11, amendments 2 and 10). Ported: the demand ranking (now
`price_refresh_priority` groups instead of a site CTE), the per-batch writes
that keep work on a mid-run failure, the deadlock / serialization retry
(`WRITE_RETRIES` 3, backoff 1s × attempt + jitter), the outcome semantics, the
"never deactivate" rule and the gate arithmetic. Not ported, on purpose: the
price-history table and "lowest price recorded" label (Participation 6(y)),
the quality-flag and reactivation logic of the products-table model, the
site-specific tier-0 arms and the Black Friday boost (a site expresses those
as demand groups), and the successor lookup (optional; a site's own
substitute logic covers it). The reference site keeps its own verifier until it
adopts this engine (a products-table write mode would be added then). Unlike
that verifier, a run Creators never answered is a failure here, not a success.
