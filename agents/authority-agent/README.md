# Authority Agent

> Finds and prioritizes off-page authority opportunities (near-miss rankings, citable assets, unlinked mentions) to break the Discovered-not-indexed wall

## Quick reference

- **Runbook:** [`AGENT.md`](AGENT.md)
- **Schedule:** `0 13 * * *` (`America/Detroit`)
- **Source:** This directory — agent code lives here in `reusable-agents`
- **Framework:** [reusable-agents](https://github.com/voidsstr/reusable-agents)

## Editing + redeploy

1. Edit [`agent.py`](agent.py) (or [`run.sh`](run.sh) for bash agents).
2. Edit [`manifest.json`](manifest.json) if schedule / category / owner / capabilities changed.
3. Re-register so the framework updates the systemd timer + registry:
   ```bash
   bash /home/voidsstr/development/reusable-agents/install/register-all-from-dir.sh "/home/voidsstr/development/reusable-agents/agents"
   ```

## Manual trigger

```bash
# Via the framework API
curl -X POST http://localhost:8090/api/agents/authority-agent/trigger \
     -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"

# Or directly (bypasses the framework, no run-recording):
bash /home/voidsstr/development/reusable-agents/agents/authority-agent/run.sh
```

## Status + history

UI: http://localhost:8091/agents/authority-agent
