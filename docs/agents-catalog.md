# Agents Catalog

This catalog lists every scheduled agent on the fleet host: what each one does, where
it lives, when it runs, and how it was doing on the verification date. It also covers
the shared engines behind the per-site instances and the agent directories that are
not registered.

**Last verified: 2026-09-23.** Sources: `systemctl --user` (69 `agent-*.timer` units),
the framework API registry at `http://localhost:8090/api/agents` (85 entries), each
agent's `manifest.json`, `site.yaml` and code, and the run logs in
`/tmp/reusable-agents-logs/`. The previous edition (2026-05-13) was missing 52 of the
69 timers. If something here disagrees with systemd or the code, the code wins. Fix the
row and re-verify it with the commands in [Keeping this catalog accurate](#keeping-this-catalog-accurate).

To add an agent, start from a blueprint ([`../blueprints/README.md`](../blueprints/README.md))
and the scaffold script described in `CLAUDE.md`.

## Contents

- [How to read this](#how-to-read-this)
- [How the fleet runs](#how-the-fleet-runs)
- [Registered agents by function](#registered-agents-by-function) (all 69 timers)
  - [SEO, indexing & competitive research](#seo-indexing--competitive-research) (15)
  - [Site quality & user feedback](#site-quality--user-feedback) (7)
  - [Editorial & content authoring](#editorial--content-authoring) (8)
  - [Catalog & commerce](#catalog--commerce) (16)
  - [Images](#images) (6)
  - [Growth & analytics](#growth--analytics) (7)
  - [Ops / fleet](#ops--fleet) (7)
  - [Research & personal](#research--personal) (3)
- [Shared engines and their instances](#shared-engines-and-their-instances)
- [Unregistered directories and registry-only entries](#unregistered-directories-and-registry-only-entries)
- [Fleet-wide open issues](#fleet-wide-open-issues-2026-09-23)
- [Quick lookup](#quick-lookup)
- [Keeping this catalog accurate](#keeping-this-catalog-accurate)

---

## How to read this

- **One row per `agent-<id>.timer`.** The id is the registered agent id (and the systemd
  unit name). It often differs from the directory name. For example,
  `specpicks-amazon-catalog-freshness` lives in `specpicks/agents/amazon-catalog-freshness-agent/`.
- **Repo** is the repo that holds the manifest and the directory the timer runs in.
  "nsc-assistant (wrapper)" means a small `run.sh` in `nsc-assistant/agents/<id>/` that
  sets env vars and execs engine code in reusable-agents. nsc-assistant is read-only
  reference for this catalog.
- **Schedule times are host-local (America/Detroit).** `framework/core/scheduler.py`
  writes `OnCalendar=` without a zone and ignores the manifest `timezone`. A manifest
  that says `0 13 * * * UTC` therefore fires at 13:00 Detroit time. Rows note this where
  the manifest disagrees. All six image timers are overridden by host-only drop-ins
  (`~/.config/systemd/user/agent-<id>.timer.d/20-stagger-gpu.conf`, 2026-09-16). The
  drop-ins put GPU consumers on separate minutes because the six RTX 5090 bus drops
  (Xid 79) from 2026-08-28 to 2026-09-15 all happened inside the :00/:15/:30 fan-out
  (per the drop-in's own comment). The GPU dropped again on 2026-09-23 at 13:16:48 EDT,
  after the stagger was in place, and is still off the bus (see
  [Fleet-wide open issues](#fleet-wide-open-issues-2026-09-23)). `digest-rollup-agent` is overridden by
  `10-daily.conf`. No other agent timer has a drop-in.
- **Timer on/off** is `systemctl --user is-enabled agent-<id>.timer`. On 2026-09-23, 67
  were enabled and 2 disabled (`oauth-heartbeat-agent`, `specpicks-scraper-watchdog`).
- **State** is a snapshot from 2026-09-23, taken from run logs, the API and the agent's
  runbook. It is not live. "Green but empty" is this fleet's most common failure: a unit
  exits 0 and produces nothing. The State column says so where that is happening.
- **Log coverage.** `/tmp/reusable-agents-logs/` is wiped at every reboot. The host last
  booted at 2026-09-22 23:10 EDT (2026-09-23 03:10Z), so log-derived counts in the State
  column cover only the time since then. Counts over longer spans come from the API run
  history.
- **Runbook** links are relative for reusable-agents paths. Other repos are given as
  `repo: path`.

---

## How the fleet runs

This catalog was verified on the machine whose `systemctl --user` manager runs the
`agent-*` units (hostname `voidsstr-OMEN-by-HP-45L-Gaming-Desktop-GT22-3xxx`, native
Linux, TZ America/Detroit). The fleet moved to this native-Linux RTX 5090 host from
`whitebeast` (WSL2, RTX 4080 SUPER) around 2026-08-24, when the fleet units here were
created (`reusable-agents-api.service` at 10:55 EDT, the first `agent-*.timer` files at
10:58–10:59 EDT). This host has no `WSLInterop` entry and runs kernel `7.0.0-31-generic`.
`CLAUDE.md` still describes whitebeast as the current host;
[`fleet-host-standup.md`](fleet-host-standup.md) now has a "Current host" section for this
machine. In per-agent runbooks, "whitebeast" means "the fleet host". The repos are at
`/home/voidsstr/development`. That path is hard-coded in manifests and unit files. Standing up a host is covered in
[`fleet-host-standup.md`](fleet-host-standup.md), and on-call in
[`keep-the-lights-on.md`](keep-the-lights-on.md).

### Scheduled runs

```
agent-<id>.timer            OnCalendar (host-local), Persistent=true unless a drop-in says otherwise
  └─ agent-<id>.service     Type=oneshot; EnvironmentFile=-~/.reusable-agents/secrets.env
                            + global drop-in service.d/10-fleet-path.conf (sets PATH only;
                              nothing in the user manager sets NODE_PATH, although
                              CLAUDE.md says this drop-in carries both)
      └─ framework/agent_run_wrapper.sh <id> <entry_command>
            exports DIGEST_ONLY=1, PYTHONPATH and the claude-pool shim on PATH,
            writes "starting" status, runs the manifest entry_command as a child,
            then writes "success"/"failure" from its exit code
          └─ python3 agent.py  (or a thin run.sh that ends in exec python3 …)
                AgentBase.run_once(): run(), progress.json, run-index, goal progress
                → state and run dirs in framework storage (framework.core.storage;
                  Azure Blob on this host) → read by the framework API and dashboard
stdout/stderr → /tmp/reusable-agents-logs/agent-<id>.log
```

Exceptions to this path:

- **Not AgentBase.** `site-goals-tracker` (both instances), `goals-tracker`,
  `agent-metrics-collector`, both `*-user-growth-strategist`,
  `aisleprompt-conversion-optimizer` and `specpicks-newsletter-digest-sender` are plain
  scripts. They get status from the wrapper, but they get no run-index entries and no
  automatic goal progress. `ebay-product-sync-agent` is AgentBase, but its `main()`
  calls `run()` directly and skips `run_once()`.
- **Separate logs, always exit 0.** The `run.sh` wrappers of both `site-goals-tracker`
  instances, `goals-tracker` and `agent-metrics-collector` append to
  `/tmp/reusable-agents-site-goals.log`, `/tmp/reusable-agents-goals-tracker.log` and
  `/tmp/reusable-agents-metrics-collector.log` instead of the agent log. They end the
  Python call with `|| echo "(… failed)"`, so the unit and the API report success even
  when the script fails. Read those files, not the API status.
- **`sessions-save`** is a hand-written unit (`OnUnitActiveSec=5min`) that does not use
  the wrapper and is not in the framework registry. A companion hand-written
  `agent-sessions-shutdown.service` (enabled, no timer) runs the same save at logout
  via `ExecStop=`.
- **The dashboard.** Nothing listened on :8091 (local UI) or :8093 on this host on
  2026-09-23. `install/register-agent.sh` defaults `FRAMEWORK_API_URL` to
  `http://localhost:8090`, the only framework port that is listening.

### Recommendations → implementer → deployer

Producers write `recommendations.json` into their run dir. Recs reach the implementer
by four routes:

```
producer run dir (recommendations.json)
 ├─ backlog-dispatcher-agent, every minute: scans the run-index of 13 hard-coded
 │    producers (PRODUCER_AGENT_IDS) and calls dispatch_now()
 ├─ direct: the producer calls gated_dispatch_now()/dispatch_now() at the end of run()
 │    (catalog-audit, shelf-audit, user-growth-strategist, specpicks-article-proposal,
 │    head-to-head, …)
 ├─ Azure auto-queue  agents/responder-agent/auto-queue/<request-id>.json
 │    (legacy producers such as aisleprompt-article-proposal and conversion-optimizer,
 │    plus dispatch_now's fallback) → drained by auto-queue-drainer.service
 └─ operator email reply → responder-agent (IMAP)  [inert on this host, see below]
            │
            ▼
   site_dispatch_lock → systemd-run --user --scope  agent-dispatch-implementer-<site>-<ts>
            ▼
   implementer  (agents/implementer: agent.py wrapping run.sh; claude-pool; Opus-required
                 kinds defer instead of downgrading)
            ├─ code recs → commit → deployer (runtime id seo-deployer:
            │                test → build → push → deploy → smoke → content verify)
            └─ article / h2h / catalog-audit recs → DB rows (deployer skipped)
```

`gated_dispatch_now()` honours `auto_implement` in `site.yaml`; backlog-dispatcher has
ignored the flag since 2026-05-13 (the check is `if False and …`). Both SEO instances
set `auto_implement: false`, so their recs ship only through backlog-dispatcher. Both
competitor-research instances also set it to false, and the engine tags every rec
`review_required`. Backlog-dispatcher skips those until `confirmed_for_implementation`
is set, which normally comes from an operator email reply, and that path is dead (see
`responder-agent`).

Operator mail: most agents queue to `digest-queue/` and `digest-rollup-agent` sends one
email a day at 07:30. Failures still mail immediately: `AgentBase` routes only
successful run summaries to the digest (`AGENT_SUMMARY_DIGEST`, default `1`).
`goals-tracker`, both competitor-research instances, `authority-agent`,
`app-store-opportunity-agent` and the newsletter (to subscribers) send mail directly
(`bypass_digest=True`, or their own sender).

### Long-running services (not timers)

| Unit | What it does | State 2026-09-23 |
|---|---|---|
| `reusable-agents-api.service` | Framework API: `uvicorn framework.api.app.main:app` on 127.0.0.1:8090 (registry, status, triggers, timer writer) | active, enabled |
| `reusable-agents-host-worker.service` | `framework/api/host-worker.sh`: claims "Run now" trigger jobs queued by the API and execs the agent's `entry_command` on the host | active, enabled |
| `auto-queue-drainer.service` | `python3 -m framework.cli.auto_queue_drainer --interval 15 --idle-backoff 60`: drains the Azure auto-queue through `responder.drain_auto_queue()` (15 s when busy, 60 s after 5 min idle) | active, enabled |
| `retro-chat-daemon.service` | `nsc-assistant/agent/tools/retro_chat_daemon.py`: LAN bridge for retro agents | active, enabled |
| `retro-chat-brain.service` | `retro-agent/scripts/retro_chat_brain.py`: Claude Agent SDK processor for the retro fleet | active, enabled |
| `local-image-gen.service` | SDXL-Turbo text-to-image daemon on :7861 (`services/local-image-gen/`) | **inactive, disabled**, so `aisleprompt-recipe-image-refiller` is blocked |
| `retro-autodeploy`, `retro-dosgames-http`, `retro-gameindex`, `retro-gameservers-watch` | Retro-fleet support services (title deploy, DOS games HTTP bridge, game/server index, game-server watchdog) | active, enabled |
| system `ollama.service` | Local LLM and vision models used by several agents | active since 2026-09-23 10:02 EDT, but not enabled at boot, so it was down from the 23:10 EDT reboot until then (ollama-dependent runs such as `specpicks-ebay-product-sync-agent` produced 0 in that window). Since 2026-09-23 13:16:48 EDT the RTX 5090 is off the PCI bus (kernel Xid 79, then Xid 154 "Node Reboot Required"). `nvidia-smi` shows no devices, and ollama runs CPU-only (`/api/ps` shows qwen3:8b and qwen3:14b with `size_vram: 0`; the journal logs `ggml_cuda_init: failed to initialize CUDA`) |
| docker container `searxng` | SearXNG on 127.0.0.1:8888, used by the hero-image and recipe-image agents | up |

Nothing listened on :4141 (the Copilot proxy used by the implementer's opt-in Opus
bridge) on 2026-09-23.

### Other host timers (not framework agents)

| Timer | Schedule | What it runs |
|---|---|---|
| `site-consistency-audit.timer` | 05:40 and 17:40, up to 15 min random delay | `framework.cli.site_consistency_audit` crawls both production sites and reports anything that looks unprofessional |
| `specpicks-visible-categorizer.timer` | 04:20, up to 10 min random delay | specpicks `scripts/ai-categorize-products.ts --visible-only`: re-scores `category_confidence` for user-visible products |
| `touch-agent-manifests.timer` | Sun 03:40, up to 15 min random delay | `framework.cli.touch_manifests`: resets the blob lifecycle clock on `agents/<id>/manifest.json` so Azure never archives live config |

---

## Registered agents by function

Column key: **Sched** = schedule (host-local), then timer on/off.

### SEO, indexing & competitive research

| Agent | Repo | Sched | Purpose | State 2026-09-23 | Runbook |
|---|---|---|---|---|---|
| `aisleprompt-seo-opportunity-agent` | aisleprompt | every 2 h at :15 · on | Instance of the [SEO engine](#seo-engine-phases). Pulls GSC, GA4, DB and live-site signals, runs rule passes and an LLM page audit, writes up to 12 recs (shipped by backlog-dispatcher) and queues an HTML report to the digest | Last 50 runs succeeded. robots.txt fetch gets a 403 (no User-Agent). Handoffs to generic handler ids are dead-lettered | `aisleprompt: agents/seo-opportunity-agent/AGENT.md` |
| `specpicks-seo-opportunity-agent` | specpicks | every 3 h at :30 · on | Same engine for specpicks.com | Last 50 runs succeeded. site.yaml has no implementer `allowed_paths`. `revenue_kpis` measure nothing. The stale `article-author-agent` handoff override means those handoffs are dead-lettered | `specpicks: agents/seo-opportunity-agent/AGENT.md` |
| `aisleprompt-gsc-coverage-auditor` | aisleprompt | daily 11:30 (manifest says UTC) · on | Resubmits sitemaps, URL-inspects the 150 least-recently-inspected URLs and appends coverageState to a local JSONL that the SEO analyzer turns into indexing-fix recs | Runs fine, but only 0.04% of 5,003 inspected URLs are indexed and 70.36% are unknown. That is a site problem | `aisleprompt: agents/gsc-coverage-auditor/AGENT.md` |
| `specpicks-gsc-coverage-auditor` | nsc-assistant (wrapper) | daily 12:00 (manifest says UTC) · on | Same engine; `run.sh` sets `GSC_INSPECT_SITE=specpicks` | 16:18Z: 1,039 URLs inspected in the last 7 days; 29.79% indexed, 30.78% unknown (universe 5,428) | [agents/gsc-coverage-auditor/AGENT.md](../agents/gsc-coverage-auditor/AGENT.md) |
| `aisleprompt-indexnow-submitter` | aisleprompt | every 15 min · on | POSTs watermarked DB URLs, the always-on category and cuisine sets and sitemap-delta URLs (about 270 per tick) to api.indexnow.org, then HEAD-checks sitemaps and resubmits them to GSC | 63 ticks between the 03:10Z reboot and 18:31Z: 269 to 403 URLs each, failed=0 on every tick; sitemap pings 15/15 on 59 ticks (11/13 on 3, 14/15 on 1) | `aisleprompt: agents/indexnow-submitter/AGENT.md` |
| `specpicks-indexnow-submitter` | nsc-assistant (wrapper) | every 5 h at :18 · on | Same engine for specpicks (`INDEXNOW_SITE=specpicks`) | 14:18Z: 3,277 URLs, 0 failed, sitemap pings 3/3 | [agents/indexnow-submitter/AGENT.md](../agents/indexnow-submitter/AGENT.md) |
| `aisleprompt-indexnow-bulk` | aisleprompt | daily 09:00 (manifest says UTC) · on | Full resubmission (`INDEXNOW_BULK=1`): about 197k aisleprompt.com URLs in 20 batches, a coverage check and a sitemap PUT to GSC. Shares its watermark file with the 15-minute submitter | Last 10 runs succeeded, failed=0 | `aisleprompt: agents/indexnow-bulk/AGENT.md` |
| `specpicks-indexnow-bulk` | nsc-assistant (wrapper) | daily 10:00 (manifest says UTC) · on | Same, for specpicks | 14:00Z: 98,764 URLs, failed=0, sitemap pings 3/3 | [agents/indexnow-submitter/AGENT.md](../agents/indexnow-submitter/AGENT.md) |
| `specpicks-schema-fix-specialist` | specpicks | every 2 h at :40 · on | Intended to repackage structured-data recs from the specpicks SEO agent as `dispatch_kind=schema-fix` recs targeting `src/services/ssrHead.ts` | **No-op.** It reads a local path that does not exist and reports a green "No specpicks-seo-opportunity-agent runs." with forwarded=0. Fixing or retiring it is an operator decision | `specpicks: agents/schema-fix-specialist/AGENT.md` |
| `specpicks-internal-link-densifier` | specpicks | daily 04:30 · on | Pure SQL, no LLM. Appends each tagged article from the last 7 days to `related_article_slugs` on up to 5 older same-vertical peers (lists capped at 12). The SSR renders these as "Recommended reading" | Last 50 runs succeeded; about 170 peer links across 34 articles per day | `specpicks: agents/internal-link-densifier/AGENT.md` |
| `authority-agent` | reusable-agents | daily 13:00 · on | Reads the `gsc_crawl_progress` indexation snapshot (specpicks by default), ranks the 40 most citable published articles and emails the operator a top-10 link-building worklist (`AUTHORITY_WORKLIST_SIZE`). It writes no recs and no DB rows | Runs succeed, but the input has been frozen since 2026-07-28 because nothing on the host (no timer, no crontab entry) schedules specpicks `scripts/gsc-crawl-tracker-cron.sh` | [agents/authority-agent/AGENT.md](../agents/authority-agent/AGENT.md) |
| `specpicks-search-demand-agent` | specpicks | 05:35 and 17:35 (manifest `cron_timezone: America/Detroit`) · on | Deterministic, no LLM. Distils 28 days of GSC and GA4 data into demand topics at `framework/demand-signal/specpicks.json`, which specpicks-article-proposal-agent injects as its PROVEN SEARCH DEMAND block | All runs 2026-09-13 to 09-23 succeeded. 09:35Z: 20 topics, uncovered=4, h2h_hot=10. `steered_published_7d` has been stuck at 0 since 2026-09-21 | `specpicks: agents/search-demand-agent/AGENT.md` |
| `aisleprompt-competitor-research-agent` | aisleprompt | every 5 h at :04 · on | Compares aisleprompt.com with 8 meal-planning competitors. Emails the top 10 open, blueprinted proposals from a cross-run backlog for approval (`auto_implement: false`) | **Degraded** 2026-09-18 to 09-23 (1 of 8 competitors crawled; the 14:04Z run failed with max_turns=1). Engine fix 7d760fd landed at 17:59Z and no scheduled run has confirmed it yet. 2,194 open proposals | `aisleprompt: agents/competitor-research-agent/README.md` |
| `specpicks-competitor-research-agent` | specpicks | daily 06:30 · on | Same engine, against 8 PC-hardware review and retail sites | **Degraded** 2026-09-18 to 09-23 (0 competitors crawled). Fix 7d760fd not yet confirmed. About 1,218 open proposals | `specpicks: agents/competitor-research-agent/README.md` |
| `specpicks-competitor-gap-consumer` | specpicks | every 4 h at :00 · on | Intended to forward high-confidence competitor-research proposals into `editorial_topics` (source='competitor-research') | **Broken no-op.** 50 of 50 runs report "No competitor-research runs found." because it reads a local path instead of framework storage | `specpicks: agents/competitor-gap-consumer/AGENT.md` |

### Site quality & user feedback

| Agent | Repo | Sched | Purpose | State 2026-09-23 | Runbook |
|---|---|---|---|---|---|
| `aisleprompt-progressive-improvement-agent` | aisleprompt | every 2 h at :45 · on | BFS-crawls up to 40 pages. Audits the changed pages with claude-sonnet-4-6 (claude-cli) in batches of 4, against the base prompt plus 75 site QA rules, using a page-hash cache. Writes up to 15 ranked recs for backlog-dispatcher and hands miscategorisations to aisleprompt-catalog-audit-agent | All runs succeeded; the latest covered 40 pages with 6 recs and a quality score of 85. `goals/changes.jsonl` is in the Azure archive tier | `aisleprompt: agents/progressive-improvement-agent/README.md` |
| `specpicks-progressive-improvement-agent` | specpicks | daily 05:30 · on | Same engine: up to 80 pages and 190 site QA rules (pricing integrity, retro eBay CTAs, schema, soft-404s, category drift), with an SSR-only path scope | Succeeded: 80 pages, 15 recs, score 81.25, about 82 min | `specpicks: agents/progressive-improvement-agent/README.md` |
| `aisleprompt-feedback-triage-agent` | aisleprompt | every 20 min · on | Atomically claims `new` rows from the "Feedback" widget table and the older `feedback` table. Deterministic regex rules (no LLM) drop test rows, praise, feature requests and vague reports. Real defects become `user-feedback-defect` recs for backlog-dispatcher | Every tick claimed 0. No goals declared | `aisleprompt: agents/feedback-triage-agent/README.md` |
| `specpicks-feedback-triage-agent` | specpicks | every 20 min · on | Same engine, specpicks "Feedback" widget table | Every tick claimed 0, and no specpicks rec has ever been produced. No goals | `specpicks: agents/feedback-triage-agent/README.md` |
| `specpicks-site-functional-tests` | specpicks | 00:40 and 12:40 · on | Runs the specpicks Playwright suite (about 195 tests) against production with workers=2 and a fail budget of 3. No LLM | **Red on every run since 2026-08-29 17:41 UTC** (retro-consoles catalog-quality tests, with periodic amazon-cta spikes). No goals | `specpicks: agents/site-functional-tests/README.md` |
| `specpicks-content-accuracy-auditor` | specpicks | daily 06:50 · on | Deterministic, report-only scan of about 3,100 published articles for physically impossible LLM tok/s claims, plus an opt-in contradiction pass (`ACCURACY_CONTRADICTIONS=1`). Findings are recorded as run decisions | Succeeded daily 2026-09-02 to 09-22 (the same 3 claims recur). The 2026-09-23 run failed on a transient DB "Connection refused". No goals | `specpicks: agents/content-accuracy-auditor/README.md` |
| `specpicks-stale-content-watcher` | specpicks | daily 03:00 · on | Proposes up to 20 article-refresh recs for published articles not updated in 180 days (`STALE_DAYS`) | **Dormant:** 0 stale rows until about 2026-12-07. The rec path is broken (it calls a nonexistent `write_recommendations`, has no route and is not a backlog-dispatcher producer) | `specpicks: agents/stale-content-watcher/AGENT.md` |

### Editorial & content authoring

All article, news and head-to-head prose is written by the **implementer**, which
requires Opus and defers when Opus is unavailable (`config/required-models.json`). The
agents below only propose topics, discover them or feed data to the implementer.

| Agent | Repo | Sched | Purpose | State 2026-09-23 | Runbook |
|---|---|---|---|---|---|
| `aisleprompt-article-proposal-agent` | aisleprompt | every 8 h at :20 (00:20, 08:20, 16:20); 7 h in-run rerun gate · on | Turns recipe-catalog, kitchen-shop, topic-queue, recipe-cluster-gap, seasonal and trends signals into Opus (claude-cli) article proposals. Dedups them, caps them at 12 per run and queues `article-author-proposal` recs on the Azure auto-queue | 12:20Z: 8 proposals queued. Publish caps are not enforced (cursor KeyError). GSC signals are empty (the runs directory is missing). `articles_indexed_pct` is 0/60 | `aisleprompt: agents/article-proposal-agent/AGENT.md` |
| `specpicks-article-proposal-agent` | specpicks | every 8 h at :45 (00:45, 08:45, 16:45); 7 h gate · on | Turns search-demand, `editorial_topics`, catalog, AI-news and featured-product signals into up to 3 Claude Opus proposals. Filters out published slugs, queues the digest and dispatches straight to the implementer | 12:45Z: 3 proposals (1 buying-guide, 1 maker, 1 trending-ai) dispatched to the implementer. The four "LOCK (expires 2026-09-22)" paragraphs are still in `site.yaml`, so the prompt still pins guides and use-cases to local-LLM hardware, suspends retro and limits maker to local-LLM topics. The GSC goals are stuck at 0 | `specpicks: agents/article-proposal-agent/AGENT.md` |
| `specpicks-head-to-head-agent` | specpicks | every 2 h at :15 · on | Discovers hardware and product pairs into `trending_comparisons`, ranks new pairs and pairs stale for 30 days, and dispatches the top 15 per run. The implementer writes the Opus `comparison_commentary`; this agent makes no LLM call | Implementer runs for its dispatches hit "claude-opus-5 TIMED OUT after 1500s" deferrals and reported 7/15 or 8/15 | `specpicks: agents/head-to-head-agent/AGENT.md` |
| `specpicks-ai-news-aggregator` | specpicks | hourly at :20 · on | Wraps `scripts/scrape-ai-news.ts`: r/LocalLLaMA, the Hugging Face blog, The Decoder and X go into `ai_news_articles`, and TLDR AI goes into `editorial_topics`. A run is blocked when the newest item is more than 6 h old | Blocked 03:11 to 12:20Z (feed 7 to 16 h stale); succeeded from 13:20Z (24 items in 14 days) | `specpicks: agents/ai-news-aggregator/AGENT.md` |
| `specpicks-trending-topic-pipeline` | specpicks | hourly at :10 · on | Runs `scrape-trending-topics.ts` and then `research-trending-topics.ts` (local Ollama qwen3:14b) to keep `editorial_topics` stocked. A run is blocked when the newest topic is more than 4 h old | All runs succeeded. Research is the bottleneck at 1 to 2 topics per run. Goals are not seeded in storage | `specpicks: agents/trending-topic-pipeline/AGENT.md` |
| `aisleprompt-recipe-generator-agent` | aisleprompt | 02:00, 03:00, 04:00, 05:00, 06:00 · on | Generates recipes with an LLM into `recipe_catalog`, planned from category deficits, keyword gaps and cuisine-hub index-floor gaps. Output passes prompt-leak gates and near-duplicate dedup | **No-op.** Runs generate 0–5 recipes (5 each on 2026-09-23; 4 of 5 generated 0 on 2026-09-22) and insert 0; nothing inserted since 2026-09-16 (cuisine-gap ordering bug) | `aisleprompt: agents/recipe-generator-agent/AGENT.md` |
| `trending-recipe-discovery` | aisleprompt | every 6 h (00:00, 06:00, 12:00, 18:00) · on | Turns Reddit food-sub hot posts and food-filtered Google Trends terms into recipes (claude-sonnet-4-6) and POSTs them to aisleprompt's `/api/admin/recipes/insert-trending` as `is_trending` rows | **Blocked.** All 50 indexed runs (2026-09-11 to 09-23) inserted 0 because `~/.aisleprompt/admin-bearer` is missing, yet each run still spends about 15 Claude generations | `aisleprompt: agents/trending-recipe-discovery/AGENT.md` |
| `aisleprompt-promo-curator-agent` | aisleprompt | Mondays 09:00 · on | Weekly UPSERT of `promo_landing_pages` for 11 dated holidays plus the current season (3 recipe rails, an LLM intro and a hero image), served only to the mobile app via `/api/promos` | **Degraded.** On 2026-09-02 it wrote template copy only (llm_calls=0); the 2026-09-21 run failed on a DB connect timeout | `aisleprompt: agents/promo-curator-agent/AGENT.md` |

### Catalog & commerce

| Agent | Repo | Sched | Purpose | State 2026-09-23 | Runbook |
|---|---|---|---|---|---|
| `aisleprompt-catalog-audit-agent` | aisleprompt | every 5 h at :02 · on | Runs aisleprompt's `scripts/catalog-quality-audit.ts` (31 criteria over `recipe_catalog` and `kitchen_products`), turns criteria that have sample rows into SQL-migration recs and dispatches them (kind `catalog-audit`; the deployer is skipped) | It re-dispatches the same 7 recs every tick: 4 duplicate migration commits on 2026-09-23, and the implementer reports shipped=0, unverified=7 | `aisleprompt: agents/catalog-audit-agent/README.md` |
| `specpicks-catalog-audit-agent` | specpicks | daily 08:30 · on | Runs specpicks' `scripts/audit-product-images.ts`, a text-only claude-haiku-4-5 classifier over a 200-product risk-tiered sweep, and dispatches "mismatch" rows as `image-name-mismatch` recs | **Blind since 2026-09-02.** claude-pool auth failures turn almost every verdict into "unsure", so each run reports a clean result while the sweep cursor keeps advancing | `specpicks: agents/specpicks-catalog-audit-agent/README.md` |
| `catalog-audit-shipped-backfill` | reusable-agents | every 30 min · on | Reads the newest catalog-audit `recommendations.json` per site, checks in the prod DB that implemented migrations took effect (AislePrompt criteria only) and flips verified recs to `shipped:true` | Every run since 2026-09-22 has found 0 candidates, because producer dedup leaves `recommendations.json` empty | [agents/catalog-audit-shipped-backfill/AGENT.md](../agents/catalog-audit-shipped-backfill/AGENT.md) |
| `aisleprompt-shelf-audit-agent` | aisleprompt | every 8 h at :55 · on | Crawls the site plus 42 kitchen-shelf API endpoints and checks each product against the Amazon Creators API (price drift, image identity, stock, title and brand). Report-only (`dispatch_findings: false`) | Last 50 runs succeeded. No goals | `aisleprompt: agents/shelf-audit-agent/README.md` |
| `specpicks-shelf-audit-agent` | specpicks | every 8 h at :25 · on | Same engine. Crawls specpicks.com 3 levels deep and dispatches up to 3 high-severity recs per run (dispatch enabled since 2026-08-30) | 7 of the last 50 runs failed with "shelf discovery found no products", all on the 00:25 tick. No goals | `specpicks: agents/shelf-audit-agent/README.md` |
| `specpicks-product-hydration-agent` | specpicks | every 2 h at :15 · on | Refreshes up to 500 stale Amazon prices (provider brightdata), writes PDP copy (description, pros and cons, FAQ, SEO meta) for 80 products with Claude Opus, and re-curates `is_featured` via `select-featured.py` | Every run 2026-09-19 to 09-23 ended `partial_failure`: about 70 to 78 of 80 products hydrated, but **0 prices refreshed** since at least 2026-08-24 (BrightData HTTP 401 "Token expired") | `specpicks: agents/product-hydration-agent/AGENT.md` |
| `specpicks-ebay-product-sync-agent` | specpicks | hourly at :30 · on | Runs 8 eBay Browse API queries per run, drawn from 413 seeds across 24 retro PC and console categories, and upserts canonical `products` and `ebay_listings` rows that back SpecPicks' eBay CTAs | After the reboot, 12 runs got 0 while ollama was unreachable. Once ollama came back, the 10:30, 11:30 and 12:30 EDT runs upserted 90, 118 and 116 products (success rate 83.33%, 75.64%, 74.36%). The 13:30 EDT run was still going at 14:40 EDT. The listing audit marks every checked listing "ended" (41/41 at 13:30 EDT) | `specpicks: agents/ebay-product-sync-agent/README.md` |
| `specpicks-ebay-counterpart-matcher` | specpicks | every 2 h at :15 · on | Finds a live, affiliate-tagged eBay listing for active Amazon products (featured first, then article-linked, then by id) and caches each match for 3 days in `product_ebay_counterparts`, which product pages read | Live. The queue stalls on products with no match. No goals | `specpicks: agents/ebay-counterpart-matcher/README.md` |
| `specpicks-amazon-catalog-freshness` | specpicks | every 8 h at :20 · on | Runs `scripts/refresh-amazon-catalog.ts` twice. Discover mode searches Creators for hardware names, recent topics and 12 seeds, then upserts the results. Refresh mode refreshes the stalest ASINs, with a BrightData fallback | Succeeds but does little: 5 to 11 keywords, 0 to 1 new products and 0 to 5 refreshed per run. The `freshened` metric is capped at 10 by a parse bug | `specpicks: agents/amazon-catalog-freshness-agent/AGENT.md` |
| `specpicks-amazon-price-verifier` | specpicks | every 6 h at :12 · on | Runs a free amazon.com/dp HTML pass over up to 200 priority products (price, availability, dead-ASIN deactivation), then a Creators/BrightData refresh of up to 200 | 122 to 141 products refreshed per run; `stale_priority` stays at 825 to 978; 0 dead links deactivated | `specpicks: agents/amazon-price-verifier/AGENT.md` |
| `specpicks-category-integrity-agent` | specpicks | every 4 h at :35 · on | Sets `category_id=NULL` on unscored products whose titles contain foreign-domain words and no domain words (up to 500 per run). Writes a `categorisation-pipeline-gap` rec when more than 25% of categorised products were never scored | 0 products de-categorised. The unscored share is 53.3%, so the pipeline rec is re-emitted every run. No goals | `specpicks: agents/category-integrity-agent/README.md` |
| `specpicks-benchmark-research-agent` | specpicks | 03:00, 11:00, 19:00 · on | Asks an LLM with web tools (claude-cli / claude-sonnet-4-6) for cited gaming, local-LLM tok/s and synthetic benchmarks for up to 14 `hardware_specs` rows per run, and inserts them into the `*_benchmarks` tables | 20 of the last 40 runs failed. The DB connection drops during long LLM calls, or the LLM returns "Connection error." immediately | `specpicks: agents/benchmark-research-agent/README.md` |
| `specpicks-youtube-review-agent` | specpicks | every 2 h at :00 · on | Picks 4 high-value products that lack a verified video, searches the YouTube Data API v3, and has Claude Sonnet (claude-pool, with Azure OpenAI as fallback) verify the top 2. Stores the winner in `product_videos` | Last 50 runs succeeded. High-value coverage is 617 of 1,932 (31.9%) | `specpicks: agents/youtube-review-agent/AGENT.md` |
| `aisleprompt-kitchen-scraper` | aisleprompt | every 5 h at :06 · on | Phase 1: `scraper-kitchen/run.sh` discovers products on Amazon and eBay search pages and upserts `kitchen_products` and listings. Phase 2: reprices up to 1,000 of the stalest Amazon listings via the Creators API | 48 of the last 50 runs succeeded. About 850 listings repriced and 0 to 3 new products per run | `aisleprompt: agents/kitchen-scraper/AGENT.md` |
| `aisleprompt-kitchen-instacart-backfill` | aisleprompt | daily 03:30 · on | For up to 200 curated-brand `kitchen_products` with no Instacart listing, calls Instacart's products_link API and inserts `kitchen_product_listings` (source='instacart'). Exits early with no API calls when nothing is missing | Backlog drained: 0 missing, 1,781 Instacart listings | `aisleprompt: agents/kitchen-instacart-backfill/AGENT.md` |
| `specpicks-scraper-watchdog` | specpicks | every 5 min · **off** (manifest `enabled=false` since 2026-08-25) | Would restart the `specpicks_scraper_azure` container via `watchdog.sh`, but only if the `specpicks-scraper:latest` image exists, and it is absent on this host. Also reports 14-day scrape coverage | Disabled. Last run 2026-08-30T15:15Z (coverage 25.97%) | `specpicks: agents/scraper-watchdog/AGENT.md` |

### Images

All image generation must use the local SDXL daemon (`CLAUDE.md` → "Image generation —
local only"). All six image timers run on staggered minutes set by the GPU drop-in
`20-stagger-gpu.conf` (`Persistent=false`), not on the manifest cron.

| Agent | Repo | Sched | Purpose | State 2026-09-23 | Runbook |
|---|---|---|---|---|---|
| `aisleprompt-article-hero-image-curator` | aisleprompt | every 15 min at :03/:18/:33/:48 (drop-in) · on | Vision-checks every published article's hero image (minicpm-v4.5 via Ollama by default). Mirrors images that pass to Azure Blob and replaces failures with an on-topic SearXNG food photo. Per-article backoff runs from 15 min to 72 h | 75 pending, 73 to 74 of them in backoff, 0 replaced. Most ticks short-circuit | `aisleprompt: agents/article-hero-image-curator/AGENT.md` |
| `specpicks-article-hero-image-curator` | specpicks | every 15 min at :09/:24/:39/:54 (drop-in) · on | No vision model. Fills NULL heroes and repairs defective ones (low-res, orphaned, hotlinked, or reused more than 23 times) using a hardware-kind gate, a brand check and a byte-size probe. Also upgrades catalog product images and clears stale `trending_comparisons` images | Since the threshold fix ad3d670 (13:54Z), every tick finds 0 defects. The earlier backlog of 611 was phantom | `specpicks: agents/article-hero-image-curator/AGENT.md` |
| `specpicks-news-hero-image-curator` | specpicks | every 30 min at :13/:43 (drop-in) · on | Replaces NULL, product-fallback or Reddit-thumbnail heroes on published news articles with a SearXNG image verified by minicpm-v4.5, mirrored to Azure Blob as `news-images/<slug>.jpg` | Idle: 0 pending on every tick; the last replacement was on 2026-08-29 | `specpicks: agents/news-hero-image-curator/AGENT.md` |
| `aisleprompt-recipe-image-refiller` | aisleprompt | every 5 min at :02/5 (drop-in) · on | Fills `recipe_catalog` rows that have a NULL `image_url`: SearXNG candidates first, then local SDXL-Turbo (:7861). Each image is vision-verified before upload | **Blocked on every run since the 03:10Z reboot** (185 by 18:32Z): `local-image-gen.service` is disabled, so :7861 refuses connections, and 40 recipes are waiting | `aisleprompt: agents/recipe-image-refiller/AGENT.md` |
| `aisleprompt-recipe-image-verifier` | aisleprompt | every 10 min at :01/10 (drop-in) · on | Vision-verifies recipe images that are set but not yet verified (calls go to Azure vision-mini). Images that pass are uploaded and stamped; rejects are NULLed for the refiller. Also NULLs `source_name` values that are not on the allowlist | Queue empty (pending_verify=0, verified_total=165,970). It never short-circuits | `aisleprompt: agents/recipe-image-verifier/AGENT.md` |
| `aisleprompt-recipe-image-archiver` | aisleprompt | every 15 min at :04/:19/:34/:49 (drop-in) · on | Mirrors verified but unarchived recipe images to Azure Blob (`scripts/_backfill-blob-storage.ts`) and stamps `image_storage_url` and `image_storage_blob`, so `/img/r/<id>` serves hosted copies | Idle: 165,970 verified and 165,970 archived | `aisleprompt: agents/recipe-image-archiver/AGENT.md` |

### Growth & analytics

| Agent | Repo | Sched | Purpose | State 2026-09-23 | Runbook |
|---|---|---|---|---|---|
| `aisleprompt-user-growth-strategist` | aisleprompt | every 5 h at :12 · on | A run.sh plus a Python script (not AgentBase). Builds a growth brief (prod DB, 90 days of GSC, 30 days of GA4, agent inventory, the last 30 commits, `docs/seo-growth-strategy.md`) and asks chat_with_fallback (claude-cli / claude-sonnet-4-6) for a DAU memo with no paid ads. Up to 3 `tier=lever` recs go to the implementer via `gated_dispatch_now` (Opus required); the memo goes to the digest | 14:12Z: 13 recs parsed, 3 dispatched | `aisleprompt: agents/user-growth-strategist/AGENT.md` |
| `specpicks-user-growth-strategist` | specpicks | every 5 h at :42 · on | Sibling fork for SpecPicks. Adds a split of GA4 traffic that separates datacenter crawlers from real users (`traffic-quality.json`) | 14:42Z: 16 recs parsed, 3 of 7 dispatched. Its DB snapshot is mostly errors because it queries aisleprompt tables against the specpicks DB. No goals | `specpicks: agents/user-growth-strategist/AGENT.md` |
| `aisleprompt-conversion-optimizer` | aisleprompt | daily 13:45 · on | A run.sh plus a Python script (not AgentBase). Joins `instacart_clicks`, `kitchen_click_events_human`, `ai_traffic_log` and GSC, flags bot-polluted cart attribution and emits page-level CTA and attribution recs. They are auto-queued because `CONVERSION_AUTOQUEUE=1` is set on the host | 17:45Z: 59,242 raw carts but only 9 human. It re-queues the same `conversion-attribution-broken` rec on every run (no dedup). No run-index entries | `aisleprompt: agents/conversion-optimizer/AGENT.md` |
| `aisleprompt-site-goals-tracker` | aisleprompt | daily 13:00 (manifest says UTC) · on | Runs the engine with `--site=aisleprompt`. Records 30-day GSC clicks and impressions, GA4 AI-Assistant sessions, Instacart and Amazon outbound clicks, GSC coverage and the active recipe count to the goals store | 17:00Z: 17 metrics recorded. GSC reports 0 clicks and 21 impressions over 30 days (not investigated) | `aisleprompt: agents/site-goals-tracker/AGENT.md` |
| `specpicks-site-goals-tracker` | nsc-assistant (wrapper) | daily 13:00 (manifest says UTC) · on | Same engine with `--site=specpicks`. Both instances append to `/tmp/reusable-agents-site-goals.log` (not the agent log) and always exit 0 | 17:00Z: 16 metrics recorded. GSC reports 2 clicks and 773 impressions over 30 days; 29.79% of 5,428 inspected URLs are indexed | [agents/site-goals-tracker/AGENT.md](../agents/site-goals-tracker/AGENT.md) |
| `specpicks-reddit-reply-queue` | specpicks | daily 08:15 · on | Runs `scripts/reddit-comment-queue.ts` over 7 subreddits. Drafts benchmark-backed replies that link the matching article, commissions `editorial_topics` rows for unanswered LLM threads, and queues up to 3 threads a day to the operator via the digest. It never posts | 3 picks queued; 3 of the 7 subreddits were throttled. Goals are not seeded | `specpicks: agents/reddit-reply-queue/AGENT.md` |
| `specpicks-newsletter-digest-sender` | specpicks | Sundays 09:30 · on | A plain script (not AgentBase). Builds the weekly Local LLM Hardware Digest (the week is skipped if fewer than 3 sections have data), upserts a `newsletter_issues` row served at `/newsletters/<slug>`, and mails due subscribers | Sent on 2026-09-06 (38), 09-13 (36) and 09-20 (36); the 2026-08-30 send failed | `specpicks: agents/newsletter-digest-sender/AGENT.md` |

### Ops / fleet

| Agent | Repo | Sched | Purpose | State 2026-09-23 | Runbook |
|---|---|---|---|---|---|
| `backlog-dispatcher-agent` | reusable-agents | every minute · on | Walks the recent run dirs of the 13 producers in `PRODUCER_AGENT_IDS` for recs that are not shipped, deferred, skipped or backed off. Dispatches them with `dispatch_now()` within the claude-pool and Copilot capacity caps, and kills stuck scopes | About 886 ticks: 814 found nothing, 44 were throttled and 28 dispatched 1 to 10 recs. `config/implementer-allowed-handlers.json` is an archived blob, so no handler restriction applies | [agents/backlog-dispatcher-agent/README.md](../agents/backlog-dispatcher-agent/README.md) |
| `agent-doctor` | reusable-agents | every 5 h at :00, plus on demand via `invoke_doctor` · on | Drains resilience incidents and polls `/api/agents` for failed runs or stuck runs (running longer than 2× the agent's p95 duration, or 30 min when there is no history). Applies narrow fixes (token refresh, stale-lock cleanup, transient no-op) or a `claude --print` diagnosis, re-queues stuck implementer chains and escalates via `notify_operator` | 35 runs. The fixes log holds 2,712 entries since 2026-08-24, and none has the outcome "fixed" | [agents/agent-doctor/AGENT.md](../agents/agent-doctor/AGENT.md) |
| `digest-rollup-agent` | reusable-agents | daily 07:30 via the host drop-in `10-daily.conf` (2026-09-15) · on | Sends the operator one consolidated email (Microsoft Graph, with msmtp as fallback) covering a hard-coded 3-hour look-back: shipped recs, the auto-queue, failed runs, agent-doctor escalations, handoffs, SpecPicks SEO readiness and `digest-queue/` mail. Then it archives the queue | Runs daily. The manifest and registry still say `16 */5 * * *` with `enabled=false`, so **re-registering from the manifest as it stands would disable the timer.** Because `WINDOW_HOURS=3` runs against a daily cadence, queue entries and runs from outside roughly 04:30 to 07:30 are archived or ignored without being rendered | [agents/digest-rollup-agent/README.md](../agents/digest-rollup-agent/README.md) |
| `responder-agent` | reusable-agents | every 2 min · on | Polls the automation IMAP inbox for operator replies (implement, skip, merge or modify on rec ids), records decisions in `agents/<target>/responses-queue/`, flips approvals and spawns the implementer | **Inert since host standup (2026-08-13).** The live `config.yaml` is still the unedited example (`imap.example.com`), so every tick fails DNS yet reports success with metrics of 0 | [agents/responder-agent/AGENT.md](../agents/responder-agent/AGENT.md) |
| `goals-tracker` | nsc-assistant (wrapper) | daily 12:00 (manifest says UTC) · on | A plain script (code in reusable-agents). Sends a daily HTML email rolling up every enabled agent's goals (baseline, current, target, trend, sparklines), two site KPI cards and a stale-metrics alert. Sends via Graph (msmtp fallback), bypassing the digest. Each run overwrites the tracked file `agents/goals-tracker/last-digest.html` in the reusable-agents working tree | 16:00Z: sent, 55 agents, 222 goals, 37 stale (log: `/tmp/reusable-agents-goals-tracker.log`). Its own goals are never recorded | [agents/goals-tracker/AGENT.md](../agents/goals-tracker/AGENT.md) |
| `agent-metrics-collector` | nsc-assistant (wrapper) | daily 11:00 (manifest says UTC) · on | A plain script (code in reusable-agents). Back-fills goal metrics for the 19 agents in `AGENT_METRIC_FNS` from run dirs, site DB counts, the GSC coverage JSONL and a host log. Some values are hard-coded placeholders | **Degraded:** 26 metrics across 16 agents. Every DB metric fails because the script reads `<SITE>_DATABASE_URL` and the host defines `DATABASE_URL_<SITE>` | [agents/agent-metrics-collector/AGENT.md](../agents/agent-metrics-collector/AGENT.md) |
| `oauth-heartbeat-agent` | reusable-agents | daily 08:37 · **off** (the manifest says `enabled=false`, the registry still says `enabled=true`) | Retired daily ping that minted Google OAuth access tokens to reset the 7-day refresh-token clock of Testing mode | Retired 2026-09-04, when the consent screen was published In production. Last run 2026-09-04 | [agents/oauth-heartbeat-agent/README.md](../agents/oauth-heartbeat-agent/README.md) |

### Research & personal

| Agent | Repo | Sched | Purpose | State 2026-09-23 | Runbook |
|---|---|---|---|---|---|
| `app-store-opportunity-agent` | reusable-agents | daily 14:00 · on | An LLM-planned scout of the iOS App Store and Google Play for popular but low-rated apps and for regional-gap apps. Accumulates them, blueprints up to 3 top picks and emails a ranked list to reply to with pursue or pass | Two runs on 2026-09-23 (03:11Z reboot catch-up and 18:00Z) both ended "success" (279 open, 2 new, email sent), but all 3 blueprint calls failed in each: claude-pool profile-4 auth expired, 600 s timeouts, and "Reached max turns (1)". Replies depend on the inert responder | [agents/app-store-opportunity-agent/AGENT.md](../agents/app-store-opportunity-agent/AGENT.md) |
| `market-research-pipeline` | nsc-assistant | every 6 h at :00 · on | AgentBase. Proposes 3 to 5 underserved SaaS opportunities per run with an LLM, dedups them, queues a digest email and learns from APPROVE/REJECT replies. Skips proposing while 30 or more are pending | Every run: "inbox full (30); waiting on operator". Replies depend on the inert responder. The log shows BlobArchived errors | `nsc-assistant: agents/market-research-pipeline/AGENT.md` |
| `sessions-save` | nsc-assistant | every 5 min (`OnUnitActiveSec=5min`; a hand-written unit with no wrapper) · on | Runs `agents/agent-session-snapshot/agent-sessions.sh -Action Save` to snapshot open AI coding-agent sessions (Claude Code, Copilot CLI, Codex) so they can be reopened as terminal tabs. It is not in the framework registry (the API returns 404); the registry lists the tool as `agent-session-snapshot` (manual) | The journal shows it completing every tick | `nsc-assistant: agents/agent-session-snapshot/SKILL.md` |

---

## Shared engines and their instances

Engine code lives in `reusable-agents/agents/<engine>/`. A per-site instance directory
usually holds only `manifest.json` and `site.yaml` (or a thin `run.sh`). Its
`entry_command` points `python3` at the engine and passes `AGENT_ID` and a config
env var. The engine is never scheduled itself.

| Engine | Kind and registration | Registered instances (config env) | Runbook |
|---|---|---|---|
| `seo-opportunity-agent` | AgentBase; no manifest; not registered | `aisleprompt-seo-opportunity-agent`, `specpicks-seo-opportunity-agent` (`SEO_AGENT_CONFIG`, and both run with `SEO_DISABLE_UNCHANGED_SHORTCIRCUIT=1`) | [AGENT.md](../agents/seo-opportunity-agent/AGENT.md), [README.md](../agents/seo-opportunity-agent/README.md) |
| `progressive-improvement-agent` | AgentBase blueprint (`is_blueprint`, `enabled:false`); not registered. Its `crawler.py` is also imported by competitor-research and the SEO engine | `aisleprompt-progressive-improvement-agent`, `specpicks-progressive-improvement-agent` (`PROGRESSIVE_IMPROVEMENT_CONFIG`) | [AGENT.md](../agents/progressive-improvement-agent/AGENT.md) |
| `competitor-research-agent` | AgentBase blueprint (site-quality-recommender); not registered (API 404) | `aisleprompt-competitor-research-agent`, `specpicks-competitor-research-agent` (`COMPETITOR_RESEARCH_CONFIG`) | [AGENT.md](../agents/competitor-research-agent/AGENT.md) |
| `catalog-audit-agent` | AgentBase; manifest `enabled=false`; registry entry with `enabled=false` and no timer | `aisleprompt-catalog-audit-agent`, `specpicks-catalog-audit-agent` (`CATALOG_AUDIT_CONFIG`) | [README.md](../agents/catalog-audit-agent/README.md) |
| `shelf-audit-agent` | AgentBase; manifest `enabled:false` with no cron; not registered | `aisleprompt-shelf-audit-agent`, `specpicks-shelf-audit-agent` (`SHELF_AUDIT_CONFIG`) | [README.md](../agents/shelf-audit-agent/README.md) |
| `feedback-triage-agent` | AgentBase; no manifest; not registered | `aisleprompt-feedback-triage-agent`, `specpicks-feedback-triage-agent` (`FEEDBACK_TRIAGE_CONFIG`) | [AGENT.md](../agents/feedback-triage-agent/AGENT.md) |
| `gsc-coverage-auditor` | AgentBase wrapper around `inspect.py`; no manifest; not registered | `aisleprompt-gsc-coverage-auditor` (aisleprompt `run.sh`), `specpicks-gsc-coverage-auditor` (nsc-assistant `run.sh`, `GSC_INSPECT_SITE`) | [AGENT.md](../agents/gsc-coverage-auditor/AGENT.md) |
| `indexnow-submitter` | AgentBase wrapper around `submit.ts`; no manifest. A stale registry entry `indexnow-submitter` has `enabled=false`, no timer, and its last run was a failure on 2026-08-25 | `aisleprompt-indexnow-submitter`, `aisleprompt-indexnow-bulk` (aisleprompt `run.sh`); `specpicks-indexnow-submitter`, `specpicks-indexnow-bulk` (nsc-assistant `run.sh`). Uses `INDEXNOW_SITE` and, for the bulk instances, `INDEXNOW_BULK=1` | [AGENT.md](../agents/indexnow-submitter/AGENT.md) |
| `site-goals-tracker` | Plain Python (not AgentBase); no manifest; site values are hard-coded in `SITE_PROFILES` | `aisleprompt-site-goals-tracker`, `specpicks-site-goals-tracker` (`--site=<site>`) | [AGENT.md](../agents/site-goals-tracker/AGENT.md) |
| `product-hydration-agent` | AgentBase blueprint (`is_blueprint`, `enabled=false`); skipped by `register-all-from-dir.sh` | `specpicks-product-hydration-agent` (`PRODUCT_HYDRATION_CONFIG`) | [AGENT.md](../agents/product-hydration-agent/AGENT.md) |
| `ebay-product-sync-agent` | AgentBase (`main()` calls `run()` directly). A registry entry exists with `enabled=true` but no cron, so it has no timer | `specpicks-ebay-product-sync-agent` (`EBAY_PRODUCT_SYNC_CONFIG`) | [README.md](../agents/ebay-product-sync-agent/README.md) |
| `category-integrity-agent` | AgentBase; no manifest | `specpicks-category-integrity-agent` (`CATEGORY_INTEGRITY_CONFIG`) | [AGENT.md](../agents/category-integrity-agent/AGENT.md) |
| `search-demand-agent` | AgentBase, deterministic; no manifest | `specpicks-search-demand-agent` (specpicks `run.sh`) | [AGENT.md](../agents/search-demand-agent/AGENT.md) |
| `agent-metrics-collector` | Plain Python; no manifest in reusable-agents | `agent-metrics-collector` (nsc-assistant `run.sh`) | [AGENT.md](../agents/agent-metrics-collector/AGENT.md) |
| `goals-tracker` | Plain Python; no manifest in reusable-agents | `goals-tracker` (nsc-assistant `run.sh`; its AGENT.md and SKILL.md are symlinks to this directory) | [AGENT.md](../agents/goals-tracker/AGENT.md) |
| `implementer` | AgentBase wrapper (`agent.py`) around a run.sh of about 3,400 lines; registry `enabled=false`, `runnable_modes=chained` | No timer. Runs as transient `agent-dispatch-implementer-<site>-<ts>` scopes spawned by `dispatch_now` (backlog-dispatcher, direct producers, auto-queue-drainer) | [README.md](../agents/implementer/README.md) (operator runbook; `AGENT.md` is the LLM prompt) |
| `deployer` | AgentBase with runtime id `seo-deployer`; manifest `is_blueprint`, registry `deployer` with `enabled=false` | No timer. Chained from `implementer/run.sh` after each batch that commits code. 50 runs since 2026-09-19: 30 succeeded, 20 failed (mostly test-gate blocks) | [AGENT.md](../agents/deployer/AGENT.md), [README.md](../agents/deployer/README.md) |

### SEO engine phases

`seo-opportunity-agent` is one AgentBase pipeline. Three phases run in sequence under a
single `run_ts`, so every artifact lands in the same run dir:

| Phase | Code | What it does |
|---|---|---|
| 1. Collect | `lib/collector/pull-data.py` | Pulls GSC, GA4, the production DB and the live site according to `site.yaml`, and writes a standardized run dir |
| 2. Analyze | `lib/analyzer/analyzer.py` + `llm_audit.py` | Deterministic rule passes plus an LLM page audit, which `SEO_DISABLE_LLM_AUDIT=1` turns off. Writes `recommendations.json` |
| 3. Finalize | `finalizer.py` + `lib/reporter/send-report.py` | Queues the HTML report to the digest, calls `gated_dispatch_now()` (a no-op while `auto_implement: false`) and records the outbound message for the Confirmations page |

Both per-site `site.yaml` files validate against `shared/schemas/site-config.schema.json`.
Blocks with `additionalProperties:false` reject unknown keys, and the agent then exits
with status 1 within about a second. Add any new field to the schema first (see
`CLAUDE.md`). Onboarding a site: [`seo-onboard-new-site.md`](seo-onboard-new-site.md).
Known engine defects are listed in the engine's [AGENT.md](../agents/seo-opportunity-agent/AGENT.md):
the `list_prefix` 10k-key cap causes "no prior snapshot", `signals()` is inert, and
there are collector/analyzer key mismatches.

---

## Unregistered directories and registry-only entries

### Directories with no timer

| Directory | What it is | Status 2026-09-23 | Doc |
|---|---|---|---|
| `reusable-agents/agents/<engine>/` (see the engine table) | Shared engines | Run only through their instances | See above |
| `reusable-agents/agents/jcode-agent/` | AgentBase blueprint wrapper around `jcode run --json` | Not registered (API 404) and never run. `jcode` is not installed. Its success path raises a caught TypeError (`RunResult(output=…)`) | [AGENT.md](../agents/jcode-agent/AGENT.md) |
| `reusable-agents/agents/seo-analyzer/` | Legacy standalone copy of the SEO analyzer phase | Not an agent. Differs from `seo-opportunity-agent/lib/analyzer/` only by a docstring. No live caller | [README.md](../agents/seo-analyzer/README.md) |
| `reusable-agents/agents/_archive/reusable-agents-competitor-research-agent/` | Archived self-improvement instance (the framework compared against n8n, Temporal, Airflow, …) | Archived. Its registry entry still exists with `enabled=false` and no timer | [README.md](../agents/_archive/reusable-agents-competitor-research-agent/README.md) |
| `aisleprompt/agents/seo-config/` | Config only: `site-indexnow.json` (query sets, sitemaps, static paths, IndexNow key, watermark) | Read by the aisleprompt IndexNow submitters and GSC auditor. `AISLEPROMPT_DATABASE_URL` is unset on the host, so readers fall back to a DSN committed in the JSON | `aisleprompt: agents/seo-config/README.md` |
| `specpicks/agents/seo-config/` | Config only: specpicks `site-indexnow.json` (10 query sets, including the canonical `/vs/` and `/compare/` pages) | Read by the specpicks IndexNow submitters and GSC auditor. `SPECPICKS_DATABASE_URL` is unset, so the committed fallback DSN is used | `specpicks: agents/seo-config/README.md` |
| `aisleprompt/agents/voice/` | Config only: a Dialogflow CX agent plus a design doc for Siri and Google Assistant voice add-to-list | An unwired scaffold from 4da0d3af (2026-05-07). `/api/voice/add-item` returns 404 in prod | `aisleprompt: agents/voice/README.md` |
| `specpicks/agents/specpicks-implementer-agent/` | An abandoned scaffold of one-off `editorial_articles` insert scripts (5a11a2a, 2026-06-07) | Dead code, with no callers. The real implementer is `reusable-agents/agents/implementer` | `specpicks: agents/specpicks-implementer-agent/AGENT.md` |
| `specpicks/agents/keep-the-lights-on/` | A git snapshot of KTLO state for SpecPicks, last changed 2026-07-15 | Nothing reads it. Live KTLO state is in framework storage at `agents/keep-the-lights-on/specpicks/` | `specpicks: agents/keep-the-lights-on/README.md` |

Timers that are disabled (`oauth-heartbeat-agent`, `specpicks-scraper-watchdog`) are
listed in the main tables above.

### Registry entries without a timer

On 2026-09-23 the API held 85 entries. 68 of them match an `agent-*` timer (the 69th
timer, `sessions-save`, is not in the registry). The other 17 have no timer:

| Entry | enabled | Notes |
|---|---|---|
| `implementer`, `deployer` | false | Chained, no timer by design (see the engine table) |
| `catalog-audit-agent`, `ebay-product-sync-agent`, `indexnow-submitter` | false, true, false | Engine-level entries; the instances do the work |
| `agent-session-snapshot` | true | A manual tool; the timer that runs it is `sessions-save` |
| `reusable-agents-competitor-research-agent` | false | Archived (see above) |
| `daily-briefing-calendar-agent`, `external-game-cataloger`, `fix-submission-agent`, `game-library-scanner`, `real-estate-agent`, `retro-multiplayer-refresh`, `security-scanner-pipeline` | false | Legacy nsc-assistant agents. Their directories are now in `nsc-assistant/agents/_archive/` |
| `daily-status-briefing`, `retro-agent-orchestrator`, `web-search` | false | nsc-assistant manual agents; the directories still exist in `nsc-assistant/agents/` |

---

## Fleet-wide open issues (2026-09-23)

These cut across several rows above. Each item is documented in the linked runbooks and
none is fixed by this doc.

- **GPU lost at 13:16:48 EDT (Xid 79).** The kernel logged
  `NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus`, then Xid 154 (recovery
  action "Node Reboot Required"). `lspci` still lists the GB202, but `nvidia-smi` reports
  "No devices were found". Until the host is rebooted, every ollama-backed step runs on
  CPU or times out: `specpicks-ebay-product-sync-agent` canonicalisation (resolved
  provider `ollama-5090` / qwen3:8b), `specpicks-trending-topic-pipeline` research
  (`SPECPICKS_PROVIDER=ollama`, qwen3:14b), competitor-research feature extraction
  (`ai_calls.extract` = `ollama-local` / qwen3:14b, both sites), and minicpm-v4.5 vision in
  `aisleprompt-article-hero-image-curator` and `specpicks-news-hero-image-curator`. SDXL
  on :7861 (`local-image-gen.service`, already disabled) hard-codes `cuda` and cannot run
  either. Check with `journalctl -k | grep -i xid` and `nvidia-smi`. Operator action:
  reboot, then confirm `nvidia-smi` lists the GPU.
- **Credentials in unit files.** 18 `agent-*.service` files carry a literal Postgres
  DSN in `ExecStart`, copied from the manifest `entry_command`: 17 specpicks agents plus
  `catalog-audit-shipped-backfill`. Several `run.sh` files, TS scripts and
  `site-indexnow.json` files also hold fallback credentials. The fix is to read
  `DATABASE_URL_<SITE>` from `secrets.env` and rotate the credentials. Never paste
  `ExecStart=` or `Environment=` lines into docs.
- **The email-reply path is dead.** `responder-agent` has a placeholder IMAP config.
  Anything that waits for an operator reply therefore never moves: competitor-research
  approvals, `market-research-pipeline` (inbox full at 30), `app-store-opportunity-agent`.
- **claude-pool auth.** Auth-dead profiles blind `specpicks-catalog-audit-agent` and
  fail the app-store blueprint calls. Re-login is an operator action
  (`python3 -m framework.cli.claude_pool login-help`).
- **`local-image-gen.service` is disabled**, so `aisleprompt-recipe-image-refiller` is
  blocked.
- **The `signals()` short-circuit is inert on many agents.** `run()` returns no
  `next_state`, so `post_run` stores `{}`. The runbooks record this for seo-opportunity,
  progressive-improvement, feedback-triage, catalog-audit, shelf-audit, search-demand,
  internal-link-densifier, specpicks-article-hero-image-curator,
  aisleprompt-recipe-image-verifier and others. `aisleprompt-article-hero-image-curator`
  does short-circuit.
- **Handoffs to generic handler ids are dead-lettered** (article-proposal-agent,
  head-to-head-agent and indexnow-submitter queues) unless `site_handler_overrides`
  maps them.
- **No goals declared**, in breach of the `CLAUDE.md` requirement: both feedback-triage
  instances, both shelf-audit instances, category-integrity, content-accuracy-auditor,
  site-functional-tests, ebay-counterpart-matcher, specpicks-user-growth-strategist,
  `specpicks-newsletter-digest-sender` (a plain script, `scripts/send-llm-hardware-digest.py`,
  with no RunResult metrics, so its manifest `target_metric` `goal-email-subscribers` is
  never recorded) and `market-research-pipeline`. Goals exist but are not seeded in
  storage for reddit-reply-queue and trending-topic-pipeline. In all, 13 registered ids
  returned 0 goals from `GET /api/agents/<id>/goals` on 2026-09-23.
- **Stale manifest text.** Many manifests still carry a wrong cadence or description, or
  a `target_metric` that matches no goal (examples: digest-rollup, responder, agent-doctor,
  kitchen-scraper, article-proposal). The per-agent runbooks list each case. The
  manifests have not been changed.

---

## Quick lookup

| You want to | Look here |
|---|---|
| Edit shared engine code | `reusable-agents/agents/<engine>/` |
| Edit a per-site config or manifest | `<site-repo>/agents/<dir>/` (`site.yaml`, `manifest.json`) |
| See a run log | `/tmp/reusable-agents-logs/agent-<id>.log` |
| See run history and status | `GET http://localhost:8090/api/agents/<id>` (and `/runs`, `/status`) with `Authorization: Bearer $FRAMEWORK_API_TOKEN` |
| See the real schedule, including drop-ins | `systemctl --user cat agent-<id>.timer` |
| Read a blueprint | `reusable-agents/blueprints/<name>/BLUEPRINT.md` |
| Re-register after a manifest change | `bash <repo>/agents/register-with-framework.sh` (specpicks and aisleprompt; both call `install/register-all-from-dir.sh`) or nsc-assistant `scripts/register-agents.sh`. Per `CLAUDE.md`, register the framework repo last (duplicate ids are last-write-wins). Check `digest-rollup-agent` first: its manifest would disable the live timer |
| Inspect the implementer queue | See `CLAUDE.md` → "Implementer queue" (Azure `agents/responder-agent/auto-queue/`) |

## Keeping this catalog accurate

Read-only checks used to build this page:

```bash
systemctl --user list-unit-files 'agent-*.timer'            # 69 on 2026-09-23 (67 enabled)
systemctl --user list-timers --all --no-pager | grep agent-  # next/last fire; NEXT "-" means it is running now
systemctl --user cat agent-<id>.timer                        # OnCalendar + drop-ins
systemctl --user list-units --type=service --no-pager | grep -E 'drainer|host-worker|reusable-agents-api|retro-chat|local-image-gen'
set -a; . ~/.reusable-agents/secrets.env; set +a
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://localhost:8090/api/agents   # registry (85 entries)
tail -n 50 /tmp/reusable-agents-logs/agent-<id>.log
```

When you add, rename or retire an agent, update its row here in the same change. Keep
the State column to one dated sentence. The per-agent runbook holds the detail.

**The dashboard does not read runbooks from disk.** It serves the runbook, skill and
readme copies embedded at registration: storage `agents/<id>/runbook.md`, `skill.md` and
`readme.md`, written from `runbook_body` / `skill_body` / `readme_body` by
`POST /api/agents/register`. `_load_md` in `framework/api/app/routes/agents.py` prefers
the stored copy and falls back to the file at `runbook_path` only when the blob is
missing. The local and Azure dashboards both read that blob. After editing a runbook,
re-register that agent with `bash install/register-agent.sh <agent-dir>` (with
`~/.reusable-agents/secrets.env` sourced for `FRAMEWORK_API_URL` and
`FRAMEWORK_API_TOKEN`) to refresh the tab. In reusable-agents, re-register agents one at
a time rather than walking the whole `agents/` directory: `digest-rollup-agent`'s manifest
still says `enabled: false`, and the register route then stops and disables the timer
that fires at 07:30. On 2026-09-23 the stored `runbook.md` differed from the file on
disk for every agent sampled (authority-agent, specpicks-ebay-counterpart-matcher,
aisleprompt-recipe-image-refiller, specpicks-product-hydration-agent,
digest-rollup-agent; those blobs were last written between 2026-08-24 and 2026-09-22), so
the dashboard still shows the runbooks from before 2026-09-23.
