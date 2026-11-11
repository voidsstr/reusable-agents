# Search-Demand Steering — engine (`search-demand-agent`)

> Turns the site's verified-human affiliate clicks plus 28 days of Google
> Search Console and GA4 data into a compact "what to write next" signal.
> It leads with WHAT SELLS (where clicks that pass `human_clicks.py` come
> from: templates, topic clusters per 100 articles, categories, pages,
> products, placements). Then it covers winning page templates, category
> demand lanes, uncovered queries, strike-distance queries and hot
> head-to-head pairs. Author agents inject it into their prompts. North
> Star: **verified-human affiliate clicks**, then organic clicks + indexed
> pages.

This is the shared **engine**. Per-site instances run it. Deterministic, no
LLM.

## At a glance

| | |
|---|---|
| Agent id | `search-demand-agent` (class default); runs as the per-site id in `AGENT_ID` |
| Home | `reusable-agents: agents/search-demand-agent/agent.py` (single file) |
| Kind | AgentBase python engine (`SearchDemandAgent`). There is no `manifest.json` in this dir |
| Schedule | none. Not registered (`GET /api/agents/search-demand-agent` → 404) |
| Entry command | `python3 agent.py` with `SEARCH_DEMAND_CONFIG` (plus `AGENT_ID`, and the DSN env if used) |
| Category | seo (per instance manifest) |
| Status | engine only. Live instance: `specpicks-search-demand-agent` (05:35 and 17:35 America/Detroit). `aisleprompt-search-demand-agent` (07:50 and 15:50) created 2026-11-11, registered by the operator |
| Contract | `framework/core/demand_signal.py` (`write_demand` / `read_demand` / `build_prompt_block`) |
| Instance runbook | `specpicks: agents/search-demand-agent/AGENT.md` |

## What it does

`run()` in `agent.py`:

1. **Config.** `_load_config()` reads the YAML at `SEARCH_DEMAND_CONFIG`.
   The `site_id`, `data_sources` and `auth` keys are required; if any is
   missing the process exits (`SystemExit`).
2. **Google token.** Runs
   `agents/seo-opportunity-agent/lib/collector/refresh-token.py --oauth-file <auth.oauth_file>`,
   the same helper and oauth file as the SEO collector. A non-zero exit
   raises `token mint failed rc=… — re-auth with: bash
   install/reauth-google-oauth.sh`; output that is not a `ya29…` access
   token raises `token mint returned no ya29 token`.
3. **GSC pull** (`searchAnalytics/query`): dimensions `query,page`, window
   today−30 → today−2 (GSC lags about 2 days), `rowLimit 5000`,
   `dataState final`. A failure here aborts the run.
4. **GA4 pull** (`runReport`): `pagePath` × `screenPageViews, sessions`,
   `28daysAgo → yesterday`, top 2000 by views. On failure it logs a warning
   decision and continues GSC-only.
5. **Category vocabulary**: `SELECT slug, name FROM categories WHERE
   is_active` via the DSN in the env var named by `data_sources.db.dsn_env`.
   With no DSN it silently skips the vocabulary (and the `/vs/` pair
   resolution and outcome metric); a DB error logs a `warning` and it steers
   by template only.
6. **Adaptive impression floor**: `eff_min = min(min_impressions, max(2,
   total_impressions // 200))`. Without it, a fixed 20 blanks the whole
   signal on a pre-indexing site (specpicks had 867 impressions per 28 days
   in 2026-08).
7. **Template winners**: one GA4 report per configured template, filtered
   `pagePath BEGINS_WITH <prefix>` with a `TOTAL` aggregation. The prefix is
   the template's `prefix` key, or derived from a `^/…` pattern. This
   measures long tails that a top-N pull can't see: specpicks has about 140k
   `/vs/` pages with a few views each. GSC clicks and impressions are added
   per template via first-match regex classification. The output is sorted
   by views, and templates with zero views and zero impressions are dropped.
8. **Per-query buckets** (queries with impressions ≥ `eff_min`): best
   position 6–20 → `strike_distance`; best position > 20 → `zero_coverage`
   (demand with no top-20 page). Both are sorted by impressions, top 25.
9. **Steer topics**: GSC queries are token-matched against the category
   vocabulary (impressions, clicks, up to 4 sample queries). GA4 views add
   evidence from `/category/<slug>` pages and from the top 25 `/vs/<A>/<B>`
   pages, which are resolved to product names and category through
   `products` + `categories`. Score = `impressions × 3 + views`. The top 20
   with score > 0 are kept, each tagged with the #1 template.
10. **Hot head-to-head pairs**: the top 10 resolved `/vs/` pairs by GA4
    views, plus the top 10 GSC queries containing " vs ", capped at 15.
11. **WHAT SELLS** (optional, `data_sources.affiliate_clicks`, added
    2026-11-11): `_conversion_winners` reads the click table through
    `human_clicks.human_counts_by`, resolving the spec with
    `profile=<AGENT_ID>`. It queries 30d, 7d and 14d per facet (page,
    product, placement, category), all in one read-only session under
    `statement_timeout`. `summarize_conversions` (pure) builds
    `conversion_winners` from those counts:
    - totals: sums over the page facet, NULL pages included;
    - pages: normalized paths, classified by the block's `templates` (else
      the top-level list);
    - template yields: `clicks_per_100` = clicks on the template's
      published-article paths (`article_clicks`) per 100 published
      articles; clicks on other pages its pattern matches (e.g. a legacy
      article table) stay in the template totals only;
    - clusters: `ai_traffic.cluster_yield` over `articles_query`;
    - products: resolved by `products_query`. A row with `active` false
      keeps its clicks (product and category totals) but gets no `link`;
    - categories: from the products, or from `category_col`.

    Any failure logs a `warning` and the signal ships without it. That
    includes a bad config value or regex: every value is parsed and every
    template / cluster regex compiled before connecting.
12. **Publish**: `demand_signal.write_demand(storage, site_id, payload)`
    writes `framework/demand-signal/<site_id>.json` and adds `site` and
    `generated_at`.
13. **Outcome metric**: `steered_published_7d` counts published
    `editorial_articles` from the last 7 days whose title `ILIKE` the first
    word of one of the top 15 steer topics. The docstring calls it
    "imperfect but directionally honest".

## Inputs

- Google Search Console Search Analytics API and GA4 Data API (OAuth
  refresh token in `auth.oauth_file`).
- Site DB (optional, read only): `categories`, `products` (to resolve `/vs/`
  ASIN pairs), `editorial_articles` (outcome metric).
- Site `ai_traffic_log` (optional, read only, `data_sources.ai_traffic`,
  added 2026-09-25) through `framework/core/ai_traffic.py`: human AI-assistant
  referrals plus live user fetches (ChatGPT-User, Perplexity-User,
  Claude-User), spoofed crawler IPs dropped. GSC and GA4 cannot see this
  demand, and the article proposer had no other input for it.
- Site affiliate click table (optional, read only,
  `data_sources.affiliate_clicks`): first-party click rows. Only the rows
  `framework/core/human_clicks.py` labels `human` count, the same
  definition the goal trackers use. GA4 views include crawlers, so this is
  the only purchase signal the authors get.
- Config: the instance `site.yaml`.

## Outputs

- **Storage**: `framework/demand-signal/<site_id>.json`, with keys `site`,
  `generated_at`, `template_winners`, `steer_topics`, `zero_coverage`,
  `strike_distance`, `h2h_hot`, and (with `data_sources.ai_traffic`)
  `ai_assistant_demand` = `{clusters: [{cluster, articles, referrals,
  fetches, referrals_per_100, fetches_per_100}], top_products: [{key, title,
  category, referrals, fetches}]}`. `demand_signal.build_prompt_block`
  renders it as the **AI ASSISTANT DEMAND** block: clusters ranked by
  referrals per 100 published articles (a yield; clusters under 20 articles
  listed last as too few to judge), then the product pages assistants land
  on (single-product lookups: PDP / head-to-head work, not category
  round-ups). With `data_sources.affiliate_clicks` the signal also carries
  `conversion_winners`, with keys `label`, `window_days`, `trend_days`,
  `definition` and `total` `{d30, d7, prev7}`, plus these lists:
  - `templates`: `{template, clicks, d7, prev7, pages, articles,
    article_clicks, clicks_per_100}`;
  - `placements`: `{placement, clicks, d7, prev7}`;
  - `clusters`: `{cluster, articles, clicks, d7, clicks_per_100}`;
  - `categories`: `{category, clicks, d7, prev7, products, value}`;
  - `products`: `{key, title, category, price, link, active, clicks, d7,
    prev7}` (`active: false` only for an inactive catalog row);
  - `pages`: `{path, template, clicks, d7, prev7}`.

  `build_prompt_block` renders it FIRST as **WHAT SELLS (verified-human
  <label> clicks, 30d; last 7d vs prior 7d)**. Its "WHAT SELLS clusters"
  are exactly `demand_signal.top_conversion_clusters(sig)`: at least one
  click, 10+ articles or 10+ clicks, a yield at or above the site average,
  at most `WHAT_SELLS_TOP_CLUSTERS` (5). Judged clusters below the average
  go on a separate "not WHAT SELLS lanes" line. The closing RULE names
  those clusters: within the topics the consumer's prompt allows, a
  proposal that extends one of them or a listed product category ranks
  above an AI-citation-only lane and must link the converting page or
  product. A BLOCKED or do-not-propose list in the consumer's prompt still
  wins. The block never shows a per-product price, so authors don't quote
  a stale one, and never lists an inactive product (`active: false`) or
  one the catalog lookup did not resolve. `template_winners` is relabelled
  "GA4 views (includes crawlers)".
  Consumers ignore a signal older than 72h (`DEFAULT_MAX_AGE_HOURS`).
- **Decisions**: `info` "demand signal written to …", plus `warning`s for
  GA4, vocabulary or template-total failures.
- **No DB writes, no recs, no handoffs, no agent-specific email.** The
  standard AgentBase run summary goes to the manifest owner: successful
  runs are queued for the daily digest. Failed runs go through
  `send_via_msmtp`, which under the wrapper's default `DIGEST_ONLY=1` also
  queues rather than sends. A failed run also enqueues an `agent-doctor`
  run.
- **RunResult**: `success` whenever the pipeline completes. Any exception
  (token mint, GSC pull) → `failure`.

`RunResult.metrics`: `gsc_rows`, `ga4_rows`, `topics_emitted`,
`strike_found`, `zero_coverage_found`, `h2h_hot_found`,
`steered_published_7d`, and with AI demand `ai_demand_clusters`,
`ai_referrals_on_articles`, `ai_demand_top_products`. With WHAT SELLS:

- `conversion_clicks_30d`, `conversion_clicks_7d`,
  `conversion_clicks_prev7d`.
- `conversion_article_clicks_30d`: clicks on published article paths.
- Only when `articles_query` returns `published_at`:
  - `conversion_new_article_clicks_30d`: clicks on articles published in
    the last 30d.
  - `conversion_recent_articles_7d`.
  - `conversion_top5_cluster_share_7d_pct`: share of the last 7 days'
    articles that fall in a WHAT SELLS cluster, the same set the block
    lists and its RULE names (`demand_signal.top_conversion_clusters`,
    top `WHAT_SELLS_TOP_CLUSTERS` = 5). It is omitted when no cluster
    qualifies.

## Goals & metrics

The engine declares none. Instances seed goals bound to the metric keys
above (specpicks: `topics_emitted`, `steered_published_7d`,
`zero_coverage_found`; see the instance runbook).

## Configuration

Env vars:

| Name | Default | Meaning |
|---|---|---|
| `SEARCH_DEMAND_CONFIG` | required | path to instance `site.yaml` |
| `AGENT_ID` | `search-demand-agent` | per-site agent id |
| DSN env named by `data_sources.db.dsn_env` (e.g. `DATABASE_URL`) | unset → template-only steering | site DB |
| `AGENT_FORCE_RUN` | unset | `1` bypasses the short-circuit for one run |

`site.yaml` keys:

| Key | Default | Meaning |
|---|---|---|
| `site_id` | required | storage key suffix + author lookup key |
| `data_sources.gsc.site_url` | required | GSC property (e.g. `sc-domain:<domain>`) |
| `data_sources.ga4.property_id` | required | GA4 property id |
| `data_sources.db.dsn_env` | none | env var holding the DSN |
| `auth.oauth_file` | required | Google OAuth refresh-token file |
| `templates[]` | `[]` | ordered `{name, pattern[, prefix]}`, where the first regex match wins. `prefix` sets the GA4 total filter explicitly |
| `min_impressions` | 20 | ceiling for the adaptive floor |
| `data_sources.affiliate_clicks` | none (off) | WHAT SELLS. Keys: `label` (merchant name in the title, default `affiliate`), `dsn_env` (default `data_sources.db.dsn_env`), `table`, `spec` (human_clicks columns; storage `by_profile[<AGENT_ID>]` overrides apply, or set `profile`), `match` (`{col: value}`; a list means IN), `page_col` (alias `group_col`; a path or full-URL column), `page_path_template` (`{value}`), optional `product_col` / `placement_col` / `category_col`, `products_query` (`key, title, category[, price][, link][, active]` for `%(keys)s`; price is used only for the aggregate category value; return `link` only for a row the site would link and `active` false for an inactive row, which is then never listed or linked; gate `category` on the site's category-confidence rule if it has one), `templates[]` (click-page classifier, default the top-level `templates`), `articles_query` / `article_path_template` / `clusters` (default `data_sources.ai_traffic`'s; a `published_at` column enables the dated metrics), `top_pages` 25, `top_products` 50, `top_categories` 10 (payload caps; the block renders 6 rows each, `conversion_clicks()` reads the payload, so raise them for a deterministic ranker), `max_product_lookup` 5000, `statement_timeout_ms` 30000. Any failure, a bad value or regex included, logs a warning and the signal ships without the block. |
| `data_sources.ai_traffic` | none (off) | AI-assistant demand. Keys: `dsn_env` (default `data_sources.db.dsn_env`), `config` (overrides for `ai_traffic.DEFAULTS`, e.g. `referral_sources`, `live_fetch_sources`), `articles_query` (`slug, title` of published articles), `article_path_template` (`/reviews/{slug}`), `clusters[]` (`{name, pattern}`; regex on "slug title", first match wins), `product_prefix`, `product_key_regex`, `products_query` (`key, title, category` for `%(keys)s`; double any literal `%`), `top_products` (15). Any failure logs a warning and the signal ships without the block. |

## Short-circuit & idempotency

- `signals()` returns `{day: date.today(), site, cfg}`. That is the
  **host-local** date (America/Detroit on the fleet host), not UTC, so the
  intent is one real run per local day. `AGENT_FORCE_RUN=1` bypasses it.
- **In practice it never fires.** `run()` returns a `RunResult` without
  `next_state`, so `post_run` persists `state = {}`. That drops the
  `_auto_signals_hash` that `_check_short_circuit` stored, and every tick
  does a full run. Evidence: the specpicks run history shows both daily
  ticks doing full work on the same local day (e.g. 2026-09-21 09:35Z and
  21:35Z). The cost is a few extra Google API calls, with no LLM involved.
  The fix belongs in the engine (return `next_state=self.state`).
- The output is a whole-file overwrite of one storage key, so re-runs are
  idempotent.

## Running & inspecting

Run it through an instance (see the instance runbook). Inspect the published
signal through the framework storage backend:

```bash
python3 -c "
import sys; sys.path.insert(0,'/home/voidsstr/development/reusable-agents')
from framework.core.storage import get_storage
from framework.core import demand_signal
sig = demand_signal.read_demand(get_storage(), 'specpicks')
print(demand_signal.build_prompt_block(sig) if sig else 'no fresh signal')"
```

This needs the same `STORAGE_BACKEND` / Azure env the agents use. It prints
exactly the block authors inject.

## Failure modes & troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `token mint failed rc=…` / no `ya29` token | Google OAuth refresh token expired or revoked (shared with the SEO agents) | `bash install/reauth-google-oauth.sh`, or see the `refresh-gsc-token` skill / `install/fix-gsc-now.sh` |
| warning `GA4 pull failed … steering from GSC only` | GA4 property id or token scope | check `data_sources.ga4.property_id` and the token scopes |
| warning `category vocab unavailable` | DSN env unset or DB unreachable | check the DSN env named in `data_sources.db.dsn_env` |
| `topics_emitted` = 0 | no GSC/GA4 evidence matched the vocabulary | check `gsc_rows` / `ga4_rows` in the run metrics |
| Authors not steered | signal older than 72h (producer dead) → `read_demand` returns None | check the instance timer and last run |
| warning `affiliate_clicks failed …` / no WHAT SELLS block | click-table name, column (`spec`, `page_col`, …) or `products_query` wrong, a bad `templates` / `clusters` regex (`bad templates regex '<name>'`) or non-integer cap, or the DSN env is unset. A window with 0 human clicks renders no block by design | run the instance once with `AGENT_FORCE_RUN=1`; compare `conversion_clicks_30d` with `python3 -m framework.cli.human_clicks` for the same table, spec and match |

## Related agents

- **Instances**: `specpicks-search-demand-agent` (`specpicks: agents/search-demand-agent/`),
  `aisleprompt-search-demand-agent` (`aisleprompt: agents/search-demand-agent/`, 2026-11-11).
- **Consumers**: `specpicks-article-proposal-agent` calls
  `demand_signal.read_demand(storage, "specpicks")` and injects
  `build_prompt_block()` above the trends block. The head-to-head agent
  does **not** consume it yet (only the article-proposal agent references
  `demand_signal` as of 2026-09-23). A consumer that only wants the
  purchase evidence calls `demand_signal.build_conversion_block(sig)`, or
  for deterministic ranking `conversion_clicks(sig, "products")` and
  `top_conversion_clusters(sig)`.
- **Shared auth**: `seo-opportunity-agent` collector (`refresh-token.py`,
  `~/.reusable-agents/seo/.oauth.json`).
- **Sibling signal primitives**: `framework/core/seasonal_calendar.py`,
  `framework/core/trends_signal.py`.
