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

**Always use the scaffold script.** Don't hand-roll agent files — the
scaffold enforces the framework's conventions (kebab-case ids, manifest
schema, AGENT.md section structure, AgentBase lifecycle, capabilities
declared, status/decision protocol, confirmation gates).

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

5. **Register with the framework**:
   ```bash
   # If the repo has its own register-with-framework.sh:
   bash <repo>/agents/register-with-framework.sh
   # Or use the framework's universal walker:
   bash /home/voidsstr/development/reusable-agents/install/register-all-from-dir.sh <repo>/agents
   ```
   Idempotent — registration upserts an existing record + re-applies
   the systemd timer.

6. **Verify**:
   - UI: http://localhost:8091/agents/<agent-id>
   - API: `curl http://localhost:8090/api/agents/<agent-id>`
   - systemd: `systemctl --user list-timers | grep agent-<agent-id>`

7. **Commit** the new agent dir to its home repo.

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

## Hosting note

This codebase is shared. When working in nsc-assistant, specpicks, or
customer apps, **don't refactor the framework itself** unless the user
explicitly asks. Submit framework changes as PRs to the
reusable-agents repo and pull them via `git pull` in the framework
checkout.
