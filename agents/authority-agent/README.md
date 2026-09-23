# Authority Agent

> Ranks a site's most link-worthy published pages against its GSC indexation
> snapshot and emails the operator a daily link-building worklist, to break
> the "Discovered – currently not indexed" wall. Currently deployed for
> specpicks only.

## Quick reference

- **Runbook:** [`AGENT.md`](AGENT.md) — what it does (and does not do),
  inputs, config, goals, known issues (the `gsc_crawl_progress` snapshot it
  reads has not been refreshed since 2026-07-28).
- **Schedule:** `0 13 * * *` (`America/Detroit`), systemd timer
  `agent-authority-agent.timer`
- **Entry:** `run.sh` (env wrapper) → `agent.py` (AgentBase)
- **Logs:** `/tmp/reusable-agents-logs/agent-authority-agent.log`

## Editing + redeploy

1. Edit [`agent.py`](agent.py) (logic) or [`run.sh`](run.sh) (env defaults:
   `AUTHORITY_SITE_ID`, `AUTHORITY_SITE_DOMAIN`, `DATABASE_URL`).
2. Edit [`manifest.json`](manifest.json) if schedule / category / owner
   changed.
3. Re-register only when the manifest changed (the host re-execs code from
   disk):
   ```bash
   set -a; . ~/.reusable-agents/secrets.env; set +a   # FRAMEWORK_API_URL (:8090) + FRAMEWORK_API_TOKEN
   bash /home/voidsstr/development/reusable-agents/install/register-agent.sh /home/voidsstr/development/reusable-agents/agents/authority-agent
   ```
   `register-agent.sh` defaults `FRAMEWORK_API_URL` to
   `http://localhost:8090`, the port the host API listens on. Without
   `FRAMEWORK_API_TOKEN` the API answers 401.

   Do not run `register-all-from-dir.sh` over `reusable-agents/agents` to
   re-register one agent. It would also re-apply `digest-rollup-agent`'s
   `enabled=false` manifest and disable its 07:30 timer.

## Manual trigger

```bash
# Via systemd (same path as the timer, includes agent_run_wrapper.sh)
systemctl --user start agent-authority-agent.service

# Via the framework API
curl -X POST http://localhost:8090/api/agents/authority-agent/trigger \
     -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"

# Directly — still an AgentBase run (records to storage) but skips the
# systemd wrapper's start/finish status floor
bash /home/voidsstr/development/reusable-agents/agents/authority-agent/run.sh
```

## Status + history

```bash
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/authority-agent/runs?limit=10
```
