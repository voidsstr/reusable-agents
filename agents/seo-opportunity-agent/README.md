# seo-opportunity-agent

The reusable SEO + revenue audit pipeline. Runs every 2-3 hours per site,
pulls Google Search Console + Google Analytics 4 (+ optional Google Ads +
production-DB content inventory), scores opportunities, queues every
recommendation to the implementer, and emails a notification of what
just shipped.

This README covers the **engine itself**. Per-site instances live next to
each site's manifest:

- `specpicks/agents/seo-opportunity-agent/` (id `specpicks-seo-opportunity-agent`)
- `nsc-assistant/agents/aisleprompt-seo-opportunity-agent/`

Both invoke `agent.py` here with `AGENT_ID` + `SEO_AGENT_CONFIG` env vars.

## Architecture

One AgentBase agent that runs three phases sequentially under a single
`run_ts` so the collector, analyzer, and finalizer all write to the same
Azure run-dir (`agents/<agent_id>/runs/<run_ts>/`).

```
┌──────────── one cron tick ──────────────────────────────────────────┐
│  Phase 1: collector   pull GSC + GA4 (+ Ads / DB / page-inventory)  │
│            ↓                                                         │
│  Phase 2: analyzer    score recs, write recommendations.json         │
│            ↓                                                         │
│  Phase 3: finalize    render report, auto-queue recs to implementer, │
│                       record outbound for Confirmations page         │
└──────────────────────────────────────────────────────────────────────┘
```

Each phase has its own README under `lib/`:

| Phase | Code | README |
|---|---|---|
| 1. Collector | `lib/collector/pull-data.py` | [`lib/collector/README.md`](lib/collector/README.md) |
| 2. Analyzer  | `lib/analyzer/analyzer.py` (+ `llm_audit.py`) | [`lib/analyzer/README.md`](lib/analyzer/README.md) |
| 3. Finalize  | `lib/reporter/send-report.py` + `finalizer.py` | [`lib/reporter/README.md`](lib/reporter/README.md) |

The two top-level files glue everything together:

- `agent.py` — `AgentBase` class. Handles run-ts management, status
  updates, run-dir lifecycle, and subprocesses each phase script with
  the same `RUN_TS` + `AGENT_ID` env so all artifacts land in one
  directory.
- `finalizer.py` — phase 3. Renders the digest entry, drops auto-queue
  records, and records outbound dispatches.

> **Legacy note:** `agents/seo-data-collector/`, `agents/seo-analyzer/`,
> and `agents/seo-reporter/` directories have been collapsed into this
> agent's `lib/` subfolders. The standalone copies are retired — keep
> changes in `lib/`. The catalog/architecture docs are being updated to
> reflect this.

## Lifecycle of one run

1. Cron tick triggers a per-site systemd unit. The unit's
   `entry_command` (from the per-site manifest) sets `AGENT_ID`,
   `SEO_AGENT_CONFIG`, `DATABASE_URL`, and `PYTHONPATH`, then invokes
   `python3 agent.py`.
2. `agent.py` validates `site.yaml` against
   `shared/schemas/site-config.schema.json`. Any extra/missing field
   here exits the run with status 1 immediately — see
   [Troubleshooting](#troubleshooting) below.
3. **Collector** pulls fresh data into `data/`:
   - 18 GSC reports (90d / 28d / prior-90d / US-only segments).
   - 23 GA4 reports (28d + 90d, organic-segment, events, geo, devices).
   - `articles-inventory.json` if `articles.sources` is configured.
   - `pages-by-type.jsonl` from a sitemap-driven sample crawl, if
     `page_inventory` is configured.
   - `repo-routes.json` from a static scan of the implementer's repo
     so recs can point at file:line locations.
   - 7 derived files (`gsc-top5-targets.json`, `gsc-striking-distance.json`,
     `gsc-zero-click.json`, etc.) computed locally from the raw reports.
4. **Analyzer** runs deterministic rule passes + an LLM audit pass:
   - Snapshot + comparison vs prior run (`snapshot.json`,
     `comparison.json`).
   - Goal scoring against the prior run's declared goals
     (`goal-progress.json`).
   - Rec generation. Every rec has `id`, `type`, `priority`, `title`,
     `rationale`, `expected_impact`, `implementation_outline`, and
     `data_refs`. Stable `id`s let the dashboard track rec lifecycle
     across runs.
5. **Finalize** writes `recommendations.json` to the digest queue and
   drops one record per rec into
   `agents/responder-agent/auto-queue/<request-id>.json`. The
   responder-agent picks those up the next minute and dispatches the
   batch to the implementer. Implementer chains to the deployer when
   `site.yaml.deployer` is configured (else no-op).

## Run-dir layout

```
agents/<agent-id>/runs/<UTC-ts>/
├── status.json           # current/final phase + progress
├── decisions.jsonl       # AgentBase decision log (one line per phase + warnings)
├── data/                 # collector outputs
│   ├── gsc-queries-90d.json
│   ├── gsc-pages-90d.json
│   ├── gsc-query-page-90d.json
│   ├── gsc-{striking-distance,top5-targets,zero-click,...}.json
│   ├── ga4-organic-landing-90d.json
│   ├── ga4-events-{28d,90d}.json
│   ├── articles-inventory.json
│   ├── pages-by-type.jsonl
│   ├── page-cache/<sha1>.html
│   ├── db-stats.json
│   └── repo-routes.json
├── snapshot.json         # this-run aggregate
├── comparison.json       # delta vs prior run
├── goal-progress.json    # prior run's goals scored against current
├── goals.json            # this run's declared goals
├── recommendations.json  # ranked recs (THE contract for downstream)
├── run-summary.md        # human-readable digest
└── digest-entry.html     # rendered email body fragment
```

## Configuration

Configuration lives in each site's `site.yaml`. It's validated against
`shared/schemas/site-config.schema.json` at the start of every run.

The minimum-viable config:

```yaml
site:
  id: my-site
  domain: example.com

data_sources:
  gsc:
    site_url: sc-domain:example.com
  ga4:
    property_id: "1234567890"

reporter:
  email:
    to: [seo@example.com]

implementer:
  repo_path: /home/voidsstr/development/my-site
  branch: master
```

See [`../../examples/sites/generic-config.yaml`](../../examples/sites/generic-config.yaml)
for the full schema. The most-used optional blocks:

| Block | What it enables |
|---|---|
| `articles` | Article inventory pull from prod DB. Lets the analyzer surface per-article SEO recs (snippet rewrite, title fix, orphan boost, missing affiliate tags). Both AislePrompt and SpecPicks use this. |
| `articles.url_template` | Canonical public URL pattern (e.g. `https://specpicks.com/reviews/{slug}`). Used by implementer + reporter for emails. **This field was added 2026-05-04 and required a schema-block update — see Troubleshooting.** |
| `page_inventory` | Per-page-type sample crawl from the sitemap. Drives the LLM audit pass. |
| `coverage_targets` | Auto-emit `new-page-<type>` recs when site has fewer than `expected_min` pages of a type. |
| `data_sources.google_ads` | 4 extra reports + `paid-organic-gap` and `ad-copy-headline-winner` rec types. |
| `deployer` | Per-batch test → build → push → deploy → smoke chain after the implementer ships. See [`../seo-deployer/README.md`](../seo-deployer/README.md). |
| `competitor_keyword_gap` | Pull a target competitor's GSC-visible keywords and emit `competitor-keyword-gap` recs for the deltas. |

## Per-site instances

| Agent ID | Manifest dir | Cron | Repo |
|---|---|---|---|
| `specpicks-seo-opportunity-agent` | `specpicks/agents/seo-opportunity-agent/` | `0 */2 * * *` (every 2h, top of hour) | specpicks |
| `aisleprompt-seo-opportunity-agent` | `nsc-assistant/agents/aisleprompt-seo-opportunity-agent/` | `15 */2 * * *` (every 2h at :15) | nsc-assistant |

The :15 offset prevents the two from competing for the same LLM
provider quota at the same instant.

A new site adds a third entry. See
[`../../docs/seo-onboard-new-site.md`](../../docs/seo-onboard-new-site.md)
for the 5-step onboarding.

## Auto-queue + confirmation flow

Both per-site instances run with
`confirmation_flow.kind = auto-queue-with-notification`. Practically:

- Every rec from every run is dropped into
  `agents/responder-agent/auto-queue/<request-id>.json` automatically.
- The responder-agent (every-minute cron) batches by `agent_subject_tag`
  and dispatches to the implementer.
- The email is **informational** — reply only to override:
  - `defer rec-NNN` — push back to next run.
  - `skip rec-NNN` — drop entirely.
  - `revert rec-NNN` — reverse a rec that already shipped.

This keeps the human reviewer optional: at full automation the loop is
collector → analyzer → implementer → deployer with no human in the
critical path.

## LLM provider

The analyzer's LLM audit pass uses the framework's pluggable provider
config under `agent_id = seo-analyzer`. Override per site:

```yaml
analyzer:
  ai_provider: anthropic       # optional
  ai_model: claude-opus-4-7    # optional
```

Set globally via the dashboard at
`http://localhost:8091/providers` (agent_id `seo-analyzer`).

Disable the LLM pass entirely via env: `SEO_DISABLE_LLM_AUDIT=1`.

## Goals + recommendation types

The analyzer scores against 9 default goals + emits recs across these
types (deterministic + LLM passes combined, deduped by `(url, type)`):

| Family | Rec types |
|---|---|
| Rank | `striking-distance`, `top5-rank-target`, `quick-win`, `competitor-keyword-gap` |
| Indexing | `indexing-fix`, `sitemap-shrank`, `broken-internal-link`, `orphan-boost` |
| CTR / SERP appearance | `zero-click`, `snippet-rewrite`, `title-fix`, `meta-description-cta`, `meta-title-keyword`, `meta-title-length` |
| Schema | `schema-product-missing`, `schema-faqpage-missing`, `schema-incomplete`, `schema-invalid`, `schema-regression`, `collection-numberOfItems` |
| On-page | `h1-missing`, `h1-multiple`, `heading-hierarchy`, `content-thin`, `image-alt-missing`, `body-link-density`, `pros-cons-density`, `article-template-completeness` |
| E-E-A-T + AI search | `eeat-author-missing`, `eeat-citations-missing`, `eeat-publish-date-missing`, `geo-direct-answer-missing`, `geo-faq-missing`, `geo-llms-txt-missing`, `freshness`, `faq-quality` |
| CWV / mobile | `mobile-viewport-missing`, `cwv-render-blocking`, `cwv-image-no-dimensions`, `cwv-ttfb-slow` |
| Trust + structure | `footer-trust-links`, `breadcrumb-parity`, `trust-signal-density`, `hreflang` |
| Revenue (PDP + article × product) | `revenue-focus`, `affiliate-tag-leak`, `paid-organic-gap`, `ad-copy-headline-winner`, `conversion-path` |
| Content gap | `new-page-<type>` (one per type in `coverage_targets`) |

Full check-id catalog: `lib/analyzer/llm_audit.py.SEO_AUDIT_CHECKLIST`.

## Manual operations

```bash
# Trigger a run via the framework API
TOKEN=$(grep '^FRAMEWORK_API_TOKEN=' /home/voidsstr/development/reusable-agents/.env | cut -d= -f2-)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://localhost:8093/api/agents/specpicks-seo-opportunity-agent/trigger

# Run the agent locally (bypasses systemd, picks up local code edits)
AGENT_ID=specpicks-seo-opportunity-agent \
SEO_AGENT_CONFIG=/home/voidsstr/development/specpicks/agents/seo-opportunity-agent/site.yaml \
DATABASE_URL='postgresql://...' \
PYTHONPATH=/home/voidsstr/development/reusable-agents \
python3 /home/voidsstr/development/reusable-agents/agents/seo-opportunity-agent/agent.py

# Re-run just the analyzer against an existing collector run
SEO_AGENT_CONFIG=/path/to/site.yaml \
python3 lib/analyzer/analyzer.py --agent-id <id> --run-ts 20260504T120000Z

# Re-render the report from an existing run (no email)
SEO_AGENT_CONFIG=/path/to/site.yaml \
python3 lib/reporter/send-report.py --run-ts 20260504T120000Z --no-email --dry-run
```

## Troubleshooting

### Run exits with status 1 within 1 second of cron tick

Almost always a `site.yaml` schema-validation failure. The agent
prints the offending field to stderr before exiting; capture it by
running the entry command manually. Example from 2026-05-04:

```
Config validation failed for .../site.yaml:
  Additional properties are not allowed ('url_template' was unexpected)
  at: articles
```

**Cause:** a per-site `site.yaml` introduced a new field
(`articles.url_template`) without a matching update to
`shared/schemas/site-config.schema.json`. The schema's `articles`
block has `additionalProperties: false`, so any unknown key fails
validation.

**Fix:** add the new field to the schema. Schema lives under
`shared/schemas/`. Both per-site `site.yaml` files inherit the same
schema — adding a field there unblocks every site instance at once.

The same failure mode applies to **any** schema block with
`additionalProperties: false`. When extending a config block:

1. Update the field's home in `shared/schemas/site-config.schema.json`.
2. Document the field with a real `description` (helpful for the
   onboarding doc + future readers).
3. Run the agent locally to confirm validation passes.
4. Commit both the per-site YAML and the schema in the same change.

### Agent reports "loading state + queues" forever

That string is a phase label, not an error. If the dashboard says
`failure` with that message, the agent crashed before the first phase
status update. Almost always config validation — same fix as above.

### Collector errors on GSC / GA4

OAuth tokens auto-refresh via `lib/collector/refresh-token.py`. If
they expired beyond refresh, run
`python3 lib/collector/refresh-token.py --bootstrap` once
interactively to re-grant. New scopes (e.g. Ads) require re-bootstrap.

### Analyzer skipped an LLM check / wrote no recs

- `SEO_DISABLE_LLM_AUDIT=1` in env disables the LLM pass entirely.
- Hallucinated `check_id`s outside `SEO_AUDIT_CHECKLIST` are silently
  filtered — see `lib/analyzer/llm_audit.py` for the whitelist.
- Each rule is opt-in via `site.yaml` flags. A site without `articles`
  configured won't emit `affiliate-tag-leak` or `revenue-focus` recs.

### Recs queued but implementer never ran

Check the responder-agent: `systemctl --user status agent-responder-agent`.
The auto-queue at `agents/responder-agent/auto-queue/` should drain
within a minute of the run completing. If files accumulate, the
responder is down — restart it and the queue replays.

### Manual short-circuit recovery

If a run dies mid-phase, the next cron tick starts fresh. The Azure
run-dir survives; you can re-run a phase against an old `run_ts` via
the manual ops above. Decisions log lives at
`<run-dir>/decisions.jsonl` and the framework's `live-llm-output`
endpoint streams the LLM audit pass in real time.

## Reuse

The whole pipeline is site-agnostic — every site-specific knob lives
in `site.yaml`. To add a third site:

1. Copy `examples/sites/generic-config.yaml` to
   `<site-repo>/agents/<site>-seo-opportunity-agent/site.yaml`.
2. Fill in `site.id`, `site.domain`, GSC + GA4 credentials, optional
   `articles` block, optional `deployer`.
3. Drop a manifest in the same dir with `entry_command` exporting
   `AGENT_ID` + `SEO_AGENT_CONFIG`.
4. Re-run `bash agents/register-with-framework.sh` to wire systemd.
5. (Optional) add `DATABASE_URL_<UPPER_SITE>` to the framework env so
   the implementer can resolve the right DSN.

Full details: [`../../docs/seo-onboard-new-site.md`](../../docs/seo-onboard-new-site.md).
