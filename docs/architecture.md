# Reusable-Agents Framework Architecture

> The big-picture map of how the framework, agents, customer repos, and
> external integrations fit together. Read this before you wire a new
> agent or wonder why a piece of state lives where it does.

## The three-tier model

```
┌──────────────────────────────────────────────────────────────────────┐
│  Customer repos  ←  application code, manifests, per-site config     │
│  (specpicks, aisleprompt-via-nsc-assistant, your-future-site)        │
└──────────────────────┬───────────────────────────────────────────────┘
                       │ POST /api/agents/register on every change
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  reusable-agents framework                                           │
│   • framework/core/      (Python lib — AgentBase, storage, dispatch) │
│   • framework/api/       (FastAPI HTTP API — registry, runs, queue)  │
│   • framework/ui/        (React dashboard — http://localhost:8091/)  │
│   • agents/<id>/         (agents whose CODE is reusable across sites)│
│   • install/             (host-worker, systemd writer, scaffolds)    │
└──────────────────────┬───────────────────────────────────────────────┘
                       │ Reads / writes
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Azure Blob Storage (or local FS for dev)                            │
│   container: agents                                                  │
│     agents/<id>/manifest.json                                        │
│     agents/<id>/runs/<run-ts>/...                                    │
│     agents/<id>/responses-queue/<request-id>.json                    │
│     agents/responder-agent/auto-queue/<request-id>.json              │
│     registry/agents.json                                             │
│     registry/events.jsonl                                            │
└──────────────────────────────────────────────────────────────────────┘
```

The boundary is intentional: **agent application logic** lives in customer
repos; **agent infrastructure** lives in the framework; **shared mutable
state** lives in blob storage. No agent ever reads another agent's
internals — they communicate only through framework primitives
(messages, responses-queue, blob reads).

## What runs where

### On the dev/host box

- `reusable-agents-api` and `reusable-agents-ui` Docker containers
  (managed by the `docker compose` in this repo). They expose port
  8093 (API) and 8091 (UI).
- `host-worker` — a systemd `--user` service that polls
  `/tmp/agent-trigger-queue/` for "Run now" trigger files and execs the
  registered agent's `entry_command` on the host (containers can't write
  systemd units, so triggered runs run on the host).
- One systemd `--user` timer per scheduled agent — auto-written by the
  framework when an agent registers with a `cron_expr`. Timer fires the
  agent's `entry_command` on the host directly.
- `claude-pool` — round-robin shim over multiple Claude Max accounts
  (`~/.reusable-agents/claude-pool/`). Used by every agent that
  invokes `claude` via PATH so concurrent runs don't queue on a single
  account.

### In Azure (production dashboard mirror)

The same framework UI/API also runs as Azure Container Apps so the
dashboard is reachable at
`agents.happysky-24190067.eastus.azurecontainerapps.io` even when the
dev box is offline. The Azure copy is a read-only mirror — actual agent
runs happen on the dev host where systemd timers fire.

### What does NOT run

- The framework does not embed an LLM. Every agent calls `claude` /
  `ollama` / etc. directly when it needs inference.
- The framework does not host the customer apps themselves (specpicks,
  aisleprompt). Those have their own deploys (Azure Container Apps for
  the runtime, separate from the agent infra).

## Agent lifecycle (Python — `AgentBase` subclass)

```
trigger (cron / manual / chained-from-another-agent)
       ▼
┌──────────────────────────────────────────────────────────────────┐
│ AgentBase.run_once()                                             │
│   1. setup()           — first-call init                         │
│   2. pre_run()         — drains responses-queue, increments      │
│                          iteration_count, sets status=running    │
│   3. run()             — your code (returns RunResult)           │
│   4. post_run(result)  — writes runs/<ts>/{progress,decisions,   │
│                          context-summary,recommendations}.json,  │
│                          updates state/latest, sets status=idle, │
│                          fires events.jsonl entry                │
│   5. teardown()        — cleanup hook                            │
└──────────────────────────────────────────────────────────────────┘
       ▼
runs/<run-ts>/ written to Azure storage + dashboard updates
```

Every step is opt-out — an override of `pre_run()` can skip the
responses-queue drain, etc. — but the defaults are what 95% of agents
want.

## Run-dir layout

Every agent run writes to `agents/<agent-id>/runs/<UTC-ts>/`:

```
runs/20260501T194718Z/
  progress.json          { schema, agent_id, run_ts, started_at,
                           ended_at, status, progress, metrics }
  decisions.jsonl        per-decision log (one JSON per line, streamed)
  context-summary.md     human-readable narrative for next run
  errors.json            errors with stack traces (when status=failure)
  recommendations.json   for site-quality / SEO agents — ranked recs
  responses.json         user-reply queue (for legacy responder consumers)
  goal-progress.json     per-goal scoring snapshot
  artifacts/             diffs, screenshots, llm-output samples, etc.
  data/                  intermediate (GSC pulls, sitemap, audit JSON)
```

The framework **never** prunes run dirs — they're durable
audit trails. Storage cost is ~$0.018/GB/mo on Azure hot tier; even
heavy-runner agents stay under 1GB total.

## Inter-agent handoffs (the routing primitive)

Some recommendations the analyzer produces aren't code edits — they're
content-team work (orphan-article boost, internal-link addition, body
rewrites, FAQ generation). The implementer can't ship these because
its job is editing TypeScript / SQL / templates, not crafting prose.

The framework's **handoff protocol** lets any agent route work to
another agent that's better suited:

```
Analyzer emits a rec   →   tags it with handoff_target="article-author-agent"
                                        based on framework/core/work_types.py
                                        rec_type → handler mapping
       ↓
Reporter writes recs to recommendations.json + auto-queue/<request-id>.json
       ↓
Responder dispatches the rec batch to the implementer
       ↓
Implementer sees handoff_target on a rec  →  send_handoff() to that agent
                                              writes
                                              agents/<target>/handoff-queue/<id>.json
                                              + appends an "out" row to
                                              agents/implementer/handoffs.jsonl
       ↓
Target agent's next pre_run() calls drain_handoffs()  →  populates
                                                          self.inbound_handoffs
                                                          archives queue → processed
                                                          appends an "in" row to
                                                          agents/<target>/handoffs.jsonl
       ↓
Target agent's run() incorporates the handoffs into its work plan
(e.g. article-author treats them as REQUIRED proposals to address)
       ↓
On completion, target agent calls record_handoff_outcome() → appends
"outcome" row (shipped / in_progress / deferred / rejected) so the
digest can show the inter-agent flow.
```

Lookup tables in `framework/core/work_types.py`:

| rec_type | work_type | default handler |
|---|---|---|
| `article-orphan-boost` | internal_link_addition | article-author-agent |
| `content-expansion` / `snippet-rewrite` | body_md_edit | article-author-agent |
| `new-page-buying_guide` / `-use_case` | new_article_creation | article-author-agent |
| `new-page-comparison` | comparison_page_generation | head-to-head-agent |
| `catalog-broken-image` | quality_audit_fix | progressive-improvement-agent |
| `product-content-incomplete` | product_content_hydration | product-hydration-agent |
| `indexnow-submit` | index_submission | indexnow-submitter |
| (everything else — code/SSR/template/route work) | code_edit | implementer |

Sites can override the default routing in `site.yaml`:

```yaml
handoff_routes:
  # Route this rec_type to a specific agent regardless of default
  internal-link-add: specpicks-progressive-improvement-agent

site_handler_overrides:
  # Resolve generic handler ids to per-site instances
  article-author-agent: specpicks-article-author-agent
  progressive-improvement-agent: specpicks-progressive-improvement-agent
```

The digest email surfaces the inter-agent flow so the user can see what
routed where + what's stuck (outcome=in_progress for >48h, or no-outcome
inbound handoffs older than 24h get flagged).

## The dispatch graph

Agents communicate via three asynchronous channels. Nothing makes a
synchronous call into another agent's process.

```
┌──────────────┐     ┌──────────────────────┐     ┌──────────────┐
│ Agent A      │     │ Inter-agent message  │     │ Agent B      │
│ (any agent)  │────▶│ shared/messages/<id> │◀────│ (next pre_run│
│              │     │                      │     │  drains)     │
└──────────────┘     └──────────────────────┘     └──────────────┘

┌──────────────┐     ┌──────────────────────┐     ┌──────────────┐
│ seo-reporter │     │ auto-queue/<id>.json │     │ responder-   │
│ writes after │────▶│ (one file per run    │────▶│ agent ticks  │
│ analyzer     │     │  with rec ids)       │     │ every 1 min  │
└──────────────┘     └──────────────────────┘     └─────┬────────┘
                                                        │
                                  systemd-run --user --scope
                                                        ▼
                                                 ┌──────────────┐
                                                 │ implementer  │
                                                 │ ships the    │
                                                 │ rec batch    │
                                                 └──────────────┘

┌──────────────┐     ┌──────────────────────┐     ┌──────────────┐
│ User email   │     │ responses-queue/     │     │ implementer  │
│ reply (rare) │────▶│ <request-id>.json    │────▶│ pre_run     │
│              │     │ (action: implement / │     │ drains it   │
│              │     │  defer / skip)       │     │             │
└──────────────┘     └──────────────────────┘     └──────────────┘
```

**Every recurring scheduled agent currently auto-queues** (see
`feedback_seo_auto_queue` memory). The user-reply path is now an
override — used only to defer / skip / revert recs after they've
already shipped. The single exception is `ebay-product-sync-agent`
which keeps a one-time `schema-mapping-approval` gate because a wrong
schema mapping ruins thousands of products.

## Storage hierarchy (Azure Blob)

Single container (default name `agents`), connection via
`AZURE_STORAGE_CONNECTION_STRING`. Hierarchical via prefixes — blob
listings with `--prefix agents/<id>/` are fast and indexable by date.

```
registry/
  agents.json            master list (replaces nsc_dashboard.db agents table)
  events.jsonl           append-only framework event log

agents/<agent-id>/
  manifest.json          id, name, category, schedule, owner, capabilities
  directives.json        current operating directives + version chain
  prompts/AGENT.md       active runbook the implementer reads
  prompts/SKILL.md       Claude Code task definition (when applicable)
  prompts/history/       past prompts (audit trail)
  goals/current.json     active goals + iteration counts
  goals/history/         historical goals snapshots
  status.json            current activity (live UI reads this)
  state/latest.json      state to carry to next run
  state/history/
  runs/<run-ts>/         see "Run-dir layout" above
  context-summaries/     daily narrative rollups
  changelog.jsonl        release tags, commits, version bumps
  outbound-emails/       email-id → routing metadata (for replies)
  responses-queue/       parsed user replies awaiting agent pickup
  confirmations/         pending dangerous-action approvals

shared/
  messages/<id>.json     inter-agent async messages
  inboxes/<agent-id>/    pointer files for fast inbox listing
```

## Per-site agent vs. reusable agent

The framework distinguishes two flavors of agent:

### Reusable agents (live in `reusable-agents/agents/`)

The agent **code** is generic. Every site that uses it ships its own
`site.yaml` to drive site-specific behavior. Examples:

- `seo-data-collector`, `seo-analyzer`, `seo-reporter`, `seo-deployer`
- `implementer`, `responder-agent`, `digest-rollup-agent`
- `competitor-research-agent`, `progressive-improvement-agent`
- `product-hydration-agent`

When AislePrompt + SpecPicks both want SEO recs, they each register a
**per-site manifest** like
`nsc-assistant/agents/aisleprompt-seo-opportunity-agent/manifest.json`
or `specpicks/agents/seo-opportunity-agent/manifest.json` whose
`entry_command` exports `SEO_AGENT_CONFIG=...site.yaml` and runs the
shared collector → analyzer → reporter chain in `reusable-agents/`.
The framework records each per-site instance as a distinct agent id
so the dashboard can show their runs separately.

### Customer-specific agents (live in customer repos)

The agent code is tightly coupled to one application. Example:
`nsc-assistant/agents/retro-multiplayer-refresh` cares about a Windows
XP retro-gaming fleet; it would never run on AislePrompt. These live
inside the customer repo and register from there.

See [`docs/repo-boundaries.md`](repo-boundaries.md) for the explicit
rules on what goes where.

## The data flow for one SEO recommendation

End-to-end trace of how a single rec moves through the system:

```
1. Cron fires `30 */3 * * *` → systemd timer execs run.sh
   in specpicks/agents/seo-opportunity-agent.

2. run.sh runs three commands in series, all writing to the same
   <run-ts> dir:
     python3 reusable-agents/agents/seo-data-collector/pull-data.py
     python3 reusable-agents/agents/seo-analyzer/analyzer.py
     python3 reusable-agents/agents/seo-reporter/send-report.py

3. Collector writes data/{gsc-*.json,ga4-*.json,sitemap-urls.json,
   pages-by-type.jsonl,db-stats.json,articles-inventory.json} via the
   framework's storage abstraction.

4. Analyzer reads those files + applies its full rule suite (~25
   passes — see [`agents-catalog.md`](agents-catalog.md) for the
   full list, includes content-gap, on-page, schema completeness,
   affiliate-tag leak, article × featured-product mention attribution,
   TTFB / freshness / FAQ-quality / hreflang / footer-trust /
   breadcrumb parity / trust-signal density / revenue-focus PDP /
   diff alarms / competitor keyword gaps, and a final adaptive LLM
   audit). Each pass emits via a shared next_id() counter so ids stay
   unique. Output: recommendations.json (~10-15 recs after dedup +
   already-handled filtering + max_recs_per_run cap).

5. Reporter renders an HTML email, hands it to send_via_msmtp. Because
   DIGEST_ONLY=1 is set in the systemd unit, the mailer suppresses the
   send and instead drops the rendered email + recs into
   agents/digest-rollup-agent/digest-queue/<ts>-<hash>.json.
   Reporter ALSO writes a separate trigger:
   agents/responder-agent/auto-queue/<request-id>.json listing every
   rec id with action=implement.

6. responder-agent ticks every minute. Its pre_run drains
   auto-queue/ first (before checking IMAP). For each pending file it
   spawns:
     systemd-run --user --scope agent-dispatch-implementer-<site>-<ts>
   passing RESPONDER_REC_IDS=<comma-list> + RESPONDER_RUN_DIR=<run-ts>
   into the implementer.

7. implementer/run.sh resolves DATABASE_URL_<UPPER_SITE> from
   ~/.reusable-agents/secrets.env, ensures claude-pool routing is
   active, and execs `claude --print` against AGENT.md with the
   recommendations.json + run-dir context.

8. Claude opens files, makes edits, commits + tags
   agent/<agent-id>/release/<run-ts>, optionally chains to
   seo-deployer for testing/pushing. Per-rec completion emails go
   through the same digest gate.

9. digest-rollup-agent runs every 3h on its own cron (0 */3 * * *).
   It reads the digest-queue/, groups by site + agent, renders one
   summary HTML, sends ONE email to mperry@nsc, archives the queue
   files into digest-queue-processed/.
```

The user receives **one email every 3 hours** summarizing every SEO,
article, and product action that fired across all registered sites.

## Reading next

| What you want to do | Read |
|---|---|
| Find what every agent does | [`agents-catalog.md`](agents-catalog.md) |
| Decide where a new agent's code lives | [`repo-boundaries.md`](repo-boundaries.md) |
| Onboard a new site to the SEO agent | [`seo-onboard-new-site.md`](seo-onboard-new-site.md) |
| Pick a blueprint for a new agent | [`../blueprints/README.md`](../blueprints/README.md) |
| Understand the manifest schema | [`../shared/schemas/site-config.schema.json`](../shared/schemas/site-config.schema.json) |
| Learn the framework Python API | [`../README.md`](../README.md) §Manifest, §Quick start |
