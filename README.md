# reusable-agents

> A self-hostable framework for running LLM-driven agents with shared
> memory, scheduled execution, human-in-the-loop confirmations, and a
> control dashboard. Agents register with a local instance from their
> own repos, get auto-scheduled via systemd, and write all state to
> Azure Blob Storage so they get smarter over time.

## Dashboard at a glance

**Agent grid** — color-coded by category, glowing while running, with
filter pills for application (🛒 aisleprompt / 🎮 specpicks /
🔧 reusable-agents / etc.) and confirmation/queue-driven badges per
card:

![Agent grid](docs/screenshots/agent-grid.png)

**n8n-style dependency graph** — every agent is a node; edges show
pipeline triggers, email-confirmation flows, queue dispatches, and
shared-config ties. Drag-to-reposition with localStorage persistence,
auto-layout via elkjs, custom edge styles per relationship kind:

![Dependency graph](docs/screenshots/dependency-graph.png)

Click any node to see what it depends on + what it triggers:

![Graph side panel](docs/screenshots/dependency-graph-side-panel.png)

**Per-agent detail** — overview with confirmation-flow banner,
dependencies, runs drill-down with per-run artifacts (recommendations,
emails, decision logs), and a Goals tab showing persistent objectives
with progress bars and 30-point sparklines:

![Agent detail with goals](docs/screenshots/agent-detail-goals.png)

Filtering the grid to a single application:

![Application filter](docs/screenshots/agent-grid-filtered.png)



## Why

Most agent systems are monoliths. You install one product and your agents
have to live inside it. This framework inverts the relationship:

- **Your agent code lives in your own repo** (or wherever it makes sense
  for its problem domain).
- The framework runs **next to** your agents and provides the cross-cutting
  infrastructure: registration, scheduling, status, decision logs, message
  bus, confirmations for dangerous actions, an HTTP API + UI.
- Each repo POSTs its `manifest.json` files to the local framework
  instance and immediately gains: scheduled execution (systemd timers
  auto-wired), live status visibility (UI glows when working), a durable
  decision log, and inter-agent communication.

You can run one framework instance for personal projects, share it across
several of your repos (this codebase already does — `nsc-assistant`,
`specpicks`, etc. all register with the same instance), or fork it for
production deployments.

## Documentation

| Doc | What you read it for |
|---|---|
| **[`docs/architecture.md`](docs/architecture.md)** | The big-picture map: framework + customer repos + Azure storage, lifecycle of one agent run, end-to-end trace of how a SEO recommendation moves through the system. **Start here.** |
| **[`docs/agents-catalog.md`](docs/agents-catalog.md)** | Every agent in the fleet, organized by category, with code path + manifest path + cron schedule + one-line description. |
| **[`docs/repo-boundaries.md`](docs/repo-boundaries.md)** | What goes in `reusable-agents/` vs in a customer repo (specpicks, nsc-assistant) vs in `~/.reusable-agents/` (per-host). Decision tree + side-by-side examples + common mistakes. |
| **[`docs/seo-onboard-new-site.md`](docs/seo-onboard-new-site.md)** | Step-by-step: add a new site to the SEO agent in 5 commands. |
| **[`blueprints/README.md`](blueprints/README.md)** | The five blueprint patterns (site-quality-recommender, pipeline-stage, inbox-poller, llm-code-editor, scheduled-task) + when to pick each. |
| **[`shared/schemas/site-config.schema.json`](shared/schemas/site-config.schema.json)** | The canonical `site.yaml` schema. Validates every per-site config at registration. |
| **[`agents/<id>/README.md`](agents/)** | Per-agent docs (collector, analyzer, reporter, deployer, implementer, responder, digest-rollup, agent-doctor, hydration, ebay-sync, progressive-improvement, competitor-research). |
| **[`CLAUDE.md`](CLAUDE.md)** | Instructions for Claude Code when working in this repo. References the docs above. |

## What's in the box

```
reusable-agents/
├─ framework/
│  ├─ core/                  Importable Python package — the foundation
│  │   ├─ agent_base.py       AgentBase lifecycle (setup/pre_run/run/post_run/teardown)
│  │   ├─ storage.py          StorageBackend abstraction (Azure Blob + LocalFS)
│  │   ├─ registry.py         Master agent list (registry/agents.json in storage)
│  │   ├─ status.py           Live status writer + global event log
│  │   ├─ messaging.py        Inter-agent async messages (shared/messages/)
│  │   ├─ confirmations.py    @requires_confirmation decorator + approve/reject
│  │   ├─ decision_log.py     Per-run jsonl log + per-agent changelog
│  │   ├─ context_index.py    Date-indexed run summaries with daily rollups
│  │   ├─ scheduler.py        systemd --user timer/service writer (cron→OnCalendar)
│  │   ├─ release_tagger.py   git commit + tag agent/<id>/release/<run-ts> + push
│  │   ├─ email_codes.py      Subject-tag encode/decode, request-id generator
│  │   ├─ guardrails.py       Capability dataclass for declared dangerous methods
│  │   └─ mailer.py           Outbound mailer abstraction (LogMailer + Graph + SMTP)
│  ├─ api/                   FastAPI service (35 routes + 2 WebSockets)
│  │   ├─ Dockerfile          python:3.12-slim, non-root, healthcheck
│  │   ├─ host-worker.sh      Systemd-user service — exec triggers on host
│  │   └─ app/                Routes for agents/runs/status/messages/etc.
│  ├─ ui/                    React + Vite + Tailwind dashboard
│  │   ├─ Dockerfile          node:20 build → nginx:1.27-alpine, iframe-friendly
│  │   ├─ nginx.conf          Reverse-proxies /api + /ws to agent-api
│  │   └─ src/                AgentList, AgentDetail, Confirmations, Events
│  └─ tests/                 pytest suite — 20 tests cover core primitives
├─ blueprints/                Reusable agent-pattern templates (see blueprints/README.md)
│  ├─ site-quality-recommender/  Crawl + LLM analysis + email recs (auto-pilot capable)
│  ├─ pipeline-stage/             One step in a multi-stage pipeline (run-dir based)
│  ├─ inbox-poller/               IMAP loop, parses tagged subjects, dispatches replies
│  ├─ llm-code-editor/            Reads recs, drives LLM to apply edits, commits + deploys
│  └─ scheduled-task/             Default cron-driven script blueprint
├─ agents/                       All in-repo reference agents (consolidated layout)
│  ├─ progressive-improvement-agent/  Reference impl of site-quality-recommender (audits)
│  ├─ competitor-research-agent/      Reference impl of site-quality-recommender (competitor)
│  ├─ seo-{data-collector,analyzer,reporter,implementer,deployer}/  Reference SEO pipeline
│  ├─ responder-agent/                Reference impl of inbox-poller
│  └─ reusable-agents-competitor-research-agent/  Self-improvement instance (codebase mode)
├─ shared/
│  ├─ schemas/                  JSON schemas (recommendations + site config)
│  ├─ site_config.py            SEO site-config loader
│  └─ site_quality.py           Site-quality config loader + tier scoring + email render
├─ install/
│  ├─ register-agent.sh         POSTs one manifest.json to the framework
│  ├─ register-all-from-dir.sh  Walks a dir and registers every manifest.json
│  ├─ install-host-worker.sh    Sets up the host-worker systemd unit
│  ├─ install.sh                One-shot installer (validates env, brings up stack, seeds providers)
│  ├─ bootstrap-azure.sh        Creates Azure resource group + storage account + container
│  ├─ seed-providers.sh         Seeds AI provider skeletons (Azure / Anthropic / Ollama / Copilot / OpenAI)
│  └─ seed-providers-local.sh   Host-tailored seeder for the dev box
├─ docker-compose.yml           API + UI services
├─ .env.example                 Operator config template
└─ examples/sites/*.yaml        Per-site SEO config templates (legacy use)
```

## Quick start

### 1. Bring up the framework

```bash
git clone https://github.com/voidsstr/reusable-agents
cd reusable-agents

cp .env.example .env
$EDITOR .env                # set FRAMEWORK_API_TOKEN, AZURE_STORAGE_CONNECTION_STRING

docker compose up -d --build
```

The API runs on port 8090, the UI on 8091. If 8090 conflicts with another
service, override via `.env`:

```dotenv
# Anywhere in .env — the docker-compose ports are also configurable
FRAMEWORK_API_PORT=8093
```

### 2. Install the host-worker

The host-worker exec's "Run now" triggers on the host (not in the API
container) so agents get full access to docker, git, az, ssh, etc.

```bash
bash install/install-host-worker.sh
```

This writes `~/.config/systemd/user/reusable-agents-host-worker.service`
and starts it. Linger is enabled so it survives logout.

### 3. Register agents from your repos

In any repo that has agent definitions, run:

```bash
bash /path/to/reusable-agents/install/register-all-from-dir.sh ./agents
```

Or build a thin wrapper in the consuming repo (see
[`nsc-assistant/scripts/register-agents.sh`](https://github.com/voidsstr/nsc-assistant/blob/master/scripts/register-agents.sh)
for an example).

### 4. Open the UI

http://localhost:8091/

The card grid auto-glows when an agent is running. Click into an agent for
runbook, runs, decisions, messages, storage browser, confirmations, and
release changelog.

## Manifest format

Every agent dir has a `manifest.json` describing it:

```json
{
  "id": "specpicks-scraper-watchdog",
  "name": "SpecPicks Scraper Watchdog",
  "description": "Restarts the scraper container if it dies.",
  "category": "research",
  "task_type": "desktop-task",
  "cron_expr": "*/5 * * * *",
  "timezone": "America/Detroit",
  "enabled": true,
  "owner": "you@example.com",
  "runbook": "AGENT.md",
  "skill": "SKILL.md",
  "entry_command": "bash /absolute/path/to/agent/run.sh",
  "metadata": {
    "framework": "reusable-agents",
    "source_repo": "specpicks"
  }
}
```

Field reference:

| Field | Required | What |
|---|---|---|
| `id` | yes | Stable kebab-case id; primary key for the framework |
| `name` | yes | Display name for the UI |
| `description` | no | One-line summary |
| `category` | no | One of `seo / research / fleet / personal / ops / misc` (or your own) |
| `task_type` | no | `desktop-task` (host) / `cloud-routine` (Anthropic Routines) / `manual` |
| `cron_expr` | no | 5-field cron — auto-wires a systemd timer if set |
| `timezone` | no | IANA tz, default `UTC` |
| `enabled` | no | If false, schedule is registered but disabled |
| `owner` | no | Email — gets confirmation requests for dangerous actions |
| `runbook` | no | Path (relative to manifest dir) to AGENT.md, or use the convention |
| `skill` | no | Path to SKILL.md (Claude Desktop task definition) |
| `entry_command` | no | Shell command for the host-worker to exec on "Run now" |
| `metadata` | no | Free-form JSON — flow through to the registry |

## Where everything lives

When an agent runs there are three places state lands: **agent data**
(the canonical, durable home in the framework storage backend), **logs**
(transient process output on the host), and **config** (some in the
storage backend, some in version-controlled repos, some on the host).
This section is the operator reference for finding any of it.

### 1. Agent data — Azure Blob (`agents` container)

Everything an agent produces or accumulates lives here. One container,
hierarchical by key prefix. Backend is selected via env (default azure
when `AZURE_STORAGE_CONNECTION_STRING` is set; falls back to local FS at
`AGENT_STORAGE_LOCAL_PATH`, default `~/.reusable-agents/data`, for dev).

```
registry/
  agents.json                          # master registry — every registered agent's manifest
  events.jsonl                         # global event log (state transitions, registrations, etc.)

config/
  ai-providers.json                    # registered AI providers (azure-openai, anthropic, ollama, copilot, claude-cli)
  ai-defaults.json                     # global default + per-agent overrides

agents/<agent-id>/
  manifest.json                        # canonical manifest (mirror of registry/agents.json[id])
  status.json                          # current state, message, progress — what the dashboard reads
  runbook.md                           # the AGENT.md prompt (embedded at registration)
  skill.md                             # the SKILL.md prompt (embedded at registration)
  readme.md                            # human-readable overview (embedded at registration)

  state/latest.json                    # carried-forward state (next-run uses this)
  state/history/<run-ts>.json          # snapshot per run for audit

  goals/active.json                    # long-running goals + current metric values
  goals/changes.jsonl                  # append-only log of recs dispatched against goals,
                                       # with metric_before/after deltas (drives adaptive prompts)

  runs/<run-ts>/
    progress.json                      # start/end ts, status, metrics
    decisions.jsonl                    # streaming log of decisions/observations from the run
    context-summary.md                 # human-readable narrative for next run
    recommendations.json               # generated recs (SEO/PI/CR/catalog-audit)
    responses.json                     # parsed user replies for THIS run
    email-rendered.html                # body of the email this run sent (where applicable)
    pages.jsonl                        # crawl output (PI/CR)
    snapshot.json / comparison.json    # pre/post run diff (SEO)
    data/                              # raw ingest (GSC, GA4, etc.)
    deploy.json                        # deployer artifacts (where applicable)
    artifacts/*                        # agent-specific extras

  context-summaries/<YYYY-MM-DD>.md    # daily rollups (caps prompt size for older runs)
  changelog.jsonl                      # release tags + commit SHAs from production-affecting runs

  outbound-emails/<request-id>.json    # email metadata (subject, recipients, rec ids) — used to
                                       # route replies and render the dashboard's Confirmations tab
  responses-queue/<request-id>.json    # parsed user replies awaiting pickup by an implementer
  confirmations/<request-id>.json      # pending dangerous-action approvals (per-action gate)
  errors/<ts>-<class>.json             # unrecoverable errors recorded by the resilience layer

shared/
  messages/<message-id>.json           # inter-agent async messages (target_agent in body)
  inboxes/<agent-id>/<message-id>      # zero-byte markers for fast inbox listing
```

Why blob keys over Storage Queues for messages: indexable by date,
auditable in the portal, no 7-day queue retention cap.

### 2. Logs — host filesystem (`/tmp/reusable-agents-logs/`)

Process stdout/stderr that doesn't belong in durable storage. Cleared on
host reboot; the API container bind-mounts this read-only so the
dashboard's "Live LLM" tab can tail them.

```
/tmp/reusable-agents-host-worker.log                          # host-worker service stdout/stderr
/tmp/reusable-agents-logs/<agent-id>-<run-ts>.log             # per-run agent stdout
/tmp/reusable-agents-logs/agent-<id>.log                      # systemd-user service log (responder, etc.)
/tmp/reusable-agents-logs/dispatch-implementer-<site>-<ts>.log
                                                              # transient implementer scope output
                                                              # (claude --print + tool calls)
```

The dispatch logs are what the **Live LLM** tab tails in real time.
Decision logs (`decisions.jsonl`) and progress JSONs go to **storage**
(durable), not here.

### 3. Configuration — three layers

Config is split intentionally so secrets stay on the host, agent code
stays in version control, and per-instance settings stay in the repo
that owns the application.

#### 3a. Framework config (host + storage)

| What | Where | Purpose |
|---|---|---|
| Storage backend choice + connection string | `reusable-agents/.env` (gitignored) | `STORAGE_BACKEND=azure`, `AZURE_STORAGE_CONNECTION_STRING=…`, `AZURE_STORAGE_CONTAINER=agents`, `FRAMEWORK_API_TOKEN=…` |
| Host-worker systemd env | `~/.config/systemd/user/reusable-agents-host-worker.service` | Same Azure env as `.env` so the host-worker writes to the same storage |
| Docker-compose host overrides | `reusable-agents/docker-compose.override.yml` (gitignored) | Per-host port bindings, bind mounts |
| AI provider registry | `agents/config/ai-providers.json` (in storage) | Editable from the dashboard's AI Providers page |
| AI provider defaults / per-agent overrides | `agents/config/ai-defaults.json` (in storage) | Same, editable from UI |
| Responder IMAP/OAuth config | `~/.reusable-agents/responder/config.yaml` (host, gitignored) | IMAP host, mailbox, oauth file path, dispatcher routes |
| Responder OAuth token | `~/.reusable-agents/responder/.oauth.json` (host, mode 0600) | XOAUTH2 refresh token; used by responder + Graph email send |

#### 3b. Reusable agent code

The reusable framework + agents repo (this repo). Cloned to a known
path on the host (default `/home/voidsstr/development/reusable-agents`).

```
reusable-agents/
  framework/                       # core lib (storage, status, registry, scheduler, …)
    api/                           # FastAPI service (Dockerized)
    ui/                            # React dashboard (Dockerized)
    core/                          # AgentBase, ai_providers, goals, goal_changes,
                                   #   email_codes, completion_email, resilience, …
  agents/<reusable-agent-id>/      # generic agent bodies (no per-site assumptions)
    agent.py                       # subclass of AgentBase
    AGENT.md                       # runbook prompt
    SKILL.md                       # task definition for Claude Desktop
    manifest.json                  # template manifest
    requirements.txt
    README.md
  shared/                          # cross-agent helpers (site_quality, run_files, schemas)
  blueprints/                      # cookiecutter-style templates for new agents
  install/                         # bootstrap scripts (create-agent.sh, install-host-worker.sh)
  examples/sites/<site>.yaml       # generic per-site configs (used by SEO/PI/CR)
```

#### 3c. Per-instance manifests + site configs

Per-app/site instances live in the repo that owns the application — NOT
in the reusable repo. This way each app's deploy pipeline carries its
own agent configs.

```
nsc-assistant/agents/<agent-id>/
  manifest.json                    # registers id, cron, owner, entry_command
  site.yaml                        # per-site config (DB URL, audit script command,
                                   #   reporter recipients, implementer repo path)
  README.md                        # operator notes for THIS instance

specpicks/agents/<agent-id>/
  manifest.json
  site.yaml
  README.md
```

The manifest's `entry_command` is what the host-worker exec's. It
typically points back at a reusable agent body in this repo:

```bash
# example from nsc-assistant/agents/aisleprompt-progressive-improvement-agent/manifest.json:
"entry_command": "PROGRESSIVE_IMPROVEMENT_CONFIG=$HOME/development/nsc-assistant/agents/aisleprompt-progressive-improvement-agent/site.yaml \
                  python3 $HOME/development/reusable-agents/agents/progressive-improvement-agent/agent.py"
```

### Quick lookup — "I want to find X for agent Y"

| Looking for… | Path |
|---|---|
| Current state of agent | dashboard `/agents/<id>` (reads `agents/<id>/status.json` from storage) |
| Live LLM output during a run | dashboard `/agents/<id>` → "Live LLM" tab (tails `/tmp/reusable-agents-logs/dispatch-implementer-*.log`) |
| Why an agent failed last run | `agents/<id>/runs/<run-ts>/decisions.jsonl` + `agents/<id>/errors/<ts>-*.json` in storage |
| What recs the agent generated | `agents/<id>/runs/<run-ts>/recommendations.json` (or the legacy `~/.openclaw/.../seo/runs/<site>/<ts>/` for SEO) |
| What user replied to | `agents/<id>/runs/<run-ts>/responses.json` |
| Email metadata for routing replies | `agents/<id>/outbound-emails/<request-id>.json` |
| What's queued for the implementer | `agents/implementer/responses-queue/*.json` |
| Goals + progress | `agents/<id>/goals/active.json` + `goals/changes.jsonl` |
| Agent's runbook prompt | `agents/<id>/runbook.md` (embedded at registration; source-of-truth is `manifest.runbook_path` in the repo) |
| Cron schedule | `~/.config/systemd/user/agent-<id>.timer` (auto-wired from manifest.cron_expr) |
| Host-worker log | `/tmp/reusable-agents-host-worker.log` |
| Responder log | `/tmp/reusable-agents-logs/agent-responder-agent.log` |
| AI provider for an agent | dashboard `/providers`, or `GET /api/providers/resolve/<id>` |
| Storage browser | dashboard `/agents/<id>` → "Storage" tab |

## Creating a new agent (the standard flow)

The framework ships an `install/create-agent.sh` scaffold script that sets up
a new agent dir conforming to all framework standards (manifest format,
runbook conventions, entry-script shape, registration glue). Use this when
adding a new agent to ANY repo — your repo, my repo, doesn't matter.

**Pick a [blueprint](blueprints/README.md) first** — it determines the shape
of what you're building:

| You want to... | Blueprint |
|---|---|
| Crawl a site, identify issues, email ranked recs, gate ship-time on user replies | `site-quality-recommender` |
| Build one stage of a multi-step pipeline (reads upstream run-dir, writes downstream) | `pipeline-stage` |
| Poll an IMAP inbox, parse subject tags, route replies to other agents | `inbox-poller` |
| Read approved recs, drive an LLM to apply edits, commit + tag + deploy | `llm-code-editor` |
| Run a script on a cron schedule (the default) | `scheduled-task` |

See `blueprints/<name>/BLUEPRINT.md` for when each fits, what files come
out, and which existing agents are reference implementations.

```bash
# Python agent (subclasses AgentBase, gets full lifecycle for free)
bash /path/to/reusable-agents/install/create-agent.sh \
    my-new-agent /path/to/your-repo/agents \
    --name "My New Agent" \
    --description "Pulls X, computes Y, emits Z" \
    --category research \
    --cron "*/30 * * * *" \
    --timezone "America/Detroit" \
    --owner "you@example.com" \
    --kind python

# Bash agent (lighter weight; no AgentBase, just an entry script)
bash /path/to/reusable-agents/install/create-agent.sh \
    my-watchdog /path/to/your-repo/agents \
    --description "..." --kind bash --cron "*/5 * * * *"

# Auto-register immediately after scaffolding
bash /path/to/reusable-agents/install/create-agent.sh \
    my-new-agent /path/to/your-repo/agents \
    --description "..." --register
```

### What gets created

```
your-repo/agents/<agent-id>/
├── manifest.json          # registry metadata (already filled in from CLI args)
├── AGENT.md               # runbook stub with conventions for decisions, state, gates
├── SKILL.md               # Claude Desktop task definition (frontmatter + body)
├── agent.py               # AgentBase subclass with example status/decide/confirm calls
├── run.sh                 # entry script the framework's host-worker invokes
├── README.md              # quick reference card
└── requirements.txt       # extra Python deps the agent needs
```

For `--kind bash`, you get `run.sh` only (no `agent.py`).

### Standards every new agent follows

**Every agent MUST declare goals.** Goals are persistent objectives
that the agent's runs incrementally advance. The framework tracks
progress over time (with a sparkline + progress bar in the dashboard)
and graduates goals to "accomplished" once their metric target is hit.

```json
{
  "id": "goal-zero-broken-pages",
  "title": "Drive broken-page count to 0",
  "description": "Every URL on the site returns 2xx with valid HTML.",
  "metric": {
    "name": "broken_pages",
    "current": 12,
    "target": 0,
    "direction": "decrease",
    "unit": "pages",
    "horizon_weeks": 4
  },
  "directives": [
    "flag every non-2xx response as critical",
    "auto-tier any rec with confidence >= 0.95 + severity in {critical,high}"
  ]
}
```

Goal directives are pasted into the agent's LLM system prompt at run
start to bias analysis. The `run()` should end with a call to
`framework.core.goals.record_goal_progress(...)` for each goal, pushing
the new measurement.

Schema: `shared/schemas/agent-goals.schema.json`. Seed via
`install/seed-default-goals.sh` (idempotent — preserves history) or PUT
to `/api/agents/<id>/goals`.



1. **Kebab-case ID** — `my-new-agent`, not `MyNewAgent` or `my_new_agent`.
2. **Manifest schema** — see [Manifest format](#manifest-format) below. The
   scaffold pre-fills it from the CLI args you pass.
3. **AGENT.md sections** — every runbook has the same eight headings so a
   new reader can scan: *What this agent does · Schedule · Inputs/Outputs ·
   Per-run flow · Hard gates · State carried · Decisions to log · Goals*.
4. **Lifecycle** (Python agents) — implement `run()` returning a `RunResult`.
   The framework handles state load + response-queue drain + decision log
   + context summary + error capture + status updates.
5. **Capabilities declared** — list every meaningful method on the class
   with `declare(name, description, confirmation_required=...)`. The UI
   audits these.
6. **Confirmation-gated dangers** — wrap any production-affecting method
   with `@requires_confirmation(reason=...)`. The framework emails the
   owner; nothing happens until the owner approves (via email reply or
   the dashboard).
7. **Status reporting** — call `self.status("doing X", progress=0.5)`
   liberally. Drives the glow animation in the UI.
8. **Decision logging** — call `self.decide("plan"|"observation"|"choice"|...)`
   for anything a future run should know about.
9. **State persistence** — return `RunResult.next_state` for state to carry
   forward. Don't write directly to the filesystem; use storage abstraction.
10. **No `--no-verify`** on git commit (release-tagger enforces).

### Authoring without the scaffold

If you want to hand-roll an agent:

#### Subclass `AgentBase` (recommended for new agents)

```python
from framework.core.agent_base import AgentBase, RunResult
from framework.core.guardrails import declare

class SeoDeployer(AgentBase):
    agent_id = "seo-deployer"
    name = "SEO Deployer"
    category = "seo"
    capabilities = [
        declare("read_metrics", "Pull GSC + GA4 data"),
        declare("ship_to_prod", "Deploy a new container revision",
                confirmation_required=True, risk_level="high",
                affects=["production", "git", "billing"]),
    ]

    def run(self) -> RunResult:
        self.status("checking metrics", progress=0.2)
        self.decide("plan", "if delta < threshold, skip deploy")
        # … work …
        self.status("ready to ship", progress=0.9)
        return RunResult(status="success", summary="ok",
                         metrics={"changes_shipped": 0})

    @requires_confirmation(reason="deploys a new tag to production Azure")
    def ship_to_prod(self, tag: str): ...

if __name__ == "__main__":
    SeoDeployer().run_once()
```

Then add a `manifest.json` next to it and register:

```bash
bash /path/to/reusable-agents/install/register-agent.sh /path/to/your/agent
```

### Bash agents (lightweight option)

You don't have to subclass anything. Bash agents work fine — they just need
a `manifest.json` declaring `entry_command`. They won't get the AgentBase
lifecycle features (status, decisions, etc.) for free, but they're easy
to drop in.

## Email confirmation flow

For dangerous actions:

```
1. Agent calls @requires_confirmation method
2. Framework writes a pending confirmation to storage
3. Framework emails the agent's owner with subject [<agent-id>:<request-id>]
4. Owner replies "yes" / "no" — the responder agent picks it up via IMAP XOAUTH2
5. Responder writes the reply to <agent>/responses-queue/<request-id>.json
6. Next agent run's pre_run() drains the queue, resolves the confirmation
7. The originally-deferred call now succeeds (or raises ConfirmationRejected)
```

The same flow can be UI-driven: the dashboard's `Confirmations` page has
approve/reject buttons that write directly to storage, bypassing email.

## Inter-agent messaging

```python
# Agent A
self.message(to=["agent-b"], kind="request", subject="please refresh",
             body={"site": "aisleprompt"})

# Agent B (next run)
for msg in self.inbox():
    if msg["kind"] == "request":
        # … handle …
        self.mark_message_read(msg["message_id"])
```

Messages persist in `shared/messages/` indefinitely — useful for analytics
("what did agent X tell agent Y last month?"). Threading via `in_reply_to`.

## Composability with other systems

- **Existing scripts**: register a manifest pointing at your existing
  bash/python script. Zero refactor.
- **Microsoft Graph email**: the framework's mailer ships a Graph sendMail
  implementation with Send-As → Send-on-Behalf fallback.
- **OAuth2 IMAP**: the responder-agent dir has a complete XOAUTH2 setup
  for Office 365 + Google Workspace (one-time browser bootstrap, refresh
  tokens auto-rotate).
- **Anthropic Routines / Desktop Scheduled Tasks**: declare `task_type:
  cloud-routine` in the manifest and provide `routine_id` + `trigger_url`
  + `trigger_token_env`; the framework's trigger endpoint POSTs to
  Anthropic's `/fire` API instead of the host-worker queue.

## Deploying applications the agents touch

When an agent commits code (e.g., the SEO implementer applies a snippet
fix to `frontend/src/pages/RecipePage.tsx`), the framework can chain
straight into a 5-stage deploy pipeline so the change reaches
production without manual intervention:

```
test → build → push → deploy → smoke_check
```

The deployer is **cloud-agnostic by design** — every stage is just a
shell command template. Whatever you can express in `bash` (Azure CLI,
AWS CLI, kubectl, Terraform, custom scripts), you can deploy.

### Configuring per site

Each site declares its own pipeline under `deployer:` in its
`site.yaml`. Drop in any recipe from `examples/deployer/`:

| Recipe                                                     | Target                            | Status |
|------------------------------------------------------------|-----------------------------------|--------|
| [`azure-container-apps.yaml`](examples/deployer/azure-container-apps.yaml) | Azure Container Apps + ACR       | **active** |
| [`azure-app-service.yaml`](examples/deployer/azure-app-service.yaml)       | Azure App Service + ACR          | sample |
| [`azure-functions.yaml`](examples/deployer/azure-functions.yaml)           | Azure Functions (consumption)    | sample |
| [`aws-ecs-fargate.yaml`](examples/deployer/aws-ecs-fargate.yaml)           | AWS ECS Fargate + ECR            | sample |
| [`aws-lambda.yaml`](examples/deployer/aws-lambda.yaml)                     | AWS Lambda + ECR                 | sample |
| [`aws-app-runner.yaml`](examples/deployer/aws-app-runner.yaml)             | AWS App Runner + ECR             | sample |

Sample recipes are valid YAML you can copy verbatim — they just aren't
currently used by any production site, so they're shipped as
documentation. The two active recipes are wired into aisleprompt and
specpicks today.

### When the deployer fires

Per-batch — every successful implementer batch chains into deploy
unless the dispatch is DB-only (article-author / catalog-audit / h2h)
or `IMPLEMENTER_SKIP_DEPLOY=1` is in the environment.

### Substitution variables

Every stage's `cmd:` runs through a template substitution before exec:

| Variable     | Source                                       |
|--------------|----------------------------------------------|
| `{tag}`      | UTC timestamp set at deploy start            |
| `{image}`    | `deploy.vars.image`                          |
| `{app}`      | `deploy.vars.app`                            |
| `{rg}`       | `deploy.vars.rg` (or any other `vars:` key)  |
| `{<custom>}` | any key under `deploy.vars:`                 |

`{tag}` and `{image}` are top-level — every stage sees them. Anything
else under `deploy.vars:` is also expanded everywhere via the same
template substitution. So a Kubernetes recipe could set
`cluster: prod-eks` and reference `{cluster}` in any stage.

See [`examples/deployer/README.md`](examples/deployer/README.md) for
recipe-by-recipe details.

## Operational rules

- Never `--no-verify` on git commit — release-tagger fails the run if hooks fail.
- Hard cap on agent decisions per run is configurable; default 50 to keep
  the decision log scannable.
- Status writes are throttled to ≤1/s per agent to avoid blob churn —
  terminal states (success/failure/blocked/cancelled) are always flushed.
- Cron expressions auto-translate to systemd OnCalendar; complex Quartz
  extensions (`L`, `W`, `?`, `#`) aren't supported — write the timer by hand
  if you need them.

## Contributing

This codebase is shared across several of my own repos but designed to be
fork-friendly. Open issues / PRs at https://github.com/voidsstr/reusable-agents.

If you build an interesting agent on top of it, I'd love to see it.

## License

MIT — see [LICENSE](LICENSE).
