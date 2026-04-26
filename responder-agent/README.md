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
  host: outlook.office365.com                # or imap.gmail.com for Google
  port: 993
  username: automation@northernsoftwareconsulting.com
  use_tls: true
  mailbox: INBOX
  auth_method: oauth2                        # recommended; or 'password' for legacy
  oauth_file: ~/.reusable-agents/responder/.oauth.json

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

**seo-reporter (sending email)** — three send paths, in priority order:

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
