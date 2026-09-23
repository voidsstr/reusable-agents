# Feedback Triage Agent (`feedback-triage-agent`) — engine runbook

> Turns reports users file through a site's feedback widget into implementer
> work. It claims each report atomically, filters out test rows, praise and
> vague reports without calling an LLM, and queues real defects as
> `user-feedback-defect` recs. North Star: **conversions and returning users**.
> A defect a real user hit (for example a broken Instacart redirect) costs
> conversions until someone fixes it.

The design narrative (why the status machine exists, why triage is
deterministic, and the first real report it handled) is in
[README.md](README.md). This file is the operational runbook.

## At a glance

| | |
|---|---|
| Agent id | `feedback-triage-agent` (engine default). Runs only as per-site instances: `aisleprompt-feedback-triage-agent`, `specpicks-feedback-triage-agent` |
| Home | `reusable-agents: agents/feedback-triage-agent/` (`agent.py`, `triage.py`) |
| Kind | AgentBase Python **engine**. This dir has no `manifest.json` and is not registered |
| Instances | `aisleprompt: agents/feedback-triage-agent/` and `specpicks: agents/feedback-triage-agent/`, each holding `manifest.json`, `site.yaml` and a `README.md` (no code) |
| Schedule | Engine: never scheduled. Instances: manifest `*/20 * * * *` (America/New_York). systemd `OnCalendar=*-*-* *:0/20:00`, `Persistent=true`, both timers **enabled** (checked 2026-09-23) |
| Entry command | `AGENT_ID=<site>-feedback-triage-agent FEEDBACK_TRIAGE_CONFIG=<site repo>/agents/feedback-triage-agent/site.yaml PYTHONPATH=/home/voidsstr/development/reusable-agents python3 /home/voidsstr/development/reusable-agents/agents/feedback-triage-agent/agent.py`, wrapped by `framework/agent_run_wrapper.sh` |
| Category | `quality` (instance manifests) |
| Status | Both instances live. Every tick on 2026-09-23 claimed 0 reports (no new feedback) |
| LLM | None. Classification is regex and rules in `triage.py` |
| Added | 2026-08-29 (`8215b9a feat: feedback-triage agent - user reports become deployed fixes`) |

## What it does

`FeedbackTriageAgent.run()`, one pass per tick:

1. **Load config.** `__init__` reads the YAML at `$FEEDBACK_TRIAGE_CONFIG`.
   Without it the process exits with `set FEEDBACK_TRIAGE_CONFIG to a site.yaml`.
2. **Connect.** DSN comes from the env var named by `site.yaml: db_env`, with
   `DATABASE_URL` as the fallback.
3. **Select new rows.** For each entry in `site.yaml: sources` it runs
   `SELECT <id> FROM <table> WHERE <status> = <statuses.new> ORDER BY <created> ASC LIMIT n`.
   `n` is what is left of `FEEDBACK_MAX_PER_RUN` across all sources.
4. **Claim atomically.** One row at a time:
   `UPDATE … SET status=<claimed> WHERE id=? AND status=<new> RETURNING …`,
   where `RETURNING` lists whichever of the `id`, `message`, `kind`,
   `severity` and `url` columns the source maps. It is committed immediately. If another worker got the row first, `RETURNING`
   comes back empty and the row is skipped.
5. **Classify** with `triage.classify()`. It runs no DB, network or LLM calls:
   - `ignore`: empty message, or a message matching `TEST_ROW_RE` (for example
     "shape test", "regression-123", "ping", "asdf", "Testing … submission").
   - `not_a_defect`: `kind` is `praise`, `question`, `general_praise` or
     `compliment`, or a feature request (`feature`, `feature_request`, `idea`).
   - `needs_info`: shorter than 15 characters with no quoted error, or no
     failure signal (`ERROR_RE`) and not a bug kind.
   - `investigate`: everything else. Confidence starts at 0.5 and gains
     +0.15 for a bug kind, +0.15 for an error signal, +0.2 for a quoted runtime
     error, +0.1 for a page URL and +0.05 for high/critical/urgent severity.
     It is capped at 1.0.

   `kind` and `severity` are lower-cased first, so the vendored widget's
   `BUG | IDEA | QUESTION | PRAISE` and `LOW | MEDIUM | HIGH` values match.
6. **Write back status and note.**

   | Class | New status |
   |---|---|
   | investigate | `working` |
   | needs_info | `needs_info` |
   | not_a_defect, ignore | `rejected` |

   A `[triage] …` note goes to the `note` column, and `updated` is set to
   `NOW()`, but only when those columns are mapped.
7. **Queue the defects.** When at least one rec was built, it writes
   `{run_ts, mode: "feedback", site, recommendations[]}` to storage key
   `agents/<agent_id>/runs/<run_ts>/recommendations.json`. The doc's own
   `run_ts` is the timestamp used in the rec ids, which is a few seconds later
   than the AgentBase `run_ts` in the key (for example, rec
   `fb-20260830T025702Z-001` lives under `runs/20260830T025658Z/`). It also writes a
   best-effort local copy to
   `~/.reusable-agents/<agent_id>/runs/<run_ts>/recommendations.json`.
8. **Return** `RunResult(status="success")`. The summary looks like
   `N claimed — X queued for the implementer, Y need info, Z not defects`.

## Inputs

| Input | Detail |
|---|---|
| Feedback tables | Defined per site in `site.yaml: sources[]`. aisleprompt reads `"Feedback"` (vendored widget, PascalCase) and `feedback` (older form). specpicks reads `"Feedback"`. The widget schema is at `<site>/vendor/feedback/migrations/001_feedback.sql` |
| DB credentials | Env var named by `db_env` (`DATABASE_URL_AISLEPROMPT` / `DATABASE_URL_SPECPICKS`), falling back to `DATABASE_URL`. Loaded from `~/.reusable-agents/secrets.env` through the unit's `EnvironmentFile=-` |
| Config | `$FEEDBACK_TRIAGE_CONFIG` (site.yaml path) |

## Outputs

| Output | Detail |
|---|---|
| DB writes | Status transitions on the feedback rows, plus the optional note and updated columns. Nothing else is written |
| Recs | `agents/<agent_id>/runs/<run_ts>/recommendations.json`. Each rec has `id` `fb-<UTC ts>-NNN`, `type: user-feedback-defect`, and `priority` `high` when the report's severity is high or critical, otherwise `medium`. It also carries `title`, `rationale` and `recommendation` (fix instructions that ask for a regression test and sibling call-site fixes), plus `evidence{feedback_id,url,severity,kind,confidence}`, `effort: unknown` and `impact: user-reported` |
| Routing | `backlog-dispatcher-agent` (every minute) lists both instance ids in `PRODUCER_AGENT_IDS`. It reads successful runs from `agents/<id>/run-index.json` and queues their `recommendations.json` into the Azure auto-queue. `auto-queue-drainer` then fires the implementer |
| Dispatch metadata | `_subject_tag_from_agent_id()` falls through to subject tag `work`. Under a dispatch-kind allowlist these recs count as `general`. `priority.tier_for_agent()` returned **tier 5** (default) for this id on 2026-09-23, because no tier names `*-feedback-triage-agent` |
| Metrics | `feedback_investigate`, `feedback_needs_info`, `feedback_not_a_defect`, `feedback_ignore`, `feedback_claimed` (floats) |
| Email | None of its own |

Status machine (column values come from each site's `statuses` map):

```
new ──claim──> claimed ──investigate──> working ──(manual)──> done
                  ├── not_a_defect / ignore ──> rejected
                  └── needs_info ────────────> needs_info
```

No code in the pipeline sets `done` (`RESOLVED` / `resolved`). The engine
never reads `statuses.done` at all. A row stays `IN_PROGRESS` / `in_progress`
after the implementer ships the fix. The vendored widget's admin API cannot
close the gap: `PATCH /admin/:id` (`vendor/feedback/server/src/router.ts`)
rejects any status outside the widget's own `NEW | TRIAGED | LINKED | CLOSED`
with `400 invalid_status`, so it can move a row to `CLOSED` but not to
`RESOLVED`. The triage statuses (`TRIAGING`, `IN_PROGRESS`, `NEEDS_INFO`,
`WONT_FIX`, `RESOLVED`) are outside that set. Setting `RESOLVED` takes a
manual SQL `UPDATE`.

## Goals & metrics

No goals are declared. Neither instance manifest has a `target_metric`, and on
2026-09-23 there were no `agents/<id>/goals/` blobs for either instance.
CLAUDE.md requires 3–7 goals per agent, so this is an open gap. Natural
candidates are the metric keys above, for example `feedback_investigate` per
run, or the age of the oldest `new` row.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `FEEDBACK_TRIAGE_CONFIG` | none (required) | Path to the site's `site.yaml` |
| `AGENT_ID` | `feedback-triage-agent` | Storage namespace and run identity. Instance manifests set it inline, and the systemd unit sets it too |
| `FEEDBACK_MAX_PER_RUN` | `20` | Max reports claimed per tick, across all sources |
| `REUSABLE_AGENTS_REPO` | `/home/voidsstr/development/reusable-agents` | Added to `sys.path` for `framework.*` imports |
| `DATABASE_URL` | none | Fallback DSN when the `db_env` var is unset |
| `AGENT_FORCE_RUN` | unset | `1` bypasses the AgentBase short-circuit for one run (see the next section) |

`site.yaml` shape:

```yaml
site: <site>
db_env: DATABASE_URL_<SITE>
sources:
  - table: '"Feedback"'          # quote PascalCase identifiers
    columns: {id, message, kind, severity, url, status, note, created, updated}
    statuses: {new, claimed, working, done, needs_info, rejected}
```

`columns.url`, `severity`, `note` and `updated` are optional: set them to
`null` or leave them out. `statuses.done` is declared by both sites but never
read by the engine. Table and column names are interpolated into SQL as
written, so quote them exactly as Postgres needs.

## Short-circuit & idempotency

- **Idempotency is the status machine.** Only `new` rows are selected, and the
  claim re-checks `status = new`. Re-running a tick cannot double-queue a
  report.
- **`signals()` exists but never short-circuits in practice.** It returns
  `None` while any `new` row is pending and `{"pending": 0}` otherwise.
  However, `run()` returns a `RunResult` with an empty `next_state`, so the
  hash AgentBase computes is never saved (`state/latest.json` held `state: {}`
  on 2026-09-23). Every 20-minute tick therefore runs `run()`, and the log
  shows `0 claimed …` rather than `short-circuited`. The cost is small (a
  COUNT and a SELECT per source, 1–2 s per tick in the log).

## Running & inspecting

```bash
systemctl --user start agent-aisleprompt-feedback-triage-agent.service     # one run now
systemctl --user list-timers | grep feedback-triage
tail -f /tmp/reusable-agents-logs/agent-aisleprompt-feedback-triage-agent.log
curl -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
     http://localhost:8090/api/agents/aisleprompt-feedback-triage-agent/runs?limit=5
```

The engine has no dry-run flag. A manual run claims and updates real rows.

## Failure modes & troubleshooting

| Symptom | Cause / fix |
|---|---|
| Exit 1 in about 1 s, `set FEEDBACK_TRIAGE_CONFIG to a site.yaml` | Env var missing from the entry command |
| Exit, `DATABASE_URL_<SITE> not set` | `secrets.env` is missing or lacks the var. The unit's `EnvironmentFile=-` tolerates a missing file, so the run starts and then dies |
| Rows stuck in the claimed status (`TRIAGING` / `triaging`) | Claims commit one row at a time *before* the write-back loop. A crash between the two leaves claimed rows that are never re-selected, because only `new` is selected. Reset them by hand with `UPDATE … SET status='<new>' WHERE status='<claimed>'` after checking no run is active |
| A real defect was rejected as `ignore` | `TEST_ROW_RE` is anchored to the whole message. One of its patterns is `test` followed by up to 20 more characters, so a short report such as "test the login button" is ignored. Check the `[triage]` note on the row |
| Queued defect never reached the implementer | Check that the run is `success` in `agents/<id>/run-index.json`, that the id is still in `PRODUCER_AGENT_IDS` (`agents/backlog-dispatcher-agent/agent.py`), and whether `config/implementer-allowed-dispatch-kinds.json` restricts `allow` to authoring kinds (these recs count as `general`) |
| Log file missing after a reboot | `/tmp/reusable-agents-logs` is wiped on reboot and the unit recreates it (`ExecStartPre=-/bin/mkdir -p …`) |

## Related agents

- Instances:
  - `aisleprompt: agents/feedback-triage-agent/README.md`
  - `specpicks: agents/feedback-triage-agent/README.md`
- Downstream:
  - `backlog-dispatcher-agent` queues the recs.
  - `auto-queue-drainer.service` drains the queue.
  - `implementer` fixes the defect. The first real report
    (`fb-20260830T025702Z-001`) shipped as aisleprompt commit `809c8f54`.
- Upstream: the site's vendored feedback widget (`<site>/vendor/feedback/`).
