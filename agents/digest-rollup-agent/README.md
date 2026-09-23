# Activity Digest Rollup (`digest-rollup-agent`)

> Sends one consolidated operator email summarizing fleet activity:
> shipped/implemented recs, the implementer queue, failed runs, agent-doctor
> escalations, inter-agent handoffs, SpecPicks SEO-readiness numbers, and the
> individual agent emails that `DIGEST_ONLY=1` suppressed. It does not move a
> site metric itself. It is the operator's main notification channel, and a
> stalled pipeline (organic clicks, indexed pages, Amazon clicks) is usually
> spotted here first.

## At a glance

| | |
|---|---|
| Agent id | `digest-rollup-agent` |
| Home | `reusable-agents: agents/digest-rollup-agent/` (`agent.py`, `manifest.json`, `goals.json`) |
| Kind | AgentBase python (`DigestRollupAgent`) |
| Schedule (manifest / registry) | `16 */5 * * *` America/Detroit, **`enabled: false`** (see [Status](#status-manifest-vs-host)) |
| Schedule (host, actual) | Daily at **07:30** host time (America/Detroit). A host-local drop-in `~/.config/systemd/user/agent-digest-rollup-agent.timer.d/10-daily.conf` (added 2026-09-15) replaces the base `0/5:16` OnCalendar. Timer is **enabled**. |
| Entry command | `DIGEST_ONLY=0 python3 ${RA_REPO_ROOT}/agents/digest-rollup-agent/agent.py` (run by `framework/agent_run_wrapper.sh`) |
| Category | ops |
| Status | live on the host (timer fires daily), even though the registry says disabled |
| Log | `/tmp/reusable-agents-logs/agent-digest-rollup-agent.log` |

## What it does

`run()` in `agent.py`. The look-back window is `WINDOW_HOURS = 3`, hard-coded:

1. **Drain the digest queue.** Reads every `digest-queue/*.json` blob. Entries
   whose `ts` falls inside the last 3 h are rendered. **Every** entry, in
   window or not, is marked for archiving.
2. **Shipped / implemented recs.** Walks every
   `agents/<id>/runs/<ts>/recommendations.json` (skips `rundir-*` copies) and
   keeps recs whose `shipped_at` or `implemented_at` falls in the window.
   Dedupes by `(agent, rec_id)` so a rec that persists across run snapshots
   counts once.
3. **Runs.** Reads each agent's `run-index.json` (`recent`, first 30 entries)
   for runs that ended in the window. It splits out `status == "failure"` and
   drops `agent-doctor` runs that investigated, fixed and escalated nothing.
4. **Queue.** Lists `agents/responder-agent/auto-queue/*.json` (pending
   implementer dispatches).
5. **Escalations.** Reads `agents/agent-doctor/fixes-log.jsonl` and keeps rows
   with `outcome == "escalated"` in the window. It renders what went wrong
   (stderr tail) and what the doctor tried.
6. **Handoffs.** Reads `agents/<id>/handoffs.jsonl` for every agent: counts
   sent/shipped/deferred/in-progress, lists sender→receiver edges, and flags
   **stuck** handoffs (inbound with no outcome after 24 h, or `in_progress`
   for more than 48 h).
7. **SEO readiness** (SpecPicks only, hard-coded `SITE_QUERIES`). When
   `DATABASE_URL_SPECPICKS` is set, it runs read-only queries against
   `editorial_articles`, `buying_guides`, `products`, `editorial_topics` and
   `trending_comparisons`: editorial coverage %, featured-price freshness,
   Amazon listings not re-priced in 24 h, and published content counts.
8. **Render + send.** Builds one HTML email and sends it through
   `shared.site_quality.send_via_msmtp(..., bypass_digest=True)`, which tries
   Microsoft Graph first and falls back to msmtp. Suppressed `seo-reporter`
   entries are inlined in `<details>` blocks. Other suppressed mail is only
   counted.
9. **Archive.** After a successful send, or an empty window, it copies each
   queue key to `digest-archive/<same name>` and deletes it from
   `digest-queue/`.

**Empty window:** if there are no shipped, implemented, queued, failed,
escalated or suppressed items, it sends nothing, archives the queue, and
returns success with summary `empty window — no email sent`.

## Inputs

| Source | What |
|---|---|
| `digest-queue/<ts>-<hash>.json` | Suppressed emails, written by `framework/core/digest_queue.queue()`, `AgentBase.queue_for_digest()` and `shared/site_quality.py` (the `DIGEST_ONLY` gate) |
| `agents/*/runs/*/recommendations.json` | Shipped / implemented recs |
| `agents/*/run-index.json` | Run outcomes |
| `agents/responder-agent/auto-queue/*.json` | Pending dispatches |
| `agents/agent-doctor/fixes-log.jsonl` | Escalations |
| `agents/*/handoffs.jsonl` | Inter-agent handoffs |
| SpecPicks Postgres, read-only (`DATABASE_URL_SPECPICKS`) | SEO-readiness block. Skipped silently if unset or on error. |

All storage reads go through `framework.core.storage` (Azure Blob on this host).

## Outputs

- **Email:** one message to `OPERATOR_EMAIL` (fallback
  `mperry@northernsoftwareconsulting.com`) from `OPERATOR_FROM_EMAIL`
  (fallback `automation@northernsoftwareconsulting.com`), msmtp account
  `automation`, header `X-Reusable-Agent: digest-rollup-agent`. Subject:
  `[digest-rollup-agent] Digest · N shipped[ · N failed][ · N queued] — last 3h`.
- **Storage:** moves queue entries to `digest-archive/`.
- **No per-run summary email** (`send_run_summary_email = False`, because
  this agent is the summary).
- **RunResult:** status is `success` when the email was accepted or the window
  was empty, and `failure` when the send failed. A failed send does **not**
  archive the queue.

## Goals & metrics

Goals are in `goals.json` (`goal-fleet-failed-runs-zero`,
`goal-fleet-escalations-low`, `goal-fleet-ship-flow`):

| Goal id | target_metric | Target | Last recorded (storage, 2026-09-23) |
|---|---|---|---|
| `goal-fleet-failed-runs-zero` | `failed_runs` | 0 (decrease) | 0 |
| `goal-fleet-escalations-low` | `escalations` | 0 (decrease) | 7 |
| `goal-fleet-ship-flow` | `shipped` | 5 (increase) | 0 |

`RunResult.metrics` keys on the send path: `shipped`, `implemented_only`,
`queued_recs`, `queued_dispatches`, `runs`, `failed_runs`, `escalations`,
`suppressed`, `emailed`. The empty-window path emits `queued` instead of
`queued_recs`/`queued_dispatches`.

The registry entry is `enabled: false`, and `goals-tracker` filters out
disabled agents, so these goals do **not** appear in the daily goals digest.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DIGEST_ONLY` | wrapper default `1`; this agent's entry command sets `0` | Global gate. `1` makes other agents queue their mail here instead of sending it. |
| `DIGEST_DISABLED` | unset | Kill switch in `shared/site_quality.py`. `1` makes the gate *drop* suppressed mail instead of queueing it. It was set 2026-08-14 → 2026-09-08 while the host had no mail transport. It is not in `secrets.env` today. |
| `AGENT_SUMMARY_DIGEST` | `1` | `framework/core/agent_base.py`. Routes successful run-summary emails into `digest-queue/`. Failed runs still send immediately. |
| `OPERATOR_EMAIL` / `OPERATOR_FROM_EMAIL` | see Outputs | Recipient / sender |
| `DATABASE_URL_SPECPICKS` | unset → SEO block skipped | Read-only DSN |
| `RA_REPO_ROOT` | must be set (it comes from `secrets.env`) | Used in the entry command. When it was unset, runs exited 2 (see manifest `_disabled_note`). |

Mail credentials: Graph uses `~/.reusable-agents/responder/.oauth.json`,
restored from Key Vault on 2026-09-04 (commit `579b875`). The msmtp binary is
installed, but `~/.msmtprc` is absent on this host (checked 2026-09-23), so
delivery depends on the Graph path.

## Status: manifest vs host

- `manifest.json` still has `enabled: false`, a note dated 2026-08-17
  (archive-and-disable because the host had no mail transport), and cron
  `16 */5 * * *`. The registry mirrors this.
- The host timer is **enabled**. The 2026-09-15 drop-in (operator request:
  "change the email notifications I am getting to be once a day") makes it
  fire daily at 07:30. The run on 2026-09-23 succeeded: `0 shipped · 0
  implemented · 0 queued · 0 failed · 7 escalations · 196 individual emails
  rolled up`.
- The daily cadence exists **only** in the host drop-in. It is not in the
  manifest and not in any repo, so a fresh `/provision-fleet` host gets the
  5-hourly schedule back. A re-register rewrites only the base `.timer` and
  `.service` files (`scheduler.write_systemd_units`), so the drop-in survives
  it. But re-registering the manifest as-is (`enabled: false`) makes the
  register route call `scheduler.systemctl_stop_and_disable`, which stops and
  disables the timer (`framework/api/app/routes/agents.py`). Reconcile the
  manifest before re-registering.

## Short-circuit & idempotency

- `signals()` hashes the sorted `digest-queue/` key list, but **the
  short-circuit never fires**. `run()` returns no `next_state`, so the hash
  is never persisted (`state/latest.json` was `{}` on 2026-09-23). Each
  07:30 run executes in full. `AgentBase._check_short_circuit()` puts
  `_auto_signals_hash` into `self.state` only in memory. `post_run()` then
  writes `result.next_state`, which defaults to `{}`, so the next run has
  nothing to compare against.
- Re-running inside the same window re-reads shipped recs, runs and
  escalations (these are not consumed) but finds an empty queue, because the
  previous run archived it.

## Running & inspecting

```bash
systemctl --user start agent-digest-rollup-agent.service     # sends a real email
tail -f /tmp/reusable-agents-logs/agent-digest-rollup-agent.log
systemctl --user list-timers | grep digest-rollup
systemctl --user cat agent-digest-rollup-agent.timer           # shows the 10-daily.conf drop-in
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://localhost:8090/api/agents/digest-rollup-agent
```

There is no dry-run flag. Any manual run sends the email and archives the
queue.

## Failure modes & troubleshooting

| Symptom | Cause / fix |
|---|---|
| The operator hears from no agent at all | The rollup is the only channel for mail suppressed by `DIGEST_ONLY=1`. On 2026-09-04 (`579b875`) two faults were fixed: no Graph credentials plus no `~/.msmtprc`, and an empty `OPERATOR_EMAIL` recipient. Check the log for `status=failure` and the `digest send failed:` decision. |
| `status=failure` but the summary lists real work | The rollup succeeded and **delivery** failed. Check mail transport (Graph oauth file, msmtp). The queue is not archived, and the next run renders only what is still inside its 3 h window. |
| Exit code 2 | `${RA_REPO_ROOT}` unset in the unit environment (history in the manifest note). |
| Most of the day's suppressed mail never shows up | Known limitation: `WINDOW_HOURS = 3` is hard-coded, but the timer now fires once a day. Queue entries older than 3 h at 07:30, and shipped recs, runs and escalations outside 04:30–07:30, are archived or ignored without being rendered. Fix it in code (tie the window to the cadence) before relying on the digest as a daily summary. |
| Queue grows without bound | Happens if sends keep failing, or the timer is off while other agents keep queueing (4,298 items by 2026-08-14). `DIGEST_DISABLED=1` stops the growth, but it also silences routine mail. |
| Alerts missing | Alerts use `bypass_digest=True` (`framework/core/resilience.notify_operator`), and failed-run summaries send directly, so neither waits for this digest. If alerts are missing, look at transport, not this agent. |

## Related agents

- **Producers:** every agent that emails (through `DIGEST_ONLY`), and
  `AgentBase` run summaries (through `AGENT_SUMMARY_DIGEST`).
- **Data it reads:** `agent-doctor` (escalations), `responder-agent`
  auto-queue, the implementer (`reusable-agents: agents/implementer/README.md`).
- **Sibling daily email:** `goals-tracker`
  (`reusable-agents: agents/goals-tracker/AGENT.md`).
