# competitor-research-agent

For each configured site: identifies competitors, extracts the discrete
features each competitor offers, then recommends:

1. **Parity-feature** — features competitors have that we don't, ranked
   by how widely they're adopted.
2. **Competitive-advantage** — speculative features we could build to
   differentiate (`tier=experimental` by default).
3. **UX-improvement** — patterns competitors use that improve onboarding
   / conversion / retention.

Output: ranked `recommendations.json` + an email with rec-id reply syntax,
shared with `progressive-improvement-agent`. Top-rated recs become
auto-pilot-eligible once the site enables `auto_implement: true`.

## Run

```bash
COMPETITOR_RESEARCH_CONFIG=/path/to/site.yaml python3 agent.py
```

Or via the framework's Run-now button. Stored under
`agents/competitor-research-agent/runs/<UTC-ts>/`.

## Config

Same schema as progressive-improvement-agent
(`shared/schemas/site-quality-config.schema.json`). The `competitors:` block
controls who we compare against:

```yaml
competitors:
  seed_domains: [mealime.com, paprika.app, plantoeat.com]
  max_competitors: 6
  max_pages_per_competitor: 4
```

If `seed_domains` is empty, the agent asks the LLM to brainstorm
competitors based on `site.what_we_do`. The result is saved to
`competitors.json` for the run so you can edit + curate over time.

## Outputs

```
~/.reusable-agents/competitor-research-agent/runs/<site>/<UTC-ts>/
  competitors.json             # which sites we used + how we found them
  features-ours.json           # extracted feature list for our site
  features-theirs.json         # per-competitor feature lists
  recommendations.json         # validated; ranked by severity → tier → confidence
  email-rendered.html
```

## Reply syntax

Same convention as the SEO + progressive-improvement agents.

```
implement rec-001 rec-005
skip rec-002
modify rec-003: build the simpler version, no admin UI
merge rec-004 rec-006
```

## Tier policy

`tier=auto` is **rare** for this agent. Adding a feature is rarely
mechanical, so the LLM is instructed to default to `review` for parity-
feature recs and `experimental` for competitive-advantage recs. Things
that DO qualify for `auto`:

- "Add JSON-LD product schema — competitors X and Y have it, we don't,
  schema content is fully derivable from our existing data."
- "Add canonical link tag to /pricing — competitors all do, we don't, no
  product decisions involved."

Things that DON'T:

- "Add a meal-planner like Mealime's." → review (or experimental)
- "Build an AI nutrition coach." → experimental

## How recs become "auto-pilot"

Same playbook as progressive-improvement-agent:

1. Run with `auto_implement: false` for a few weeks.
2. Watch which `tier=auto` recs the agent picks. They should never be
   ones you'd reject.
3. Once trusted, flip `auto_implement: true` and configure
   `implementer.agent_id` + `repo_path`.
4. From then on, `tier=auto` recs ship without a reply gate.
