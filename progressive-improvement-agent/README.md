# progressive-improvement-agent

Crawls a configured website starting from the top level, working inward, and
flags quality issues — incorrect categorization, broken pages, outdated /
duplicate / missing content, layout problems, accessibility nits. Produces a
ranked `recommendations.json` and emails it to the configured recipient
with reply-to-ship syntax (handled by `responder-agent`).

Each recommendation gets a **tier**:
- `auto` — high confidence + meaningful severity → eligible for
  auto-implementation (only if `auto_implement: true` in site config)
- `review` — needs explicit human approval
- `experimental` — speculative, low confidence

## Run

```bash
PROGRESSIVE_IMPROVEMENT_CONFIG=/path/to/site.yaml python3 agent.py
```

Or via the framework's Run-now button. The dashboard's `/agents` page
shows it in the grid; runs are stored under
`agents/progressive-improvement-agent/runs/<UTC-ts>/`.

## Config

See `config.example.yaml` and the JSON schema at
`shared/schemas/site-quality-config.schema.json`. The same schema is
shared with `competitor-research-agent`, so a single YAML can drive both.

Key fields:

| Field | What |
|---|---|
| `site.id` / `site.domain` / `site.label` | identity |
| `site.what_we_do` | one-paragraph context fed to the LLM |
| `crawler.seed_urls` / `max_depth` / `max_pages` | crawl shape |
| `crawler.path_excludes` | glob patterns to skip |
| `analyzer.auto_implement_threshold` | confidence cutoff for `tier=auto` (default 0.95) |
| `analyzer.max_recs_per_run` | cap; default 15 |
| `auto_implement` | master toggle (off until track record proven) |
| `implementer.agent_id` | downstream agent for auto-tier dispatch (default `seo-implementer`) |
| `reporter.email.to` / `subject_template` | who gets the report |

## Outputs

```
~/.reusable-agents/progressive-improvement-agent/runs/<site>/<UTC-ts>/
  pages.jsonl                  # one line per crawled page
  recommendations.json         # validated against quality-recommendations.schema.json
  email-rendered.html          # exact body sent
```

## Reply syntax

Reply to the email — keep `Re:` so the responder can route. The body can
contain any of:

```
implement rec-001 rec-005   # ship
skip rec-002                # dismiss
modify rec-003: shorter title
merge rec-004 rec-006       # combine
```

The next run picks the responses out of the agent's `responses-queue/` and
records the user's choices in the prior run's `recommendations.json`.

## How recs become "auto-pilot"

1. Run the agent against your site for a few weeks with `auto_implement:
   false`.
2. As the agent ships recs (via your `implement` replies), watch the
   tier-`auto` ones — those should never be ones you'd reject.
3. When you trust them — typically 20+ recs shipped + 95%+ no-regression
   rate — flip `auto_implement: true` AND set `implementer.agent_id` +
   `implementer.repo_path`. From then on, `tier=auto` recs are dispatched
   to the implementer's response queue without an email gate.
4. `tier=review` and `tier=experimental` still always require your reply.

## Failure modes

- AI provider not configured → run fails fast with a clear message.
  Configure one in the `/providers` UI first.
- Crawler returns 0 pages → either base URL wrong or robots-blocked. Set
  `user_agent` to something polite + check `seed_urls`.
- LLM returns non-JSON → individual batches drop, others continue. Check
  `decisions.jsonl` in the run dir.
