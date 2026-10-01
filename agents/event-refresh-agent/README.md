# Event Refresh Agent

> The day after a retail sale ends, queues one Opus `article-refresh` rec per
> published article tagged `holiday:<id>` on every configured site.

- **Runbook:** [`AGENT.md`](AGENT.md)
- **Goals:** [`goals.json`](goals.json)
- **Schedule:** `30 6 * * *` (America/New_York)
- **Calendar primitives:** `framework/core/seasonal_calendar.py` →
  `event_state`, `recently_ended`, `next_occasion`
- **Config:** `config/event-refresh-config.json` (storage copy overrides)
- **Tests:** `framework/tests/test_event_refresh_agent.py`

Register (idempotent; writes the systemd timer):

```bash
FRAMEWORK_API_URL=http://localhost:8093 bash /home/voidsstr/development/reusable-agents/install/register-agent.sh /home/voidsstr/development/reusable-agents/agents/event-refresh-agent
```

UI: http://localhost:8091/agents/event-refresh-agent
