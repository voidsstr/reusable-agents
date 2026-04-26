# reusable-agents

> A self-hostable framework for running LLM-driven agents with shared
> memory, scheduled execution, human-in-the-loop confirmations, and a
> control dashboard. Agents register with a local instance from their
> own repos, get auto-scheduled via systemd, and write all state to
> Azure Blob Storage so they get smarter over time.

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
├─ install/
│  ├─ register-agent.sh        POSTs one manifest.json to the framework
│  ├─ register-all-from-dir.sh Walks a dir and registers every manifest.json
│  └─ install-host-worker.sh   Sets up the host-worker systemd unit
├─ docker-compose.yml          API + UI services
├─ .env.example                Operator config template
└─ examples/sites/*.yaml       Per-site SEO config templates (legacy use)
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

## Storage layout

The framework writes everything to a single Azure Blob container (default
name `agents`). Hierarchical via key prefixes:

```
registry/
  agents.json                          # master agent list
  events.jsonl                         # framework event log

agents/<agent-id>/
  manifest.json                        # canonical manifest
  status.json                          # live status (UI reads)
  state/latest.json                    # carried-forward state
  state/history/<run-ts>.json
  goals/current.json
  goals/history/<run-ts>.json
  runs/<run-ts>/
    progress.json                      # success criteria, metrics
    errors.json                        # exceptions + tracebacks
    decisions.jsonl                    # streaming decision log
    context-summary.md                 # narrative for next run
    recommendations.json               # SEO-style recs (where applicable)
    responses.json                     # parsed user replies
    deploy.json                        # deployer artifacts
  context-summaries/<YYYY-MM-DD>.md    # daily rollups (smart cap on next-run context)
  changelog.jsonl                      # release tags + commits
  outbound-emails/<request-id>.json    # routing for replies
  responses-queue/<request-id>.json    # parsed user replies awaiting pickup
  confirmations/<request-id>.json      # pending dangerous-action approvals

shared/
  messages/<message-id>.json           # inter-agent async messages
  inboxes/<agent-id>/<message-id>      # zero-byte markers for fast inbox listing
```

Why blob keys instead of e.g. Storage Queues for messages:
- Indexable by date — agents pull a bounded window each run
- Auditable — humans can read everything in the portal
- No queue retention limits (Azure Queues cap at 7d)

## Authoring an agent

### Subclass `AgentBase` (recommended for new agents)

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
