# Site Feedback Notifier — runbook

**What it does.** Hourly (`7 * * * *` UTC) it reads new rows from each
subscribed site's reader-feedback table and emails the fleet operator one
message per site listing them: type, severity, page, time, the reader's text
(HTML-escaped, marked untrusted) and a link to the site's admin view. Sites
have no mail transport of their own (the web containers have no MTA), so this
agent is how feedback reaches a human.

**Why it exists.** Until 2026-11-22 the SpecPicks FeedbackWidget posted to an
unmounted route and 0 submissions were ever saved. The route now stores rows
in `"Feedback"`; this agent replaces a stop-gap hourly check that lived in the
keep-the-lights-on loop.

## Subscribing a site

Edit the STORAGE copy of `config/site-feedback-notifier-config.json` (it
replaces the repo default wholesale; no commit or deploy needed):

```json
"by_site": {
  "<site>": {
    "enabled": true,
    "label": "<Display name>",
    "dsn_env": "DATABASE_URL_<SITE>",
    "app_key": "<appKey the site's router writes>",
    "admin_url": "https://<site>/admin/hub?tab=feedback",
    "start_from": "<ISO timestamp: report rows created at or after this>"
  }
}
```

`start_from` only matters the first time the agent sees the site; without it
the site starts at the run's start (no backlog email). The table defaults to
`Feedback` (the vendored feedback package's schema: `id`, `appKey`, `kind`,
`severity`, `message`, `pageUrl`, `pageTitle`, `createdAt`, `screenshot`).

## Guarantees

- **Read-only** on site databases (read-only session, 20 s statement timeout).
- **At least once:** a site's cursor (newest reported `createdAt` + the ids at
  that instant) advances only after its email is delivered (`graph:`/`sent`).
  A failed send keeps the cursor; the next run retries.
- **Fails closed:** an unreadable storage config, a missing recipient, an
  undelivered email, a site that vanished from the config, or a site whose
  read failed two runs in a row FAILS the run (AgentBase emails the failure).
  One failed read is a warning.
- **Quiet when idle:** a run with nothing new reports `no new feedback on N
  site(s)`, which AgentBase treats as a no-op (no run-summary email).
- **No LLM:** the LLM helpers raise.

## Goals (goals.json)

| Goal | Metric | Target |
|---|---|---|
| Every submission reaches the operator | `feedback_undelivered` | 0 |
| Emailed within 90 minutes | `feedback_notify_lag_min` | ≤ 90 |
| Every subscribed table readable | `site_errors` | 0 |

## Operating it

```bash
# dry run (reads, sends nothing, keeps state)
cd /home/voidsstr/development/reusable-agents && set -a && . ~/.reusable-agents/secrets.env && set +a
STORAGE_BACKEND=azure FEEDBACK_NOTIFIER_DRY_RUN=1 python3 agents/site-feedback-notifier/agent.py

# run now / check the timer / logs
systemctl --user start agent-site-feedback-notifier.service
systemctl --user list-timers | grep agent-site-feedback-notifier
tail -20 /tmp/reusable-agents-logs/agent-site-feedback-notifier.log
```

Env overrides: `FEEDBACK_NOTIFIER_DRY_RUN=1`, `FEEDBACK_NOTIFIER_SITES=a,b`,
`FEEDBACK_NOTIFIER_NOW=<ISO>`.

## Troubleshooting

- *"no recipient/sender"*: `OPERATOR_EMAIL` / `OPERATOR_FROM_EMAIL` missing
  from `~/.reusable-agents/secrets.env` (the unit's EnvironmentFile). Never put
  a personal address in the config.
- *"not delivered (…)"*: the Graph token or msmtp; see the KTLO runbook's mail
  transport note. The rows are retried next run.
- *"env DATABASE_URL_<SITE> is not set"*: add it (single-quoted) to secrets.env.
- *"sites tracked before are missing from the config"*: someone removed a
  site from the storage config. Re-add it, or set `enabled: false` to retire it
  on purpose.

Tests: `framework/tests/test_site_feedback_notifier.py`.
