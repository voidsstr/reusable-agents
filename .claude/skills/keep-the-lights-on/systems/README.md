# keep-the-lights-on — systems

Where each managed system's config lives, and how the skill finds it.

**Framework-first split (matches the reusable-agents contract):**
- **reusable-agents repo** (here) owns the *shared* pieces: the runbook
  (`../SKILL.md`), the config template (`_example.yaml`), and the
  `registry.yaml` that maps each system → its config path.
- **each SITE repo** owns its *own* config, committed there:
  - `aisleprompt` → `/home/voidsstr/development/aisleprompt/keep-the-lights-on.yaml`
  - `specpicks`   → `/home/voidsstr/development/specpicks/keep-the-lights-on.yaml`

The skill resolves a system's config via `registry.yaml` (path in the site
repo) → convention (`<repo>/keep-the-lights-on.yaml`) → `systems/<name>.yaml`
(dev/example fallback).

## Onboard a new system

1. `cp _example.yaml <site-repo>/keep-the-lights-on.yaml`
2. Edit it: `system`, `growth`, `agents` (FULL roster — critical +
   supporting; every site-prefixed agent in scope), `db`, `urls`,
   `owner_email`, `known_excluded`, `cadence`, `improvement_every_hours`,
   `standing_incidents`.
3. Add a `systems.<name>` entry to `registry.yaml` here.
4. Commit the config **in the site repo** (main-first) and the registry
   entry here.
5. Invoke: "keep the lights on for `<name>`".

## Design notes

- `owner_email` MUST be the fleet operator address, never a personal inbox.
- Keep the `agents:` roster complete — it's the authoritative "in scope"
  inventory; the tick's failure scan is site-prefix-scoped.
- `known_excluded` keeps healthy ticks cheap.
- `standing_incidents` are operator-gated (risky live-app/infra); the skill
  re-notes them and only actions on a real go-ahead.
- Changing a live config = commit in the SITE repo, not reusable-agents.
