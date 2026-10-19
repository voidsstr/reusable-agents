# Deployer (`deployer`, runs as `seo-deployer`)

> The framework's cloud-agnostic ship step. After an implementer batch commits
> application code, it runs the site's `deployer:` block from `site.yaml`:
> **test → build → push → deploy → smoke check → content verify**. On success
> it pushes the commits, tags the release and marks the batch's recs
> `shipped`. It gates hard on tests and smoke checks. Every site change the
> fleet makes for organic clicks, indexed pages or conversions only counts
> once this step puts it live.

## At a glance

| | |
|---|---|
| Manifest id | `deployer` (`manifest.json`: `enabled: false`, empty cron, `task_type: manual`, `runnable_modes: ["chained"]`, `metadata.is_blueprint: true`) |
| Runtime agent id | **`seo-deployer`**. `agent.py` hard-codes it so a chained run is not recorded under the dispatching agent's `AGENT_ID`. Run history lives under `seo-deployer`: the latest 50 runs (the API's page size, back to 2026-09-19) are 30 success / 20 failure as of 2026-09-23. `seo-deployer` has no registry entry of its own. |
| Home | `reusable-agents/agents/deployer/` (`run.sh` shim → `agent.py` AgentBase wrapper → `deployer.py` pipeline) |
| Kind | AgentBase python, **chained** (engine shared by all sites; per-site behaviour comes from each site's `deployer:` block) |
| Schedule | none. No systemd timer or service. `register-all-from-dir.sh` skips it (`is_blueprint`), though a registry entry for `deployer` exists (`GET /api/agents/deployer`, `enabled: false`). |
| Invoked by | `agents/implementer/run.sh` after a batch: `SEO_AGENT_CONFIG=<site.yaml> DEPLOYER_TEST_SCOPE=${DEPLOYER_TEST_SCOPE:-smoke} bash agents/deployer/run.sh --run-dir $RESPONDER_RUN_DIR` |
| Category | `ops` (manifest); the class declares `seo` |
| Status | live via the implementer chain (several specpicks and aisleprompt deploys on 2026-09-23) |
| Docs | this runbook · [`README.md`](README.md) (config reference + example blocks) · recipes: `reusable-agents: examples/deployer/*.yaml` + `examples/deployer/README.md` |

## When it runs

The implementer chains to the deployer after every successful batch
(per-batch deploy), **except** in these cases:

- `DISPATCH_KIND` is `h2h`, `article-author` or `catalog-audit`. These are
  DB-only, with no docker build.
- `IMPLEMENTER_SKIP_DEPLOY=1`.
- The batch made no commit (HEAD unchanged). The implementer then marks
  those recs `deferred` so they are not re-dispatched forever. This was added
  after rec-001 was re-dispatched ~50× overnight on 2026-05-13.

A non-zero deployer exit makes the implementer exit with the same rc. Since
2026-10-19 the chain no longer forces `DEPLOYER_TEST_SCOPE=smoke`: every deploy
runs the site's own gate (`deployer.test`), which is meant to be the in-depth
all-pages suite. A tiered site picks its tier by `test.test_scope` or
`DEPLOYER_TEST_SCOPE` (aisleprompt is pinned to `smoke` until its all-pages
gate lands).

## What it does

`agent.py` (`SEODeployer.run()`):

1. Reads the run dir from `RESPONDER_RUN_DIR` (or `SEO_DEPLOYER_RUN_DIR`).
   If it is missing or not a directory, returns `failure`.
2. Calls `deployer.main()` in-process with `--run-dir` (plus `--skip-test`
   when `DEPLOYER_SKIP_TEST=1`).
3. Reads `<run-dir>/deploy.json` and returns `RunResult` (see Goals &
   metrics). `send_run_summary_email = False`.

`deployer.py main()`:

0. Loads the site config from `SEO_AGENT_CONFIG` (`shared.site_config`). With
   no `deployer:` block it prints "nothing to do" and exits 0. Relative
   `cwd`s resolve against `implementer.repo_path`.
   - **Lock first.** Takes `site_deploy_lock(<deploy.vars.app>)` before the
     tests and holds it through smoke/rollback. Waiting prints a line a minute
     (`DEPLOYER_LOCK_WAIT_S`, default 6600).
   - **Tag.** Stamped after the lock as the UTC time `%Y%m%d-%H%M`. A run that
     waited never reuses its start minute. (On 2026-09-23 two pairs of runs
     shared a tag.)
   - **Already live.** When HEAD equals the gate state's `last_deployed_sha`,
     another run already tested and shipped this commit. Status
     `already-live`: the recs are marked shipped, nothing is built.
   - **Checkout.** One detached worktree of HEAD (`gate_runner.checkout`)
     serves both the test and the build. `deploy.json.sha` records it.

1. **Test → repair → retest (hard gate)** — `gate_runner.GateRunner.run()`
   - **Runs** the scope's command from the checkout as
     `<cmd> --reporter=line,json --output <artifacts>/test-results`. Any
     `--reporter` already in the command is dropped. The env carries
     `GATE_SOURCE_REPO`, `GATE_ARTIFACT_DIR` and
     `PLAYWRIGHT_JSON_OUTPUT_FILE`, and no `DATABASE_URL*` or other
     credentials. Output is streamed to `<artifacts>/<run>/output.log`, with a
     heartbeat line a minute. A timeout (`test.timeout_s`, default 1800)
     kills the whole process group.
   - **Classifies** the result with `deploy_repair.classify` (exit code + output
     + Playwright JSON report): `pass`, `flaky`, `code`, `infra` or `config`.
     Before a code failure counts, the failed tests are re-run alone at
     `--workers=1` (`--last-failed`). Green there → `flaky`, the deploy goes
     on.
   - **infra** → re-run (`repair.infra_retries`, default 2, after
     `infra_retry_wait_s`). Never sent to an LLM.
   - **code** → an Opus repair session (`claude --print` through the
     claude-pool, `--model repair.model`, `--max-turns repair.max_turns`).
     cwd is the checkout, the prompt is `deploy_repair.build_repair_prompt` +
     `REPAIR.md`, and it holds the failing tests with errors, the server log
     tail, the commits since the last green gate, and a one-test re-run
     command. The session commits in the checkout. `deploy_repair.check_repair`
     then vets the commits: implementer scope (`scope_by_dispatch_kind:
     deploy-repair`), protected gate files, and no weakened tests (skip,
     fixme, only, fail, fewer `expect()` calls, a deleted test, or a test
     edit without `TEST-CHANGE-JUSTIFICATION:`). A rejected attempt is reset
     and the next attempt is told why. An accepted one is re-tested. Up to
     `repair.max_attempts` (default 3) within `repair.max_wall_s` (5400).
   - **Green after a repair** → `gate_runner.land` fast-forwards the live
     branch to the repair, or cherry-picks it if the branch moved. A
     conflict aborts the pick, blocks the deploy, keeps the repair at
     `refs/deploy-repair/<site>/<tag>` and emails the operator.
   - **Still red** → status `blocked`, exit 1, and
     `<run-dir>/test-failure-context.json` (failures, signature, repairs,
     artifact paths). The gate state backs off on that failure signature
     (`repair.backoff_s`). Once a full cycle is spent, the operator gets one
     email per signature (`reporter.email`, re-sent after `repair.realert_s`).
     `repair.enabled: false`, storage `config/deploy-repair-config.json`
     `{"enabled": false}` or `DEPLOY_REPAIR_DISABLED=1` stops repairs. The gate
     still blocks.

   The test scope and pre-checks below still apply:
   - **Scope** (`_select_test_scope`):
     - a block with only `test.cmd` runs as scope `legacy` every time;
     - otherwise `DEPLOYER_TEST_SCOPE` wins, then `test.test_scope`;
     - otherwise the scope is `full` when the last full run is older than
       `test.full_interval_days` (default 7), and `smoke` if not.
   - **Local dev server.** If the site declares `local_dev.health_url`, the
     deployer probes it. When it is down it runs
     `local_dev.ensure_running.cmd` and polls until healthy. Still unhealthy
     → status `blocked`, `test.rc=79`.
   - **Placeholders.** `{local_dev_url}` and `{local_dev_port}` in the test
     command are expanded.
   - **Empty test scope.** Output containing "No tests found" or
     "0 tests found" is forced to rc 78 (an empty `@smoke` grep must not
     ship untested code).
   - **Failure.** A gate still red after the loop above sets status `blocked`
     and writes `<run-dir>/test-failure-context.json`, then exits 1.
   - **Full-scope pass.** Records
     `agents/deployer/state/<site>-last-full-test.json`. That file is in the
     repo directory and git-tracked; the current files date from 2026-05-05
     and 05-06.
2. **Build.** Ships the image the gate tested: `docker tag
   <image-under-test> {image}:{tag}` (`build.reuse_tested_image`, default
   true). Without a tested image (`--skip-test`, no test command) it runs
   `build.cmd` in the checkout with `{tag}`, `{image}` and `deploy.vars`
   expanded. Failure → status `failure`.
3. **Push.** Runs `push.cmd`. Failure → status `failure`.
4. **Deploy.** Runs `deploy.cmd`.
   - **Busy retry.** When stderr matches
     `ContainerAppOperationInProgress`, `another operation is in progress`,
     `ResourceGroupOperationInProgress` or `AnotherOperationInProgress`, it
     retries after 60 s, 180 s and 360 s.
   - **Post-flight check.** If rc is still non-zero and `vars.app` and
     `vars.rg` are set, it runs `az containerapp revision list`. When the
     active image ends in `:<tag>`, the deploy counts as a success
     (`verified_via_revision`).
5. **Smoke check.** Only runs when `smoke_check.base_url` and `paths` are
   set.
   - Sleeps `settle_seconds` (default 60), then GETs each path; a code in
     200–399 passes.
   - On a miss it retries once after 30 s.
   - Still failing → `deployer.rollback` (below) redeploys the prior image
     → status `rolled-back`; without a usable rollback config, status
     `failure` and "SMOKE FAILED — manual rollback needed".
5b. **Indexability + latency gate** (`smoke_check.gate`,
   `framework/core/deploy_gate.py`, added 2026-09-25 after audit F115: a
   release that noindexed 99.8% of aisleprompt's recipes shipped through 39
   releases, and a shop-sitemap 500 went unnoticed, because step 5 only
   checks status codes).
   - **Baseline.** Before step 4 the gate measures the live site: every
     `expect_indexable_paths` canary (200, no noindex in X-Robots-Tag or
     meta robots/bingbot, self-canonical, ≥ `min_text_chars` visible text;
     cold TTFB + a warm re-fetch), every child of `sitemap_index` (200 with
     ≥1 URL; RSS/Atom feeds count), and `sitemap_sample` random page URLs.
   - **After the deploy** it measures the same things (same sample URLs) and
     fails only on **regressions** — a check that passed before and fails
     now; a warm TTFB over `warm_max_ttfb_s` that was within it before; a
     sample non-indexable share above `max_bad_ratio` and ≥15 points worse.
     Problems that already existed are warnings, so a broken page never
     blocks the deploy that fixes it. Cold TTFB ≥ `cold_fail_s` is a warning
     (a new revision starts with empty caches).
   - A failing gate is re-measured once after 45 s, then rolls back.
   - If the baseline was expected but could not be taken, the gate only
     reports (`(no baseline — not gating)` warnings) and never rolls back:
     without a baseline a pre-existing problem looks like a regression.
   - Fetches use an honest `IndexabilityCheck` UA — never an AI-crawler UA,
     which the sites log into their AI-traffic tables.
   - Results land in `deploy.json` → `gate{pre,post,failures,warnings,ok}`.
5c. **Rollback** (`deployer.rollback`). `capture_cmd` runs before the deploy
   and must print the image being served (recorded as `prior_image`);
   `cmd` redeploys it via `{prior_image}` when the smoke check
   (`on_smoke_failure`) or the gate (`on_gate_failure`) fails. Cloud
   specifics live in these templates, not in `deployer.py`. The failing
   commits stay on the branch: the next deploy rebuilds them and fails the
   gate again until the regression is fixed.
   `capture_cmd` also runs right after the deploy (`deployed_ref`) and again
   just before a rollback: if the site no longer serves `deployed_ref` (a
   newer or manual deploy landed while the gate ran, or someone already
   rolled back), the rollback is skipped (`rollback.why_not`) so it never
   clobbers a newer deploy.
6. **Content verify.** Runs only as part of the smoke-check step (so only
   when `smoke_check.base_url` and `paths` are set). Skipped when
   `RESPONDER_SKIP_CONTENT_VERIFY=1`.
   - For each `implemented` rec in `<run-dir>/recommendations.json` (either
     shape), it uses `rec.content_check`, or a rule inferred from the rec
     type (below).
   - It fetches the URL and checks that the needle is present. Any miss →
     status `failure`: the deploy is live but the rec's intent is not
     visible (orphan code).

   | Rec type | Check |
   |---|---|
   | `schema-markup`, `schema-product-missing`, `schema-article-missing`, `schema-faqpage-missing`, `schema-howto-missing`, `schema-breadcrumblist-missing`, `schema-organization-missing` (and `ux-improvement` with "json-ld" in the title) | `application/ld+json` present |
   | `onpage-canonical-missing`, `onpage-canonical-issue` | canonical link present |
   | `onpage-twitter-card-missing` | twitter card tag present |
   | `onpage-og-incomplete` | `og:` tags present |
   | `h1-missing` | `<h1` present |
   | `indexing-sitemap-404` | `/sitemap.xml` contains `<urlset` |
   | `article-author-proposal` | slug present (only the `/articles/` prefix is actually tried) |
7. **Success.**
   1. `deploy.json` gets `status: success` and `rollback_cmd` (the expanded
      `rollback.cmd` with the captured `prior_image`; without a rollback
      config, the deploy command with a `<PRIOR_TAG>` placeholder).
   2. `install/push-unpushed.sh <repo_root>` pushes the deployed commits.
   3. `install/tag-release.sh <site> <tag>` stamps
      `release/<site>/NNNN` on HEAD and pushes the tag.

   Steps 2 and 3 run only when `implementer.repo_path` is an existing
   directory.

   A push or tag failure is logged and never fails a successful deploy.
   This push/tag step was added on 2026-08-30 after specpicks and aisleprompt
   had built up 184 and 265 unpushed commits (commit `42ef4a0`).
8. **Mark shipped.** In `<run-dir>/recommendations.json` (bare-list or
   `{recommendations: […]}` shape), every `implemented: true` rec not yet
   shipped gets `shipped: true`, `shipped_at`, `shipped_tag` and
   `shipped_image`. `deploy.json.shipped_rec_count` records the count.

Any step that exceeds `run_step`'s 1800 s timeout → `failure` and exit 2.

## Inputs

- `SEO_AGENT_CONFIG`: a site config with `deployer:`, optionally
  `local_dev:`, and `implementer.repo_path`. Live declarations:
  - `specpicks/agents/seo-opportunity-agent/site.yaml`. Legacy single test
    `bash run-local-suite.sh --reporter=line` in `tests/`. Per its
    2026-08-30 comment, that suite builds the production `Dockerfile.azure`
    image from the working tree and tests it on `:10021`. The block also
    configures an ACR push, `az containerapp update` for app `specpicks`,
    and a 6-path smoke check.
  - `aisleprompt/agents/seo-opportunity-agent/site.yaml`. Tiered `smoke`
    (`@smoke` grep) and `full` Playwright suites against `local_dev` on
    port 4001 (`docker compose up -d frontend backend db`), app
    `aisleprompt`, and a 6-path smoke check.

  (CLAUDE.md's pointer to an nsc-assistant path for the AislePrompt block is
  stale.)
- `<run-dir>/recommendations.json`: the recs the batch implemented.
- Host tools: `docker`, `az` (logged in), plus whatever the site's commands
  call.

## Outputs

- `<run-dir>/deploy.json`, with fields `site`, `tag`, `image`,
  `started_at`/`ended_at`, `test{rc,scope,skipped,stderr_tail}`,
  `build`/`push`/`deploy{rc,…,verified_via_revision}`,
  `smoke{ok,results,retried}`, `gate{pre,post,failures,warnings,ok}`,
  `content_verify`, `prior_image`, `rollback{attempted,rc,…}`, `status`
  (`running|blocked|failure|rolled-back|dry-run|success`), `rollback_cmd`,
  `shipped_rec_count` and `error`.
- `<run-dir>/gate.json`: the gate summary (`ok`, `sha`, `kind`, `rc`, `failures`
  with title/file/line/error, `runs`, `repairs`, `flaky`, `landed`,
  `image_id`). The same object sits in `deploy.json` under `test.gate`.
- `<run-dir>/test-failure-context.json` when the gate stays red: `kind`,
  `reason`, `signature`, `failures`, `repairs`, `artifacts`.
- `~/.reusable-agents/deployer/gate-artifacts/<site>/<tag>-<pid>/`: per gate
  run `output.log`, Playwright `report.json`, `test-results/` (traces,
  screenshots), `container.log` on failure, `image-under-test.txt`; per repair
  `prompt.md` and `session.log`. Kept outside the run dir (which is synced to
  blob storage) and pruned to the newest 20 runs per site.
- `~/.reusable-agents/deployer/gate-state/<site>.json`: red/green, failure
  signature, exhausted cycles, `next_repair_at`, last green and last deployed
  commit, alert times, the last 30 events. `DEPLOY_GATE_STATE_DIR` overrides
  the directory.
- The updated `<run-dir>/recommendations.json` (shipped markers).
- A container image `<image>:<tag>` in the registry, and the new Container
  App revision.
- The git push of the site repo, plus the `release/<site>/NNNN` tag on the
  shipped commit (`RELEASE_SHA`). A green repair also lands its commits on the
  live branch.
- Standard AgentBase artifacts under `agents/seo-deployer/runs/<run_ts>/`
  (`plan` / `result` decisions).

## Goals & metrics

`RunResult.metrics` keys: `tag`, `image`, `sha`, `test_rc`, `build_rc`,
`deploy_rc`, `smoke_ok`, `gate_kind`, `gate_repaired` (1 when a repair turned
the gate green), `gate_repair_attempts`, `gate_runs`.

`goals.json` declares these goals under agent id **`deployer`**:

| Goal id | target_metric | Target |
|---|---|---|
| `goal-deploys-green` | `deploy_rc` | 0 |
| `goal-tests-gate-green` | `test_rc` | 0 |
| `goal-smoke-checks-pass` | `smoke_ok` | 1 |
| `goal-red-gates-self-repaired` | `gate_repaired` | 1 |

Runs record under `seo-deployer`, which has no goals, so these goals are
**not auto-updated**. They still show their seed values on 2026-09-23. To fix
this, either declare the goals for `seo-deployer`, or run as `deployer`
while keeping the explicit agent-id override.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `SEO_AGENT_CONFIG` | none (required) | site config path (the implementer passes it through) |
| `RESPONDER_RUN_DIR` / `SEO_DEPLOYER_RUN_DIR` | none (required) | run dir to ship; `run.sh --run-dir` sets the former |
| `DEPLOYER_TEST_SCOPE` | auto (`test.test_scope`, else the tier auto-pick) | `smoke`, `full` or `legacy` |
| `DEPLOYER_LOCK_WAIT_S` | 6600 | how long to wait for another gate/deploy of the target |
| `DEPLOYER_FORCE` | unset | `1` re-deploys a HEAD the gate state says is already live |
| `DEPLOY_REPAIR_DISABLED` | unset | `1` stops repair sessions (the gate still blocks) |
| `DEPLOY_REPAIR_CLAUDE_BIN` | `claude` on PATH | the claude executable for repairs |
| `DEPLOY_GATE_STATE_DIR` / `DEPLOY_GATE_ARTIFACTS` | `~/.reusable-agents/deployer/gate-state` / `…/gate-artifacts` | where gate state and artifacts live |
| `DEPLOYER_SKIP_TEST` | unset | `1` skips the test gate. Dangerous; meant for shipping a fix while test infrastructure is independently broken. |
| `RESPONDER_SKIP_CONTENT_VERIFY` | unset | `1` disables per-rec content verification |
| `IMPLEMENTER_SKIP_DEPLOY` | unset | read by the implementer: `1` skips the chain entirely |

Site `deployer:` keys:

- `test.{cmd,cwd}`, or `test.{smoke,full}.{cmd,cwd}`, `test.test_scope`,
  `test.full_interval_days` and `test.timeout_s`
- `build.reuse_tested_image` (default true) and `build.from_clean_checkout`
- `repair.*`: `enabled`, `max_attempts`, `max_wall_s`, `attempt_timeout_s`,
  `test_timeout_s`, `infra_retries`, `infra_retry_wait_s`, `confirm_failures`,
  `model`, `max_turns`, `claude_bin`, `allow_test_edits`, `test_globs`,
  `protected_globs`, `backoff_s`, `alert_after_cycles`, `realert_s`
  (`framework/core/deploy_repair.py` DEFAULTS; the storage config
  `config/deploy-repair-config.json` overrides, top level then `by_site.<site>`)
- `implementer.scope_by_dispatch_kind.deploy-repair`: the paths a repair may
  touch (else the default implementer scope)
- `build.{cmd,cwd}`, `push.{cmd,cwd}`, `deploy.{cmd,cwd,vars}`. `vars`
  expand as `{key}`; `image`, `app` and `rg` have special meaning.
- `smoke_check.{base_url,paths,timeout_seconds,settle_seconds}`

Top-level `local_dev.{port,url,health_url,ensure_running.{cmd,cwd},health_timeout_s,health_interval_s,ensure_timeout_s}`.

## Short-circuit & idempotency

- No short-circuit. It runs once per implementer batch that committed code.
- Re-running on the same run dir rebuilds and redeploys under a new minute
  tag. Shipped markers are only added (implemented and not yet shipped),
  never removed.

## Running & inspecting

```bash
# Re-run the ship step for an existing implementer run dir (real deploy!)
SEO_AGENT_CONFIG=/home/voidsstr/development/specpicks/agents/seo-opportunity-agent/site.yaml \
  bash /home/voidsstr/development/reusable-agents/agents/deployer/run.sh --run-dir <run-dir>

# Test stage only, then stop (dry run is only available on deployer.py directly)
SEO_AGENT_CONFIG=<site.yaml> PYTHONPATH=/home/voidsstr/development/reusable-agents \
  python3 /home/voidsstr/development/reusable-agents/agents/deployer/deployer.py --run-dir <run-dir> --dry-run

# Outcomes
cat <run-dir>/deploy.json
grep -h '\[deployer' /tmp/reusable-agents-logs/dispatch-implementer-<site>-<ts>.log
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  "http://localhost:8090/api/agents/seo-deployer/runs?limit=10"
```

Implementer run dirs live under `/tmp/reusable-agents-logs/dispatch-rundirs/`.
Deployer stderr lines (`[deployer:<stage>] rc=…`) land in the dispatching
implementer's log.

## Failure modes & troubleshooting

| Symptom (`deploy.json.status`) | Cause / action |
|---|---|
| `blocked`, "TEST GATE RED … code: N test(s) failed; N repair attempt(s) did not get the gate green" | The gate failed on code and the repair loop could not fix it this run. Read `<run-dir>/test-failure-context.json` (failures, repairs) and the artifacts it names (`report.json`, each repair's `session.log`). The next run re-tries after `next_repair_at` in the gate state. |
| `blocked`, "… backing off until …" | Same failure signature as a spent repair cycle. Wait, fix it by hand, or clear the site's gate-state file to retry now. |
| `blocked`, "infra: …" | Test infrastructure (container race, Docker, lock wait) failed after the retries. Nothing was sent to an LLM. Check `docker ps`, `/tmp/agent-framework-locks/local-suite-<site>.lock`, disk. |
| `blocked`, "repair is green but could not land" | Another commit conflicts with the repair. It is kept at `refs/deploy-repair/<site>/<tag>`; cherry-pick it by hand. |
| `already-live` | Another run already tested and deployed this HEAD while this one waited for the lock. Recs marked shipped. `DEPLOYER_FORCE=1` re-deploys anyway. |
| `blocked`, `test.rc=79` | `local_dev` health URL never came up. Check `docker compose ps` in the site repo (the KTLO deploy-gate `:4001` playbook). |
| `blocked`, `test.rc=78` | The test scope matched no tests. Tag at least one test `@smoke` or use `full`. |
| `failure` at deploy with `ContainerAppOperationInProgress` | Another deploy held the app for all 3 retries (60/180/360 s). Re-run later. |
| `failure` at smoke | The site is live but a path returned 4xx/5xx or timed out after the 60 s settle and the 30 s retry. **Roll back manually** with `rollback_cmd` (fill in the prior tag, e.g. from `release/<site>/*` or `az containerapp revision list`). |
| `failure`, `content_verify.failed_rec_ids` non-empty | Deployed, but the rec's marker is missing from the page (unwired code). Nothing consumes `failed_rec_ids` automatically: the recs stay `implemented` but not `shipped`, and the implementer run exits with the deployer's rc. |
| "no deployer block in config — nothing to do" | Wrong or missing `SEO_AGENT_CONFIG`. It exits 0 and nothing ships. |
| `[deployer] WARNING: push skipped` / `release tag skipped` | Diverged branch or git auth problem. The deploy still succeeded; push by hand. |

## Related agents

- **Upstream:** `implementer`, fed by `auto-queue-drainer`; the recs come from
  SEO, PI, competitor-research, growth and conversion producers.
- **Sibling bookkeeping:** `catalog-audit-shipped-backfill` marks `shipped`
  for DB-only catalog-audit recs, which never reach the deployer. The
  implementer auto-ships article-author and `pre-existing` recs itself.
- **Operator-driven deploys of the framework dashboard** are separate:
  `install/deploy-azure.sh`.
