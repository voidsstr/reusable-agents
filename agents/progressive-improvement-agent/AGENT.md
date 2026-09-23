# Progressive Improvement Agent (`progressive-improvement-agent`) — engine runbook

> Crawls a configured site from the top down, has an LLM audit each page for
> quality defects, and emits ranked `recommendations.json` that the
> implementer ships. Defects include broken pages, soft-404s, duplicates,
> stale content, content errors, schema and CTA problems, and site-specific QA
> rules. North Star: **indexed pages and organic clicks** (fewer broken, thin
> or duplicate pages to index) and **conversions** (working CTAs, correct
> prices and products).

Quick start and config reference: [README.md](README.md).
[SKILL.md](SKILL.md) is a calibration rubric for running the audit as a
Claude Desktop or sub-agent task. `agent.py` does **not** load it. The runtime
prompt is `ANALYSIS_SYSTEM` in `agent.py` plus the site's
`qa_detection_rules`. Per-site instances:
`aisleprompt: agents/progressive-improvement-agent/README.md` and
`specpicks: agents/progressive-improvement-agent/README.md`.

## At a glance

| | |
|---|---|
| Agent id | `progressive-improvement-agent` (engine/blueprint). It runs as `aisleprompt-progressive-improvement-agent` and `specpicks-progressive-improvement-agent`, which pass their id via `AGENT_ID` |
| Home | `reusable-agents: agents/progressive-improvement-agent/`. `agent.py` (run loop and LLM analysis), `crawler.py` (BFS crawler), and shared helpers in `shared/site_quality.py` |
| Kind | AgentBase Python **engine**. `manifest.json` has `metadata.is_blueprint: true`, `enabled: false` and an empty `cron_expr`, and `install/register-all-from-dir.sh` skips blueprints, so the engine id is **not registered** |
| Schedule | Engine: never. aisleprompt: `45 */2 * * *` (America/Detroit), `OnCalendar=*-*-* 0/2:45:00`. specpicks: `30 5 * * *` (America/Detroit), `OnCalendar=*-*-* 5:30:00`. Both timers **enabled** (checked 2026-09-23) |
| Entry command | Instances: `PROGRESSIVE_IMPROVEMENT_CONFIG=<site repo>/agents/progressive-improvement-agent/site.yaml python3 /home/voidsstr/development/reusable-agents/agents/progressive-improvement-agent/agent.py`, wrapped by `framework/agent_run_wrapper.sh <id>`. The systemd unit sets `AGENT_ID` |
| Category | `seo` |
| Priority tier | 1: `*-progressive-improvement-agent` is in the default tier-1 list in `framework/core/priority.py` |
| LLM | `self.ai_client()` resolved through `config/ai-defaults.json`. On 2026-09-23 both instances were overridden to `claude-cli` / `claude-sonnet-4-6` (log lines are prefixed `[claude-cli <id> claude-sonnet-4-6]`). The engine id's own override (`ollama-local` / `qwen3:32b`) is unused because the engine never runs under its own id |
| Status | Engine: not registered. Instances: live, and every run on 2026-09-23 ended `success` |

## What it does

Per run (`ProgressiveImprovementAgent`):

0. **setup()** loads `$PROGRESSIVE_IMPROVEMENT_CONFIG` through
   `shared.site_quality.load_quality_config_from_env()` and validates it
   against `shared/schemas/site-quality-config.schema.json`. An invalid file
   stops the run. It then creates the local run dir
   `<runs_root>/<site.id>/<UTC ts>/`. The default `runs_root` is
   `~/.reusable-agents/<agent_id>/runs`. Both sites set
   `~/.reusable-agents/progressive-improvement-agent/runs`.
   AgentBase `pre_run()` has already drained `agents/<id>/responses-queue/`
   into `self.responses` and `agents/<id>/handoff-queue/` into
   `self.inbound_handoffs`.
1. **Inbound handoffs.** They are logged as an observation decision.
   `work_types.DEFAULT_REC_ROUTING` maps `catalog-broken-image` and
   `catalog-miscategorization` (work type `quality_audit_fix`) to the
   blueprint id `progressive-improvement-agent`, not to an instance, and no
   `site.yaml` sets `handoff_routes` for them. By 2026-09-23 the instances had
   processed 3 handoffs in total (aisleprompt 2, specpicks 1, from
   `seo-implementer` and `implementer`).
2. **Apply user replies.** `apply_user_responses()` writes each reply's
   choice into the most recent *local* prior `recommendations.json` (the disk
   copy under the run dir, not the blob).
3. **Crawl** with `crawler.crawl()`:
   - It is a BFS from `crawler.seed_urls`, plus the first 50 sitemap URLs when
     `use_sitemap` is on. `/sitemap.xml` and its sitemap-index children are
     read, capped at 200 URLs.
   - It stays on the same host, where the apex and `www.` count as one host
     (`_same_origin()`, since `7d760fd` on 2026-09-23). It applies
     `path_excludes` as fnmatch on the path and honours `max_depth` and
     `max_pages`. The throttle is applied *before* each request after the
     first.
   - Transport errors and 5xx responses are retried `retry_on_error` times
     with a `retry_backoff_s` wait. A 4xx is never retried.
   - Redirects are followed and the page is recorded at its **final** URL
     (`redirected_from` keeps the requested URL). A redirect to a URL already
     seen, or off-site, is dropped.
   - Each page records title, meta description, canonical, H1, JSON-LD
     `@type`s and block count, `og:type`, robots meta, `twitter:card`, and
     **all** `<a href>` links (harvested before nav, footer and header are
     stripped). It also records anchor-free body text and `body_hash`
     (sha1[:16]).
   - `pages.jsonl` goes to the run dir and to
     `agents/<id>/runs/<run_ts>/pages.jsonl`.
4. **Broken pages without the LLM.** Any page with a fetch error, or a status
   outside 200–399, becomes a `broken-page` issue. Status 0, 500, 502 and 503
   are `critical` at confidence 0.99, and other statuses are `high`. A
   transport failure that was never retried is downgraded to `high` at 0.6.
5. **Page-hash cache.** A 2xx page whose `body_hash` matches the last run
   skips the LLM and replays its cached findings, until it has been cached
   `analyzer.revisit_unchanged_after_runs` times in a row (default 6).
6. **LLM audit, batched.** Fresh pages go to the LLM in batches of
   `analyzer.batch_size` (default 5) with `temperature=0.1` and
   `max_tokens=2000`.
   - The system prompt is the fixed `ANALYSIS_SYSTEM`, which contains the
     category enum and "evidence discipline" rules against inferring absences
     from truncated body text. The site's `analyzer.qa_detection_rules` are
     appended as `[SEVERITY] id: summary` bullets, followed by
     `goal_changes.adaptive_context_block()` (30-run horizon).
   - Each page contributes its head signals, a link profile (a histogram by
     first path segment plus up to 15 targets from the rarest sections) and
     the first 6,000 characters of body text, labelled as truncated or
     complete.
   - JSON parsing is tolerant. A failed batch is logged and skipped.
7. **Score, tier and dedupe.**
   - `score_tier()` assigns `auto` when confidence ≥
     `analyzer.auto_implement_threshold` (default 0.95) *and* severity is
     medium, high or critical. Confidence below 0.5 gives `experimental`, and
     everything else is `review`. Confidence is the LLM's own number, with no
     re-scoring.
   - Issues are dropped when their canonical key
     (`category|first evidence URL minus query|normalised title[:60]`)
     matches a rec that was shipped, implemented or skipped in the last 30
     runs. Live-state rec types are exempt (`work_types.is_live_state`).
   - Each rec gets `goal_ids` from `_CATEGORY_GOAL_MAP`.
8. **Handoff of miscategorisations.** `incorrect-categorization` recs leave
   the rec list and go to `<site>-catalog-audit-agent` through
   `send_handoff(work_type="quality_audit_fix")`. The storage key is
   `agents/<target>/handoff-queue/<request_id>.json`. If the handoff fails,
   they stay in the list as `deferred`.
9. **Rank, cap and assign ids.** Recs are sorted by severity, then tier, then
   confidence descending, and capped at `analyzer.max_recs_per_run` (default
   15). Each gets an id `rec-NNN` and a `rec_uid` `r-<8hex>`, and `type`
   mirrors `category`.
10. **Persist.**
    - Producer-history dedup (`filter_proposals_against_history`, state at
      `agents/<id>/state/emitted-titles.json`) drops titles emitted before.
      The run summary ends with `[producer-history dedup dropped N]`.
    - `stamp_implementer_safety(dispatch_kind="pi")` runs, and the doc is
      validated against `shared/schemas/quality-recommendations.schema.json`.
    - `recommendations.json` is saved to disk and to
      `agents/<id>/runs/<run_ts>/`.
11. **Email.**
    - `render_recs_email()` produces the body, saved as `email-rendered.html`.
      `send_via_msmtp()` is then called. `DIGEST_ONLY` defaults to `1` in
      the code and the wrapper also exports it, so the mail is **queued to
      `digest-queue/`** for `digest-rollup-agent` rather than sent (with
      `DIGEST_DISABLED=1` it is dropped).
    - When the send returns ok, which includes the digest-suppressed case, it
      writes `agents/<id>/outbound-emails/<request_id>.json` and calls
      `dispatch.gated_dispatch_now()`. With `auto_implement: false` (both
      sites) that call only logs "awaiting email approval".
12. **Goal metrics.** `_measure_and_update_goals()` sets `metric.current` on
    active goals whose `metric.name` is `broken_pages`,
    `miscategorized_count`, `duplicate_count`, `stale_count` or
    `accessibility_violations`, and back-fills `metric_after` in
    `goals/changes.jsonl`.
13. **`dispatch_auto_recs()`.** Only when `auto_implement: true`, it writes
    `agents/<implementer.agent_id>/responses-queue/<ts>-auto-progressive-improvement-agent.json`
    with the auto-tier rec ids. The payload's `from_agent` is the **engine**
    id, not the instance id. This is a no-op on both sites today.
14. **Handoff outcomes.** Each inbound handoff is marked `in_progress` only
    when a produced rec's category starts with one of the prefixes that
    `HANDOFF_RECTYPE_TO_REC_CATEGORY` maps from the handoff's `rec.type`.
    Otherwise it is marked `deferred`. The mapped prefixes (`catalog-quality`,
    `content`, `ux`, `internal-link`) are mostly not PI categories. Only
    `content-error` can match (through `content`). `catalog-broken-image`,
    `catalog-miscategorization` and handoffs with no `rec.type` never match,
    so they are always recorded `deferred`.
15. **Return** `RunResult(status="success")` with the metrics and
    `next_state` below. The only `failure` return is "AI provider not
    configured". Exceptions go to AgentBase's failure path.

**How recs actually reach the implementer.** Neither producer-side path above
fires on either site (`auto_implement: false`). Delivery is through
`backlog-dispatcher-agent`, which runs every minute:

1. Both instance ids are in its `PRODUCER_AGENT_IDS`.
2. It reads successful runs from `agents/<id>/run-index.json` and queues
   un-shipped recs from their `recommendations.json` into the Azure
   auto-queue.
3. It deliberately **ignores** `auto_implement` (gate disabled 2026-05-13, see
   the comment in its `agent.py`).
4. `auto-queue-drainer` then fires the implementer.

On 2026-09-23 the dispatcher log shows it materialising run-dirs for both PI
instances (`rundir-<id>-<run_ts>-…`).

## Inputs

| Input | Detail |
|---|---|
| Live site | HTTP GET from `site.base_url` (or `https://<site.domain>`), with User-Agent `crawler.user_agent` |
| Site config | `$PROGRESSIVE_IMPROVEMENT_CONFIG` (see Configuration) |
| Carried state | `agents/<id>/state/latest.json` → `last_seen_hashes`, `last_findings_by_hash`, `hash_revisit_counter` |
| Prior runs | `agents/<id>/runs/*/recommendations.json` (last 30 runs, for handled-rec dedupe). `agents/<id>/state/emitted-titles.json` (producer history) |
| Goals | `agents/<id>/goals/active.json`, `goals/changes.jsonl` (adaptive context) |
| Replies / handoffs | `agents/<id>/responses-queue/` (from `responder-agent`), `agents/<id>/handoff-queue/` |
| LLM | Whatever `self.ai_client()` resolves (see At a glance) |

No database is read. Everything the engine knows about the site comes from
the crawl.

## Outputs

| Output | Where |
|---|---|
| `pages.jsonl`, `recommendations.json`, `email-rendered.html` | Local `<runs_root>/<site>/<ts>/` and blob `agents/<id>/runs/<run_ts>/` (`pages.jsonl` body text capped at 8,000 chars) |
| Recs | `category` / `type` ∈ `broken-page`, `outdated-content`, `duplicate-content`, `missing-content`, `layout-issue`, `accessibility`, `performance`, `content-error`, `other` (`incorrect-categorization` is handed off). Fields: `severity`, `confidence`, `tier`, `title`, `rationale`, `evidence[{url,snippet}]`, `affected_urls`, `implementation_outline.approach`, `goal_ids`, `implementer_safety`, `id`, `rec_uid` |
| Handoffs | `agents/<site>-catalog-audit-agent/handoff-queue/*.json` (miscategorisations) |
| Email | `digest-queue/<ts>-<hash>.json` (digest mode), metadata in `agents/<id>/outbound-emails/<request_id>.json`. The recipients are in `reporter.email.to` (both sites: `mperry@northernsoftwareconsulting.com`) |
| Goals | `agents/<id>/goals/active.json` (current values), `goals/changes.jsonl` (`metric_after`) |
| Producer history | `agents/<id>/state/emitted-titles.json` |

**RunResult.metrics:**

| Key | Meaning |
|---|---|
| `pages_crawled` | Pages crawled this run |
| `recs_total` | Recs after the cap, **before** producer-history dedup |
| `recs_auto`, `recs_review`, `recs_experimental` | Recs per tier |
| `applied_responses` | User replies applied to prior recs |
| `auto_dispatched` | Recs sent by `dispatch_auto_recs()` (always 0 while `auto_implement: false`) |
| `broken_pages` | Pages with a fetch error or a status outside 200–399 |
| `miscategorized_count` | See the caveat under Goals & metrics |
| `duplicate_count` | `duplicate-content` recs |
| `stale_count` | `outdated-content` plus `missing-content` recs |
| `accessibility_violations` | `accessibility` recs |
| `quality_score_0_100` | `100 − 100·recs/pages`, clamped to 0–100 |

**next_state:** `last_run_ts`, `last_request_id`, `site_id`,
`last_seen_hashes`, `last_findings_by_hash`, `hash_revisit_counter`.

## Goals & metrics

Seeded by `install/seed-default-goals.sh` for both instances. On 2026-09-23
the live `goals/active.json` also held three more goals (the first three
rows below). Instance manifests declare `target_metric: goal-organic-clicks-30d`,
which is a goal-id label rather than a key this engine emits, and no goal with
that id exists in either instance's `active.json`.

| Goal id | `target_metric` (RunResult key) | Target | aisleprompt | specpicks |
|---|---|---|---|---|
| `goal-issues-found-per-run` | `recs_total` | 20 (increase) | 6 (active) | 0 (active) |
| `goal-issues-fixed-30d` | `applied_responses` | 200 (increase) | 0 | 0 |
| `goal-quality-score-trend` | `quality_score_0_100` | 85 (increase) | 85.0 | 81.25 |
| `goal-zero-broken-pages` | `broken_pages` | 0 (decrease) | 0 | 0 |
| `goal-zero-miscategorized-products` | `miscategorized_count` | 0 | 0 | 0 |
| `goal-zero-duplicate-content` | `duplicate_count` | 0 | 0 | 0 |
| `goal-content-freshness` | `stale_count` | 0 | 0 | 2 |
| `goal-accessibility-baseline` | `accessibility_violations` | 0 | 0 | 0 |

The values are `metric.current` from `goals/active.json` on 2026-09-23. All
goals except `goal-issues-found-per-run` were stored with
`status: accomplished`, including specpicks' quality score at 81.25 against a
target of 85. `goals.record_goal_progress()` marks a goal accomplished the
first time its target is reached and never sets it back to active when the
metric regresses (specpicks scored 98.75 on 2026-09-22).

Caveats, all visible in the code:

- **`miscategorized_count` is structurally 0.** Step 8 removes
  `incorrect-categorization` recs before this count is taken, so it only
  counts them when the handoff fails. The real miscategorisation work shows up
  in `<site>-catalog-audit-agent`.
- **Most goals are only updated by AgentBase auto-tracking.** Only
  `goal-zero-broken-pages` (`broken_pages`) and
  `goal-zero-miscategorized-products` (`miscategorized_count`) have a
  `metric.name` that `_measure_and_update_goals()` writes. The seeded names
  `duplicate_groups`, `stale_pages` and `a11y_violations` look meant for it but
  do not match. Those three, and the three extra goals, move only through
  `target_metric`.
- **`goals/changes.jsonl` is archived on both instances.** On 2026-09-23 it
  sat in the Azure *archive* tier. Every read fails with `BlobArchived`,
  logged twice per run. It is non-fatal, but the adaptive-context block and
  `metric_after` back-fill are effectively off until the blob is rehydrated.

## Configuration

**`site.yaml`** (schema `shared/schemas/site-quality-config.schema.json`, shared
with `competitor-research-agent`):

| Key | Default in code | Meaning |
|---|---|---|
| `site.id`, `site.domain`, `site.label`, `site.base_url`, `site.what_we_do` | — / — / `id` / `https://<domain>` / "" | Identity. `what_we_do` is fed to the LLM as `SITE PURPOSE` |
| `crawler.seed_urls` | `["/"]` | BFS roots |
| `crawler.use_sitemap` | `true` | Add up to 50 sitemap URLs to the seeds |
| `crawler.max_depth` / `max_pages` | 2 / 30 | Crawl shape |
| `crawler.path_excludes` | `[]` | fnmatch globs on the URL path |
| `crawler.request_timeout_s` / `throttle_ms` | 15 / 500 | Per request |
| `crawler.retry_on_error` / `retry_backoff_s` | 1 / 1.5 | Retries for transport errors and 5xx. `0` means single-shot |
| `crawler.user_agent` | `reusable-agents-quality-crawler/1.0` | |
| `analyzer.batch_size` | 5 | Pages per LLM call |
| `analyzer.max_recs_per_run` | 15 | Cap after ranking |
| `analyzer.auto_implement_threshold` | 0.95 | Confidence needed for `tier=auto` |
| `analyzer.revisit_unchanged_after_runs` | 6 | Page-hash cache lifetime, in runs |
| `analyzer.qa_detection_rules[]` | `[]` | `{id, severity, summary}`, injected into the system prompt |
| `analyzer.issue_categories` | — | Allowed by the schema but **not read by the engine**. The category enum is fixed in `ANALYSIS_SYSTEM` |
| `auto_implement` | false in `dispatch_auto_recs`; `gated_dispatch_now` treats a missing flag as true | Producer-side direct dispatch. It does not gate the backlog-dispatcher path |
| `implementer.agent_id` | `implementer` | Target for `dispatch_auto_recs()` |
| `implementer.repo_path`, `branch`, `allowed_paths`, `excluded_paths`, `post_apply`, `scope_by_dispatch_kind` | — | Implementer scope policy (`framework/core/implementer_scope.py`). The engine itself does not read these |
| `reporter.email.to` / `from` / `msmtp_account` / `subject_template` | — / — / `automation` / `<agent_id> — {site} — {tag}` | Report email. Without both `to` and `from`, only `email-rendered.html` is written and gated dispatch is never evaluated |
| `runs_root` | `~/.reusable-agents/<agent_id>/runs` | Local run dirs |

**Environment:**

| Env var | Default | Meaning |
|---|---|---|
| `PROGRESSIVE_IMPROVEMENT_CONFIG` | none (required) | Path to `site.yaml`. Without it: `SystemExit: set PROGRESSIVE_IMPROVEMENT_CONFIG …` |
| `AGENT_ID` | `progressive-improvement-agent` | Instance id and storage namespace. Set in the instance's systemd unit, **not** in `entry_command`, so a bare manual run of the entry command writes under the engine id |
| `PI_DISABLE_HANDLED_DEDUPE` | unset | `1` turns off the "already shipped or skipped" dedupe |
| `DIGEST_ONLY` | `1` (code default in `shared/site_quality.py`, also exported by `agent_run_wrapper.sh`) | `0` sends the report email directly instead of queueing it to the digest |
| `DIGEST_DISABLED` | unset | `1` drops digest-mode mail instead of queueing it |
| `AGENT_FORCE_RUN` | unset | Bypasses the AgentBase run-level short-circuit (see below) |
| `STORAGE_BACKEND` | set to `azure` by the unit | Storage backend |

## Short-circuit & idempotency

- **Per-page cache: active and effective.** On 2026-09-23 the aisleprompt
  16:45 UTC run sent 4 of 40 pages to the LLM (36 cached) and finished in
  about 6 minutes. specpicks runs daily, so most pages count as fresh: the
  09:30 UTC run sent 73 fresh pages and 7 cached ones, and took about 82
  minutes.
- **Run-level `signals()`: dormant, and it has to stay that way until it is
  fixed.** The hook hashes root-level storage `queue/` and
  `context-index.json`. On 2026-09-23 the first had 0 keys and the second
  didn't exist, so the inputs never change. The hash is not saved either,
  because `run()` returns a `next_state` without `_auto_signals_hash`, so
  AgentBase never short-circuits. Do not "fix" the persistence without first
  pointing `signals()` at real inputs, or every run after the first will skip.
- **Idempotency.** Three things stop re-running from double-queuing work: the
  handled-rec dedupe over the last 30 runs, the producer-history title dedupe,
  and the backlog dispatcher's `queued_ids` state.

## Running & inspecting

```bash
systemctl --user start agent-aisleprompt-progressive-improvement-agent.service   # or specpicks-…
systemctl --user list-timers | grep progressive-improvement
tail -f /tmp/reusable-agents-logs/agent-aisleprompt-progressive-improvement-agent.log
curl -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
     http://localhost:8090/api/agents/aisleprompt-progressive-improvement-agent/runs?limit=5
ls ~/.reusable-agents/progressive-improvement-agent/runs/aisleprompt/ | tail -3   # local artifacts
```

A manual run outside systemd must export `AGENT_ID=<site>-progressive-improvement-agent`,
or it writes under the engine id. There is no dry-run flag. A run crawls the
live site, calls the LLM and writes recs that the backlog dispatcher will
queue.

## Reply commands

Replies are parsed by `shared/site_quality.parse_user_action` after
responder-agent drops them into `responses-queue/`. The notification email is
rendered with `auto_queued=True` (whenever there are recs) and advertises
`defer` / `skip` / `revert` overrides. PI's own parser recognises only the
verbs below, and no `defer` or `revert` handling was found in
`agents/responder-agent/*.py` on 2026-09-23. The email itself is queued to the
digest rather than sent (step 11), so whether replies reach this agent at all
is unverified. `responses-queue/` was empty for both instances on 2026-09-23.

**By rec id.** Accepts `rec-NNN`, with ranges (`rec-001 - rec-007`,
`rec-001 to rec-007`, `implement 1-7`) and bare lists after a verb
(`implement 1, 3, 5`). The parser also collects global `r-<8hex>` uids, but
PI's `apply_user_responses()` matches only on `id` (`rec-NNN`), so uid replies
are not applied to PI's prior recs file.

```
implement rec-001 rec-005
skip rec-002
modify rec-003: only the title, leave the layout
merge rec-004 rec-006
```

Each verb is only recorded as `user_response.action` on the rec (see below).
PI attaches no further meaning to it. The backlog dispatcher reads the blob
copy, not the local file, so a recorded `skip` does not stop it from queuing
the rec.

**In bulk, by tier or severity.** Only after `implement` or `skip`; filters
are unioned:

```
implement all | implement auto | implement high | implement critical and high
implement high+medium       # '+' / ',' / 'and' all work
skip experimental
```

| Filter | Matches |
|---|---|
| `all` | every rec |
| `auto`, `review`, `experimental` | by tier |
| `critical`, `high`, `medium`, `low` | by severity |

Choices are written as `user_response` on the matching recs in the most recent
**local** prior `recommendations.json`. The `applied_responses` metric counts
them. It was 0 on every run on 2026-09-23.

## Auto-implement gating (history)

`auto_implement: true` enables the producer-side paths
(`gated_dispatch_now()` direct dispatch and the `dispatch_auto_recs()`
responses-queue write). The promotion bar originally written for flipping it
was ≥ 20 shipped recs, a ≥ 95% no-regression rate over the next 3 runs, and an
explicit operator toggle. Both sites still have it `false`. Since 2026-05-13,
recs ship anyway through `backlog-dispatcher-agent`, which ignores the flag and
applies its own classifier and capacity caps.

## Failure modes & troubleshooting

| Symptom | Cause / fix |
|---|---|
| `SystemExit: set PROGRESSIVE_IMPROVEMENT_CONFIG …` or `config invalid at <path>: …` | Missing env var, or `site.yaml` fails schema validation. Add new keys to `site-quality-config.schema.json` first |
| `status=failure`, "AI provider not configured" | `self.ai_client()` raised. Check `config/ai-defaults.json` or the `/providers` page |
| `LLM batch N failed: …` in decisions, fewer recs | That batch is skipped and the others continue. With `claude-cli`, check claude-pool health (the KTLO skill) |
| `azure read_bytes …/goals/changes.jsonl: … BlobArchived` twice per run | The blob is in the archive tier (seen on both instances 2026-09-23). Non-fatal, but adaptive context and `metric_after` are lost until it is rehydrated |
| A flood of `broken-page` critical recs | Look at the `[N fetch attempts]` suffix in the rationale. Transport blips are retried since 2026-09-02 (`2d9b456`). A single-attempt status 0 is downgraded to high / 0.6 |
| The same SEO or indexing finding keeps coming back on a redirected slug | Fixed 2026-09-18 (`1f460cc`): the crawler records pages at their final URL |
| False claims that a page has no links, pagination or JSON-LD | Fixed by passing the link profile and JSON-LD types to the LLM (2026-08-14, `e94d1fd`) and harvesting links before stripping chrome (2026-09-03, `12c29d0`). Anything left is usually a prompt or rule issue: tighten the site's `qa_detection_rules` |
| An availability rec is never re-proposed after being "handled" | It should be. Live-state rec types are exempt from dedupe since 2026-09-10 (`c17d59f`) |
| Crawler returns 0 pages | Wrong `base_url`/`seed_urls`, every seed excluded, or the origin is refusing the UA |
| Almost every page dropped as "off-site" | `base_url` 301s between the apex and `www.`. From `1f460cc` (2026-09-18) pages were recorded at their final URL, and the then exact-netloc `_same_origin()` treated them as off-site. This emptied competitor-research crawls, which share this crawler. Fixed 2026-09-23 in `7d760fd`: the apex and `www.` now count as one host (regression test `framework/tests/test_crawler_origin.py`). If it recurs with another host change (for example a different subdomain), set `site.base_url` to the final host |

## Related agents

- **Instances:**
  - `aisleprompt: agents/progressive-improvement-agent/README.md`
  - `specpicks: agents/progressive-improvement-agent/README.md`
- **Downstream:**
  - `backlog-dispatcher-agent` queues the recs, `auto-queue-drainer` drains
    the queue and `implementer` ships them. The implementer applies each
    site's `implementer.*` scope.
  - `<site>-catalog-audit-agent` receives miscategorisation handoffs.
  - `digest-rollup-agent` sends the queued report email.
- **Upstream:** `responder-agent` (email replies go to `responses-queue/`).
  `handoff` senders use the `catalog-broken-image` and
  `catalog-miscategorization` rec types.
- **Sibling:** `competitor-research-agent` shares the site-quality config
  schema and `shared/site_quality.py`.
- **`crawler.py` has other importers:** `competitor-research-agent`,
  `seo-opportunity-agent` (`lib/analyzer/analyzer.py`) and `seo-analyzer` all
  put this dir on `sys.path` and import `crawl`. A crawler change affects all
  of them.
