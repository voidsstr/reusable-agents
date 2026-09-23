# seo-opportunity-agent: engine reference

The reusable SEO + revenue audit pipeline. Every 2–3 hours per site it pulls
Google Search Console, Google Analytics 4, production-DB stats, and live-site
signals. It then scores opportunities with deterministic rules plus an LLM
page audit, writes up to 12 recommendations for the implementer pipeline, and
queues an HTML report into the operator digest.

> **Operational runbook:** [`AGENT.md`](AGENT.md) covers schedule, inputs,
> outputs, goals, env vars, current known issues, and how to run and inspect.
> This README is the **deep reference**: architecture, run-dir layout, config
> blocks, the rec-type catalog, troubleshooting history, and onboarding a new
> site.

This README covers the **engine**. The per-site instances keep only
`manifest.json` + `site.yaml` (+ SQL) next to each site's code:

- `aisleprompt: agents/seo-opportunity-agent/` (id `aisleprompt-seo-opportunity-agent`)
- `specpicks: agents/seo-opportunity-agent/` (id `specpicks-seo-opportunity-agent`)

Both run `agent.py` here with the `AGENT_ID` and `SEO_AGENT_CONFIG` env vars.
The aisleprompt instance moved from nsc-assistant into the aisleprompt repo
on 2026-05-12 (`aisleprompt: e1b5a2c6`). nsc-assistant keeps only
`_legacy-seo-opportunity-agent*/` for reference.

## Architecture

One AgentBase agent runs three phases one after another under a single
`run_ts`, so the collector, analyzer, and finalizer all write to the same
Azure run dir (`agents/<agent_id>/runs/<run_ts>/`).

```
┌──────────── one cron tick ──────────────────────────────────────────┐
│  Phase 1: collector   GSC + GA4 + DB stats + site signals + sitemap │
│            ↓                                                         │
│  Phase 2: analyzer    snapshot, rule passes + LLM audit,             │
│                       recommendations.json + goals.json              │
│            ↓                                                         │
│  Phase 3: finalize    render report → digest queue, persist recs,    │
│                       gated dispatch (off on both sites)             │
└──────────────────────────────────────────────────────────────────────┘
```

| Phase | Code |
|---|---|
| 1. Collector | `lib/collector/pull-data.py` (+ `refresh-token.py` for the Google token) |
| 2. Analyzer | `lib/analyzer/analyzer.py` (+ `llm_audit.py`: 133-check whitelist, prompt, check-id → rec-type mapping) |
| 3. Finalize | `finalizer.py` (+ `lib/reporter/send-report.py:render_html`) |

There are no per-phase READMEs. The docstrings at the top of each file are the
phase documentation.

- `agent.py` is the `AgentBase` class. It handles run-ts management, status
  updates, and run-dir lifecycle, and runs phases 1–2 as subprocesses with
  `--agent-id` / `--run-ts`, so all artifacts land in one directory.
- `finalizer.py` is phase 3.

> **Legacy note.** `agents/seo-data-collector/` and `agents/seo-reporter/`
> have been deleted. `agents/seo-analyzer/` still exists as a standalone copy,
> but it has **diverged** from `lib/analyzer/analyzer.py` (the lib copy got
> `c2de01e` on 2026-08-29). Make analyzer changes only in `lib/`.
>
> Also note that `lib/{collector,analyzer}` were **reconstructed** on
> 2026-08-13/14 after the fleet-host loss. A bare `lib/` pattern in
> `.gitignore` had kept them out of git, and the files existed only on the old
> host. They are tracked now. The reconstructed collector writes a
> **smaller** set of files than the original (see below), so several
> analyzer rule families have no input today.

## Lifecycle of one run

1. The cron tick starts the per-site systemd unit. Its `entry_command` (from
   the per-site manifest) sets `SEO_DISABLE_UNCHANGED_SHORTCIRCUIT=1`,
   `AGENT_ID`, `SEO_AGENT_CONFIG`, and `PYTHONPATH`. The specpicks instance
   also sets `DATABASE_URL`. It then runs `python3 agent.py`.
2. `agent.py` loads `site.yaml` and validates it against
   `shared/schemas/site-config.schema.json`. A key rejected by the schema ends
   the run immediately; see [Troubleshooting](#troubleshooting).
3. The **collector** writes to `data/`:
   - 4 GSC reports, as raw API responses: `gsc-queries-90d`, `gsc-pages-90d`,
     `gsc-devices-90d`, `gsc-countries-90d`. The window is 90 days ending
     3 days ago. **If GSC fails, the run fails.**
   - 4 GA4 reports: `ga4-summary-28d`, `ga4-events-28d`, `ga4-geo-28d`,
     `ga4-traffic-sources-28d`. A failed report degrades to `{}`.
   - `db-stats.json`: one key per `-- @@QUERY: <name>` block in
     `data_sources.db.queries_file`.
   - `site-signals.json`: robots.txt, plus the homepage title, meta
     description, H1 count, canonical, and JSON-LD `@type`s.
   - `sitemap-urls.json`: per-`coverage_targets` pattern counts across the
     full sitemap (up to 100 child sitemaps), with `complete: false` when the
     fetch was partial.
4. The **analyzer**:
   - Writes `snapshot.json`.
   - Compares with the prior run (`comparison.json`) and scores that run's
     goals (`goal-progress.json`). *Currently these never run; see AGENT.md →
     Failure modes.*
   - Builds recs from the deterministic rule passes, then from the LLM audit
     (at most `analyzer.max_llm_audit_pages` pages).
   - Tags each rec with `work_type` / `handoff_target`.
   - Writes `recommendations.json` and `goals.json`. Each rec carries `id`,
     `type`, `priority`, `title`, `rationale`, `expected_impact`,
     `implementation_outline`, and `data_refs`.
5. The **finalizer**:
   - Renders the HTML report.
   - Writes `recommendations.json` to storage.
   - Calls `agent.queue_for_digest(...)`. The report reaches the inbox through
     `digest-rollup-agent`.
   - Calls `framework.core.dispatch.gated_dispatch_now(cfg=cfg, …)`. Both
     sites set `auto_implement: false` (since 2026-05-13), so **no dispatch
     happens here**. Instead, `backlog-dispatcher-agent` picks the recs up
     from the run dir. See [Rec flow](#rec-flow-to-the-implementer).

## Run-dir layout

This listing is from a real run on 2026-09-23. The files marked † are added
later by the implementer and the framework.

```
agents/<agent-id>/runs/<UTC-ts>/
├── data/
│   ├── gsc-{queries,pages,devices,countries}-90d.json
│   ├── ga4-{summary,events,geo,traffic-sources}-28d.json
│   ├── db-stats.json
│   ├── site-signals.json
│   ├── sitemap-urls.json
│   └── pages.jsonl            # pages fetched by the LLM-audit on-demand crawl
├── snapshot.json              # this run's aggregate
├── recommendations.json       # ranked recs (THE downstream contract)
├── goals.json                 # goals declared by this run
├── goal-progress.json         # AgentBase goal mirror
├── decisions.jsonl · progress.json · context-summary.md
├── _ship_status.json · applied-recs.json · changes/ · verifications/ †
└── dispatch-batches.json · handoffs-sent.json · deploy.json †
```

`comparison.json` is not produced today (no prior snapshot is found).

## Configuration

Configuration lives in each site's `site.yaml` and is validated against
`shared/schemas/site-config.schema.json` at the start of every run. Templates:
`examples/sites/generic.yaml`, `examples/sites/aisleprompt.yaml`,
`examples/sites/specpicks.yaml`. The **live** configs are the per-site
`site.yaml` files, not the `examples/` copies.

A minimal config:

```yaml
site:
  id: my-site
  domain: example.com

data_sources:
  gsc:
    site_url: sc-domain:example.com
  ga4:
    property_id: "1234567890"
  db:
    type: postgres
    dsn_env: DATABASE_URL_MY_SITE          # env var NAME; value lives in secrets.env
    queries_file: /path/to/db-queries.sql  # `-- @@QUERY: <name>` blocks

reporter:
  email:
    to: [mperry@northernsoftwareconsulting.com]
    from: SEO Agent <automation@northernsoftwareconsulting.com>

auth:
  oauth_file: ~/.reusable-agents/seo/.oauth.json

implementer:
  repo_path: /home/voidsstr/development/my-site
  branch: master
  allowed_paths: ["src/**"]   # required by the path-scope policy (see CLAUDE.md)

auto_implement: false          # recs flow via backlog-dispatcher
```

Optional blocks and whether each is used today:

| Block | Status (verified 2026-09-23) |
|---|---|
| `coverage_targets` | **Used.** Emits one `new-page-<type>` rec per shortfall, counting URLs that match `sitemap_pattern` against `expected_min`. |
| `analyzer.coverage_target_files` | **Used.** Attaches repo-relative `target_files` to `gsc-coverage-*` recs so the implementer's scope gate does not defer them. Added 2026-08-18. |
| `analyzer.{max_recs_per_run, max_llm_audit_pages, pre_traffic_impr_threshold, primary_objective, ai_provider, ai_model}` | **Used.** |
| `revenue_kpis` | **Used** by the `conversion-path` rule (db-stats + GA4 events). |
| `revenue_focus` | Read, but **no effect today**: its two rules need `pages-by-type.jsonl` (`featured-product-pdp-improve`) and `articles-inventory.json` (`article-featured-product-mention-untagged`), and the collector produces neither. |
| `handoff_routes`, `site_handler_overrides` | **Used** when tagging `handoff_target`. |
| `articles` | Only `articles.url_template` is read, by the implementer (`agents/implementer/run.sh`). The inventory pull that fed the `article-*` rules is **not** in the reconstructed collector. |
| `page_inventory` | **Not read.** The sitemap sample crawl (`pages-by-type.jsonl`) is not implemented in the reconstructed collector. |
| `data_sources.google_ads` | **Not collected** (no `ads-*.json`), so `paid-organic-gap` and `ad-copy-headline-winner` cannot fire. |
| `geo` (top-level) | In the schema, but **no code reads it** (there is no `_add_geo_recs`). |
| `deployer` | Read by the implementer → deployer chain. See `../deployer/README.md`. |

AGENT.md → Configuration lists further prose-only `analyzer.*` keys that load
without error but are read by no code.

## Per-site instances

| Agent ID | Manifest dir | Cron (America/Detroit) | Repo |
|---|---|---|---|
| `aisleprompt-seo-opportunity-agent` | `aisleprompt/agents/seo-opportunity-agent/` | `15 */2 * * *` (every 2 h at :15) | aisleprompt |
| `specpicks-seo-opportunity-agent` | `specpicks/agents/seo-opportunity-agent/` | `30 */3 * * *` (every 3 h at :30) | specpicks |

The cadences were restored on 2026-08-13 (`aisleprompt: e8c89717`,
`specpicks: 2704a67`) to match what the retired fleet host was running. The
offset start minutes keep the two instances from competing for LLM quota at
the same moment. A third site should use a different minute.

To add a site, follow the 5-step onboarding in `../../docs/seo-onboard-new-site.md`.

## Rec flow to the implementer

Both instances carry `confirmation_flow.kind = auto-queue-with-notification`
in their manifest. The actual path is:

1. `auto_implement: false` → the finalizer's `gated_dispatch_now()` returns
   `None` and nothing is dispatched from the producer.
2. `backlog-dispatcher-agent` (cron every minute; both SEO instances are in
   its `PRODUCER_AGENT_IDS`) reads each producer's `run-index.json`.
   - It selects recs not marked `shipped` / `implemented` / `deferred` /
     `duplicate` / `skipped`.
   - It applies its classifier, caps, and the
     `config/implementer-allowed-dispatch-kinds.json` allowlist (SEO recs are
     dispatch kind `seo`).
   - It **ignores** `auto_implement` on purpose (2026-05-13 note in its code).
3. It then materializes the run dir and calls
   `framework.core.dispatch.dispatch_now()` directly. This is the queue-less
   path it has used since 2026-05-12. `dispatch_now()` starts the
   `implementer` as a systemd-run unit and falls back to
   `agents/responder-agent/auto-queue/` (drained by
   `auto-queue-drainer.service`) only if the direct dispatch fails. The
   implementer may chain to the `deployer`.

Recs that have a `handoff_target` (for example `article-proposal-agent`,
`head-to-head-agent`, `indexnow-submitter`) are meant for those specialist
agents rather than a code edit. The implementer writes them to
`agents/<handoff_target>/handoff-queue/`. Those three generic ids are not
registered agents, and on 2026-09-23 their queues held 4, 1 and 93 unprocessed
envelopes, so today such handoffs are dead-lettered unless
`site_handler_overrides` maps the id to a live per-site agent (see AGENT.md →
Outputs). The report email is informational. The
manifest text describes replying `defer` / `skip` / `revert rec-NNN`; that
reply handling belongs to `responder-agent` and was not re-verified here.

## LLM provider

The analyzer's LLM audit uses the framework provider config for agent id
`seo-analyzer` (`framework.core.ai_providers.ai_client_for`). To override per
site:

```yaml
analyzer:
  ai_provider: anthropic       # optional
  ai_model: <model id>         # optional
```

To change it globally, use the dashboard's `/providers` page or
`POST /api/providers/defaults/agent-override` with agent id `seo-analyzer`.
On 2026-09-23 the resolved client was `claude-cli` with `claude-sonnet-4-6`
(visible as `[claude-cli <agent> claude-sonnet-4-6]` lines in the log). This
is an audit, not article prose, so the Opus-only authoring rule does not apply.

Set `SEO_DISABLE_LLM_AUDIT=1` to turn the LLM pass off entirely.

## Recommendation types

Rules run in a fixed order until the 12-rec budget is full; LLM-audit recs
then fill any slots left. In pre-traffic mode (90-day impressions below
`analyzer.pre_traffic_impr_threshold`: 100 for aisleprompt, 1000 for
specpicks), the content-gap pass (`new-page-*`) runs before the on-page and
template passes instead of as a fill-in at the end. Both sites were in
pre-traffic mode on 2026-09-23.

**Active: inputs are produced** (the "Seen 2026-09-23" column is from that
day's runs)

| Type | Source | Seen 2026-09-23 |
|---|---|---|
| `new-page-<target>` | `coverage_targets` vs `sitemap-urls.json` | `new-page-buying-guide`, `-use-case`, `-comparison` |
| `gsc-coverage-*` (`not-indexed`, `discovered`, `redirect`, `unknown`, `soft-404`, `canonical-mismatch`, `noindex`, `issues`) | `*-gsc-coverage-auditor` JSONL (`$GSC_INSPECT_STATE_DIR/<site>-coverage.jsonl`) | all except `noindex` / `issues` |
| LLM-audit types: `content-expansion`, `ssr-fix`, `schema-markup`, `internal-link`, `indexing-fix`, `ctr-fix`, `redirect-fix`, `conversion-path`, `sitemap-fix`, `other`, plus page-type check ids kept verbatim (`product-*`, `recipe-*`, `h2h-*`, `article-*`, `review-*`, `feature-*`) | `llm_audit.CHECK_ID_TO_REC_TYPE` / `rec_type_for_check()` | `content-expansion`, `indexing-fix`, `product-specs-table-missing`, `product-pros-cons-missing` |

**Can fire: inputs are produced**

| Type | Source |
|---|---|
| `conversion-path` | `revenue_kpis` vs db-stats / GA4 events. Fires only when a source was actually measured and is zero. |
| `home-jsonld-missing` | `site-signals.json` `homepage.jsonld_types` |
| `content-expansion` (competitor parity) | `~/.reusable-agents/competitor-research-agent/runs/<site>/…/parity-gaps.json`, pre-traffic mode only |

**Dormant: their input file is not produced by the reconstructed collector**

| Type(s) | Missing input |
|---|---|
| `top5-target-page`, `ctr-fix`, `internal-link` (striking distance), `indexing-fix` (rank regressions) | `gsc-top5-targets` / `gsc-zero-click` / `gsc-striking-distance` / `gsc-rank-regressions.json` |
| `onpage-*` (per page), `broken-internal-link`, `review-template-incomplete`, `product-pros-cons-missing`, `<type>-schema-incomplete` (JSON-LD completeness), `eeat-outbound-citation-count`, `body-internal-links-thin`, `cwv-ttfb-slow`/`-very-slow`, `content-freshness-low`, `faq-quality-thin`, `indexing-breadcrumb-parity`, `trust-signal-density-thin`, `indexing-itemlist-numberOfItems-missing`, `topical-cluster-orphan`, `internal-link-graph-regression`, `featured-product-pdp-improve` | `pages-by-type.jsonl` |
| `robots-no-ai-allow`, `robots-no-sitemap` | `site-signals.json` keys `robots.bots` / `robots.sitemap_directive`. The collector writes `robots.has_sitemap` instead, and no per-bot map. |
| `indexing-hreflang-missing`, `footer-trust-links-missing` | `site-signals.json` keys `homepage.hreflang_links` / `homepage.footer_trust_links` (not collected). hreflang also needs `site.locales` with 2+ entries. |
| `article-snippet-rewrite`, `article-title-fix`, `article-orphan-boost`, `article-featured-product-mention-untagged` | `articles-inventory.json` |
| `rich-result-error`/`-warning`, `schema-validator-error` | `rich-results-test.jsonl` |
| `paid-organic-gap`, `ad-copy-headline-winner` | `ads-*.json` |
| `indexing-sitemap-shrank`, `schema-markup` (diff) | prior run's `data/` (never mirrored) |
| `product-affiliate-tag-missing` (`_add_amazon_tag_recs`) | **Dead on purpose** (`c2de01e`, 2026-08-29). Covered at render time and by `specpicks-site-functional-tests`. |

Full LLM check-id catalog: `lib/analyzer/llm_audit.py` (`SEO_AUDIT_CHECKLIST`,
133 checks). Check ids outside the whitelist are dropped.

## Manual operations

```bash
# One full run through systemd (preferred: same env as cron)
systemctl --user start agent-specpicks-seo-opportunity-agent.service

# Trigger via the framework API (token is FRAMEWORK_API_TOKEN in ~/.reusable-agents/secrets.env)
curl -X POST -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/specpicks-seo-opportunity-agent/trigger

# Run locally (bypasses systemd, picks up local code edits). Needs the DSN env
# var named by the site's data_sources.db.dsn_env, plus STORAGE_BACKEND/Azure env.
AGENT_ID=specpicks-seo-opportunity-agent \
SEO_AGENT_CONFIG=/home/voidsstr/development/specpicks/agents/seo-opportunity-agent/site.yaml \
PYTHONPATH=/home/voidsstr/development/reusable-agents \
python3 /home/voidsstr/development/reusable-agents/agents/seo-opportunity-agent/agent.py

# Re-run just the analyzer against an existing run
SEO_AGENT_CONFIG=/path/to/site.yaml \
python3 lib/analyzer/analyzer.py --agent-id <id> --run-ts 20260923T163000Z

# Re-render the report from an existing run (prints HTML, sends nothing).
# --agent-id is required: without it the script looks for a local run dir.
SEO_AGENT_CONFIG=/path/to/site.yaml \
python3 lib/reporter/send-report.py --agent-id <id> --run-ts 20260923T163000Z --dry-run
```

## Troubleshooting

See AGENT.md → Failure modes for the current known issues. The problems
below are listed there too; these are the longer write-ups.

### Run exits with status 1 within 1 second of the cron tick

This is almost always a `site.yaml` schema-validation failure. The agent
prints the offending field to stderr before exiting; run the entry command
manually to see it. Example from 2026-05-04:

```
Config validation failed for .../site.yaml:
  Additional properties are not allowed ('url_template' was unexpected)
  at: articles
```

**Cause:** a per-site `site.yaml` added a new field (`articles.url_template`)
without a matching update to `shared/schemas/site-config.schema.json`. The
schema's `articles` block has `additionalProperties: false`, so any unknown
key fails validation.

**Fix:** add the new field to the schema. Both per-site `site.yaml` files use
the same schema, so one schema change unblocks every site at once.

The same failure applies to **any** schema block with
`additionalProperties: false`, including the top level. When extending a
config block:

1. Add the field to `shared/schemas/site-config.schema.json`.
2. Document it with a real `description`.
3. Run the agent locally to confirm validation passes.
4. Commit the per-site YAML and the schema in the same change.

### Agent reports "loading state + queues" forever

That string is a phase label, not an error. If the dashboard shows `failure`
with that message, the agent crashed before its first phase update. This is
almost always config validation; same fix as above.

### Collector errors on GSC / GA4: OAuth token storage + refresh

The shared OAuth refresh token lives at **`~/.reusable-agents/seo/.oauth.json`**
(keys `client_id`, `client_secret`, `refresh_token`; mode 600). It powers both
`*-seo-opportunity-agent` instances, `*-gsc-coverage-auditor`,
`*-site-goals-tracker`, `oauth-heartbeat-agent`, and the sitemap submitter.

While the Google OAuth app is in **Testing** mode, Google revokes the refresh
token after **7 days without use**. The collector then fails with `token mint
failed … invalid_grant`. Use the operator wrapper:

```bash
bash install/refresh-gsc-token.sh status     # is it alive? (no browser)
bash install/refresh-gsc-token.sh refresh    # mint a fresh access token, reset the 7-day clock
bash install/refresh-gsc-token.sh reauth     # full re-consent; needs a browser/GUI session
bash install/reauth-gsc.sh                   # re-consent AND verify the granted scope (read-write webmasters)
```

- `refresh-token.py` requests read-write `auth/webmasters` by default since
  2026-08-25 (`b2d8d53`). With read-only scope, Sitemaps.submit returns 403.
  `GSC_READONLY=1` requests the narrow scope.
- Both SEO agents recover on their next tick, or run
  `systemctl --user start agent-<id>.service`.
- **Permanent fix:** set the OAuth consent screen to "In production" in Google
  Cloud Console.
- `oauth-heartbeat-agent` exists to call `refresh` daily, but its **timer was
  disabled** as of 2026-09-23.

### Analyzer skipped an LLM check / wrote no recs

- `SEO_DISABLE_LLM_AUDIT=1` in the environment turns the LLM pass off.
- Check ids the LLM invents outside `SEO_AUDIT_CHECKLIST` are dropped.
- An LLM error is caught (`LLM audit failed: …` in the log) and the run still
  reports `success` with rule-pass recs only. A run that finishes in about
  2 minutes with no `llm_check_id` recs is this case (seen 2026-09-19 to
  2026-09-22).
- Many deterministic rules have no input today; see the dormant table above.

### Detector fires the same rec every run (unsatisfiable inputs)

Three detectors used to treat "the collector never measured it" as "the site
is broken", and shipped the same high-priority recs every run. This was fixed
on 2026-08-18 (`fa5548c`). The rule for all detectors is: **an unmeasured
signal must never ship a rec.**

- `home-jsonld-missing` fires only when `site-signals.json` contains
  `homepage.jsonld_types` (a list). If the homepage fetch fails, the key is
  absent and the check is skipped.
- `new-page-<type>` needs `sitemap-urls.json`. With `complete: false`, the
  affected targets are skipped. If a target keeps firing even though the pages
  are live, the `sitemap_pattern` probably does not match the real URL layout.
  Past offenders: aisleprompt cuisine hubs live at `/recipes/cuisine/<slug>`
  but the pattern was `/cuisines/`; specpicks troubleshooting pages live under
  `/reviews/` but the pattern was `/(?:articles|guides)/`.
- `conversion-path` fires only when at least one source for the KPI was
  actually measured and every measured source is zero. The sources are
  `<id>_db_7d` / `<id>_db_30d` from db-stats and `<id>_event_28d` from a
  non-empty GA4 events report.

### Recs queued but implementer never ran

With `auto_implement: false`, the path is backlog-dispatcher →
`dispatch_now()`, with the auto-queue and drainer only as a fallback (see
[Rec flow](#rec-flow-to-the-implementer)). Check:

```bash
systemctl --user list-timers | grep backlog-dispatcher
tail -50 /tmp/reusable-agents-logs/agent-backlog-dispatcher-agent.log
systemctl --user status auto-queue-drainer.service     # fallback path
```

Also check `config/implementer-allowed-dispatch-kinds.json` (`allow` must
include `seo` or `*`). Recs flagged `duplicate: true` by AgentBase's title
dedup are skipped by design.

### Log full of `BlobArchived` errors / "no prior snapshot"

See AGENT.md → Failure modes. The prior-run mirror lists run dirs with the
10,000-key-capped `list_prefix()` and gets the oldest (archived) runs.

## Reuse

The whole pipeline is site-agnostic; every site-specific setting lives in
`site.yaml`. To add a site:

1. Copy `examples/sites/generic.yaml` to
   `<site-repo>/agents/seo-opportunity-agent/site.yaml`, plus a
   `db-queries.sql` with `-- @@QUERY: <name>` blocks.
2. Fill in `site.id`, `site.domain`, the GSC + GA4 ids, `data_sources.db`,
   `implementer` (including `allowed_paths`), and optionally `deployer`.
3. Add a manifest in the same dir whose `entry_command` sets `AGENT_ID` and
   `SEO_AGENT_CONFIG`. Keep DSNs out of the manifest; put them in
   `~/.reusable-agents/secrets.env` and name them via `dsn_env`.
4. Run `bash agents/register-with-framework.sh` in the site repo. It
   registers against `FRAMEWORK_API_URL`, default `http://localhost:8090`,
   and writes the systemd timer.
5. Seed goals: `install/seed-default-goals.sh`.

Full details: `../../docs/seo-onboard-new-site.md`.
