# pipeline-stage — Blueprint

## When to use

Your agent is one step in a multi-stage pipeline:

- Reads a run dir produced by an upstream stage
- Does one thing
- Writes its outputs to the same run dir for downstream stages

Examples: `seo-data-collector` (writes raw GSC/GA4 data) →
`seo-analyzer` (reads data, writes recommendations) → `seo-reporter`
(reads recs, sends email) → `seo-implementer` (reads recs + replies,
writes code) → `seo-deployer` (reads commits, ships).

## What you get

```
<agent-id>/
  AGENT.md                  # runbook
  SKILL.md                  # if the stage uses an LLM, the prompt lives here
  manifest.json             # registers as desktop-task with no cron
                            # (chained from a parent agent, not standalone)
  pull-data.py / analyze.py / etc.   # the stage's logic
  requirements.txt
  README.md
```

Stages are typically NOT subclasses of AgentBase — they're Python scripts
the upstream agent shells out to. The upstream agent handles lifecycle;
the stage just reads/writes files.

## Run-dir conventions

```
~/.reusable-agents/<pipeline-name>/runs/<site-id>/<UTC-ts>/
  raw/                       # collector output
  analysis.json              # analyzer output
  recommendations.json       # validated against shared/schemas/recommendations.schema.json
  responses.json             # parsed user replies
  deploy.json                # deployer output
  artifacts/                 # diffs, screenshots, build logs
```

Every stage:
1. Takes `--run-ts <UTC-ts>` (or `SEO_AGENT_CONFIG=...` env var) +
   discovers the run dir from site config.
2. Reads only the inputs it needs (e.g., the analyzer reads
   `raw/*.json`, doesn't care about `responses.json`).
3. Writes its outputs atomically (write to `<file>.tmp`, rename).
4. Exits 0 on success, non-zero on failure. The upstream agent decides
   whether to chain.

## Integration points

| Touchpoint | How |
|---|---|
| Config | `shared/site_config.py:load_config_from_env(...)` — same YAML the upstream agent uses. |
| Run dir | `shared.run_files` helpers — `read_recommendations()`, `write_recommendations()`, `read_responses()`, etc. Centralizes path conventions. |
| Schemas | Use the existing schemas (`recommendations.schema.json`, `responses.schema.json`). Add a new one only if your stage emits a fundamentally new output. |
| AI | If the stage calls AI, use `python3 -m framework.cli.ai_chat --agent <stage-agent-id>` so the framework's provider config drives backend selection. |

## Reference implementations

- **`seo-data-collector/pull-data.py`** — pure data fetch (GSC + GA4 +
  optional DB). No LLM, no decisions.
- **`seo-analyzer`** — reads raw data, calls LLM via the framework CLI,
  emits ranked `recommendations.json`.
- **`seo-reporter/send-report.py`** — reads recs, renders HTML email,
  ships via msmtp. Tags subject with `[<agent-id>:<request-id>]`.
- **`seo-deployer`** — reads commits + deploy config, runs build + push +
  smoke check.

## Anti-patterns

1. **Don't make a stage call other stages.** Each stage does one thing.
   The orchestrator (typically a parent agent or `run.sh`) chains them.
2. **Don't write to the run dir from random places.** Funnel everything
   through `shared/run_files.py` so paths stay consistent.
3. **Don't read upstream stage's *internal* state.** If you need
   intermediate data, ask for it as a documented output. State that
   isn't in the schema isn't a contract — it'll get refactored away.
4. **Don't fail silently.** If you can't produce your output, exit
   non-zero with a clear stderr message. The orchestrator catches it.
5. **Don't skip schema validation.** Validate every JSON file you write
   before finalizing the rename.

## Hybrid: pipeline-stage + AgentBase

If your stage *also* needs framework integration (status reporting,
decisions, mailer), it can subclass AgentBase AND read upstream run-dir
files. `seo-reporter` works this way — it's both a pipeline stage AND a
framework agent that reports status as it sends.
