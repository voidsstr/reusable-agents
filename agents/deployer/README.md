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

- **Test must pass.** Non-zero exit → status `blocked`, no build/push/deploy,
  exit 1. An empty test selection ("No tests found") counts as a failure
  (rc 78). An unhealthy `local_dev` server blocks with rc 79.
- **Smoke check must pass.** Any path outside 200–399, after a 60 s settle and
  one 30 s retry → status `failure`, exit 1. **There is no automatic rollback.**
  The log says "manual rollback needed", and `deploy.json.rollback_cmd` holds
  the deploy command with a `<PRIOR_TAG>` placeholder. The prior tag is not
  recorded.
- **Content verify must pass** for recs that carry a `content_check` or have
  a type with an inferred check. It runs only when a smoke check is
  configured. Opt out with `RESPONDER_SKIP_CONTENT_VERIFY=1`.
- **Tag per deploy.** `{tag}` is the UTC time as `%Y%m%d-%H%M`, unique per
  minute (two deploys in the same minute collide).

## Usage

```bash
# How the implementer calls it
SEO_AGENT_CONFIG=<site.yaml> DEPLOYER_TEST_SCOPE=smoke \
  bash agents/deployer/run.sh --run-dir /tmp/reusable-agents-logs/dispatch-rundirs/<rundir>

# Test stage only (dry run; deployer.py directly)
SEO_AGENT_CONFIG=<site.yaml> python3 agents/deployer/deployer.py --run-dir <path> --dry-run
```

Output: `<run-dir>/deploy.json`, which records the tag, image, per-stage
rc/stderr tails, test scope, smoke and content-verify results, status and
`rollback_cmd`. A failed test gate also writes
`<run-dir>/test-failure-context.json`.

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
