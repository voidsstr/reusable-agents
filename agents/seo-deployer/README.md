# seo-deployer

Reads the `deployer.*` block from your site config and runs the configured
test → build → push → deploy → smoke-check sequence.

**Pluggable backends** — config-driven, not hardcoded to a particular cloud:

- **Test**: any shell command (Playwright, Cypress, Vitest, pytest, etc.)
- **Build**: any Docker / npm / etc. command
- **Push**: any registry push
- **Deploy**: any container / serverless / static-host CLI
- **Smoke check**: any HTTP path-list against the deployed origin

This agent runs **only when `site.mode = implement`** AND a `deployer:` block
is configured. In `mode: recommend` runs, the deployer is skipped — the
implementer doesn't run, so there's nothing to deploy.

## Hard gates

- **Test must pass.** Non-zero exit → no deploy, exit with status 1.
- **Smoke check must pass.** Any 4xx/5xx on a configured path → roll back.
- **Unique tag per deploy.** `{tag}` is auto-substituted with `$(date -u +%Y%m%d-%H%M)`.

## Usage

```bash
# Triggered by implementer
bash run.sh --run-dir ~/.reusable-agents/seo/runs/my-site/20260425T140245Z

# Manual smoke
SEO_AGENT_CONFIG=my-site.yaml bash run.sh --run-dir <path>
```

Output: `<run-dir>/deploy.json` with deploy tag, prior tag, smoke results,
rollback command.

## Example deployer config

### Azure Container Apps
```yaml
deployer:
  test:
    cwd: tests
    cmd: TEST_URL=http://localhost:4001 npx playwright test --config=pw.config.ts --reporter=line
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
```

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

### No deploy (recommend mode never runs deployer; this is an explicit "deploy nothing" config for testing)
```yaml
deployer: null
```

## Reuse

Deployer is fully site-agnostic — it just runs whatever shell commands you
configure in YAML. Useful as a generic "test → ship → smoke" wrapper for
non-SEO uses too.
