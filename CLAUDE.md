# Claude Instructions — reusable-agents framework

## What this repo is

A self-hostable framework for running scheduled / triggered LLM agents.
Agents register from their own home repos (nsc-assistant, specpicks,
customer apps, anywhere) and inherit lifecycle, scheduling, status,
inter-agent messaging, confirmations, and a control UI from this
codebase.

Source of truth for the architecture + standards is [`README.md`](README.md).
Read it before designing an agent.

## When the user asks you to create a new agent (in ANY repo)

**Always start from a blueprint.** Pick the closest one and use the
scaffold script — don't hand-roll agent files. The blueprint + scaffold
enforce conventions (kebab-case ids, manifest schema, AGENT.md section
structure, AgentBase lifecycle, capabilities declared, status/decision
protocol, confirmation gates).

### Pick a blueprint

| User's intent | Blueprint | Reference impl |
|---|---|---|
| "audit my site for issues / send me recommendations / put fixes on auto-pilot" | `site-quality-recommender` | `progressive-improvement-agent` |
| "compare my site to competitors / find feature gaps" | `site-quality-recommender` | `competitor-research-agent` |
| "one step in a pipeline (collect / analyze / report / ship)" | `pipeline-stage` | `seo-data-collector`, `seo-analyzer` |
| "poll an inbox / route replies" | `inbox-poller` | `responder-agent` |
| "apply approved recs as code edits" | `llm-code-editor` | `implementer` |
| "just run a script on a cron" | `scheduled-task` | most agents in `nsc-assistant/agents/` |

Each blueprint's `BLUEPRINT.md` has anti-patterns + integration points.
Read it before scaffolding.

### Step-by-step

1. **Confirm the agent's home repo** with the user. The agent dir lives
   inside that repo (typically under `<repo>/agents/<id>/`). If the
   repo doesn't have an `agents/` dir yet, the script creates it.

2. **Confirm core fields**:
   - `id` (kebab-case, ≤63 chars, starts with a letter)
   - One-line description
   - Category (`seo`, `research`, `fleet`, `personal`, `ops`, `misc`)
   - Schedule (cron expression — empty for manual-only agents)
   - Owner email (gets confirmation requests for dangerous actions)
   - Kind: `python` (subclass `AgentBase`) or `bash` (entry script only)

3. **Run the scaffold**:
   ```bash
   bash /home/voidsstr/development/reusable-agents/install/create-agent.sh \
       <agent-id> <repo>/agents \
       --name "<Display Name>" \
       --description "<one line>" \
       --category <category> \
       --cron "<cron-expr>" \
       --timezone "<tz>" \
       --owner "<email>" \
       --kind <python|bash>
   ```
   The script copies `_template/agent/*` into the new dir, substitutes
   placeholders, and prints next steps.

4. **Edit the generated files** to fill in the agent's actual work:
   - `AGENT.md` — flesh out *What this agent does*, *Per-run flow*, etc.
   - `agent.py` (Python) — implement `run()`. Use `self.status`,
     `self.decide`, and the `@requires_confirmation` decorator on any
     dangerous method.
   - `run.sh` (bash) — implement the work between the marker comments.
   - `manifest.json` — only edit if user wants different cron / category
     than what was passed to the scaffold.

5. **Declare goals** (REQUIRED). Every agent in the framework MUST
   declare 3-7 long-running goals it incrementally advances each run.
   Goals stick around forever; the dashboard shows them in the Goals
   tab with progress bars and a separate Accomplished section. Each
   goal has:
   - `id` (kebab-case, stable, never reused)
   - `title` + `description`
   - `metric: {name, current, target, direction, unit}` (optional but
     strongly encouraged — drives the progress bars)
   - `directives: list[str]` (what the agent should DO each run to
     advance this goal — read by the agent's LLM at run start to bias
     analysis)

   Add the seed call to `install/seed-default-goals.sh` (one section per
   agent) so re-runs don't wipe progress, OR PUT directly to
   `/api/agents/<id>/goals` after registration. Schema:
   `shared/schemas/agent-goals.schema.json`. The `run()` should call
   `record_goal_progress(agent_id, goal_id, value, run_ts=...)` at the
   end to update metrics + push progress_history.

   Without goals the agent's purpose isn't legible to the human reviewing
   the dashboard. Don't ship without them.

6. **Register with the framework**:
   ```bash
   FRAMEWORK_API_URL=http://localhost:8093 \
       bash <repo>/agents/register-with-framework.sh
   # Or use the framework's universal walker:
   FRAMEWORK_API_URL=http://localhost:8093 \
       bash /home/voidsstr/development/reusable-agents/install/register-all-from-dir.sh <repo>/agents
   ```
   Idempotent — registration upserts an existing record + re-applies
   the systemd timer. (Use port 8093 because 8090 is held by
   application-research on this dev box.)

7. **Verify**:
   - UI: http://localhost:8091/agents/<agent-id>
   - API: `curl http://localhost:8090/api/agents/<agent-id>`
   - systemd: `systemctl --user list-timers | grep agent-<agent-id>`
   - Goals tab shows the declared objectives with progress bars

8. **Commit** the new agent dir to its home repo.

### Repos in this ecosystem

| Repo | Where agents live | How to register |
|---|---|---|
| `nsc-assistant` | `agents/<id>/` | `bash scripts/register-agents.sh` |
| `specpicks` | `agents/<id>/` | `bash agents/register-with-framework.sh` |
| Customer apps inside nsc-assistant | `Customer Applications/<app>/<ver>/agents/<id>/` (when domain-specific) OR `nsc-assistant/agents/<id>/` (when shared) | nsc-assistant's register-agents.sh |

### When NOT to use the scaffold

- The user explicitly asks for a one-off / throwaway script that doesn't
  need scheduling, status, or registration. Then just write a normal
  bash/python script.
- The user is editing an existing agent — go straight to the AGENT.md
  + agent.py + manifest.json files; re-register after.

### Anti-patterns to avoid

- ❌ Hand-creating `agents/<id>/manifest.json` without using the scaffold
  (you'll miss conventions / metadata fields).
- ❌ Putting agent definitions in `~/.openclaw/...` or `/tmp/...`. Agents
  belong in their home repo, under version control.
- ❌ Hardcoding production credentials in the agent dir. Secrets live in
  the host's OS env, in `~/.<vendor>/` config files (mode 0600), or in
  Azure Key Vault references — *never* in the manifest or runbook.
- ❌ Skipping `@requires_confirmation` on production-affecting methods.
  Add the decorator, declare it in `capabilities` with
  `confirmation_required=True`, let the framework gate it.
- ❌ Calling out to the legacy `agents.lib.agent_recorder` (HTTP
  recorder living in nsc-assistant). New agents use AgentBase, which
  writes status + runs to the framework's storage backend directly.

## When the user asks to edit an existing agent's behavior

1. Find the agent's home repo (check the registry: `curl
   http://localhost:8090/api/agents/<id>`; the `repo_dir` field tells
   you where it lives).
2. Edit `AGENT.md` (runbook) and/or `agent.py` (code) in that repo.
3. Re-register if `manifest.json` changed (cron, owner, category, etc.):
   ```bash
   bash <home-repo>/agents/register-with-framework.sh
   ```
4. Manifest changes that affect scheduling auto-update the systemd
   timer.
5. Trigger a fresh run if needed:
   ```bash
   curl -X POST http://localhost:8090/api/agents/<id>/trigger \
        -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"
   ```

## When the user asks to inspect / debug an agent

- `curl http://localhost:8090/api/agents/<id>` — full detail
- `curl http://localhost:8090/api/agents/<id>/runs?limit=20` — history
- `curl http://localhost:8090/api/agents/<id>/status` — live status
- `journalctl --user -u agent-<id>.service` — systemd run logs
- UI: http://localhost:8091/agents/<id> → Storage tab — browse the
  agent's blob hierarchy directly

## Framework architecture cheat sheet

Python core under `framework/core/`:

| Module | Role |
|---|---|
| `agent_base.py` | `AgentBase` class + `RunResult` + lifecycle |
| `storage.py` | Pluggable backend (Azure Blob default, Local FS for tests) |
| `registry.py` | Master agent list at `registry/agents.json` |
| `status.py` | Live status writes + global event log |
| `messaging.py` | Inter-agent async messages |
| `confirmations.py` | `@requires_confirmation` decorator |
| `decision_log.py` | Per-run jsonl decision log |
| `context_index.py` | Date-indexed run summaries (caps next-run context budget) |
| `scheduler.py` | systemd `--user` timer/service writer |
| `release_tagger.py` | git commit + tag agent/<id>/release/<run-ts> + push |
| `email_codes.py` | Subject-tag encode/decode for routing replies |
| `guardrails.py` | `Capability` declaration dataclass |
| `mailer.py` | Outbound mailer interface (LogMailer + Graph + SMTP impls) |

API service: `framework/api/app/main.py` (FastAPI). 35 routes + 2 WS
streams. Token auth via `FRAMEWORK_API_TOKEN`.

UI: `framework/ui/src/`. React + Vite + Tailwind. Iframe-friendly.

Templates: `_template/agent/` is what `create-agent.sh` clones from.

Tests: `framework/tests/` — 20 pytest cases against LocalFilesystemStorage.

## Deploying the agent dashboard to Azure — REQUIRED after every change

**Every change to `framework/api/` or `framework/ui/` MUST be deployed to
Azure as part of the same task.** Local docker compose updates only the
dev box at http://localhost:8091; the production dashboard at the
Container App FQDN keeps serving the previous build until you push.

Deploy command (from the repo root):

```bash
cd /home/voidsstr/development/reusable-agents
bash install/deploy-azure.sh
```

The script builds + pushes both images to `nscappsacr` ACR and updates
the `agents` Container App in the `nsc-apps` resource group. Default
tag is a UTC timestamp; pass an explicit tag if you need to roll back
to a specific build (`bash install/deploy-azure.sh 20260428-1620`).

When this is required:
- `framework/api/**` (FastAPI routes, lifespan, host-worker, core/*)
- `framework/ui/**` (React components, pages, styling)
- `framework/core/**` (shared modules the API imports — agent_base,
  status, ghost_reaper, llm_stream, storage, decision_log, etc.)

When this is NOT required:
- Agent code under `agents/<id>/` (host-worker re-execs each run from
  the host filesystem, so a `git pull` on the host is enough)
- Docs (`*.md`), tests, blueprints
- `_template/`, `install/` (shell scripts run on the host)

After deploying, verify the new build:

```bash
# FQDN comes from the script's final "Deploy complete" line, or:
az containerapp show -g nsc-apps -n agents --query properties.configuration.ingress.fqdn -o tsv

# Confirm it's serving the new tag
curl -sI https://<fqdn>/ | head -3
```

If the user reports "the dashboard doesn't show my change," your first
check should be: did this task include a deploy-azure.sh run?

## Hosting note

This codebase is shared. When working in nsc-assistant, specpicks, or
customer apps, **don't refactor the framework itself** unless the user
explicitly asks. Submit framework changes as PRs to the
reusable-agents repo and pull them via `git pull` in the framework
checkout.
