# goals-tracker — runbook

Daily email digest summarizing every agent's goals + metrics.

## What it does

1. Walks `framework.core.registry.list_agents()` for every registered agent.
2. For each agent, reads `agents/<id>/goals/active.json` (definitions) and
   `agents/<id>/goals/timeseries-cache.json` (recorded points).
3. Computes baseline (first recorded point), current (latest recorded
   point), trend %, and progress vs target.
4. Renders an HTML email with:
   - Site KPI roll-up (organic clicks, conversions for AislePrompt + SpecPicks)
   - Per-site sections grouping every agent
   - Inline SVG sparkline for each goal (30 most recent points)
   - "Stale agents" alert listing every agent whose latest progress
     point is older than `GOALS_TRACKER_STALE_HOURS` (default 30h)
5. Sends to `mperry@northernsoftwareconsulting.com` via msmtp[automation].
6. Saves a copy at `last-digest.html` for inspection.

## Schedule

Per-instance manifest cron is `0 12 * * *` UTC = 7am Detroit (8am during
DST). Adjust per-instance `cron_expr` if you need a different morning.

## Config (env vars)

- `GOALS_TRACKER_TO` — recipient (default mperry@...)
- `GOALS_TRACKER_FROM` — sender (default automation@...)
- `GOALS_TRACKER_MSMTP_ACCOUNT` — msmtp account (default `automation`)
- `GOALS_TRACKER_STALE_HOURS` — staleness threshold (default 30)
- `GOALS_TRACKER_SUBJECT` — subject template (vars: `{date}`, `{n_agents}`,
  `{n_goals}`, `{n_stale}`)

## Manual run

```bash
# Render only, write to last-digest.html, no email sent
python3 agents/goals-tracker/agent.py --no-email

# Send live
python3 agents/goals-tracker/agent.py
```

## Agent-agnostic

This agent reads ANY agent's goals — it doesn't know about specific
agents. To track a new agent in the digest, just declare goals via
`PUT /api/agents/<id>/goals` (or `goals.init_goals()` from Python).
