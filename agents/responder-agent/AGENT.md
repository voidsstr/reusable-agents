# Responder Agent (`responder-agent`)

> The email-reply half of the human-in-the-loop. It polls the automation mailbox over IMAP, parses operator replies to agent emails (for example `implement rec-001 rec-005`, `skip art-003`, `implement all high`), records each decision in framework storage, and dispatches `implement` / `merge` actions to the implementer. It serves every site metric indirectly: approved recs only ship if a reply gets through. Setup, OAuth and the reply grammar are in [README.md](README.md). This file is the operational runbook.

## At a glance

| | |
|---|---|
| Agent id | `responder-agent` |
| Home | `reusable-agents: agents/responder-agent/`. A stale fork with an empty `cron_expr` also exists in `nsc-assistant`, so the framework repo must register **last** (`install/standup-fleet-host.sh` handles this) |
| Kind | AgentBase python (`agent.py`), wrapping `responder.py:tick()` |
| Schedule | Manifest `*/2 * * * *` (UTC). systemd `OnCalendar=*-*-* *:0/2:00`. Timer **enabled**. That is every 2 minutes, not the "60 s" or "15 min" given in older docs |
| Entry command | `RESPONDER_CONFIG=$HOME/.reusable-agents/responder/config.yaml PYTHONPATH=/home/voidsstr/development/reusable-agents python3 …/agents/responder-agent/agent.py` (wrapped by `framework/agent_run_wrapper.sh`) |
| Category | `ops` |
| Status | **Registered and firing, but inert since host standup (2026-08-13).** `~/.reusable-agents/responder/config.yaml` on whitebeast is the unedited `config.example.yaml` that `install/standup-fleet-host.sh` seeded (`imap.host: imap.example.com`), so every tick fails DNS and processes no mail. See [Failure modes](#failure-modes--troubleshooting) |
| Runbook / deep reference | This file · [README.md](README.md) (OAuth bootstrap, send paths, reply grammar) · [SKILL.md](SKILL.md) · `config.example.yaml` |

## What it does

**`agent.py` → `run()`**

1. Resolve the config: `RESPONDER_CONFIG`, else `~/.reusable-agents/responder/config.yaml`. If the file is missing, return `failure`.
2. Password mode only: if `REUSABLE_AGENTS_IMAP_PASS` is unset, read it from `~/.reusable-agents/imap-password` when that file exists.
3. Load the config. `load_config` validates `imap.host` and `port`, then `auth_method` (`oauth2` needs `oauth_file`; `password` needs `username` and `password_env`). Load state from `~/.reusable-agents/responder/state.json`; a corrupt or empty file falls back to a clean state.
4. Call `responder.tick(cfg, state)`, save the state, and return `success`.

**`responder.tick()`**

1. **Auto-queue drain: off by default.** It runs only when `RESPONDER_DRAIN_AUTO_QUEUE=1` or the config sets `responder.drain_auto_queue: true`. Otherwise it logs "auto-queue drain skipped — responder is email-only". Draining `agents/responder-agent/auto-queue/` belongs to `auto-queue-drainer.service`, which imports this same `responder.py` and reads this same config.
2. **IMAP connect.** `framework.core.resilience.with_retry(retries=3, backoff=2.0, base_delay=2.0)` with a 30 s socket timeout (`imap.timeout_s`). The login is XOAUTH2, using a token minted by `mint-token.py` from `imap.oauth_file`, or a password from the env var named by `imap.password_env`. After the retries run out (4 attempts in all) it calls `notify_operator(agent_id="responder-agent", severity="high")` with `context.phase="imap-connect"` and returns the state unchanged.
3. **Poll.** `SEARCH UNSEEN`. Messages whose `Message-ID` is already in `processed_message_ids` are just marked seen. Each other message goes through `process_message()`, is marked `\Seen`, and has its Message-ID remembered. The stored list is capped at 1,000 ids; it is built from an unordered set, so which ids survive the cap is arbitrary.

**`process_message()`, per email**

1. **Is it one of ours?** It must have at least one of: an `X-Reusable-Agent` header, a run-ts (`YYYYMMDDTHHMMSSZ`) in the subject, or a `[<agent>:<site>]` subject tag. Otherwise it is skipped.
2. **Body.** Uses text/plain if present, otherwise HTML cut at the first quote boundary (Outlook, Gmail or blockquote markers). Quoted reply text is stripped.
3. **Parse actions** (`parse_actions()`; the grammar is in the README):
   - Verbs: `implement`, `skip`, `merge`, `modify`. A line with no verb defaults to `implement`.
   - Ids: `rec-NNN`, `art-NNN`, uids `r-xxxxxxxx`, ranges (`rec-001 - rec-007`, and `1-7` after a verb), and bare number lists after a verb.
   - Bulk filters for implement and skip: `all | auto | review | experimental | critical | high | medium | low`.
   - A `[agent:site]` line prefix is honoured. Sentences are split on `.!?;` and leading "Hey —" framing is dropped. Lines that look like HTML residue are rejected.
4. **Resolve the source agent.**
   - A full-id subject tag is used as-is: `<site>-(progressive-improvement|competitor-research|seo-opportunity|catalog-audit|head-to-head)-agent`.
   - Short tags expand: `[SEO:<site>]` → `<site>-seo-opportunity-agent`, `[H2H:<site>]` → `<site>-head-to-head-agent`, `[ARTICLE:<site>]` → `<site>-article-proposal-agent`.
5. **Find the run dir** (`find_run_dir_for_site`). First choice is framework storage: `agents/<source>/runs/<ts>/`, using the subject's run-ts or else the latest run, materialized to a persistent tempdir `rundir-<agent>-<ts>-*`. The fallbacks are the local FS under `AGENT_STORAGE_LOCAL_PATH`, then the legacy `<runs_roots>/<site>/<ts>/`.
6. **Expand bulk filters** against that run's `recommendations.json`, matching `tier`, `severity` or `priority`.
7. **Record each rec** (`record_action`):
   - Writes `agents/<target_agent>/responses-queue/r-<run_ts>-<rec_id>.json` with `source: "email-reply"`. The target is the matched route's `target_agent`, default `implementer`. That agent's `AgentBase.pre_run()` drains the queue into `self.responses`.
   - For `implement` on a `review_required` rec, it flips `confirmed_for_implementation: true` (with `approved_via: "email-reply"`) in the source run's `recommendations.json`, so the backlog-dispatcher releases the rec. This mirrors the dashboard Approve button.
   - It also appends to the legacy local `responses.json` and the global queue.
   - For `implement`, when the rec carries `goal_ids` and the source agent can be read from the run-dir path, it logs a goal change (`framework.core.goal_changes`) with `metric_before`.
8. **Dispatch** (`implement` / `merge` only, and only when a route matches). Match rules:
   - `match.header: X-Reusable-Agent` plus `equals` (a string or a list)
   - `agent_prefix` (the body line prefix)
   - `agent_subject_tag`
   - `agent_subject_tag_re`
   - `fallback: true`

   The recs are sorted by priority and split into batches of the site's `max_recs_per_run` (default 12), and `dispatch-batches.json` is written into the run dir. Batch 1 is spawned as `systemd-run --user --scope --collect --unit=agent-dispatch-<type>-<site>-<ts> bash <dispatcher.script>`, so it outlives this oneshot unit; the implementer auto-chains the remaining batches. Output goes to `$RESPONDER_DISPATCH_LOG_DIR/dispatch-<type>-<site>-<ts>.log`.
9. **Archive** (only when at least one action was recorded). For each matching outbound-email record (`agents/<src>/outbound-emails/*` with `expects_response`), it writes `agents/<src>/responses-archive/<request_id>.json`, so the dashboard Confirmations page stops listing that email as pending.

## Inputs

| Input | Detail |
|---|---|
| IMAP mailbox | `imap.host` / `imap.mailbox` (INBOX), unseen messages |
| `~/.reusable-agents/responder/config.yaml` | imap, `runs_roots`, `routes`, optional `responder.drain_auto_queue` |
| OAuth file | `imap.oauth_file`. The example value is `.oauth.json`, which holds the Graph-scoped token; `install/setup-imap-oauth.sh` creates a separate Outlook-scoped `.imap-oauth.json` and repoints the config |
| Framework storage | `agents/<source>/runs/<ts>/recommendations.json`, `agents/<source>/outbound-emails/*`, `agents/<source>/goals/active.json` (for goal-change `metric_before`) |
| State | `~/.reusable-agents/responder/state.json` (`processed_message_ids`, `last_uid`) |

## Outputs

| Output | Detail |
|---|---|
| Storage | `agents/<target_agent>/responses-queue/r-<run_ts>-<rec_id>.json`; approval flip in `agents/<source>/runs/<ts>/recommendations.json`; `agents/<source>/responses-archive/<request_id>.json`; goal-change log entries |
| Local files | `dispatch-batches.json` in the (materialized) run dir; legacy `responses.json` and the global response queue; dispatch logs under `/tmp/reusable-agents-logs/` |
| Processes | A `systemd-run --user --scope` running the route's `dispatcher.script` (the implementer's `run.sh`), with env `RESPONDER_ACTION, RESPONDER_REC_IDS, RESPONDER_SITE, RESPONDER_RUN_TS, RESPONDER_RUN_DIR, RESPONDER_BATCH_INDEX/TOTAL, RESPONDER_SOURCE_AGENT, RESPONDER_AGENT_ID, RESPONDER_REQUEST_ID, DISPATCH_LOG_PATH` |
| Email | None per run (`send_run_summary_email = False`). IMAP connect or per-message failures go to `notify_operator` |
| `RunResult` | `success` whenever `tick()` returns, **including when IMAP could not connect**. `failure` only when the config file is missing or an ordinary exception escapes `run()`. An invalid config makes `load_config` raise `SystemExit`, which `run_once()` does not catch as a failure |
| `RunResult.metrics` | `messages`, `dispatched`, `auto_queue_drained`. These are **always 0**: `agent.py` reads `last_tick_*` keys from the state, and `tick()` never writes them. Read the log for real counts |

## Goals & metrics

There is no goals file in the repo. Framework storage (read on 2026-09-23) holds two goals:

| Goal id | Metric | Current / target |
|---|---|---|
| `goal-zero-stuck-replies` | `unrouted_replies` | 0 / 0 |
| `goal-fast-routing-latency` | `median_route_latency_s` | 60 / 60 |

Neither metric key is emitted by the agent, so these goals never auto-progress. Their values are seeded, not measured.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RESPONDER_CONFIG` | `~/.reusable-agents/responder/config.yaml` | Config path (set in `entry_command`) |
| `REUSABLE_AGENTS_IMAP_PASS` (or whatever `imap.password_env` names) | unset; falls back to `~/.reusable-agents/imap-password` | Password auth only |
| `RESPONDER_DRAIN_AUTO_QUEUE` | unset (off) | `1` = also drain the auto-queue in this process (operator flush tool) |
| `RESPONDER_DISPATCH_WAIT_S` / `RESPONDER_DISPATCH_GAP_S` | `7200` / `30` | Drain mode only: wait for each dispatch, then sleep before the next |
| `RESPONDER_DISPATCH_LOG_DIR` | `/tmp/reusable-agents-logs` | Per-dispatch log directory |
| `AGENT_STORAGE_LOCAL_PATH` | `~/.reusable-agents/data` | Local-FS run-dir fallback |
| `STORAGE_BACKEND` | `azure` (unit) | Framework storage backend |

Config keys: `imap.{host, port, username, use_tls, mailbox, auth_method, oauth_file, password_env, timeout_s}`, `runs_roots[]`, `routes[].{match.{header, equals, agent_prefix, agent_subject_tag, agent_subject_tag_re, fallback}, dispatcher.{type, script}, target_agent}`, `responder.drain_auto_queue`. The README's `dashboard:` block is **not read** by the current code.

The seeded config has a single route (`X-Reusable-Agent: seo-reporter` → `agents/implementer/run.sh`). Outlook usually strips custom X-headers from replies, so without an `agent_subject_tag` or `fallback` route, replies are **recorded but not dispatched** (the log shows `[no-route]`).

## Short-circuit & idempotency

- No `signals()` override, so every tick runs. A tick with no mail is a few IMAP round-trips.
- Messages are marked `\Seen` and remembered by Message-ID (last 1,000), so a re-run never re-processes a reply. `request_id = r-<run_ts>-<rec_id>` is deterministic, so a duplicate reply overwrites the same queue key.

## Running & inspecting

```bash
systemctl --user start agent-responder-agent.service
tail -n 30 /tmp/reusable-agents-logs/agent-responder-agent.log          # wiped on reboot
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://localhost:8090/api/agents/responder-agent
python3 agents/responder-agent/mint-token.py --check --oauth-file ~/.reusable-agents/responder/.imap-oauth.json
python3 agents/responder-agent/responder.py --once                         # one tick, outside AgentBase
ls /tmp/reusable-agents-logs/dispatch-*.log                                # what was dispatched
```

## Failure modes & troubleshooting

| Symptom | Cause / fix |
|---|---|
| Every tick logs `_connect attempt N/4 failed: gaierror: [Errno -5] No address associated with hostname`, then `success … messages=0` | **The current state on 2026-09-23.** `config.yaml` still has the placeholder `imap.host: imap.example.com` and `username: automation@example.com`. Standup seeded it from `config.example.yaml` "(imap block still needs OAuth)", and `.imap-oauth.json` does not exist. To fix: (1) set `imap.host: outlook.office365.com`, and set `imap.username` to the automation mailbox (or leave it blank to inherit the OAuth `username_hint`); (2) run `bash install/setup-imap-oauth.sh`, which does a device-code bootstrap with Outlook scopes, repoints `imap.oauth_file`, and smoke-tests an IMAP login; (3) watch the next tick's log. Keep the file parseable: `auto-queue-drainer.service` loads it too |
| Runs are green but nothing happens | Expected given the two issues above: IMAP failures don't fail the run, and the metrics are always 0. Judge health from the log (`tick: N unseen messages`, `[recorded]`, `[dispatch]`) |
| `[no-route] no dispatcher matched` | The reply lost its X-header. Add an `agent_subject_tag` / `agent_subject_tag_re` / `fallback: true` route |
| `[skip] no run dir found` | The subject had no resolvable source agent, or the run is gone from storage |
| Replies silently ignored (historical) | 2026-04-29: `[H2H:specpicks]` subjects without a run-ts were dropped; the fix accepts `[agent:site]` tags alone. `art-NNN` ids were not recognized; `_REC_PATTERN` accepts `rec\|art` since commit `ab2fc5e` (2026-05-13) |
| Service stuck `activating` (historical) | 2026-04-27: IMAP hung with no timeout and about 2.5 h of timer firings were dropped. The 30 s socket timeout exists for this |
| Double-shipped recs (historical) | Concurrent drains dispatched the same auto-queue keys. `framework.core.locks.responder_drain_lock` (2026-05-20) guards drains; dispatches in drain mode are serialized (2026-05-02, Anthropic edge rate-limit) |

## Related agents

- **`implementer`**: default `target_agent` and dispatcher script (`agents/implementer/run.sh`).
- **`auto-queue-drainer.service`** (`framework/cli/auto_queue_drainer.py`): owns draining `agents/responder-agent/auto-queue/`, reusing `responder.drain_auto_queue()` and this config.
- **`backlog-dispatcher-agent`**: releases `review_required` recs after the email approval flip.
- **Producers whose replies it can resolve** (via the subject tags above): `*-seo-opportunity-agent`, `*-progressive-improvement-agent`, `*-competitor-research-agent`, `*-catalog-audit-agent`, `*-head-to-head-agent`, `*-article-proposal-agent`. It archives their `agents/<id>/outbound-emails/*` records that have `expects_response` set (written by `AgentBase.record_outbound`).
