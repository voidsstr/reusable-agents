# Repo Boundaries: Framework vs Customer Repos

> When you build something new, it goes in exactly one of three places.
> This doc tells you which.

## The three repos

| Repo | What it holds |
|---|---|
| **`reusable-agents`** | Framework code (lifecycle, storage, dispatch, UI/API), plus agents whose **code** is generic and runs against any site. |
| **Customer repos** (`specpicks`, `nsc-assistant`, future apps) | The application itself + **per-site agent manifests** + **per-site config** that drive the reusable agents. |
| **Per-host configuration** (`~/.reusable-agents/`) | Secrets, OAuth tokens, runtime data — never committed. |

## Decision rules

```
                 ┌──────────────────────────────────────────────┐
                 │  Are you adding new agent CODE or new        │
                 │  application CODE?                            │
                 └────────────┬─────────────────────────────────┘
                              │
            ┌─────────────────┴────────────────────┐
            ▼                                      ▼
      Agent code                            Application code
            │                                      │
            ▼                                      ▼
    ┌──────────────────────┐                ┌──────────────────────┐
    │ Will this code run   │                │ Goes in the          │
    │ against multiple     │                │ customer repo.       │
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
 reusable-agents     customer repo
 /agents/<id>/       /agents/<id>/
```

### What goes in `reusable-agents/agents/<id>/`

Code whose **logic doesn't change per site**. The behavior is shaped
by `site.yaml` (or equivalent config) injected via env at run time.

Examples:

- `seo-data-collector/` — pulls GSC/GA4 for any site (site_url +
  property_id come from config).
- `seo-analyzer/` — applies the same rule passes regardless of site.
- `progressive-improvement-agent/` — generic site-quality auditor
  driven by per-site `site.yaml`.
- `competitor-research-agent/` — generic competitor-comparison engine.
- `product-hydration-agent/` — hydrates a product catalog; works for
  any DB schema that exposes the configured column names.
- `responder-agent/`, `digest-rollup-agent/`, `agent-doctor/` —
  framework infrastructure.
- `implementer/` — generic LLM code editor; the per-site repo path,
  branch, and DB DSN come from `site.yaml` and env.

**Anti-pattern**: an agent that hardcodes a domain, an Amazon associate
tag, a database name, or a customer's column names. If you find yourself
typing `if site == 'specpicks':` in the framework, **stop** — that
behavior belongs in the per-site `site.yaml` instead.

### What goes in `<customer-repo>/agents/<id>/`

Three things, no more:

1. **`manifest.json`** — registers the per-site agent instance with
   the framework. Has the `id` (typically `<site>-<agent>`), `cron_expr`,
   `entry_command` that exports a config env var pointing at this dir's
   `site.yaml`, and the path to the `runbook` / `skill` files.
2. **`site.yaml`** (or `<config>.yaml`) — the site-specific knobs
   (domain, GSC site URL, GA4 property id, DB DSN env, page-inventory
   regexes, revenue_focus block, etc.).
3. **`AGENT.md`** + **`README.md`** — runbook the LLM reads + human
   overview.

When the manifest's `entry_command` looks like:

```bash
SEO_AGENT_CONFIG=/abs/path/to/site.yaml \
    bash /home/voidsstr/development/reusable-agents/agents/seo-opportunity-agent-orchestrator/run.sh
```

…you have correctly separated config from code.

**Anti-pattern**: putting agent **code** in the customer repo. If a
new site needs an agent that doesn't exist yet, the code goes in
`reusable-agents/agents/<id>/` — not next to the site.yaml. Even if
the first user is just one site, the next site will want the same
engine and shouldn't have to copy code across repos.

### Customer-specific agents (the rare case)

A small set of agents are tightly coupled to one customer and would
never make sense elsewhere. Examples:

- `nsc-assistant/agents/retro-multiplayer-refresh/` — only meaningful
  for the four-machine retro fleet at NSC.
- `nsc-assistant/agents/aisleprompt-kitchen-scraper/` — pulls data
  AislePrompt's specific scraping pipeline expects.
- `specpicks/agents/article-author-agent/` — its DB queries are
  bonded to SpecPicks' `editorial_articles` and `buying_guides`
  tables. (Generalizing this into reusable-agents is a future task.)

These keep their **code** in the customer repo because the code is
truly site-specific. They register the same way as any other agent.

### What goes in `~/.reusable-agents/` (NOT in any repo)

- `secrets.env` — `BRIGHTDATA_API_KEY`, `DATABASE_URL_<UPPER_SITE>`,
  `AMAZON_PAAPI_*` (when applicable), `FRAMEWORK_API_TOKEN`.
- `claude-pool/` — round-robin Claude Max account profiles.
- OAuth tokens for GSC + GA4 + Microsoft Graph (sending mail).
- Per-agent run-data dirs when running with the local-FS storage
  backend (production uses Azure Blob).

Anything in `~/.reusable-agents/` is per-host and never crosses the
git boundary.

## Side-by-side example: SEO agent

The SEO agent is the canonical case for the boundary. Here's exactly
how the parts split:

```
reusable-agents/                           ← framework + reusable engine
├── agents/seo-data-collector/             ← collector code (site-agnostic)
├── agents/seo-analyzer/                   ← analyzer code (site-agnostic)
├── agents/seo-reporter/                   ← reporter code (site-agnostic)
├── agents/seo-deployer/                   ← deployer code (site-agnostic)
├── agents/implementer/                    ← LLM editor (site-agnostic)
├── examples/sites/
│   ├── generic.yaml                       ← reference template
│   ├── specpicks.yaml                     ← example: SpecPicks
│   └── aisleprompt.yaml                   ← example: AislePrompt
└── shared/schemas/site-config.schema.json ← validates site.yaml

specpicks/                                  ← customer repo
└── agents/seo-opportunity-agent/
    ├── manifest.json                      ← registers `specpicks-seo-opportunity-agent`
    ├── site.yaml                          ← SpecPicks-specific values
    ├── db-queries.sql                     ← SpecPicks DB-specific @@QUERY blocks
    ├── README.md                          ← per-site overview
    ├── AGENT.md                           ← LLM runbook
    └── run.sh                             ← exports SEO_AGENT_CONFIG
                                            and runs the framework chain

nsc-assistant/                              ← AislePrompt's host repo
└── agents/aisleprompt-seo-opportunity-agent/
    ├── manifest.json                      ← registers `aisleprompt-seo-opportunity-agent`
    ├── site.yaml                          ← AislePrompt-specific values
    ├── db-queries.sql                     ← AislePrompt @@QUERY blocks
    ├── README.md
    ├── AGENT.md
    └── run.sh                             ← exports SEO_AGENT_CONFIG

~/.reusable-agents/                         ← per-host (not in any repo)
└── secrets.env
    ├── DATABASE_URL_SPECPICKS=...
    ├── DATABASE_URL_AISLEPROMPT=...
    └── BRIGHTDATA_API_KEY=...
```

Adding a new site to SEO automation is **fully config-only**:

1. Copy `examples/sites/generic.yaml` → `examples/sites/<new-site>.yaml`,
   edit values.
2. Copy a `seo-opportunity-agent/` dir from any existing customer repo
   into the new site's repo, edit `manifest.json` `id` + `entry_command`
   path, and either symlink or copy the new yaml.
3. Add `DATABASE_URL_<NEW_SITE>=...` to `~/.reusable-agents/secrets.env`.
4. Re-register: `bash scripts/register-agents.sh`.

No code change needed. Same for adding a new site to the
`progressive-improvement-agent` or `competitor-research-agent`
blueprints.

## Side-by-side example: Product hydration

Same pattern, different agent:

```
reusable-agents/
└── agents/product-hydration-agent/        ← engine
    ├── agent.py                            ← reads site.yaml, calls
    │                                         BrightData OR PA-API,
    │                                         hydrates content via Claude
    ├── brightdata_client.py                ← reusable price-refresh client
    ├── paapi_client.py                     ← reusable Amazon PA-API client
    └── prompts/hydrate_product_system.md   ← LLM system prompt

specpicks/
└── agents/product-hydration-agent/
    ├── manifest.json                       ← cron `15 */2 * * *`
    ├── site.yaml                           ← SpecPicks DB knobs
    ├── select-featured.py                  ← SpecPicks-specific curator
    │                                         (called via FEATURED_SELECT_SCRIPT
    │                                         env var — opt-in)
    └── run.sh
```

Even `select-featured.py` — which IS site-specific (its focus areas are
SpecPicks's home-page categories) — is wired through a generic env-var
hook (`FEATURED_SELECT_SCRIPT`). The hydration agent in
`reusable-agents/` doesn't know about SpecPicks; it just shells out to
whatever script the env var points at, if any.

## Migration: when an agent crosses the boundary

It's normal for an agent to start as customer-specific and graduate
into reusable. The migration pattern:

1. **Build it in the customer repo first.** Don't pre-generalize.
2. When a second site wants the same engine, look at what's actually
   per-site (almost always: domain, DB connection, table names, column
   names, credentials). Move the rest to `reusable-agents/agents/<id>/`.
3. Add the per-site values to a `site.yaml` schema in
   `shared/schemas/`. Keep `additionalProperties: false` strict so
   typos are caught at registration.
4. The customer repo keeps a thin manifest + `site.yaml` + (optional)
   per-site script wired through an env var.
5. Update [`agents-catalog.md`](agents-catalog.md) with the new
   reusable code path.

## Common mistakes (don't do these)

| Mistake | Why it's wrong | What to do instead |
|---|---|---|
| Hardcoding a domain in `reusable-agents/agents/.../analyzer.py` | Forks behavior per-site invisibly | Read it from `cfg.site.domain` |
| Putting `requirements.txt` for site-specific Python deps in `reusable-agents/` | Bloats the framework's deps | Install them in the customer-repo agent's venv |
| Committing `secrets.env` or OAuth tokens to ANY repo | Leaks credentials | `~/.reusable-agents/secrets.env` (mode 0600) |
| Embedding the `claude-pool` config inline in an agent | Each agent reinvents pool routing | The agent's `run.sh` exports `PATH=$CLAUDE_POOL_ROOT/bin:$PATH` and the rest is automatic |
| Skipping `register-agents.sh` after editing a manifest | Framework keeps the old version | Always re-register; it's idempotent |
| Manually creating a systemd timer for a new agent | The framework writes them on register | `cron_expr` in manifest → timer auto-written |
| Writing recommendations or run state to a custom path | Dashboard can't surface it | Use `framework.core.storage` (or `self.storage` on AgentBase) |

## Quick reference

| Question | Answer |
|---|---|
| New agent's first home | Wherever you're building. Move to reusable-agents on second use. |
| New per-site instance of an existing reusable agent | Customer repo's `agents/<site>-<id>/` with a manifest + site.yaml |
| Site-specific helper script (curator, query-builder) | Customer repo, called from the reusable agent via env-var hook |
| New rec type / rule pass | Reusable-agents (analyzer.py) — gate it on a `site.yaml` flag if it's optional |
| New blueprint | `reusable-agents/blueprints/<name>/BLUEPRINT.md` + `template/` |
| Cross-cutting framework feature (storage, status, dispatch) | `reusable-agents/framework/core/` |
