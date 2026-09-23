# Agent Doctor — self-healer (`agent-doctor`)

> Watches every registered agent for **failures**, **stuck runs** and
> **stuck implementer queues**. It classifies each failure, applies a small
> set of safe recipes, asks an LLM to diagnose anything it does not
> recognise, and escalates the rest to the operator. A durable
> `fixes-log.jsonl` keeps it from looping on the same broken signature.
> Serves the North Star indirectly: every broken producer (SEO,
> article-author, catalog agents) is lost site growth until someone notices.

## At a glance

| | |
|---|---|
| Agent id | `agent-doctor` |
| Home | `reusable-agents/agents/agent-doctor/` (`agent.py`, `manifest.json`, this file) |
| Kind | AgentBase Python (`class AgentDoctor(AgentBase)`), `send_run_summary_email = False`, `auto_recovery_enabled = False` (it never doctors itself) |
| Schedule | **Two triggers.** (1) Timer: manifest `0 */5 * * *` `America/Detroit`, `OnCalendar=*-*-* 0/5:0:00`. It fires every **5 hours** at 00:00, 05:00, 10:00, 15:00 and 20:00 host-local, not every 5 minutes as the manifest description says. (2) Event-driven: `framework.core.resilience.invoke_doctor()` queues a host-worker job whenever another agent fails (see below). On 2026-09-23 there were 35 event-driven runs (host-worker per-run logs `agent-doctor-<ts>.log`, 03:23Z to 16:46Z), on top of the timer ticks. |
| Entry | `python3 /home/voidsstr/development/reusable-agents/agents/agent-doctor/agent.py` (timer runs go through `framework/agent_run_wrapper.sh`) |
| Category | ops |
| Status | Live, timer enabled. Latest outcome 2026-09-23 16:47Z: `1 investigated · 0 fixed · 1 escalated · 7 already-seen`. |

## Invocation model

The doctor runs when any of these happen:

- **Timer tick** (every 5 h), for a broad fleet sweep.
- **AgentBase failure**: `AgentBase.run_once()` calls `invoke_doctor()`
  when `result.status == "failure"` or `run()` raises
  (`framework/core/agent_base.py`, post-run hook).
- **Host-worker events**, only for runs launched through the host-worker
  trigger queue (dashboard "Run now", dispatches): a wall-clock timeout
  (rc 124), a non-zero exit, or the EXIT trap flipping an unfinished run to
  failure (`framework/api/host-worker.sh`).
- **Manual**: the dashboard "Run now" button, or the command in
  *Running & inspecting* below.

A plain systemd-timer run of a *non-AgentBase* agent that exits non-zero
does **not** call `invoke_doctor()`, because `agent_run_wrapper.sh` only
writes status. Those failures are caught on the next timer sweep through
`/api/agents` `last_run_status`.

`invoke_doctor()` writes an incident to
`agents/agent-doctor/incidents/<incident_id>.json` and a job file
`/tmp/agent-trigger-queue/agent-doctor-<incident_id>.json`, which the
host-worker executes. Dedupe: the same `(failed_agent_id, error_class)`
within 600 s queues only once. That dedupe map lives in memory in the
failing process.

## What it does (one run)

`AgentDoctor.run()`:

1. **Auth check.** It needs `FRAMEWORK_API_TOKEN`, either from the
   environment or from a `FRAMEWORK_API_TOKEN=` line in the framework
   repo's `.env`. Without it the run returns `failure`.
2. **Drain incidents** (`resilience.drain_incident_queue(archive=True, limit=50)`).
   Drained entries move to `agents/agent-doctor/incidents-processed/`.
   Agents named in an incident are investigated first, whatever their API
   status says.
3. **Broad poll**: `GET {FRAMEWORK_API_URL}/api/agents`. For each agent
   other than itself, a candidate is one of:
   - `last_run_status == "failure"`
   - **stuck**: `last_run_status == "running"` and `last_run_at` is more
     than 30 min old (`STUCK_FALLBACK_MINUTES`). `STUCK_GRACE_MULTIPLIER`
     (p95-based) is declared but not implemented.
   - named in a drained incident.
4. **Dedupe**: `state/seen.json` maps agent → `"<status>@<last_run_at>"`.
   An agent is skipped as already-seen only if `fixes-log.jsonl` *also* has
   an entry for that `(agent, last_run_at)`.
5. **Investigate**: `_read_log()` reads `/tmp/reusable-agents-logs/agent-<id>.log`
   plus the two newest `<id>-*.log` per-run files. It returns up to 12
   one-KB windows around error patterns (Traceback, ERROR, `exited rc=`,
   5xx, …) plus the last 4 KB, capped at 32 KB. It also calls
   `GET /api/agents/<id>/status`.
6. **Classify** (`_classify_error`, first match wins):
   `stuck-no-output`, `oauth-token-expired` (401 / invalid_token /
   TokenExpired / insufficient_scope), `imap-auth-failed`,
   `transient-network`, Traceback subtypes (`missing-file`, `schema-drift`,
   `missing-import`, `oom`, `python-traceback`), `missing-env-<var>`,
   `timeout`, `stale-lock`, `unknown`.
7. **Decide**, using the fixes-log history for `(agent, signature)`:
   - If an earlier LLM verdict was `fix=data-issue`, `fix=external-blocker`
     or `fix=wait`, the outcome is `skipped` and no email is sent.
   - If there have been ≥2 attempts since the last `fixed` *and* the LLM
     has already been tried, it escalates as `loop-broken`. The email is
     suppressed (outcome `skipped`) when a similar excerpt was already
     escalated in the last 24 h. Similar means Jaccard ≥ 0.70 on the
     normalised first 1,500 chars.
   - Otherwise it runs the recipe (table below).
8. **Record**: appends one line to `fixes-log.jsonl` with `ts`,
   `doctor_run_ts`, `target_agent`, `trigger`, `last_run_at`,
   `error_signature`, `recipe_applied`, `outcome`, `notes`, `log_excerpt`
   (600 chars) and `attempt_n`. It also updates `seen.json` and the
   decision log.
9. **Stuck-queue sweep**: `GET /api/implementer/batches?limit=20`. Chains
   with `chain_status == "queued"`, no batch started, and ≥30 min since
   `mtime_iso` are re-queued through `recipe_stuck_queue`. After 2 attempts
   per chain it escalates instead.
10. **Weekly digest counters** go to `weekly-stats.json`. Once
    `week_start` is 7+ days old, the digest email goes out only if
    `escalations > 0` or (investigations > 0 and fixes == 0). A green week
    just resets the counters.
11. Returns `RunResult(status="success", metrics={investigated, fixed, escalated, skipped_already_seen, agents_total})`.
    It returns `failure` only when the token is missing or `/api/agents`
    fails or has an unexpected shape.

## Recipes

| Signature | Recipe | Outcome |
|---|---|---|
| `oauth-token-expired` | runs `agents/responder-agent/mint-token.py --refresh` | `fixed` on rc 0, otherwise `escalated` |
| `transient-network` | nothing | `no-op` (next firing retries) |
| `stale-lock` | deletes `/tmp/agent-<id>*.lock` / `/tmp/<id>*.lock` older than 1 h | `fixed` if any were removed, otherwise `no-op` |
| `timeout` | none (never auto-extends) | `escalated` |
| `missing-env-<var>` | inspects the registered `entry_command` for unexpanded `${VAR}` | always `escalated`, with a diagnosis |
| `stuck-queue` (sweep) | rewrites `agents/responder-agent/auto-queue/r-<run_ts>-seo-<site>.json` from the source run's `recommendations.json` | `fixed`, or `escalated` if data is missing |
| **anything else** (`unknown`, `python-traceback`, `schema-drift`, `missing-file`, `missing-import`, `oom`, `imap-auth-failed`, `stuck-no-output`) | `recipe_llm_diagnose` (disable with `AGENT_DOCTOR_USE_LLM=0`) | see below |

**LLM diagnose.** It shells to `claude --print --dangerously-skip-permissions --max-turns 1 --output-format text`
(PATH `claude` is the claude-pool shim that `agent_run_wrapper.sh` puts
first), with a 180 s timeout. It does not go through
`framework.core.ai_providers`, so the `agent-doctor` override in
`config/ai-defaults.json` (`ollama-5090` / `qwen3:32b` on 2026-09-23) has
no effect on this path. The prompt carries the manifest from the API,
the runbook (first 8 KB), the names (not values) of keys in the agent's
local `.env`, and the log excerpt. It expects JSON back:
`{diagnosis, confidence, fix_type, fix_detail, auto_apply}`.

With `auto_apply=true`:

| `fix_type` | Action | Outcome |
|---|---|---|
| `wait`, `data-issue` | none | `no-op` |
| `manifest-env-inline` | rewrites `${VAR}` in the manifest `entry_command` on disk, then POSTs `/api/agents/register` | `fixed` |
| `stale-service-unit` | POSTs `/api/agents/register` | `fixed` |
| `runbook-tweak` | a second `claude --print` edits the runbook (<16 KB, rejected if it shrinks >40%), then runs `git add`, `commit` **and `push`** on that repo | intended `fixed`, but see defect below |

Any other `fix_type`, or `auto_apply=false`, is escalated with the
diagnosis prepended. The doctor never applies a code edit.

**Known defect (from reading the code; not seen in the fixes-log):**
`_apply_runbook_tweak()` calls `subprocess.run` but `subprocess` is only
imported inside other functions, not at module level. Once the runbook is
found and is under 16 KB, the call raises `NameError`, which the function
catches, so the tweak ends as `escalated` with
`runbook-tweak attempt failed: runbook tweak exception: name 'subprocess' is not defined`.

**Lookup blind spots (seen in logs).** Every path lookup
(`_read_agent_runbook`, `_agent_source_dir`, `_re_register_agent`,
`_apply_manifest_env_inline`, `_apply_runbook_tweak`) searches only
`reusable-agents/agents`, `nsc-assistant/agents` and `specpicks/agents`,
by full agent id and by the id with its first `-` segment stripped. They
**never search `aisleprompt/agents/`**. An aisleprompt agent is found only
when its stripped id matches a shared engine dir in reusable-agents (for
example `seo-opportunity-agent`), and then the doctor reads the engine's
files, not the instance's; manifest auto-fixes and re-register never find
an aisleprompt manifest. `_read_agent_runbook` is narrower still: it tries
the stripped id only under `reusable-agents/agents`, so it also misses
specpicks dirs that drop the `specpicks-` prefix. The 2026-09-23 logs show
`runbook_chars=0` for `aisleprompt-recipe-image-verifier` (runbook at
aisleprompt `agents/recipe-image-verifier/AGENT.md`) and
`specpicks-ebay-counterpart-matcher` (specpicks
`agents/ebay-counterpart-matcher/README.md`). For those agents the LLM
diagnoses without a runbook.

## Outputs

| Output | Where |
|---|---|
| Investigation history | `agents/agent-doctor/fixes-log.jsonl` (append-only; 2,712 entries from 2026-08-24 to 2026-09-23) |
| Dedupe state | `agents/agent-doctor/state/seen.json` |
| Incident queue | `agents/agent-doctor/incidents/` → `incidents-processed/` |
| Weekly counters | `agents/agent-doctor/weekly-stats.json` |
| Standard run artifacts | `agents/agent-doctor/runs/<run-ts>/` (AgentBase: progress, decisions) |
| Escalations | `resilience.notify_operator(agent_id="agent-doctor", severity="medium")`. That writes `agents/agent-doctor/errors/<ts>-_E.json` and sends mail to `FRAMEWORK_OPERATOR_EMAIL` / `OPERATOR_EMAIL` |
| Weekly digest | `send_via_msmtp`, subject `[agent-doctor] weekly summary — N investigations · M escalations`, to `FRAMEWORK_OPERATOR_EMAIL` (default `mperry@northernsoftwareconsulting.com`), from `IMPLEMENTER_FROM` (default `automation@northernsoftwareconsulting.com`). Under the wrapper's `DIGEST_ONLY=1` it is queued to the daily digest. |
| Queue repair | `agents/responder-agent/auto-queue/<request_id>.json` (stuck-queue recipe) |
| Repo writes | `manifest.json` edits and runbook commits+pushes, only through the LLM auto-apply paths above |

**How escalation emails are throttled.** Every doctor escalation uses the
same `(agent-doctor, _E)` key:

- Within one doctor process, only the **first** escalation reaches
  `notify_operator`'s send path. The rest hit the in-memory 1 h cooldown
  and return "suppressed by rate limit".
- Across runs, repeats within `ALERT_DEDUP_WINDOW_S` (24 h), or anything
  over `ALERT_MAX_PER_DAY` (1 immediate alert per day for the whole host,
  not per agent), are folded into the daily digest. Both are tracked in the
  local ledger `~/.reusable-agents/alert-dedup.json`.

So the fixes-log, not the inbox, is the full record. For example, the
03:11Z run on 2026-09-23 escalated 8 agents.

## Goals & metrics

Goals (in storage, `agents/agent-doctor/goals/active.json`; this dir has no goals file):

| Goal id | `target_metric` | Target | Current (2026-09-23) | Notes |
|---|---|---|---|---|
| `goal-fixes-applied` | `fixed` | 50 (increase) | 0 | Auto-recorded from `RunResult.metrics['fixed']`. That is a **per-run** count, so a target of 50 is not reachable per run. The fixes-log holds 0 `fixed` outcomes in its whole window (1,050 escalated, 1,027 no-op, 635 skipped). |
| `goal-escalations-down` | `escalated` | 2 (decrease, baseline 10) | 1 | Accomplished 2026-04-30. Status is sticky. |

`RunResult.metrics` keys: `investigated`, `fixed`, `escalated`,
`skipped_already_seen`, `agents_total`.

`agent-metrics-collector` also writes `goal-doctor-checks-7d = 7` and
`goal-doctor-issues-found-7d = 0` into this agent's timeseries cache every
day. Both are **hardcoded placeholders** in `m_agent_doctor()`, and neither
has a goal definition. Ignore them.

## Configuration

| Env / constant | Default | Meaning |
|---|---|---|
| `FRAMEWORK_API_URL` | `agent.py` fallback `http://localhost:8093` (nothing listens there on whitebeast). Timer runs get it from `secrets.env`; `agent_run_wrapper.sh` otherwise defaults it to the prod dashboard URL. | API to poll/register |
| `FRAMEWORK_API_TOKEN` | env, else the framework repo `.env` (absent on whitebeast) | required |
| `AGENT_DOCTOR_USE_LLM` | `1` | `0` turns off LLM diagnosis. Unknown signatures then escalate as "no recipe". Use it when the claude-pool is exhausted. |
| `FRAMEWORK_OPERATOR_EMAIL` / `OPERATOR_EMAIL`, `IMPLEMENTER_FROM` / `OPERATOR_FROM_EMAIL` | see above | alert and digest addressing |
| `ALERT_DIGEST`, `ALERT_DIGEST_ALL`, `ALERT_DEDUP_WINDOW_S`, `ALERT_MAX_PER_DAY` | `1`, `0`, `86400`, `1` | fleet-wide alert batching (`framework/core/resilience.py`) |
| `STUCK_FALLBACK_MINUTES`, `MAX_RETRIES_PER_SIGNATURE`, `WEEKLY_DIGEST_DAYS`, `STUCK_QUEUED_MIN_AGE_MIN` | 30, 2, 7, 30 | module constants in `agent.py` |

## Short-circuit & idempotency

It does not override `signals()`. The seen/fixes-log dedupe keeps a quiet
tick free of LLM calls: one `/api/agents` call, one
`/api/implementer/batches` call, and a full `fixes-log.jsonl` read for each
failed or stuck agent that is already-seen.
Recipes are meant to be idempotent, because the doctor may apply the same
recipe to several agents in one tick.

## Running & inspecting

```bash
systemctl --user start agent-agent-doctor.service          # full sweep via systemd (uses secrets.env)
tail -40 /tmp/reusable-agents-logs/agent-agent-doctor.log  # timer runs
ls -t /tmp/reusable-agents-logs/agent-doctor-*.log | head  # event-driven runs (host-worker per-run logs)
systemctl --user list-timers | grep agent-doctor
```

To read the fixes-log, use `framework.core.storage.get_storage().read_jsonl("agents/agent-doctor/fixes-log.jsonl")`
with `STORAGE_BACKEND=azure`, or the dashboard Storage tab. There is no
dry-run flag. A manual run can edit manifests, re-register agents and
rewrite auto-queue files.

## Failure modes & troubleshooting

| Symptom | Cause / fix |
|---|---|
| Run fails `missing FRAMEWORK_API_TOKEN` | The token is not in the environment. Timer runs source `~/.reusable-agents/secrets.env`. |
| Every unknown failure escalates `LLM diagnosis failed (claude rc=…)` | claude-pool exhausted or auth-dead. Re-auth with `python3 -m framework.cli.claude_pool login-help`, or set `AGENT_DOCTOR_USE_LLM=0`. |
| `LLM previously classified as non-actionable … silently accepting state` | Intended. Clear it by fixing the root cause. A `fixed` entry, or a new signature, restarts attempts. |
| Same agent "already-seen" forever | `seen.json` matches and fixes-log has an entry for that `last_run_at`. It re-investigates on the agent's next failure. |
| Investigations log `runbook_chars=0` for aisleprompt agents without a shared engine dir | Lookup blind spot (above). |
| Inbox shows 1 doctor email, but the fixes-log shows many escalations | Alert throttling (above). Read the fixes-log or the daily digest. |

## Adding a recipe

1. Write `recipe_<signature>(target, ctx) -> (outcome, notes)`. `outcome`
   is one of `fixed`, `no-op`, `escalated`, `skipped`. `ctx` carries
   `agent` (the API row) and `excerpt`.
2. Register it in `RECIPES`, or add a prefix rule in `_resolve_recipe`.
3. Add or adjust the pattern in `_classify_error`. Order matters: first
   match wins.
4. Reproduce with a failing run that produces the pattern, run the doctor,
   and confirm the fixes-log entry.

## Related agents

- **Feeds it:** every AgentBase agent (post-run hook), `host-worker.sh`
  (timeouts, rc≠0, trap), the framework API (`/api/agents`,
  `/api/implementer/batches`).
- **It acts on:** `implementer` / `auto-queue-drainer` (stuck-queue
  re-queue), `responder-agent` (`mint-token.py`), any agent's manifest or
  runbook (LLM auto-fix).
- **Overlaps with:** the `keep-the-lights-on` skill, which does the
  in-session on-call, and `agent-metrics-collector`, which writes the
  placeholder metrics above.
