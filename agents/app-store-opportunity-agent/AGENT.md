# App Store Opportunity Agent (`app-store-opportunity-agent`)

> LLM-planned daily scout of the iOS App Store and Google Play for apps that
> are **popular but low-rated** (and apps popular in some markets but absent
> in others), accumulated into a ranked backlog plus a daily operator email.
> This is a research agent for picking the *next* product to build; it does
> not serve an aisleprompt/specpicks site metric directly.

## At a glance

| | |
|---|---|
| Agent id | `app-store-opportunity-agent` |
| Home | `reusable-agents/agents/app-store-opportunity-agent/` (`agent.py`, `_accumulator.py`, `config.yaml`, `manifest.json`) |
| Kind | AgentBase python; blueprint instance (`metadata.blueprint: app-store-opportunity-finder`, `is_blueprint: false`) |
| Schedule | manifest `0 14 * * *` (America/Detroit) → systemd `OnCalendar=*-*-* 14:0:00`, `Persistent=true`; timer **enabled** |
| Entry command | `APP_STORE_OPPORTUNITY_CONFIG=${APP_STORE_OPPORTUNITY_CONFIG:-…/app-store-opportunity-agent/config.yaml} python3 …/app-store-opportunity-agent/agent.py` |
| Category | research |
| Status | live |
| Deep reference | `blueprints/app-store-opportunity-finder/BLUEPRINT.md` (how to clone for another deployment); accumulator schema in `_accumulator.py` top comment; store clients `framework/core/app_store_clients.py`; scoring `framework/core/app_store_scoring.py` |

**Note on the other Markdown in this directory.** `README.md` (titled
"BulkWise"), `product-spec.md`, `ux-research.md`, `ux-spec.md`,
`tech-stack.md`, `api-spec.md`, `data-model.md` and `implementation-plan.md`
are an app **blueprint spec set** (the file set this agent's blueprint prompt
asks for), committed 2026-07-06 in `d89047a` — they are not documentation of
the agent. The untracked `buzzkitchen/` subdirectory is the same kind of
output (see *Failure modes*). `blueprints/` holds only two empty
subdirectories (`mensamax-v2/`, 2026-08-30; `ryuji-kitchen/`, 2026-09-09) of
unverified origin. This file is the agent's runbook.

## What it does

1. **Plan** (`_plan_search`) — one LLM call (`chat_with_fallback`, 1500
   tokens) sees a compact accumulator summary, goal progress and the iTunes
   genre catalog, and returns JSON: `countries` (≤3), `genre_ids` (≤6),
   `search_queries` (≤7), `gap_check.popular_in` (≤3) / `missing_in` (≤4),
   `rationale`. On LLM error or parse failure a fixed fallback plan is used
   (us/gb/de; Productivity, Health, Finance, Shopping, Lifestyle, Education;
   gap us,gb → br,mx,in). Saved as run artifact `search-plan.json`.
2. **Discover**
   - iTunes top charts (`free` + `grossing`, overall 50 + 25 per planned
     genre) in each planned country;
   - each planned query on iTunes **and** Google Play (Play scrape failures
     are logged and skipped);
   - regional-gap check (`discover_country_gaps`): apps charting in the
     `popular_in` markets, looked up per `missing_in` market; hits carry
     `gap_present_in`, `gap_absent_in`, `gap_score`.
3. **Filter** (pre-LLM, cheap) — drop excluded publishers / keywords; hard
   rating gate `min_average_rating ≤ rating ≤ max_average_rating`; then keep
   an app if it is **neglected** (`reviews ≥ neglected_min_reviews` and
   `days_since_update ≥ neglected_min_days_since_update`) or **popular**
   (`reviews ≥ popular_min_reviews`). Capped at `discovery.max_apps_per_run`.
4. **Analyze** — batched LLM scoring, 8 apps per call (4000 tokens):
   `build_complexity` 1–5, `revenue_potential` low/medium/high/very_high,
   `complexity_drivers`, `revenue_thesis`, `decline_thesis`,
   `feature_gaps`, `v2_pitch`, `moats_to_take`. Missing answers default to
   complexity 3 / medium.
5. **Score** — deterministic `opportunity_score` + `rank_signals`
   (`decline_signal`, `install_base`, `monetization`, `simplicity`) weighted
   by `scoring.weights`.
6. **Apply replies + merge** under `framework.core.locks.accumulator_lock`:
   operator replies drained from `responses-queue/` are mapped through the
   newest `runs/*/opp-id-map.json` (`pursue|build|ship opp-NNN` → `pursued`,
   `pass|skip|no opp-NNN` → `passed`); `merge_run` upserts by
   (store, store_id), refreshes snapshot fields, and auto-closes a
   rediscovered open item as `obsolete` on a major-version bump or a rating
   rise of ≥0.5 to ≥4.2. `merge_run` never reopens a closed item (an operator
   reply can still move it). Open items beyond
   `max_open_opportunities` are auto-`passed`. Only apps that passed step 3
   reach `merge_run`, so with `max_average_rating: 4.0` the
   rating-recovered branch cannot fire — only the major-version branch can.
7. **Blueprint** — for up to `blueprints.max_per_run` open items (highest
   revenue/complexity first) without a blueprint newer than
   `regenerate_after_days`, one LLM call (8000 tokens) with
   `docs/reference-app-architecture.md` (first 12k chars) in the prompt
   returns a file map written to storage under `blueprints/<opp-id>/` plus a
   `manifest.json` tagged `rec_type: app-build-from-blueprint`; the item gets
   `build_status: blueprint-ready`.
8. **Email** — top `reporter.email.backlog_cap` open items sorted by
   revenue_potential ÷ build_complexity (then `opportunity_score`, then
   newest), labelled `opp-001…`, sent with `send_via_msmtp(...,
   bypass_digest=True)`.

## Inputs

- Apple iTunes Search / Lookup and top-chart RSS; Google Play pages
  (`framework/core/app_store_clients.py`, polite sleeps between calls).
- Storage: `agents/app-store-opportunity-agent/opportunities/active.json`
  (accumulator), `…/responses-queue/` (parsed email replies), prior
  `…/runs/<ts>/opp-id-map.json`.
- `docs/reference-app-architecture.md` (blueprint prompt).
- LLM via `framework.core.ai_providers.chat_with_fallback(agent_id=…)`, i.e.
  `config/ai-defaults.json` + per-agent overrides. The 2026-09-23 run
  resolved to the `claude-cli` provider with `claude-sonnet-4-6`. The
  config's `ai:` block is **not** read by the code.

## Outputs

| Storage key (under `agents/app-store-opportunity-agent/`) | Content |
|---|---|
| `opportunities/active.json` | accumulator (states open/pursued/passed/obsolete) — also served as the dashboard "opportunities" knowledge bucket |
| `runs/<ts>/search-plan.json` | the planner's JSON |
| `runs/<ts>/opp-id-map.json` | `opp-NNN` → `opportunity_id` for the next run's reply handling |
| `runs/<ts>/email-rendered.html` | email body |
| `outbound-emails/<request_id>.json` | send record (`expects_response: true`, transport, ok) |
| `blueprints/<opp-id>/*.md`, `manifest.json` (or `blueprint-raw.txt` on parse failure) | build spec set |

Email: to/from/account from `config.yaml` `reporter.email`
(`mperry@northernsoftwareconsulting.com` from
`automation@northernsoftwareconsulting.com`, msmtp account `automation`),
subject `App Store Opportunities — <date>`. The email invites
`implement opp-NNN` replies on blueprint-ready items; no code in this repo
handles that reply or `rec_type: app-build-from-blueprint` yet
(`docs/implementer-app-build-contract.md` is marked "spec only").

`RunResult` is always `success` when the run completes; metrics:
`open_opportunities`, `low_complexity_wins` (open, complexity ≤2, revenue
high/very_high), `country_gap_finds` (open, `gap_score > 0`, absent in ≥2
markets), `discovered_this_run` (eligible count), `obsoleted_this_run`
(cumulative `obsolete` count), `email_sent`. The run summary's "N new this
run" is the same eligible count, not the number of newly added items.

## Goals & metrics

Declared in `manifest.json` `goals`; live values 2026-09-23:

| Goal id | Metric | Current | Target |
|---|---|---|---|
| `goal-qualifying-backlog` | `open_opportunities` | 279 | 25 |
| `goal-low-complexity-wins` | `low_complexity_wins` | 0 | 5 |
| `goal-regional-gap-finds` | `country_gap_finds` | 0 | 5 |

## Configuration

`APP_STORE_OPPORTUNITY_CONFIG` points at the YAML (default in the entry
command: this dir's `config.yaml`, currently identical to
`config.example.yaml`). Knobs the code actually reads:

| Key | Code default | `config.yaml` |
|---|---|---|
| `discovery.max_apps_per_run` | 200 | 200 |
| `search.results_per_query` | 25 | 25 |
| `filters.max_average_rating` / `min_average_rating` | 4.0 / 0.0 | 4.0 / 0.0 |
| `filters.neglected_min_reviews` | 500 | 500 |
| `filters.neglected_min_days_since_update` | 180 | 180 |
| `filters.popular_min_reviews` | 50000 | 50000 |
| `filters.exclude_publishers` / `exclude_keywords` | none | Google/Apple/Meta/Microsoft/OpenAI/Anthropic/Amazon; banking, insurance claim, telemedicine, … |
| `scoring.weights` | 0.30 / 0.25 / 0.25 / 0.20 | decline 0.30, install 0.30, monetization 0.20, simplicity 0.20 |
| `max_open_opportunities` | 200 | 300 |
| `blueprints.max_per_run` / `regenerate_after_days` / `tech_stack` | 3 / 90 / RN+Expo, Node+Express+Postgres | same |
| `reporter.email.to` / `from` / `msmtp_account` / `subject_template` / `backlog_cap` | operator pair / 40 | operator pair / 50 |

Present in the YAML but **ignored** by the code: `discovery.countries`,
`discovery.top_charts.*` (countries/genres come from the planner; chart kinds
and limits are hard-coded), `search.queries` (queries come from the planner),
`scoring.build_complexity` / `scoring.revenue_potential` (the prompt is
hard-coded), `ai.*`.

## Short-circuit & idempotency

No `signals()` override — every tick runs (exploration; time is the
signal). The accumulator is keyed by `sha1("<store>::<store_id>")[:16]`, so
rediscovery updates rather than duplicates, and closed states are sticky.

## Running & inspecting

```bash
systemctl --user start agent-app-store-opportunity-agent.service
tail -f /tmp/reusable-agents-logs/agent-app-store-opportunity-agent.log
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/app-store-opportunity-agent/runs?limit=5
# Set blueprints.max_per_run: 0 in a copy of config.yaml to skip the
# expensive blueprint phase; point APP_STORE_OPPORTUNITY_CONFIG at it.
```

## Failure modes & troubleshooting

| Symptom | Evidence / action |
|---|---|
| Blueprint phase burns ~30 min and writes nothing | 2026-09-23 03:20–03:48 UTC: all 3 blueprint calls failed. On the claude-cli provider, claude-pool `profile-4` returned "OAuth session expired", `profile-2` "claude CLI timed out after 600s" and `profile-1` "Reached max turns (1)"; the decision log records each as `blueprint LLM call failed … Connection error.` after the rest of the fallback chain. The run still succeeded (279 open, 2 apps eligible, email sent). A failed call leaves the item without a blueprint, so the same top items are retried next run. Re-auth the pool (`python3 -m framework.cli.claude_pool login-help`) or set `blueprints.max_per_run: 0`. |
| Spec files appear in the repo checkout | `buzzkitchen/` (a spec set for a リュウジのバズレシピ competitor) was written into this directory 2026-09-23 03:42–03:47 UTC, during that blueprint phase; the unit's `WorkingDirectory` is this dir. Likely the claude-cli session writing files instead of returning JSON (unverified). Move such output out of the repo; it is not agent code. |
| `play scrape failed for …` observations | Google Play HTML changed or throttled; iTunes results still flow. |
| `plan … failed — using fallback plan` | LLM unavailable; the run continues with the fixed plan. |
| `regional-gap discovery failed` | logged as an error decision; the rest of the run continues. |
| `email_sent=0` | check `outbound-emails/<request_id>.json` `transport_detail`. |

## Anti-patterns

- ❌ Skipping the eligibility filters and asking the LLM to score every app.
  The heuristic filters are much cheaper.
- ❌ Re-opening a `passed` opportunity because the LLM re-suggested it —
  `merge_run` enforces stickiness; don't bypass it.
- ❌ Shortening the store-client sleeps without a reason.
- ❌ Adding outbound recipients. See `reusable-agents/CLAUDE.md` →
  "Outbound-email recipient policy".

## Related agents

- `responder-agent` — parses email replies into this agent's
  `responses-queue/`.
- `implementer` — intended consumer of blueprint `manifest.json`
  (`app-build-from-blueprint`), not yet implemented.
- Reuse for another deployment: new manifest id + config, per
  `blueprints/app-store-opportunity-finder/BLUEPRINT.md`.
