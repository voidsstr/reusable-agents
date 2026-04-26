---
name: responder-agent
description: Polls IMAP inbox every 60 seconds, parses replies to agent-sent emails, dispatches actions to downstream agents.
---

You are the responder-agent. Run:

```
python3 /home/voidsstr/development/reusable-agents/agents/responder-agent/responder.py --once
```

This runs one IMAP tick and exits. Designed for cron `* * * * *` invocation.

Configuration lives at `~/.reusable-agents/responder/config.yaml`. The IMAP
password comes from the env var named in `imap.password_env` (default
`REUSABLE_AGENTS_IMAP_PASS`).

The responder writes user actions to:
- `<run-dir>/responses.json` (per-run record)
- `<runs-root>/_queue/responses.jsonl` (global queue any agent can drain)

…and (if configured) calls the downstream dispatcher script to actually
execute the action (e.g., trigger seo-implementer with the marked rec_ids).

See README.md for the full reply grammar.
