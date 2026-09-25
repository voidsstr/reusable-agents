# Repo Boundaries: Framework vs Site Repos

> When you build something new, it goes in exactly one of a few places.
> This doc tells you which.
>
> Last audited against the repos and the fleet host on 2026-09-23.

## Where to find agent docs

| You want | Read |
|---|---|
| Every agent, by category | [`agents-catalog.md`](agents-catalog.md) |
| Framework repo index (engines + fleet ops agents) | [`../agents/README.md`](../agents/README.md) |
| AislePrompt agents index | `/home/voidsstr/development/aisleprompt/agents/README.md` |
| SpecPicks agents index | `/home/voidsstr/development/specpicks/agents/README.md` |
| nsc-assistant wrappers (read-only reference) | `/home/voidsstr/development/nsc-assistant/agents/README.md` |
| How the pieces talk to each other | [`architecture.md`](architecture.md) |

## The repos

| Repo | What it holds |
|---|---|
| **`reusable-agents`** | Framework code (`framework/`: lifecycle, storage, dispatch, priority, scheduler, API/UI), **shared engines** whose code runs against any site (`agents/<engine>/`), the fleet ops agents (implementer, deployer, backlog dispatcher, responder, doctor, digest), schemas, blueprints, and install/deploy scripts. |
| **`aisleprompt`**, **`specpicks`** | The application itself, plus `agents/`. That dir holds **per-site instance dirs** (manifest + `site.yaml`, running a shared engine) and **site-specific agents** (their own `agent.py`). AislePrompt's agents moved here from nsc-assistant on 2026-05-12 (aisleprompt commit `e1b5a2c6`). |
| **`nsc-assistant`** | Legacy home. It still holds 8 timer-backed wrappers: `agent-metrics-collector`, `goals-tracker`, `market-research-pipeline`, `sessions-save` (a hand-written unit for the `agent-session-snapshot` dir), `specpicks-gsc-coverage-auditor`, `specpicks-indexnow-bulk`, `specpicks-indexnow-submitter`, `specpicks-site-goals-tracker`. Six of them run engine code from `reusable-agents`; `market-research-pipeline` and `sessions-save` run their own code. Treat it as read-only from this repo. |
| **Per-host config** (`~/.reusable-agents/`) | Secrets, OAuth tokens, the claude-pool, and runtime scratch. Never committed. |

## Decision rules

```
                 ┌──────────────────────────────────────────────┐
                 │  Are you adding new agent CODE or new        │
                 │  application CODE?                           │
                 └────────────┬─────────────────────────────────┘
                              │
            ┌─────────────────┴────────────────────┐
            ▼                                      ▼
      Agent code                            Application code
            │                                      │
            ▼                                      ▼
    ┌──────────────────────┐                ┌──────────────────────┐
    │ Will this code run   │                │ Goes in the          │
    │ against multiple     │                │ site repo.           │
    │ sites with different │                └──────────────────────┘
    │ behavior driven by   │
    │ config?              │
    └────┬─────────────────┘
         │
    ┌────┴───────────────┐
    │                    │
    ▼                    ▼
   YES                  NO
    │                    │
    ▼                    ▼
 reusable-agents     site repo
 /agents/<engine>/   /agents/<id>/
 + one config-only   (own agent.py)
 dir per site
```

Cross-cutting framework behaviour (a new primitive, a queue, a config
knob) goes in `reusable-agents/framework/core/` as primitive + storage
config + manifest field, per the framework-first policy in `CLAUDE.md`.
`CLAUDE.md` also requires every registered agent to subclass
`framework.core.agent_base.AgentBase`. Nine registered ids did not on
2026-09-23 (listed in
[`architecture.md`](architecture.md#agent-lifecycle-python-agentbase-subclass));
don't add to that list.

### What goes in `reusable-agents/agents/<engine>/`

Code whose **logic doesn't change per site**. The behavior is shaped by
the site's config file, whose path is injected through an env var at
run time.

Current shared engines (each run by per-site instances; see
[`architecture.md`](architecture.md#shared-engine-vs-site-specific-agent)
for the instance list):

- `seo-opportunity-agent/`: the SEO + revenue pipeline (collector →
  analyzer → finalize in one AgentBase `run()`; phase code under
  `lib/{collector,analyzer,reporter}/`). Config: `SEO_AGENT_CONFIG`.
- `progressive-improvement-agent/`: site-quality auditor. Its
  `crawler.py` is also imported by `competitor-research-agent`, the
  SEO analyzer, and the legacy `seo-analyzer/`.
- `competitor-research-agent/`, `catalog-audit-agent/`,
  `shelf-audit-agent/`, `feedback-triage-agent/`.
- `product-hydration-agent/`, `ebay-product-sync-agent/`,
  `category-integrity-agent/`, `search-demand-agent/` (SpecPicks is the
  only instance of each today).
- `gsc-coverage-auditor/`, `indexnow-submitter/`, `site-goals-tracker/`.

`goals-tracker/` and `agent-metrics-collector/` hold engine code too,
but they run once for the whole fleet, through nsc-assistant wrappers.

Fleet ops agents that run once for the whole fleet: `implementer/`,
`deployer/`, `backlog-dispatcher-agent/`, `responder-agent/`,
`agent-doctor/`, `digest-rollup-agent/`, `catalog-audit-shipped-backfill/`.
Also registered straight from this repo: `authority-agent/`,
`app-store-opportunity-agent/`, and `oauth-heartbeat-agent/` (timer
disabled). Not agents: `jcode-agent/` (an unregistered blueprint),
`seo-analyzer/` (a legacy copy of the SEO engine's analyzer, no
manifest), and `_archive/`.

**Anti-pattern**: an engine that hardcodes a domain, an Amazon associate
tag, a database name, or a site's column names. If you find yourself
typing `if site == 'specpicks':` in the framework, **stop**. That
behavior belongs in the per-site `site.yaml` or a storage config.

Existing debt you should not copy (verified 2026-09-23):

- `backlog-dispatcher-agent`'s hard-coded `PRODUCER_AGENT_IDS` and its
  `aisleprompt`/`specpicks` prefix list.
- `priority._STARVATION_SITES`.
- `site-goals-tracker`'s `SITE_PROFILES`.
- The site-prefix list in `AgentBase.post_run`'s verification step.

Fix these when you are already in the file.

### What goes in a per-site instance dir (`<site-repo>/agents/<engine>/`)

1. **`manifest.json`** registers the instance. It has the `id`
   (`<site>-<engine>`), `cron_expr`, `timezone`, and an `entry_command`
   that sets the engine's config env var, then runs the engine. The
   engine's runtime id comes from `AGENT_ID`: SEO, feedback-triage, and
   category-integrity instances set it in `entry_command`, and the
   generated unit and the host-worker set it for every run anyway. See
   the timezone note below. (One dir name breaks the pattern:
   SpecPicks' catalog-audit instance lives at
   `specpicks/agents/specpicks-catalog-audit-agent/`.)
2. **`site.yaml`** holds the site-specific knobs (domain, GSC site URL,
   GA4 property, DB DSN **env var name**, page-inventory regexes,
   `auto_implement`, `implementer.allowed_paths`, `deployer:` recipe,
   `handoff_routes` / `site_handler_overrides`, …). SEO configs are
   validated against `shared/schemas/site-config.schema.json`, and PI /
   competitor-research configs against `site-quality-config.schema.json`.
   A key that is unknown in an `additionalProperties: false` block fails
   `jsonschema` validation, and `shared.site_config.load_config` raises
   `SystemExit` at startup.
3. **Runbook**: `AGENT.md` and/or `README.md`.
4. Optional site-specific helpers wired through an env var or config
   path (e.g. `db-queries.sql`, `select-featured.py`).

A correctly separated `entry_command` looks like the aisleprompt SEO
instance's (`SEO_DISABLE_UNCHANGED_SHORTCIRCUIT=1` turns off the
analyzer's unchanged-input short-circuit):

```bash
SEO_DISABLE_UNCHANGED_SHORTCIRCUIT=1 \
AGENT_ID=aisleprompt-seo-opportunity-agent \
SEO_AGENT_CONFIG=/home/voidsstr/development/aisleprompt/agents/seo-opportunity-agent/site.yaml \
PYTHONPATH=/home/voidsstr/development/reusable-agents \
    python3 /home/voidsstr/development/reusable-agents/agents/seo-opportunity-agent/agent.py
```

A `bash run.sh` entry is acceptable only as a thin env wrapper that
ends in `exec python3 …/agent.py`. `specpicks/agents/product-hydration-agent/run.sh`
is an example.

**Don't inline credentials in `entry_command`.** The command is copied
into the systemd `ExecStart=` and into the registry. Name the env var
in `site.yaml` and let `secrets.env` supply it (`DATABASE_URL_<SITE>`).
Several current manifests still inline a DSN (many SpecPicks agents and
`catalog-audit-shipped-backfill` on 2026-09-23); several of those
agents' runbooks note it. That is a defect to fix, not a pattern.

**Timezone:** the timer writer ignores the manifest `timezone`, and
timers fire in host-local time (America/Detroit on `whitebeast`). Write
the cron in host time.

**Anti-pattern**: copying engine **code** into a site repo. If a second
site needs an engine that only one site has, move the generic part into
`reusable-agents/agents/<engine>/` and leave config behind.

### Site-specific agents (`<site-repo>/agents/<id>/` with their own code)

Many agents are tightly coupled to one site's schema or product and
keep their code in the site repo. As of 2026-09-23 this covers most of
`aisleprompt/agents/` and `specpicks/agents/`. Examples:

- `aisleprompt/agents/article-proposal-agent/` and
  `specpicks/agents/article-proposal-agent/`: two separate per-site
  implementations, not a shared engine.
- `specpicks/agents/head-to-head-agent/`: bound to SpecPicks'
  `trending_comparisons`.
- `aisleprompt/agents/kitchen-scraper/`: wraps AislePrompt's
  `scraper-kitchen/` pipeline.
- The recipe-image agents (`recipe-image-refiller`, `-verifier`,
  `-archiver`): wrap AislePrompt TS scripts.

They register the same way as instances. They must still use framework
primitives for cross-agent state (`framework.core.storage`,
`peer_runs`, `dispatch`, `digest_queue`) rather than host paths.
`specpicks-competitor-gap-consumer` and `specpicks-schema-fix-specialist`
read `/home/voidsstr/.reusable-agents/storage/…` directly. On an Azure
host that path holds none of the other agents' runs, so both are no-ops.

Not everything under `agents/` is an agent:

- `aisleprompt/agents/seo-config/` and `specpicks/agents/seo-config/`
  are config read by the IndexNow and GSC engines.
- `aisleprompt/agents/voice/` is a Dialogflow config.
- `specpicks/agents/keep-the-lights-on/` is a stale state snapshot.
- `specpicks/agents/specpicks-implementer-agent/` is an abandoned
  scaffold.

Their READMEs say so (the scaffold's `AGENT.md` does).

### What goes in `~/.reusable-agents/` (NOT in any repo)

- `secrets.env` (mode 0600; values must be single-quoted, because an
  Azure connection string's `;` truncates on shell source). Every
  framework-written agent unit loads it with `EnvironmentFile=-` (the
  hand-written `agent-sessions-save.service` does not). It holds env var
  names such as
  `DATABASE_URL_AISLEPROMPT`, `DATABASE_URL_SPECPICKS`,
  `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_STORAGE_CONTAINER`,
  `STORAGE_BACKEND`, `FRAMEWORK_API_URL`, `FRAMEWORK_API_TOKEN`,
  `BRIGHTDATA_API_KEY`, `AMAZON_CREATORS_*`, `AMAZON_PAAPI_ASSOCIATE_TAG`,
  `EBAY_*`, `OPERATOR_EMAIL`, and `OPERATOR_FROM_EMAIL`.
- `claude-pool/`: Claude Max profiles `profile-1`..`profile-5`, the
  `bin/claude` shim, and `state.json`.
- `seo/.oauth.json`: the Google OAuth token for GSC + GA4 (re-auth:
  `install/reauth-gsc.sh` / `install/fix-gsc-now.sh`).
- `responder/`: `config.yaml` (IMAP settings; still the example file on
  `whitebeast`), `state.json`, and `.oauth.json` (the Microsoft Graph
  token used for outbound mail).
- Per-agent local scratch, e.g. `~/.reusable-agents/specpicks/`
  (audit cursors) and `~/.reusable-agents/<agent-id>/`.
- `storage/` and `data/`: local-FS storage backend roots. On
  `whitebeast` the storage of record is Azure. Queue dirs under these
  paths are deprecated (see `CLAUDE.md`). Never read other agents' state
  from them.

Anything in `~/.reusable-agents/` is per-host and never crosses the git
boundary. `install/recover-credentials.sh` and
[`fleet-host-standup.md`](fleet-host-standup.md) cover moving it to a
new host.

## Side-by-side example: SEO agent

The SEO agent is the canonical case for the boundary:

```
reusable-agents/                              ← framework + shared engine
├── agents/seo-opportunity-agent/
│   ├── agent.py                              ← AgentBase: collect → analyze → finalize
│   ├── finalizer.py                          ← digest queue + gated dispatch
│   └── lib/{collector,analyzer,reporter}/    ← phase code (site-agnostic)
├── agents/backlog-dispatcher-agent/          ← dispatches unshipped recs
├── agents/implementer/                       ← agent.py wrapper + run.sh
├── agents/deployer/                          ← runtime id seo-deployer
├── examples/sites/
│   ├── generic.yaml                          ← reference template
│   ├── aisleprompt.yaml                      ← implementer's site config when
│   └── specpicks.yaml                          SEO_AGENT_CONFIG isn't inherited
└── shared/schemas/site-config.schema.json    ← validates site.yaml

aisleprompt/agents/seo-opportunity-agent/     ← instance (config only)
├── manifest.json                             ← aisleprompt-seo-opportunity-agent, 15 */2 * * *
├── site.yaml
├── db-queries/
├── AGENT.md  README.md  SKILL.md

specpicks/agents/seo-opportunity-agent/       ← instance (config only)
├── manifest.json                             ← specpicks-seo-opportunity-agent, 30 */3 * * *
├── site.yaml
├── db-queries.sql
├── AGENT.md  README.md
└── run.sh                                    ← legacy; the manifest does not call it

~/.reusable-agents/                           ← per-host (not in any repo)
├── secrets.env    (DATABASE_URL_AISLEPROMPT, DATABASE_URL_SPECPICKS, …)
└── seo/.oauth.json
```

Adding a new site to SEO automation needs **config plus a few
framework edits**. The framework edits are the hard-coded lists above,
which are debt, not design:

1. Create `<new-site>/agents/seo-opportunity-agent/` by copying an
   existing instance dir. Edit `site.yaml`, `db-queries`, and
   `manifest.json` (`id`, `AGENT_ID`, `SEO_AGENT_CONFIG`, and a cron
   offset that doesn't collide with the existing `:15` and `:30`
   instances).
2. Add `examples/sites/<new-site>.yaml` with the `implementer:` and
   `deployer:` blocks. The implementer falls back to this file for
   backlog-dispatcher dispatches.
3. Add `DATABASE_URL_<NEW_SITE>` to `~/.reusable-agents/secrets.env`.
4. For recs to reach the implementer with `auto_implement: false`, add
   the new instance ids to `backlog-dispatcher-agent`'s
   `PRODUCER_AGENT_IDS` and `_site_from_agent_id()`. For handoffs to
   land, map each generic handler id in `work_types.DEFAULT_REC_ROUTING`
   (`article-proposal-agent`, `head-to-head-agent`,
   `product-hydration-agent`, `progressive-improvement-agent`,
   `indexnow-submitter`) that the site runs an agent for, in the new
   `site.yaml`'s `site_handler_overrides`. Don't copy SpecPicks' block as-is: it maps
   the legacy `article-author-agent` id and omits `article-proposal-agent`
   and `indexnow-submitter`, which is why those handoffs dead-letter (see
   [`architecture.md`](architecture.md#inter-agent-handoffs-the-routing-primitive)).
5. Register: `bash <new-site>/agents/register-with-framework.sh`. It
   wraps `install/register-all-from-dir.sh`, which calls
   `install/register-agent.sh` against `FRAMEWORK_API_URL`, default
   `http://localhost:8090`.

The full walk-through is [`seo-onboard-new-site.md`](seo-onboard-new-site.md).
The same pattern applies to the other shared engines (PI,
competitor-research, catalog-audit, shelf-audit, feedback-triage).

## Side-by-side example: Product hydration

Same pattern, different agent:

```
reusable-agents/
└── agents/product-hydration-agent/          ← engine (blueprint, not registered)
    ├── agent.py                              ← reads the site config; refreshes prices
    │                                           via Amazon Creators API, PA-API, or
    │                                           BrightData; hydrates copy with Claude
    ├── brightdata_client.py
    ├── paapi_client.py
    ├── config.example.yaml
    └── prompts/hydrate_product_system.md     ← LLM system prompt

specpicks/
└── agents/product-hydration-agent/          ← instance
    ├── manifest.json                         ← cron `15 */2 * * *`; entry_command sets
    │                                           PRODUCT_HYDRATION_CONFIG and runs run.sh
    ├── site.yaml                             ← SpecPicks DB + provider knobs
    ├── select-featured.py                    ← SpecPicks-specific curator
    ├── run.sh                                ← exports FEATURED_SELECT_SCRIPT (default
    │                                           ./select-featured.py), then
    │                                           exec python3 <engine>/agent.py
    └── AGENT.md  README.md
```

Even `select-featured.py`, which IS site-specific (its focus areas are
SpecPicks' home-page categories), is wired through a generic env-var
hook (`FEATURED_SELECT_SCRIPT`). The hydration engine doesn't know about
SpecPicks. It shells out to whatever script the env var points at, if
any. The Creators API client is a framework primitive
(`framework/core/amazon_creators.py`), shared with `kitchen-scraper`
and `shelf-audit-agent`. It covers `getItems` (refresh known ASINs) and
`searchItems` (`search_items()`, discover new ones), and keeps one shared
per-UTC-day call budget for every agent on the credential: the split is the
storage config `config/amazon-creators-budget.json` (per-consumer shares
such as `price-refresh`, `discovery`, `hydration`, `shelf-audit`), and agents
check `client.budget_ok()` / `client.remaining_budget()` before large loops.

## Migration: when an agent crosses the boundary

It's normal for an agent to start site-specific and graduate into a
shared engine. The migration pattern:

1. **Build it in the site repo first.** Don't pre-generalize.
2. When a second site wants the same engine, look at what's actually
   per-site (almost always: domain, DB connection, table names, column
   names, credentials). Move the rest to `reusable-agents/agents/<engine>/`.
3. Add the per-site values to a schema in `shared/schemas/`. Keep
   `additionalProperties: false` strict so typos are caught, and add
   every new key to the schema **before** adding it to a `site.yaml`.
   Otherwise the agent that loads it exits at startup.
4. Each site keeps a config-only instance dir (manifest + `site.yaml` +
   optional helper script wired through an env var).
5. Update [`agents-catalog.md`](agents-catalog.md) and the affected
   repos' `agents/README.md` indexes with the new code path.

## Common mistakes (don't do these)

| Mistake | Why it's wrong | What to do instead |
|---|---|---|
| Hardcoding a domain or site id in an engine or `framework/` | Forks behavior per-site invisibly | Read it from the site config or a storage config |
| Putting site-only Python deps in `reusable-agents/` | Bloats the framework's deps | Keep them with the site-specific agent |
| Committing `secrets.env`, OAuth tokens, or a DSN in `manifest.json` / `run.sh` | Leaks credentials. Manifest commands also land in systemd units and the registry | `~/.reusable-agents/secrets.env` (mode 0600), referenced by env var name |
| Reading another agent's state from `~/.reusable-agents/storage/…` | Empty on an Azure host, so the agent silently does nothing | `framework.core.storage.get_storage()`, `peer_runs`, `run-index.json` |
| Embedding claude-pool routing in an agent | Each agent reinvents pool routing | `agent_run_wrapper.sh` already prepends `$CLAUDE_POOL_ROOT/bin` to `PATH` for timer runs |
| Skipping re-registration after editing a manifest | The registry and the systemd unit keep the old version | `bash <repo>/agents/register-with-framework.sh` (idempotent). Editing only `site.yaml` needs no re-register |
| Hand-writing a systemd timer for an agent | The framework writes the base units on register | `cron_expr` in the manifest. For host-only overrides use a `.timer.d/` drop-in and note it in the runbook |
| Writing the cron in UTC because the manifest says `timezone: UTC` | Timers fire in host-local time | Write the cron in host time (America/Detroit) |
| Writing recs or run state to a custom path | The dashboard, backlog-dispatcher, and digest can't see it | `self.storage` / `framework.core.storage`, and `runs/<ts>/recommendations.json` |

## Quick reference

| Question | Answer |
|---|---|
| New agent's first home | The site repo where it's needed. Move the generic part to `reusable-agents/agents/` on second use. |
| New per-site instance of an existing engine | `<site-repo>/agents/<engine>/` with `manifest.json` (`id` `<site>-<engine>`) + `site.yaml` |
| Site-specific helper script (curator, query file) | Site repo, called from the engine via an env-var or config hook |
| New rec type / rule pass | The engine's phase code (e.g. `agents/seo-opportunity-agent/lib/analyzer/`), gated on a `site.yaml` flag if optional. Add routing to `work_types.DEFAULT_REC_ROUTING` if it shouldn't go to the implementer |
| New blueprint | `reusable-agents/blueprints/<name>/BLUEPRINT.md` (the scaffold files come from `_template/agent/`, cloned by `install/create-agent.sh`) |
| Cross-cutting framework feature (storage, status, dispatch, priority) | `reusable-agents/framework/core/` + a storage config + a `framework/cli/` entry if shell callers need it |
