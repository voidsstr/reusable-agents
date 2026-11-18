# Pick Builder — runbook

**Engine:** `agents/pick-builder-agent` (AgentBase, Python, deterministic,
no LLM, no external API). **Per-site instances** are config-only
directories in the site repos (`<site repo>/agents/pick-builder/`:
`manifest.json` + `site.yaml` + goals). This engine's manifest is
`enabled: false` with no cron: a template, never scheduled itself.

## What it does

Fills a site's merchandising selections from ONE eligibility view:

* **ladder slots** — budget tiers (e.g. $500 / $1k / $2.5k / $5k kits),
  each filled from its ladder kind's PLAN: an ordered list of role slots
  (e.g. station → panel that charges it → the exact adapter between them →
  an alternative). Later slots may depend on earlier picks through
  compat edges.
* **best lists** — "best X under $Y", "best X for <use>", deals: one
  selector per list kind, parameterised by the list row.
* **the substitutes map** — for every product a site may show (buyable or
  not), up to N eligible products that match its key specs ("what to buy
  instead"); on an unbuyable page it leads the hero.
* **the pick-ASIN list** — published to framework storage for
  `framework.core.price_refresh_priority` (a `storage_list` group), so the
  price refresh keeps every pick fresh (and therefore eligible).

**Policy is never loosened.** A slot no eligible product satisfies is
written EMPTY with the reason (`"no eligible candidate: usable_wh >= 5000"`,
`"depends on the station slot, which is empty"`). No filter, budget or view
is relaxed to fill it, and a filter that cannot be evaluated (a missing tier
/ list parameter, a subject without the attribute) empties the slot instead
of being skipped. Nothing is picked from outside the eligibility view. A
slot that a direct connection makes unnecessary (`"direct connection: no
adapter needed"`) is written with status `not-needed`: the kit is complete,
so it is not counted as a gap.

**What is stored never carries Amazon data.** Reasons and the eligibility
evidence outlive the 24h Amazon display window (binding amendment 2: only the
ASIN and our own derived outcome persist), so they never show `price`,
`list_price`, `currency` or an attribute listed under
`eligibility.never_store`, nor a number derived from them (a remaining
budget): reason templates naming one are rejected at config time, the
budget check is stored as "price within the tier's remaining budget". They
carry no candidate counts either, so a row is rewritten only when its pick
or its reason changes. The counted trace (`"18 eligible; kind = station ->
9; usable_wh >= 5000 -> 0"`) is printed by `--plan` and never stored.

## One run

1. Open one REPEATABLE READ snapshot (now() is fixed, so a 24h freshness
   window cannot move under the run) and load: the eligibility pool, the
   compat edges the plans depend on, active ladders / tiers / lists, the
   substitute subjects, and the current outputs.
2. Apply the **pricing-integrity floor** to every pool row on top of the
   view (null / sub-$1 price, non-USD currency, list price below price,
   category confidence below 0.5, malformed ASIN): such rows are never
   picked and are counted (`pool_integrity_rejects`, goal 0) because they
   mean the view is wrong.
3. **Short-circuit:** when the inputs hash (pool rows, compat edges,
   definitions, subjects, config) and the outputs hash both equal the last
   run's, the last publish succeeded (no `publish_pending` in the state) and
   storage still holds that list, nothing is rebuilt or written
   (`short_circuited`). `AGENT_FORCE_RUN=1` bypasses it.
4. Fill every tier of every active ladder from its kind's plan
   (`plans.<kind>`, else `plans.default`), every active list from its
   kind's selector, every subject's substitutes. Ladders without a plan and
   lists without a selector are only **swept**: ineligible products are
   cleared, nothing new is chosen.
5. Write only the GROUPS (one tier, one list, one subject) whose rows
   changed (DELETE + INSERT of that group), all in the snapshot
   transaction; commit.
6. Publish the pick-ASIN list: ladder picks (tier order, plan order, rank),
   then list items, then (when `publish.include` has it) substitutes. Picks
   dropped this run stay on the list for `keep_dropped_runs` runs so the
   refresh re-checks them (a pick that fell out because its price aged
   past 24h can come back). The list is read back after the write; a
   failed or unverified publish (`pick_publish_failed` = 1) leaves
   `publish_pending` in the state, so the next run, even with nothing
   changed, rebuilds and publishes again.
7. **Verify** in a fresh transaction: live picks no longer in the
   eligibility pool (`ineligible_in_picks`, goal 0; a race with a price
   aging out between build and check is the only benign cause and the next
   run replaces it).

Deterministic: every ordering ends with price, ASIN, product id; input row
order never matters (several compat rows for one pair are merged by an
explicit precedence). One run per instance at a time (session advisory
lock, `pick-builder:<agent id>`). A run that raises returns `failure` with
its previous state kept (published list, dropped picks), never an empty one.

## Selector language

```yaml
- role: panel                       # ladder slots only
  count: 1                          # int or "{list.size|10}"
  filters:
    - {attr: kind, op: eq, value: panel}
    - {attr: usable_wh, op: gte, value: "{tier.required_wh}"}   # tier / list / ladder / earlier slot params
    - {attr: list_price, op: lte, expr: "price * 3"}            # arithmetic over the candidate itself
  compat: {relation: charges_from, with: station, side: b,      # an edge with the station pick's model
           verdicts: [fits, fits_with_adapter], include_via_in_budget: true}
  via_of: {relation: charges_from, a_role: station, b_role: panel}   # the edge's exact adapter model
  sort: [{key: "rated_w / price", dir: desc}]                   # NULL last; then price, ASIN, id
  max_per: [{attr: brand, count: 2}]
  one_per_model: true               # default: one listing per model
  max_budget_share: 0.8             # this slot may use at most 80% of the tier budget
  budget: kit                       # kit (default) | own: an alternative judged on the tier budget alone
  reason: "{rated_w:,.0f} W panel that charges the station"     # never price / list_price / currency
```

* `op`: `eq ne gt gte lt lte in not_in truthy falsy is_null not_null`;
  substitute constraints also `within_pct` (value = percent of the
  subject's attribute) and `gte_subject lte_subject eq_subject`. A NULL
  attribute fails every comparison.
* Expressions: attribute names, numbers, `+ - * /`, parentheses, unary
  minus — parsed, never eval'd; a NULL operand or a division by zero gives
  NULL.
* Templates are whole-string `{namespace.key}` or `{namespace.key|default}`:
  `tier.*` (the tier's params + `budget`), `ladder.*` (`slug`, `kind`, `id`),
  `list.*` (the list's params + `slug`, `kind`), `slot.<role>.<attr>`.
* Ladder budget (`ladders.budget.enforce`, default on): picks are taken in
  plan order while the tier budget lasts; a tier with no budget leaves
  every slot empty. `include_via_in_budget`: a candidate whose compat edge
  needs an adapter must fit the budget with the cheapest eligible listing
  of that adapter, and is skipped when none is eligible; that adapter's
  money is then RESERVED and released only to the `via_of` slot of that
  edge (an adapter no slot buys stays reserved), so a slot in between (an
  expansion battery) cannot spend it — a kit is never left needing an
  adapter it cannot afford. `budget: own` marks an alternative (another
  station for the same tier): each candidate is judged against the tier
  budget (and `max_budget_share`) alone, and it spends nothing of the kit's.
* **Compat slots are ONE unit.** An edge qualifies only when its verdict is
  in the slot's `verdicts` (default `compat.fit_verdicts`: `fits`,
  `fits_with_adapter`), ONE unit of the B model works (`fits` by
  definition; `fits_with_adapter` only when its stored 1 x 1 configuration
  fits — the adapter label also covers series-only arrangements; a
  `fits_in_series` / `fits_in_parallel` edge never qualifies, and config
  rejects those verdicts for a slot), and it carries none of
  `compat.exclude_flags` (default `computed-check-unknown`: a safety check,
  such as the cold-Voc check, could not run). Several rows of one pair are
  merged conservatively: any `over_limit` / `no_fit` wins, then manual over
  manufacturer-listed over computed, then the most conservative verdict.
* Substitute rules (`substitutes.rules.<subject kind>`): `constraints`,
  `sort` (default price asc), `reason`, `match_kind` (default true).
  `same_model: prefer` (default) lists the same model's other eligible
  listings first; `exclude` drops them. A subject without the attribute a
  constraint needs gets no substitute: nothing beats a near-miss. Subject
  comparisons (`within_pct`, `gte_subject`, ...) read the subject's mapped
  `substitutes.subjects.attributes` (a mapped name wins over the base
  columns); a price band needs a mapped subject price (e.g. `{attr: price,
  op: lte_subject}` with `price` mapped under
  `substitutes.subjects.attributes`), and an unknown subject price gives no
  substitute.

## TABLE CONTRACT

Every table and column is named in `site.yaml`; reference implementation
and the tests' schema: [`fixtures/schema.sql`](fixtures/schema.sql).
WHERE fragments are trusted config and see these aliases: eligibility `e`,
ladders `l`, pick lists `pl`, substitute subjects `s`.

### `eligibility` — read; the only source of picks (required)

| logical | required | meaning |
|---|---|---|
| `product_id` | yes | the listing (what slots / items / substitutes store) |
| `asin` | yes | what the published list carries |
| `kind` | yes | product kind |
| `price` | yes | the current, quotable price (already fresh-filtered by the view) |
| `model_id` | no | our model (compat edges, one-per-model, same-model substitutes) |
| `list_price`, `currency`, `category_confidence` | no | checked by the integrity floor when mapped (a settings warning names each one left unmapped) |
| `attributes.<name>` | no | any column a selector reads (specs, brand, ...) |
| `never_store: [<attribute>]` | no | attributes from the Amazon API besides the three above: filter / sort only, never in stored text |

The relation must already implement the site's eligibility policy (fresh
price, live and active, not recalled, certification, ...); the engine adds
only the integrity floor.

### `ladders` (enabled → required)

* `ladders.ladders`: `id`, `kind` required, `slug` optional; `where`
  selects the active ladders (others are not touched).
* `ladders.tiers`: `id`, `ladder_id` required; `budget` (number), `params`
  (jsonb object), `order` (sort key) optional; `param_columns` adds plain
  columns as params (e.g. `required_wh: required_wh`).
* `ladders.slots`: `tier_id`, `role`, `rank`, `product_id` required
  (`product_id` NULL = deliberately empty); `reason` (text), `eligibility`
  (jsonb evidence: `{status: filled|empty|not-needed, view, checks}`; the
  checks a filled pick passed, without counts or prices), `updated_at` (set
  on write) optional. Recommended key: `PRIMARY KEY (tier_id, role, rank)`.

### `lists` (enabled → required)

* `lists.lists`: `id`, `kind` required; `slug`, `params` (jsonb) optional;
  `where` selects the active lists.
* `lists.items`: `list_id`, `product_id`, `rank` required; `reason`,
  `updated_at` optional. Recommended key: `PRIMARY KEY (list_id, rank)`.

### `substitutes` (enabled → required)

* `substitutes.subjects`: a relation of every product that may need one —
  `product_id`, `kind` required; `asin`, `model_id`, `unbuyable` (boolean,
  for the goal) optional; `attributes.<name>` for the rules.
* `substitutes.output`: `subject_id`, `substitute_id`, `rank` required;
  `reason`, `updated_at` optional. Recommended key: `(subject_id, rank)`.

### `compat` (required when a plan uses `compat` / `via_of`)

`table` + `a_model_id`, `b_model_id`, `relation`, `verdict`, `config`
(jsonb: `series`, `parallel`, `options`, `flags` decide whether ONE unit
works) required; `via_model_id`, `rule_id`, `confidence` optional
(`rule_id` / `confidence` order and merge several rows of one pair)
(`framework.core.compat_graph.reference_ddl()` shape). `fit_verdicts` is the
default verdict set a `compat` constraint accepts; `exclude_flags` the edge
flags that disqualify it.

### Published pick-ASIN list (framework storage)

`publish.key` (default `agents/{agent_id}/state/pick-asins.json`):
`{"asins": [...best first...], "run_ts", "source", "generated_at",
"counts": {"current", "recently_dropped"}}`. The price refresh reads it as:

```yaml
groups:
  - {name: picks, kind: storage_list, key: agents/<site>-pick-builder/state/pick-asins.json, gate_hours: 12}
```

## Configuration

Template: [`site.example.yaml`](site.example.yaml) (the tests run it
against the fixture schema); schema:
[`pick-builder-config.schema.json`](pick-builder-config.schema.json).
Semantic checks run before any DB work: identifiers, fragments, operators,
attribute names, expressions, template namespaces, slot dependencies that
must refer to EARLIER roles, subject attributes the rules compare.

`database.dsn_env` is the NAME of the env var holding the site DSN (a
pasted connection string is rejected without being echoed). The DSN never
reaches a run record, framework storage, a log or the CLI: a connect
failure is reported as `cannot connect to the site DB ($NAME): <class>:
<detail>`, where a malformed DSN keeps no driver text at all (libpq echoes
the connection string, a mis-encoded password included), every other
driver message loses its quoted segments, and every fragment of the
password is replaced by `<redacted>`.

## Metrics and goals

Goals ([`goals.json`](goals.json)): `ineligible_in_picks` = 0,
`slots_filled_pct` ≥ 80 (filled ladder slots + list positions over all of
them except `not-needed` slots; raised by adding eligible inventory, never
by loosening), `unbuyable_with_substitute_pct` ≥ 60, `pick_publish_failed`
= 0, `pool_integrity_rejects` = 0.

Other keys: `pool_size`, `pool_duplicates`, `ineligible_found` (picks that
went ineligible since the last run), `ladder_slots_total|filled|empty|
not_needed`, `ladder_slots_filled_pct` (not-needed slots excluded),
`list_positions_total`, `list_items_filled`, `lists_short` / `lists_empty`
(lists with fewer items than positions; the warning says why),
`substitute_subjects`, `substitute_subjects_with`,
`substitute_subjects_without`, `unbuyable_subjects`,
`unbuyable_with_substitute`, `groups_written`, `rows_inserted`,
`rows_deleted`, `pick_asins_published` (from a write that was read back),
`ladders_without_plan`, `lists_without_selector`, `compat_duplicate_pairs`
(pairs with several compat rows, merged by precedence).

## Per-site instance

```json
{
  "id": "<site>-pick-builder",
  "name": "<Site> Pick Builder",
  "category": "seo",
  "cron_expr": "35 */4 * * *",
  "timezone": "America/Detroit",
  "entry_command": "PICK_BUILDER_CONFIG=<site repo>/agents/pick-builder/site.yaml python3 /home/voidsstr/development/reusable-agents/agents/pick-builder-agent/agent.py",
  "owner": "framework"
}
```

Schedule it 30 minutes after the price refresh (eligibility depends on
fresh prices). Seed the goals from [`goals.json`](goals.json).

## CLI (no run record)

```bash
A=agents/pick-builder-agent/agent.py
python3 $A --config     --site-yaml <site.yaml>   # resolved settings
python3 $A --print-sql  --site-yaml <site.yaml>   # the eligibility-pool SQL
python3 $A --plan       --site-yaml <site.yaml>   # READ-ONLY session: every slot (an empty one with its
                                                  # reason and counted trace) and every list (a short
                                                  # one with why); no writes, no publish
```

## Troubleshooting

| Symptom | Look at |
|---|---|
| a slot is empty | its `reason` names the filter that left no candidate; `--plan` shows the counted trace; fix the inventory or data, not the filter |
| a panel slot is empty although a compat edge exists | the edge needs several panels (series / parallel), carries `computed-check-unknown`, or its adapter does not fit the budget: `--plan` shows which check left 0 |
| a list is short | `lists_short` and the run warning (e.g. a missing `max_price` parameter) |
| `pick_publish_failed` = 1 | framework storage; the next run publishes again (`publish_pending`) |
| `ineligible_found` high every run | pick ASINs are going stale: is the price refresh reading the published list (`storage_list` group) and are its gates inside 24h? |
| `pool_integrity_rejects` > 0 | the eligibility view lets invalid prices through: fix its WHERE |
| `ladders_without_plan` / `lists_without_selector` > 0 | a ladder / list kind in the DB with no plan / selector in site.yaml (its picks are only swept) |
| config fails in the first second | the summary lists every schema / semantic problem |
| `cannot connect to the site DB ($NAME): ProgrammingError: not a valid connection string or URI` | the DSN in `$NAME` does not parse (the driver's text is withheld because it echoes the DSN): usually an unencoded special character in a URI password (`%` → `%25`, `@` → `%40`, `/` → `%2F`) |

## Tests

`framework/tests/test_pick_builder_agent.py` — offline; DB tests use the
throwaway postgres:16 container (skip without docker / the local image)
loaded with `fixtures/schema.sql` and run the shipped `site.example.yaml`.
