# reusable-agents-competitor-research-agent

Self-improvement instance: the framework competitor-research-agent
pointed at THIS repo. Weekly scan of our own codebase, comparison
against competitor agent / workflow platforms (n8n, Temporal, Airflow,
Prefect, Inngest, Dagster, Argo, Windmill, Trigger.dev, LangChain),
ranked recommendations emailed to mperry@nsc with rec-id reply syntax.

Code lives in [competitor-research-agent/](../../competitor-research-agent/).

This dir holds:
- `manifest.json` — framework registration (cron `0 7 * * 1` Mon 7 AM)
- `site.yaml` — codebase-mode config + competitor list

## Schedule

Weekly Monday 7 AM America/Detroit. Less frequent than the per-site PI
+ CR agents because:
1. The competitor space moves slowly (a week is plenty)
2. Self-improvement recs are typically larger commitments than site-quality
   fixes — fewer per cycle is fine

## Manual run

```bash
COMPETITOR_RESEARCH_CONFIG=/home/voidsstr/development/reusable-agents/agents/reusable-agents-competitor-research-agent/site.yaml \
    python3 /home/voidsstr/development/reusable-agents/competitor-research-agent/agent.py
```

## How codebase mode works

Set `scan_mode: codebase` in `site.yaml`. The agent then:
1. Reads files matched by `codebase.include_globs` (capped at
   `max_files`, each truncated to `max_chars_per_file`)
2. Always pulls `feature_summary_files` first (READMEs, blueprints, key
   modules)
3. Concatenates → ships to LLM → extracts feature list
4. Same competitor analysis as website mode, but with the LLM also
   crawling competitor docs / GitHub READMEs (text only, no images)
5. Emits ranked recs comparing our codebase's actual capabilities
   against competitor offerings

The output is a `features-ours.json` derived from code/docs, not from a
public-facing site.

## Competitor list

Curated for the AI-agent / workflow-automation space:

| Competitor | Why |
|---|---|
| n8n.io | Visual workflow builder; we now have a graph UI inspired by them |
| temporal.io | Durable execution; we don't have replay yet |
| airflow.apache.org | DAG scheduling; standard in data engineering |
| prefect.io | Pythonic flows; closer to our shape |
| inngest.com | Event-driven, serverless. AI-native |
| dagster.io | Asset-based orchestration. Good observability |
| argoproj.github.io | K8s-native workflows |
| windmill.dev | Open-source workflow + scripts |
| trigger.dev | Long-running tasks, good DX |
| langchain.com | AI-agent orchestration (more about LLMs than schedulers) |

Edit `site.yaml` → `competitors.seed_domains` to add/remove.

## Reply syntax

Same as the per-site PI/CR agents:

```
implement rec-001 rec-005      # ship specific recs
skip rec-002                   # dismiss
implement all                  # ship everything (rare for this agent)
implement high                 # by severity
implement parity-feature       # by category — useful here
skip experimental
```
