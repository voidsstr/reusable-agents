# IndexNow Submission Agent

Site-agnostic IndexNow (Bing/Yandex/Seznam/Naver) URL submitter. Runs every
15 minutes via the reusable-agents framework and notifies participating
search engines about new or changed URLs for every site in `sites.json`.

## What it does

For each site in `sites.json`, this agent collects URLs from THREE sources
and submits the deduped union to IndexNow:

1. **Static paths** — hardcoded SEO landing pages (homepage, /faq, /blog,
   /best, all `/vs/*`, all `/recipes/q/*` keyword pages, all `/best/*`
   collection pages, etc.). Submitted on every non-empty run.
2. **DB query sets** — incremental SQL queries against each site's tables,
   filtered by a per-site watermark (`updated_at > $last_run`). One query
   per indexable URL family (recipes, kitchen products, products,
   brands, articles, reviews, benchmarks, comparisons, categories…).
3. **Sitemap discovery** — fetches the site's `/sitemap.xml`, follows
   sitemap-index links one level, and submits any `<url>` whose `<lastmod>`
   is newer than the watermark. This is a *safety net* — it catches URLs
   that sitemap.xml emits but `sites.json` doesn't yet model, so the
   submitter stays in sync with the site's SSR sitemap automatically.

URLs are deduped, batched (≤10,000 per POST), and submitted to
`https://api.indexnow.org/indexnow`. Bing propagates to the other
participating engines. The watermark advances only if every batch
succeeded — failures retry on the next tick.

## Registered sites

| Site | Host | URL families | Key file |
|------|------|--------------|----------|
| aisleprompt | aisleprompt.com | recipes, recipe-categories, recipe-cuisines, kitchen-products, kitchen-categories + 36 static (incl. /best/* and /recipes/q/*) | `/fdae54f621e3caa4f5d2822a66955dcc.txt` |
| specpicks | specpicks.com | products (`/product/{asin}`), articles, categories, buying-guides, brands, reviews, benchmarks, trending-comparisons (`/vs/{a}/{b}`), retro-marketplace-categories + 23 static | `/549c4d17dad4b4906b0323e6cd6b1e6e.txt` |

IndexNow does NOT require a Bing API key. The only auth is the `<key>.txt`
file hosted at the site root — Bing fetches that URL to prove we control
the domain before accepting a submission. Both keys are checked into each
app's `frontend/public/` directory.

## URL coverage (verified against each site's sitemap.xml)

| Site | Bulk URL count (as of last seed) |
|------|----------------------------------|
| aisleprompt | ~136,000 (mostly recipe_catalog) |
| specpicks | ~52,000 (products + brands + benchmarks + comparisons) |

Run `./submit.sh --bulk --dry-run` to print the current candidate count
without hitting the API.

## Files

- `submit.ts` — site-agnostic submitter, driven entirely by sites.json
- `submit.sh` — wrapper (uses AislePrompt's node_modules for ts-node + pg)
- `sites.json` — per-site config (host, key, DB, query sets, sitemap URLs, watermark path)
- `manifest.json` — framework registration (cron `*/15 * * * *`)

## Schedule

`*/15 * * * *` America/Detroit (every 15 minutes). The reusable-agents
framework owns the systemd `--user` timer:

```
agent-indexnow-submitter.timer → agent-indexnow-submitter.service
```

15 minutes is the sweet spot:
- Watermark + batched POST means a no-op tick costs ~3 small DB queries
  per site (zero API calls when there are no candidates).
- IndexNow's search-engine indexing latency is 10–60 min anyway, so
  faster ticks don't make pages appear sooner.
- Matches the cadence of upstream content scrapers (kitchen-scraper
  every 30 min, recipe scrapers, etc.) — a new row hits IndexNow
  within ~15 min of insert.

## Manual runs

```bash
cd /home/voidsstr/development/nsc-assistant/agents/indexnow-submitter
./submit.sh                       # all sites, incremental (what cron runs)
./submit.sh --site=aisleprompt    # one site only
./submit.sh --bulk                # full catalog re-submit (initial rollout)
./submit.sh --dry-run             # log candidates without hitting the API
./submit.sh --no-sitemap          # DB-only run (skip sitemap parsing)
```

Trigger via the framework API (records a run row, surfaces in the UI):

```bash
curl -X POST http://localhost:8093/api/agents/indexnow-submitter/trigger \
    -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"
```

Log: `/tmp/reusable-agents-indexnow.log` (capped at 5MB).

## Adding a new site

1. Host a 32-char hex key at `https://<newhost>/<key>.txt` containing that
   key verbatim (Vite's `public/` or Express static works fine).
2. Add a site entry to `sites.json`:
   - `host`, `key`
   - `databaseUrlEnv` + `databaseUrlFallback`
   - `watermarkFile` — a path under `~/.reusable-agents/indexnow-submitter/`
   - `staticPaths` — SEO landing pages to ping on every non-empty run
   - `querySets` — one entry per table, with `bulkSql` / `incrementalSql`
     and a `urlTemplate`
   - `sitemapUrls` — list of sitemap.xml URLs to crawl as a safety net
3. Deploy the key file to production.
4. Run `./submit.sh --site=<name> --bulk` once to seed.

## Watermark behavior

A per-site ISO-timestamp text file. Read at start, advanced to the run's
start time only if every batch succeeded. If an entire run fails, the
next run picks up where this one left off. Manual reset:

```bash
rm /home/voidsstr/.reusable-agents/indexnow-submitter/<site>-watermark.txt
```

Next run will bulk-submit everything.

## URL template formats

- `"slug"` — uses `row.slug` directly (no slugify; the column already holds a clean slug)
- `"slugify:title|-|id"` — builds `${slugify(row.title)}-${row.id}` (the AislePrompt recipe URL shape)
- `"slugify:slug"` — slugifies a free-text column (e.g., `recipe_catalog.category`, `recipe_catalog.cuisine`)
- `"compose:left_ref|/|right_ref"` — joins two columns verbatim with a literal `/` (no slugify; used for `/vs/{asin}/{asin}` URLs)
- Literal separators in any template: `-`, `_`, `.`, `/`

## Sitemap discovery internals

The agent fetches each URL in `sitemapUrls`, parses `<urlset>` and
`<sitemapindex>` forms, and follows index links one level deep (max 25
fetches, 30s timeout per fetch). Cross-domain `<loc>` entries are
filtered out (IndexNow rejects URLs not on the registered host). On
incremental runs, only entries with `<lastmod>` newer than the watermark
are submitted; entries without `<lastmod>` are included on the
assumption they're static / always-fresh pages.

The DB queries remain the primary discovery mechanism (faster, more
precise, no HTTP round-trip). Sitemap discovery exists to catch the
"oh, the site started serving a new URL family and we forgot to add it
here" case automatically.

## When a new URL pattern goes live on a site

You usually have a choice:
- **Cheap**: do nothing — the sitemap fallback will pick it up on the
  next tick (assuming the site's sitemap.xml emits it with a fresh
  `<lastmod>`).
- **Better**: model it explicitly in `sites.json` so the DB watermark
  catches it within seconds of insert (the sitemap is cached and may
  lag by an hour). Add a `querySet`, regenerate the AGENT.md table,
  and re-register: `bash scripts/register-agents.sh`.
