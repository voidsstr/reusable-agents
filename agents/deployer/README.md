# Deployer (runs as `seo-deployer`)

Reads the `deployer:` block from a site config (`SEO_AGENT_CONFIG`) and runs
**test → build → push → deploy → smoke check → content verify**. On success it
pushes the deployed commits, tags `release/<site>/NNNN`, and marks the batch's
recs `shipped`.

**Full runbook:** [`AGENT.md`](AGENT.md). It covers when the implementer
chains here, every stage in detail, env vars, `deploy.json` fields, goals and
failure modes.

**Pluggable backends.** Config-driven, not tied to a particular cloud:

- **Test**: any shell command (Playwright, Cypress, Vitest, pytest, etc.). It
  can be tiered into `smoke` / `full`.
- **Build**: any Docker / npm / etc. command
- **Push**: any registry push
- **Deploy**: any container / serverless / static-host CLI. There is an
  Azure-specific busy-retry and post-flight revision check when `vars.app` and
  `vars.rg` are set.
- **Smoke check**: GET a list of paths on the deployed origin

The deployer is not scheduled. The implementer runs it after each batch that
produced a code commit. It is skipped for DB-only dispatch kinds
(`article-author`, `h2h`, `catalog-audit`) and when
`IMPLEMENTER_SKIP_DEPLOY=1`. The deployer itself does not check `site.mode`;
whether it runs is decided entirely by the implementer.

## Hard gates

- **One gate and deploy per target at a time.** The deployer takes
  `site_deploy_lock(<app>)` *before* the tests (since 2026-10-19) and holds it
  through smoke and rollback. Waiters print a line a minute, so the
  backlog-dispatcher's 30-minute silence check does not kill them
  (`DEPLOYER_LOCK_WAIT_S`, default 6600). Same-site runs used to test in
  parallel on one fixed container and kill each other's suite (rc=4, six
  blocked deploys in four days).
- **What is tested is what ships.** One detached worktree of HEAD (under
  `~/.reusable-agents/deploy-worktrees/`) serves the gate *and* the build. The
  gate builds the production image from it (`tests/run-local-suite.sh`). The
  deployer then ships that very image (`docker tag` of
  `GATE_ARTIFACT_DIR/image-under-test.txt`, `build.reuse_tested_image`, default
  true) instead of building again. Uncommitted or untracked files in the live
  tree never reach the image, and neither do commits that land after the tests
  pass. If another run already deployed this exact HEAD while we waited for
  the lock, the run ends `already-live` and marks its recs shipped.
- **Test must pass — and a red gate gets repaired.** The gate run is
  classified (`framework/core/deploy_repair.classify`):
  - `pass` / `flaky` → continue. A failed test that passes on a
    `--last-failed --workers=1` re-run is flaky and does not block.
  - `infra` (suite exit 2/4/6/7, 79, timeouts, Docker or registry errors)
    → re-run up to `repair.infra_retries`, never "fixed" in code.
  - `config` (rc 78, no tests matched) → blocked.
  - `code` (failing tests, rc 3 image does not build, rc 5 boot crash) → up
    to `repair.max_attempts` **Opus repair sessions** (`REPAIR.md`, claude-pool,
    cwd = the worktree). Each one is vetted by `deploy_repair.check_repair`:
    it must stay in the site's `implementer` scope (`scope_by_dispatch_kind:
    deploy-repair`), must not touch the gate's machinery, and must not weaken
    a test. Weakening means added skip/fixme/only, fewer `expect()` calls, a
    deleted test file, or a test edit without a `TEST-CHANGE-JUSTIFICATION:`
    line. The gate is then re-run. Green → the repair commits are landed on
    the live branch (fast-forward, else cherry-pick; a conflict blocks and
    keeps them at `refs/deploy-repair/<site>/<tag>`), and the deploy continues.
  - Still red → status `blocked`, no build/push/deploy, exit 1. The per-site
    gate state (`~/.reusable-agents/deployer/gate-state/<site>.json`) remembers
    the failure signature. Later runs back off on the same signature
    (`repair.backoff_s`) and re-try after it. The operator gets one email per
    signature once a full repair cycle is spent (`reporter.email`, de-duplicated,
    `repair.realert_s`).
  An empty test selection ("No tests found") counts as a failure (rc 78). An
  unhealthy `local_dev` server blocks with rc 79. `--skip-test` (or
  `DEPLOYER_SKIP_TEST=1`) skips the gate and says so loudly; it is for
  operator rollbacks only.
- **Smoke check must pass.** Any path outside 200–399, after a 60 s settle and
  one 30 s retry → rollback to the prior image (`deployer.rollback`), status
  `rolled-back`, exit 1. The indexability/latency gate (`smoke_check.gate`)
  rolls back on regressions the same way.
- **Content verify must pass** for recs that carry a `content_check` or have
  a type with an inferred check. It runs only when a smoke check is
  configured. Opt out with `RESPONDER_SKIP_CONTENT_VERIFY=1`.
- **Tag per deploy.** `{tag}` is the UTC time as `%Y%m%d-%H%M`, stamped once
  the lock is held. The release tag `release/<site>/NNNN` goes on the commit
  that was tested and shipped (`RELEASE_SHA`), not on whatever HEAD is by then.

Turn repair off fleet-wide with the storage config
`config/deploy-repair-config.json` → `{"enabled": false}` (or per site under
`by_site.<site>`), or `DEPLOY_REPAIR_DISABLED=1` in the environment. The gate
still blocks. Gate artifacts (each run's `output.log`, Playwright
`report.json`, `test-results/`, the container log and each repair's
`prompt.md` / `session.log`) live in
`~/.reusable-agents/deployer/gate-artifacts/<site>/`, outside the run dir
(which is synced to blob storage), pruned to the newest 20 runs.

## Usage

```bash
# How the implementer calls it
SEO_AGENT_CONFIG=<site.yaml> \
  bash agents/deployer/run.sh --run-dir /tmp/reusable-agents-logs/dispatch-rundirs/<rundir>

# Gate (and repair) only, no build/deploy: deployer.py directly
SEO_AGENT_CONFIG=<site.yaml> python3 agents/deployer/deployer.py --run-dir <path> --dry-run
```

Output: `<run-dir>/deploy.json`, which records the tag, the shipped `sha`,
image, per-stage rc/stderr tails, test scope with the gate summary
(`test.gate`: kind, failures, runs, repairs, flaky, landed), smoke and
content-verify results, status and `rollback_cmd`. `<run-dir>/gate.json` holds
the same gate summary. A red gate also writes
`<run-dir>/test-failure-context.json` with the failing tests (title, file,
line, error), the failure signature, the repair attempts and the artifact
paths.

## Example deployer config

Vetted recipes live in `examples/deployer/` (`azure-container-apps.yaml`,
`azure-app-service.yaml`, `azure-functions.yaml`, `aws-ecs-fargate.yaml`,
`aws-lambda.yaml`, `aws-app-runner.yaml`). The live AislePrompt and SpecPicks
blocks are in each site's `agents/seo-opportunity-agent/site.yaml`.

### Azure Container Apps (tiered tests + local dev server)
```yaml
deployer:
  test:
    full_interval_days: 7
    smoke:
      cwd: tests
      cmd: TEST_URL={local_dev_url} npx playwright test --config=pw.config.ts --reporter=line --grep "@smoke"
    full:
      cwd: tests
      cmd: TEST_URL={local_dev_url} npx playwright test --config=pw.config.ts --reporter=line
  build:
    cwd: .
    cmd: docker build -f Dockerfile.azure -t {image}:{tag} .
  push:
    cmd: az acr login --name nscappsacr && docker push {image}:{tag}
  deploy:
    cmd: az containerapp update --name {app} --resource-group {rg} --image {image}:{tag}
    vars:
      app: aisleprompt
      rg: nsc-apps
      image: nscappsacr.azurecr.io/aisleprompt
  smoke_check:
    base_url: https://aisleprompt.com
    paths: [/, /sitemap.xml, /recipes]
    timeout_seconds: 30
local_dev:
  port: 4001
  url: http://localhost:4001
  health_url: http://localhost:4001/
  ensure_running:
    cwd: .
    cmd: docker compose up -d frontend backend db
  health_timeout_s: 90
```

A block with a single `test: {cmd, cwd}` also works. It runs as scope
`legacy` on every deploy.

### Vercel
```yaml
deployer:
  test:
    cmd: pnpm test
  deploy:
    cmd: vercel deploy --prod --token=$VERCEL_TOKEN --confirm
  smoke_check:
    base_url: https://my-site.vercel.app
    paths: [/, /sitemap.xml]
```

### Cloudflare Workers
```yaml
deployer:
  test:
    cmd: npx vitest run
  deploy:
    cmd: wrangler deploy --name {worker}
    vars:
      worker: my-seo-site
  smoke_check:
    base_url: https://my-seo-site.workers.dev
    paths: [/]
```

### No deploy
```yaml
deployer: null
```
With no block, the deployer prints "no deployer block in config — nothing to
do" and exits 0.

## Reuse

The deployer is site-agnostic: it runs whatever shell commands you configure
in YAML. Do not add per-cloud branches to `deployer.py`. A new target is a new
recipe in `examples/deployer/`. It also works as a generic "test → ship →
smoke" wrapper outside SEO.
