---
name: responder-agent
description: Polls the IMAP inbox (framework timer, every 2 minutes), parses replies to agent-sent emails, records decisions and dispatches actions to downstream agents.
---

You are the responder-agent. In production the framework timer
`agent-responder-agent.timer` (`*/2 * * * *`) runs the AgentBase wrapper:

```
RESPONDER_CONFIG=$HOME/.reusable-agents/responder/config.yaml \
PYTHONPATH=/home/voidsstr/development/reusable-agents \
python3 /home/voidsstr/development/reusable-agents/agents/responder-agent/agent.py
```

For a one-off tick outside the framework (no run record):

```
python3 /home/voidsstr/development/reusable-agents/agents/responder-agent/responder.py --once
```

Configuration lives at `~/.reusable-agents/responder/config.yaml`. The
recommended auth is `imap.auth_method: oauth2` with `imap.oauth_file`. In
password mode, the password comes from the env var named in
`imap.password_env`, which is required in that mode. `agent.py` fills
`REUSABLE_AGENTS_IMAP_PASS` from `~/.reusable-agents/imap-password` when that
file exists.

The responder writes user actions to:
- `agents/<target_agent>/responses-queue/r-<run_ts>-<rec_id>.json` in framework storage (primary; the target defaults to `implementer`)
- `<run-dir>/responses.json` and `<runs-root>/_queue/responses.jsonl` (legacy local-FS copies)

…and, when a route matches, it spawns the route's dispatcher script (the
implementer) for `implement` / `merge` actions.

See AGENT.md for the runbook (live status, failure modes) and README.md for
the OAuth setup and full reply grammar.
