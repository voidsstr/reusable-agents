# agent-doctor — Event-Driven Self-Healer

## Purpose

An ops agent that watches every other agent in the framework for
**failures** and **stuck runs**, investigates the root cause, applies
known fix recipes automatically, and escalates anything novel to the
operator with full context. It also keeps a durable history of every
investigation + fix attempt so the same broken signature never gets
retried in an infinite loop.

## Invocation Model

**Event-driven** — no cron. The doctor is auto-invoked by
`framework.core.resilience.invoke_doctor()` whenever:

- An `AgentBase` agent's `run_once()` returns `RunResult(status="failure")`
  or raises an unhandled exception (wired into the post-run hook).
- The host-worker's wall-clock watchdog kills a stuck agent (timeout).
- The host-worker's EXIT trap fires because a worker bash crashed before
  its agent posted a terminal state.
- A non-AgentBase agent (shell-only) exits with non-zero rc.

Each failure path writes an incident to
`agents/agent-doctor/incidents/<incident_id>.json` (durable storage)
**and** drops a job file into `/tmp/agent-trigger-queue/agent-doctor-*.json`
that the host-worker picks up to exec the doctor. Same trigger mechanism
the dashboard's "Run now" button uses, so the queue doesn't care where
the request came from.

The doctor is also runnable **manually** from the dashboard for ad-hoc
fleet sweeps.

Dedupe protects against tight failure loops: same `(failed_agent_id,
error_class)` inside a 10-minute window enqueues only one doctor run.

## Detection

On each invocation:
1. **Drain the incident queue** at `agents/agent-doctor/incidents/` —
   each entry names a specific agent that just failed plus full error
   context (class, message, traceback, log path). These are processed
   first. Drained entries archive to `incidents-processed/`.
2. **Broad poll** `/api/agents` from the framework API. For each agent
   (excluding self):
   - **failure**: `last_run_status == "failure"` and `(status, last_run_at)`
     is novel (not in `state/seen.json` from a prior tick).
   - **stuck**: `last_run_status == "running"` and the run has been
     running for >30 minutes (or 2× the agent's historical p95 if known —
     not yet implemented).
   - **incident-flagged**: any agent with a queued incident, regardless
     of API status (the failing agent posts the incident before
     `status.json` flips, so the API may show stale data).

## Investigation

For each detected target, agent-doctor:
1. Reads up to 32 KB of the most recent agent log from
   `/tmp/reusable-agents-logs/agent-<id>.log` (or `<id>-*.log`).
2. Pulls live status from `/api/agents/<id>/status`.
3. Classifies the failure into one of these signatures:
   - `oauth-token-expired` — 401 / invalid_token / TokenExpired in log
   - `imap-auth-failed` — IMAP auth-specific failures
   - `transient-network` — connection refused/reset/DNS/timeout
   - `python-traceback` — generic Python exception
   - `missing-file` — FileNotFoundError
   - `schema-drift` — UndefinedColumn / KeyError patterns
   - `missing-import` — ImportError / ModuleNotFoundError
   - `oom` — MemoryError
   - `missing-env-<var>` — heuristic match on "X is required" / "must be set"
   - `timeout` — systemd timeout signal
   - `stale-lock` — lock-file phrasing in log
   - `unknown` — fallback (always escalates)

## Recipes (auto-fix)

Recipes are intentionally narrow + safe-by-default:

| Signature | Recipe | Action |
|---|---|---|
| `oauth-token-expired` | mint fresh token | runs `responder-agent/mint-token.py --refresh` |
| `transient-network` | no-op | next cron firing retries — no action needed |
| `stale-lock` | cleanup | removes `/tmp/agent-<id>*.lock` if mtime > 1h |
| `timeout` | escalate | does NOT silently extend timeouts — emails operator |
| Everything else | escalate | emails operator with full diagnosis |

**No code edits are ever applied automatically.** Schema migrations,
agent.py changes, dependency bumps, etc. always go to email.

## Loop prevention

`fixes-log.jsonl` is the durable record. If the same `(target_agent,
error_signature)` has been attempted 2 times without producing a `fixed`
outcome, agent-doctor stops trying and emails the operator with a
"loop-broken" tag so the human knows to step in.

A successful fix (`outcome: "fixed"`) resets the counter — if the
signature reappears later, it will be retried fresh.

## Storage layout

```
agents/agent-doctor/
  manifest.json
  state/
    seen.json                     # {agent_id: "<status>@<last_run_at>"}
  fixes-log.jsonl                 # one entry per investigation, append-only
  runs/<run-ts>/
    progress.json
    decisions.jsonl
    errors.json
```

## Manual trigger

```bash
FRAMEWORK_API_URL=http://localhost:8093 \
FRAMEWORK_API_TOKEN=<token> \
python3 /home/voidsstr/development/reusable-agents/agents/agent-doctor/agent.py
```

## Adding a recipe

1. Add a `recipe_<signature>(target, ctx) -> (outcome, notes)` function
   in `agent.py`. Outcome must be `"fixed"`, `"no-op"`, `"escalated"`,
   or `"skipped"`.
2. Register it in the `RECIPES` dict.
3. Update the classifier in `_classify_error` if a new signature is
   needed.
4. Test by triggering a failing run that produces the matching log
   pattern, then run agent-doctor manually and confirm the recipe
   fires.

Recipes must be **idempotent** — agent-doctor may attempt the same
recipe multiple times in a single tick if multiple agents fail with
the same signature.
