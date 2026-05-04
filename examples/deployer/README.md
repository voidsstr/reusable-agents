# Deployer recipes

The framework's deployer (`agents/seo-deployer/deployer.py`) reads a
`deployer:` block from a site's `site.yaml` and runs a 5-stage pipeline:

```
test → build → push → deploy → smoke_check
```

Each stage is a shell command template — the deployer is **cloud-agnostic
by design**. Whatever you can express in `bash`, you can deploy. This
directory holds vetted recipes for common cloud + container targets.

## How it works

`site.yaml` contains a `deployer:` block; copy any recipe below into
that block, swap the `vars:` for your project, and the framework will
fire it after every successful implementer batch (when a non-DB-only
dispatch produces a code commit).

```yaml
# site.yaml
deployer:
  test:
    cwd: tests
    cmd: TEST_URL=http://localhost:4001 npx playwright test --reporter=line
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

## Template variables available everywhere

| Var          | Source                                       | Example                                |
|--------------|----------------------------------------------|----------------------------------------|
| `{tag}`      | UTC timestamp at deploy start                | `20260504-143343`                      |
| `{image}`    | `deploy.vars.image`                          | `nscappsacr.azurecr.io/aisleprompt`    |
| `{app}`      | `deploy.vars.app`                            | `aisleprompt`                          |
| `{rg}`       | `deploy.vars.rg` (or any other `vars:` key)  | `nsc-apps`                             |
| `{<custom>}` | any key under `deploy.vars:`                 | whatever your cloud needs              |

`{tag}` and `{image}` are top-level — every stage sees them. Anything
under `deploy.vars:` is also expanded everywhere via the same template
substitution, so you can put, say, `cluster: my-eks` in `vars:` and use
`{cluster}` in the build/push stages too.

## When the deployer skips itself

| Dispatch kind     | Behavior                  |
|-------------------|---------------------------|
| `article-author`  | **skip** — articles are DB-INSERT, no docker build needed |
| `catalog-audit`   | skip — DB-only fixes |
| `h2h`             | skip — DB-only |
| (everything else) | run if `IMPLEMENTER_SKIP_DEPLOY != 1` and `deployer.build.cmd` is set |

## Recipes in this directory

| Recipe                                | Target                          | Status     |
|---------------------------------------|---------------------------------|------------|
| `azure-container-apps.yaml`           | Azure Container Apps + ACR     | **active** (aisleprompt, specpicks) |
| `azure-app-service.yaml`              | Azure App Service + ACR        | dormant — sample only |
| `azure-functions.yaml`                | Azure Functions (consumption)  | dormant — sample only |
| `aws-ecs-fargate.yaml`                | AWS ECS Fargate + ECR          | dormant — sample only |
| `aws-lambda.yaml`                     | AWS Lambda + ECR               | dormant — sample only |
| `aws-app-runner.yaml`                 | AWS App Runner + ECR           | dormant — sample only |

"Dormant" recipes are valid YAML you can drop into a `site.yaml`
verbatim — they just aren't currently used by any of our production
sites. They'll work the moment you point a real project at them.

## Configuring per-site

Each site has its own `site.yaml`. To switch a site from one cloud
target to another, replace its `deployer:` block with a different
recipe. Example: moving aisleprompt from Azure Container Apps to AWS
ECS would mean swapping in `aws-ecs-fargate.yaml`, updating the
`vars:` for the AWS project, and ensuring the host has `aws` CLI
configured (the deployer just runs whatever `cmd:` you give it).

## Where production sites declare their deployer

| Site         | Path                                                                                                  |
|--------------|-------------------------------------------------------------------------------------------------------|
| aisleprompt  | `nsc-assistant/agents/aisleprompt-seo-opportunity-agent/site.yaml`                                   |
| specpicks    | `specpicks/agents/seo-opportunity-agent/site.yaml`                                                   |

Reference the relevant `examples/deployer/<recipe>.yaml` when editing.
