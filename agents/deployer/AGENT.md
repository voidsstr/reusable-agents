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

A non-zero deployer exit makes the implementer exit with the same rc. The
chain forces `DEPLOYER_TEST_SCOPE=smoke` unless the caller overrides it, so a
tiered site never runs its full suite per batch. A site with a single legacy
`test.cmd` (specpicks today) still runs that command on every deploy.

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
   `cwd`s resolve against `implementer.repo_path`. `tag` is the UTC time as
   `%Y%m%d-%H%M`, so two deploys in the same minute share a tag. On
   2026-09-23 two runs shared `20260923-1106` and two shared
   `20260923-1110`; all four were blocked at the test gate, so no image was
   built under a shared tag.

1. **Test (hard gate)**
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
   - **Failure.** Any non-zero rc sets status `blocked` and writes
     `<run-dir>/test-failure-context.json`, then exits 1.
   - **Full-scope pass.** Records
     `agents/deployer/state/<site>-last-full-test.json`. That file is in the
     repo directory and git-tracked; the current files date from 2026-05-05
     and 05-06.
2. **Build.** Runs `build.cmd` with `{tag}`, `{image}` and `deploy.vars`
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
   - Still failing → status `failure` and "SMOKE FAILED — manual rollback
     needed". **No automatic rollback.**
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
   1. `deploy.json` gets `status: success` and `rollback_cmd` (the deploy
      command with `<PRIOR_TAG>` as a placeholder; the prior tag is **not**
      recorded).
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
  `smoke{ok,results,retried}`, `content_verify`, `status`
  (`running|blocked|failure|dry-run|success`), `rollback_cmd`,
  `shipped_rec_count` and `error`.
- `<run-dir>/test-failure-context.json` when the test gate fails.
- The updated `<run-dir>/recommendations.json` (shipped markers).
- A container image `<image>:<tag>` in the registry, and the new Container
  App revision.
- The git push of the site repo, plus the `release/<site>/NNNN` tag.
- Standard AgentBase artifacts under `agents/seo-deployer/runs/<run_ts>/`
  (`plan` / `result` decisions).

## Goals & metrics

`RunResult.metrics` keys: `tag`, `image`, `test_rc`, `build_rc`, `deploy_rc`,
`smoke_ok`.

`goals.json` declares these goals under agent id **`deployer`**:

| Goal id | target_metric | Target |
|---|---|---|
| `goal-deploys-green` | `deploy_rc` | 0 |
| `goal-tests-gate-green` | `test_rc` | 0 |
| `goal-smoke-checks-pass` | `smoke_ok` | 1 |

Runs record under `seo-deployer`, which has no goals, so these goals are
**not auto-updated**. They still show their seed values on 2026-09-23. To fix
this, either declare the goals for `seo-deployer`, or run as `deployer`
while keeping the explicit agent-id override.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `SEO_AGENT_CONFIG` | none (required) | site config path (the implementer passes it through) |
| `RESPONDER_RUN_DIR` / `SEO_DEPLOYER_RUN_DIR` | none (required) | run dir to ship; `run.sh --run-dir` sets the former |
| `DEPLOYER_TEST_SCOPE` | auto (the implementer passes `smoke`) | `smoke`, `full` or `legacy` |
| `DEPLOYER_SKIP_TEST` | unset | `1` skips the test gate. Dangerous; meant for shipping a fix while test infrastructure is independently broken. |
| `RESPONDER_SKIP_CONTENT_VERIFY` | unset | `1` disables per-rec content verification |
| `IMPLEMENTER_SKIP_DEPLOY` | unset | read by the implementer: `1` skips the chain entirely |

Site `deployer:` keys:

- `test.{cmd,cwd}`, or `test.{smoke,full}.{cmd,cwd}`, `test.test_scope` and
  `test.full_interval_days`
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
DEPLOYER_TEST_SCOPE=smoke \
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
| `blocked`, `test.rc` = 1, 4 or 5, "TEST FAILED scope=legacy" | The site's test suite failed. Seen on specpicks `run-local-suite.sh` several times on 2026-09-23. Read `<run-dir>/test-failure-context.json`. |
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
