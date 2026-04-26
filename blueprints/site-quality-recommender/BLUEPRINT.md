# site-quality-recommender — Blueprint

## When to use

You want an agent that:

- Periodically reviews one or more configured websites
- Produces ranked recommendations (not commits — the user decides what
  ships)
- Emails a report and waits for `implement rec-NNN` replies
- Eventually graduates high-confidence recs to "auto-pilot"

Examples that fit: quality audit, competitor research, accessibility
review, performance audit, content-freshness watcher, broken-link
crawler.

Examples that **don't** fit (use a different blueprint):
- "I want to ship code immediately when an issue is detected" → see
  `llm-code-editor`
- "I want to chain processing steps with intermediate run dirs" → see
  `pipeline-stage`
- "I just want a cron-driven script" → see `scheduled-task`

## What you get

```
<agent-id>/
  AGENT.md                # runbook (purpose, lifecycle, outputs)
  SKILL.md                # the LLM analyzer prompt — what to ask the AI
  manifest.json           # framework registration
  agent.py                # subclass of AgentBase, uses shared/site_quality.py
  config.example.yaml     # site config example
  requirements.txt
  README.md
```

The blueprint comes pre-wired to `shared/site_quality.py`, which gives
you for free:

- `load_quality_config(path)` — schema-validated YAML loader
- `score_tier(confidence, severity, threshold)` — auto/review/experimental
  tier assignment
- `assign_rec_ids([...])` — stable rec-001..rec-NNN ids
- `validate_recs_doc(...)` — schema validation against
  `shared/schemas/quality-recommendations.schema.json`
- `render_recs_email(...)` — HTML email body with rec-id reply syntax
- `apply_user_responses(...)` — write replies back into the prior run's
  recommendations.json
- `dispatch_auto_recs(...)` — drop tier=auto recs into the implementer's
  response queue (only when site config opts in)

## Site config schema

`shared/schemas/site-quality-config.schema.json` — shared across all
agents that follow this blueprint, so a single YAML can drive multiple
agents per site.

Required: `site.id`, `site.domain`. Everything else has sensible
defaults.

Key per-site knobs:
- `crawler.seed_urls` / `max_depth` / `max_pages` / `path_excludes`
- `analyzer.auto_implement_threshold` (confidence cutoff for tier=auto)
- `analyzer.max_recs_per_run`
- `auto_implement` (master toggle for auto-pilot — off by default)
- `implementer.agent_id` / `repo_path` (downstream for auto-tier
  dispatch)
- `reporter.email.to` / `subject_template`

## Integration points

| Touchpoint | Wire-up |
|---|---|
| Storage | `self.storage` from AgentBase. Run output goes to disk under `runs_root`; the framework storage gets the agent's status, decisions, outbound-emails, response-queue entries. |
| AI provider | `self.ai_client()` — backend resolved per-agent via `/api/providers/defaults/agent-override`. |
| Mailer | `self.mailer` — `LogMailer` in dev (records-only), `GraphMailer`/`SmtpMailer` when configured by the API service. |
| Reply routing | The responder-agent reads inbox, parses `[<agent-id>:<request-id>]` subjects, drops payloads in `agents/<agent-id>/responses-queue/`. AgentBase drains the queue at `pre_run()`. |
| Auto-implement | `dispatch_auto_recs(...)` writes to the implementer's response-queue. The implementer (typically `seo-implementer`) does the actual code work. |

## Reference implementations

Both reference impls share `shared/site_quality.py` and the
`progressive-improvement-agent`'s crawler. Read them in this order:

1. **`progressive-improvement-agent/agent.py`** — simpler shape (one
   crawl → one LLM analysis → emit recs).
2. **`competitor-research-agent/agent.py`** — fancier (crawl-ours +
   crawl-each-competitor + extract features per site + compare LLM call
   → emit recs).

Both files are < 350 lines because all the bookkeeping lives in
`shared/site_quality.py` + `framework/core/agent_base.py`.

## Anti-patterns

1. **Don't bypass the schema.** If your recs don't validate against
   `quality-recommendations.schema.json`, fix the agent — don't disable
   validation.
2. **Don't hardcode confidence thresholds.** The threshold is a per-site
   knob (`analyzer.auto_implement_threshold`). Different sites have
   different risk tolerance.
3. **Don't auto-implement by default.** `auto_implement` is OFF in
   `config.example.yaml` for a reason. The user opts in after a track
   record. Don't push them to enable it before the agent has earned it.
4. **Don't generate hundreds of recs.** Cap at `max_recs_per_run`
   (default 15). Email overload is the main reason agents like this get
   ignored.
5. **Don't crawl indiscriminately.** Set `max_pages` and `path_excludes`
   so the agent stays polite + bounded. Add a recognizable
   `user_agent` so site owners can identify your traffic.
6. **Don't skip de-duplication.** If the same issue appears on 50 pages,
   it's ONE rec with 50 evidence URLs, not 50 recs.

## Cron pattern

This blueprint runs daily — the report is meant to be a digest the user
reviews with their morning coffee. Schedule each agent's instance
staggered (e.g., AislePrompt at 5:00, SpecPicks at 5:30) so:

- LLM provider rate limits don't collide
- Email floods are spread out
- The user can read each one in turn

## Promoting recs to auto-pilot

The blueprint defaults to `auto_implement: false`. To turn it on for a
site:

1. Run for ≥ 4 weeks. The agent must have produced ≥ 20 recs the user
   marked `implement`.
2. Verify post-ship validation pass rate ≥ 95% — i.e., the next 3 runs
   after each shipped rec didn't surface a regression caused by it.
3. Edit `site.yaml`, set `auto_implement: true`, fill in
   `implementer.agent_id` + `repo_path`.
4. The next run dispatches tier=auto recs without the email gate. Tier
   review and experimental still require explicit replies.

There's no UI toggle for `auto_implement` (yet) — it's a deliberate
high-friction step.
