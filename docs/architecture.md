# Reusable-Agents Framework Architecture

> The big-picture map of how the framework, agents, site repos, and
> external integrations fit together. Read this before you wire a new
> agent or wonder why a piece of state lives where it does.
>
> Last audited against the code and the native-Linux RTX 5090 fleet host
> (hostname `voidsstr-OMEN-by-HP-45L-Gaming-Desktop-GT22-3xxx`; it replaced
> the WSL2 `whitebeast` around 2026-08-24) on 2026-09-23. Where a
> statement describes live state (counts, what is running), it is dated.

## Where to find agent docs

This page covers the plumbing. For individual agents:

| You want | Read |
|---|---|
| Every agent, by category, with schedule and code path | [`agents-catalog.md`](agents-catalog.md) |
| Index of the framework repo's engines and ops agents | [`../agents/README.md`](../agents/README.md) |
| Index of AislePrompt's agents | `/home/voidsstr/development/aisleprompt/agents/README.md` |
| Index of SpecPicks' agents | `/home/voidsstr/development/specpicks/agents/README.md` |
| The few agents still wrapped from nsc-assistant (read-only here) | `/home/voidsstr/development/nsc-assistant/agents/README.md` |
| One agent's runbook | `AGENT.md` and/or `README.md` in the agent's dir. Which one varies per agent (e.g. `backlog-dispatcher-agent` and `digest-rollup-agent` have only `README.md`), so follow the link in the repo's `agents/README.md` index |

## The three-tier model

```
┌──────────────────────────────────────────────────────────────────────┐
│  Site repos  ←  application code, per-site manifests + site.yaml,    │
│                 site-specific agents                                 │
│  aisleprompt/agents/   specpicks/agents/                             │
│  (nsc-assistant/agents/ still holds 8 registered wrappers)           │
└──────────────────────┬───────────────────────────────────────────────┘
                       │ install/register-agent.sh → POST /api/agents/register
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  reusable-agents framework                                           │
│   • framework/core/   Python lib: AgentBase, storage, dispatch,      │
│                       priority, handoff, scheduler, goals, …         │
│   • framework/cli/    shell-callable entry points (drainer, status,  │
│                       priority, claude_pool, touch_manifests, …)     │
│   • framework/api/    FastAPI app + host-worker.sh                   │
│   • framework/ui/     React dashboard (served from Azure)            │
│   • framework/agent_run_wrapper.sh   ExecStart wrapper for timers    │
│   • agents/<id>/      shared engines + fleet ops agents              │
│   • install/          register, deploy, standup, re-auth scripts     │
└──────────────────────┬───────────────────────────────────────────────┘
                       │ framework.core.storage.get_storage()
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Azure Blob Storage — account nscagentstorage, container `agents`    │
│  (LocalFilesystemStorage for tests/dev)                              │
│     agents/<id>/…          per-agent state, runs, queues             │
│     registry/…             master registry + event log               │
│     config/…               operator-editable framework config        │
│     framework/…            fleet-wide primitives (defer backoff, …)  │
│     digest-queue/ digest-archive/ _trigger-queue/ decommissioned/    │
└──────────────────────────────────────────────────────────────────────┘
```

The boundary is intentional: **agent application logic** lives in site
repos (or in a shared engine in `reusable-agents/agents/`), **agent
infrastructure** lives in the framework, and **shared mutable state**
lives in blob storage. Agents do not call into each other's processes.
They communicate through storage: each other's run-dirs (read via
`framework.core.peer_runs` / `run-index.json`), handoff queues, the
responder auto-queue, the digest queue, and responses-queues.

`framework/core/messaging.py` (`shared/messages/`, `shared/inboxes/`)
and the `AgentBase.message()` / `inbox()` helpers still exist, but no
agent in the three site repos calls them and there are no `shared/`
keys in storage (checked 2026-09-23).

## What runs where

### On the fleet host (native-Linux RTX 5090, repos under `/home/voidsstr/development`)

The fleet host is the native-Linux RTX 5090 machine (hostname
`voidsstr-OMEN-by-HP-45L-Gaming-Desktop-GT22-3xxx`; it replaced the WSL2
`whitebeast` around 2026-08-24). Everything that *executes* agents is a
systemd `--user` unit on this one host.

| Component | Unit / location | What it does |
|---|---|---|
| Framework API | `reusable-agents-api.service` | `python3 -m uvicorn framework.api.app.main:app --host 127.0.0.1 --port 8090`. Registry, runs, status, triggers, and the systemd timer writer (the register route). Token auth (`FRAMEWORK_API_TOKEN`); an unauthenticated request gets 401. Background threads: `snapshot_updater` (writes `registry/agent-snapshot.json`, interval `SNAPSHOT_UPDATER_INTERVAL_S`, default 5 s), the ghost reaper (every 60 s), and the batch reaper (`framework.core.batch_reaper`, once at startup and then every 300 s). |
| host-worker | `reusable-agents-host-worker.service` | `framework/api/host-worker.sh`. Every `POLL_INTERVAL_S` (default 15 s) it pulls `_trigger-queue/*.json` blobs into `/tmp/agent-trigger-queue/` (`framework.cli.pull_blob_triggers`), sorts jobs by priority tier (`framework.cli.priority`), and runs each job's `entry_command` detached via `bash -c`, under `timeout` `AGENT_DEFAULT_TIMEOUT_S` (default 7200 s, per-agent `AGENT_TIMEOUT_<ID>`). Job log: `/tmp/reusable-agents-logs/<id>-<run_id>.log`. A `flock` on `/tmp/reusable-agents-host-worker.lock` keeps it single-instance. |
| Auto-queue drainer | `auto-queue-drainer.service` | `python3 -m framework.cli.auto_queue_drainer --interval 15 --idle-backoff 60`. Drains `agents/responder-agent/auto-queue/` (see [dispatch graph](#the-dispatch-graph)). |
| Agent timers | `agent-<id>.timer` + `agent-<id>.service` in `~/.config/systemd/user/` | One pair per registered agent with a cron. On 2026-09-23 there were 69 `agent-*.timer` units: 67 enabled, 2 disabled (`oauth-heartbeat-agent`, `specpicks-scraper-watchdog`). 68 are framework-written. `agent-sessions-save.timer` is hand-written: it fires every 5 min (`OnUnitActiveSec`), runs nsc-assistant's `agent-session-snapshot/agent-sessions.sh` without the wrapper or `secrets.env`, and its registry id `agent-session-snapshot` has no cron. The registry itself held 85 ids. The other 17 are disabled or have no cron (e.g. `implementer`, `deployer`, the engine ids `catalog-audit-agent` / `indexnow-submitter` / `ebay-product-sync-agent`, and retired personal agents). Log: `/tmp/reusable-agents-logs/agent-<id>.log`. |
| Implementer runs | transient `agent-dispatch-implementer-<site>-<ts>` scopes | Spawned on demand by `framework.core.dispatch` (`systemd-run --user --scope`). Log: `/tmp/reusable-agents-logs/dispatch-implementer-<site>-<ts>.log`. |
| Host-only timers (not agents) | `touch-agent-manifests.timer` (Sun 03:40), `site-consistency-audit.timer` (05:40, 17:40), `specpicks-visible-categorizer.timer` (04:20) | Plain units, not registered with the framework: no run-index, no dashboard tile. |
| claude-pool | `~/.reusable-agents/claude-pool/` (`profile-1`..`profile-5`, `bin/claude` shim, `state.json`) | `agent_run_wrapper.sh` prepends `bin/` to `PATH` unless `CLAUDE_POOL=0`, so any `claude` call picks a pool profile. |
| Local model / search services | Ollama `127.0.0.1:11434`; SearXNG (docker) `127.0.0.1:8888`; `local-image-gen.service` (`:7861`) | Called by agents, not part of the framework. `local-image-gen.service` was disabled and inactive on 2026-09-23. |

Other notes:

- The API and UI are **not** run from `docker compose` on the fleet host.
  `docker-compose.yml` still maps 8090/8091 for local dev, but on
  2026-09-23 nothing listened on 8091 or 8093. `install/register-agent.sh`
  defaults `FRAMEWORK_API_URL` to `http://localhost:8090`.
- `secrets.env` sets `FRAMEWORK_API_URL` to the local API on this host.
  Without it, `agent_run_wrapper.sh` falls back to the Azure API URL and
  `host-worker.sh` falls back to `http://localhost:8093`.
- The same host runs unrelated services (retro game servers, the
  `retro-chat-daemon` / `retro-chat-brain` units). They share the box,
  not the framework.

### In Azure

- **Dashboard:** Container App `agents` in resource group `nsc-apps`,
  containers `agent-api` + `agent-ui`, FQDN
  `agents.happysky-24190067.eastus.azurecontainerapps.io` (HTTP 200 on
  2026-09-23). Built and pushed by `install/deploy-azure.sh` (images
  `agents-api` / `agents-ui` in `nscappsacr`). On 2026-09-23 both
  containers ran tag `20260610-1011`, so framework API/UI changes made
  after 2026-06-10 are not on the Azure dashboard until the next deploy.
  It runs with
  `STORAGE_BACKEND=azure`, so it reads the same blobs the host writes.
  It cannot execute agents. Its "Run now" writes
  `_trigger-queue/<agent>-<run_id>.json`, which the host-worker pulls.
- **State:** storage account `nscagentstorage`, container `agents`.

### What does NOT run in the framework

- **A model server.** Chat-style calls go through
  `framework.core.ai_providers` (`config/ai-defaults.json`,
  `config/ai-providers.json`). Code edits go through the claude-pool or
  the `framework.core.code_editor` chain (`config/code-editor-config.json`).
  Local models are Ollama on the host.
- **The sites themselves.** AislePrompt and SpecPicks have their own
  deploys. The framework's `deployer` runs each site's `deployer:` recipe
  from its site config.

## Scheduling: how a manifest becomes a timer

1. `install/register-agent.sh` (called by `install/register-all-from-dir.sh`
   and each repo's `agents/register-with-framework.sh`) POSTs the
   manifest to `/api/agents/register`. `register-all-from-dir.sh` skips
   manifests with `metadata.is_blueprint: true`.
2. `registry.register_agent()` updates `registry/agents.json` under the
   storage lock, writes `agents/<id>/manifest.json`, and appends to
   `registry/events.jsonl`. Runbook, skill, and readme bodies sent in the
   request land at `agents/<id>/runbook.md`, `skill.md`, and `readme.md`.
3. If the request has a `cron_expr` and `entry_command` (and
   `autowire_cron`, default true), `scheduler.write_systemd_units()`
   writes `agent-<id>.service` and `agent-<id>.timer`. It then enables
   and starts the timer if the request's manifest is `enabled`, or stops
   and disables it if not.

The registry and the timer can disagree. `register_agent()` never
re-enables an id the registry already holds as disabled, but the timer
step follows the `enabled` flag in the request. Re-registering a
manifest that says `enabled: true` restarts the timer of an agent an
operator disabled in the registry.

The generated service (`framework/core/scheduler.py` `SERVICE_TEMPLATE`):

- `Type=oneshot`, `WorkingDirectory=<repo_dir>`.
- `EnvironmentFile=-/home/voidsstr/.reusable-agents/secrets.env`. The
  leading `-` means a missing file is ignored silently, so a clean
  `systemctl start` does not prove the credentials loaded.
- `ExecStartPre=-/bin/mkdir -p /tmp/reusable-agents-logs`. `/tmp` is
  wiped on reboot, and without this every unit failed with `209/STDOUT`
  after the 2026-08-30 reboot.
- `ExecStart=/bin/bash framework/agent_run_wrapper.sh <id> <entry_command>`.
  The entry command is wrapped in `/bin/sh -c '…'` when it starts with a
  `VAR=value` prefix or uses shell metacharacters.
- `Environment=AGENT_ID=<id>` and `AGENT_TRIGGERED_BY=cron`, plus the
  API's own `AZURE_STORAGE_CONNECTION_STRING` / `AZURE_STORAGE_CONTAINER`
  / `STORAGE_BACKEND` values, forwarded as `Environment=` lines.
- Output is appended to `/tmp/reusable-agents-logs/agent-<id>.log`.

The timer is `OnCalendar=<cron_to_oncalendar(cron)>` with `Persistent=true`.

**Timezone gotcha.** `write_systemd_units()` accepts a `timezone`
argument but never uses it, and the generated `OnCalendar=` has no zone.
Every timer fires in host-local time (America/Detroit on the fleet host),
whatever the manifest's `timezone` says. A manifest reading
`0 11 * * *` + `UTC` fires at 11:00 local.

**Drop-ins the framework does not write** (they survive re-registration,
because it rewrites only the base units):

| Drop-in | Effect |
|---|---|
| `~/.config/systemd/user/service.d/10-fleet-path.conf` | Sets `PATH` for every user service, with `~/.reusable-agents/claude-pool/bin` FIRST so a bare `claude` goes through the pool (rotation + limit tracking) instead of the operator's personal login. Written by `standup-fleet-host.sh` phase `claude-pool`. Node from nvm is otherwise invisible to systemd. |
| `agent-<id>.timer.d/20-stagger-gpu.conf` (6 image agents: the recipe-image archiver/refiller/verifier, both article-hero curators, the news-hero curator) | Replaces the manifest schedule with staggered minutes and `Persistent=false`, so GPU jobs don't collide. |
| `agent-digest-rollup-agent.timer.d/10-daily.conf` | Replaces the manifest's `16 */5 * * *` with daily 07:30. The manifest still says `enabled: false`, so re-registering it as-is stops and disables the timer. |

**What `agent_run_wrapper.sh` adds to every cron-fired run:** a
`starting` status write via `framework.cli.status`; `DIGEST_ONLY`
(default `1`); `PYTHONPATH` with the framework repo; the claude-pool
`PATH`; `CLAUDE_POOL_REAL_CLAUDE=~/.local/bin/claude-via-proxy` when that
wrapper exists (per-profile egress; disable with `AGENT_USE_PROXY=0`);
and a `RUN_TS`. After the entry command exits it writes `success` or
`failure` with `--skip-if-terminal`, so an AgentBase agent's own terminal
status (for example `blocked`) is not overwritten. It exits with the
entry command's return code.

Runs triggered through the host-worker do **not** go through the
wrapper. The host-worker writes its own status and runs `bash -c
"<entry_command>"`.

## Agent lifecycle (Python `AgentBase` subclass)

```
trigger (systemd timer / host-worker "Run now" / dispatch scope)
       ▼
┌──────────────────────────────────────────────────────────────────────┐
│ AgentBase.run_once()                                                 │
│   1. setup()                once-per-process init hook               │
│   2. heartbeat thread       status.json updated_at every 30 s while   │
│                             state is running/starting                │
│   3. pre_run()              load state/latest.json (iteration_count, │
│                             state); status=starting; drain           │
│                             responses-queue/ → responses-archive/;   │
│                             drain handoff-queue/ into                │
│                             self.inbound_handoffs; resolve pending   │
│                             confirmations matched by request_id      │
│   4. _check_short_circuit() signals() hash vs                        │
│                             state["_auto_signals_hash"]              │
│   5. run()                  your code → RunResult                    │
│   6. post_run(result)       persist everything (list below)          │
│   7. auto-recovery          status=failure → invoke_doctor()         │
│   8. teardown()                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

`run()` exceptions are mapped as follows: `ConfirmationPending` →
`blocked`, `ConfirmationRejected` → `cancelled`, anything else →
`failure` with the traceback in `error_text`.

**`RunResult` fields:** `status` (`success` | `failure` | `blocked` |
`cancelled`; any other value sets the dashboard status to `idle` and
skips the doctor), `summary`, `summary_md`, `next_state`, `metrics`,
`progress`, `error_text`, and `short_circuited`.

**What `post_run()` writes, in order:**

1. `runs/<ts>/progress.json`: status, metrics, summary, timings,
   iteration count, and `triggered_by`.
2. `run-index.json`: `total_runs` plus the 50 most recent entries. The
   dashboard and `backlog-dispatcher-agent` read this instead of
   listing `runs/`.
3. **Auto-dedup.** If `runs/<ts>/recommendations.json` exists, recs
   whose title this producer emitted in an earlier run get
   `duplicate: true` (`framework.core.producer_history`, history at
   `state/emitted-titles.json`).
4. **Verifications.** Recs marked `shipped` or `implemented` get
   `runs/<ts>/verifications/<rec_id>.json`.
5. **Goal progress.** Layer (a) is an explicit
   `runs/<ts>/goal-progress.json` written by the agent. Layer (b)
   applies to every active goal whose `target_metric` is a key in
   `result.metrics`: the value is recorded via `goals.record_goal_progress`,
   and `goal-progress.json` is written if the agent did not write one.
6. `runs/<ts>/context-summary.md`, taken from `summary_md`, or a
   summary of `decisions.jsonl` if `summary_md` is empty.
7. `state/latest.json` and `state/history/<ts>.json`, both from
   **`result.next_state`**.
8. Final status, then the live-LLM-tail blob is finalised.
9. The run-summary email. On `success`, it is queued to `digest-queue/`,
   unless the run short-circuited or its summary matches the no-op
   pattern ("nothing to do", "throttled:", "no new …"). On `failure`
   (and any other non-success status, e.g. `cancelled`), `post_run`
   calls `send_via_msmtp` for the manifest `owner` (or
   `AGENT_DEFAULT_OWNER_EMAIL`). The code comment says failures "send
   immediately", but `send_via_msmtp` first runs the digest gate
   (`shared.site_quality.maybe_queue_to_digest`). With `DIGEST_ONLY=1`,
   which the wrapper sets for every timer run and which is also the
   default when unset, the failure summary is **queued to
   `digest-queue/` like a success**. On 2026-09-23 the queue held
   `failure` summaries from `specpicks-benchmark-research-agent`,
   `specpicks-ebay-counterpart-matcher`, and
   `specpicks-site-functional-tests`. A real send happens only with
   `DIGEST_ONLY=0`. Graph is tried first when
   `~/.reusable-agents/responder/.oauth.json` exists, then msmtp. On
   `blocked`, nothing is sent. Agents opt out with
   `send_run_summary_email = False`, and `AGENT_SUMMARY_DIGEST=0`
   skips the `queue_for_digest` step (the digest gate above still
   applies).

**Short-circuit: the hash must survive `next_state`.** When `run()` is
not skipped, `_check_short_circuit()` stores the new hash in
`self.state`. `post_run()`, however, persists only `result.next_state`.
An agent that returns a `RunResult` without `next_state`, or with a
freshly built dict, drops `_auto_signals_hash`, and its `signals()` can
never fire. Return `next_state={**self.state, ...}`. Several live agents
have this bug; their runbooks say so. `AGENT_FORCE_RUN=1` bypasses the
short-circuit for one run. It takes effect only if it is in the agent
process's environment, so it works with `python3 agents/<id>/agent.py`
but not as a prefix on `systemctl --user start`.

**Paths that skip parts of the lifecycle:**

- Agents whose `main()` calls `agent.run()` instead of `run_once()`
  (for example `ebay-product-sync-agent`) get the heartbeat but no
  `post_run`: no `progress.json`, no run-index entry, no goal tracking.
- Registered entries that are not AgentBase at all get only the
  wrapper's start/end status and no run-index. On 2026-09-23 these were
  `site-goals-tracker` (both instances), `goals-tracker`,
  `agent-metrics-collector`, `aisleprompt-conversion-optimizer`, both
  `user-growth-strategist` instances, and
  `specpicks-newsletter-digest-sender`. CLAUDE.md requires converting
  them when they are next changed. The hand-written `sessions-save`
  unit (a bash script) does not even use the wrapper, so it writes no
  status at all.

**Liveness:** the ghost reaper (in the API) flips a `running`/`starting`
status to `failure` once `updated_at` is older than
`AGENT_STALE_RUN_GRACE_S` (default 900 s). A failed run queues a doctor
incident at `agents/agent-doctor/incidents/<incident_id>.json`, plus a
local trigger file `/tmp/agent-trigger-queue/agent-doctor-<incident_id>.json`,
deduped per (agent, error class) within the cooldown.

## Run-dir layout

Every AgentBase run writes to `agents/<agent-id>/runs/<UTC-ts>/`. What
you find there depends on the agent:

| File | Written by | Present |
|---|---|---|
| `progress.json` | `post_run` | every AgentBase run |
| `decisions.jsonl` | `DecisionLog` (`self.decide`, `self.decisions.*`) | when the run logged decisions |
| `context-summary.md` | `post_run` | every run |
| `goal-progress.json` | the agent (layer a) or `post_run` (layer b) | agents with goals bound to metrics |
| `recommendations.json` | producers (SEO, PI, catalog-audit, competitor-research, feedback-triage, …) | producers only |
| `verifications/<rec_id>.json` | `post_run` | once recs are shipped/implemented |
| `llm-output.jsonl` | `framework.core.llm_stream` | agents that stream LLM output |
| `dispatch-batches.json`, `handoffs-sent.json`, `applied-recs.json`, `_ship_status.json`, `deploy.json`, `changes/` | the implementer and deployer, written back into the **producer's** run-dir | producer runs that were dispatched |
| agent-specific (`data/`, `snapshot.json`, `goals.json`, `pages.jsonl`, `email-rendered.html`, …) | the agent | varies |

Example, `specpicks-seo-opportunity-agent` run `20260923T163000Z`:
`_ship_status.json applied-recs.json changes/ context-summary.md data/
decisions.jsonl deploy.json dispatch-batches.json goal-progress.json
goals.json handoffs-sent.json progress.json recommendations.json
snapshot.json verifications/`. The implementer's own run-dir for the
same day (`20260923T181642Z`) held only `progress.json` and
`decisions.jsonl`. Its work products land in the producer's run-dir.

Errors captured by `framework.core.resilience` go to
`agents/<id>/errors/<ts>-<ErrorClass>.json`, not into the run-dir.

**Listing caveat.** `StorageBackend.list_prefix()` stops at `limit`
(default 10,000) keys in lexicographic order, so on a long history it
returns the **oldest** slice. Use `run-index.json`, or
`list_child_prefixes()` (a delimiter walk on Azure, not cap-bound). On
2026-08-14 this made `peer_runs.latest_run_ts()` report a three-month-old
run as current.

**Retention.** Framework code never deletes run-dirs, but Azure does.
Lifecycle rule `agents-runhistory-tier-then-delete` on `nscagentstorage`
has `prefixMatch` `agents/`. In Azure's syntax that is the container
name, so the rule covers every key in the container: Cool after 30 days
without modification, Archive after 120, delete after 365 (policy read
2026-09-23; `config/priority-config.json` was already Cool). An Archive
blob cannot be read or written until it is rehydrated. That is where the
`BlobArchived` errors in agent logs come from. For example,
`aisleprompt-progressive-improvement-agent`'s `goals/changes.jsonl` was
last written 2026-05-05 and tiered to Archive on 2026-09-02. `touch-agent-manifests.timer`
(`framework.cli.touch_manifests`) rewrites every `manifest.json` weekly
to keep manifests out of Archive. After 10 of 83 manifests were
archived, registry writes broke fleet-wide on 2026-09-08. Any other
long-lived config blob that nobody rewrites is exposed to the same rule.

## Storage hierarchy (Azure Blob)

Backend selection (`framework.core.storage.get_storage()`): an explicit
argument, else `STORAGE_BACKEND`, else `azure` if
`AZURE_STORAGE_CONNECTION_STRING` is set, else `local`. Azure uses
`AZURE_STORAGE_CONTAINER` (default `agents`). Local uses
`AGENT_STORAGE_LOCAL_PATH` (default `~/.reusable-agents/storage`; the
implementer's `run.sh` sets `~/.reusable-agents/data`). The Azure backend
has a short process-local read cache (`AGENT_STORAGE_READ_TTL_S` default
3 s; negative-hit TTL `AGENT_STORAGE_NEG_TTL_S` default 10 s). It creates the container only when
`AGENT_STORAGE_ENSURE_CONTAINER=1`. `lock()` is a 60 s blob lease on
`<key>.lock`. Intra-host locks such as `site_dispatch_lock` and
`responder_drain_lock` are fcntl files under `FRAMEWORK_LOCK_DIR`
(default `/tmp/agent-framework-locks`).

```
registry/
  agents.json              master registry (written under lock())
  events.jsonl             registry events + runtime status events
  agent-snapshot.json      dashboard rollup (API snapshot_updater)

config/                    operator-editable framework config
  priority-config.json               tier ladder (framework.core.priority)
  required-models.json               hard model requirements
  implementer-allowed-dispatch-kinds.json   dispatch pause switch
  implementer-allowed-handlers.json         rec-handler allowlist
  ai-defaults.json, ai-providers.json       chat provider routing
  code-editor-config.json                   code-editor chain
  article-link-guard-config.json
  live-state-rec-types.json          optional; see work_types.py

framework/
  defer-backoff/<source_agent_id>.json      per-rec defer cooldowns
  demand-signal/<site>.json                 search-demand-agent output
  llm-usage/<YYYY-MM>.jsonl, llm-usage-summary.json, llm-usage-backfill-cursor.json

digest-queue/<ts>-<hash>.json   pending digest entries (digest_queue.queue)
digest-archive/                 entries moved here by digest-rollup-agent
_trigger-queue/<agent>-<run_id>.json   dashboard "Run now" jobs
decommissioned/<date>/          see docs/decommissioned-services.md

agents/<agent-id>/
  manifest.json            registered manifest
  runbook.md skill.md readme.md   bodies uploaded at registration
  status.json              live status (dashboard tile)
  run-index.json           total_runs + last 50 runs
  live-llm-output.txt      live LLM tail
  runs/<run-ts>/           see "Run-dir layout"
  state/latest.json        {iteration_count, state, updated_at}
  state/history/<run-ts>.json
  state/emitted-titles.json      producer_history (auto-dedup)
  state/accumulator.json         rec_memory accumulator (producers that use it)
  goals/active.json              declared goals (goals.py)
  goals/accomplished.jsonl, goals/history/<run-ts>.json, goals/changes.jsonl
  goals/progress/<goal-id>.jsonl, goals/timeseries-cache.json  (metric_helper)
  responses-queue/ → responses-archive/   parsed email replies
  handoff-queue/ → handoff-processed/     inbound handoffs
  handoffs.jsonl                          handoff out/in/outcome rows
  outbound-emails/<request-id>.json       routing metadata for replies
  confirmations/                          @requires_confirmation records
  errors/<ts>-<ErrorClass>.json           resilience error captures

agents/responder-agent/auto-queue/ → auto-queue-processed/
agents/agent-doctor/incidents/ → incidents-processed/, fixes-log.jsonl
```

`context_index.build_daily_rollup()` can write
`agents/<id>/context-summaries/<YYYY-MM-DD>.md`, but nothing calls it
(checked 2026-09-23), so `find_context()` falls back to per-run
`context-summary.md` files.

## The dispatch graph

A rec reaches the implementer by one of four paths. All of them end in
`framework.core.dispatch` or the responder's `trigger_dispatcher`, which
spawn the implementer in a `systemd-run --user --scope`.

```
 producer run() ──► runs/<ts>/recommendations.json (storage)
     │                      │
     │ (A) gated_dispatch_now / dispatch_now
     │                      │ (C) backlog-dispatcher-agent, every minute:
     │                      │     reads run-index.json of 13 producers,
     │                      │     picks unshipped recs, dispatch_now()
     │ (B) queue_recs() ──► agents/responder-agent/auto-queue/<req>.json
     │                      │     auto-queue-drainer.service (15 s / 60 s idle)
     ▼                      ▼
 site lock (skipped for data-only kinds) ──► systemd-run --user --scope
     agent-dispatch-implementer-<site>-<ts>  python3 agents/implementer/agent.py
                                                   │
                                                   ▼ run.sh
                   handoffs → other agents' handoff-queue/
                   required-model gate → defer (deferred.json) or edit
                   code dispatch with commits → agents/deployer (seo-deployer)
 (D) operator email reply → responder-agent (IMAP) → responses-queue / dispatch
```

### (A) Producer-side direct dispatch

`dispatch.gated_dispatch_now(cfg=…)` reads the producer's site config
flag `auto_implement` (default `true`). If it is `false`, nothing is
dispatched: the email is the proposal and the call only logs a decision.
If it is `true`, the call goes to `dispatch_now()`.

`dispatch_now()`:

- Takes a per-site `site_dispatch_lock` (timeout 1800 s), except for
  data-only kinds. `DATA_ONLY_KINDS` defaults to
  `catalog-audit,h2h,article-author,product-hydration`, so a deploy does
  not block a DB migration or an article insert. The check is on the
  caller's `subject_tag`. `backlog-dispatcher-agent` passes `work` for
  article-proposal, feedback-triage, and category-integrity recs (its
  `_subject_tag_from_agent_id` only maps `article-author` ids to
  `article`), so those dispatches do take the site lock.
- Copies the caller's local run-dir to
  `/tmp/reusable-agents-logs/dispatch-rundirs/`. The caller's `RunDir`
  cleanup would otherwise delete it under the detached implementer.
- Writes `dispatch-batches.json` into that copy.
- Spawns `agents/implementer/agent.py` (override:
  `FRAMEWORK_IMPLEMENTER_SCRIPT`) with `RESPONDER_ACTION`,
  `RESPONDER_REC_IDS`, `RESPONDER_SITE`, `RESPONDER_RUN_DIR`,
  `RESPONDER_RUN_TS`, `RESPONDER_AGENT_ID`, `RESPONDER_SUBJECT_TAG`,
  `IMPLEMENTER_RUN_TS`, and optionally `IMPLEMENTER_BACKEND`.
- Retries transient spawn failures (`FRAMEWORK_DISPATCH_RETRIES`,
  default 3). On permanent failure it can write the auto-queue as a
  fallback and email the operator.

Producers that dispatch this way (code grep, 2026-09-23):

- Through `gated_dispatch_now` with `auto_implement: true`: the
  catalog-audit engine (both instances) and
  `specpicks-article-proposal-agent`.
- Through `gated_dispatch_now`: both `user-growth-strategist`s, and the
  shelf-audit engine when its `dispatch_findings` flag is on.
- Through `dispatch_now` directly: `specpicks-head-to-head-agent`.

The SEO, PI, and competitor-research engines also call
`gated_dispatch_now`, but all six of their instances set
`auto_implement: false`, so they never dispatch from the producer side.

### (B) Auto-queue + drainer

`implementation_queue.queue_recs()` (`self.queue_recs`) writes
`agents/responder-agent/auto-queue/<request-id>.json`. Writers of the
auto-queue: `aisleprompt-article-proposal-agent` (its own
`_write_auto_queue`, gated by `auto_implement` plus weekly and daily
caps), `aisleprompt-conversion-optimizer` (only when
`CONVERSION_AUTOQUEUE=1`), `agent-doctor`'s `stuck-queue` recipe (it
re-writes a trigger for a stuck SEO run), `install/requeue-deferred.py`,
and `dispatch_now()`'s permanent-failure fallback. The drainer
reuses `responder.drain_auto_queue()`:

- Takes the global `responder_drain_lock`, non-blocking.
- Sorts items by `(tier, run_ts, key)`. The tier comes from
  `priority.tier_for_agent(source_agent)`, adjusted by pool pressure and
  starvation (see below) and floored at 1.
- Picks a route from `~/.reusable-agents/responder/config.yaml` and
  splits the recs into batches of the site's `max_recs_per_run`.
- Spawns the implementer scope and moves the file to
  `auto-queue-processed/`.
- Dispatches serially. It waits up to `RESPONDER_DISPATCH_WAIT_S`
  (default 7200 s) for each dispatch, then sleeps
  `RESPONDER_DISPATCH_GAP_S` (default 30 s). Concurrent `claude --print`
  calls from one IP caused a 429 lockout on 2026-05-02.

The responder's own tick no longer drains the auto-queue unless
`RESPONDER_DRAIN_AUTO_QUEUE=1` is set. Don't run a second drainer; the
drain lock makes the extra process wasted churn.

### (C) `backlog-dispatcher-agent` (the main path for SEO and PI recs)

Runs every minute. Its module docstring still describes queueing to the
auto-queue; it now calls `dispatch_now()` directly. Each tick it:

1. SIGTERMs implementer scopes whose dispatch log has been silent for
   longer than `BACKLOG_DISPATCHER_STUCK_THRESHOLD_S` (default 1800 s).
2. Applies two capacity caps and skips the tick if in-flight scopes are
   at or above their sum:
   - Claude: `BACKLOG_DISPATCHER_MAX_INFLIGHT` (default 1), lowered
     automatically when fewer claude-pool profiles are healthy.
   - Copilot: `BACKLOG_DISPATCHER_MAX_COPILOT` (default 3).
3. Walks the hard-coded `PRODUCER_AGENT_IDS` (SEO, PI,
   competitor-research, catalog-audit, and article-proposal for both
   sites, both feedback-triage instances, and
   `specpicks-category-integrity-agent`) **in list order**, reading each
   producer's `run-index.json`, newest run first.
4. Skips recs that are `shipped`, `implemented`, `deferred`,
   `duplicate`, or `skipped`; `review_required` without
   `confirmed_for_implementation`; outside
   `config/implementer-allowed-handlers.json` or
   `config/implementer-allowed-dispatch-kinds.json`; or in cooldown per
   `defer_backoff.should_skip()`.
5. Takes up to `BACKLOG_DISPATCHER_MAX_PER_PRODUCER` (default 10) recs
   per producer, sorted by (severity, `tier == auto` first). It picks a
   backend with `implementer_safety.backend_for()` (`claude` or
   `copilot-gpt-4.1`) and calls `dispatch_now(fallback_to_queue=False)`.

It **ignores** the producer's `auto_implement` flag on purpose (the gate
is `if False and …`, disabled on 2026-05-13): the flag stops producer-side
dispatch (A), not this path. It does **not** use the priority tiers;
producer order is the list order.

### (D) Operator email replies

`responder-agent` (every 2 min) polls IMAP. It parses replies
(`implement` / `skip` / `merge` / … on rec ids, ranges, and bulk
filters), writes `agents/<target>/responses-queue/`, flips
`review_required` approvals, and can spawn the implementer. On
the fleet host it has been inert since it was stood up (around
2026-08-23): `~/.reusable-agents/responder/config.yaml`, carried over
unchanged from the 2026-08-13 `whitebeast` standup, is still the example
(`imap.example.com`), so every tick fails DNS but reports success.

### Inside the implementer

`agents/implementer/agent.py` is an AgentBase wrapper around `run.sh`
(about 3,400 lines). In order:

1. If `SEO_AGENT_CONFIG` is not already set, it derives it as
   `examples/sites/<RESPONDER_SITE>.yaml`. This is what happens for
   backlog-dispatcher dispatches.
2. It sends handoffs for recs that carry a `handoff_target` and drops
   them from the batch.
3. It resolves the required model (below).
4. It runs the edit through the claude-pool, or through the framework
   code-editor chain when `IMPLEMENTER_BACKEND` or
   `IMPLEMENTER_FORCE_FALLBACK` selects it.
5. It commits.
6. For code dispatches that produced commits, it chains to
   `agents/deployer/run.sh`, which is AgentBase with runtime id
   `seo-deployer`.

The deployer is skipped for `h2h`, `article-author`, and
`catalog-audit` dispatches (DB-only), when `IMPLEMENTER_SKIP_DEPLOY=1`
is set, or when HEAD did not move. The deployer runs the site config's
`deployer:` stages (test → build → push → deploy → smoke → content
verify), pushes, tags `release/<site>/NNNN`, and marks the batch's recs
shipped. On 2026-09-23 the newest SpecPicks tag was
`release/specpicks/0086`.
`framework.core.release_tagger` (`agent/<id>/release/<ts>` tags)
exists, but the site repos carry no such tags.

**Path scope.** `framework.core.implementer_scope` enforces
`implementer.allowed_paths` / `excluded_paths` from the site config the
implementer loaded. `examples/sites/aisleprompt.yaml` and
`specpicks.yaml` have no `allowed_paths` (checked 2026-09-23), so
backlog-dispatcher dispatches run with the allow-all default. The
scope blocks in the per-site `site.yaml` files (both PI configs and
AislePrompt's SEO config have one; SpecPicks' SEO config does not) apply
only when the dispatching process already carries `SEO_AGENT_CONFIG`.

## Priority, pool pressure, starvation, and defer backoff

**Tiers** (`framework/core/priority.py`, config `config/priority-config.json`;
the stored config equals `DEFAULT_CONFIG` as of 2026-09-23). Lower runs
first. Resolution: manifest `priority_tier` → first matching pattern →
`default_tier` → 5.

| Tier | Label | Patterns |
|---|---|---|
| 1 | SEO + ranking signals | `*-seo-opportunity-agent`, `*-progressive-improvement-agent`, `*-competitor-research-agent` (and bare ids), `seo-implementer`, `seo-analyzer` |
| 2 | AislePrompt content | `aisleprompt-article-proposal-agent`, `aisleprompt-head-to-head-agent` |
| 3 | SpecPicks content | `specpicks-article-proposal-agent`, `specpicks-head-to-head-agent` |
| 4 | Research / catalog hygiene | `*-catalog-audit-agent`, `*-product-hydration-agent`, `*-benchmark-research-agent`, `*-ebay-product-sync-agent`, `*-user-growth-strategist`, `*-kitchen-scraper` |
| 5 | Ops / housekeeping (default) | `agent-doctor`, `digest-rollup-agent`, `responder-agent`, `indexnow-submitter`, `*-scraper-watchdog` |

Tiers order the **auto-queue drain** (B) and the **host-worker trigger
queue** (the host-worker also honours `AGENT_PRIORITY_<ID>` env
overrides). They do not order backlog-dispatcher dispatches (C).

**Pool-pressure demotion** (`effective_tier_with_pool_pressure`, drain
path only): if no authenticated claude-pool profile has Opus available
within `POOL_OPUS_GRACE_S` (a module constant, 900 s, not an env var),
a rec whose `required_model` contains `opus` sinks to tier 9. The check
reads `~/.reusable-agents/claude-pool/state.json` (`CLAUDE_POOL_ROOT`)
and assumes Opus is reachable on any read error.

**Starvation boost** (`site_starvation_boost`, drain path only): counts
`editorial_articles` created in the last 7 days per site, using
`DATABASE_URL_<SITE>` for the sites in `_STARVATION_SITES`, and caches
the result for 300 s. If a site has ≤1 while its peer has >10, that
site's tier gets −2. If it has ≤5 while its peer has >20, it gets −1.
The final tier is floored at 1.

**Defer backoff** (`framework/core/defer_backoff.py`, storage
`framework/defer-backoff/<source_agent_id>.json`): the implementer
calls `record_defer()` when it defers for `required-model-unavailable`,
and the backlog-dispatcher calls `should_skip()` before picking a rec.
The retry ladder is 60 s → 5 min → 30 min → 2 h → 6 h → 12 h (cap).
`record_success()` exists, but nothing calls it (2026-09-23), so the
count is never reset automatically. `reset_all(<agent_id>)` clears one
producer's cooldowns.

## Required model (Opus-only authoring)

`framework/core/required_model.py` resolves a **hard** model tier. If
that model is unavailable the implementer defers instead of falling
back. Resolution order:

1. `rec.required_model_tier`
2. `config/required-models.json` `by_dispatch_kind[<kind>]`
3. `by_agent_id[<source_agent>]`
4. none (soft `recommended_model_tier` applies)

Tier → model: `opus` = `claude-opus-5`, `sonnet` = `claude-sonnet-4-6`,
`haiku` = `claude-haiku-4-5` (`implementer_safety.MODEL_FOR_TIER`).
Retired Opus ids (`claude-opus-4-6/4-7/4-8`) still normalise to `opus`.

Stored config on 2026-09-23:

- `by_dispatch_kind`: `article-author`, `news-author`, `news-rewrite`,
  `h2h`, `h2h-commentary`, `comparison_page_generation`, and `growth`
  are all `opus`.
- `by_agent_id`: `opus` for `specpicks-article-author-agent`,
  `aisleprompt-article-author-agent`, `specpicks-news-writer`,
  `specpicks-head-to-head-agent`, and both `*-user-growth-strategist`
  agents.

The `*-article-author-agent` ids are legacy names. The live article
producers are `*-article-proposal-agent`, so they are covered only
through the dispatch kind.

The implementer's `run.sh` computes `REQUIRED_MODEL` via
`required_model_for_batch()` before choosing any fallback. If the
required model is unreachable, it writes `deferred.json` in the run-dir
and calls `defer_backoff.record_defer()`.

## Inter-agent handoffs (the routing primitive)

Some recommendations are specialist work rather than code edits: new
articles, comparison pages, product hydration, index submission. The
implementer is a code editor, so the **handoff protocol**
(`framework/core/handoff.py` + `work_types.py`) routes those recs to
the agent that owns the work:

```
seo-opportunity-agent analyzer, end of analysis:
  for each rec: work_type, handler = work_types.handler_for(rec.type,
                                         site_routes=cfg.handoff_routes)
                rec.handoff_target = cfg.site_handler_overrides[handler]
                                     or handler           ("" = implementer)
       ↓
rec reaches the implementer via one of the dispatch paths above
       ↓
implementer/run.sh: for each requested rec with a handoff_target
  (deduped against the receiver's queue/archive):
     send_handoff(from_agent="implementer", to_agent=target, …)
       → agents/<target>/handoff-queue/<request-id>.json
       → "out" row in agents/implementer/handoffs.jsonl
  and drops those rec ids from the batch (exit 0 if none remain)
       ↓
target's next pre_run(): drain_handoffs()
       → self.inbound_handoffs; queue file → handoff-processed/;
         "in" row in agents/<target>/handoffs.jsonl
       ↓
target's run() works them; record_handoff_outcome() appends
"outcome" (shipped / in_progress / deferred / rejected)
```

Default routing (`DEFAULT_REC_ROUTING`). Lookup order: site
`handoff_routes` → default table → `("code_edit", "")`:

| rec_type | work_type | default handler |
|---|---|---|
| `new-page-buying_guide`, `-use_case`, `-troubleshooting`, `-brand` (underscore and hyphen spellings) | new_article_creation | `article-proposal-agent` |
| `new-page-comparison` | comparison_page_generation | `head-to-head-agent` |
| `gsc-coverage-not-indexed` | body_md_edit | `article-proposal-agent` |
| `price-stale`, `product-content-incomplete`, `featured-set-curation`, `catalog-thin-description` | price_refresh / product_content_hydration / featured_set_curation | `product-hydration-agent` |
| `catalog-broken-image`, `catalog-miscategorization` | quality_audit_fix | `progressive-improvement-agent` |
| `indexnow-submit`, `gsc-coverage-unknown` | index_submission | `indexnow-submitter` |
| `article-orphan-boost`, `internal-link-*`, `content-expansion`, `snippet-rewrite`, `title-fix`, FAQ/freshness/citation/thin-content body edits, the other `gsc-coverage-*` types | internal_link_addition / body_md_edit / code_edit / schema_markup_fix | `""`, the implementer ships it (these used to go to article-author, and the handoffs rotted) |
| anything else | code_edit | `""`, the implementer |

Site config knobs (keys are rec types in `handoff_routes`, generic
handler ids in `site_handler_overrides`). Illustrative shape, not a
copy of either site's config:

```yaml
handoff_routes:              # rec_type → agent id, overrides the default table
  article-orphan-boost: specpicks-progressive-improvement-agent
site_handler_overrides:      # generic handler id → per-site instance id
  article-proposal-agent: specpicks-article-proposal-agent
  head-to-head-agent: specpicks-head-to-head-agent
```

**Dead-letter risk.** Handlers are generic ids. If a site has no
`site_handler_overrides` entry for one, the handoff lands in the generic
id's queue, and no running agent drains it (the generic
`indexnow-submitter` registry entry is disabled with no timer). On 2026-09-23,
`agents/indexnow-submitter/handoff-queue/` held 93 items,
`article-proposal-agent/` 4, and `head-to-head-agent/` 1. Each
had 0 processed. The cause is in the SEO site configs (checked
2026-09-23):

- `specpicks/agents/seo-opportunity-agent/site.yaml` (and its copy
  `examples/sites/specpicks.yaml`) maps `article-author-agent`,
  `progressive-improvement-agent`, `product-hydration-agent`,
  `head-to-head-agent`, and `catalog-audit-agent`. The routing table's
  handler is now `article-proposal-agent`, which is not mapped, and
  `indexnow-submitter` is not mapped either. `handoff_routes` is `{}`.
- `aisleprompt/agents/seo-opportunity-agent/site.yaml` has no
  `site_handler_overrides` at all, so every AislePrompt handoff keeps
  its generic id.

The digest's stuck check (`digest-rollup-agent` `_handoff_metrics`)
flags inbound handoffs with no outcome after 24 h, and `in_progress`
outcomes older than 48 h. Its inbound set is built from `"in"` rows,
which exist only after a drain, so dead-lettered handoffs never show up
as stuck.

**Live-state rec types** (`work_types.DEFAULT_LIVE_STATE_REC_TYPES`:
`broken-page`, `broken-link`, `fetch-error`, `http-error`, `uptime`,
`cwv-ttfb-slow`, extendable via `config/live-state-rec-types.json`) are
exempt from run-to-run dedupe. They re-measure production, so a past
"resolved" must not hide a current outage.

A new rec type that should not go to the implementer needs an entry in
`DEFAULT_REC_ROUTING`. Unknown types fall through to the implementer.

## Shared engine vs. site-specific agent

### Shared engines (code in `reusable-agents/agents/<engine>/`)

The engine's code is generic. Each site ships a config-only dir
(`manifest.json` + `site.yaml`) whose `entry_command` sets the engine's
config env var and runs the engine. The engine takes its runtime id from
`AGENT_ID`. Only the SEO, feedback-triage, and category-integrity
instances set it in `entry_command`; the others rely on the
`Environment=AGENT_ID=<id>` line in the generated unit (the host-worker
also sets it for "Run now"). The framework records each instance as its
own agent id. No engine id has a timer: engine manifests are absent,
`enabled: false`, `is_blueprint`, or cron-less. A few engine ids still
sit in the registry from earlier registrations (`catalog-audit-agent`,
`indexnow-submitter`, and `ebay-product-sync-agent`, the last
`enabled: true` with no cron), so they show on the dashboard without
ever running.

| Engine | Instances (home repo) |
|---|---|
| `seo-opportunity-agent` (collector → analyzer → finalize in one run) | `aisleprompt-`, `specpicks-seo-opportunity-agent` |
| `progressive-improvement-agent` | `aisleprompt-`, `specpicks-progressive-improvement-agent` |
| `competitor-research-agent` | `aisleprompt-`, `specpicks-competitor-research-agent` |
| `catalog-audit-agent` | `aisleprompt-catalog-audit-agent`, `specpicks-catalog-audit-agent` |
| `shelf-audit-agent` | `aisleprompt-`, `specpicks-shelf-audit-agent` |
| `feedback-triage-agent` | `aisleprompt-`, `specpicks-feedback-triage-agent` |
| `gsc-coverage-auditor` | `aisleprompt-gsc-coverage-auditor`; `specpicks-gsc-coverage-auditor` (nsc-assistant wrapper) |
| `indexnow-submitter` | `aisleprompt-indexnow-submitter`, `aisleprompt-indexnow-bulk`; `specpicks-indexnow-submitter`, `specpicks-indexnow-bulk` (nsc-assistant wrappers) |
| `site-goals-tracker` | `aisleprompt-site-goals-tracker`; `specpicks-site-goals-tracker` (nsc-assistant) |
| `product-hydration-agent`, `ebay-product-sync-agent`, `search-demand-agent`, `category-integrity-agent` | SpecPicks instance only |
| `goals-tracker`, `agent-metrics-collector` | registered from nsc-assistant wrappers |

Example instance command (aisleprompt SEO):
`SEO_DISABLE_UNCHANGED_SHORTCIRCUIT=1 AGENT_ID=aisleprompt-seo-opportunity-agent SEO_AGENT_CONFIG=…/aisleprompt/agents/seo-opportunity-agent/site.yaml PYTHONPATH=…/reusable-agents python3 …/reusable-agents/agents/seo-opportunity-agent/agent.py`.

Fleet ops agents registered straight from `reusable-agents/agents/`:
`agent-doctor`, `backlog-dispatcher-agent`, `digest-rollup-agent`,
`responder-agent`, `catalog-audit-shipped-backfill`, `authority-agent`,
`app-store-opportunity-agent`, and `oauth-heartbeat-agent` (disabled).
`implementer` and `deployer` are chained, not timed. `jcode-agent` is an
unregistered blueprint. `seo-analyzer/` is a legacy copy of the engine's
analyzer, not an agent.

The old `seo-data-collector`, `seo-reporter`, and `seo-deployer` dirs
are gone. They were collapsed into `seo-opportunity-agent` (phases under
`lib/{collector,analyzer,reporter}/`) and `agents/deployer/`.

### Site-specific agents (code in the site repo)

Most agents in `aisleprompt/agents/` and `specpicks/agents/` carry their
own `agent.py`, because their logic is tied to one site's schema or
product. Examples: both `article-proposal-agent`s (separate per-site
implementations, not a shared engine), `specpicks/agents/head-to-head-agent`,
`aisleprompt/agents/kitchen-scraper`, and the recipe-image agents. They
register the same way. See [`repo-boundaries.md`](repo-boundaries.md)
for when code belongs where.

## The data flow for one SEO recommendation

End-to-end trace for SpecPicks, verified 2026-09-23:

```
1. agent-specpicks-seo-opportunity-agent.timer fires
   (OnCalendar *-*-* 00/3:30:00, host-local America/Detroit)
   → agent_run_wrapper.sh → SEO_DISABLE_UNCHANGED_SHORTCIRCUIT=1
     AGENT_ID=specpicks-seo-opportunity-agent
     SEO_AGENT_CONFIG=specpicks/agents/seo-opportunity-agent/site.yaml
     python3 reusable-agents/agents/seo-opportunity-agent/agent.py

2. AgentBase.run_once() → run() drives three phases under one run_ts,
   working in a local RunDir that syncs to
   agents/specpicks-seo-opportunity-agent/runs/<ts>/:
     collector  GSC + GA4 (+ DB / site crawl per site.yaml)
     analyzer   rule passes + batched LLM page audit →
                recommendations.json (analyzer.max_recs_per_run: 12),
                each rec tagged with work_type + handoff_target
     finalize   persists recommendations.json to storage, queues the
                HTML report to digest-queue/, calls gated_dispatch_now()
                (auto_implement: false → no dispatch), records the
                outbound email

3. post_run: progress.json, run-index.json, auto-dedup
   (duplicate=true), goal progress, context-summary.md, state.

4. backlog-dispatcher-agent (next minute) reads the producer's
   run-index.json, picks unshipped recs that pass its filters and caps,
   and calls dispatch_now(subject_tag="seo") →
   systemd-run scope agent-dispatch-implementer-specpicks-<ts>.

5. implementer: SEO_AGENT_CONFIG=examples/sites/specpicks.yaml
   (derived), DATABASE_URL_SPECPICKS from secrets.env, sends handoffs
   for tagged recs, applies the rest, commits.

6. deployer (seo-deployer): test → build → push → deploy → smoke →
   content verify; push; tag release/specpicks/NNNN; mark recs shipped
   in the producer's run-dir.

7. digest-rollup-agent, daily 07:30 host time (timer drop-in):
   renders digest-queue/ entries plus shipped/failed/escalation
   sections into one email and moves the entries to digest-archive/.
   Its WINDOW_HOURS is hard-coded to 3, so entries outside roughly
   04:30–07:30 are archived without being rendered.
```

## Reading next

| What you want to do | Read |
|---|---|
| Find what every agent does | [`agents-catalog.md`](agents-catalog.md), then the per-repo `agents/README.md` indexes above |
| Decide where a new agent's code lives | [`repo-boundaries.md`](repo-boundaries.md) |
| Stand up or rebuild the fleet host | [`fleet-host-standup.md`](fleet-host-standup.md) |
| Keep the fleet running (on-call) | [`keep-the-lights-on.md`](keep-the-lights-on.md) |
| Onboard a new site to the SEO agent | [`seo-onboard-new-site.md`](seo-onboard-new-site.md) |
| What was retired from Azure and where its data went | [`decommissioned-services.md`](decommissioned-services.md) |
| Pick a blueprint for a new agent | [`../blueprints/README.md`](../blueprints/README.md) |
| SEO site config schema | [`../shared/schemas/site-config.schema.json`](../shared/schemas/site-config.schema.json) |
| PI / competitor-research site config schema | [`../shared/schemas/site-quality-config.schema.json`](../shared/schemas/site-quality-config.schema.json) |
| Implementer operator runbook | [`../agents/implementer/README.md`](../agents/implementer/README.md) |
| Backlog dispatcher runbook | [`../agents/backlog-dispatcher-agent/README.md`](../agents/backlog-dispatcher-agent/README.md) |
| Learn the framework Python API | [`../README.md`](../README.md) §Manifest format, §Quick start |
