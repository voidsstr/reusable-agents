# app-store-opportunity-finder — Blueprint

Reusable pattern for an agent that scans the iOS App Store + Google
Play for **apps in decline that still command profitable user bases**,
ranks the find by `revenue_potential / build_complexity`, and
accumulates a dashboard backlog the operator works through over time.

## When to use

You want a daily report of "apps you could clone better." Specifically:

- Targets are existing apps with **decay signals**: stale updates,
  declining or low ratings, but still high install/review counts.
- The operator wants the email **ordered by ease-to-ship** — a
  one-trick pony with 50k angry users beats a complex platform with
  the same reviews.
- The list should **persist** across runs (passed today still passed
  tomorrow), and self-clean when the tracked app's publisher wakes up
  (major version bump or rating recovery).
- The operator works the list async via `pursue opp-NNN` /
  `pass opp-NNN` email replies.

Reference deployment: `agents/app-store-opportunity-agent/`. Drop a new
config file + new manifest id to spin up a second hunting brief.

## What you get

```
agents/<your-agent-id>/
  AGENT.md
  agent.py                # the runtime — usually unchanged
  _accumulator.py
  lib/app_stores.py       # iTunes API + Play scraper
  lib/scoring.py          # opportunity_score weights → rank_signals
  manifest.json
  config.yaml             # YOUR hunt parameters
```

The accumulator stores at:

```
agents/<your-agent-id>/opportunities/active.json
```

with this shape (one entry per unique app, deduped by store + store_id):

```json
{
  "schema_version": "1",
  "agent_id": "...",
  "opportunities": [
    {
      "opportunity_id": "<sha1[:16] of store::store_id>",
      "state": "open" | "pursued" | "passed" | "obsolete",
      "first_seen_at": "<iso>",
      "last_seen_at":  "<iso>",
      "times_seen":    <int>,
      "store":         "ios" | "android",
      "store_id":      "...",
      "name":          "...",
      "publisher":     "...",
      "average_rating": 0.0..5.0,
      "review_count":   <int>,
      "current_version": "...",
      "days_since_update": <int>,
      "build_complexity": 1..5,
      "revenue_potential": "low" | "medium" | "high" | "very_high",
      "v2_pitch": "...",
      "decline_thesis": "...",
      "feature_gaps": [...],
      "moats_to_take": [...],
      "opportunity_score": 0.0..1.0,
      "rank_signals": {
        "decline_signal": 0.0..1.0,
        "install_base":   0.0..1.0,
        "monetization":   0.0..1.0,
        "simplicity":     0.0..1.0
      }
    }
  ]
}
```

## Per-run flow

1. **Search** — iTunes Search API (free, official) + Google Play scrape
   for each `search.queries[]`, country `us` by default.
2. **Filter** — drop low-review, high-rated, recently-updated apps,
   exclude_publishers, keyword excludes.
3. **LLM analysis** — batched (≤8 apps per call). Each app gets:
   `build_complexity` 1-5, `revenue_potential` low|medium|high|very_high,
   `decline_thesis`, `feature_gaps`, `v2_pitch`, `moats_to_take`.
4. **Score** — deterministic weighted mix of decline / install_base /
   monetization / simplicity → `opportunity_score`.
5. **Apply replies** — translate `pursue opp-NNN` / `pass opp-NNN` from
   prior email into accumulator state changes.
6. **Merge** — dedupe by (store, store_id). Closed states never reopen.
   Auto-close if tracked app shipped a major version OR rating recovered.
7. **Cap** — auto-pass anything beyond `max_open_opportunities`.
8. **Email** — top N (default 40) sorted by
   `revenue_potential / build_complexity` desc.

## Configuration

`config.yaml` (a copy of `config.example.yaml`) drives every behavior:

```yaml
search:
  queries:
    - "your seed terms"
  ios_categories: []
  android_categories: []
  countries: ["us"]
  results_per_query: 25
  max_apps_per_run: 80

filters:
  min_review_count: 500
  max_average_rating: 3.9
  min_days_since_update: 365
  exclude_publishers:  [...]
  exclude_keywords:    [...]

scoring:
  weights:
    decline_signal: 0.30
    install_base:   0.25
    monetization:   0.25
    simplicity:     0.20

max_open_opportunities: 200

reporter:
  email:
    to: [mperry@northernsoftwareconsulting.com]
    from: automation@northernsoftwareconsulting.com
    msmtp_account: automation
    backlog_cap: 40
```

## Anti-patterns

- ❌ Asking the LLM to score every app the search returns. Filter
  first; LLM after. The eligibility filters are 100x cheaper.
- ❌ Storing per-run scoring in the run dir but NOT the accumulator.
  Future-you will look at the dashboard, not yesterday's run dir —
  always merge into `opportunities/active.json`.
- ❌ Re-ranking on `opportunity_score` alone. The whole point of this
  agent is `revenue_potential / build_complexity` — that ratio is what
  promotes a low-complexity, high-upside find above a complex
  marketplace clone.
- ❌ Treating Google Play scraping as guaranteed-correct. The HTML
  layout shifts; the scraper falls back to skipping rather than
  crashing. Don't tighten the regex without a fallback.
- ❌ Adding additional outbound recipients beyond the canonical
  operator inbox. See `reusable-agents/CLAUDE.md` → "Outbound-email
  recipient policy".

## Integration points

- **Storage** — Azure Blob via `framework.core.storage`.
- **LLM** — `framework.core.ai_providers.chat_with_fallback` (default
  chain: copilot → azure_openai → openai → anthropic → ollama).
- **Email** — `shared.site_quality.send_via_msmtp` with the
  `automation` msmtp account on the framework host.
- **Responder** — replies parsed by `responder-agent`, surfaced via
  `self.responses` next run; `_apply_responses()` translates them.

## Deploying another instance

Want a second hunting brief — say, "fitness apps with decay signals"?

1. `cp -r agents/app-store-opportunity-agent agents/fitness-app-opportunity-agent`
2. Edit `manifest.json`: change `id`, `name`, `description`,
   `cron_expr` (offset from the base agent so they don't race for
   provider quota).
3. Edit `config.yaml`: tighten `search.queries` to your niche, adjust
   filters.
4. Register: `bash install/register-all-from-dir.sh agents/`
5. Verify in the dashboard: http://localhost:8091/agents/fitness-app-opportunity-agent

The base agent code in `agent.py` + `_accumulator.py` + `lib/` does
NOT need to be duplicated; new instances reuse it via the entry
command and config.
