# {{AGENT_NAME}} — Runbook

> {{AGENT_DESCRIPTION}}

## What this agent does

_(One paragraph: the goal of this agent, the systems it operates on,
the users it affects.)_

## Schedule

- Cron: `{{AGENT_CRON}}` (`{{AGENT_TIMEZONE}}`)
- Triggered by:
  - The framework's systemd `--user` timer (auto-wired at registration)
  - Manual: `curl -X POST http://localhost:8090/api/agents/{{AGENT_ID}}/trigger`
  - UI: http://localhost:8091/agents/{{AGENT_ID}} → "▶ Run now"

## Inputs / Outputs

**Reads:**
- _(External APIs, files, databases, credentials)_

**Writes:**
- _(Files / blobs / DB rows / git commits this agent produces)_
- Always: `agents/{{AGENT_ID}}/runs/<run-ts>/{progress,errors,decisions,context-summary}.{json,jsonl,md}` in the framework storage backend.

## Per-run flow

1. _(What pre_run does — load credentials, drain response queue, etc.)_
2. _(The main `run()` work — step by step)_
3. _(What post_run does — write summary, update goals)_

## Hard gates / guardrails

_(Any operations that should never happen without confirmation. Methods
gated with `@requires_confirmation` should be listed here. The framework's
Confirmations UI will surface pending requests.)_

- Example: `@requires_confirmation(reason="deploys to production Azure")`
  on `deploy_to_azure()` — the agent emails the owner; nothing ships
  until the owner replies "yes".

## State carried between runs

State written by the agent persists at:
- `agents/{{AGENT_ID}}/state/latest.json` — the most recent state object
- `agents/{{AGENT_ID}}/state/history/<run-ts>.json` — historical snapshots

The framework auto-loads `latest.json` into `self.state` at the start of
each run via `pre_run()`. Update `RunResult.next_state` in `run()` to
persist for the next pass.

## Decisions to log

`self.decide("plan", "...")` — what the agent intends to do this run
`self.decide("observation", "...")` — something noteworthy it noticed
`self.decide("choice", "...", evidence=...)` — why it chose option A over B
`self.decide("skip", "...")` — what it deliberately didn't do
`self.decide("defer", "...")` — work pushed to a future run
`self.decide("warning", "...")` — a caveat for the next run
`self.decide("result", "...")` — outcome of an action

These render to `runs/<run-ts>/decisions.jsonl` and into the next-run
context summary the agent uses to learn over time.

## Goals + success criteria

_(Optional — define measurable outcomes the agent works toward.
Goals live in `agents/{{AGENT_ID}}/goals/current.json` and get scored
against the next run's snapshot.)_

```json
{
  "goals": [
    {
      "id": "example-goal",
      "description": "Move metric X from Y to Z",
      "target_metric": "...",
      "baseline": 0,
      "target": 100,
      "check_by": "+4-weeks"
    }
  ]
}
```

## Operational notes

- _(Anything an operator (human) needs to know about running, debugging,
  or recovering this agent.)_
- _(Logs are at `/tmp/reusable-agents-logs/{{AGENT_ID}}-*.log` if invoked
  via host-worker; otherwise wherever the entry script writes.)_

## When something breaks

_(Common failure modes + recovery steps.)_

## See also

- [`SKILL.md`](SKILL.md) — Claude Desktop task definition (if used)
- [`manifest.json`](manifest.json) — registry metadata
- Framework: https://github.com/voidsstr/reusable-agents
