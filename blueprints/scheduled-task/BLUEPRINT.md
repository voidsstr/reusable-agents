# scheduled-task — Blueprint

## When to use

Your agent is just a cron-driven script that does one thing:

- Scrapes a website
- Hits an API and stores results
- Sends a daily summary
- Runs maintenance / cleanup

It doesn't need recommendations / email-confirmation / multi-stage chaining.
This is the **default blueprint** when nothing fancier fits.

The vast majority of agents in `nsc-assistant/agents/` are this shape.

## What you get

```
<agent-id>/
  AGENT.md                  # runbook
  SKILL.md                  # optional — Claude Desktop task definition
  manifest.json
  agent.py    or   run.sh   # the script
  requirements.txt          # if Python
  README.md
```

Two flavors:

### Python flavor — subclass AgentBase

```python
class MyAgent(AgentBase):
    agent_id = "my-agent"

    def run(self) -> RunResult:
        self.status("doing work", progress=0.5)
        # ...
        return RunResult(status="success", summary="...", metrics={...})

if __name__ == "__main__":
    MyAgent().run_once()
```

Use this when you want full framework integration — status, decisions,
mailer, ai_client, response-queue, confirmations.

### Bash flavor — record runs via legacy recorder

```bash
#!/usr/bin/env bash
python3 -m agents.lib.agent_recorder start --agent-id my-agent
# ...do work...
python3 -m agents.lib.agent_recorder complete --agent-id my-agent --summary "ok"
```

Use this when the agent is shell-driven and refactoring it into Python
would be busywork. Eventually you should refactor onto AgentBase, but
this is fine as a stepping stone.

## Integration points

| Touchpoint | Python flavor | Bash flavor |
|---|---|---|
| Status | `self.status(...)` | n/a (just commit recorder output) |
| Decisions | `self.decide(...)` | n/a |
| Mailer | `self.mailer` if set | shell out to `msmtp` |
| AI | `self.ai_client()` | `python3 -m framework.cli.ai_chat --agent <id>` |
| Storage | `self.storage` | `~/.reusable-agents/data/agents/<id>/` |

## Reference implementations

- **Python:** `_template/agent/agent.py.template` — the generic
  scaffold. Copy this when starting fresh.
- **Bash:** Most legacy agents in `nsc-assistant/agents/*/run*.sh`.
  E.g., `aisleprompt-kitchen-scraper/run.sh`,
  `retro-multiplayer-refresh/run.sh`.

## Anti-patterns

1. **Don't reinvent the recommendation flow.** If you find yourself
   building "send a list of things, get user replies, dispatch
   actions" — switch to the `site-quality-recommender` blueprint.
2. **Don't bypass status reporting.** The dashboard's glow animation
   reads `status.json`. If you don't update it, the agent looks dead
   even when it's working.
3. **Don't skip RunResult.** Returning `None` or raising on failure
   confuses the framework's run-recording. Return a RunResult with
   `status="failure"` + a clear summary instead.
4. **Don't hardcode credentials.** Env vars or files under
   `~/.reusable-agents/<agent-id>/` (mode 0600).
5. **Don't run as root.** All framework agents run as the user who
   owns `~/.reusable-agents/`. If the agent needs root for a single
   step, use sudo with NOPASSWD scoped to that exact command.

## Schedule patterns

| Cron | Use case |
|---|---|
| `* * * * *` | every minute (rare — usually inbox-poller) |
| `*/15 * * * *` | every 15 min (frequent scrapes) |
| `*/30 * * * *` | every 30 min (typical "stay fresh") |
| `0 * * * *` | hourly (light-touch checks) |
| `0 5 * * *` | daily 5 AM (most digests / audits) |
| `0 5 * * 0` | weekly Sunday 5 AM (slow-changing data) |

Stagger schedules across agents so they don't all fire at the same
minute. The framework handles each one independently but your LLM
provider rate limits don't.
