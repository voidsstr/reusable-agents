# {{AGENT_NAME}}

> {{AGENT_DESCRIPTION}}

## Quick reference

- **Runbook:** [`AGENT.md`](AGENT.md)
- **Schedule:** `{{AGENT_CRON}}` (`{{AGENT_TIMEZONE}}`)
- **Source:** This directory — agent code lives here in `{{SOURCE_REPO}}`
- **Framework:** [reusable-agents](https://github.com/voidsstr/reusable-agents)

## Editing + redeploy

1. Edit [`agent.py`](agent.py) (or [`run.sh`](run.sh) for bash agents).
2. Edit [`manifest.json`](manifest.json) if schedule / category / owner / capabilities changed.
3. Re-register so the framework updates the systemd timer + registry:
   ```bash
   bash {{REGISTER_SCRIPT_PATH}}
   ```

## Manual trigger

```bash
# Via the framework API
curl -X POST http://localhost:8090/api/agents/{{AGENT_ID}}/trigger \
     -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"

# Or directly (bypasses the framework, no run-recording):
bash {{ABSOLUTE_AGENT_DIR}}/run.sh
```

## Status + history

UI: http://localhost:8091/agents/{{AGENT_ID}}
