# progressive-improvement-agent

Crawls a configured website from the top level inward and flags quality
issues:

- broken pages and soft-404s
- outdated, duplicate or missing content
- layout, accessibility and performance issues
- content errors
- site-specific QA patterns (`analyzer.qa_detection_rules`)
- miscategorisation, which is handed off to the site's catalog-audit agent

It produces a ranked `recommendations.json` that `backlog-dispatcher-agent`
feeds to the implementer. It also renders a notification email, which is
queued to the fleet digest.

> **Runbook:** [AGENT.md](AGENT.md) has phases, inputs and outputs, metrics,
> goals, short-circuit behaviour and failure modes. [SKILL.md](SKILL.md) is a
> rubric for Claude Desktop or sub-agent use. The runtime prompt lives in
> `agent.py`. This is the **engine**, and it is never scheduled
> under its own id. The live instances are
> `aisleprompt: agents/progressive-improvement-agent/` (every 2 h at :45) and
> `specpicks: agents/progressive-improvement-agent/` (daily 05:30,
> America/Detroit).

Each recommendation gets a **tier**:
- `auto`: confidence ≥ `analyzer.auto_implement_threshold` (default 0.95)
  and severity medium, high or critical.
- `review`: confidence from 0.5 up to the threshold, or a low-severity rec
  above the threshold.
- `experimental`: confidence < 0.5.

Tiers drive sorting and the producer-side `auto_implement` path. The backlog
dispatcher queues every tier.

## Run

```bash
AGENT_ID=<site>-progressive-improvement-agent \
PROGRESSIVE_IMPROVEMENT_CONFIG=/path/to/site.yaml python3 agent.py
```

On the fleet host use `systemctl --user start agent-<site>-progressive-improvement-agent.service`.
The unit supplies `AGENT_ID`, `STORAGE_BACKEND=azure` and the secrets file.
Without `AGENT_ID` the run writes under the engine id
(`agents/progressive-improvement-agent/…`). Runs are stored in blob storage
under `agents/<AGENT_ID>/runs/<run_ts>/`.

## Config

See `config.example.yaml` and the JSON schema at
`shared/schemas/site-quality-config.schema.json`. The same schema is shared
with `competitor-research-agent`, so a single YAML can drive both. The full key
table with code defaults is in [AGENT.md](AGENT.md#configuration).

Key fields:

| Field | What |
|---|---|
| `site.id` / `site.domain` / `site.label` / `site.base_url` | Identity |
| `site.what_we_do` | One-paragraph context fed to the LLM |
| `crawler.seed_urls` / `max_depth` / `max_pages` / `use_sitemap` | Crawl shape |
| `crawler.path_excludes` | fnmatch globs on the URL path to skip |
| `crawler.retry_on_error` / `retry_backoff_s` | Retries for transport errors and 5xx (default 1 / 1.5 s) |
| `analyzer.batch_size` / `revisit_unchanged_after_runs` | LLM batch size (default 5), page-hash cache lifetime (default 6 runs) |
| `analyzer.qa_detection_rules` | Site-specific `{id, severity, summary}` rules injected into the prompt |
| `analyzer.auto_implement_threshold` | Confidence cutoff for `tier=auto` (default 0.95) |
| `analyzer.max_recs_per_run` | Cap (default 15) |
| `auto_implement` | Producer-side direct dispatch toggle. Both sites: `false`. It does **not** stop the backlog dispatcher |
| `implementer.agent_id` | Target for auto-tier dispatch when `auto_implement: true` (default `implementer`) |
| `implementer.allowed_paths` / `excluded_paths` / `post_apply` | Implementer scope policy, read by the implementer rather than this engine |
| `reporter.email.to` / `from` / `msmtp_account` / `subject_template` | Report email |
| `runs_root` | Local run-dir root (default `~/.reusable-agents/<agent_id>/runs`) |

## Outputs

```
<runs_root>/<site>/<UTC-ts>/              # local, e.g. ~/.reusable-agents/progressive-improvement-agent/runs/specpicks/…
  pages.jsonl                  # one line per crawled page
  recommendations.json         # validated against quality-recommendations.schema.json
  email-rendered.html          # the rendered report body
agents/<AGENT_ID>/runs/<run_ts>/          # blob copies of the same three files
```

## Reply syntax

Reply to the email and keep `Re:` so responder-agent can route it. The body
can contain any of:

```
implement rec-001 rec-005
skip rec-002
modify rec-003: shorter title
merge rec-004 rec-006
implement high              # bulk by severity or tier (implement/skip only)
```

The next run picks the replies out of `agents/<AGENT_ID>/responses-queue/` and
records them as `user_response` in the most recent **local** prior
`recommendations.json`. That is all a reply does in PI: the backlog dispatcher
reads the blob copy and ignores `user_response`. The email is queued to the
digest rather than sent directly, so whether replies reach this agent is
unverified (`applied_responses` was 0 on every 2026-09-23 run).

## How recs ship today

1. Each run writes `recommendations.json` to blob storage.
2. `backlog-dispatcher-agent` (every minute) lists both site instances in
   `PRODUCER_AGENT_IDS`. It queues un-shipped recs into the Azure auto-queue
   and ignores `auto_implement` (gate disabled 2026-05-13).
3. `auto-queue-drainer` fires the implementer, which applies the site's
   `implementer.*` scope.

The original "auto-pilot" playbook was ≥ 20 shipped recs with ≥ 95% no
regressions before flipping `auto_implement: true`. It now only governs the
producer-side direct-dispatch paths. See
[AGENT.md](AGENT.md#auto-implement-gating-history).

## Failure modes

- AI provider not configured → the run returns `failure` fast. Configure one in
  `config/ai-defaults.json` or on the `/providers` page.
- Crawler returns 0 pages → the base URL is wrong, every seed is excluded, or
  the origin is blocking the UA. Check `seed_urls` and `user_agent`.
- LLM returns non-JSON → that batch is dropped and the others continue. Check
  the run's `decisions.jsonl`.
- `BlobArchived` on `goals/changes.jsonl` → non-fatal. See
  [AGENT.md](AGENT.md#goals--metrics).
