# product-hydration-agent

Daily, batch product-catalog content hydration. Replaces request-time
AI content generation (Ollama / OpenAI per HTTP request) with a
precompute-and-cache model: this agent runs nightly, picks the most
popular products that need fresh content, asks Claude to produce
SEO-optimized JSON, validates it, and writes the result back to the
products table. Runtime endpoints read directly from DB — no
generators in the request path.

## Why

The legacy generators had four problems:
1. **Latency** — request-time LLM calls took 5-30s per page load.
2. **Quality drift** — every request got a different generation;
   no canonical, citable content.
3. **Cost** — Ollama runs locally so it's "free", but the OpenAI
   fallback was per-request and unpredictable.
4. **No quality bar** — whichever provider answered first won, even
   if its output was generic boilerplate.

This agent fixes all four:
- precomputed (no request-time wait);
- one canonical version per `stale_after_days` window;
- one Claude Max-plan call per product (covered by subscription);
- strict JSON validation (pros_cons must have 5+5, faq must have ≥1
  Q&A, seo_meta must respect 60/160 char caps, etc.).

## Per-site instance pattern

Code lives here in reusable-agents. Customer repos add a thin shell
with manifest + site.yaml + run.sh; the manifest's `entry_command`
exports `PRODUCT_HYDRATION_CONFIG=...site.yaml` and exec's this
`agent.py`. See `specpicks/agents/product-hydration-agent/` for the
canonical instance.

## Files

| File | Purpose |
|------|---------|
| `agent.py` | Main entrypoint (subclasses `AgentBase`). |
| `prompts/hydrate_product_system.md` | Claude system prompt — embeds the goals + strict output schema. |
| `manifest.json` | Blueprint manifest (never scheduled itself). |
| `config.example.yaml` | Annotated site.yaml template. |
| `AGENT.md` | Operator runbook (how a run flows + DB schema requirements). |

## Inputs

- `PRODUCT_HYDRATION_CONFIG` (env var) — path to site.yaml.
- `DATABASE_URL` (or env var named by `database.url_env`) — Postgres DSN.
- `claude` CLI on PATH — uses Claude Max subscription, no API cost.

## Outputs

Per run, in framework storage at `agents/<id>/runs/<run-ts>/`:
- `results.json` — totals, success rate, per-content-type breakdown, model.
- `hydration-log.jsonl` — one line per product processed.
- `llm-output.txt` — sample of Claude raw outputs (≤50 KB).
- `context-summary.md` — narrative for next run.
- `goal-progress.json` — % catalog hydrated, % stale.

DB writes:
- `products.description` (TEXT) — when `description` requested
- `products.pros_cons` (JSONB) — when `pros_cons` requested
- `products.faq` (JSONB) — when `faq` requested
- `products.seo_meta` (JSONB) — when `seo_meta` requested
- `products.hydrated_at` (TIMESTAMPTZ) — stamped on every UPDATE
- `products.hydration_model` (VARCHAR) — e.g. `claude-opus`
