# Competitor Research Agent (`competitor-research-agent`) — shared engine runbook

> For one site per run, the agent crawls our site and a curated list of
> competitor sites, then extracts each site's feature list with an LLM.
> Next it asks an LLM for fully blueprinted **parity-gap / competitive-
> advantage / UX / content-gap** recommendations, keeps them in a cross-run
> proposal backlog and emails the top of that backlog for approval.
>
> It serves site conversions and engagement. The per-site manifests
> declare `target_metric: goal-instacart-clicks-30d` (aisleprompt) and
> `goal-amazon-clicks-30d` (specpicks).

This directory is the **engine**. It is not scheduled and not registered.
The scheduled agents are the per-site instances listed below. For an
overview and the tier policy, see [README.md](README.md). This file is the
operational runbook.

## At a glance

| | |
|---|---|
| Agent id | `competitor-research-agent` (engine). Instances: `aisleprompt-competitor-research-agent`, `specpicks-competitor-research-agent` |
| Home | `reusable-agents/agents/competitor-research-agent/`: `agent.py`, `_accumulator.py`, `_backfill_accumulator.py`, `config.example.yaml`, `SKILL.md` |
| Kind | AgentBase python (`CompetitorResearchAgent`). Shared engine |
| Schedule | None. The manifest sets `enabled: false`, `cron_expr: ""`, `metadata.is_blueprint: true`. `install/register-all-from-dir.sh` skips `is_blueprint` manifests, so the engine is **not in the registry**. `GET /api/agents/competitor-research-agent` returns 404 (checked 2026-09-23), even though the manifest description says the entry exists "so the framework can show … run-history" |
| Entry command | Per instance: `COMPETITOR_RESEARCH_CONFIG=<site.yaml> python3 /home/voidsstr/development/reusable-agents/agents/competitor-research-agent/agent.py` |
| Category | `research` (blueprint `site-quality-recommender`) |
| Status | Engine only. Both instances are live. They were **degraded from 2026-09-18 to 2026-09-23** by four defects, all fixed in commit `7d760fd` (2026-09-23 17:59Z). At the time of writing, no scheduled run had yet confirmed the fix (see Failure modes) |
| Instance runbooks | aisleprompt: `aisleprompt/agents/competitor-research-agent/README.md`. specpicks: `specpicks/agents/competitor-research-agent/README.md` |

### Instances (checked 2026-09-23)

| Instance | Cron (manifest, America/Detroit) | systemd `OnCalendar` | `auto_implement` |
|---|---|---|---|
| `aisleprompt-competitor-research-agent` | `4 */5 * * *` | `*-*-* 0/5:4:00` (enabled) | `false` |
| `specpicks-competitor-research-agent` | `30 6 * * *` | `*-*-* 6:30:00` (enabled) | `false` |

Both instances set `runs_root: ~/.reusable-agents/competitor-research-agent/runs`,
so local run dirs are `…/runs/<site_id>/<UTC-ts>/`.

## What it does

`run()` runs these phases in order.

0. **setup**:
   - Reads `COMPETITOR_RESEARCH_CONFIG`; if it is unset, the agent exits
     with `SystemExit`.
   - Loads the file with `load_quality_config()`, which validates it
     against `shared/schemas/site-quality-config.schema.json`.
   - Creates the local run dir.
   - The instance `agent_id` comes from the unit's `AGENT_ID` environment
     variable.
1. **Prior replies**: `apply_user_responses()` applies `self.responses`
   (from `agents/<agent_id>/responses-queue/`) to the previous run's local
   `recommendations.json`.
2. **AI client check**: if `self.ai_client()` raises, the agent returns
   `failure` with "AI provider not configured".
3. **Competitor list**: taken from `competitors.seed_domains`, capped at
   `max_competitors` (default 6).
   - If the list is empty, the agent brainstorms domains with
     `ai_client(call="brainstorm")`.
   - The result is normalised, the site's own domain is dropped, and the
     list is saved as `competitors.json`.
   - The scheme is removed with the prefix regex `^https?://`, and a
     trailing `/` is stripped. Before `7d760fd` (2026-09-23) the code used
     `str.lstrip("https://")`, which mangled domains; see Failure modes.
4. **Our features**:
   - **`scan_mode: website`** (the default). The agent crawls our site
     with `agents/progressive-improvement-agent/crawler.py`, driven by the
     `crawler.*` keys. It keeps 2xx pages that have a body, then runs
     `_extract_features()` with `ai_client(call="extract")`,
     `EXTRACT_FEATURES_SYS` and `max_tokens=2000`.
     - Pages are formatted with the body capped at 7,000 characters, plus
       JSON-LD types, og/twitter/robots and the canonical URL. That was
       the fix for JSON-LD blindness on 2026-05-31.
   - **`scan_mode: codebase`**. The agent runs `_scan_codebase()` over
     `codebase.*`, then `_extract_features_from_text()`, and saves the
     scan as `codebase-scan.txt`.
   - Either way, the output is `features-ours.json`.
5. **Competitor features**:
   - For each seed, the agent crawls `https://<seed>` from the paths `/`,
     `/features`, `/pricing`, `/products` and `/about`, with no sitemap
     and depth 0, up to `max_pages_per_competitor` pages (default 4).
   - Competitors with no 2xx pages are dropped.
   - The remaining pages go to **one** batched extract call
     (`EXTRACT_FEATURES_BATCH_SYS`, `max_tokens=4500`).
   - If that call fails, the agent falls back to per-competitor calls; any
     domains the batch misses also get per-competitor calls.
   - The output is `features-theirs.json`.
   - The crawler treats `example.com` and `www.example.com` as the same
     site (`_same_origin`, fixed in `7d760fd`). Real off-site redirects
     are still dropped.
6. **Optional `app_stores:` block**. The agent looks up iTunes and Google
   Play metadata with `framework.core.app_store_clients` and writes
   `app-stores-ours.json` and `app-stores-theirs.json`.
   - No instance configures this block.
   - From reading the code, merging competitor apps assigns into
     `theirs_features` with a string key while `theirs_features` is a
     list. The step would raise, and the error would be logged as
     "app-store scan failed". This is untested.
6b. **No-evidence guard** (added in `7d760fd`, 2026-09-23). If
   `features-theirs.json` is empty, the run returns
   `RunResult(status="blocked", …)` with the summary "No competitor pages
   crawled for any of N seed(s) (…); skipped compare…" and the metrics
   `{"competitors_analyzed": 0, "recs_total": 0}`. Compare, email and
   dispatch are all skipped.
7. **Compare**. Inputs and limits:
   - `max_recs` comes from `analyzer.max_recs_per_run` (default 8 at this
     step; both instances set 15).
   - The prompt includes:
     - OUR FEATURES
     - COMPETITOR FEATURES for this chunk
     - `current_state_inventory`: up to 120 operator lines, treated as
       ground truth by the EXISTING-FEATURE GATE
     - a PREVIOUSLY-PROPOSED block: up to 120 titles from the accumulator,
       so the model does not re-emit them

   How the calls run:
   - Competitors are split into chunks of
     `analyzer.compare_batch_competitors` (default 2), with
     `per_chunk_recs = max(2, ceil(max_recs / chunks))`.
   - Each chunk makes one call:
     `self.ai_chat(COMPARE_SYS, temperature=0.2, max_tokens=6000,
     timeout=analyzer.compare_timeout_s (default 1800), max_turns=8)`.
     `max_turns=8` was added in `7d760fd`; with `max_turns > 1` the
     claude-cli provider uses stream-json output. That call goes
     through the agent's default provider with the framework fallback
     chain. The instance manifests note that the default is
     `claude-cli / claude-sonnet-4-6`, which the 2026-09-23 logs confirm.
   - Titles are de-duplicated across chunks, and the loop stops once
     `max_recs` is reached.

   Results:
   - The raw responses are saved to `compare-raw.txt`.
   - If every chunk fails, the run returns `failure`: "LLM compare
     failed: all N chunk(s) errored".
   - If the calls succeed but no recs parse, the agent logs a
     `compare-empty` decision.
8. **Score and tier**:
   - The tier is `score_tier(confidence, severity,
     threshold=analyzer.auto_implement_threshold)` (default 0.95). An LLM
     `tier_recommendation: experimental` always wins.
   - The agent keeps `user_story`, `blueprint`, `success_metrics` and
     `expected_impact`, plus `implementation_outline.approach` (taken from
     `fix_suggestion`).
   - Recs are sorted by severity, then tier, then confidence, capped at
     `max_recs_per_run` (default 15 here), and assigned rec ids.
9. **Accumulator**. Both steps run under
   `framework.core.locks.accumulator_lock`.
   - **Replies.** User replies are mapped through the previous run's
     `rec-id-to-proposal-id.json`: `implement`/`ship` become
     `implemented`, `skip` becomes `skipped` and `defer` becomes
     `deferred`.
   - **Merge.** This run's recs are then merged into
     `agents/<agent_id>/proposals/active.json`.
     - The key is `proposal_id = sha1(normalised title)[:16]`.
     - Closed proposals are never reopened.
     - Every re-seen proposal gets `times_seen` incremented; only open
       ones also get their fields refreshed.
10. **`recommendations.json`** (this run's new recs only):
    1. Producer-history dedup.
    2. `stamp_implementer_safety(dispatch_kind="comp-research")`.
    3. `review_required: true` on every rec that lacks
       `confirmed_for_implementation`.
    4. Schema validation, then save, then `record_emitted_proposals`.
11. **Email**:
    - The body is built from the **whole open backlog**. The agent
      applies `open_proposals(max_open=analyzer.max_open_proposals)` (sorted
      by severity, then tier, then confidence), then
      `reporter.email.backlog_cap` (default 50), and renumbers the result
      as `rec-NNN`.
    - The number map is saved as `rec-id-to-proposal-id.json`.
    - The mail is sent with `send_via_msmtp(bypass_digest=True)`, which is
      a direct send (Graph first, then msmtp) that skips the digest.
    - The outbound record, including the actual transport, is written to
      `agents/<agent_id>/outbound-emails/<request_id>.json`.
12. **Dispatch gate**: `dispatch.gated_dispatch_now(subject_tag="competitor-research")`
    with this run's rec ids (the pre-dedup list; skipped when empty). Both instances set `auto_implement: false`,
    so this returns `None` and **nothing is dispatched**. The call only
    logs "awaiting email approval".
    - If `auto_implement` were `true`, *all* of this run's rec ids would be
      sent directly, including `review` and `experimental` recs.
    - Nothing on the direct dispatch path checks `review_required`. See
      README.md → "How recs reach the implementer".
13. `RunResult(success)` with the metrics listed below. `next_state`
    holds `last_run_ts`, `last_request_id`, `site_id` and
    `competitors_used`.

## Inputs

| Input | Detail |
|---|---|
| Site config | `COMPETITOR_RESEARCH_CONFIG` → instance `site.yaml` |
| Our site | Public HTTP crawl of `site.base_url` (or a local repo in `codebase` mode) |
| Competitor sites | Public HTTP crawl, UA `…ReusableAgentsCompetitorResearch/1.0…` |
| LLM | `ai_client(call="extract")` / `call="brainstorm"` resolve through the instance manifest `metadata.ai_calls` to `ollama-local`. Since 2026-09-23 every local call runs the fleet model `qwen3.8:27b` (`FLEET_LOCAL_MODEL`) with `think:false` at `num_ctx` 65536. `_OllamaClient` rewrites any other manifest model (see `framework/core/local_llm.py`). Compare uses the agent default |
| Backlog | `agents/<agent_id>/proposals/active.json` |
| Replies | `agents/<agent_id>/responses-queue/` + prior run's `rec-id-to-proposal-id.json` |

The agent reads no site database.

## Outputs

| Output | Where |
|---|---|
| Run artifacts | Local run dir + storage `agents/<agent_id>/runs/<run_ts>/`. The files are `competitors.json`, `features-ours.json`, `features-theirs.json`, `compare-raw.txt`, `recommendations.json`, `rec-id-to-proposal-id.json` and `email-rendered.html`. `codebase-scan.txt` and `app-stores-*.json` are written only when those modes are used |
| Backlog | `agents/<agent_id>/proposals/active.json` (states `open` / `implemented` / `deferred` / `skipped`) |
| Emitted-title history | `agents/<agent_id>/state/emitted-titles.json` |
| Email | Direct send (`bypass_digest=True`). Record at `agents/<agent_id>/outbound-emails/<request_id>.json`. On 2026-09-23 the aisleprompt record shows `transport: graph`, `graph:send_as`, `ok: true` |
| Rec routing | Recs carry `review_required: true`. `backlog-dispatcher-agent` walks both instances (`PRODUCER_AGENT_IDS`) but skips any `review_required` rec until `confirmed_for_implementation` is true. That happens through the dashboard Approve button or an `implement rec-NNN` email reply handled by `responder-agent`. The dispatch kind is `comp-research`. Caveat (code reading, unverified live): the email numbers the whole backlog (mapped by `rec-id-to-proposal-id.json`), while the responder's approval flip matches `rec-NNN` against a run's `recommendations.json` ids, which number only that run's new recs, so an email reply may not approve the intended proposal |
| Implementer config | Dispatches via `framework.core.dispatch` set `RESPONDER_SITE` but not `SEO_AGENT_CONFIG`, so `implementer/run.sh` loads `reusable-agents/examples/sites/<site>.yaml` rather than the instance `site.yaml` (observed for a catalog-audit dispatch on 2026-09-23). The instance `implementer.*` block is therefore not the scope applied |
| RunResult | `success` for a normal run. `blocked` when no competitor pages were crawled (6b). `failure` when the AI client is missing or every compare chunk failed. The systemd unit exits 0 in all of these cases; read the log status line |

**`RunResult.metrics` keys:** `competitors_analyzed` (the length of the
competitor feature list), `recs_total`, `recs_auto`, `recs_review`,
`recs_experimental`, `applied_responses`, `awaiting_user_reply` (equal to
`recs_total`).

## Goals & metrics

The engine has no `goals.json` and is not registered. The instances'
goals live in storage. See each instance runbook. They are the same six
ids:

- `goal-competitor-pages-analyzed-30d`
- `goal-content-gaps-found`
- `goal-competitor-coverage`
- `goal-feature-parity`
- `goal-unique-advantages`
- `goal-ux-improvements`

They bind to `competitors_analyzed`, `recs_total` and `recs_auto`.
`agent-metrics-collector` (`m_competitor_research`) also writes
`goal-competitor-pages-analyzed-30d` (rec count × 5, labelled "rough" in
code) and `goal-content-gaps-found` (rec count).

## Configuration

**Environment**

| Name | Default | Meaning |
|---|---|---|
| `COMPETITOR_RESEARCH_CONFIG` | none (required) | Instance `site.yaml` path |
| `AGENT_ID` | `competitor-research-agent` | Set by the unit to the instance id |
| `AGENT_FORCE_RUN` | unset | Generic AgentBase bypass. It has no effect here because there is no `signals()` |

**`site.yaml` keys**

| Key | Default in code | Notes |
|---|---|---|
| `site.{id,domain,label,base_url,what_we_do}` | `base_url` → `https://<domain>` | `what_we_do` is sent to both the brainstorm and compare prompts |
| `competitors.seed_domains` | `[]`, which triggers the LLM brainstorm | Bare domains. A path such as `newegg.com/category` is accepted, but the competitor seeds are absolute (`/`, `/features`, …), so `urljoin` resolves them against the host root and the path has no effect (code reading) |
| `competitors.max_competitors` | 6 | Both instances: 8 |
| `competitors.max_pages_per_competitor` | 4 | Both instances: 4 |
| `crawler.seed_urls` / `use_sitemap` / `max_depth` / `max_pages` / `path_excludes` / `request_timeout_s` / `throttle_ms` / `user_agent` | `["/"]` / true / 1 / 12 / `[]` / 15 / 500 / built-in UA | Our-site crawl. Both instances: 20 or 13 seed URLs, `max_pages: 30`, timeout 20, throttle 600 |
| `current_state_inventory` | `[]` | Operator ground truth for the EXISTING-FEATURE GATE. The first 120 lines are used |
| `analyzer.max_recs_per_run` | 8 (compare) / 15 (scoring) | Both instances: 15 |
| `analyzer.auto_implement_threshold` | 0.95 | |
| `analyzer.max_open_proposals` | unset (unbounded) | Caps the emailed/visible top-N only. The accumulator keeps growing. Both instances: 10 |
| `analyzer.compare_batch_competitors` | 2 | **Not in the schema.** `analyzer` has `additionalProperties: false`, so adding this key to `site.yaml` fails validation at setup. Add it to the schema first |
| `analyzer.compare_timeout_s` | 1800 | Same schema caveat as above |
| `reporter.email.{to,from,msmtp_account,subject_template,backlog_cap}` | `msmtp_account`: `automation`; `backlog_cap`: 50 | |
| `auto_implement` | `gated_dispatch_now` treats a missing value as true | Both instances: `false` |
| `implementer.*` | | Path scope for approved recs |
| `scan_mode` / `codebase.*` | `website` | `codebase` mode reads `repo_path`, `include_globs`, `exclude_globs`, `max_files` (30), `max_chars_per_file` (5000) and `feature_summary_files` |
| `app_stores.{country,ios_app_id,android_package,competitors.ios,competitors.android}` | unset | See the caveat in phase 6 |
| `runs_root` | `~/.reusable-agents/<agent_id>/runs` | |

The instance manifest's `metadata.ai_calls.{extract,brainstorm}` sets the
provider and model per call. The compare step is deliberately not routed
there; the manifest `_compare_note` explains that its blueprints ship to
production.

## Short-circuit & idempotency

- The code has **no** `signals()`. A comment explains that competitor
  pages can change at any time. The TODO is `partition_by_hash` per
  competitor after the crawl.
- Idempotency across runs comes from three places:
  - The accumulator: title-hash `proposal_id`, and closed states that are
    never reopened.
  - The PREVIOUSLY-PROPOSED prompt block.
  - Producer-history dedup on `recommendations.json`.
- Nothing is dispatched automatically while `auto_implement` is false.
- `_backfill_accumulator.py` is a one-shot, re-runnable seeding tool. It
  takes `--dry-run` and `--agent <id>`. It needs `STORAGE_BACKEND`,
  `AZURE_STORAGE_CONNECTION_STRING` and `AZURE_STORAGE_CONTAINER` in the
  environment. It rebuilds `proposals/active.json` from historical run
  recs.

## Running & inspecting

```bash
systemctl --user start agent-aisleprompt-competitor-research-agent.service
systemctl --user start agent-specpicks-competitor-research-agent.service
tail -f /tmp/reusable-agents-logs/agent-<instance-id>.log

# what the last run actually saw (competitor list, features, raw compare)
d=$(ls -d ~/.reusable-agents/competitor-research-agent/runs/<site>/*/ | tail -1)
cat "$d/competitors.json"; python3 -m json.tool "$d/features-theirs.json" | head -50
head -c 2000 "$d/compare-raw.txt"

curl -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://localhost:8090/api/agents/<instance-id>
```

There is no dry-run flag. A manual run crawls, calls the LLMs and sends
the email.

## Failure modes & troubleshooting

All of the following were verified from logs, run artifacts or code on
2026-09-23 unless a different date is given.

### Incident 2026-09-18 → 2026-09-23: fixed in `7d760fd`

The commit landed on 2026-09-23 at 13:59 local (17:59Z). It fixed four
defects. Each one let runs finish without a `failure` status while
producing no useful output. At the time of writing, no scheduled run had
yet exercised the committed code.

- **The crawler dropped apex→www redirects.**
  - **When it started.** Commit `1f460cc` (2026-09-18) began reporting
    redirected pages at their final URL.
  - **What broke.** `_same_origin` compared netlocs exactly. An apex that
    301s to `www.` (rtings.com, tomshardware.com, logicalincrements.com,
    and so on) therefore yielded zero pages.
  - **Evidence.** On 2026-09-23, `features-theirs.json` held 1 of 8
    competitors for aisleprompt (only `jow.fr`) and 0 of 8 for specpicks.
    The commit message says specpicks went from 5 competitors analyzed on
    2026-09-17 to 0 afterwards.
  - **Fix.** Apex and `www.` now count as one site.
- **Seed domains were mangled by `lstrip`.**
  - **What happened.** `str.lstrip("https://")` strips a character set,
    not a prefix.
  - **Evidence.** The 2026-09-23 `competitors.json` files list
    `aprika.app`, `lantoeat.com` and `amsungfood.com` (aisleprompt), and
    `cpartpicker.com` and `omshardware.com` (specpicks).
  - **Fix.** A prefix regex replaced the `lstrip` call.
- **The compare step ran against nothing.**
  - **What happened.** With no competitor features, the chunker fell back
    to `[[]]`, and the LLM still emitted parity-gap recs.
  - **Evidence.** The off-schedule specpicks run started at 17:34Z
    logged at 17:56Z: "Compared SpecPicks against 0 competitors. 7 new
    candidate(s) this run". The commit
    message also cites 6 such recs on 2026-09-19. Those recs sit in the
    specpicks accumulator; consider `skip`ping them.
  - **Fix.** The run now returns `blocked` (phase 6b).
- **`Reached max turns (1)` → "LLM compare failed: all N chunk(s)
  errored".**
  - **Evidence.** The specpicks run started 06:30 local and the
    aisleprompt run started 10:04 local on 2026-09-23. claude-cli spends
    its first turn on a tool call, so the default `max_turns=1` stops it
    before it emits any text.
  - **Fix.** `max_turns=8`.

**If `blocked` "No competitor pages crawled…" appears after the fix:**

1. Check the seeds in `site.yaml`.
2. Check whether the competitors block the crawler's user agent. The
   agent sends `…ReusableAgentsCompetitorResearch/1.0…`, and the blocked
   summary itself suggests this check.
3. Look at the per-competitor pages the run actually fetched.

### Other failure modes

- **Our-site feature extraction has never succeeded in the retained
  runs.** Every local run dir (aisleprompt: 127 runs, 2026-08-24 →
  2026-09-23; specpicks: 27 runs, 2026-08-25 → 2026-09-23) has an empty
  `features-ours.json`, so every compare ran with an empty OUR FEATURES
  block and relied on `current_state_inventory` alone. Competitor
  features were non-empty in only 9 aisleprompt runs and 1 specpicks run.
  The recorded causes are `(parse failed)` (the most common),
  `timed out`, `generator didn't stop after throw()`,
  `[Errno 7] Argument list too long` and, on 2026-09-23,
  `ollama unreachable at http://127.0.0.1:11434 (provider=ollama-local)`
  (aisleprompt 03:11Z–09:04Z, specpicks 10:30Z). The manifest `_why`
  explains that `qwen3:14b` (the extract model until 2026-09-23) under-read
  large batched prompts: lower `max_pages_per_competitor` or chunk per
  competitor. Check `features-ours.json` before trusting any parity rec.
- **Truncated or odd compare output → few or 0 recs.**
  - **Evidence.** On 2026-09-23 the aisleprompt `compare-raw.txt` began
    with "Continuing from the cut —…" (03:11Z and 09:04Z runs) and with
    the claude-pool line "Failed to authenticate: OAuth session expired…"
    (04:04Z run).
  - **Effect.** The 03:11Z run parsed 0 new candidates; the 09:04Z and
    04:04Z runs still parsed 2 and 5.
- **claude-pool auth.** The logs show `profile-4 auth-dead … OAuth
  session expired`, followed by failover to other profiles. Re-auth is an
  operator action; see KTLO.
- **The backlog never drains.** `proposals/active.json` holds **2,194**
  open proposals for aisleprompt and **1,218** for specpicks (after the
  specpicks 17:56Z run), with 0 implemented, deferred or skipped. Only the
  top 10 are emailed.
- **Setup rejects the config.** An unknown key under `analyzer`,
  `codebase`, `competitors`, `crawler`, `reporter` or `site` fails schema
  validation.
  Top-level extras such as `current_state_inventory` and `app_stores` are
  allowed.
- **Crawler noise.** `XMLParsedAsHTMLWarning` from
  `progressive-improvement-agent/crawler.py` is harmless.
- **History.** The following fixes are in git:
  - The JSON parse fix (2026-05-07). Before it, both instances emitted 0
    features every run.
  - The JSON-LD blindness fix (`328b11a`, 2026-05-31).
  - The EXISTING-FEATURE GATE + `current_state_inventory` (`3990848`,
    2026-05-31). These stop hallucinated "add pantry tracking" style
    parity recs.
  - The accumulator lock (`60981ef`, 2026-05-20).
  - Extraction routed separately from blueprinting (`88e4c17`,
    2026-08-25).

## Related agents

- **Instances**:
  - `aisleprompt: agents/competitor-research-agent/README.md`
  - `specpicks: agents/competitor-research-agent/README.md`
- **Shared code**:
  - `agents/progressive-improvement-agent/crawler.py`
  - `shared/site_quality.py`
  - `framework/core/{dispatch,locks,producer_history,implementer_safety,app_store_clients}.py`
- **Downstream**:
  - `responder-agent` (email replies)
  - `backlog-dispatcher-agent` (releases confirmed recs)
  - `implementer`
  - `specpicks-competitor-gap-consumer` (specpicks repo, every 4 h). It
    reads the latest specpicks run's `recommendations.json` and inserts
    topics with confidence ≥ 0.6 into `editorial_topics`
    (`source='competitor-research'`). On 2026-09-23 it logged "No
    competitor-research runs found." because it reads the local path
    `$FRAMEWORK_STORAGE_ROOT/agents/specpicks-competitor-research-agent/runs`
    (default root `~/.reusable-agents/storage`), which does not exist on
    this host, rather than framework storage.
- **Metrics**: `agent-metrics-collector`.

`SKILL.md` in this directory is an older, simplified description of the
two LLM jobs. The live prompts are the `EXTRACT_FEATURES_SYS`,
`EXTRACT_FEATURES_BATCH_SYS` and `COMPARE_SYS` constants in `agent.py`.
`COMPARE_SYS` additionally requires `user_story`, `blueprint.*` and
`success_metrics`, and enforces the EXISTING-FEATURE GATE.
