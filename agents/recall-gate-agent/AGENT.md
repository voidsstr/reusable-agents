# Recall Gate Agent — Runbook

> Daily: pull CPSC recalls and product-safety warnings, match them to every
> subscribed site's catalog with deterministic brand/model rules, write the
> result into the site's database so the site hides the buy button, and email
> the operator once per new match. One instance serves every site. **It never
> calls an LLM.**

Primitive: `framework/core/recall_gate.py` (sources, matcher, tiers, DB
contract, CLI). Config: `config/recall-gate-config.json` (storage copy at the
same key replaces it wholesale). Tests: `framework/tests/test_recall_gate*.py`.

## Status

Built inert: `manifest.json` has `enabled: false` and no `cron_expr`, and the
config subscribes no site. `owner` is empty on purpose, so a failed run's
email goes to `AGENT_DEFAULT_OWNER_EMAIL` (the fleet operator). Intended
schedule once a site is live: `15 6 * * *` America/Detroit (host time).
Turn-on order (design D6 / A1):

1. The site migration creates the tables and views: paste the output of
   `python3 -m framework.core.recall_gate schema --site <site>` into the
   site's own migration (change the `model_id`/`product_id` types if the
   catalog keys are not bigint).
2. Add `by_site.<site>` to the storage config (copy the whole file; see
   `_example_by_site` in the repo default): `enabled`, `profile`,
   `catalog_sql`, and a buy-button check (`buy_button.sql` and/or
   `buy_button.http`). Without a check every blocked SKU counts as an
   unverified live buy button. `catalog_sql` must return EVERY product
   whose page the site still renders, retired or inactive ones included (no
   `WHERE p.is_active`): a product it leaves out is treated as having no
   page.
3. The DSN env var NAME comes from the site registry, including its storage
   override (`python3 -m framework.core.site_registry dsn-env <site>`), unless
   the entry sets `dsn_env`. The value is read from the environment and never
   logged. The `queue`/`review` CLI resolves it the same way.
4. Dry run: `RECALL_GATE_DRY_RUN=1 RECALL_GATE_SITES=<site> python3 agents/recall-gate-agent/agent.py`.
   Every site write is rolled back; no email, no live-page probe, no
   buy-button SQL; the agent state is left exactly as it was (the API's
   730-day backfill is NOT used up); the goal metrics are reported as
   `dry_run_<name>` so no goal records a rolled-back run; the summary still
   shows the queue and blocks the real run would leave.
5. First real sync, then review the queue by hand (D6) before registering
   the cron.

## Per-run flow

1. **Fetch** (`SourceFetcher`):
   - SaferProducts API `RestWebServices/Recall?format=json` with
     `LastPublishDateStart` = today − 21 days, widened back to the last
     success after an outage. It reads 730 days instead until EVERY
     subscribed site has committed a sync of that history (per-site
     `api_backfilled_at` in the agent state), so a site subscribed later, or
     one whose first sync failed, still gets it.
   - CPSC recall CSV and CPSC product-safety-warning CSV, conditional GET
     (If-Modified-Since) against `~/.cache/reusable-agents/recall-gate/`.
     A header change or a short file fails the source instead of
     producing empty records.
   - The cpsc.gov HTML pages are behind a bot wall and are never fetched.
     Warnings exist only in the warning CSV (the API has none).
   - The source state (API window anchor, CSV validators) only advances when
     at least one site committed the data.
2. **Merge**: one record per `cpsc-recall-<digits>` / `cpsc-warning-<digits>`;
   the API wins for text and URL, the CSV adds the normalised remedy type and
   the warning→recall link. Records a site already stores are re-checked
   too, so a source outage never shrinks what the catalog is tested against.
3. **Match** per site (`Matcher`), see below.
4. **Write** in one transaction under `pg_try_advisory_xact_lock` with
   `statement_timeout`: `recalls` (relevant or matched records),
   `recall_matches` (insert, or the update `plan_match_update` allows), stale
   flags, one `recall_sync_log` row, then `post_sync_sql` hooks. A re-run
   with the same inputs changes nothing but the sync-log row.
5. **Notify**: one email per new match, buy-button flip (either way), match
   that became confirmed, or review the gate requested. More than
   `email.max_per_run` (25) become one summary email. Sent state is
   `notified_key` on the match row (kind/tier/status/hold/review). No
   recipient or sender configured fails the run.
6. **Verify**: `buy_button.sql` (site data, run in a READ ONLY transaction
   that is always rolled back) and/or the HTTP probe (live pages of the
   blocked products, `markers` regexes). The probe only GETs the site
   registry's domain (and `www.`) plus `buy_button.http.allowed_hosts`,
   follows same-host redirects only, and treats a redirect to a marker (an
   Amazon `/dp/` link) as a live buy button without fetching it. Blocked SKUs
   nobody could check (probe error, other host, no URL, past `max_probes`, or
   no check configured) count as live. A blocked product that catalog_sql no
   longer returns (retired, deactivated) keeps its block and is probed at the
   page stored with its match (`recall_matches.item_locator`: url, slug,
   asin); a 404/410 proves it has no page. One with no stored page to check
   is reported as `recalled_blocked_unlisted` (by the catalog_sql contract it
   renders no page), never as a live buy button, so a retired SKU cannot pin
   the goal. One alert email per distinct set.
7. **Report**: metrics, decisions, and the cross-site manual queue mirror at
   `agents/recall-gate-agent/manual-queue.json`.

### Fails closed

- A storage config that exists but cannot be read (an archived blob reads as
  None, invalid JSON) halts the run as a **failure** with the previous state
  kept; it never falls back to the repo default (whose `by_site` is empty).
- A site this agent has synced before that has **no** `by_site` entry halts
  the run as a failure (a lost or truncated storage copy). To unsubscribe on
  purpose set `by_site.<site>.enabled = false`: the agent forgets the site
  quietly and the entry can be deleted afterwards.
- Both halts still report `recall_sync_age_h` (from the stored per-site
  successes) and `site_errors`.

## Matching (deterministic)

Searched text: title, product names/models, description, image captions.
Remedy, hazard, contact and sold-at text are never searched for models.

| Kind | When | Tier / status |
|---|---|---|
| exact | ASIN or UPC printed by CPSC; a model number (or a kit component's, checked against the component's own brand) with the brand named as the subject; or every distinguishing token of the model name in one window with no variant beside it | the recall's tier, `active` |
| sibling | same brand + same family/line; the name next to another variant ("Seagull" vs "Seagull Pro"); a row named by a number alone when the recall names other numbers of the brand ("300W" vs "400W and 500W") | `review`, `manual-review` |
| ambiguous | model evidence only after "for"/"compatible with", or where the recall scopes it out ("… is not included in this recall", "excluding …"); a number found only as a rating in running text; a catalog row with no model detail; a recall that maps to none of the brand's models (`brand-wide` when it names no model, `unmapped-model` when it names models we do not carry); a strong model number of a row whose brand the recall does not name (sub-brand, legal name); model-NAME evidence on a recall outside the site's categories | `review`, `manual-review` |

- Brand is the subject when the title, product name, caption or a company
  field names it outside a compatibility cue. "Replacement Battery Packs for
  Dyson Vacuums" is not a Dyson recall.
- List naming is not a variant: "Explorer 1000, 1500 and 2000" names all
  three, "Q20 and Q20 Pro" names both. So does an ELIDED list, whose shared
  leading words stand once: "Explorer 1000 and 1000 Plus", "Prowler Pro /
  Pro Crew", "Explorer 1000 Pro and 2000 Pro". The elided reading is
  strict (`Matcher._windows`): the skipped part is whole list items, each
  starting or ending with a word of the named item, after a prefix that runs
  straight into them, in the name's word order; so "EU2200i, EU2200i
  Companion and EB2200i" never joins its first and last items, and "Explorer
  1000 and Solar Generator 1000 Plus" never names "Explorer 1000 Plus". A
  shorter catalog name is a variant only where every mention of it is part
  of a mention of a longer one ("HALO 1000" inside "HALO 1000 Bolt").
- All distinguishing words must fall within `matching.max_span` (8) tokens:
  in "RANGER 1000, 1000 Crew, XP 1000, XP 1000 Crew" the last item's shared
  "RANGER" stands 9 tokens back, so "RANGER XP 1000 Crew" is a sibling
  (manual queue), not exact.
- Numbers match with or without their unit ("DELTA Max 2000" ~ "2000W",
  "500W" ~ "500"), never across W/Wh. Pure-digit model numbers never match a
  unit count ("About 25,030 units").
- Identifier evidence (ASIN, UPC, model or component number) does not depend
  on the profile's category regexes; family/brand-level evidence only counts
  for items in the recall's categories (a Yeti cable recall says nothing
  about Yeti stations).
- Golden cases (tests): EcoFlow DELTA Max 2000 → exact by `EFD310`,
  repair-hold until 2026-01-07; DELTA Max 1600 → sibling; DELTA 2 Max,
  RIVER 2 → nothing; a kit listing `EFD310` as a component → exact. Aeiusny
  warning → 400W and 500W exact stop-use, 300W sibling (review), a row with
  no wattage → ambiguous. Aiper Seagull Pro → exact (`ZT6001`), Seagull SE →
  sibling, Scuba S1 (named only in the remedy) → nothing. Real review cases:
  Ridstar "Q20 and Q20 Pro" (both exact), Boon "Flair & Flair Elite", Thermos
  "King 3000 and 3020", Frigidaire `EFMIS129-B` ("not included") → review, LG
  RESU 10H home energy storage → in-niche exact; elided lists: Arctic Cat
  "Prowler Pro / Pro Crew" and Tracker "Off Road 800SX / 800SX Crew" (25-701),
  Tike Tech "Single City X3 and X3 Sport" (11-002), Polaris "RANGER 1000,
  1000 Crew, XP 1000" (23-258): every named model exact.
- CPSC printed ASIN `B093Y1KK5Q` under two different warnings (a battery
  pack and a CO detector): ASIN hits stay exact, with `brand_named: false`
  in the evidence when the recall does not name the catalog brand.

## Tiers

Ordered rules in config (`tiers.rules`, first match wins):

1. CPSC warning → `stop-use`.
2. Remedy only repair / inspect / new instructions / label (firmware fixes
   included) → `repair-hold`, 90 days from the recall date
   (`hold_anchor: published`). With `first_match` or `later` the hold counts
   from when the product was first matched, which is the run itself for a
   new match, so it blocks from the first sync.
3. Anything else (refund, replace, dispose, no remedy) → `stop-use`.

## The site renders from the views — only from the views

The reference DDL ships three views and they are the ONLY buy-button / pick
/ notice rule a site may use (the PDP, the `/r/` redirect and any API must
all read them; re-deriving the rule from `recall_matches` columns is how a
page and a redirect end up disagreeing). A match row may carry only
`model_id` or only `product_id`, so join on
`v.product_id = p.id OR v.model_id = p.model_id`.

- `v_recall_buy_blocks`: no buy button (active stop-use, or repair-hold
  before `hold_until`).
- `v_recall_pick_exclusions`: never in picks, ladders or substitutes: every
  match that is not `cleared` (design 0008_views: "no active or
  manual-review recall match"), including a confirmed recall whose hold
  ended.
- `v_recall_notices`: recall notice data for every active match, including
  expired holds.

## Manual queue and review

```bash
python3 -m framework.core.recall_gate queue  --site <site>
python3 -m framework.core.recall_gate review --site <site> --match-key '<key>' \
        --decision confirm|clear|reopen --reviewer <name> [--note "..."] [--tier stop-use|repair-hold]
```

- The queue holds `manual-review` rows and rows with `review_requested_at`
  (the gate wants a human; `gate_proposal` says what it found and why):
  - `gate-would-lift`: the gate's new result would demote an active match
    (to review, or from blocking to not blocking). It never does that by
    itself; the block stays until you confirm or clear.
  - `gate-no-longer-matches`: the gate no longer finds the match for a
    product the catalog still lists (a product that left the catalog only
    gets `stale_since`). The block stays.
  - `stronger-evidence-after-clear`: a match you cleared now has stronger
    evidence (e.g. sibling → exact) that does NOT block (a repair hold that
    is over). It is back in the queue (out of picks, buy button unchanged).
  - `gate-escalated-over-review`: the gate blocked a product over a review
    decision; `gate_proposal.human_decision` keeps that decision. Either a
    match you cleared now has stronger evidence that blocks (e.g. ambiguous
    → exact stop-use), or the recall itself turned stricter (a repair recall
    re-announced as a refund: `recall_tier_was` → `recall_tier`) and the
    stricter tier reached a match you confirmed. Confirm keeps the block;
    clear lifts it.
- `confirm` → `active` with the proposed (or given) tier and its hold;
  `clear` → `cleared`; `reopen` → back to the gate. Every decision resolves
  a pending review request.
- One-directional: the gate never lifts or clears a match by itself, and it
  always applies a stricter block, over a pending request or a review
  decision too (then it asks you and emails, `gate-escalated-over-review`).
  A recall escalation reaches a confirmed match once: if you then confirm a
  lower tier on purpose (`--tier repair-hold`), that stands. Otherwise a
  reviewed row is never overwritten (evidence still refreshes).
- A gate-only request (`gate-would-lift`, `gate-no-longer-matches`) is
  withdrawn once the gate's result and the stored decision agree again; a
  request that carries your earlier decision stays until you decide.
- `review` waits up to 60 s for a running sync of the site (same advisory
  lock), then exits 2.

## Email

To the fleet operator pair only (CLAUDE.md "Outbound-email recipient
policy"). Recipient and sender come from the env vars named in
`email.to_env` / `email.from_env` (`FRAMEWORK_OPERATOR_EMAIL`,
`OPERATOR_EMAIL` / `IMPLEMENTER_FROM`, `OPERATOR_FROM_EMAIL`), in the same
order `framework.core.resilience.notify_operator` reads them. Sent through
`shared.site_quality.send_via_msmtp` with `bypass_digest` (an alert, not
digest traffic) and recorded via `record_outbound` for the dashboard. No
recipient or sender configured: the emails count as failed, the run FAILS
(so the failure path reaches the operator another way), and they are retried
next run. With `email.enabled: false` (or `by_site.<site>.email.enabled:
false`) unsent matches stay pending (`emails_pending`) and go out, capped,
when email is turned back on. A live or unverified buy button on a blocked
SKU sends one alert per distinct set of SKUs (`email.alert_on_live_buy_button`).

## Goals

| Goal | Metric | Target |
|---|---|---|
| goal-recall-sync-fresh | `recall_sync_age_h` (max of source age and each site's last committed sync) | ≤ 26 |
| goal-no-recalled-live-buy-buttons | `recalled_live_buy_buttons` (live + unverified) | 0 |
| goal-manual-queue-under-72h | `manual_queue_oldest_h` | ≤ 72 |
| goal-recall-gate-sites-clean | `site_errors` | 0 |

Diagnostic metrics: `recalled_buy_buttons_unverified`,
`buy_button_unchecked_sites`, `recalled_blocked_unlisted` (blocked products
that left catalog_sql with no stored page to check; not counted as live),
`review_requested`, `manual_queue_size`, `matches_*`, `emails_*`,
`source_errors`, `llm_calls` (always 0).

## Amazon licence

`catalog_sql` text columns must come from the site's own models/brands
tables, never the Creators/PA-API offer cache: the gate keeps
`brand model_name` forever (`recall_matches.item_label`, the evidence, the
manual-queue mirror, emails) and the 24h purge only covers the offer cache.
There is no free-text `label` column (one in `catalog_sql` is ignored).

## Env overrides

- `RECALL_GATE_DRY_RUN=1` — roll back every write, send no email, probe
  nothing, keep the state, report goal metrics as `dry_run_<name>`.
- `RECALL_GATE_SITES=a,b` — only these subscribed sites (also skips the
  lost-subscription check).
- `RECALL_GATE_NOW=2026-11-17T06:15:00+00:00` — evaluate as of that instant.

## Troubleshooting

- `recall gate halted: config unreadable` — the storage copy of
  `config/recall-gate-config.json` exists but cannot be read (archived blob,
  invalid JSON). Rehydrate or rewrite it; the agent will not guess.
- `recall gate halted: config has no by_site entry for …` — see "Fails
  closed" above.
- `no DSN: none of [...] is set` — export the env var the site registry
  names (or set `by_site.<site>.dsn_env`).
- `relation "recalls" does not exist` — the site migration has not applied
  the DDL (step 1 above).
- `another recall-gate run or a review holds the lock` — a manual run or a
  review overlapped the cron; the next run proceeds.
- `cpsc_recall_csv: unexpected CSV header` — CPSC changed the export; fix the
  parser and `RECALL_CSV_REQUIRED` together.
- A product that should be blocked still sells: check `v_recall_buy_blocks`
  for its id, then the site renderer and its HTML cache (s-maxage ≤ 600).
- `recalled_blocked_unlisted` > 0: blocked products that catalog_sql no
  longer returns and that have no stored page to probe. Fine if the site
  really renders no page for them; if it does, fix catalog_sql to return
  every product that still renders a page (never clear the match).
