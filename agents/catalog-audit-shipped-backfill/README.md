# Catalog-Audit Shipped Backfill

> Every 30 minutes, checks the prod DB to confirm that implemented
> catalog-audit migrations took effect, then flips those recs from
> `shipped:false` to `shipped:true`.

## Quick reference

- **Runbook:** [`AGENT.md`](AGENT.md). It covers the verifier table, goals,
  failure modes and the known short-circuit bug.
- **Schedule:** `*/30 * * * *` (UTC); systemd timer
  `agent-catalog-audit-shipped-backfill.timer`.
- **Source:** `agent.py` in this directory (AgentBase). `run.sh` is a thin
  `exec python3 agent.py` shim.
- **Origin:** a wrapper around the one-shot script
  `agents/implementer/catalog-audit-shipped-backfill.py`, which still exists
  and still supports `--dry-run`.

## Editing + redeploy

1. Edit [`agent.py`](agent.py). The next timer tick picks it up from disk; no
   redeploy is needed.
2. Only if [`manifest.json`](manifest.json) changed (schedule, entry command),
   re-register so the framework rewrites the systemd timer and the registry:
   ```bash
   set -a; . ~/.reusable-agents/secrets.env; set +a   # FRAMEWORK_API_URL (:8090) + FRAMEWORK_API_TOKEN
   bash /home/voidsstr/development/reusable-agents/install/register-agent.sh /home/voidsstr/development/reusable-agents/agents/catalog-audit-shipped-backfill
   ```
   `register-agent.sh` defaults `FRAMEWORK_API_URL` to
   `http://localhost:8090`, the port the host API listens on. Without
   `FRAMEWORK_API_TOKEN` the API answers 401.

   Do not run `register-all-from-dir.sh` over `reusable-agents/agents` to
   re-register one agent. It would also re-apply `digest-rollup-agent`'s
   `enabled=false` manifest, and the register route stops and disables the
   timer of any agent registered disabled, so its 07:30 timer would go off.

## Manual trigger

```bash
# Same env as the scheduled run (DSNs come from the unit's entry command)
systemctl --user start agent-catalog-audit-shipped-backfill.service

# Via the local framework API (bearer token required)
curl -X POST http://localhost:8090/api/agents/catalog-audit-shipped-backfill/trigger \
     -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"
```

Running `bash run.sh` directly still records a normal AgentBase run. It needs
`DATABASE_URL_AISLEPROMPT` / `DATABASE_URL_SPECPICKS` in the environment;
otherwise each site is skipped.

## Status + history

- Log: `/tmp/reusable-agents-logs/agent-catalog-audit-shipped-backfill.log`
- API: `GET http://localhost:8090/api/agents/catalog-audit-shipped-backfill/runs`
  (token required); the dashboard is the Azure-hosted framework UI.
