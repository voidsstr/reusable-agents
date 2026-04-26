# Progressive Improvement Agent — Runbook

## Purpose

Crawl one or more configured sites starting from top-level pages and working
inward (BFS, capped depth + page count). For each page, identify:

- **Incorrect categorization** — products in the wrong category, broken
  taxonomies, mismatched tags
- **Broken pages** — non-2xx responses, JS errors, missing critical assets
- **Outdated content** — dates older than `staleness_days`, references to
  things that no longer exist
- **Duplicate content** — pages with near-identical titles, descriptions, or
  body text
- **Missing content** — pages with thin/empty critical sections
- **Layout / accessibility** — missing alt text, broken structured data,
  heading order, contrast, form-label issues
- **Performance smells** — render-blocking resources, oversized images,
  unbatched API calls
- **Content errors** — typos, wrong product names, broken interpolation
  (`{undefined}`, `[object Object]`, etc.)

Emit a ranked `recommendations.json` with each rec scored on
`{severity, confidence, tier}`. `tier=auto` recs are eligible for
auto-implementation once the per-site `auto_implement_threshold` is met +
the user opts in.

## Inputs

- `$PROGRESSIVE_IMPROVEMENT_CONFIG` — path to the per-site YAML config.
- `~/.reusable-agents/progressive-improvement/runs/<site-id>/<UTC-ts>/` —
  written each run.
- `agents/progressive-improvement-agent/responses-queue/*.json` —
  user replies parsed by responder-agent (consumed at run start).

## Outputs

- `<run_dir>/pages.jsonl` — one JSON line per crawled page with title, hash,
  links, status code, response time, and extracted summary.
- `<run_dir>/recommendations.json` — validated against
  `shared/schemas/quality-recommendations.schema.json`.
- `<run_dir>/email-rendered.html` — the HTML body sent to the recipient.
- Outbound email to the configured recipient with `[progressive-improvement-agent:<request-id>]`
  subject prefix so replies route back via responder-agent.

## Lifecycle

The agent is `AgentBase`-backed. Each run:

1. **`pre_run()`** (framework) — reads carried state (`last_seen_hashes`,
   `auto_implement_track_record`), drains response queue, resolves any
   pending confirmations.
2. **`run()`**:
   1. Load config.
   2. Drain `self.responses` and write user choices back into the
      previous run's `recommendations.json`.
   3. **Crawl** — BFS from `seed_urls` (defaults to `/` and sitemap
      entries), respecting `max_depth`, `max_pages`, and `path_excludes`.
      Each fetch records status + extracted text + outgoing links.
   4. **Analyze** — for each page (in batches of `analyzer.batch_size`),
      ask the configured AI provider (`self.ai_client()`) to flag issues
      using the prompt in `prompts/analyzer.md`.
   5. **Score + tier** — for each issue, compute confidence based on:
      LLM-stated confidence, independent corroboration (same issue on
      multiple pages), severity heuristics. Issues at
      `confidence ≥ auto_implement_threshold` and `severity ∈ {medium,
      high, critical}` get `tier=auto`. Speculative issues get
      `tier=experimental`. Everything else `tier=review`.
   6. **Email** — render the HTML report, send via the framework's
      mailer (LogMailer in dev), with subject prefix
      `[progressive-improvement-agent:<request-id>]`.
3. **`post_run()`** (framework) — writes per-run progress, errors,
   decisions, context summary; updates state.

## Auto-implement gating

Until the user opts a site in via `auto_implement: true` in the config,
all recs require explicit reply ("implement rec-007"). Once enabled,
each tick the agent dispatches `tier=auto` recs to seo-implementer
directly (logged in `decisions.jsonl`, no email gate).

The promotion criteria for adding `auto_implement: true` for a site:
- ≥ 20 prior recommendations shipped
- ≥ 95% post-ship validation pass rate (no regressions in the next 3 runs)
- User explicit toggle

## Site config shape

See `config.example.yaml`. Schema:
`shared/schemas/site-quality-config.schema.json`.

## Reply commands (parsed by responder-agent)

Two selection modes, both supported in a single reply:

**By rec id (precise):**

```
implement rec-001 rec-005   # ship these
skip rec-002                # dismiss
modify rec-003: only the title, leave the layout
merge rec-004 rec-006       # combine into one PR
```

**Bulk by tier or severity:**

```
implement all                       # ship every rec in the email
implement auto                      # only the auto-tier ones
implement high                      # all severity=high recs
implement critical and high         # multiple filters union
implement high+medium               # '+' / ',' / 'and' all work
skip experimental                   # dismiss every experimental
```

| Filter | Matches |
|---|---|
| `all` | every rec |
| `auto`, `review`, `experimental` | by tier |
| `critical`, `high`, `medium`, `low` | by severity |

You can mix modes — `implement rec-001 and all high` ships rec-001 plus
every high-severity rec.
