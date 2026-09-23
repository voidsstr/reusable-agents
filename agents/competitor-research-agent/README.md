# competitor-research-agent

This is the shared engine behind the per-site competitor-research agents.
For each configured site, it identifies competitors, extracts the discrete
features each competitor offers, and then recommends changes in these
categories:

1. **parity-feature**: features competitors have that we don't (one rec
   per distinct feature, however many competitors have it).
2. **competitive-advantage**: features we could build to differentiate
   (no `competitor` field).
3. **ux-improvement**: patterns competitors use to improve onboarding,
   conversion or retention.
4. The prompt also allows `content-gap`, `marketing-positioning`,
   `monetization`, `integrations` and `other`.

Every rec is a **full blueprint**, because the implementer ships straight
from it. Each one carries a user story, a blueprint (UI, backend, data
model, API, integrations, edge cases, rollout, complexity) and success
metrics.

> **Operational runbook:** [AGENT.md](AGENT.md) covers phases,
> inputs/outputs, config keys, goals, failure modes and the 2026-09-23
> health status. This README is the overview.

## Where it runs

The engine itself is never scheduled or registered (`metadata.is_blueprint:
true`). The registered, timer-driven instances are:

| Instance | Home | Schedule (America/Detroit) |
|---|---|---|
| `aisleprompt-competitor-research-agent` | `aisleprompt/agents/competitor-research-agent/` | `4 */5 * * *` |
| `specpicks-competitor-research-agent` | `specpicks/agents/competitor-research-agent/` | `30 6 * * *` |

Each instance's entry command is:

```bash
COMPETITOR_RESEARCH_CONFIG=/path/to/site.yaml \
  python3 /home/voidsstr/development/reusable-agents/agents/competitor-research-agent/agent.py
```

The systemd unit sets `AGENT_ID=<instance id>` in its environment, and
every artifact is keyed by that id. The artifacts land under
`agents/<instance-id>/runs/<UTC-ts>/` in framework storage, and in the
local `runs_root/<site>/<UTC-ts>/` directory (both instances use
`~/.reusable-agents/competitor-research-agent/runs`).

## Config

The schema is `shared/schemas/site-quality-config.schema.json`, the same
one `progressive-improvement-agent` uses. See `config.example.yaml`.

The `competitors:` block controls who we compare against:

```yaml
competitors:
  seed_domains: [mealime.com, anylist.com]   # bare domains
  max_competitors: 6
  max_pages_per_competitor: 4
```

If `seed_domains` is empty, the agent asks the LLM to brainstorm
competitors from `site.what_we_do` and saves the result in the run's
`competitors.json`.

> Until commit `7d760fd` (2026-09-23), seed normalisation used
> `str.lstrip("https://")`, which mangled `paprika.app` into `aprika.app`
> and `tomshardware.com` into `omshardware.com`. It is now a prefix regex.
>
> The same commit made the crawler treat apex and `www.` as one site; from
> 2026-09-18 almost no competitor pages had been crawled. It also made a
> run with zero crawled competitors return `blocked` instead of comparing
> against nothing. See AGENT.md → Failure modes.

Two keys guard against hallucinated "we don't have X" recs:

- **`current_state_inventory:`** is a top-level list of strings. It is the
  operator-curated ground truth that the prompt's EXISTING-FEATURE GATE
  trusts over the crawl.
- **`analyzer.max_open_proposals`** caps how many open proposals are
  emailed. The backlog itself keeps growing.

## Outputs

These are written per run:

```
competitors.json             # which sites we used + how we found them
features-ours.json           # extracted feature list for our site
features-theirs.json         # per-competitor feature lists
compare-raw.txt              # raw compare LLM output (debug silent 0-rec runs)
recommendations.json         # this run's new recs, validated, review_required=true
rec-id-to-proposal-id.json   # email rec-NNN → accumulator proposal_id
email-rendered.html
```

The cross-run backlog lives at `agents/<instance-id>/proposals/active.json`.
The email lists the **top open proposals from the whole backlog**, not just
this run's.

## Reply syntax

This is the same convention as the SEO and progressive-improvement agents.
`responder-agent` parses replies.

```
implement rec-001 rec-005
skip rec-002
defer rec-003
```

The accumulator maps `implement`/`ship` to `implemented`, `skip` to
`skipped` and `defer` to `deferred`, using `rec-id-to-proposal-id.json`
from the run that sent the email.

## Tier policy

`tier=auto` is **rare** for this agent. Adding a feature is rarely
mechanical, so `COMPARE_SYS` tells the LLM to reserve `auto` for narrow,
mechanical changes, to default to `review` for feature additions and to
`experimental` for "what if we built X" speculation. The final tier is
`score_tier(confidence, severity, threshold=analyzer.auto_implement_threshold)`;
the LLM's `tier_recommendation` is ignored except that `experimental`
always wins.

Things that DO qualify for `auto`:

- "Add JSON-LD product schema — competitors X and Y have it, we don't,
  schema content is fully derivable from our existing data."

  This example only counts when our page evidence really lacks the
  schema. The prompt now includes `JSONLD_TYPES` and the head tags for
  exactly that check.

Things that DON'T:

- "Add a meal-planner like Mealime's." → review (or experimental)
- "Build an AI nutrition coach." → experimental

## How recs reach the implementer

Three gates control this.

1. **`auto_implement`** (site.yaml) feeds `framework.core.dispatch.gated_dispatch_now`.
   Both instances set `false` as of 2026-09-23, so the run itself
   dispatches nothing.
2. **`review_required: true`** is stamped on every rec. `backlog-dispatcher-agent`
   skips these until `confirmed_for_implementation` is set, either through
   the dashboard Approve button or an `implement rec-NNN` email reply.
3. **The implementer path scope**: dispatches via `framework.core.dispatch`
   do not pass the instance `site.yaml`; `implementer/run.sh` loads
   `reusable-agents/examples/sites/<site>.yaml` instead (see AGENT.md →
   Outputs → Implementer config). The instance `implementer.*` block is
   not what gets applied.

**Before flipping `auto_implement: true`, read the code.**
`gated_dispatch_now` is passed **every** rec id from the run, not only the
`tier=auto` ones. `framework/core/dispatch.py` and the implementer do not
check `review_required`, so the backlog-dispatcher's review gate would not
protect those direct dispatches, and every blueprint, including
`experimental` ones, would go straight to the implementer. The older
"flip auto_implement and only tier=auto ships" playbook does not match the
current code. If auto-pilot is wanted, filter to `tier == "auto"` in
`run()` first.
