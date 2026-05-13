# app-store-opportunity-agent

Daily cron that scans the iOS App Store + Google Play for **apps in
decline that still have profitable user bases** — the "build a better v2,
take their lunch" signal — and accumulates them into a dashboard +
operator email that grows over time.

## What this agent does

Each run:

1. Searches the configured queries (and optional category walks) on
   both stores.
2. Filters out: low-review-count apps (no signal), high-rated apps
   (their users are happy), recently-updated apps (publisher is
   active), mega-publishers (Google/Apple/Meta/Microsoft), and any
   keyword-excluded categories (default: banking, insurance,
   telemedicine — domains we don't want to clone).
3. Asks the LLM to score each surviving app on **build_complexity (1-5)**
   and **revenue_potential (low|medium|high|very_high)** and to write a
   "v2 pitch", decline thesis, and feature-gap list.
4. Computes a deterministic `opportunity_score` from rating decay,
   review-count saturation, monetization presence, and inverse build
   complexity.
5. Merges into the cross-run accumulator at
   `agents/<id>/opportunities/active.json`. Closed states (pursued /
   passed / obsolete) never re-open.
6. Auto-closes opportunities whose tracked app has shipped a major
   version since first_seen, or whose rating recovered above 4.2.
7. Emails the top N (default 40, configurable) sorted by
   `revenue_potential / build_complexity` so the simplest, most
   lucrative wins appear first.

## Per-run flow

```
search → filter → llm-batched-scoring → opportunity_score
       → apply-operator-replies → merge-accumulator
       → cap-open-list → render-email → send-via-msmtp
```

The accumulator file shape is documented in `_accumulator.py`
top-comment.

## Operator workflow

Daily email arrives with up to 40 top-ranked opportunities, each labeled
`opp-001`…`opp-040`. Reply with one or more lines like:

```
pursue opp-003
pass opp-007 opp-012
```

On the next run those entries flip to state `pursued` (operator is
building it — tracked elsewhere) or `passed` (never resurface).

## Configuration

`config.example.yaml` documents every knob. Highlights:

| Knob | Default | Why |
|---|---|---|
| `search.queries` | (5 sample queries) | Seed terms — broaden by category over time |
| `filters.min_review_count` | 500 | Below this, signal is noise |
| `filters.max_average_rating` | 3.9 | The whole point — we want LOW ratings |
| `filters.min_days_since_update` | 365 | Stale-only |
| `filters.exclude_publishers` | Google/Apple/Meta/MS | Can't compete with mega-publishers |
| `scoring.weights` | balanced | Tune to bias toward your strengths |
| `max_open_opportunities` | 200 | Cap on active backlog |
| `reporter.email.backlog_cap` | 40 | Email truncation; full list on dashboard |

## Inputs

- `APP_STORE_OPPORTUNITY_CONFIG=path/to/config.yaml` — YAML config
- Storage: read/write `agents/<agent_id>/opportunities/active.json`
- LLM: any chat provider via `framework.core.ai_providers`

## Outputs

- `agents/<agent_id>/opportunities/active.json` — accumulator
- `agents/<agent_id>/runs/<ts>/email-rendered.html` — sent email body
- `agents/<agent_id>/runs/<ts>/opp-id-map.json` — opp-NNN → opportunity_id
  map used by the next run to apply operator replies
- Outbound mail recorded via `record_outbound()` for the
  Confirmations page

## Goals

See `manifest.json` `goals` block. Three goals:

- `discover-opportunities` — keep growing the open backlog
- `low-complexity-wins` — bias surfacing toward build_complexity ≤ 2 +
  revenue_potential ≥ high
- `fresh-signals-only` — refresh ratings every run, auto-close when the
  tracked publisher ships a major update

## Anti-patterns

- ❌ Skipping the eligibility filters and asking the LLM to score every
  app. Wastes tokens; the heuristic filters are MUCH cheaper.
- ❌ Re-opening a `passed` opportunity because the LLM re-suggested it.
  `merge_run` enforces stickiness — don't bypass it.
- ❌ Using the Play scraper without a backoff. The `search_play()` helper
  sleeps between pages; don't shorten without a reason.
- ❌ Hardcoding additional outbound recipients. See
  `reusable-agents/CLAUDE.md` → "Outbound-email recipient policy".

## Reusing this agent for another deployment

This whole agent is a blueprint — drop a new config file and a new
manifest with a different `id` and you've got a second instance.
See `blueprints/app-store-opportunity-finder/BLUEPRINT.md`.
