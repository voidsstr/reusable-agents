# Amazon Catalog Discovery (`amazon-catalog-discovery-agent`)

> Grows an affiliate site's catalog from Amazon Creators `searchItems`, so the
> site has a product page for every buyable product in its lanes (North Star:
> indexed pages and Amazon conversions). It persists the ASIN and our own lane
> label, and nothing Amazon wrote.

## At a glance

| | |
|---|---|
| Engine id | `amazon-catalog-discovery-agent` (this directory; `enabled: false`, no cron: engines never run on their own) |
| Instances | config-only directories in the site repos, e.g. `<site repo>/agents/amazon-catalog-discovery/` with `manifest.json` + `site.yaml`; their `AGENT_ID` is `<site>-amazon-catalog-discovery` |
| Kind | AgentBase python, a thin wrapper on `framework.core.amazon_discovery.discover()` (+ `amazon_creators`, `site_registry`) |
| Config | `CATALOG_DISCOVERY_CONFIG` = path of a YAML/JSON file, or the `catalog_discovery:` block of a larger site.yaml. Schema: [`catalog-discovery-config.schema.json`](catalog-discovery-config.schema.json) |
| Worked examples | [`examples/home-power.site.yaml`](examples/home-power.site.yaml) (launch niche), [`examples/cleaning-robots.site.yaml`](examples/cleaning-robots.site.yaml) (fallback niche) |
| Writes | the site's catalog table via its `insert_sql` (ASIN + lane label only); framework storage: run report, decisions, state, the Creators usage ledger |
| Reads | Creators `searchItems`; the catalog via `known_sql`; seed files; optional read-only seed SELECTs on other databases |
| LLM | none |
| Build item | F12 (build design, section 5) |

## What one run does

1. **Config.** Load and validate the site's config (JSON Schema, then the
   semantic checks in `catalog_discovery/config.py`). An invalid config fails
   the run without touching the DB, the API or the state.
2. **Catalog connection.** DSN from `catalog.dsn_env`, else the site
   registry's `dsn_env` for `site` (`framework.core.site_registry`), else the
   registry's derived name `DATABASE_URL_<SITE>`. The value is never logged.
   Session `statement_timeout` from the config.
3. **SEEDS** (no Amazon calls). Every due seed source:
   * **files**: a JSON list of ASINs, list of records, ASIN -> lane map, or
     ASIN -> record map, at `json_path`, kept by `where` (every field must
     match) and dropped by `where_not` (any field matches);
   * **queries**: ONE read-only `SELECT asin[, lane][, phase][, title]` on
     another database. It runs as a subquery in keyset pages of `max_rows`
     (`asin > <cursor> ORDER BY asin`), so a large source continues on the
     next run instead of being cut, in a session with
     `default_transaction_read_only=on`.

   A source's own labels go through `lane_map` (e.g. a sizing run's
   `expansion_battery` -> `expansion`). Then the trust rules (see *Seed
   trust* below), the active phases, the source's `lanes` allow-list and the
   ASIN regex; the rest is checked against the catalog and new ones are
   inserted (cap `max_seed_asins_per_run`; a cut source continues next run).
   A file whose content and config are unchanged since its last complete
   import is skipped; a query source read to its end re-runs every
   `refresh_hours`.
4. **SEARCH.** The run's call allowance (see *Budget*), then slot by slot:
   * the rotation hands out the next (query, itemPage), breadth first;
   * `discover()` makes exactly that one call through `PinnedPageClient`,
     which pins the page and adds the query's filters (browse node, brand,
     sort, price band). `discover()` still applies the pricing-integrity gate
     (title, USD price >= $1, New offer, buyable or orderable), the new/known
     split against `known_sql`, the budget check and 429 handling;
   * every new listing is classified into the site's lanes (in memory);
     in-lane ones are inserted and committed page by page, so an interrupted
     run keeps what it found;
   * the rotation records what the page means for the query (see *Paging*).

   It stops at the allowance, at `max_new_per_run` (the cut page is fetched
   again next run), at the end of the cycle, when the consumer's daily budget
   is spent or Amazon throttles (the slot is retried next run), after three
   server errors in a row (`server-errors`; those slots are retried next
   run), or when the API is unavailable (run fails, work so far is kept).

   Listings the catalog already carries are re-screened with their fresh
   title: one that now hits a global reject pattern is reported
   (`known_now_rejected`, ASIN + rule id, and a warning decision). Discovery
   never changes an existing row; the operator removes or relabels it.
5. **REPORT.** Metrics (see *Goals*), decisions, the run report
   `agents/<id>/runs/<run_ts>/discovery-report.json`, and `next_state`.

## What is persisted, and what never is

Persisted: the ASIN (Amazon allows storing it indefinitely) and our own
values: lane, lane kind, lane category, our confidence, provenance (`via`,
`seed`), the run timestamp, the configured `site_id`. These are the only
names `insert_sql` may use (`config.INSERT_PARAMS`); validation rejects any
other placeholder, and the engine never binds an Amazon value.

Never persisted: titles, prices, list prices, images, URLs, brands,
availability, browse nodes, product groups. They are read in memory to
classify and then dropped. Decisions, the run report and the state carry
ASINs, lane ids and rule ids only (a test scans every stored file for a
title token). Prices and display data are the price-refresh engine's job,
in its 24-hour cache. `--show-titles` prints titles to the terminal during a
dry run only.

## Lanes

A lane is our product class, e.g. `station` or `consumable`. Lanes are tried
in config order, which is **precedence**:

1. A global `reject_patterns` hit rejects the listing (`global-reject`).
2. For each lane: positive evidence per `match`
   (`title`: every `include_all` and one `include_any`; `node`: the listing's
   browse nodes intersect `browse_nodes`; `title_and_node`; `title_or_node`),
   then `exclude`, `context_required`, `min_price` and `measures` (numbers
   read from the title, e.g. `capacity_wh` 250-5200). Failing any of these
   passes the listing on to the next lane, so a $15 solar extension cable
   still lands in a cable lane after the panel lane's price floor.
3. No lane: `off-lane`. A matched lane that is held back (inactive phase or
   `enabled: false`): `lane-held-back`. Held-back lanes keep their place in
   the precedence, so a phase-2 inverter is never filed as a cable.

Confidence (`%(confidence)s`): title and node agree 0.9, title only 0.7,
node only 0.6, seed label 0.8 (all configurable; a seed source's own
`confidence` overrides them for its rows).

**Safety boundaries are global rejects.** A rule that must hold for every
listing (a site's year-1 exclusions, e.g. 240 V / L14-30 / L5-30 / 14-50
cords) belongs in `reject_patterns`, never only in one lane's `exclude`:
precedence can file a listing in an earlier lane (a $160 cord whose title
says "power station") before a later lane's exclude is ever tried. Make
such rules negation-aware where a product states a safety feature
('anti-backfeed', 'no gas' on a station that states its Wh), and scope
plug / voltage rules to cords and adapters that state no Wh capacity. The
worked example does all three; its fixture
`fixtures/lane-cases.home-power.json` prices the cord cases at $150-200 so
the station lane's floors cannot hide a missing global rule.

**Measures.** A pattern's `dot_thousands` ('1.024' read as 1024) is set per
pattern, on the unscaled Wh pattern only: on the kWh pattern '3.072kWh'
is 3,072 Wh. The validator refuses `dot_thousands` on a scaled pattern, and
a measure-level `dot_thousands` applies to unscaled patterns only. The
classifier and the capacity reader are checked against a frozen copy of the
sizing run's `classify_lane` / `capacity_wh`
(`test_port_matches_the_reference_classifier`).

## Seed trust

The year-1 boundary applies to seeds too.

* A record **with a title** (a query's `title` column, a file's
  `title_field`) is screened first: a global reject or a held-back lane
  skips it. With `classify_titles` the lane rules pick its lane and a
  source label that disagrees skips it (`label-disputed`); without it the
  label (or `default_lane`) is kept unless the title hits that lane's own
  `exclude` (`lane-excluded`). The title is never stored.
* A record **without a title** cannot be screened, so its lane label is used
  only for lanes the source lists under `lanes`, or with
  `trust_labels: true`; otherwise it is skipped as `untrusted-label` (and a
  warning decision says so). `default_lane` is the operator's own assertion.
  A source can set its own `confidence`, e.g. below a site's 0.5 pick floor
  for labels nobody has re-screened.

The worked example's sizing import shows the pattern. The sizing file
(`sizing-power.json`, 2,573 records) holds 1,300 counted launch-lane ASINs
and 651 counted phase-2 ones. `sizing-launch` imports the 747 station / kit
/ expansion / panel labels (`lane_map: {expansion_battery: expansion}`,
`where: {counted: true}`, phase-2 held back by `active_phases`). It does not
import the 553 cable labels on trust, because the sizing classifier's cable
lane admitted L14-30, L5-30, 14-50 and NEMA cords (its 125
`ac-rv-generator` records), and there is no title to re-screen. The
opt-in `sizing-launch-dc-cables` source (`enabled: false`) adds the 370
DC-solar and car-charging cables at confidence 0.45. With every launch
lane trusted, the same file plans exactly 1,300 rows
(`test_sizing_shaped_seed_file_imports_the_counted_launch_lanes`; set
`CATALOG_DISCOVERY_SIZING_FILE` to run the same check on the real file).

## Paging and rotation (the W3 workaround)

`discover()` has three known defects, fixed centrally in W3 (after the
holiday freeze); until then this engine pages itself:

| Shared-module behaviour | What this engine does |
|---|---|
| A short page (6-9 items) mid-list ends the keyword (`amazon_discovery.py`, "a short page is the last page"); the sizing run saw it on 142 of 400 pages | pages on; only an **empty page** ends a query. `short_pages_continued` counts the pages the shared loop would have stopped at |
| Pages without an upper bound; Creators refuses `itemPage` above 10 | never requests past `creators.max_item_page`, which a config may lower but never raise above 10 (schema maximum, validator, and a clamp in the rotation) |
| Docstring says totalResultCount caps at 1,000; it clips at **306** | a count below 306 is exact: the query ends once `page x 10` reaches it; at 306 the length is unknown. A count above 100 (10 pages) marks the query **saturated**: split it with price bands, brands or browse nodes |
| No search filters reach `search_items` | `PinnedPageClient` adds browse node, brand, sort and the price band |
| A request error (any status) ends the keyword | a **4xx** ends the query for the cycle; a **5xx / 408** leaves the slot for the next run, and only the third failure of the same page ends the query (`server-error`) |

Order is breadth first: every query's page 1, then every page 2, config order
within a level. Progress per query key (`<lane>/<seed>[/brand=..][/price=..]`)
lives in the state, so runs resume exactly; a query that left the config is
pruned, a new one starts at page 1. When every query has ended the cycle is
complete; the next run starts again at page 1 once
`rotation.cycle_cooldown_hours` have passed since the cycle ended. A client
error ends a query for the cycle (`query_errors`; on page 1 the query itself
is bad, e.g. a filter Creators refuses).

**Low-yield stop** (`limits.stop_if_new_in_lane_below`, the sizing run's
`stop_if_new_inlane_lt`): a page at or past `limits.low_yield_from_page`
(default 2) that adds fewer new in-lane ASINs than the threshold ends its
query for the cycle (`low-yield`, counted in `low_yield_queries`). Off by
default; the worked example uses 2 from page 2, so a cycle over a mostly
known catalog does not walk every query to page 10.

Price bands use `minPrice`/`maxPrice` in **cents**, verified by the sizing
run on 2026-11-12 (1,005 of 1,016 priced items on banded pages fell inside
their band). The engine still counts listings outside their band
(`partition_price_misses`) and warns when most of them are, as a regression
guard. The `brand`, `browseNodeId` and `sortBy` filters remain unverified on
Creators: a refusal is a 4xx and ends that query for the cycle.

## Budget

`allowed calls = min(limits.max_calls_per_run, the consumer's remaining daily
share, day total left - protected reserve - keep-unused floor)`.
The protected reserve is what every `budget.protect_consumers` entry (the
price refreshes) may still spend under its share today. This is the local
stand-in for the reserved floor W3 adds to `amazon_creators`: today
`remaining_budget()` lets a small consumer spend day-total headroom that the
price refresh needs later in the UTC day. `discover()` also checks
`client.budget_ok()` before every call. Calls are charged to `consumer` in
`config/amazon-creators-usage/<day>/<consumer>.json`; its share belongs in
`config/amazon-creators-budget.json` (F15c).

## Goals & metrics

Declared in [`goals.json`](goals.json) (seed it under each instance's id):

| Goal | Metric (RunResult.metrics) | Target |
|---|---|---|
| goal-new-in-lane-asins | `new_in_lane_asins` | 40 per run, increase |
| goal-reject-precision | `reject_precision` | >= 0.95 |
| goal-in-lane-precision | `in_lane_precision` | >= 0.95 |
| goal-budget-adherence | `budget_overrun_calls` | 0 |
| goal-calls-per-new-asin | `calls_per_new_asin` | <= 0.5 |

Precision comes from operator labels (`qa.golden` / `qa.golden_file`:
ASIN -> lane id or `reject`, our labels only). Whenever a labelled ASIN shows
up in a search, the rules' verdict is recorded; the latest verdict per ASIN
is kept (window `qa.window`) and a precision is reported once
`qa.min_observations` decisions back it. Label from the run report's
`reject_samples` and `rows`.

Other metrics: `seed_asins_imported`, `seed_asins_skipped`,
`seed_source_errors`, `api_calls`, `pages_fetched`, `throttled`,
`request_errors`, `transient_errors`, `candidates`, `known_seen`,
`known_now_rejected`, `rejected_lane`, `rejected_global`,
`rejected_off_lane`, `rejected_held_back`, `rejected_pricing`, `cross_lane`,
`node_confirmed`, `deferred_max_new`, `insert_errors`,
`short_pages_continued`, `queries_ended`, `saturated_queries`,
`query_errors`, `low_yield_queries`, `cycle`, `cycle_progress`,
`budget_allowed_calls`, `partition_price_checked`, `partition_price_misses`,
`golden_observations`.

## State carried between runs

`agents/<id>/state/latest.json` -> `state`:
`rotation` (cycle, per-query `next_page` / `ended` / `end_reason` / `total` /
`saturated` / `retries`), `seed_sources` (per source: signature, config
fingerprint, complete, counts, and for a query source the keyset `cursor`
of an unfinished read), `golden` (latest verdict per labelled ASIN),
`config_fingerprint`, `last_run`. A failed run returns the progress it made,
so a crash never resets the rotation.

`signals()` returns None on purpose: the rotation advances every run, and a
run with nothing to do (disabled, cooldown, no allowance) returns in
milliseconds without an API call.

## Running and inspecting

```bash
cd /home/voidsstr/development/reusable-agents
# Config only (no DB, no API):
python3 agents/amazon-catalog-discovery-agent/agent.py --config <site.yaml> --validate
# Dry run: REAL searchItems calls (budget-charged), no DB writes, no state or run record:
set -a; . ~/.reusable-agents/secrets.env; set +a
python3 agents/amazon-catalog-discovery-agent/agent.py --config <site.yaml> --dry-run --max-calls 3 [--show-titles]
# Seed import only (a recorded run; the D1 path):
AGENT_ID=<site>-amazon-catalog-discovery python3 agents/amazon-catalog-discovery-agent/agent.py --config <site.yaml> --seeds-only
```

Per-site instance manifest (S12; registered at A1, never before):

```json
{
  "id": "<site>-amazon-catalog-discovery",
  "name": "<Site> Amazon Catalog Discovery",
  "description": "Grows <site>'s catalog from Creators searchItems over its lanes; ASIN + lane label only.",
  "category": "seo",
  "task_type": "scheduled-cron",
  "cron_expr": "20 3,15 * * *",
  "timezone": "America/Detroit",
  "enabled": true,
  "owner": "mperry@northernsoftwareconsulting.com",
  "runbook": "README.md",
  "entry_command": "CATALOG_DISCOVERY_CONFIG=/home/voidsstr/development/<site>/agents/amazon-catalog-discovery/site.yaml PYTHONPATH=/home/voidsstr/development/reusable-agents python3 /home/voidsstr/development/reusable-agents/agents/amazon-catalog-discovery-agent/agent.py",
  "metadata": {"framework": "reusable-agents", "code_repo": "reusable-agents",
               "code_path": "agents/amazon-catalog-discovery-agent", "site": "<site>"},
  "runnable_modes": ["cron", "manual"]
}
```

The catalog DSN env var (from `site.yaml` or the site registry) and the
`AMAZON_CREATORS_*` credentials come from `~/.reusable-agents/secrets.env`.

A seed query's DSN (a sister site's catalog) should be a **SELECT-only
role**. The engine adds three guards of its own: the SQL must be one
statement (validation refuses a `;` before the end), it runs as a subquery
(a `COMMIT; DELETE ...` tail is a syntax error there), and the session has
`default_transaction_read_only=on`, so even a transaction started after a
COMMIT is read-only.

## Failure modes

| Symptom | Meaning / action |
|---|---|
| `Config invalid: <path>: ...` | the config failed validation; run `--validate` for every problem. Nothing else ran |
| `Catalog DSN not set: X` | env var `X` (from `catalog.dsn_env` or the registry) is empty in the unit's environment |
| `Catalog DB unreachable via $X` | connect failed. The error reads `cannot connect to the catalog DB ($X): <class>: <detail>` and never carries the DSN (`framework.core.dsn_redaction`): libpq echoes a malformed DSN, password included, so a DSN parse error (`not a valid connection string or URI`) keeps no driver text, and other messages lose quoted segments and every piece of the password. Usually an unencoded special character in a URI password (`%` → `%25`, `@` → `%40`, `/` → `%2F`) |
| seed source `<name>` skipped: `CatalogConnectError: cannot connect to the seed DB ($X): ...` | the seed query's DSN failed the same way (same redaction); the other sources and the search still run |
| `Amazon Creators API not configured` | `AMAZON_CREATORS_*` missing; the seed phase still ran |
| `Creators unavailable: ...` (failure) | auth / network; pages fetched before it are inserted and recorded, the rest retry next run |
| `stopped: budget-exhausted` | the consumer's daily share is spent; the slot is retried next run |
| `stopped: throttled` | Amazon 429 after the client's backoff; lower `total_calls_per_day` in the budget config |
| `stopped: no-allowance` | protected consumers' reserve or `keep_unused_fraction_of_day` leave no headroom today |
| `stopped: cooldown` | the cycle is complete and `cycle_cooldown_hours` have not passed |
| `stopped: server-errors` / `slots-deferred` | Creators answered 5xx; those slots are retried by the next run (a page failing on three runs ends its query as `server-error`) |
| `query_errors > 0` | a query's request failed with a client error (often a filter Creators refuses: brand / browse node), or a page kept failing with server errors; see the report's `ended_with_errors` |
| `saturated_queries > 0` | a query reports more results than 10 pages reach; add price bands, brands or browse-node scopes |
| `low_yield_queries > 0` | queries ended early because their pages added few new in-lane ASINs: normal once the lanes are enumerated |
| `known_now_rejected > 0` | catalog rows whose current title hits a global reject (an older rule set, or a trusted untitled seed label): remove or relabel them; the report lists ASIN + rule |
| partition-unit warning | most listings fall outside their price band: the cents unit verified on 2026-11-12 no longer holds; check `creators.price_filter_unit` |
| `insert_errors > 0` | the site's `insert_sql` failed for some rows (CHECK, FK); samples in the decisions; other rows landed |
| seed source "labelled record(s) without a title were not imported" | list the lanes you trust under the source's `lanes` (or set `trust_labels: true`) after checking where the labels came from |
| precision metrics missing | fewer than `qa.min_observations` labelled listings seen yet |

## Provenance and differences

The search loop follows the reference site's catalog-freshness agent
(DISCOVER phase): `discover()` per keyword, caps, budget, pricing gate,
SAVEPOINT per row, cursor that resumes after an early stop. The lane rules
port the sizing run's listing-churn classifier (precedence, include / exclude,
capacity and wattage bounds); a differential test pins the port to a frozen
copy of that classifier and its rules (`fixtures/reference-churn-rules.power.json`).
The worked example's paging follows the sizing run too: its A01-A20 and
family-B queries and bands, and its low-yield stop. Deliberate differences:
no Amazon content is stored (the reference stores titles, prices and
images); known listings get no "free re-price" (prices belong to the
price-refresh engine's 24-hour cache); the classifier is config, not a site
script; paging is the engine's own (see W3); a seed's own floor
(`seeds[].min_price`) can only raise the $1 pricing floor, never lower it.

## When W3 lands

W3 fixes `discover()` (empty page or itemPage 10 ends paging; the 306 clip in
the docstring) and adds a reserved floor for price-refresh consumers to
`amazon_creators`. The engine keeps working unchanged. Afterwards
`PinnedPageClient` and `budget.protect_consumers` may be retired in favour of
the shared behaviour, with this suite as the equivalence check.

## Related

- `amazon-price-refresh-agent` (F11): prices, titles and images of these
  ASINs in the site's 24-hour cache; new ASINs reach it through the site's
  refresh view.
- `recall-gate-agent` (F8), `spec-extractor` / `pick-builder` (F17): consume
  the catalog this agent grows.
