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

**Google Ads** (optional, only if `data_sources.google_ads` is configured): 4
reports across the last 90 days. Enables `paid-organic-gap` and
`ad-copy-headline-winner` recommendations from the analyzer.

- `ads-search-terms.json` — search-term-view: what users actually typed.
- `ads-keyword-perf.json` — keyword-view: targeted keywords + match types.
- `ads-ad-copy.json` — ad-group-ad: top-CTR responsive search ad headlines.
- `ads-paid-vs-organic.json` — paid-organic-search-term-view (the goldmine).
  Pairs each search term with paid clicks/impressions AND organic
  clicks/impressions side-by-side, surfacing queries paid wins but organic
  loses.

Setup: install the optional google-ads SDK (`pip install google-ads`) — without
it the collector falls back to the REST API via urllib (slower; same data).
The collector reuses the OAuth file from `auth.oauth_file`; the bootstrap
script now requests the `https://www.googleapis.com/auth/adwords` scope along
with GSC + GA4. Existing OAuth files keep working for GSC/GA4 — re-run
`refresh-token.py --bootstrap` once to add the Ads scope.

The Ads SDK requires a developer-token (set in
`data_sources.google_ads.developer_token`) and the target customer id
(`customer_id`). MCC-managed accounts also need `login_customer_id`. All
errors are non-fatal — empty result files are written and the run continues.

**Page-type inventory crawl** (optional, only if `page_inventory` is set in
the site config): the collector reads the site's sitemap (and recurses into
sitemap-indexes), filters URLs by per-type regex, samples up to `sample_size`
per type (weighted by GSC impressions when available), and crawls the sampled
pages with a polite 1.5s delay + per-URL HTML cache. Output:

- `pages-by-type.jsonl` — one record per crawled page: `{url, type, status,
  html_size, fetched_at, cache_path, title, description, h1, canonical, body_text}`.
- `page-cache/<sha1>.html` — persistent across runs. The collector sends
  `If-Modified-Since` from the cache file's mtime; 304 responses use cached
  HTML and skip re-parsing.

The analyzer feeds these records (capped at `analyzer.max_llm_audit_pages`,
default 30, per site) into the LLM audit pass. Each record's `type` field
activates page-type-specific checks (`recipe-*`, `product-*`, `h2h-*`,
`article-*`, `feature-*`).

**Repository scan** (always runs when `implementer.repo_path` is configured):
the collector walks the site's repo, identifies SEO-surface files (server
routes, page components, sitemap generators, files with `<title>`,
`Helmet`, `<meta name=`, JSON-LD, etc.), and writes:

- `repo-routes.json` — `{routes: [{url_pattern, file, line, emits, missing,
  todos}]}`. The analyzer wires this into each rec's
  `implementation_outline.files` so recommendations point at concrete file
  locations the implementer can read.

The repo scan skips: `node_modules`, `.git`, `dist`, `build`, `.next`, `.venv`,
`__pycache__`, `*.lock`, `*.log`, `*.db`, test/spec files. No external
network calls; safe to run on locked-down hosts.

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
`https://www.googleapis.com/auth/webmasters.readonly`,
`https://www.googleapis.com/auth/analytics.readonly`, and (for the optional
Google Ads collector) `https://www.googleapis.com/auth/adwords` scopes.

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
(`seo-analyzer`, `seo-reporter`, `implementer`, `seo-deployer`) all
read from the run dir this collector produces.
