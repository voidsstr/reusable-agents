# Deploy templates (kit item K6)

Provisioning, deploy and teardown scripts for a kit site on **Azure Container
Apps** (the live target) and **ECS Fargate** (the parallel target), driven by one
per-site `deploy.env`. They are ported from SpecPicks' `azure/{deploy,provision,
teardown}.sh`, `aws/deploy.sh`, `Dockerfile.azure` and `tests/predeploy-gate.sh`
(last changed upstream in `0454c444`, 2026-10-19; read at `b5415b08`), with every
site name, resource name and domain moved into `deploy.env`.

A site uses them from its vendored copy (`vendor/affiliate-kit/deploy/`); agents
never edit them there.

| File | What it does |
|---|---|
| `deploy.env.example` | The contract: every key, documented. Copy it to the site root as `deploy.env`. |
| `azure/provision.sh` | Phases `foundation`, `check`, `database`, `app`, `secrets`, `domain`, `all`. |
| `azure/deploy.sh` | Stages `gate`, `build`, `push`, `migrate`, `release`, `smoke`. |
| `azure/teardown.sh` | Deletes the site's Container App (and optionally certificates, images, database). |
| `aws/provision.sh`, `aws/deploy.sh`, `aws/teardown.sh` | The same phases, stages and flags on ECS Fargate behind the shared ALB. |
| `Dockerfile.azure.tmpl` | The production image (SSR only, no SPA stage), rendered per site. |
| `render-dockerfile.sh` | Renders the template with `deploy.env` (`--write`), or checks a render (`--check`). |
| `predeploy-gate.sh` | Runs the site's gate suite on the tree about to be built; fails if the tree changes meanwhile. |
| `dockerignore.example` | The `.dockerignore` the template expects. |
| `lib/common.sh`, `lib/deploytool.py` | Shared bash (config, dry run, secrets, SQL, stages) and the Python half (parsing, SCRAM, cache rule, smoke check, rendering, JSON). |

Every script takes `--env deploy.env`, `--dry-run` and `--help`.

## Rules the scripts enforce

- **No site literals in code.** Names come from `deploy.env`; unknown keys are an
  error, so a typo never falls back to a default.
- **Secrets never on a command line, never printed.** `deploy.env` holds only
  references: `env:NAME` (the environment, then `SECRETS_FILE`) or `file:PATH`.
  Values reach Azure as `az ... name=@file` (a 0600 file in a 0700 directory,
  removed on exit; the az CLI reads the file), AWS as `file://`, psql and docker
  through one child's environment (`PGPASSWORD`, `-e DATABASE_URL`). Every
  script loads every secret `deploy.env` refers to when it starts, and every
  command line (and every dry-run line, task definition and JSON document) is
  checked against each of them; a match aborts with exit 70 before anything
  runs. A secret shorter than 8 characters is refused (the guard could not tell
  it from ordinary text). The scripts refuse to run under `bash -x` / `set -x`,
  which would print secret values; `--dry-run` shows the commands instead. Role
  passwords go to Postgres as SCRAM-SHA-256 verifiers computed locally, so the
  plaintext never reaches the server or its logs.
- **Minimum 1 replica.** `MIN_REPLICAS=0` is refused: scale-to-zero cold starts
  time out AI-assistant fetches, and managed-certificate renewal needs a running
  app. Defaults: 0.5 vCPU / 1 GiB, 1 to 3 replicas (Fargate 512 / 1024, 1 to 3 tasks).
- **SSR HTML carries `s-maxage` <= 600.** Before a build, `deploy.sh` checks
  `site.json` (`cache.htmlSMaxAgeSeconds`, `htmlMaxAgeSeconds` <= `HTML_MAX_S_MAXAGE`,
  which may not exceed 600). After a release, the smoke check fails any HTML
  response without `s-maxage`; with `s-maxage` or `max-age` above the limit;
  with `stale-while-revalidate` or `stale-if-error` above 0 (a cache could serve
  the page stale past the limit); with a CDN-targeted header
  (`CDN-Cache-Control`, `Cloudflare-CDN-Cache-Control`, `Surrogate-Control`)
  that caches longer, or sets no lifetime; or with an `Age` above the limit (a
  CDN caching HTML past it). The price window (24h minus the HTML cache age)
  depends on this, so `SMOKE_PATHS` should list a product page and a best-of
  list (the pages that quote prices); the check warns when it saw fewer than
  two HTML pages.
- **Least-privilege database roles**, each with a `CONNECTION LIMIT` and a
  `statement_timeout`, checked inside the database after every run. Grants may
  name columns: the publisher gets `SELECT` and `UPDATE (status, published_at)`
  on `editorial_articles`, no more (the publish guard's contract).
- **Untested code does not ship.** `deploy.sh` runs the pre-deploy gate before
  it builds. A run that builds without the gate stage must say why:
  `--gated-by=WHO` when an outer pipeline ran the gate on that tree (the
  deployer's `test` stage), or `--skip-tests[=REASON]`, which prints a loud
  banner, as in SpecPicks. A release of an image the run did not build says so.
  An empty stage list (`--only ''`) is an error, never a full deploy.
- **Only this site's resources are changed or deleted.** The database server,
  resource group, environment, registry and AWS foundation are shared with
  other sites, so the scripts mark what they create: a `COMMENT 'affiliate-kit
  app=<APP_NAME>'` on the four roles and the database, an `affiliate-kit-app`
  tag on the Container App, the ECS service, the Secrets Manager secret, the
  target group, the ACM certificate, the HTTPS listener's host-header rule and
  the ECR repository. A role, database or resource that `deploy.env` names (or
  that `SITE_DOMAIN` finds: the certificate and the rule) but that lacks this
  site's marker (another site's, or a typo) stops provision, deploy and
  teardown before anything is changed. To take over objects that predate the
  kit, run the provision phase with `--adopt-existing`, which marks them;
  objects marked for another kit site are never adopted. ACR repositories and
  managed certificates carry no tags, so Azure teardown proves them otherwise:
  `--with-images` stops when any other Container App or job in the
  subscription runs an image from the repository, and `--with-certificates`
  deletes only the certificates this site's app binds (and failed attempts).
- **A failed query is not "absent".** Existence checks treat only az/aws
  not-found errors as "does not exist"; any other failure (network, auth,
  throttling) stops the script, so a transient error never sends it down a
  create path over a live resource.

## First deployment (Azure)

Prerequisites: `az login` on the fleet host; the site repo with `site.json`, the
rendered `Dockerfile.azure`, `.dockerignore` and `deploy.env`; the server admin
password where `AZ_DB_ADMIN_PASSWORD` points; this host allowed through the
Postgres server's firewall. `deploy.env` decides which database, roles and
resources the scripts touch, so it is operator-owned: agents never edit it.

```bash
K=vendor/affiliate-kit/deploy
bash $K/render-dockerfile.sh --env deploy.env --write        # commit Dockerfile.azure
bash $K/azure/provision.sh --env deploy.env --dry-run all    # read the plan first
bash $K/azure/provision.sh --env deploy.env check

# Roles + database. Either put the four DSNs (DB_*_DSN) in secrets.env first, or
# let the script generate the passwords and write the DSNs for you to merge:
bash $K/azure/provision.sh --env deploy.env database --write-new-dsns ~/new-site-dsns.env
#   then append ~/new-site-dsns.env to ~/.reusable-agents/secrets.env (single-quoted as written) and delete it

bash $K/azure/deploy.sh --env deploy.env --build-only        # gate, build, push :<tag> and :latest
bash $K/azure/deploy.sh --env deploy.env --only migrate --image-tag <tag>   # if MIGRATE_COMMAND is set
bash $K/azure/provision.sh --env deploy.env database         # again, now the tables exist: grants converge
bash $K/azure/provision.sh --env deploy.env app --image-tag <tag>
bash $K/azure/provision.sh --env deploy.env domain           # prints the DNS records; exit 3 until they resolve
#   create the records, wait for propagation, run the domain phase again
bash $K/azure/deploy.sh --env deploy.env --skip-build --image-tag <tag>     # release + smoke on the custom domain
```

`app` creates the Container App with its secrets (`--secrets name=@file`) and
env (`DATABASE_URL=secretref:database-url`), external ingress on `TARGET_PORT`,
single revision mode, the `affiliate-kit-app` tag, and the ACR admin
credentials the az CLI looks up itself. Run on an existing app of this site it
reconciles ingress, resources, replicas and env (`--replace-env-vars`: a
variable removed from `deploy.env` leaves the app too, as on AWS, where the task
definition is rebuilt). `secrets` re-sets the secret values later and removes
the ones `APP_SECRETS` no longer lists, keeping the registry password secret az
stored (`--restart` restarts the active revision so it reads them).

**Kit updates.** The rendered `Dockerfile.azure` carries the kit version, and
`deploy.sh` refuses to build a render that differs from the template. Whenever
the vendored kit (or `deploy.env`'s image keys: `APP_NAME`, `NODE_IMAGE`,
`TARGET_PORT`, `SERVER_ENTRY`, `BUILD_SCRIPT`, `HEALTH_PATH`,
`DOCKER_EXTRA_PATHS`) changes, run `render-dockerfile.sh --env deploy.env
--write` and commit the result in the same change. A scaffolded site's
`tests/unit/kit-integrity.test.ts` runs `render-dockerfile.sh --check`, so a
missed re-render fails its unit tests before a deploy does (the deployer's gate
builds the committed file, and only the build stage after it runs the check),
and its `deploy-config.test.ts` also pins the kit version on the rendered
file's first line.

## Routine deploys

```bash
bash vendor/affiliate-kit/deploy/azure/deploy.sh --env deploy.env
```

The deployer agent runs the gate itself (its `test` stage), so its `site.yaml`
block calls the stages it needs and says who gated the build:

```yaml
deployer:
  test:
    cwd: tests
    cmd: bash run-local-suite.sh --workers=4
  build:
    cmd: bash vendor/affiliate-kit/deploy/azure/deploy.sh --env deploy.env --image-tag {tag} --only build --gated-by=deployer
  push:
    cmd: bash vendor/affiliate-kit/deploy/azure/deploy.sh --env deploy.env --image-tag {tag} --only push
  deploy:
    cmd: bash vendor/affiliate-kit/deploy/azure/deploy.sh --env deploy.env --image-tag {tag} --only migrate,release,smoke --rollback-on-smoke-failure
    vars: {app: <APP_NAME>, image: <AZ_ACR>.azurecr.io/<APP_NAME>}
  smoke_check:
    base_url: https://<SITE_DOMAIN>
    paths: [/, /health, /robots.txt, /llms.txt, /sitemap.xml]
  rollback:
    capture_cmd: az containerapp show --name {app} --resource-group <AZ_RESOURCE_GROUP> --query "properties.template.containers[0].image" -o tsv
    cmd: bash vendor/affiliate-kit/deploy/azure/deploy.sh --env deploy.env --only release --image-tag {prior_image}
```

(`--image-tag` also takes a full reference to the site's own repository, which
is what `{prior_image}` is.)

**`vars` has no `rg`, on purpose.** When `deploy.cmd` exits non-zero and
`vars` has both `app` and `rg`, the deployer reads the image of the app's
active revision, and if it ends with the new tag it records the deploy as a
success (its override for an az call that timed out after Azure accepted the
release). Its own smoke check only looks at status codes, so a failure only
the kit's smoke check sees (the live HTML cache rule, `Age`, the CDN headers,
the extra `SMOKE_PATHS`) would be recorded as a success whenever the new image
is still serving, as it is when the automatic rollback itself fails (exit 4,
"... may still be serving"). Without `rg` the deployer never runs that check:
every non-zero exit of the kit deploy is a failed deploy. `app` stays, because
the deployer serializes deploys per `vars.app` and `{app}` fills the capture
command; the resource group is written into `capture_cmd` instead. The cost is
the timeout override: such a release is recorded as failed and the next
deployer run does it again, the safe direction for the price window.

`--rollback-on-smoke-failure` is still wanted: a smoke failure after the
release puts the previous image back before exiting 4, so a failed check
does not leave the new image serving. The rollback command has no smoke
stage, so a rollback that released cleanly is not reported as failed (and
never "rolls back the rollback"). `unit/deploy/deployerBlock.test.ts` checks
this block, and fails if the deployer's post-flight condition changes.

`release` refuses to run when the ingress target port differs from
`TARGET_PORT` (the SpecPicks 2026-09-16 incident: the new revision never
activates), or when the app lacks this site's `affiliate-kit-app` tag. It
records the serving image, updates the app, and waits until the new revision
is the latest ready one. On failure it prints the previous image and the
command that puts it back (through this script on both clouds).

## Domain and certificates

`provision.sh domain` prints, for every `SITE_HOSTNAMES` entry:

| Type | Name | Value |
|---|---|---|
| A | apex | the environment's static IP (or a CNAME to the app FQDN, flattened at the apex) |
| CNAME | subdomain | the app's generated FQDN |
| TXT | `asuid.<host>` | the app's domain verification id |

plus `CAA <apex> 0 issue "digicert.com"` if the zone has CAA records. It checks
them over DNS-over-HTTPS (exit 3 until they are right), then runs
`az containerapp hostname add` and `hostname bind --validation-method HTTP`
(apex) or `CNAME` (subdomains) for a free managed certificate.

**Cloudflare:** managed certificates are issued *and renewed* by DigiCert over
these records, so they must be **DNS only** (grey cloud), as specpicks.com's are
today. A proxied record answers with Cloudflare's addresses and the issuance or
renewal fails; the DNS check reports exactly that. Proxying (for example to use
Cloudflare AI Crawl Control) needs a certificate the site brings itself; these
scripts do not do that yet.

## Database roles

| Role | Default grants (`DB_*_GRANTS`) | Limits (default) |
|---|---|---|
| owner | owns the database and the `public` schema; runs migrations | 3 connections, 15min |
| app | `SELECT` on every table; `INSERT` on `outbound_clicks`, `ai_traffic_log` (+ their sequences) | 20, 15s |
| agents | `SELECT, INSERT, UPDATE, DELETE` on every table (+ sequences) | 10, 5min |
| publisher | `SELECT, UPDATE (status, published_at)` on `editorial_articles` | 2, 30s |

All four also get `idle_in_transaction_session_timeout` (60s). `PUBLIC` loses
`CONNECT` on the database and every privilege on `public`. `*` grants also set
default privileges, so tables the owner creates later are covered; named tables
are granted when they exist (a warning says to re-run after the migrations). The
grants converge: anything not declared is revoked (column grants included), in
one transaction.

Before it changes anything on the server, the database phase checks that each
of the four roles and the database either does not exist or carries this
site's marker (`COMMENT 'affiliate-kit app=<APP_NAME>'`, set in the same
transaction that creates a role, and right after `CREATE DATABASE`). One that
exists without it stops the phase with the list of what it found, and nothing
is altered; `--adopt-existing` marks such objects instead, unless another kit
site's marker is on them.

Each role's password comes from its DSN (`DB_*_DSN`, the same DSNs the app and
agents use), so the role and the DSN cannot drift; the DSN's user must equal the
role and its database `DB_NAME`. The admin needs `CREATEROLE` and `CREATEDB`
(the Azure flexible-server admin has both); the scripts never use `SUPERUSER`.
`MIGRATE_COMMAND` runs inside the new image as the owner role, before the
release; the owner DSN must log in as `DB_OWNER_ROLE` to `DB_NAME`.

## AWS (parallel target)

The shared foundation is `reusable-agents/install/deploy-aws.sh provision`,
which writes `~/.aws-deploy/state.env`; `aws/provision.sh foundation` only
verifies it. Per site: `database` (the same SQL on RDS), `secrets` (Secrets
Manager `AWS_SECRET_ID`, required in `deploy.env`, written with `file://`), `domain` (ACM DNS-validated
certificate, then the target group, HTTPS listener and host-header rule; exit 3
until ACM says `ISSUED`), `app` (ECR repository, task definition with secrets by
ARN, service with circuit-breaker rollback, auto scaling `MIN..MAX` on CPU).
`all` runs domain before app because ECS attaches a service only to a target
group behind a listener. `aws/deploy.sh` has the same stages and flags as the
Azure one; the smoke check reaches the origin through the ALB with the custom
domain's name. The container gets the same env names and secret refs on both
clouds.

## Teardown

```bash
bash vendor/affiliate-kit/deploy/azure/teardown.sh --env deploy.env --confirm <APP_NAME> [--with-certificates] [--with-images] \
  [--with-database --confirm-database <DB_NAME>]
```

Everything is checked before the first delete:

- Azure: a Container App without this site's tag stops the teardown.
  `--with-images` also stops it when any other Container App or Container Apps
  job in the subscription (matched by resource id, so a same-named app in
  another group counts) runs an image from `AZ_REGISTRY/APP_NAME`, any tag or
  digest, init containers included: ACR repositories carry no tags, and the
  registry is shared. `--with-certificates` reads the certificates the app
  binds before deleting it, then deletes those and failed attempts for
  `SITE_HOSTNAMES`; any other certificate for those names is kept, with a
  warning (after the app is gone, only failed attempts go).
- AWS: a service, target group, secret, ACM certificate or host-header rule
  (the last two found by `SITE_DOMAIN`, checked on every teardown because the
  rule is deleted and the certificate taken off the shared listener) without
  this site's tag stops the teardown, and with `--with-images` so does an
  untagged ECR repository.
- Both: the drop SQL checks the database and role markers before its first
  `DROP`.

The resource group, environment, registry, Postgres server, ALB, cluster and
RDS instance are never touched. (SpecPicks' `teardown.sh --all` deleted the whole
resource group, which on the shared `nsc-apps` group means every site; there is
no such mode here.)

## Dry run

`--dry-run` prints every `az`, `aws`, `docker`, `psql` and network command
instead of running it, including the SQL and the JSON documents the commands
would read (secret values never appear; verifiers are placeholders). Read-only
probes are printed too and answered with placeholders: existence probes say
"absent" in `provision.sh` (to preview the create path) and "present" in
`deploy.sh` and `teardown.sh`. Override per resource with
`KIT_DEPLOY_DRY_EXISTING=app,service` / `KIT_DEPLOY_DRY_MISSING=hostname:example.com`.

## Exit codes

`0` ok; `1` a step failed; `2` usage or config error (including a resource
that is not this site's); `3` the operator has to act (DNS records,
certificate validation, a missing secret or image) and re-run; `4`
(`deploy.sh`) the smoke check failed: after a release in the same run, with
`--rollback-on-smoke-failure` the previous image serves again unless the
rollback itself failed (the message then says the new image may still be
serving, and prints the command that puts the previous one back) or there was
no previous image to put back; without the flag the new one serves; `70`
internal error (a secret reached a command line; nothing was run).

## Tests

`unit/deploy/*.test.ts` (run by `./test.sh`) run every script with `--dry-run`
against `unit/deploy/fixtures/deploy.env` in a sandbox whose `PATH` has only
logging stubs for `az`, `aws`, `docker`, `psql`, `curl` and `git`, and assert the
command lines, the SQL, the JSON documents, that no stub ran, and that no secret
value appears anywhere. They also run the real secret plumbing against the stubs
(`name=@file`, `file://`, 0600, cleaned up), the smoke check against a local HTTP
server, the DNS check against a local DNS-over-HTTPS stub, the SCRAM verifier
against an independent implementation, and shellcheck when it is installed
(`SHELLCHECK=/path/to/shellcheck` to point at one). Real-mode tests also cover
the smoke-failure rollback on both clouds (the smoke check runs against names
under `.invalid`, so it fails fast and offline), the ownership refusals, and
how existence probes read not-found and other errors. With
`KIT_DEPLOY_IT_PG_PORT` (and `KIT_DEPLOY_IT_PG_PASSWORD`) set, an integration
test runs the database phase against a throwaway Postgres as a non-superuser
`CREATEROLE CREATEDB` admin and checks the resulting privileges by logging in as
each role, including the refusal to take over or drop another site's role or
database and `--adopt-existing`.
