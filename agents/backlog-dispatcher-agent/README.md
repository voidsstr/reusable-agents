# Backlog Dispatcher (`backlog-dispatcher-agent`)

> Every minute, walks the producer agents' recent `recommendations.json`
> files and hands unshipped recs straight to the implementer, so SEO / PI /
> catalog / article recs keep turning into shipped site changes even when the
> producers themselves are paused. It serves the North Star indirectly: a
> rec that never ships never moves organic clicks or indexed pages.

## At a glance

| | |
|---|---|
| Agent id | `backlog-dispatcher-agent` |
| Home | `reusable-agents/agents/backlog-dispatcher-agent/` (`agent.py`, `manifest.json`, `goals.json`) |
| Kind | AgentBase python (manifest `metadata.blueprint: scheduled-task`) |
| Schedule | manifest `* * * * *` (America/Detroit) → systemd `OnCalendar=*-*-* *:*:00`, `Persistent=true`; timer **enabled** |
| Entry command | `python3 …/agents/backlog-dispatcher-agent/agent.py` (via `framework/agent_run_wrapper.sh`) |
| Category | ops |
| Status | live |
| Runbook | this file (manifest `runbook: README.md`) |

**The manifest description is stale.** It says the agent queues recs into the
responder auto-queue, dedupes by accumulator id and short-circuits when
nothing changed. Since the 2026-05-12 "queue-less dispatch" rewrite the code
reads producer **run-dirs** (not accumulators), calls
`framework.core.dispatch.dispatch_now()` directly (no auto-queue blob,
`fallback_to_queue=False`), dedupes on its own `queued_ids` state, and has
**no** `signals()` short-circuit. The `CLAUDE.md` "Implementer cadence"
table (dispatcher = "producer-side queue feeder") predates this as well; the
`auto-queue-drainer.service` still drains recs other producers/the responder
put in `agents/responder-agent/auto-queue/`.

## What it does (per tick)

1. **Kill stuck scopes** — any running `agent-dispatch-implementer-*.scope`
   whose `/tmp/reusable-agents-logs/dispatch-implementer-*.log` has not been
   written for `BACKLOG_DISPATCHER_STUCK_THRESHOLD_S` (1800 s) gets
   `systemctl --user kill --signal=SIGTERM`.
2. **Count in-flight scopes** (`systemctl --user list-units --type=scope
   --state=running`), then clamp stale claude-pool `in_use` counters in
   `~/.reusable-agents/claude-pool/state.json` down to that count (phantom
   counters left by killed scopes made new work avoid healthy profiles,
   2026-05-13).
3. **Heartbeat** — writes `agents/backlog-dispatcher-agent/state/live-scopes.json`
   (scope names + best-effort `source_agent_id`/`rec_ids` from
   `dispatch-rundirs/*/dispatch-batches.json`) every tick, even when
   throttled, for the Azure dashboard.
4. **Capacity** — `claude_cap = min(BACKLOG_DISPATCHER_MAX_INFLIGHT,
   #claude-pool profiles authenticated and not rate-limited for
   IMPLEMENTER_MODEL_FAMILY)`; `total_cap = claude_cap +
   BACKLOG_DISPATCHER_MAX_COPILOT`. If in-flight ≥ `total_cap` → return
   `throttled` (state preserved).
5. **Walk producers** in the fixed order of `PRODUCER_AGENT_IDS` (see below),
   at most `BACKLOG_DISPATCHER_MAX_PRODUCERS` per tick, re-checking in-flight
   before each. For each producer:
   - list run-dirs from `agents/<aid>/run-index.json` `recent` entries with
     `status == success` whose `runs/<ts>/recommendations.json` exists
     (fallback: `list_prefix`, sorted by embedded `YYYYMMDDTHHMMSSZ`).
     AgentBase keeps only the newest 50 runs in `recent`, so run-dirs older
     than that are not reached while a run-index exists;
   - scan newest-first (up to `BACKLOG_DISPATCHER_RUN_DIR_SCAN` run-dirs) and
     pick the **first run-dir** with eligible recs, collecting up to
     `BACKLOG_DISPATCHER_MAX_PER_PRODUCER` of them.
6. **Rec eligibility** — skipped when any of: `shipped`, `implemented`,
   `deferred`, `duplicate`, `skipped` (added 2026-09-10, `2a40bfa`);
   `review_required` without `confirmed_for_implementation`; handler not in
   `config/implementer-allowed-handlers.json` `allow`; dispatch kind not in
   `config/implementer-allowed-dispatch-kinds.json` `allow`;
   `defer_backoff.should_skip(rec_id, aid)`; dedup key `<run_ts>:<rec_id>`
   or title key (`title:<aid>:<normalized title>[:<affected_url>]`) already
   in `queued_ids`. Candidates are sorted by (severity
   critical→low, `tier == auto` before others).
7. **Dispatch** — materialize the run-dir from blob storage to a temp dir
   (`RunDir.materialize`), pick a backend with
   `implementer_safety.backend_for(first_rec, <subject tag>)` (`claude` or
   `copilot-gpt-4.1`), skip the batch if that backend's cap is 0, then
   `dispatch_now(action="implement", subject_tag=<tag>, request_id=
   "r-<run_ts>-<tag>-<site>", fallback_to_queue=False,
   notify_on_failure=False)`. `dispatch_now()` copies the run-dir to a
   persistent `<AGENT_LOG_DIR>/dispatch-rundirs/rundir-…` for the scope and
   spawns the implementer scope, under the per-site lock unless the tag is in
   `DATA_ONLY_KINDS` (default `catalog-audit,h2h,article-author,product-hydration`;
   of the dispatcher's tags only `catalog-audit` qualifies).
8. **Persist** `queued_ids` and `last_claude_cap` in state. `queued_ids` is
   capped at 2000 entries, but the cap is applied after converting a `set`
   to a list (`list(already | set(queued_now))[-2000:]`), so which entries
   survive is arbitrary, not "the newest 2000" (code reading).

### Producers walked (hard-coded)

`aisleprompt-` / `specpicks-` × `progressive-improvement-agent`,
`seo-opportunity-agent`, `competitor-research-agent`, `catalog-audit-agent`,
`article-proposal-agent`, `feedback-triage-agent`; plus
`specpicks-category-integrity-agent`. Adding a producer means editing
`PRODUCER_AGENT_IDS` (the goal directive "no hardcoded producer lists — walk
the registry" is not met yet).

### Producer → dispatch kind / subject tag

Derived from substrings of the producer id: `seo-opportunity`→`seo`,
`progressive-improvement`→`pi`, `competitor-research`→`cr` (subject tag) /
`comp-research` (dispatch kind), `catalog-audit`→`catalog-audit`,
`head-to-head`→`h2h`, `article-author`→`article`/`article-author`; anything
else → subject tag `work`, dispatch kind `general`. A rec's own
`dispatch_kind` overrides the dispatch-kind check only. Note (code reading):
the `*-article-proposal-agent` ids do not contain `article-author`, so their
recs map to `work`/`general` unless the rec sets `dispatch_kind`, and neither
proposer's recs (`type: article-author-proposal`) set it. Effects: under a
restricted dispatch-kind allowlist they are held as `general`; the scope gets
`subject_tag=work` (the implementer's `run.sh` passes it as
`--subject-tag`) and takes the site lock that `article-author` would skip.
Whether `work` changes how the implementer handles these recs is
unverified.

## Inputs

- Storage: `agents/<producer>/run-index.json`,
  `agents/<producer>/runs/<ts>/recommendations.json` (+ the rest of the
  run-dir for materialization), `config/implementer-allowed-handlers.json`,
  `config/implementer-allowed-dispatch-kinds.json`,
  `framework/defer-backoff/<source_agent_id>.json` (via `defer_backoff`).
- Host: systemd `--user` scope list, `/tmp/reusable-agents-logs/`
  (`dispatch-implementer-*.log`, `dispatch-rundirs/`),
  `~/.reusable-agents/claude-pool/state.json`.

## Outputs

- Implementer scopes (`agent-dispatch-implementer-*.scope`) via
  `dispatch_now()`. The dispatcher's own temp copy of the run-dir is removed
  on a failed dispatch but not on success.
- `agents/backlog-dispatcher-agent/state/live-scopes.json` heartbeat.
- Writes to the claude-pool `state.json` (`in_use` clamp only).
- Decisions: `dispatched`, `action` (auto-killed scopes), `observation`
  (throttle, pool-health change, backend-cap skip), `error`.
- `RunResult` is always `success` unless the run crashes. Summaries:
  `dispatched N rec(s) across M producer(s) directly to implementer (no
  queue)`, `no producer had unshipped recs in run-dirs`, or
  `throttled: N scope(s) in flight ≥ cap X` — **X prints
  `BACKLOG_DISPATCHER_MAX_INFLIGHT` (1), not the effective `total_cap`**
  (default 1 + 3 = 4), which is what actually throttled.
- Metrics: `producers_dispatched`, `recs_dispatched`, plus `inflight_after`
  on dispatch or `inflight` + `throttled` on throttle.
- Run-summary mail: `agent_base._NO_OP_SUMMARY` treats both the "no producer
  had unshipped recs" and `throttled:` summaries as no-ops, so only
  dispatching ticks reach the daily digest. A crashed run takes the
  direct-send path, which the shared `DIGEST_ONLY` gate (default on) also
  folds into the digest.

## Goals & metrics (`goals.json`)

| Goal id | Metric | Target | Live 2026-09-23 |
|---|---|---|---|
| `goal-recs-dispatched-flow` | `recs_dispatched` | 20 | 0 |
| `goal-producer-coverage` | `producers_dispatched` | 5 | 0 |
| `goal-dispatch-liveness` | `recs_dispatched` | 1 | 0 |

The goal units say "/day", but each run records its own per-tick count, so
"current" is just the last tick (usually 0 — most ticks find nothing).

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `BACKLOG_DISPATCHER_MAX_PER_PRODUCER` | 10 | recs per producer per tick |
| `BACKLOG_DISPATCHER_MAX_PRODUCERS` | 10 | producers dispatched per tick |
| `BACKLOG_DISPATCHER_MAX_INFLIGHT` | 1 | claude-channel scope cap (before pool auto-throttle) |
| `BACKLOG_DISPATCHER_MAX_COPILOT` | 3 | copilot-gpt-4.1 channel scope cap |
| `BACKLOG_DISPATCHER_STUCK_THRESHOLD_S` | 1800 | stuck-scope kill threshold |
| `BACKLOG_DISPATCHER_RUN_DIR_SCAN` | 200 | run-dirs scanned per producer |
| `BACKLOG_DISPATCHER_MAX_QUEUE_DEPTH` | 30 | read but **unused** (queue-era leftover) |
| `IMPLEMENTER_MODEL_FAMILY` | `sonnet` | family used for the pool-health cap |
| `AGENT_LOG_DIR` | `/tmp/reusable-agents-logs` | where `dispatch-rundirs/` is read (stuck-scope sweep hard-codes the default path) |

Storage configs (operator levers):

- `config/implementer-allowed-dispatch-kinds.json` — `{"allow": ["*"]}` on
  2026-09-23. History in the blob: restricted to
  `article-author`/`news-author`/`news-rewrite` on 2026-05-24 for an
  operator editing session; lifted 2026-06-01 after the 8-day pause starved
  SEO/PI/catalog-audit shipping (organic clicks 75 → 1, indexed_pct flat at
  4 %). See `CLAUDE.md` → "Dispatch-kind pause".
- `config/implementer-allowed-handlers.json` — see *Failure modes*.

The producer `site.yaml` `auto_implement` flag is deliberately **ignored**
here since 2026-05-13 (`if False and …`); the classifier + caps are the
safety net.

## Short-circuit & idempotency

No `signals()` override (the old `(producer, total_open)` hash froze the
dispatcher while shipped recs were still in flight). Idempotency comes from
`queued_ids` (bounded to 2000 entries, preserved on throttled ticks — losing
it once caused a rec to be re-dispatched ~50× in 2 h) and from producers /
the implementer marking recs `shipped`/`deferred`/`skipped`.

## Running & inspecting

```bash
systemctl --user list-timers | grep backlog-dispatcher
tail -f /tmp/reusable-agents-logs/agent-backlog-dispatcher-agent.log
systemctl --user list-units --type=scope --state=running | grep agent-dispatch-implementer-
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/backlog-dispatcher-agent/runs?limit=20

# Pause / resume dispatching (per CLAUDE.md)
systemctl --user stop agent-backlog-dispatcher-agent.timer
systemctl --user enable --now agent-backlog-dispatcher-agent.timer

# Clear a stuck defer cooldown for one producer (needs the Azure storage env)
set -a; . ~/.reusable-agents/secrets.env; set +a
cd /home/voidsstr/development/reusable-agents && STORAGE_BACKEND=azure \
  python3 -c "from framework.core import defer_backoff; print(defer_backoff.reset_all('<producer-agent-id>'))"
```

There is no dry-run flag; running `agent.py` by hand dispatches for real.

## Failure modes & troubleshooting

| Symptom | Evidence / action |
|---|---|
| `azure read_bytes config/implementer-allowed-handlers.json: … BlobArchived` on every non-throttled tick | 2026-09-23 log (starts 03:11 UTC): ~2,580 occurrences by 18:06 UTC, on 853 of 897 ticks (all but the throttled ones). The read happens once per candidate rec. The blob is in the Azure archive tier; the read throws, the exception is swallowed and **no handler restriction applies**. Harmless but noisy; rehydrate or rewrite the blob if a handler allowlist is ever needed. |
| `no producer had unshipped recs in run-dirs` most ticks | Normal when caught up (824 of 896 completed ticks on 2026-09-23, 03:11–18:05 UTC; 28 ticks dispatched 1–10 recs). If producers clearly hold open recs, check the skip filters above (review_required, defer-backoff, dispatch-kind allowlist). |
| `throttled: N scope(s) in flight ≥ cap 1` | Effective cap was reached (see the summary caveat). 44 such ticks on 2026-09-23 with 4–6 scopes running. |
| Recs re-dispatched forever | Historically caused by missing terminal flags (`deferred` 2026-05-13, `skipped` 2026-09-10) and by losing `queued_ids` on throttle. Check the rec's flags in its `recommendations.json`. |
| Newest recs never seen | Fixed 2026-05-24 by walking `run-index.json` instead of the 10K-key-capped `list_prefix`. If a producer has no run-index, the fallback can still miss new runs. |
| Claude work held back | `claude_cap` drops to the count of healthy pool profiles; at 0 only `copilot-gpt-4.1` recs dispatch. Re-auth the pool (`python3 -m framework.cli.claude_pool login-help`). |

## Not part of the agent

`insert_articles.py` and `insert_articles_005_006.py` are one-off SpecPicks
`editorial_articles` insert scripts committed here on 2026-05-20 (`60981ef`).
Nothing imports or schedules them, and they embed a hard-coded database
connection string. Treat them as stray artifacts.

## Related agents

- Producers listed above (upstream).
- `implementer` — runs in the spawned scopes; marks recs shipped/deferred/skipped.
- `responder-agent` + `auto-queue-drainer.service` — the parallel,
  queue-based path for email replies and producer self-dispatch.
- `framework/core/dispatch.py`, `implementer_safety.py`, `defer_backoff.py`,
  `run_dir.py` — the primitives this agent composes.
