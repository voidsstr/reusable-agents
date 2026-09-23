# responder-agent

Polls an IMAP inbox (framework systemd timer, every 2 minutes), parses
replies to agent-sent emails, and dispatches actions to the right downstream
agent (e.g., the implementer with `RESPONDER_REC_IDS=rec-001,rec-005`).

> **Operational runbook:** [AGENT.md](AGENT.md) covers schedule, live
> status, storage keys, metrics and troubleshooting. This README is the
> setup reference (OAuth bootstrap, send paths, reply grammar).
> **2026-09-23:** on whitebeast the live `config.yaml` is still the
> unedited example (`imap.host: imap.example.com`), so no mail is being
> read. See AGENT.md → Failure modes.

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
                  Write agents/<target>/responses-queue/<request-id>.json
                  (+ legacy <run>/responses.json + global queue)
                           Trigger implementer (systemd-run scope)
```

## What it does each tick

1. Connect to the IMAP server (TLS, 30 s socket timeout, 3 retries).
2. Search for `UNSEEN` emails in the configured mailbox.
3. For each:
   - Identify the reply from the `X-Reusable-Agent` header, a run-ts in
     the subject, or a `[<agent>:<site>]` subject tag. `In-Reply-To` /
     `References` are read (`In-Reply-To` is copied into the archive entry)
     but **not** used for matching: the reporter doesn't persist its
     Message-IDs.
   - Parse the body for command lines:
     - `implement rec-001 rec-005`
     - `skip rec-002`
     - `merge rec-003 rec-004` (combine into one impl)
     - Or just `rec-001` (defaults to `implement`)
   - Write each rec's decision to framework storage at
     `agents/<target_agent>/responses-queue/r-<run_ts>-<rec_id>.json`
     (the target defaults to `implementer`).
   - Also append to the run's legacy `responses.json` and the global
     `<runs_root>/_queue/responses.jsonl`.
   - Mark the email as read.
4. For `implement` / `merge` with a matching route, dispatch batch 1 to the
   route's script. The implementer auto-chains the remaining batches.

Draining `agents/responder-agent/auto-queue/` is **off** in this agent by
default (`RESPONDER_DRAIN_AUTO_QUEUE=1` turns it on). The
`auto-queue-drainer.service` daemon owns that job.

## Configuration

`~/.reusable-agents/responder/config.yaml` (or pass `--config`):

```yaml
imap:
  host: outlook.office365.com                # or imap.gmail.com for Google
  port: 993
  username: automation@northernsoftwareconsulting.com
  use_tls: true
  mailbox: INBOX
  auth_method: oauth2                        # recommended; or 'password' for legacy
  oauth_file: ~/.reusable-agents/responder/.oauth.json   # IMAP needs the Outlook-scoped token:
                                                         # install/setup-imap-oauth.sh writes
                                                         # .imap-oauth.json and repoints this key

# Map the X-Reusable-Agent header (or a subject tag / body prefix) to a downstream agent.
# Also supported (see responder.py _match_route_for_email): agent_prefix,
# agent_subject_tag, agent_subject_tag_re, fallback: true, and a per-route
# target_agent. Outlook usually strips X-headers from replies, so an
# X-header-only route records replies without dispatching them.
# When a reply matches, the responder routes the parsed action to this agent.
routes:
  - match:
      header: X-Reusable-Agent
      equals: seo-reporter
    dispatcher:
      type: implementer
      script: /home/voidsstr/development/reusable-agents/agents/implementer/run.sh

# Where to find each site's runs (so we can append to <run>/responses.json)
runs_roots:
  - ~/.reusable-agents/seo/runs

# NOTE: a `dashboard:` block was documented here historically; the current
# responder.py does not read it. Runs show up in the dashboard through the
# AgentBase wrapper (agent.py) instead.
```

## OAuth setup (recommended — no password in a file)

The responder supports IMAP **XOAUTH2** for both Microsoft 365 and Google
Workspace. One-time browser consent → refresh token; subsequent polls mint
short-lived access tokens automatically.

### Microsoft 365 (Office 365)

1. **Create the Azure AD app** (one-time, in your tenant):
   - portal.azure.com → Azure Active Directory → App registrations → New registration
   - Name: `reusable-agents responder` (anything)
   - Supported accounts: *Accounts in this organizational directory only*
   - Redirect URI: **Public client/native** → `http://localhost`
   - After creation: **Authentication → Allow public client flows: Yes**

2. **Grant API permissions** (delegated, all five):

   **Microsoft Graph** (used by the reporter for `sendMail`, recommended path):
   - `Mail.Send`
   - `Mail.Send.Shared` (lets the reporter send via `/users/{shared}/sendMail`
     when the from-address is a shared mailbox)
   - `offline_access`
   - `User.Read` (usually auto-added)

   **Office 365 Exchange Online** (used by the responder for IMAP):
   - `IMAP.AccessAsUser.All`
   - `SMTP.Send` (optional — only used if you want SMTP fallback in addition
     to Graph sendMail)

   *If you don't see the Exchange Online options under Microsoft Graph,
   they're under the "APIs my organization uses" tab — search for
   "Office 365 Exchange Online".*

   Then click **Grant admin consent for [tenant]**.

3. **Copy the Application (client) ID** and **Directory (tenant) ID**
   from the Overview tab.

4. **Run the bootstrap** (browser opens, log in with whichever account has
   FullAccess to the automation/shared mailbox):
   ```bash
   python3 oauth-bootstrap.py \
       --provider microsoft \
       --client-id   <client-id-from-azure> \
       --tenant      <tenant-id-from-azure> \
       --username    automation@yourdomain.com
   ```
   `--username` is what gets written into `username_hint` in the oauth file
   — that's the mailbox the responder talks to and the reporter sends from.

   The browser flow logs in YOU (the human with FullAccess delegated to the
   shared mailbox), grants the app permission, and returns a refresh token.

   This saves `~/.reusable-agents/responder/.oauth.json` (mode 0600).

5. **Smoke-test**:
   ```bash
   python3 mint-token.py --check
   # → OK provider=microsoft user=automation@... token_chars=2347
   ```

### Google Workspace

1. **Create the OAuth client**:
   - console.cloud.google.com → APIs & Services → Credentials
   - Create OAuth client ID → **Desktop app**
   - Enable the Gmail API for the project

2. **Bootstrap**:
   ```bash
   python3 oauth-bootstrap.py \
       --provider google \
       --client-id     <client-id> \
       --client-secret <client-secret> \
       --username      automation@yourdomain.com
   ```

### After bootstrap, the OAuth token is used by:

**responder.py (IMAP)** — already wired. Set `auth_method: oauth2` in the
responder config (the example shows both forms).

**seo-reporter (sending email)** (`agents/seo-opportunity-agent/lib/reporter/send-report.py`; with `DIGEST_ONLY=1`, its default, the mail goes to the digest queue instead) — three send paths, in priority order:

1. **Graph `sendMail`** (recommended for M365). No SMTP needed; uses Mail.Send
   delegated permission. The reporter's site config uses a `graph:` block:
   ```yaml
   reporter:
     email:
       to: [you@example.com]
       from: SEO Agent <automation@example.com>
       graph:
         oauth_file: ~/.reusable-agents/responder/.oauth.json
         scope: "offline_access https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/Mail.Send.Shared"
         use_shared_mailbox: true
         from_address: automation@example.com
   ```

2. **smtplib XOAUTH2** (fallback for non-M365 providers, or M365 tenants
   where SMTP AUTH is enabled and Graph is blocked):
   ```yaml
   reporter:
     email:
       smtp:
         host: smtp.office365.com         # or smtp.gmail.com
         port: 587
         auth_method: oauth2
         username: automation@example.com
         oauth_file: ~/.reusable-agents/responder/.oauth.json
   ```

3. **msmtp** (legacy / password auth). Note: under Ubuntu's AppArmor profile
   `usr.bin.msmtp`, msmtp can't exec `python3`, so XOAUTH2 via `passwordeval`
   doesn't work out of the box. Use the smtplib path above instead — same
   token, same outcome, no AppArmor friction.

## Why poll IMAP instead of a webhook?

- Works with any inbox / provider — no need for the email server to push.
- A 2-minute cadence is close enough to "respond next minute" UX.
- Stateless — no inbound HTTP endpoint to expose / secure.
- One responder can watch many automation inboxes if needed.

## Scheduling

The agent is registered with the framework (`manifest.json`,
`cron_expr: "*/2 * * * *"`, UTC). The framework writes the systemd user
timer `agent-responder-agent.timer` (`OnCalendar=*-*-* *:0/2:00`), which runs
`agent.py` (the AgentBase wrapper) through `framework/agent_run_wrapper.sh`.
The log goes to `/tmp/reusable-agents-logs/agent-responder-agent.log`.
Don't add a separate crontab line. For a one-off tick outside the framework:
`python3 responder.py --once`. `--daemon --interval 60` also exists, but is
not used in production.

## Reply parsing

Email body grammar (case-insensitive, line-based):

| Line | Action |
|---|---|
| `implement rec-001` | implement that one rec |
| `implement rec-001 rec-002 rec-003` | implement all three |
| `skip rec-005` | mark rec as skipped (no action taken, but recorded) |
| `merge rec-001 rec-002` | combine into a single implementation |
| `rec-001` | defaults to implement |
| `[seo:aisleprompt] implement rec-001` | explicit prefix: the site part sets the site; the agent part is matched by `agent_prefix` routes |
| `implement art-001 art-003` | article-proposal ids (`art-NNN`) are accepted like `rec-NNN` |
| `implement rec-001 - rec-007` / `implement 1-7` / `implement 1, 3, 5` | ranges and bare numbers (bare numbers only after a verb) |
| `implement r-1a2b3c4d` | globally-unique rec uid, resolved downstream |
| `implement all` / `implement high and critical` / `skip experimental` | bulk filters (`all auto review experimental critical high medium low`), expanded against the run's `recommendations.json` by `tier` / `severity` / `priority` |
| `modify rec-004 …` | recorded with action `modify` (not dispatched) |

Lines without a recognized command are ignored. Multiple commands per email
are fine — they're processed in order, and sentences on one line are split
(`Hey — implement art-001. Skip art-005.` yields two actions).

## Schema

Every parsed action is written as a [Response](../../shared/schemas/responses.schema.json)
entry, with `source: "email-reply"`.
