# Pick Builder (engine)

> Deterministic merchandising for catalog sites: ladder slots (budget tiers
> with role plans and compat-aware dependencies, one unit per slot) and best
> lists filled from ONE eligibility view, the substitutes map, and the
> pick-ASIN list the price refresh prioritises. A slot no eligible product
> satisfies stays empty with its reason: policy is never loosened. Stored
> reasons never carry Amazon prices. No LLM.

## Quick reference

- **Runbook, selector language and TABLE CONTRACT:** [`AGENT.md`](AGENT.md)
- **Kind:** AgentBase engine with config-only per-site instances. The engine
  manifest is `enabled: false` with no cron; it is never scheduled itself.
- **Per-site instance:** `<site repo>/agents/pick-builder/` with
  `manifest.json` (id `<site>-pick-builder`, cron 30 minutes after the
  price refresh) and `site.yaml` (a `pick_builder:` block). Template:
  [`site.example.yaml`](site.example.yaml); schema:
  [`pick-builder-config.schema.json`](pick-builder-config.schema.json);
  reference schema: [`fixtures/schema.sql`](fixtures/schema.sql).
- **Entry command (instance manifest):**
  ```bash
  PICK_BUILDER_CONFIG=<site repo>/agents/pick-builder/site.yaml \
    python3 /home/voidsstr/development/reusable-agents/agents/pick-builder-agent/agent.py
  ```
- **Price refresh wiring:** a `storage_list` group on
  `agents/<site>-pick-builder/state/pick-asins.json` in the site's
  price-refresh `demand_priority.groups`.
- **Goals:** [`goals.json`](goals.json).
- **Tests:** `framework/tests/test_pick_builder_agent.py`.

## Operator CLI (no run record)

```bash
cd /home/voidsstr/development/reusable-agents
A=agents/pick-builder-agent/agent.py
python3 $A --config    --site-yaml <site.yaml>
python3 $A --print-sql --site-yaml <site.yaml>
python3 $A --plan      --site-yaml <site.yaml>     # read-only: every slot and list, with why one is empty
```
