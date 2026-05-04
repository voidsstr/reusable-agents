# gsc-coverage-auditor — runbook

Per-site GSC URL Inspection auditor. Walks the URL universe (DB + sitemap)
in oldest-first order and calls Google's URL Inspection API on each, storing
verdict + coverageState + lastCrawlTime per URL. The seo-analyzer reads the
output JSONL and emits recommendations for "Crawled — currently not indexed",
"Page with redirect", and other indexing-blocking states.

## Inputs

- `sites.json` (symlinked to indexnow-submitter's) — same DB queries +
  sitemap URLs. We reuse so adding a new URL pattern only happens once.
- OAuth token at `~/.reusable-agents/seo/.oauth.json`. Same scope set
  as seo-data-collector — `webmasters.readonly` covers URL Inspection.
- Env:
  - `GSC_INSPECT_SITE=<aisleprompt|specpicks>` — required
  - `GSC_INSPECT_LIMIT=500` — URLs per run (Google quota: 2k/day/property)
  - `GSC_INSPECT_QPS=1.0` — rate limit (1 req/sec is polite)
  - `GSC_INSPECT_STATE_DIR=~/.reusable-agents/gsc-coverage-auditor`
  - `GSC_INSPECT_DRY_RUN=1` — list URLs to inspect, no API calls

## Outputs

- `<state-dir>/<site>-coverage.jsonl` — append-only inspection log.
  Each row is one inspection result (see flatten_inspection in
  inspect.py). The seo-analyzer reads this file.
- `<state-dir>/<site>-last-inspected.json` — `{url: ISO-ts}` map of when
  each URL was last inspected. Drives the round-robin pick policy.

## Quota math

- Google quota: 2,000 inspections/day per GSC property.
- Default budget: 500/run. With 1 run/day per site, that's 25% of quota
  consumed, leaving 75% for ad-hoc usage / SEO data-collector / spot-checks.
- AislePrompt has ~100K URLs, SpecPicks has ~22K. At 500/day the round
  trip is:
  - SpecPicks: 22K / 500 = 44 days for a full sweep
  - AislePrompt: 100K / 500 = 200 days for a full sweep
- That's slow; consider raising LIMIT to 1500 if quota allows. The script
  is idempotent (each URL's last-inspected timestamp prevents duplicate
  work in the same window).

## How the analyzer consumes this

`seo-analyzer/analyzer.py` imports the latest coverage JSONL via the
collector handoff, then `_add_index_coverage_recs()` emits:

| coverageState | Recommendation | Routes to |
|---|---|---|
| Crawled — currently not indexed | Content quality fix (rewrite, expand) | article-author |
| Discovered — currently not indexed | Crawl budget pressure or redirect chain | implementer (canonical fix) |
| Submitted and indexed, with issues | Schema / mobile / image issues | implementer |
| Page with redirect | Unintended canonical chain | implementer |
| URL is unknown to Google | IndexNow or sitemap reachability | indexnow-submitter (manual re-fire) |

## Manual run

```bash
GSC_INSPECT_SITE=specpicks GSC_INSPECT_LIMIT=10 GSC_INSPECT_DRY_RUN=1 \
  python3 inspect.py
```

Then drop `DRY_RUN=1` to actually call the API.
