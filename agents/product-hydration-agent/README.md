# product-hydration-agent

Batch product-catalog content hydration and Amazon price refresh. This
replaces request-time AI content generation (Ollama / OpenAI called per HTTP
request) with a precompute-and-cache model. Each run:

1. refreshes stale Amazon prices and offers through a configurable provider;
2. picks the highest-priority products that need content, asks Claude for
   SEO-oriented JSON, validates it, and writes it back to the `products`
   table;
3. optionally refreshes the site's `is_featured` set.

Runtime endpoints read the columns directly, so no generator sits in the
request path.

**Operator runbook** (phases, config keys and defaults, metrics, failure
modes): [AGENT.md](AGENT.md).

## Why

The legacy generators had four problems:
1. **Latency.** Request-time LLM calls took 5-30s per page load.
2. **Quality drift.** Every request got a different generation, so there was
   no canonical, citable content.
3. **Cost.** Ollama runs locally so it's "free", but the OpenAI fallback was
   per-request and unpredictable.
4. **No quality bar.** Whichever provider answered first won, even if its
   output was generic boilerplate.

This agent addresses all four:
- content is precomputed, so there is no request-time wait;
- there is one canonical version per `stale_after_days` window;
- each product takes one `claude --print` call on the Claude Max
  subscription (through the claude-pool shim when present);
- output is validated per type before it's written. The prompt demands
  exactly 5 pros, 5 cons and 5 FAQ pairs, plus 60/160-char SEO caps. The
  validators trim overruns: at most 5 bullets of 100 chars or less, title
  cut to 60, meta cut to 160, a `short_meta_description` flag when meta is
  under 120. They reject a type only when nothing usable came back. See the
  validation table in AGENT.md.

## Per-site instance pattern

The code lives here in reusable-agents. Site repos add a thin shell:
`manifest.json`, `site.yaml` and a `run.sh` that exports
`PRODUCT_HYDRATION_CONFIG=…/site.yaml` and execs this `agent.py`. This
directory's own `manifest.json` is a blueprint (`is_blueprint: true`, no
cron). It is never registered or scheduled.

As of 2026-09-23 the only instance is `specpicks-product-hydration-agent`
(`specpicks: agents/product-hydration-agent/`), which runs every 2 hours at
:15.

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Main entrypoint (`ProductHydrationAgent`, subclasses `AgentBase`). |
| `paapi_client.py` | Amazon PA-API v5 client (SigV4, `GetItems`) and response parser. |
| `brightdata_client.py` | Bright Data Amazon Products dataset client (trigger, poll, download) and record parser. |
| `prompts/hydrate_product_system.md` | Claude system prompt with the hydration goals and strict output schema. |
| `manifest.json` | Blueprint manifest (never scheduled itself). |
| `config.example.yaml` | Annotated site.yaml template. It predates the `price_refresh:` block, which is documented in AGENT.md. |
| `AGENT.md` | Operator runbook. |

The Amazon Creators API client is the shared framework primitive
`framework/core/amazon_creators.py`, not a file in this directory.

## Inputs

- `PRODUCT_HYDRATION_CONFIG` (env var): path to site.yaml.
- `DATABASE_URL` (or the env var named by `database.url_env`): Postgres DSN.
- `claude` CLI on PATH.
- Price provider credentials (env var names only): `AMAZON_CREATORS_*`,
  `AMAZON_PAAPI_*`, or `BRIGHTDATA_API_KEY`.
- Optional `FEATURED_SELECT_SCRIPT`: site-specific featured-set script.

## Outputs

Per run, in framework storage at `agents/<id>/runs/<run-ts>/`, mirrored to
`<runs_root>/<site.id>/<run-ts>/` on local disk:
- `results.json`: totals, success rate, per-content-type breakdown, model,
  coverage, and the price-refresh summary (`amazon_paapi`, whatever the
  provider).
- `hydration-log.jsonl`: one line per product processed.
- `llm-output.txt`: sample of Claude raw outputs (50 KB or less).
- `context-summary.md`: narrative for the next run.
- `goal-progress.json`: % catalog fully hydrated, % stale, price-refresh
  counts.

DB writes (`products`):
- `description` (TEXT), `pros_cons` / `faq` / `seo_meta` (JSONB): only the
  requested types that pass validation.
- `hydrated_at` (TIMESTAMPTZ) and `hydration_model` (e.g. `claude-opus`):
  stamped on every hydration UPDATE.
- Price phase: `price`, `original_price`, `currency`, `availability`,
  `is_prime`, `rating`, `review_count`, `main_image_url`, `amazon_url`,
  `brand` (only if empty), `raw_amazon_data`, `price_updated_at`,
  `last_fetched_at`; plus `fetch_attempts` / `fetch_error` on misses.
