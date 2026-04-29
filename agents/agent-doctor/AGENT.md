# agent-doctor — Continuous Self-Healer

## Purpose

A perpetual ops agent that watches every other agent in the framework
for **failures** and **stuck runs**, investigates the root cause, applies
known fix recipes automatically, and escalates anything novel to the
operator with full context. It also keeps a durable history of every
investigation + fix attempt so the same broken signature never gets
retried in an infinite loop.

## Cadence

Cron: `*/5 * * * *` — every 5 minutes. Most ticks are no-ops (no agents
in failure or stuck). Tick latency under load is dominated by the API
call to list agents.

## Detection

On each tick:
1. Pull `/api/agents` from the framework API.
2. For each agent (excluding self):
   - **failure**: `last_run_status == "failure"` and `(status, last_run_at)`
     is novel (not in `state/seen.json` from a prior tick).
   - **stuck**: `last_run_status == "running"` and the run has been
     running for >30 minutes (or 2× the agent's historical p95 if known —
     not yet implemented).

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
