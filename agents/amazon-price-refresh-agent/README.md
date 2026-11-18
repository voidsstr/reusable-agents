# Amazon Price Refresh (engine)

> Demand-ranked Amazon price and display-cache refresh for cache-model sites:
> re-checks the ASINs buyers and assistants land on first, writes each fresh
> Creators answer into the site's `amazon_item_cache`, never blanks data on a
> failed run, and after a successful one NULLs Amazon data past a purge
> horizon (24h minus the cron period and a run) so nothing is stored for 24h.
> The one exception is a row the run asked about and could not refresh (a
> failed batch is a failed run for its rows): it keeps its values while still
> displayable, and the next run re-asks it or purges it.

## Quick reference

- **Runbook:** [`AGENT.md`](AGENT.md) (read this first)
- **Kind:** AgentBase engine with config-only per-site instances. The engine
  manifest is `enabled: false` with no cron; it is never scheduled itself.
- **Per-site instance:** `<site repo>/agents/price-refresh/` with
  `manifest.json` (id `<site>-price-refresh`, its own cron) and `site.yaml`
  (a `price_refresh:` block). Template: [`site.example.yaml`](site.example.yaml);
  schema: [`price-refresh-config.schema.json`](price-refresh-config.schema.json).
- **Entry command (instance manifest):**
  ```bash
  PRICE_REFRESH_CONFIG=<site repo>/agents/price-refresh/site.yaml \
    python3 /home/voidsstr/development/reusable-agents/agents/amazon-price-refresh-agent/agent.py
  ```
- **Goals:** [`goals.json`](goals.json) (seed them under the instance id).
- **Tests:** `framework/tests/test_amazon_price_refresh_agent.py`
  (offline; one opt-in test against a throwaway Postgres via
  `PRICE_REFRESH_TEST_PG_DSN`).

## Operator CLI (no run record)

```bash
cd /home/voidsstr/development/reusable-agents
A=agents/amazon-price-refresh-agent/agent.py
python3 $A --config     --site-yaml <site.yaml>          # resolved settings (no DB, no API)
python3 $A --print-sql  --site-yaml <site.yaml>          # upsert / purge / metric SQL
python3 $A --plan       --site-yaml <site.yaml> --limit 50   # read-only DB, no API
python3 $A --dry-run    --site-yaml <site.yaml> --asins B0...,B0...   # LIVE getItems, no DB writes; exit 1 if a call failed
```

## Status + history

UI: `http://localhost:8091/agents/<site>-price-refresh` once an instance is
registered (not before the launch-agent registration step).
