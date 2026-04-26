# inbox-poller — Blueprint

## When to use

You're building the *server side* of the email-confirmation flow:

- Poll an IMAP inbox every N minutes
- Find unseen emails whose `[<agent-id>:<request-id>]` subject tag
  matches an outbound email this framework sent
- Parse the body for commands (`implement rec-NNN`, `skip`, `yes`,
  `no`, `merge`, etc.)
- Drop a payload in the matching agent's `responses-queue/` (or for
  confirmations, `confirmations/`)
- Mark the email as read

This is the inverse of every agent that uses the email-confirmation
flow. There is typically ONE inbox-poller per framework instance — it
serves all agents.

## What you get

```
<agent-id>/
  AGENT.md
  manifest.json             # cron: * * * * * (every minute)
  responder.py              # the IMAP loop
  oauth-bootstrap.py        # one-time OAuth setup
  mint-token.py             # invoked on each tick to refresh access tokens
  config.example.yaml       # IMAP host, account, dispatch routing
  requirements.txt
  README.md
```

## Subject-tag routing

Outbound emails sent via the framework mailer have the subject prefix
`[<agent-id>:<request-id>]`. The poller decodes this with
`framework.core.email_codes.decode_subject(...)`, looks up the original
email's metadata in `agents/<agent-id>/outbound-emails/<request-id>.json`
to confirm it's ours, then parses the reply.

Reply parsing rules (current convention):

```
implement rec-001 rec-005   # action=implement, rec_ids=[rec-001, rec-005]
skip rec-002                # action=skip
modify rec-003: ...notes... # action=modify, notes=text after colon
merge rec-004 rec-006       # action=merge
yes / no / confirm / reject # for @requires_confirmation flows
```

Anything else → action=unknown, log + drop (don't dispatch).

## Integration points

| Touchpoint | How |
|---|---|
| OAuth | The poller uses XOAUTH2 against Office365 / Google IMAP. Tokens come from `mint-token.py`. |
| Tagged-subject decode | `framework.core.email_codes.decode_subject(subject)` |
| Outbound lookup | `storage.read_json(f"agents/{agent_id}/outbound-emails/{request_id}.json")` |
| Dispatch | `storage.write_json(f"agents/{target_agent}/responses-queue/{request_id}.json", payload)` |
| Confirmation resolve | `storage.write_json(f"agents/{agent_id}/confirmations/{conf_id}.json", {state: "approved", ...})` |
| Mark seen | IMAP `STORE +FLAGS \\Seen` after dispatch succeeds. Don't mark seen on parse failure — the user might re-trigger by replying again. |

## Reference implementation

`responder-agent/responder.py` — the only inbox-poller in this repo.
~400 lines, handles connection retries, OAuth refresh, subject-tag
routing, and response-queue dispatch.

## Anti-patterns

1. **Don't poll the same inbox from multiple agents.** Race conditions
   on `\\Seen` flags + duplicate dispatches. One poller per inbox.
2. **Don't trust unsigned subject tags.** Always look up the
   `outbound-emails/<request-id>.json` to confirm we sent the original.
3. **Don't dispatch on parse failure.** If you can't tell what the user
   wants, log + drop (don't mark seen). They'll see "no response" and
   reply more clearly.
4. **Don't store IMAP passwords inline.** Use OAuth (XOAUTH2) when the
   provider supports it; for password-only IMAPs, read from a 0600 file
   under `~/.reusable-agents/<poller>/imap-password`.
5. **Don't schedule more frequently than 1 minute.** IMAP is rate-limited
   on most providers. Once-a-minute is the sweet spot for "feels
   instant" without DOSing the inbox.

## Schedule pattern

`* * * * *` UTC. Each tick:
- Idempotent: if the inbox has nothing new, exit clean.
- Bounded: don't process more than N messages per tick (default 50) so a
  flood doesn't starve the next tick.
- Status: write `status.json` so the dashboard shows green/red.
