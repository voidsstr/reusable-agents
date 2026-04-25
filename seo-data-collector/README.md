# seo-data-collector

Pulls fresh **Google Search Console + Google Analytics 4 + (optional) production
DB** data for one or more sites. Writes a standardized run-dir layout that
every other agent in this suite reads from.

This agent is site-agnostic — point it at your config and it works.

## What gets collected

**Google Search Console** (18 reports per site):
- 90-day window: queries (top 5000), pages (top 5000), query+page pairs (5000),
  query×device, query×country, page×device, page×country, devices, countries
  (top 50), search-appearance, daily date trend.
- 28-day window: queries (top 2000), pages (top 2000) — fresh-rank-shift signal.
- Prior 90-day window: queries, pages, query+page pairs — for trend comparison.
- US-only segment: queries (top 1000), pages (top 1000) — the rank that pays.

**Google Analytics 4** (23 reports):
- Site-wide totals (28d + 90d).
- Landing pages (28d + 90d, top 250).
- Pages (28d + 90d, top 500).
- Events (28d + 90d, top 100, with revenue events highlighted via config).
- Geo, devices, traffic-source channel groups, session source/medium.
- US-only segments.
- **Organic-search-only segment** (the SEO truth-set): summary, landing pages,
  events, geo, source.

**Production DB** (optional, only if `data_sources.db` is configured): catalog
stats, content coverage, and revenue KPI counts (rows in `instacart_clicks`,
`kitchen_click_events`, etc.).

**Derived files** (computed locally from the raw data):
- `gsc-top5-targets.json` — keywords currently ranking pos 6-50 with measurable
  demand. The primary work-list for the analyzer.
- `gsc-us-top5-targets.json` — same, US-only segment.
- `gsc-striking-distance.json` — pos 4-10.
- `gsc-quick-wins.json` — pos 11-30.
- `gsc-zero-click.json` — pos ≤20, ≥10 impressions, 0 clicks (CTR/snippet fix).
- `gsc-rank-improvements.json` / `gsc-rank-regressions.json` — pages whose
  position moved most vs. prior 90d.
- `gsc-position-distribution.json` — count of pages per SERP bucket.

## Quick start

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Bootstrap OAuth once (interactive — needs a browser)
python3 refresh-token.py --bootstrap

# 3. Make a config
cp ../examples/sites/generic-config.yaml my-site.yaml
# (edit my-site.yaml with your GSC site URL, GA4 property id, etc.)

# 4. Pull data
SEO_AGENT_CONFIG=my-site.yaml python3 pull-data.py

# Output:
#   ~/.reusable-agents/seo/runs/<site-id>/<UTC-ts>/data/*.json
#   ~/.reusable-agents/seo/runs/<site-id>/latest -> <UTC-ts>
```

## Output

Files land in `<runs_root>/<site-id>/<UTC-ts>/` (config controls runs_root):

```
data/
  gsc-queries-90d.json       gsc-pages-90d.json
  gsc-query-page-90d.json    gsc-query-device-90d.json
  gsc-page-country-90d.json  gsc-us-queries-90d.json
  ga4-organic-landing-90d.json   ga4-summary-28d.json
  ga4-events-28d.json        db-stats.json (if configured)
  ... (41 reports total + 7 derived)
```

The `latest` symlink points at the most recent run dir for that site.

## OAuth setup

This agent uses Google's OAuth2 with the
`https://www.googleapis.com/auth/webmasters.readonly` and
`https://www.googleapis.com/auth/analytics.readonly` scopes.

`refresh-token.py --bootstrap` walks you through the OOB flow once. After
that, the access token is auto-refreshed each run from `.oauth.json`
(stored at `auth.oauth_file` in your config; default
`~/.reusable-agents/seo/.oauth.json`, mode 0600).

## Configuration

See [`../examples/sites/generic-config.yaml`](../examples/sites/generic-config.yaml)
for the full schema. The collector reads:

- `data_sources.gsc.site_url` — `sc-domain:example.com` or `https://example.com/`
- `data_sources.ga4.property_id` — numeric GA4 property id
- `data_sources.db` — optional; supports `docker-exec`, `postgres`, `sqlite`
- `auth.oauth_file` — where to read/write OAuth state
- `runs_root` — where to write the run dir

## Reuse: standalone vs orchestrated

This collector works on its own — useful if you just want the broadest GSC/GA4
data dump and you'll do your own analysis. The downstream sub-agents
(`seo-analyzer`, `seo-reporter`, `seo-implementer`, `seo-deployer`) all
read from the run dir this collector produces.
