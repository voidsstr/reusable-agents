# Amazon Catalog Discovery

> Engine: grows an affiliate site's catalog from Amazon Creators `searchItems`
> over the site's own lanes, persisting only the ASIN and our lane label.

- **Runbook:** [`AGENT.md`](AGENT.md)
- **Config:** `CATALOG_DISCOVERY_CONFIG` (or the `catalog_discovery:` block of a site.yaml), schema [`catalog-discovery-config.schema.json`](catalog-discovery-config.schema.json)
- **Examples:** [`examples/home-power.site.yaml`](examples/home-power.site.yaml), [`examples/cleaning-robots.site.yaml`](examples/cleaning-robots.site.yaml)
- **Goals:** [`goals.json`](goals.json)
- **Tests:** `framework/tests/test_amazon_catalog_discovery_agent.py`
- **Schedule:** none here (`enabled: false`). Per-site instances are
  config-only directories in the site repos with their own manifest and cron.

## Layout

| File | Role |
|---|---|
| `agent.py` | AgentBase wrapper + CLI (`--validate`, `--dry-run`, `--seeds-only`, `--no-seeds`) |
| `catalog_discovery/config.py` | config model and validation |
| `catalog_discovery/lanes.py` | lane classifier (titles / browse nodes, in memory) |
| `catalog_discovery/rotation.py` | slot planner: breadth-first paging resumed across runs |
| `catalog_discovery/seeds.py` | seed-ASIN import (JSON files, read-only SQL) |
| `catalog_discovery/catalog.py` | dedupe + ASIN-only inserts; DSN resolution via the site registry |
| `catalog_discovery/engine.py` | one run: budget, search loop around `amazon_discovery.discover()`, metrics |

## Quick commands

```bash
cd /home/voidsstr/development/reusable-agents
python3 agents/amazon-catalog-discovery-agent/agent.py --config <site.yaml> --validate
PYTHONPATH=. python3 -m pytest framework/tests/test_amazon_catalog_discovery_agent.py -q
```
