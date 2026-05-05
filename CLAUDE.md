# Claude Instructions — reusable-agents framework

> ## 🎯 NORTH STAR — READ EVERY SESSION 🎯
>
> **Everything we build here exists to drive user usage of the
> configured websites.** Today that's aisleprompt.com and
> specpicks.com; new sites slot in via per-site config. We measure
> success by site-side metrics — DAU, organic clicks, indexed
> pages, conversions, returning users — *not* by code shipped, runs
> completed, or LLM tokens consumed.
>
> **Goals are the prioritization lens, not decoration.** Every agent
> declares 3–7 long-running goals at registration. Each goal has a
> `target_metric` that points at a key in `RunResult.metrics` so the
> framework auto-records progress on every run. The Goals tab in the
> dashboard is the single pane of glass for "is this agent moving the
> needle?"
>
> **The decision procedure for any new feature, refactor, or bugfix:**
>
> 1. Open the Goals tab (or `GET /api/agents/<id>/goals` and
>    `/api/agents/<id>/goals/cache`).
> 2. Find a stalled goal — target gap large, recent progress flat.
>    Bias toward goals that map directly to user-facing site metrics
>    (organic clicks, DAU, conversions, indexed pages).
> 3. Trace that goal back to the agent that owns its `target_metric`.
>    Read its `run()` and find the bottleneck — under-batching,
>    missing handoff, low cap, no LLM short-circuit, etc.
> 4. Make the smallest change that moves the metric. Verify the
>    metric actually moved on the next 1–3 runs before declaring it
>    done. **A change that doesn't tick a goal didn't happen.**
>
> **Anti-patterns to refuse on sight:**
>
> - "Refactor for cleanliness" without a specific goal it unblocks.
> - "Add a feature the user might want" without a metric it serves.
> - "Wire a new agent" before declaring goals + `target_metric`
>   bindings. (No goals → no legibility → won't survive review.)
> - "Fix a bug" by adding an `if site == "x": …` branch in framework
>   code. Lift to config; see the Framework-First Policy below.
>
> See **[`README.md` → "Goals & metrics — the north star"](README.md)**
> for the goals pipeline architecture, layer A vs layer B metric
> capture, and how `init_goals` merges work.

> ## ⚠️ AGENTBASE IS MANDATORY — READ EVERY SESSION ⚠️
>
> **Every registered agent MUST subclass
> `framework.core.agent_base.AgentBase`.** Bash-driven multi-stage
> pipelines, scripts that write `status.json` directly via
> `framework.cli.status`, and ad-hoc shapes (synthesizing run
> summaries from work-product files instead of `progress.json`) are
> forbidden.
>
> **Why this matters:** non-AgentBase agents create silent
> inconsistencies — runs invisible in the dashboard's Runs tab, no
> `run-index.json` for fast list endpoints, no per-run heartbeat,
> no auto goal-progress tracking, no auto-generated verification
> scripts. Past workarounds (e.g. `framework.cli.status` writing a
> synthetic `progress.json` on terminal states; `_list_runs_legacy`
> reading both `progress.json` and `recommendations.json`) were
> retired on 2026-05-04 once every active SEO agent moved to
> AgentBase. Don't reintroduce them.
>
> **Decision tree (apply to EVERY new agent or refactor):**
>
> 1. **"Is this work an agent?"** If it has a cron schedule, run
>    history, status, or queueable triggers → AgentBase. If it's a
>    one-shot CLI tool (e.g. `agent/tools/wake_on_lan.py`) → not an
>    agent, no manifest, doesn't go in `agents/`.
> 2. **"Is it multi-stage?"** Stages are phases inside one agent's
>    `run()` method, NOT separate registered agents. They share one
>    `run_ts`, one `progress.json`, one `run-index` entry. If the
>    cross-stage state is too coarse for one agent, lift the contract
>    into `framework/core/` as a primitive (see
>    `implementation_queue.py`, `digest_queue.py`,
>    `outbound_email.py` for examples).
> 3. **"Does my entry_command invoke `bash`?"** Only acceptable if
>    the bash is a thin env-setup wrapper that ends in
>    `exec python3 .../agent.py` (an AgentBase entrypoint). Bash
>    that orchestrates pipeline stages, writes status, or shells out
>    to multiple python scripts is the anti-pattern this rule bans.
> 4. **"Am I about to write `if has_progress_json else
>    synthesize_from_other_files`?"** STOP. The producer should
>    write `progress.json` via `AgentBase.post_run()` — fix the
>    producer, don't add a synthesis branch in the consumer.
>
> **Reference implementation — collapsed pipeline pattern.** The SEO
> opportunity agent (`agents/seo-opportunity-agent/`) is the canonical
> example of converting a bash-orchestrated pipeline to AgentBase.
> Three former agents (`seo-data-collector`, `seo-analyzer`,
> `seo-reporter`) and a per-site `run.sh` collapsed into one
> `AgentBase` subclass with three internal phases (collect → analyze
> → finalize). Their script bodies became internal modules under
> `agents/seo-opportunity-agent/lib/{collector,analyzer,reporter}/`
> (still subprocess-called by `_run_phase()` to avoid rewriting 7,500
> LOC, but no longer registered as separate agents). Read
> `agents/seo-opportunity-agent/agent.py` + `finalizer.py` before
> refactoring any other bash pipeline.
>
> **Cross-agent contracts live in `framework/core/`.** When agent A's
> output flows to agent B, the file format / queue location belongs
> in a framework primitive, not inline JSON-shape conventions:
>
> - `framework/core/implementation_queue.py` — `queue_recs()` writes
>   `agents/responder-agent/auto-queue/<request-id>.json`
> - `framework/core/digest_queue.py` — `queue()` writes
>   `digest-queue/<ts>-<hash>.json` for the digest rollup
> - `framework/core/outbound_email.py` — `record()` writes
>   `agents/<id>/outbound-emails/<request-id>.json` for the
>   Confirmations page
> - `framework/core/handoff.py` — typed inter-agent handoffs
> - `framework/core/short_circuit.py` — snapshot hashing + replay
>
> AgentBase exposes shorthand wrappers: `self.queue_recs(...)`,
> `self.queue_for_digest(...)`, `self.record_outbound(...)`. Use
> these. If you find yourself writing `self.storage.write_json(
> "agents/responder-agent/auto-queue/...", ...)` inline, you're
> bypassing the contract — call the wrapper.
>
> **Conversion backlog (must convert when next touched).** As of
> 2026-05-04 the following agents still drive work outside AgentBase
> and need to be lifted on the next change. (`responder-agent`,
> `implementer`, `seo-deployer` were converted on 2026-05-04 in the
> same session as the SEO collapse — they're AgentBase wrappers
> around their existing scripts/run.sh now. The implementer's
> `run.sh` is still the heavy-lifter inside; rewriting it is its
> own follow-up.)
>
> - `gsc-coverage-auditor` (and per-site `aisleprompt-` /
>   `specpicks-` instances)
> - `daily-briefing-calendar-agent`, `daily-status-briefing`,
>   `fix-submission-agent`, `retro-agent-orchestrator`
> - `indexnow-submitter` (and per-site instances), `aisleprompt-indexnow-bulk`,
>   `specpicks-indexnow-bulk`
> - `external-game-cataloger`, `game-library-scanner`,
>   `market-research-pipeline`, `real-estate-agent`,
>   `retro-multiplayer-refresh`, `security-scanner-pipeline`
> - `web-search`, `product-hydration-agent`, `scraper-watchdog`
>
> Pick one of these only when its functionality is being changed —
> don't open a side-quest. The framework primitives needed already
> exist; if you find a gap, add the primitive to `framework/core/`
> first, never inline.

> ## ⚠️ FRAMEWORK-FIRST POLICY — READ EVERY SESSION ⚠️
>
> **Every change MUST be evaluated for framework abstraction BEFORE
> writing the first line of code.** Ask:
>
> 1. **"Could another site or agent ever want this?"** If yes → it goes
>    in `framework/core/` or `framework/cli/` and is **configurable per
>    deployment**.
> 2. **"Is the LOGIC site-specific, or only the VALUES?"** Logic →
>    framework with knobs. Values (DSNs, brand voice, slugs) → site repo
>    or per-deployment storage config.
> 3. **"If I deploy this framework somewhere else tomorrow, would they
>    have to fork my changes to get value?"** If yes — STOP, refactor as
>    framework primitive + config now, NOT later.
>
> **Required pattern: primitive + config + extension point.** Every new
> framework feature ships as:
>
> - A reusable primitive in `framework/core/<name>.py` with no
>   site-specific assumptions
> - A storage-backed config (`config/<name>-config.json`) so each
>   deployment can override behavior without code changes
> - A manifest field (added to `framework/core/registry.py`
>   `AgentManifest`) when per-agent override matters
> - A CLI entry point in `framework/cli/<name>.py` when shell-side
>   callers (host-worker, scripts) need it
> - **Default config that ships sensible behavior out of the box** so
>   first-time users don't need to configure anything to get going
>
> **Anti-patterns that MUST be refactored on sight:**
>
> - `if site == "specpicks": ...` in framework code — extract to config
> - Hardcoded agent-id lists in `host-worker.sh`, `responder.py`,
>   anywhere in `framework/` — move to a storage config
> - Duplicate logic in two site-agent `agent.py` files — lift to
>   `framework/core/` and have both call into it
> - Site-specific tier/priority/threshold constants — config-driven
>   per deployment
> - "I'll abstract it later" — there is no later
>
> **Reference implementations of this pattern:**
>
> - [`framework/core/priority.py`](framework/core/priority.py) +
>   [`framework/cli/priority.py`](framework/cli/priority.py) +
>   `config/priority-config.json` — agent queue priority tiers
> - [`framework/core/short_circuit.py`](framework/core/short_circuit.py) —
>   skip-LLM-when-inputs-unchanged primitives
> - [`framework/core/work_types.py`](framework/core/work_types.py) +
>   per-site `handoff_routes` in `site.yaml` — rec routing
> - [`framework/core/article_mentions.py`](framework/core/article_mentions.py) —
>   article-product mention extraction (used by any site)
>
> **When in doubt, build it in the framework.** Cost of unnecessary
> abstraction: small (one extra file). Cost of duplicated site logic:
> drift, fan-out bugs, broken priority across sites.
>
> **The contract with every site (aisleprompt, specpicks, future sites):**
> sites are *config consumers*, never *code-fork targets*. A site repo
> contributes a `manifest.json` + `site.yaml` + (optional) per-site
> `prompts/*.md`. It does NOT contribute logic, branching, or
> "site-specific overrides" embedded in framework code. If a feature
> can't be expressed through (a) a storage config the framework reads,
> (b) a manifest field, or (c) a `site.yaml` knob — STOP and add the
> missing extension point to the framework before writing the feature.
> This applies to EVERY change, not just obviously-shared ones — even
> a fix for one site goes through the same gate. No site has special
> status; aisleprompt and specpicks are reference deployments, not
> privileged.

## What this repo is

A self-hostable framework for running scheduled / triggered LLM agents.
Agents register from their own home repos (nsc-assistant, specpicks,
customer apps, anywhere) and inherit lifecycle, scheduling, status,
inter-agent messaging, confirmations, and a control UI from this
codebase.

**Documentation:**

- [`README.md`](README.md) — overview + quickstart + manifest format
- [`docs/architecture.md`](docs/architecture.md) — three-tier model
  (customer repos → framework → blob storage), agent lifecycle,
  storage layout, dispatch graph, end-to-end trace of one rec
- [`docs/agents-catalog.md`](docs/agents-catalog.md) — every agent across
  every repo, categorized, with code path + manifest path + schedule
- [`docs/repo-boundaries.md`](docs/repo-boundaries.md) — **read this
  before adding code anywhere**: what goes in framework vs customer
  repo vs `~/.reusable-agents/`. Decision tree + anti-patterns
- [`framework/core/handoff.py`](framework/core/handoff.py) +
  [`framework/core/work_types.py`](framework/core/work_types.py) —
  inter-agent handoff protocol. When agent A finds work that belongs
  to agent B, A calls `send_handoff()` and the framework routes it.
  See `docs/architecture.md` § "Inter-agent handoffs" for the full
  flow. **Whenever you add a new rec_type, also add it to
  `DEFAULT_REC_ROUTING` in work_types.py.**
- [`docs/seo-onboard-new-site.md`](docs/seo-onboard-new-site.md) —
  add a new site to SEO automation in 5 steps
- [`blueprints/README.md`](blueprints/README.md) — pick the right
  blueprint when scaffolding a new agent

Read `architecture.md` once. Re-read `repo-boundaries.md` every time
you wonder "should this code live here or in the customer repo?"

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

## When the user asks to edit a per-site `site.yaml` (SEO + revenue agents)

Per-site SEO instances (`specpicks-seo-opportunity-agent`,
`aisleprompt-seo-opportunity-agent`, future sites) all share **one
JSON schema**: `shared/schemas/site-config.schema.json`. Every block
that has `additionalProperties: false` rejects unknown keys at startup.
The agent calls `load_config_from_env()` first thing and **exits
status 1 within ~1 second** when validation fails — silently in
systemd-journal terms (only "Main process exited, code=exited,
status=1/FAILURE" shows up).

**Failure signature you've seen before (2026-05-04):** commit added
`articles.url_template` to `specpicks/agents/seo-opportunity-agent/site.yaml`
without updating the schema. Both SEO agents (specpicks + aisleprompt)
broke for ~2 hours until the schema was extended. The error only
appears when running the entry command manually:

```
Config validation failed for .../site.yaml:
  Additional properties are not allowed ('url_template' was unexpected)
  at: articles
```

**Hard rule when adding any `site.yaml` field:**

1. **Add it to the schema first.** Open
   `shared/schemas/site-config.schema.json`, find the right block
   (`articles`, `data_sources`, `analyzer`, `reporter`, `implementer`,
   `deployer`, `coverage_targets`, `page_inventory`, etc.), and add
   the property under `properties` with a real `description`.
2. **Pre-commit-test by running the agent locally** before pushing:
   ```bash
   AGENT_ID=specpicks-seo-opportunity-agent \
   SEO_AGENT_CONFIG=/home/voidsstr/development/specpicks/agents/seo-opportunity-agent/site.yaml \
   DATABASE_URL='postgresql://...' \
   PYTHONPATH=/home/voidsstr/development/reusable-agents \
   python3 /home/voidsstr/development/reusable-agents/agents/seo-opportunity-agent/agent.py
   ```
   Validation errors print to stderr in the first second of output.
3. **Update both per-site `site.yaml` files at once** if the field is
   shared semantics — schema changes apply to every site instance.
4. **Update `agents/seo-opportunity-agent/README.md`** if the field
   adds a user-visible capability (link it in the
   "Configuration → most-used optional blocks" table).
5. **Re-register the manifest** only if the manifest itself changed
   (`bash <site-repo>/agents/register-with-framework.sh`). Editing
   `site.yaml` alone doesn't require re-registration — the next cron
   tick or manual trigger picks it up.

**Cron scheduling discipline.** The two SEO instances run on a
2-hour cycle with a `:15` offset between them
(`specpicks-seo-opportunity-agent: 0 */2 * * *`,
`aisleprompt-seo-opportunity-agent: 15 */2 * * *`) so they don't
race for the same LLM provider quota. **Don't put a third site at
:00 or :15.** Pick :30 or :45 when adding a new site.

**Documentation map (where to put what):**

| Change | Update |
|---|---|
| New phase rule in collector / analyzer / reporter | The phase's `lib/<phase>/README.md` + the engine's [`agents/seo-opportunity-agent/README.md`](agents/seo-opportunity-agent/README.md) (rec-type catalog) |
| New `site.yaml` field | The schema + the engine README + (if onboarding-relevant) `docs/seo-onboard-new-site.md` |
| New rec type | The rec-type catalog table in the engine README |
| New troubleshooting failure mode | Engine README's Troubleshooting section + this CLAUDE.md if it's a recurring class of mistake |
| Architecture-level change to the pipeline | `docs/agents-catalog.md` SEO section + the engine README |

**Per-site app-deploy recipes** (Azure Container Apps, Vercel,
Cloudflare Workers, Netlify) are documented in the
[`agents/seo-deployer/README.md`](agents/seo-deployer/README.md)
example sections — copy from there into a new site's `site.yaml`
under `deployer:`.

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
| `handoff.py` + `work_types.py` | Inter-agent handoff routing |
| `resilience.py` | `safe_run`, `with_retry`, `notify_operator`, `invoke_doctor` |
| `short_circuit.py` | Skip-LLM-when-inputs-unchanged primitives — `signal_hash`, `snapshot_hash`, `should_skip`, `partition_by_hash`, `merge_findings_cache`. **Any agent that fires on a cron and only sometimes has new work to do MUST use these** rather than re-rolling its own hashing logic. |
| `ai_providers.py` | Provider/model registry — agents call `ai_client_for(agent_id)` and the framework resolves via overrides + defaults |

## LLM efficiency — required patterns for every agent

The framework provides primitives that EVERY new agent should use rather
than reinventing. Failing to use them costs tokens AND introduces drift:

1. **`framework.core.short_circuit.signal_hash` / `snapshot_hash`** —
   hash an agent's inputs (excludes timestamps, rounds floats). Use
   `should_skip(self.state, "last_signals_hash", sig)` at the top of
   `run()` to skip the LLM call when nothing has changed since the last
   successful run. Persist the hash to `next_state["last_signals_hash"]`.

2. **`framework.core.short_circuit.partition_by_hash`** — for
   per-page / per-product / per-row LLM analysis. Skips items whose
   hash matches the prior run + replays prior findings via
   `merge_findings_cache`.

3. **`framework.core.resilience.safe_run`** — wrap any external call
   (LLM, HTTP, DB) so the framework handles retries + operator-alert on
   final failure. Don't roll your own try/except retry loops.

4. **`framework.core.handoff.send_handoff`** — when your agent finds
   work that belongs to a specialist (article generation, code edit, …),
   send a handoff. Don't try to do work outside your role.

5. **Always batch LLM calls when iterating over N items.** A single
   structured-output prompt with N items is dramatically cheaper than N
   individual prompts. See `competitor-research-agent._extract_features_batched`
   for the canonical pattern.

**Rule of thumb:** if you're about to add a `for item in items: client.chat(...)`
loop, stop and ask whether you can do it in one batched call instead.

## LLM provider routing — chat vs code-editor

The framework has TWO independent LLM systems. Don't conflate them:

1. **Chat** — `framework.core.ai_providers`. Used by anything that
   calls `self.ai_client()` or `chat_with_fallback(...)`. Provider
   kinds: `copilot` (GitHub Copilot proxy, subscription billing — the
   default), `claude-cli` (Claude Max session), `anthropic`/`openai`/
   `azure_openai` (per-token API), `ollama` (free local).
   Defaults + per-agent overrides live at
   `config/ai-defaults.json` in storage; edit via the dashboard
   `/providers` page or `POST /api/providers/defaults/{set,agent-override}`.
   `chat_with_fallback` auto-walks `('copilot', 'azure_openai',
   'openai', 'anthropic', 'ollama')` on rate-limit/timeout/quota
   errors.

2. **Code editor** — `framework.core.code_editor`. Used by the
   implementer + any `llm-code-editor` blueprint. A chain of editor
   binaries (`aider`, `opencode`, `crush`, `codex`, `plandex`) each
   paired with a model (claude-sonnet-4.6 via copilot proxy is top of
   chain; gpt-4.1-mini via Azure is fallback). Configured at
   `config/code-editor-config.json` in storage. The chain runs *after*
   any agent-specific editor logic (e.g. the implementer's claude-pool
   path).

**When the user says "switch from claude to copilot/aider":**
- Chat agents → change the provider in
  `config/ai-defaults.json` (global default or per-agent override).
- Implementer → set `IMPLEMENTER_FORCE_FALLBACK=1` in its env, or
  `IMPLEMENTER_LLM=framework`, to skip the claude-pool path and go
  straight to the framework code-editor chain (which uses aider).

**Don't shell out to `claude` / `aider` / `gh copilot` directly** from
new agent code — both systems above already wrap those binaries with
live LLM stream capture, usage tracking (`config/llm-usage-*.jsonl`),
fallback chains, and dashboard visibility. Direct shell invocations
bypass all of that and create yet another duplicated provider chain.

**Where the docs are:**
- `README.md` → "LLM provider chain — chat agents + code editor" (operator-facing tables of providers, backends, env knobs)
- `agents/implementer/AGENT.md` → "## LLM driver" (implementer's specific claude-pool → framework-chain path)
- This file → routing rules + when to switch what

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

## Per-site app deployments — pick a recipe, edit the site.yaml

When an agent commits **application code** (not just data), the
framework's deployer chains into a 5-stage pipeline so the change
ships without manual intervention:

```
test → build → push → deploy → smoke_check
```

Every stage is a shell command template. The deployer is cloud-agnostic
— whatever `bash` can do, this can deploy.

**To configure a site for deployment**:

1. Pick a recipe from `examples/deployer/`:
   - `azure-container-apps.yaml` — **active**, used by aisleprompt + specpicks
   - `azure-app-service.yaml` — Azure App Service (sample)
   - `azure-functions.yaml` — Azure Functions (sample)
   - `aws-ecs-fargate.yaml` — AWS ECS Fargate + ECR (sample)
   - `aws-lambda.yaml` — AWS Lambda container image + ECR (sample)
   - `aws-app-runner.yaml` — AWS App Runner + ECR (sample)
2. Drop the entire `deployer:` block from the recipe into the site's
   `site.yaml` (under the same key).
3. Edit `deploy.vars:` for the site (app name, resource group, image
   URI, etc.). Every key here is expanded as `{key}` in any stage's
   `cmd:`.
4. Make sure the deployer host has the cloud CLI configured (az login
   for Azure, aws configure for AWS, etc.) and docker reachable.

**Where existing sites declare their deployer**:

| Site         | Path                                                                                  |
|--------------|---------------------------------------------------------------------------------------|
| aisleprompt  | `nsc-assistant/agents/aisleprompt-seo-opportunity-agent/site.yaml`                   |
| specpicks    | `specpicks/agents/seo-opportunity-agent/site.yaml`                                   |

**When the deployer SKIPS itself** (intentional):

| Dispatch kind     | Behavior  | Why |
|-------------------|-----------|-----|
| `article-author`  | skip      | articles ship via DB INSERT — no docker build needed |
| `catalog-audit`   | skip      | DB-only fixes |
| `h2h`             | skip      | DB-only |
| `IMPLEMENTER_SKIP_DEPLOY=1`  | skip | env override (manual rollbacks, debug runs) |
| anything else     | run       | the per-batch deployer fires |

**To add a new cloud target**:

1. Copy any of the dormant recipes in `examples/deployer/` as a starting
   point.
2. The framework's `agents/seo-deployer/deployer.py` does NOT need to
   change — it just runs whatever shell commands you give it. So to
   add Kubernetes, GCP Cloud Run, Cloudflare Workers, etc., you just
   write the recipe.
3. Add it to the recipe table in `examples/deployer/README.md` and the
   table in this section.

**Don't hardcode cloud-specific logic in `deployer.py`** — every site's
target is config, not code. If you find yourself wanting a per-cloud
branch in the framework, that's a sign the recipe is missing a knob.
Add the knob to the recipe + README and keep the deployer dumb.

## Hosting note

This codebase is shared. When working in nsc-assistant, specpicks, or
customer apps, **don't refactor the framework itself** unless the user
explicitly asks. Submit framework changes as PRs to the
reusable-agents repo and pull them via `git pull` in the framework
checkout.
