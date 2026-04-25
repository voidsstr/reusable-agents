# responder-agent

Polls an IMAP inbox every 60 seconds, parses replies to agent-sent emails,
and dispatches actions to the right downstream agent (e.g.,
`seo-implementer --recommendations rec-001,rec-005`).

This is the human-in-the-loop bridge. The flow:

```
seo-reporter ─sends email─► automation@company.com ─► You read it
                                                           │
                                                  Reply with rec-001 rec-005
                                                           │
                                       ▼
              automation@company.com inbox ◄────── responder-agent polls
                                                           │
                                                Parse subject + body
                                                           │
                                       ▼
                           Write to <run>/responses.json
                           Write to global responses queue
                           Trigger seo-implementer
```

## What it does each tick

1. Connect to IMAP server (TLS).
2. Search for `UNSEEN` emails to the configured inbox.
3. For each:
   - Extract the **agent id + run-ts** from the email's `In-Reply-To` /
     `References` header (matches the `Message-ID` the original report set)
     OR from the `Re: …` subject pattern.
   - Parse the body for command lines:
     - `implement rec-001 rec-005`
     - `skip rec-002`
     - `merge rec-003 rec-004` (combine into one impl)
     - Or just `rec-001` (defaults to `implement`)
   - Append to the matching run's `responses.json`.
   - Append to the global `<runs_root>/_queue/responses.jsonl`.
   - Mark the email as read.
4. (Optional) trigger the downstream agent immediately.

## Configuration

`~/.reusable-agents/responder/config.yaml` (or pass `--config`):

```yaml
imap:
  host: imap.example.com
  port: 993
  username: automation@northernsoftwareconsulting.com
  password_env: REUSABLE_AGENTS_IMAP_PASS    # env var name (NEVER paste the password into the YAML)
  use_tls: true
  mailbox: INBOX

# Map subject prefixes / X-Reusable-Agent-Site headers to a downstream agent.
# When a reply matches, the responder routes the parsed action to this agent.
routes:
  - match:
      header: X-Reusable-Agent
      equals: seo-reporter
    dispatcher:
      type: seo-implementer
      script: /home/voidsstr/development/reusable-agents/seo-implementer/run.sh

# Where to find each site's runs (so we can append to <run>/responses.json)
runs_roots:
  - ~/.reusable-agents/seo/runs

# (Optional) log every parsed action to this dashboard for visibility
dashboard:
  base_url: http://localhost:8080
  agent_id: responder-agent
```

## Why poll IMAP instead of a webhook?

- Works with any inbox / provider — no need for the email server to push.
- 60-second cadence matches "respond next minute" UX.
- Stateless — no inbound HTTP endpoint to expose / secure.
- One responder can watch many automation inboxes if needed.

## Cron

```cron
* * * * * cd /home/voidsstr/development/reusable-agents/responder-agent && python3 responder.py >> /tmp/responder-agent.log 2>&1
```

Or wire as a `desktop-task` with `cron_expr: "* * * * *"` so the dashboard
shows it next to the other agents.

## Reply parsing

Email body grammar (case-insensitive, line-based):

| Line | Action |
|---|---|
| `implement rec-001` | implement that one rec |
| `implement rec-001 rec-002 rec-003` | implement all three |
| `skip rec-005` | mark rec as skipped (no action taken, but recorded) |
| `merge rec-001 rec-002` | combine into a single implementation |
| `rec-001` | defaults to implement |
| `[seo:aisleprompt] implement rec-001` | explicit site/agent prefix (overrides routing) |

Lines without a recognized command are ignored. Multiple commands per email
are fine — they're processed in order.

## Schema

Every parsed action is written as a [Response](../shared/schemas/responses.schema.json)
entry, with `source: "email-reply"`.
