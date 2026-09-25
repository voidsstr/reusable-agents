# Search-Demand Steering — engine (`search-demand-agent`)

> Turns 28 days of Google Search Console and GA4 data into a compact
> "what to write next" signal: winning page templates, category demand
> lanes, uncovered queries, strike-distance queries and hot head-to-head
> pairs. Author agents inject it into their prompts, which biases new
> content toward proven search demand. North Star: **organic clicks +
> indexed pages** (publish where readers already search and click).

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
| Status | engine only. Live instance: `specpicks-search-demand-agent` (05:35 and 17:35 America/Detroit). No aisleprompt instance as of 2026-09-23 |
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
11. **Publish**: `demand_signal.write_demand(storage, site_id, payload)`
    writes `framework/demand-signal/<site_id>.json` and adds `site` and
    `generated_at`.
12. **Outcome metric**: `steered_published_7d` counts published
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
  round-ups). Consumers ignore a signal older than 72h
  (`DEFAULT_MAX_AGE_HOURS`).
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
`ai_referrals_on_articles`, `ai_demand_top_products`.

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

## Related agents

- **Instance**: `specpicks-search-demand-agent` (`specpicks: agents/search-demand-agent/`).
- **Consumers**: `specpicks-article-proposal-agent` calls
  `demand_signal.read_demand(storage, "specpicks")` and injects
  `build_prompt_block()` above the trends block. The head-to-head agent
  does **not** consume it yet (only the article-proposal agent references
  `demand_signal` as of 2026-09-23).
- **Shared auth**: `seo-opportunity-agent` collector (`refresh-token.py`,
  `~/.reusable-agents/seo/.oauth.json`).
- **Sibling signal primitives**: `framework/core/seasonal_calendar.py`,
  `framework/core/trends_signal.py`.
