# keep-the-lights-on — system configs

One YAML per system the skill manages. The skill runbook (`../SKILL.md`)
is generic; every site-specific value lives here.

## Onboard a new system

1. `cp aisleprompt.yaml <name>.yaml`
2. Edit: `system`, `growth` (North Star + cheap proxy queries), `agents`
   (critical + supporting), `db` (DSN env-var names), `urls`,
   `owner_email`, `known_excluded`, `cadence`, `improvement_every_hours`,
   `standing_incidents`.
3. Invoke: "keep the lights on for `<name>`". The skill loads
   `systems/<name>.yaml` and starts the loop.

## Design notes

- `owner_email` MUST be the fleet operator address, never a personal
  inbox (framework outbound-email policy).
- `known_excluded` keeps healthy ticks cheap — list every documented
  non-issue so the loop never re-diagnoses it.
- `standing_incidents` are operator-gated items the skill will NOT action
  autonomously (e.g. risky live-app / infra changes). It re-notes them
  and, on a real go-ahead, starts with the lowest-risk step.
- Keep `improvement_every_hours` aligned with the system's cron windows
  so an improvement cycle has fresh signal to act on.
