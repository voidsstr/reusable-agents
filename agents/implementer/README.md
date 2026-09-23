# Implementer (`implementer`), operator runbook

> The fleet's only LLM code/content editor. It takes a batch of approved or
> auto-queued recommendations from a producer agent (SEO, PI, catalog-audit,
> article-proposal, head-to-head, growth strategist and others) and applies
> them. For code recs it edits and commits the site repo, then chains to the
> deployer. For article, H2H and catalog-audit recs it writes DB rows. Every
> user-facing North Star metric (indexed pages, organic clicks, conversions)
> moves only when this agent ships.

> **Read this first: most `.md` files in this directory are LLM prompts, not
> docs.** `run.sh` feeds them verbatim to `claude --print`: `AGENT.md` for
> seo/pi/default dispatches, `H2H.md`, `ARTICLE_AUTHOR.md` and
> `CATALOG_AUDIT.md`. The `framework` LLM mode also passes `AGENT.md` as the
> system prompt. **Editing them changes implementer behavior.**
> `AGENT-AIDER.md` is legacy and no longer read (it was replaced by
> `build-aider-invocation.py`). The manifest's `runbook` field points at
> `AGENT.md`, so the dashboard's Runbook panel shows the prompt. This
> README is the operator runbook.

## At a glance

| | |
|---|---|
| Agent id | `implementer` |
| Home | `reusable-agents: agents/implementer/` |
| Kind | AgentBase python wrapper (`agent.py`, class `Implementer`) that subprocess-runs `run.sh` (~3,400 lines of bash that do the real work) |
| Schedule | **None.** Manifest `cron_expr: ""`, `runnable_modes: ["chained"]`, registry `enabled: false`, no systemd timer or service. It runs **on demand** as a transient `systemd-run --user --scope` unit named `agent-dispatch-implementer-<site>-<ts>`. |
| Entry | `framework/core/dispatch.py` → `_resolve_implementer_script()` → `python3 agents/implementer/agent.py` (override with `FRAMEWORK_IMPLEMENTER_SCRIPT`). The manifest `entry_command` KEY=VAL prefix (`IMPLEMENTER_ALLOW_REC_TYPES=…`) is parsed from the registry and injected by `dispatch._spawn_implementer`. |
| Category | seo |
| Status | live (dozens of dispatches per day; 62 dispatch logs dated 2026-09-23) |
| Logs | `/tmp/reusable-agents-logs/dispatch-implementer-<site>-<ts>.log` (full `set -x` trace by default). Persistent copies of dispatched run dirs are in `/tmp/reusable-agents-logs/dispatch-rundirs/`. |
| Run records | `agents/implementer/runs/<IMPLEMENTER_RUN_TS>/` (AgentBase + `framework.cli.dispatch_run_record`). The source run dir is synced back to `agents/<source>/runs/<run_ts>/`. |

## How work reaches it

| Path | Mechanism |
|---|---|
| Producer direct dispatch | The producer calls `framework.core.dispatch.dispatch_now(...)`, for example `specpicks-head-to-head-agent` and article-proposal agents |
| Auto-queue | Producers or scripts write `agents/responder-agent/auto-queue/<request-id>.json`. The `auto-queue-drainer.service` daemon (`python3 -m framework.cli.auto_queue_drainer --interval 15 --idle-backoff 60`) drains it by tier (`framework/core/priority.py`) and dispatches. Large requests are split into batches (`dispatch-batches.json`). |
| Backlog feeder | `backlog-dispatcher-agent` (every minute) collects undispatched recs, honoring `config/implementer-allowed-dispatch-kinds.json` and `defer_backoff` |
| Email reply | `responder-agent` parses `implement rec-NNN` replies and dispatches |

`dispatch_now` takes a per-site lock (`framework.core.locks.site_dispatch_lock`)
for app-code kinds. Kinds listed in `DATA_ONLY_KINDS` (default
`catalog-audit,h2h,article-author,product-hydration`) skip the lock. Spawn
failures are attempted up to `FRAMEWORK_DISPATCH_RETRIES` times (default 3)
with exponential backoff. After that the request falls back to the
auto-queue and the operator is emailed.

## What it does (run.sh phases)

1. **Setup.** Adds claude-pool `bin/` to PATH (`CLAUDE_POOL=1`) and the
   `claude-via-proxy` wrapper (`IMPLEMENTER_USE_PROXY=1`). When
   `STORAGE_BACKEND` is unset, it sets it to `azure` if
   `AZURE_STORAGE_CONNECTION_STRING` is present, otherwise `local`. Derives `SEO_AGENT_CONFIG` from `RESPONDER_SITE` as
   `examples/sites/<site>.yaml`, which every dispatch on 2026-09-23 used.
   Requires `RESPONDER_REC_IDS` and `RESPONDER_RUN_DIR`.
2. **Run record + context.** Writes the start record, materializes
   `rec-context/<rec-id>/` bundles (`framework/core/rec_context.py`), routes
   recs that carry a `handoff_target` to that agent (deduped), and drops
   article recs whose body already exists in the DB
   (`framework.core.article_progress`). If nothing is left, it exits 0.
3. **Classify.** Captures the pre-run git SHA of `implementer.repo_path`.
   Sets `DISPATCH_KIND` from the recs' `agent_id` suffix
   (`*-head-to-head-agent`→`h2h`, `*-article-proposal-agent`→`article-author`,
   `*-catalog-audit-agent`→`catalog-audit`, otherwise `seo`; a rec of type
   `article-author-proposal` also selects `article-author`). Resolves
   `DATABASE_URL` from `DATABASE_URL_<SITE>` or `database.url_env`.
4. **LLM (default `IMPLEMENTER_LLM=claude`).** Picks the runbook by kind and
   builds the prompt.
   - Probes the pool with a haiku ping. A dead result is cached 15 min in
     `/tmp/claude-pool-disabled-probe`.
   - **Required-model gate:** `framework.core.required_model.required_model_for_batch`
     reads storage config `config/required-models.json`. As of 2026-09-23,
     the dispatch kinds `article-author`, `news-author`, `news-rewrite`,
     `h2h`, `h2h-commentary`, `comparison_page_generation` and `growth`
     require opus (`claude-opus-5`), as do the agent ids listed under
     `by_agent_id`. Examples: `specpicks-head-to-head-agent` and both
     `*-user-growth-strategist` agents.
   - Tier walk: `claude --print --max-turns $IMPLEMENTER_MAX_TURNS`, each tier
     wrapped in `timeout $CLAUDE_TIER_TIMEOUT_S`. Without a requirement it
     starts at the stake-aware model (`implementer_safety.recommended_model_for_batch`),
     default `claude-sonnet-4-6`, and falls through the other tiers.
   - With a requirement, it runs that model only. On failure (rc=124
     timeout, rc=75 pool exhausted) it writes `deferred.json` and **exits
     0**. It never falls back. `defer_backoff.record_defer` is called only
     on the other defer path, where the pool is forced off and the
     Copilot-opus bridge is not serving opus; the timeout path records no
     backoff.
   - The optional Copilot-opus bridge is enabled with
     `IMPLEMENTER_COPILOT_OPUS_BRIDGE=1` (see `reusable-agents/CLAUDE.md`).
5. **Framework code-editor fallback** (rc=75 pool exhausted,
   `IMPLEMENTER_FORCE_FALLBACK=1`, or `IMPLEMENTER_BACKEND=copilot-gpt-4.1`).
   Never used for required-model batches, and skipped for `h2h`.
   - `build-aider-invocation.py` builds per-rec prompts and file lists. It
     applies the `IMPLEMENTER_ALLOW_REC_TYPES` allowlist and the pre-LLM
     scope filter.
   - `framework.core.code_editor` walks its chain
     (`config/code-editor-config.json`).
   - Then these gates run: orphan-scaffolding detection, SSR-mismatch check,
     **post-LLM scope revert** (`framework/core/implementer_scope.py`),
     article-scaffold revert, edit guards, a commit-size warning
     (`IMPLEMENTER_MAX_COMMIT_LINES`, 800), and the **build gate** (`npm run
     build` if `frontend/` was touched, esbuild parse of touched `src/*.ts`;
     on failure it reverts, defers and emails an alert).
   - It commits, then pushes.
6. **Article post-step** (article-author). This block sits **inside the
   step-5 framework-chain branch**, so it runs only when that chain ran and
   returned rc=0. On the claude path, the article insert is whatever the
   `ARTICLE_AUTHOR.md` prompt tells Claude to do. The step INSERTs the
   article from `changes/<rec>.body.md` + `.meta.json` and runs the
   `framework.core.article_link_guard` minima check. When `DATABASE_URL` is
   set it then runs `resolve-article-links.py --apply` and
   `framework.cli.article_heading_repair` (strips leaked outline headings
   from bodies written in the last 6 h). `reconcile-shipped.py` runs either
   way.
7. **Deploy.** Skipped for `h2h`, `article-author`, `catalog-audit` and
   `IMPLEMENTER_SKIP_DEPLOY=1`. If HEAD did not change, it skips the deploy
   **and marks the batch's recs `deferred`** in the source
   `recommendations.json`, so they are not re-dispatched forever. Otherwise
   it runs `agents/deployer/run.sh --run-dir … ` with
   `DEPLOYER_TEST_SCOPE=smoke`. A deployer failure exits with its rc.
8. **Ship evidence.** A ship needs a new commit, ids in `applied-recs.json`,
   or requested recs stamped `implemented` by this run in
   `recommendations.json`. `changes/<rec>.*` files alone do not count. The
   result goes to `_ship_status.json` (only if absent). Then: handoff outcomes, the results
   file, lifecycle markers (`implemented`, `shipped`) propagated to the
   source agent's `recommendations.json`, and a completion email through
   `framework.core.completion_email` (sent as `responder-agent`, subject to
   the digest gate).
9. **Auto-chain.** When a `dispatch-batches.json` manifest exists, it spawns
   the next pending batch.
10. **EXIT trap** (always): end run record, sync the run dir back to Azure,
    stop the live-LLM sidecar, and push unpushed commits with
    `install/push-unpushed.sh` (refuses diverged branches and never fails the
    run).

The defer paths in steps 4 and 5 `exit 0` before steps 6–9 run, so a
deferred batch gets no deploy, ship-status sidecar or completion email; only
the EXIT trap runs.

`agent.py` then turns the artifacts into the RunResult using a fail-closed
`_ship_evidence()`. Precedence: `_ship_status.json`, then `applied-recs.json`,
then recs stamped `implemented` in this run, then `deferred.json`.

## Inputs

- **Env from dispatch:** `RESPONDER_REC_IDS`, `RESPONDER_RUN_DIR`,
  `RESPONDER_SITE`, `RESPONDER_RUN_TS`, `RESPONDER_AGENT_ID` /
  `RESPONDER_SOURCE_AGENT`, `RESPONDER_REQUEST_ID`, `RESPONDER_SUBJECT_TAG`,
  `RESPONDER_ACTION`, `RESPONDER_BATCH_INDEX` / `_TOTAL`, `IMPLEMENTER_RUN_TS`,
  `DISPATCH_LOG_PATH`, optionally `IMPLEMENTER_BACKEND`.
- **Site config:** `SEO_AGENT_CONFIG`, in practice
  `reusable-agents: examples/sites/{aisleprompt,specpicks}.yaml`. Blocks used:
  `implementer:` (`repo_path`, `branch`, `commit_message_template`) and
  `deployer:`.
- **Storage configs:** `config/required-models.json`,
  `config/code-editor-config.json`,
  `config/implementer-allowed-dispatch-kinds.json` (read by the dispatcher;
  `allow: ["*"]` since 2026-06-01).
- **Secrets** (`~/.reusable-agents/secrets.env`): `DATABASE_URL_<SITE>`,
  `AZURE_STORAGE_CONNECTION_STRING`, claude-pool profiles under
  `~/.reusable-agents/claude-pool/`.

## Outputs

- Git commits on the site repo's `branch`, pushed through `push-unpushed.sh`.
  Release tags are stamped by the deployer.
- DB rows: `editorial_articles` (article-author), `comparison_commentary`
  (h2h), catalog/product fixes (catalog-audit).
- Run-dir artifacts: `changes/<rec>.summary.md` / `.diff`,
  `applied-recs.json`, `deferred.json`, `_ship_status.json`,
  `_build_failed.json`, `deferred-by-allowlist.json`.
- Lifecycle flags written back to the producer's `recommendations.json`.
- Emails: completion email (digest-gated) and build-gate alerts (sent
  directly by msmtp).
- **RunResult.metrics:** `rec_count`, `shipped`, `deferred`, `exit_code`,
  plus `unverified` when there is no evidence. Status is `failure` only when
  `run.sh` exits non-zero or required env is missing. Deferrals are
  `success`.

## Goals & metrics

From `goals.json` (mirrored in storage):

| Goal id | target_metric | Target | Last recorded (2026-09-23) |
|---|---|---|---|
| `goal-recs-shipped-per-run` | `shipped` | ≥ 5 recs/run | 6 |
| `goal-deferred-backlog-drain` | `deferred` | 0 | 0 |
| `goal-clean-exits` | `exit_code` | 0 | 0 |

The registry entry is `enabled: false`, so `goals-tracker` omits these from
the daily goals digest. The dashboard Goals tab still has them.

## Configuration (env, `run.sh` defaults)

| Var | Default | Effect |
|---|---|---|
| `IMPLEMENTER_LLM` | `claude` | `claude` / `framework` / `noop` (`none`) |
| `IMPLEMENTER_FORCE_FALLBACK` | `0` | skip claude and use the framework chain (never for required-model batches, which defer instead) |
| `IMPLEMENTER_BACKEND` | unset | `copilot-gpt-4.1` = aider + gpt-4.1 through the Copilot proxy |
| `IMPLEMENTER_CLAUDE_MODEL` | unset | force the starting tier (ignored when a required model applies) |
| `CLAUDE_TIER_TIMEOUT_S` | `1500` | per-tier wall clock |
| `IMPLEMENTER_MAX_TURNS` | `200` | `claude --max-turns` |
| `CLAUDE_POOL` / `CLAUDE_POOL_ROOT` / `CLAUDE_POOL_FAIL_FAST` | `1` / `~/.reusable-agents/claude-pool` / `1` | pool routing; fail-fast gives rc=75 when all profiles are limited |
| `IMPLEMENTER_USE_PROXY` | `1` | route through `~/.local/bin/claude-via-proxy` if present |
| `IMPLEMENTER_COPILOT_OPUS_BRIDGE` | `0` | allow opus through `localhost:4141` |
| `IMPLEMENTER_ALLOW_REC_TYPES` | set in the manifest entry_command | framework-chain rec-type allowlist |
| `IMPLEMENTER_SKIP_DEPLOY` / `_SKIP_BUILD_GATE` / `_SKIP_SSR_GATE` / `_SKIP_ORPHAN_GATE` | `0` | escape hatches |
| `IMPLEMENTER_DISABLE_REQUIRED_TIER` / `_STAKE_TIER` / `_EDIT_GUARDS` / `_CLAUDE_PROBE` / `_ARTICLE_PROGRESS_FILTER` | `0` | disable individual safety layers |
| `IMPLEMENTER_PER_REC_SPLIT` | `0` | framework-chain per-rec phase split |
| `IMPLEMENTER_MAX_COMMIT_LINES` | `800` | commit-size warning threshold |
| `IMPLEMENTER_TRACE` | `1` | `set -x` trace in the dispatch log |
| `DEPLOYER_TEST_SCOPE` | `smoke` | passed to the deployer |
| `IMPLEMENTER_NOTIFY_EMAIL` / `IMPLEMENTER_FROM` / `IMPLEMENTER_MSMTP_ACCOUNT` | unset / `automation@northernsoftwareconsulting.com` / `automation` | completion email |
| `IMPLEMENTER_BUILD_ALERT_EMAIL` / `_FROM` / `_MSMTP_ACCOUNT` | see `run.sh` | build-gate alert (defaults to msmtp account `augusto` since 2026-06-14 because `automation` OAuth was broken, commit `462de1c`) |
| `DATA_ONLY_KINDS`, `FRAMEWORK_DISPATCH_RETRIES`, `FRAMEWORK_IMPLEMENTER_SCRIPT` | see above | read by `dispatch.py` |

**Path scope.** Both scope checkpoints (pre-LLM in
`build-aider-invocation.py`, post-LLM in `run.sh`) read
`implementer.allowed_paths` / `excluded_paths` / `scope_by_dispatch_kind`
from `SEO_AGENT_CONFIG`. As of 2026-09-23 the `implementer:` blocks in
`examples/sites/aisleprompt.yaml` and `examples/sites/specpicks.yaml` declare
**no** `allowed_paths`, and `ScopePolicy` has no default. So for dispatches
that derive their config from `RESPONDER_SITE` (all observed ones), the
scope gates are **no-ops**. Both checkpoints also sit on the
framework-chain path only. On the claude path, Claude commits inside its own
session. Add the scope block to `examples/sites/<site>.yaml` if scope
enforcement is expected (see "Implementer path-scope" in
`reusable-agents/CLAUDE.md`).

## Short-circuit & idempotency

The implementer does not short-circuit, because it only runs when
dispatched. Repeat protection comes from other layers:
- the article-progress filter (bodies already in the DB)
- the pre-flight "already implemented" instruction in the prompts
- the no-commit guard that marks recs `deferred`
- `defer_backoff` (1 m → 12 h) checked by the backlog dispatcher

## Running & inspecting

```bash
# queue depth (see reusable-agents/CLAUDE.md for the one-liner)
systemctl --user status auto-queue-drainer.service
tail -f /tmp/reusable-agents-logs/auto-queue-drainer.log
ls -t /tmp/reusable-agents-logs/dispatch-implementer-*.log | head
grep -v '^+' "$(ls -t /tmp/reusable-agents-logs/dispatch-implementer-*.log | head -1)" | tail -40   # hide set -x noise
systemctl --user list-units --all 'agent-dispatch-implementer-*'
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" "http://localhost:8090/api/agents/implementer/runs?limit=20"

# pause / resume the whole pipeline
systemctl --user stop auto-queue-drainer.service agent-backlog-dispatcher-agent.timer agent-responder-agent.timer
systemctl --user enable --now auto-queue-drainer.service agent-backlog-dispatcher-agent.timer agent-responder-agent.timer
```

Manual dispatch goes through `framework.core.dispatch.dispatch_now(...)` or
an auto-queue file. `run.sh` takes **env vars only** (it has no
`--recs`/`--run-dir` flags). `IMPLEMENTER_LLM=noop` skips the LLM, but the
later phases still run. On an app-code dispatch an unchanged HEAD marks the
batch's recs `deferred` in the source run.

## Failure modes & troubleshooting

| Symptom (log line) | Cause / fix |
|---|---|
| `required model (claude-opus-5) unavailable (rc=124)` or `(rc=75)`, then `deferring all recs` | Opus timed out (1,500 s) or the pool is exhausted. `deferred.json` is written; this path records no `defer_backoff` entry. Check the pool (`python3 -m framework.cli.claude_pool login-help`, and `jq` on `~/.reusable-agents/claude-pool/state.json`). Seen on every H2H dispatch on 2026-09-23. |
| `implemented 8/15 rec(s) … (16 deferred)` | **Reporting bug.** `run.sh` writes `deferred.json` `rec_ids` as a comma-joined **string**, and `agent.py::_ship_evidence` iterates it character by character. The deferred count is then the number of distinct characters (16 for `pair-001…pair-015`). The shipped count (from `applied-recs.json`) is still real. |
| `implemented 7/1 rec(s)` | `_ship_evidence` counts every id in `applied-recs.json` without intersecting with the requested ids. A run dir reused across dispatches can over-count. |
| `deployer failed rc=1` → `implementer exited rc=1` | Usually the deployer's local test suite failed: on 2026-09-23, 7 of 8 such failures were `TEST FAILED scope=legacy` (rc 1, 4 or 5), and 1 failed after the push stage. The commit already exists locally, and `push-unpushed` may warn that the push failed. Fix the tests, then redeploy. The `Failed to authenticate: OAuth session expired` lines in the same logs come from the claude-pool probe (profile-4 auth-dead, pool failed over), not from the deployer. |
| `build-gate: FRONTEND BUILD FAILED` / `BACKEND PARSE FAILED` | The LLM edit broke the build. Files were reverted, the rec was deferred, and an alert email was attempted. That alert goes **directly through msmtp**, and this host has no `~/.msmtprc` (2026-09-23), so expect `build-gate: WARN msmtp returned non-zero (alert NOT sent)` and check the dispatch log instead. |
| `all recs deferred by allowlist` | The rec type is not in `IMPLEMENTER_ALLOW_REC_TYPES` (framework chain only). |
| `FATAL: no code-editor backend ran` | Every framework-chain member was skipped. Check its preflight env and `config/code-editor-config.json`. |
| `no verified ship for N rec(s)` | rc=0 but no commit, no `applied-recs.json` and no sidecar. The LLM bailed. This is reported honestly as unverified. |
| `missing env: RESPONDER_REC_IDS …` | Dispatched without the responder env. Use `dispatch_now`. |
| Unpushed commits pile up | `push-unpushed.sh` refuses diverged branches. A human merge is needed (history: specpicks reached 184 and aisleprompt 265 unpushed commits before the 2026-08-30 fixes `42ef4a0`/`50d4979`). |

## Related agents

- **Producers:** `*-seo-opportunity-agent`, `*-progressive-improvement-agent`,
  `*-catalog-audit-agent`, `*-article-proposal-agent`,
  `specpicks-head-to-head-agent`, `*-user-growth-strategist` and others.
- **Plumbing:** `auto-queue-drainer.service`, `backlog-dispatcher-agent`,
  `responder-agent`.
- **Downstream:** `deployer` (`agents/deployer/`), and
  `catalog-audit-shipped-backfill` (an agent that ports
  `catalog-audit-shipped-backfill.py` from this dir).
- **Helper scripts here:** `build-aider-invocation.py`,
  `resolve-article-links.py`, `reconcile-shipped.py` (called by `run.sh`);
  `azure-shipped-backfill.py`, `catalog-audit-shipped-backfill.py` (manual
  backfills).
- **Legacy / unused:** `specpicks: agents/specpicks-implementer-agent/` is a
  non-functional scaffold, not part of this pipeline.
