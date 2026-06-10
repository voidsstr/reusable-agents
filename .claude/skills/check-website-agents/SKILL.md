---
name: check-website-agents
description: |
  End-to-end health + value audit of every AislePrompt and SpecPicks
  agent in dependency order. Invoke when the operator says any of:
  "check on the website agents", "check the site agents", "are the
  agents healthy", "audit the agents", "is the pipeline working",
  "why isn't aisleprompt/specpicks publishing", "run the agents and
  verify", or similar. The skill: (1) takes a per-agent snapshot of
  cron status + last-run age + last-run output; (2) for agents that
  HAVEN'T run in the last 3 hours, triggers a fresh run via
  systemctl-start in the correct dependency order, then waits + checks
  output; (3) for agents that HAVE run in the last 3 hours, reads the
  last run's progress.json + recommendations.json + deferred.json +
  per-agent DB impact instead of re-running; (4) verifies output is
  high-quality (no garbage patterns, real recs, DB impact > 0); (5)
  emits site-by-site recommendations grounded in the framework's North
  Star (traffic + quality, not code shipped). Read once at session
  start, then drive the audit from this runbook.
---

# Website-agents health + value audit

You are auditing the production agent fleet for two content sites whose
metrics are the framework's North Star: **AislePrompt** (aisleprompt.com,
recipes + meal-planning) and **SpecPicks** (specpicks.com, retro PC +
gaming hardware). Per CLAUDE.md: success = DAU, organic clicks, indexed
pages, conversions, returning users. NOT code shipped, runs completed,
or tokens consumed.

This audit answers four questions for each agent:

1. **Is it running?** (cron enabled, last run within expected cadence)
2. **Is it producing output?** (recommendations.json non-empty, DB
   impact > 0, no defer-loop, no all-failures)
3. **Is the output valuable?** (recs map to real search intent / real
   user needs, no garbage patterns, no duplicates of last week)
4. **What's blocking it from moving the metric?** (capacity, model
   defer, stale signals, low-quality producer, missing config)

## Agent inventory (audit order — respects dependencies)

The order matters: SEO + GSC inputs feed the analyzers; analyzers feed
content producers; producers feed implementer + image curator. Audit
upstream first so you catch the root cause when downstream is empty.

### Tier 0 — infra / data ingestion (must be healthy first)

| Agent | Both sites? | Purpose | Cadence |
|---|---|---|---|
| `*-scraper-watchdog` | specpicks only | Keeps eBay/Amazon scraper container alive | every 5 min |
| `*-ebay-product-sync-agent` | specpicks | Pulls retro listings from eBay Browse API | every 4h |
| `*-kitchen-scraper` | aisleprompt | Pulls product feeds | every 4h |
| `*-product-hydration-agent` | specpicks | Hydrates products with full specs | event-driven |
| `*-benchmark-research-agent` | specpicks | Pulls Phoronix/OpenBenchmarking data | on-demand |
| `trending-recipe-discovery` | aisleprompt | Trending food queries → /api/admin/recipes/discover-trending | every 6h |
| `gsc-coverage-auditor` | aisleprompt | GSC coverage data per page | every 24h |

### Tier 1 — analyzers (consume Tier 0, emit recs)

| Agent | Purpose | Cadence | Output sink |
|---|---|---|---|
| `*-seo-opportunity-agent` | GSC + GA + DB → SEO recs | every 6h (was 2h) | `agents/<id>/runs/<run-ts>/recommendations.json` |
| `*-progressive-improvement-agent` | Crawls site, finds quality issues | every 6h (was 2h) | same |
| `*-competitor-research-agent` | Compares vs competitors, finds gaps | every 6h | same |
| `*-catalog-audit-agent` | Audits product/recipe catalog | daily | same |
| `*-stale-content-watcher` | Flags stale articles | daily | same |

### Tier 2 — content producers (Opus-only per CLAUDE.md)

| Agent | Purpose | Cadence | Required model |
|---|---|---|---|
| `*-article-proposal-agent` | Proposes new articles | every 8h (was 2h) | **opus** |
| `*-head-to-head-agent` | Head-to-head product comparisons | daily | **opus** |
| `*-news-writer` (specpicks) | News rewrites | daily | **opus** |

### Tier 3 — implementer + post-publish

| Agent | Purpose | Cadence |
|---|---|---|
| `implementer` | Edits code per rec | event-driven via auto-queue-drainer |
| `*-recipe-generator-agent` (aisleprompt) | Generates a recipe per call | every 24h |
| `*-article-hero-image-curator` | Verifies + replaces hero images | every 15 min |
| `*-recipe-image-refiller` (aisleprompt) | Backfills missing recipe images | every 5 min |
| `*-recipe-image-verifier` (aisleprompt) | Verifies recipe images load | every 15 min |
| `*-conversion-optimizer` (aisleprompt) | Suggests UX/funnel improvements | daily |

### Tier 4 — distribution + reporting

| Agent | Purpose | Cadence |
|---|---|---|
| `*-indexnow-submitter` | Pings IndexNow on new/updated URLs | every 15 min |
| `*-indexnow-bulk` | Bulk re-submission | daily |
| `*-site-goals-tracker` | Per-site goal metric snapshot | daily |
| `*-user-growth-strategist` (aisleprompt) | Growth analysis | weekly |
| `*-promo-curator-agent` (aisleprompt) | Promo offer curation | weekly |
| `auto-queue-drainer` | Drains responder auto-queue → implementer | 15s daemon |
| `agent-backlog-dispatcher-agent` | Walks producers' run-dirs, enqueues open recs | every 1 min |
| `agent-responder-agent` | Polls IMAP inbox, dispatches operator replies | every 15 min |

## Phase 1 — collect status (read-only)

Snapshot all agents in one pass. Use the framework storage (Azure) as
source of truth — local file systems are deprecated per CLAUDE.md.

```bash
# Per-agent cadence + last-run-age
systemctl --user list-timers --all | grep -E "agent-(aisleprompt|specpicks)" | \
  awk '{printf "%-50s next=%s\n", $11, $1}'

# Currently-running claude PIDs (active dispatches)
ps -eo pid,etime,cmd | grep "claude --print" | grep -v grep | \
  awk '{printf "PID=%s age=%s\n", $1, $2}'

# Pool state — capacity check
python3 -c "
import json
from datetime import datetime, timezone
s = json.load(open('/home/voidsstr/.reusable-agents/claude-pool/state.json'))
now = datetime.now(timezone.utc)
for pid in sorted([k for k in s if k.startswith('profile-')], key=lambda x: int(x.split('-')[1])):
    info = s[pid]
    auth = 'yes' if info.get('authenticated') else 'NO'
    limits = info.get('limit_resets_at') or {}
    opus = limits.get('opus','')
    msg = 'OPEN' if not opus else f'opus blocked until {opus[:16]}'
    print(f'  {pid} auth={auth} {msg}  {info.get(\"label\",\"\")}')
"
```

For agents that completed in the last 3 hours, read their last-run
artifacts directly from Azure (don't trigger):

```bash
AZURE_CS=$(systemctl --user show reusable-agents-host-worker.service \
  --property=Environment -o cat | tr ' ' '\n' | \
  grep -oP 'AZURE_STORAGE_CONNECTION_STRING="?\K[^"]+')
export AZURE_STORAGE_CONNECTION_STRING="$AZURE_CS" STORAGE_BACKEND=azure

# Per-agent — last 3 runs + status + recs count + deferred
cd /home/voidsstr/development/reusable-agents
python3 <<'PYEOF'
import sys; sys.path.insert(0, '.')
from framework.core.storage import get_storage
s = get_storage()
AGENTS = [
  'aisleprompt-seo-opportunity-agent', 'aisleprompt-progressive-improvement-agent',
  'aisleprompt-article-proposal-agent', 'aisleprompt-competitor-research-agent',
  'aisleprompt-catalog-audit-agent', 'aisleprompt-trending-recipe-discovery',
  'specpicks-seo-opportunity-agent', 'specpicks-progressive-improvement-agent',
  'specpicks-article-proposal-agent', 'specpicks-competitor-research-agent',
  'specpicks-ebay-product-sync-agent', 'specpicks-benchmark-research-agent',
  'specpicks-head-to-head-agent', 'specpicks-catalog-audit-agent',
]
print(f"{'agent':<48} {'last_run':<22} {'status':<10} {'recs':<5} {'deferred?':<10}")
print('-'*100)
for aid in AGENTS:
    runs = set()
    for k in s.list_prefix(f'agents/{aid}/runs/', limit=2000):
        parts = k.split('/')
        if len(parts) >= 4: runs.add(parts[3])
    if not runs:
        print(f"{aid[:48]:<48} {'(no runs)':<22}")
        continue
    latest = sorted(runs, reverse=True)[0]
    base = f"agents/{aid}/runs/{latest}/"
    p = s.read_json(base + "progress.json") or {}
    recs_doc = s.read_json(base + "recommendations.json") or {}
    recs = recs_doc.get('recommendations', recs_doc if isinstance(recs_doc, list) else [])
    d = s.read_json(base + "deferred.json") or {}
    n = len(recs) if isinstance(recs, list) else 0
    defer_reason = str(d.get('reason',''))[:60] if d else ''
    print(f"{aid[:48]:<48} {latest:<22} {p.get('status','?'):<10} {n:<5} {defer_reason}")
PYEOF
```

## Phase 2 — verify DB impact (the real metric)

A successful agent run is meaningless if it didn't move a DB metric.
For each producer-class agent, check what landed:

```sql
-- Articles published per site, last 24h + 7d + 30d
SELECT
  '<site>' AS site,
  COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours') AS d1,
  COUNT(*) FILTER (WHERE created_at > now() - interval '7 days')  AS d7,
  COUNT(*) FILTER (WHERE created_at > now() - interval '30 days') AS d30,
  MAX(created_at) AS most_recent
FROM editorial_articles;

-- Recipe count (aisleprompt only)
SELECT COUNT(*) FROM recipes WHERE created_at > now() - interval '24 hours';

-- Product count (specpicks only)
SELECT COUNT(*) FROM products WHERE updated_at > now() - interval '24 hours' AND is_active;

-- Hero image health
SELECT COUNT(*) FILTER (WHERE hero_image_url IS NOT NULL) AS with_hero,
       COUNT(*) AS total
FROM editorial_articles WHERE created_at > now() - interval '30 days';
```

Source DSNs are in `~/.reusable-agents/secrets.env` as
`DATABASE_URL_AISLEPROMPT` + `DATABASE_URL_SPECPICKS`. Source them via:

```bash
set -a; source ~/.reusable-agents/secrets.env; set +a
python3 -c "
import os, psycopg2
dsn = os.environ['DATABASE_URL_AISLEPROMPT']
with psycopg2.connect(dsn) as c, c.cursor() as cur:
    cur.execute('<query>')
    print(cur.fetchall())
"
```

## Phase 3 — verify output QUALITY (not just quantity)

A 1,000-rec batch can be 1,000 worthless duplicates. For each rec
type, validate against the known patterns:

**Article proposals** — `recommendations.json` entries should have:
- `title` — searchable, evergreen-ish, not a news headline
- `target_keywords` — at least one real long-tail
- `expected_recipe_slugs` + `expected_kitchen_slugs` (aisleprompt) OR
  `expected_product_slugs` (specpicks) — non-empty (else article-link-guard
  rejects at insert time per CLAUDE.md)
- `holiday:<id>` tag when seasonal — required for featured_rotation per
  CLAUDE.md "Article authoring — timely > evergreen"
- NO: release-note titles ("v0.30.4-rc1"), deal clickbait ("Save 72%"),
  bench-news ("RX 9070 GRE Linux Performance"), price-watch ("$315
  surfaces online"), bare-package-version ("datasette-llm 0.1a8")

**SEO recs** — should reference specific URLs + a measurable fix (title
length, missing JSON-LD, missing canonical, broken page). Generic
"improve SEO" recs are noise.

**PI recs** — `affected_url` + `target_files` populated. Without these
the implementer can't act per CLAUDE.md "Implementer path-scope".

**Competitor-research recs** — must include the competitor URL +
the specific feature/page they have that we don't. Aspirational
"build a leaderboard" recs without artifact links are noise.

**Catalog-audit recs** — should name specific product/recipe IDs that
need fixing, not aggregate metrics.

**eBay sync** — should be inserting ~30-200 new listings per run (per
the throttled 8 queries × 30 limit). Zero inserts = scraper / API
issue. Thousands of inserts = throttle didn't apply.

**Trending-recipe-discovery** — output recipes should reference
trending search terms (Google Trends / Reddit hot), have realistic
ingredient lists, not be holiday-only.

**Hero image curator** — should be replacing NSFW / medbay / sci-fi /
small-thumbnail images per CLAUDE.md image rules. Check
`editorial_articles WHERE hero_image_url ILIKE '%spaceship%'` etc.
returns 0.

## Phase 4 — trigger fresh runs only when stale

If an agent's last run is >3h old AND its timer is enabled AND the
agent isn't currently active:

```bash
# Trigger ONE agent
systemctl --user start agent-<full-id>.service

# Watch it run (~5-20 min depending on agent)
journalctl --user -u agent-<full-id>.service -f --since "1 minute ago"
```

Wait for completion (`systemctl --user is-active agent-<id>.service`
returns `inactive` or `failed`), then re-read its latest run-dir from
Azure storage and run Phase 2-3 verification on the fresh output.

**Trigger order** (respects dependencies — don't skip ahead):

1. Infra: `*-scraper-watchdog`, `*-ebay-product-sync-agent`,
   `*-kitchen-scraper`, `gsc-coverage-auditor`, `trending-recipe-discovery`
2. Analyzers: `*-seo-opportunity-agent`, `*-progressive-improvement-agent`,
   `*-competitor-research-agent`, `*-catalog-audit-agent`
3. Content producers: `*-article-proposal-agent`, `*-head-to-head-agent`
4. Post-publish: `*-article-hero-image-curator`, `*-recipe-image-refiller`,
   `*-recipe-image-verifier`, `*-conversion-optimizer`
5. Distribution: `*-indexnow-submitter`, `*-site-goals-tracker`

Skip Tier 3 implementer triggers — it's event-driven. If the producer
ran + emitted recs, the auto-queue-drainer fires it within 60s.

**Don't trigger:** any agent currently rate-limited (check pool state
first). Don't trigger article-proposal when no profile has opus open
— the rec will defer immediately per the implementer's required-model
gate. Wait for opus to free up (`limit_resets_at.opus` in
`claude-pool/state.json`).

## Phase 5 — emit per-site recommendations

Output structure — one block per site. Be concrete and actionable. The
operator should be able to copy-paste a fix from the recommendation.

```
═══ AISLEPROMPT ═══
Health:          <healthy | degraded | broken>
24h articles:    N (target: ≥3)
24h recipes:     N (target: ≥5)
Active issues:   <list>
  • <agent> — <one-line specific issue + suggested fix>
  • …
Goal-tracker:    <which goals stalled, which improving>
Recommendations to move the needle (ranked by impact):
  1. <highest-leverage change> — expected: <metric Δ> over <timeframe>
  2. …
═══════════════════
```

**Recommendation templates** (don't make these up; match patterns
that have worked in prior audits):

- **Cron throttle** — if an agent is firing more than `editorial_articles`
  growth can absorb (e.g. SEO every 2h producing duplicate recs the
  implementer can't keep up with): lower to every 6-8h. See CLAUDE.md
  "Implementer queue — Prioritization" for the levers.

- **Model downgrade** — if an agent uses opus for structured output
  (recipe-generator, catalog-audit, ebay hydration): switch to sonnet
  or local ollama. Frees opus quota for article authoring per CLAUDE.md
  "ARTICLE / NEWS / H2H WRITING — OPUS-ONLY" contract.

- **Short-circuit hook** — if an agent re-runs full work even when its
  inputs haven't changed (no new pages crawled, no new GSC data day):
  add a `signals()` override per CLAUDE.md "Auto short-circuit". The
  SEO + PI agents already have this since 2026-06-09.

- **Producer-side quality gate** — if junk recs reach the queue:
  add a regex tier-0 filter at INSERT time + an LLM tier-2 gate before
  the expensive analysis (qwen3:14b is fine, NEVER opus). See the
  research-trending-topics pattern in specpicks.

- **Throttle per-run budget** — if a single run burns hundreds of LLM
  calls (ebay-sync went 3,500 in 30 min): cap `max_queries_per_run` +
  `per_query_limit` so each run is bounded.

- **Defer-backoff reset** — if recs are stuck in cooldown after a
  capacity issue resolved: `from framework.core import defer_backoff;
  defer_backoff.reset_all('<agent-id>')`.

- **Pool capacity** — if all opus-capable profiles are rate-limited:
  log in another Max account via the `add-claude-max-profile` skill.

- **Site starvation** — if one site has shipped 0–1 articles in 7d
  while the peer ships >10: the `site_starvation_boost` primitive
  auto-boosts the starved site by -2 tiers next drain. Check that it
  fired by reading `framework/defer-backoff/` summaries.

- **Producer-data-source dead** — if Phoronix RSS / Reddit JSON / GSC
  API returns errors: surface to operator + propose alternative
  sources. NEVER let an agent silently produce 0 recs because its
  data source is dead.

- **Goal target_metric not moving** — find the goal in `agents/<id>/`,
  trace which agent owns the metric, recommend a producer-side change
  that ticks it. Per CLAUDE.md North Star: a change that doesn't tick
  a goal didn't happen.

## Phase 6 — re-auditing — use the dashboard

Don't re-run the whole skill blindly. After applying a fix, check the
specific metric the fix targeted:

- Cron throttle → wait one cron-period, check next-run-age + queue size
- Model downgrade → check llm-usage `/api/llm/usage` dashboard,
  confirm provider/model attribution changed
- Quality gate → re-run producer, check rejection log in run-dir
- Backoff reset → check `defer_backoff.summary()` for that agent

Dashboard URL: https://agents.happysky-24190067.eastus.azurecontainerapps.io
- `/llms` — per-agent LLM burn + trust-flagged attribution
- `/agents/<id>` — per-agent run history + status + manifest + override controls
- `/goals` — per-agent goal progress (the North Star)

## Anti-patterns — refuse on sight

- **Running every agent simultaneously** — pool exhaustion within
  minutes. Respect the dependency order in Phase 4. Wait for upstream
  to finish before triggering downstream.
- **Triggering an agent whose timer is disabled** — operator
  intentionally paused it; re-enable explicitly, don't sneak around it.
- **Reporting "agent ran successfully" when the run-dir is empty** —
  status=success + recs=0 means the agent self-skipped (signals()
  short-circuit) OR its analyzer found nothing. Surface either way;
  don't claim success.
- **Recommending the same fix every audit** — track what was already
  applied. If "throttle SEO to 6h" was applied last audit and SEO is
  STILL burning hot, the right answer is a different lever (model
  swap, quality gate, queue priority change), not the same throttle
  again.
- **Skipping the DB-impact check** — only the DB metric tells you the
  agent moved the needle. recs=10 with editorial_articles=0 over 24h
  is a stalled pipeline, not a healthy one.
- **Triggering on a saturated pool** — check `claude-pool/state.json`
  first. If 0 profiles have opus headroom + you trigger article-proposal,
  you've burned a dispatch slot for nothing (it defers).
- **Assuming rename is complete by checking code only** — operator-side
  config in storage (`config/ai-defaults.json` agent_overrides,
  `config/priority-config.json` agent patterns,
  `config/required-models.json` by_agent_id) is keyed by agent_id strings
  that DON'T move when you rename a directory. After any agent rename:
  walk every `config/*.json` in Azure storage, find old agent-id
  references, rename in-place. (2026-06-10: the article-author →
  article-proposal rename missed this for 1 day, sonnet kept getting
  picked because the override didn't match the new id.)
- **Trusting `ai_client().chat()` defaults** — `_ClaudeCliClient` defaults
  `max_turns=1` (one-shot). Long-form prompts (article-proposal, h2h
  generation) MUST pass `max_turns=N` AND `timeout=N` explicitly or
  Claude returns empty after 1 turn and the agent dies "empty output".
  Use site.yaml `claude.max_turns` + `claude.per_call_timeout_s` values.

## Operator-facing summary at the end

Always finish with a short pinned table:

| Site | Pipeline | 24h articles | 7d articles | Top blocker | Top recommendation |
|---|---|---|---|---|---|
| aisleprompt | <status> | N | N | <agent or issue> | <action> |
| specpicks | <status> | N | N | <agent or issue> | <action> |

Plus one line: total wall-clock this audit took, and whether you
triggered any agents (`triggered: 0 / 3 / N` so the operator can see
your blast radius).
