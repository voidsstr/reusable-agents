# IndexNow Submitter (`indexnow-submitter`)

> Submits each site's new and changed URLs to IndexNow
> (`api.indexnow.org`, shared by Bing, Yandex, Seznam, and Naver). It also
> checks the site's sitemaps and resubmits them to Google Search Console
> (GSC). North Star link: **indexed pages → organic clicks**. A new page
> reaches the engines within one tick of its DB insert, without waiting for
> a crawl.

**One runbook for the whole family.** These files are symlinks to this one:
`aisleprompt: agents/indexnow-submitter/AGENT.md`,
`nsc-assistant: agents/specpicks-indexnow-submitter/AGENT.md`, and
`nsc-assistant: agents/specpicks-indexnow-bulk/AGENT.md`. The `SKILL.md`
files (including `aisleprompt: agents/indexnow-bulk/SKILL.md`) are linked
the same way. Edit this file only.

## At a glance

| | |
|---|---|
| Engine id | `indexnow-submitter` (class default). Runs use the per-site `AGENT_ID` |
| Home | `reusable-agents: agents/indexnow-submitter/` |
| Kind | AgentBase python engine (`agent.py` → `IndexnowSubmitter`; converted 2026-05-11 per its docstring, first committed 2026-05-13 in `ab2fc5e`). It subprocess-runs the TypeScript worker `submit.ts` via `npx ts-node`. There is no manifest in the engine dir; the dir is **not registered** itself |
| Entry (instances) | `agent_run_wrapper.sh <id> bash <instance>/run.sh`. `run.sh` exports `INDEXNOW_SITE=<site>`, `AGENT_ID=<id>`, and, for the bulk instances, `INDEXNOW_BULK=1`. It then runs `exec python3 …/agents/indexnow-submitter/agent.py` |
| Category | `seo` |
| Legacy | `nsc-assistant: agents/indexnow-submitter/manifest.json` (id `indexnow-submitter`, `enabled: false`, entry points at a `submit.sh` that no longer exists there). It is not registered as a timer |

### Instances

| Instance id | Home | Mode | Schedule (systemd, host TZ America/Detroit) | Timer | Latest run (2026-09-23) |
|---|---|---|---|---|---|
| `aisleprompt-indexnow-submitter` | `aisleprompt: agents/indexnow-submitter/` | incremental | Manifest `*/15 * * * *` America/Detroit; `OnCalendar=*-*-* *:0/15:00` | enabled | 61 runs (03:15–18:01 UTC), all success with `failed=0`: 269–274 URLs per tick (403 on the first tick), sitemap pings `15/15` except 3 ticks (`11/13` ×2, `14/15`). Latest 18:01 UTC: `submitted 274 URLs … ok=15/15` |
| `specpicks-indexnow-submitter` | `nsc-assistant: agents/specpicks-indexnow-submitter/` | incremental | Manifest `18 */5 * * *`; `OnCalendar=*-*-* 00/5:18:00` | enabled | 14:18 UTC: `submitted 3277 URLs (failed=0) … ok=3/3` |
| `aisleprompt-indexnow-bulk` | `aisleprompt: agents/indexnow-bulk/` | `--bulk` | `OnCalendar=*-*-* 09:00:00` | enabled | 13:01 UTC: `submitted 196837 URLs … ok=15/15` |
| `specpicks-indexnow-bulk` | `nsc-assistant: agents/specpicks-indexnow-bulk/` | `--bulk` | `OnCalendar=*-*-* 10:00:00` | enabled | 14:00 UTC: `submitted 98764 URLs … ok=3/3` |

The bulk instances have their own dirs and are listed here because they
run this engine. `aisleprompt-indexnow-bulk` has its own `AGENT.md`;
`specpicks-indexnow-bulk` uses this file through the symlink above. The aisleprompt manifest description
("every 5 hours") is stale: the cron is every 15 minutes.

## What it does

`agent.py` → `run()`:

1. **Resolve the site and mode.** The site comes from `INDEXNOW_SITE` (or `INDEXNOW_TARGET_SITE`); empty means all configured sites. `INDEXNOW_BULK=1` selects bulk mode.
2. **Pick node modules.** Uses `$INDEXNOW_TS_APP_DIR/node_modules` (default `/home/voidsstr/development/specpicks`). If that dir is absent, as on a freshly cloned host, it falls back to `NODE_PATH` or `npm root -g` with `npx --no-install`.
3. **Run the worker.** Executes `npx ts-node --transpile-only … submit.ts [--site=<site>] [--bulk]` with timeout `INDEXNOW_TIMEOUT_S` (900 s). The output is written to a `NamedTemporaryFile` `/tmp/tmp*.log`, which is never deleted and not echoed to the unit log.
4. **submit.ts, per site:**
   1. **Config.** Loads `SITE_CONFIG_PATHS` if set. Otherwise it auto-discovers `~/development/{aisleprompt,specpicks}/agents/seo-config/site-indexnow.json` (both are loaded, then filtered by `--site`). The legacy `sites.json` in this dir is the last-resort fallback.
   2. **Watermark.** Reads the ISO timestamp in `watermarkFile` (default epoch).
   3. **Query sets.** Sets with `incrementalSql` run it with `$1` = watermark. `bulkOnly` sets **always** run their `bulkSql`. `--bulk` runs every `bulkSql`. `$SITE_ID` and `$SITE_IDS` are interpolated.
   4. **Sitemaps.** Fetches `sitemapUrls`, following index files to depth 2 with at most 25 fetches and a 30 s timeout each. Only same-host `<loc>` entries count. Incremental runs keep entries whose `<lastmod>` ≥ watermark, or whose `<lastmod>` is missing or unparseable.
   5. **Static paths.** Adds every `staticPaths` entry when any dynamic URL was found (always in bulk mode).
   6. **Submit.** POSTs batches of ≤ 10,000 URLs to `https://api.indexnow.org/indexnow` with `{host, key, keyLocation: https://<host>/<key>.txt, urlList}`.
   7. **Advance the watermark.** Sets it to the run's start time only if `failed == 0`. That includes runs with nothing to submit, and **dry runs**.
   8. **Report.** Prints `[indexnow:<site>] done submitted=N failed=M`, and exits 1 if any site hit a fatal error.
5. **Parse counts.** Parses the `done` lines. Only the site's own lines count when a site is set.
6. **Layer-A metrics.** Records `goal-urls-submitted-30d` and `goal-runs-success-rate-7d` through `metric_helper.record_many`.
7. **Canonical coverage.** Runs only when a site is set and the legacy `sites.json` entry has `canonical_urls_endpoint`. It fetches that endpoint, diffs it against every `<loc>` in the sitemaps, and emits `canonical_urls_*` and `sitemap_coverage_pct` metrics. Missing URLs **overwrite** `~/.reusable-agents/indexnow-submitter/<site>.force-submit.txt` (at most 5,000).
8. **Sitemap checks and GSC submit.** Uses the legacy `sites.json` `sitemapUrls`, and runs for all sites when no site is set. For each sitemap it sends a HEAD request; if that returns 2xx, it PUTs `webmasters/v3/sites/<gsc_property>/sitemaps/<sitemap>`. Finally, one robots.txt check looks for a `Sitemap:` line. With 7 sitemaps, aisleprompt makes 7 + 7 + 1 = 15 checks; specpicks makes 1 + 1 + 1 = 3.
9. **Result.** A non-zero `submit.ts` rc gives `RunResult(failure)`, with the metrics kept. Otherwise `success`, with the summary `submitted N URLs (failed=M) to IndexNow across K site(s); sitemap pings ok=X/Y`.

### URL families (per-site `site-indexnow.json`, 2026-09-23)

| Site | Query sets (`*` = bulkOnly, re-sent every run) | Static paths | Sitemaps |
|---|---|---|---|
| aisleprompt | `recipes`, `kitchen-products`, `kitchen-categories*`, `recipe-categories*`, `recipe-cuisines*`, `blog-articles` | 36 | 6 in `site-indexnow.json`; 7 in legacy `sites.json` (`sitemap-recipes-1/-2.xml`) |
| specpicks | `products`, `articles`, `categories*`, `buying-guides*`, `brands`, `reviews`, `benchmarks`, `trending-comparisons-product` (`/vs/<a>/<b>`), `trending-comparisons-hardware` (`/compare/…`), `retro-marketplace-categories*` | 23 | 1 (`/sitemap.xml`) |

Observed candidate mix (captured `submit.ts` output, 2026-09-23):

- **aisleprompt 17:15 UTC:** `recipes=0 kitchen-products=0 kitchen-categories=10 recipe-categories=94 recipe-cuisines=126 blog-articles=8 sitemap=0 static=36 → 274`. The three bulkOnly sets always return rows, so the same ~230 category/cuisine URLs plus the 36 static paths are resubmitted on every 15-minute tick.
- **specpicks 14:18 UTC:** `categories=56 buying-guides=56 trending-comparisons-product=2931 trending-comparisons-hardware=200 retro-marketplace-categories=8 sitemap=22 static=23 → 3277`.

## Inputs

| Input | Detail |
|---|---|
| Per-site config (worker) | `<site repo>/agents/seo-config/site-indexnow.json`: `name`, `host`, `key`, `databaseUrlEnv` / `databaseUrlFallback`, `siteIds`, `watermarkFile`, `staticPaths`, `querySets[]`, `sitemapUrls`, `gsc_property`. Shared with `gsc-coverage-auditor` |
| Legacy `sites.json` (this dir) | Read **always** by `agent.py` for the sitemap checks and `canonical_urls_endpoint`, and by `queue-publish.py` to turn a slug into a host. `submit.ts` reads it only as the last-resort fallback |
| DB | The env var named by `databaseUrlEnv` (`AISLEPROMPT_DATABASE_URL` / `SPECPICKS_DATABASE_URL`) is **not set on whitebeast**; `secrets.env` uses `DATABASE_URL_<SITE>`. So the `databaseUrlFallback` DSN in the config connects. That is a credential in a tracked file; never copy it. Tables: aisleprompt `recipe_catalog`, `kitchen_products`, `kitchen_categories`, `editorial_articles`. specpicks `products`, `articles`, `categories`, `brands`, `editorial_articles`, `hardware_specs`, `trending_comparisons` |
| IndexNow key | 32-hex `key` per site, hosted as `https://<host>/<key>.txt`. The files live in each site repo's `frontend/public/`. IndexNow needs no API key; the hosted file proves ownership |
| GSC OAuth | `INDEXNOW_GSC_OAUTH_FILE` → `GSC_OAUTH_FILE` → `~/.reusable-agents/seo/.oauth.json`. Needs `client_id`, `client_secret`, and `refresh_token`, and the token needs webmasters **write** scope for the sitemap PUT |
| Canonical endpoints | `https://<host>/api/seo/canonical-urls` (legacy `sites.json`) |

## Outputs

| Output | Detail |
|---|---|
| IndexNow POSTs | Batched URL lists per site |
| Watermark | `~/.reusable-agents/indexnow-submitter/<site>-watermark.txt` |
| Force-submit file | `~/.reusable-agents/indexnow-submitter/<site>.force-submit.txt`. Written by step 7 above and by `queue-publish.py`. **No code reads it** (see Known issues) |
| GSC | Sitemap resubmission on every run |
| Temp files | `/tmp/tmp*.log`, one per run, holding the `submit.ts` output. This is the only place the per-query-set candidate counts are kept |
| Recs / emails | No recs. The only email is the AgentBase run summary (framework default), addressed to the manifest `owner`. Under the wrapper's `DIGEST_ONLY=1` both successes and failures end up in the digest queue (failures through the digest gate in `send_via_msmtp`) |

## Goals & metrics

`RunResult.metrics`: `urls_submitted_this_run`, `urls_failed_this_run`,
`success_rate_pct`, `rc`, `urls_submitted_30d_cumulative`,
`canonical_urls_total`, `canonical_urls_in_sitemap`, `canonical_urls_missing`,
`sitemap_coverage_pct`, `sitemap_pings_attempted`, `sitemap_pings_ok`.

Goals (incremental instances, from `GET /api/agents/<id>/goals` on 2026-09-23):

| Goal id | target_metric | aisleprompt | specpicks | Target |
|---|---|---|---|---|
| `goal-urls-submitted-30d` | `urls_submitted_30d_cumulative` | 274 | 3,277 | 50,000 |
| `goal-runs-success-rate-7d` | `success_rate_pct` | 100 | 100 | ≥ 95 % |

`urls_submitted_30d_cumulative` is **not cumulative.** `agent.py` reads
`cache["metric_values"]`, but `metric_helper`'s timeseries cache stores
`goals.<id>.latest_value`, so the prior total is always 0 and the goal
equals one run's count. The legacy `submit.sh` reads the correct cache path.
The aisleprompt manifest `target_metric` (`goal-indexed-pages-pct`) matches
no goal id. The specpicks manifest sets none.

## Configuration

| Name | Default | Meaning |
|---|---|---|
| `INDEXNOW_SITE` / `INDEXNOW_TARGET_SITE` | unset (all sites) | Site filter. Set by the instance `run.sh` |
| `INDEXNOW_BULK` | `0` | `1` passes `--bulk` (the `*-indexnow-bulk` instances) |
| `AGENT_ID` | engine id | Per-site id |
| `INDEXNOW_TS_APP_DIR` | `/home/voidsstr/development/specpicks` | Supplies `node_modules` (ts-node + pg) |
| `NODE_PATH` | `npm root -g` | Fallback modules when the app dir has no `node_modules` |
| `INDEXNOW_TIMEOUT_S` | `900` | Timeout for the `submit.ts` subprocess |
| `SITE_CONFIG_PATHS` | unset | Comma-separated config files; overrides discovery in `submit.ts` (and in `gsc-coverage-auditor`) |
| `INDEXNOW_GSC_OAUTH_FILE` / `GSC_OAUTH_FILE` | `~/.reusable-agents/seo/.oauth.json` | Token for the GSC sitemap PUT |
| `INDEXNOW_QUEUE_ROOT` | derived | `queue-publish.py` only. Otherwise it walks up from a claude-pool `HOME` to the real `.reusable-agents` root |

`submit.ts` CLI flags: `--site=<name>`, `--bulk`, `--dry-run` (logs, no
POST), `--no-sitemap`. `agent.py` only ever passes `--site` and `--bulk`.

**URL templates** (`submit.ts` `buildUrl`):

- `slug` → `row.slug`.
- `slugify:title|-|id` → `slugify(title)` + `-` + `slugify(id)`.
- `slugify:slug` → a slugified free-text column.
- `compose:a|<sep>|b` → `a` + `<sep>` + `b`, verbatim.

A separator must match `^[-_.\\/]+$` (only `-`, `_`, `.`, `\`, `/`).
Anything else is read as a column name.

## Short-circuit & idempotency

- `signals()` hashes `~/.reusable-agents/indexnow-submitter/<site>.watermark`,
  but the real file is `<site>-watermark.txt`. `signals()` therefore returns
  `None`, and the agent **never short-circuits**. Every tick runs `submit.ts`.
  The watermark is what keeps incremental runs small.
- The watermark only advances when every batch succeeded, so a failed tick
  is retried next tick.
- Handoffs: `pre_run` drains `agents/<AGENT_ID>/handoff-queue/`, but `run()`
  never reads `self.inbound_handoffs`.

## Running & inspecting

```bash
systemctl --user start agent-aisleprompt-indexnow-submitter.service
systemctl --user list-timers | grep indexnow
tail -5 /tmp/reusable-agents-logs/agent-aisleprompt-indexnow-submitter.log   # status lines only
# Latest per-query-set candidate breakdown:
grep -l "indexnow:aisleprompt" $(ls -t /tmp/tmp*.log | head -40) | head -1 | xargs grep "indexnow:"
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/aisleprompt-indexnow-submitter
curl -s -X POST -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/aisleprompt-indexnow-submitter/trigger
```

**Before a manual dry run:**

- `--dry-run` still **advances the watermark**, because `failed` stays 0.
  Copy `~/.reusable-agents/indexnow-submitter/<site>-watermark.txt` first
  and restore it afterwards.
- `submit.sh --site=<x>` also records goal points (treating dry-run counts
  as submitted) and logs to `/tmp/reusable-agents-indexnow.log`.

Prefer calling the worker directly:

```bash
cd /home/voidsstr/development/specpicks && NODE_PATH=$PWD/node_modules \
  npx ts-node --transpile-only \
  --compiler-options '{"module":"node16","moduleResolution":"node16","esModuleInterop":true,"skipLibCheck":true,"resolveJsonModule":true}' \
  /home/voidsstr/development/reusable-agents/agents/indexnow-submitter/submit.ts --site=aisleprompt --dry-run
```

To force a full re-send, trigger the site's `*-indexnow-bulk` instance, or
delete `<site>-watermark.txt` so the next incremental run starts from epoch.

### Queueing a just-published URL

```bash
python3 /home/voidsstr/development/reusable-agents/agents/indexnow-submitter/queue-publish.py \
  --site specpicks --url https://specpicks.com/reviews/<slug>
```

This appends to `<site>.force-submit.txt` (see Known issue 1: nothing
currently submits that file).

### Adding a new site

1. Host a 32-hex key at `https://<newhost>/<key>.txt`, for example in the
   site's `frontend/public/`.
2. Add `{"sites": [...]}` to `<site repo>/agents/seo-config/site-indexnow.json`
   with `name`, `host`, `key`, `databaseUrlEnv` (+ fallback), `watermarkFile`,
   `staticPaths`, `querySets`, `sitemapUrls`, and `gsc_property`. Then add
   the repo to the discovery list in `submit.ts`, or pass `SITE_CONFIG_PATHS`.
   The discovery list is hardcoded to aisleprompt and specpicks (so is
   `load_site()` in `gsc-coverage-auditor/inspect.py`).
3. Add the same entry to this dir's `sites.json` if you want sitemap checks,
   GSC sitemap submit, and canonical coverage. `agent.py` reads only that file.
4. Create per-site instance dirs (`manifest.json` + `run.sh` exporting
   `INDEXNOW_SITE` / `AGENT_ID`), symlink `AGENT.md` / `SKILL.md` to this
   dir, and register them.
5. Seed once with the bulk instance.

## Failure modes & troubleshooting

| Symptom | Cause / evidence | Action |
|---|---|---|
| `submit.ts timed out after 900s` | Very large candidate set, or a slow DB | Raise `INDEXNOW_TIMEOUT_S` for bulk, or check the DB |
| `npx/ts-node not found`, or `TypeError: Cannot read properties of undefined (reading 'fileExists')` | Fresh host: `npx` fetched ts-node without its typescript peer | `npm install` in the specpicks repo, or install `ts-node typescript@5 tsx pg` globally, as the `setup-fleet-host` skill's `deps` step lists. `install/standup-fleet-host.sh deps` does not install these node globals |
| `[indexnow:<set>] query failed: …` in the temp log | One query set's SQL failed. It contributes 0 URLs silently, and the run still succeeds | Fix the SQL in `site-indexnow.json` |
| `batch N: FAIL HTTP …` / `failed=N` | IndexNow rejected the batch (bad key file, foreign-host URL) | The watermark holds, so the next tick retries. Check the key file URL |
| `sitemap pings ok=8/15` (aisleprompt) with `GSC ERR 401/403` | A token was minted but all 7 GSC PUTs were rejected (usually read-only scope). The reachability checks and robots.txt still pass | Follow `.claude/skills/refresh-gsc-token/SKILL.md` (write scope) |
| `sitemap pings ok=8/8` (aisleprompt) with `=NO_OAUTH` | No access token: the OAuth file is missing, or the refresh failed (for example `invalid_grant`), so the GSC PUTs are not attempted | Same skill (re-mint the token) |
| `sitemap pings ok=1/2` (specpicks, 03:11 UTC on 2026-09-23) | The sitemap HEAD failed (`sitemap.xml=HTTP 0` in that run's `decisions.jsonl`), so the GSC PUT was skipped; robots.txt passed | Check `curl -sI https://specpicks.com/sitemap.xml`. It recovered on the next tick |
| Queued URLs never submitted, via a claude-pool `HOME` | Historical, fixed 2026-09-11 (`19a8e27`): `queue-publish.py` wrote under the profile's shadow `~/.reusable-agents` | none |

### Known issues (verified 2026-09-23)

1. **The force-submit file is write-only.** `submit.ts` never reads
   `<site>.force-submit.txt`. URLs from `queue-publish.py` (called per
   `agents/implementer/ARTICLE_AUTHOR.md`) and from the canonical-coverage
   gap are never submitted by this path. `agent.py` also overwrites the
   file whenever it finds a gap, which drops earlier `queue-publish.py`
   entries. On 2026-09-23 the aisleprompt file held the 362 canonical URLs
   missing from the sitemaps (`sitemap coverage 100503/100865`, 18:00 UTC
   run). The specpicks file held 144 `/reviews/` URLs in `queue-publish.py`'s
   format, last written at 13:04 UTC; the 14:18 UTC run logged no coverage
   result and did not rewrite it.
2. **`gsc-coverage-unknown` / `indexnow-submit` handoffs are stranded.**
   These rec types route to the generic id `indexnow-submitter`
   (`work_types.DEFAULT_REC_ROUTING`), which no registered agent drains.
   `agents/indexnow-submitter/handoff-queue/` held 93 items. The 25
   readable ones (2026-09-11 to 2026-09-23) are from `implementer` with rec
   type `gsc-coverage-unknown`. The other 68 (2026-05-05 to 2026-05-13) are
   in the Azure Archive tier and cannot be read.
3. **The hardware comparison URLs are likely malformed (from code reading).**
   specpicks `trending-comparisons-hardware` uses
   `compose:left_ref|-vs-|right_ref`. `-vs-` fails the separator test, so it
   is read as a column and becomes empty, and URLs are built as
   `/compare/<a><b>` instead of `/compare/<a>-vs-<b>` (200 per run on
   2026-09-23). This is unverified against the live POST body.
   `gsc-coverage-auditor`'s own URL renderer builds these correctly.
4. The 30-day goal is not cumulative (see Goals & metrics). `signals()` never
   fires (see Short-circuit). `/tmp/tmp*.log` files accumulate until the
   next reboot.

## Files

| File | Role |
|---|---|
| `agent.py` | AgentBase wrapper. This is what the instances run |
| `submit.ts` | Worker: config, watermark, SQL, sitemaps, IndexNow POSTs |
| `submit.sh` | Legacy manual wrapper. Logs to `/tmp/reusable-agents-indexnow.log` and records Layer-A goal points when given `--site` |
| `sites.json` | Legacy multi-site config, still read by `agent.py` and `queue-publish.py`. `gsc-coverage-auditor/sites.json` symlinks here |
| `queue-publish.py` | Appends URLs to `<site>.force-submit.txt` |
| `SKILL.md` | Short skill description (symlinked into the instances) |

## Related agents

- **Siblings:** the `*-indexnow-bulk` instances (same engine, `--bulk`), and
  `*-gsc-coverage-auditor` (`reusable-agents: agents/gsc-coverage-auditor/AGENT.md`),
  which shares `site-indexnow.json` and the GSC token. Its analyzer turns
  "URL is unknown to Google" into `gsc-coverage-unknown` recs routed here.
- **Upstream:** the site DBs (recipes, products, and articles written by the
  content agents), and the implementer's article-publish path through
  `queue-publish.py`.
- **Token:** shared with `*-seo-opportunity-agent`.
