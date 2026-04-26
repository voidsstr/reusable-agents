# Blueprints — reusable agent templates

A blueprint is a documented + scaffold-able pattern for a class of agents.
When you build a new agent, **start from a blueprint** so the result is
consistent with what's already in the repo and the framework.

## Why blueprints

The framework gives you `AgentBase`, storage, status, decisions, mailer,
ai_client, and the responder/email-confirmation flow. But *how* you wire
them together depends on what kind of agent you're building. A "scrape
+ recommend + email" agent needs different scaffolding than a "poll an
inbox + dispatch" agent or a "read upstream run dir + write downstream
run dir" agent.

Each blueprint here documents:

- **When to use it** — the problem shape
- **What you get** — files, dependencies, integration points
- **Anti-patterns** — what NOT to do
- **Reference implementations** — existing agents in this repo that
  follow the blueprint, so you can copy proven code

## Blueprint catalog

| Blueprint | Pattern | Reference impls | Schedule shape |
|---|---|---|---|
| [site-quality-recommender](site-quality-recommender/BLUEPRINT.md) | Crawl a site → LLM analysis → email ranked recs with rec-id reply syntax → optional auto-implement dispatch | `progressive-improvement-agent`, `competitor-research-agent` | daily |
| [pipeline-stage](pipeline-stage/BLUEPRINT.md) | Read upstream stage's run dir → do one thing → write downstream stage's expected outputs | `seo-data-collector`, `seo-analyzer`, `seo-reporter`, `seo-deployer` | event-driven (chained from cron) |
| [inbox-poller](inbox-poller/BLUEPRINT.md) | Poll IMAP every N min → parse subject tags / commands → dispatch to other agents' response queues | `responder-agent` | minutely |
| [llm-code-editor](llm-code-editor/BLUEPRINT.md) | Read recommendations → drive an LLM (or `claude` CLI) to apply edits → tests → commit + tag → optional deploy | `seo-implementer` | on-demand or chained |
| [scheduled-task](scheduled-task/BLUEPRINT.md) | Run a script on a cron schedule with framework lifecycle bookkeeping. The "default" blueprint when nothing else fits. | most agents in `nsc-assistant/agents/` | cron |

## Using a blueprint

The fastest path:

```bash
bash install/create-agent.sh <agent-id> <target-dir> \
    --blueprint <name> \
    --name "<Display Name>" \
    --description "..." \
    --category <seo|research|fleet|personal|ops|misc> \
    --cron "<cron-expr>" \
    --owner "<email>" \
    --kind <python|bash>
```

This copies the blueprint's `template/` files into `<target-dir>/<agent-id>/`,
substitutes placeholders, and prints next-step instructions. Without
`--blueprint` it falls back to the generic `_template/agent/` (which is
the same as the `scheduled-task` blueprint).

## Manually adapting a blueprint

If your agent is a hybrid of two blueprints (e.g., scheduled-task that
also polls an inbox), pick the closer one as the starting point and bring
in pieces from the other. Each blueprint's `BLUEPRINT.md` calls out the
files + integration points you'd need to add.

## Adding a new blueprint

If you're building an agent that doesn't fit any blueprint and the
pattern is likely to repeat:

1. Build the agent first. Don't pre-generalize.
2. After it's working, look at what was reused vs. what was specific.
   The reusable bits are blueprint material.
3. Create `blueprints/<new-blueprint>/` with:
   - `BLUEPRINT.md` — when to use, what you get, anti-patterns,
     reference impl
   - `template/` — cookiecutter-style files with `{{PLACEHOLDER}}`
     substitutions matching what `create-agent.sh` provides
4. Add a row to the catalog table in this README.
5. Update `install/create-agent.sh` to know about the new blueprint
   (just a case statement that copies from `blueprints/<name>/template/`
   instead of `_template/agent/`).

## Conventions every blueprint follows

These are framework-level expectations regardless of which blueprint you
pick:

1. **Subclass `framework.core.agent_base.AgentBase`** for Python agents.
   Bash agents that can't easily subclass should use
   `agents/lib/agent_recorder.py` to record runs.
2. **Use `self.ai_client()` for any LLM call.** Don't import provider
   SDKs directly — that defeats the framework's swappable-backend design.
3. **Declare capabilities** with `framework.core.guardrails.declare()`
   so the dashboard can show what an agent is allowed to do.
4. **Use `@requires_confirmation`** for anything that ships to prod /
   touches money / has irreversible side effects. The framework handles
   the email confirmation flow — don't reinvent it.
5. **Tag outbound emails** with `[<agent-id>:<request-id>]` via
   `framework.core.email_codes.encode_subject()` so the responder can
   route replies. The mailer (`self.mailer`) does this for you when set
   from the API.
6. **Write recommendations.json validated against a schema** in
   `shared/schemas/`. Pick an existing schema or add a new one — but
   schemas are mandatory.
7. **Per-site agent instances live in the customer repo, code lives
   here.** Customer repos register their site-specific manifests via
   `<repo>/agents/register-with-framework.sh`. The manifest's
   `entry_command` references the code in this repo.
