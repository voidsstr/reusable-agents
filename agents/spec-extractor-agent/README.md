# Spec Extractor (engine)

> Manufacturer specs with provenance for any catalog niche: polite fetching
> of manufacturer pages and manuals, deterministic per-brand extraction (an
> optional batched cheap-model fallback and a budgeted strong-model conflict
> adjudication), one provenance row per value, typed spec rows, then
> compatibility edges and runtime estimates. Never parses Amazon text.
> Every phase short-circuits on unchanged input.

## Quick reference

- **Runbook and TABLE CONTRACT:** [`AGENT.md`](AGENT.md) (read this first)
- **Kind:** AgentBase engine with config-only per-site instances. The engine
  manifest is `enabled: false` with no cron; it is never scheduled itself.
- **Per-site instance:** `<site repo>/agents/spec-extractor/` with
  `manifest.json` (id `<site>-spec-extractor`, its own cron) and `site.yaml`
  (a `spec_extractor:` block). Template: [`site.example.yaml`](site.example.yaml);
  schema: [`spec-extractor-config.schema.json`](spec-extractor-config.schema.json);
  reference schema: [`fixtures/schema.sql`](fixtures/schema.sql).
- **Entry command (instance manifest):**
  ```bash
  SPEC_EXTRACTOR_CONFIG=<site repo>/agents/spec-extractor/site.yaml \
    python3 /home/voidsstr/development/reusable-agents/agents/spec-extractor-agent/agent.py
  ```
- **Niche profiles:** `config/spec-sources-config.json` and
  `config/compat-graph-config.json` (storage), selected by name.
- **Goals:** [`goals.json`](goals.json) (seed them under the instance id).
- **Tests:** `framework/tests/test_spec_extractor_agent.py` (offline; DB
  tests use a throwaway postgres:16 container).

## Operator CLI (no run record)

```bash
cd /home/voidsstr/development/reusable-agents
A=agents/spec-extractor-agent/agent.py
python3 $A --config    --site-yaml <site.yaml>
python3 $A --print-sql --site-yaml <site.yaml>
python3 $A --plan      --site-yaml <site.yaml>
python3 $A --dry-run   --site-yaml <site.yaml> --model-id <id>
```
