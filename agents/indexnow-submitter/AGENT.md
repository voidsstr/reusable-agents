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
3. **Run the worker.** Executes `npx ts-node --transpile-only … submit.ts [--site=<site>] [--bulk]` with timeout `INDEXNOW_TIMEOUT_S` (1500 s). The output is written to a `NamedTemporaryFile` `/tmp/tmp*.log`, which is never deleted and not echoed to the unit log.
4. **submit.ts, per site** (rewritten 2026-09-25 after the AI-visibility audit found the full catalog re-sent daily, the same ~277 URLs every 15 minutes, and 404/noindex URLs in the batches). Every candidate passes three gates — SOURCE → LEDGER → VERIFY:
   1. **Config.** `SITE_CONFIG_PATHS` if set, else every `<INDEXNOW_SITE_REPOS_ROOT or ~/development>/*/agents/seo-config/site-indexnow.json` (discovered, no site names in code), else the legacy `sites.json`.
   2. **Sources.**
      - *querySets* — incremental runs `incrementalSql` with `$1` = watermark; `--bulk` runs `bulkSql`. `bulkOnly` sets run in `--bulk` only; `incrementalOnly` sets never run in `--bulk`. An optional `lastmod` column (or `lastmodColumn`) is the change token. `excludeIfMatches: {column: regex}` drops rows in JS (e.g. a site's noindex title terms). `verify: true` forces a live check of every URL the set yields.
      - *force queue* — `<site>.force-submit.txt` (queue-publish.py + agent.py's coverage gap). Drained by incremental runs; URLs not reached this tick stay queued.
      - *sitemaps* — incremental: a snapshot DIFF (`<site>.sitemap-snapshot.json`) at most every `policy.sitemapIntervalHours`: URLs new to the site-wide snapshot or whose `<lastmod>` changed. A child sitemap whose lastmod is "today" on ≥90% of URLs is treated as synthetic (only new locs count). A child that fails, times out (90 s) or collapses (<10% of its previous URL count) keeps its previous snapshot, so an outage never reads as a mass deletion or, on recovery, a mass addition. The first run is a baseline (no diffs). `--bulk`: every `<loc>`.
      - *staticPaths* — `--bulk` only.
   3. **Ledger** (`<site>.indexnow-ledger.tsv`: url, status, time, token). New → submit. Sent inside `minResubmitHours` (24) → skip. Both tokens present: changed → submit, same → skip (incremental), or re-confirm after `bulkResubmitDays` (28) in `--bulk`. Sent without a token (force queue, static) and now seen with a change date later than the send day → changed. No comparable token → re-send after `resubmitDays` (7). Rejected → skip for `rejectRecheckDays` (3), then re-verify. A *transient* verify failure (`tmp:<reason>` — timeout, fetch error, 5xx, 429) is retried at the next chance, up to `transientMaxAttempts` (6), before it counts as a rejection. `--bulk` sends at most `bulkMaxPerRun`, new/changed first, then least-recently sent — a rotation, not a daily full re-send.
   4. **Verify.** Untrusted candidates (`verify.sources`: force, sitemap diff, static; plus `verify: true` sets and re-checks) are fetched (GET, no redirect follow, honest `IndexNowVerifier` UA) and dropped unless 200, no `noindex` in X-Robots-Tag or meta robots/bingbot, and a self-canonical. A random sample of trusted DB candidates (`sampleTrusted` / `sampleTrustedBulk`) is checked as a drift canary (transient failures do not count as drift). Rejections go to the ledger; transient failures are re-queued (incremental) for the next tick. Bounded by `maxPerRun`, `concurrency`, `budgetSeconds`; overflow is deferred to the force queue (incremental) or the next bulk.
   5. **Submit** in ≤10,000-URL batches; ledger entries are written only for batches that returned 2xx. A failed batch puts its queue and sitemap-diff URLs back on the force queue (DB rows come back via the un-advanced watermark).
   6. **Persist** (never on `--dry-run`): ledger (merged with any concurrent writer), snapshot, force queue (keeps lines other writers appended meanwhile), watermark — incremental runs only (a `--bulk` run never moves it), and only when every batch succeeded AND every query ran. Queue lines spliced together by a writer that appended to a file without a trailing newline are split back apart on read.
   7. **Report.** `[indexnow:<site>] stats {json}` then `done submitted=N failed=M`.
5. **Parse counts.** Parses the `done` lines. Only the site's own lines count when a site is set.
6. **Layer-A metrics.** Records `goal-urls-submitted-30d` and `goal-runs-success-rate-7d` through `metric_helper.record_many`.
7. **Canonical coverage** (at most every `INDEXNOW_COVERAGE_INTERVAL_H`, default 6 h — it crawls every sitemap child). Runs only when a site is set and the legacy `sites.json` entry has `canonical_urls_endpoint`. Emits `canonical_urls_*` and `sitemap_coverage_pct`. Missing URLs are **appended** to `<site>.force-submit.txt`, de-duplicated against the queue and against URLs the ledger sent (7 d) or rejected (3 d) recently; submit.ts live-verifies them.
8. **Sitemap checks and GSC submit.** Uses the legacy `sites.json` `sitemapUrls`, and runs for all sites when no site is set. For each sitemap it sends a HEAD request; if that returns 2xx, it PUTs `webmasters/v3/sites/<gsc_property>/sitemaps/<sitemap>`. Finally, one robots.txt check looks for a `Sitemap:` line. With 7 sitemaps, aisleprompt makes 7 + 7 + 1 = 15 checks; specpicks makes 1 + 1 + 1 = 3.
9. **Result.** A non-zero `submit.ts` rc gives `RunResult(failure)`, with the metrics kept. Otherwise `success`, with the summary `submitted N URLs (failed=M) to IndexNow across K site(s); sitemap pings ok=X/Y`.

### URL families (per-site `site-indexnow.json`, 2026-09-25)

| Site | Query sets (`b` = bulkOnly, `i` = incrementalOnly, `v` = verify every URL) | Static paths | Sitemaps |
|---|---|---|---|
| aisleprompt | `recipes`(i,v — bulk takes recipe URLs from the sitemap, which has the canonical slugs), `kitchen-products` (noindex categories excluded in SQL, noindex title terms via `excludeIfMatches`), `kitchen-categories`(b,v), `blog-articles`(v — the YMYL guard noindexes some) | 36 | `/sitemap.xml` index (9 children) |
| specpicks | `products` (the product sitemap's (A) price / (B) eBay-routed predicate, every site_id), `ai-used-products` (PDPs AI assistants used — `ai_traffic_log`), `articles`, `categories`(b,v), `buying-guides`(b,v — some slugs 301/410), `brands`, `reviews`, `benchmarks` | 21 | `/sitemap.xml` index (16 children) |

Removed 2026-09-25: aisleprompt `recipe-categories`/`recipe-cuisines` (slugify of the raw column built 404s like `/recipes/category/bbq-smoked`; the site's slugs come via `sitemap-core.xml`/`sitemap-cuisines.xml`); specpicks `trending-comparisons-*` (2,928 pairs outside the filtered compare sitemap, mostly 404; the hardware template also dropped `-vs-`) and `retro-marketplace-categories` (all 404); `/products` and `/search` static paths (404 / noindex). Both configs dropped the plaintext `databaseUrlFallback` DSN; `databaseUrlEnv` is now `DATABASE_URL_<SITE>` from secrets.env.

Dry runs on 2026-09-25 (production data): aisleprompt incremental 370 candidates → 1 submittable (24 of the first 25 force-queue URLs were 404/noindex/canonical-elsewhere); `--bulk` 157k candidates → 30k rotation, drift canary 0/40. specpicks incremental 9,004 candidates (5,829 changed products, 389 AI-used PDPs, 2,947 queued) → ~6.4k + verified queue; `--bulk` 94.9k → 25k rotation.

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
| Force-submit file | `~/.reusable-agents/indexnow-submitter/<site>.force-submit.txt`. Appended by `queue-publish.py` and the coverage gap; **drained by submit.ts** (incremental runs) |
| Ledger | `<site>.indexnow-ledger.tsv` next to the watermark (url, `sent`/`rej:<reason>`, epoch ms, token). Pruned after 120 days |
| Sitemap snapshot | `<site>.sitemap-snapshot.json` next to the watermark (per child sitemap: loc → lastmod) |
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
| `INDEXNOW_TIMEOUT_S` | `1500` | Timeout for the `submit.ts` subprocess (sitemap intake + up to `verify.budgetSeconds` of page checks fit inside it) |
| `SITE_CONFIG_PATHS` | unset | Comma-separated config files; overrides discovery in `submit.ts` (and in `gsc-coverage-auditor`) |
| `INDEXNOW_GSC_OAUTH_FILE` / `GSC_OAUTH_FILE` | `~/.reusable-agents/seo/.oauth.json` | Token for the GSC sitemap PUT |
| `INDEXNOW_QUEUE_ROOT` | derived | Force-queue dir. `queue-publish.py` walks up from a claude-pool `HOME` to the real `.reusable-agents` root; `submit.ts` defaults to the watermark file's dir (the same place) |
| `INDEXNOW_SITE_REPOS_ROOT` | `~/development` | Parent dir scanned for `*/agents/seo-config/site-indexnow.json` (submit.ts + gsc-coverage-auditor) |
| `INDEXNOW_ENDPOINT` | `https://api.indexnow.org/indexnow` | Tests point it at a mock |
| `INDEXNOW_COVERAGE_INTERVAL_H` | `6` | Minimum hours between canonical-coverage crawls |

Per-site knobs in `site-indexnow.json` (all optional; defaults in `submit.ts` `DEFAULT_POLICY` / `DEFAULT_VERIFY`):

| Key | Default | Meaning |
|---|---|---|
| `policy.resubmitDays` | 7 | Token-less candidates re-send after this |
| `policy.bulkResubmitDays` | 28 | `--bulk` re-confirms unchanged URLs after this |
| `policy.minResubmitHours` | 24 | Hard floor between two sends of one URL |
| `policy.bulkMaxPerRun` | 0 (∞) | `--bulk` rotation size (aisleprompt 30,000; specpicks 25,000) |
| `policy.sitemapIntervalHours` | 6 | Incremental sitemap-snapshot refresh interval (specpicks 4) |
| `policy.rejectRecheckDays` | 3 | Re-verify a rejected URL after this |
| `policy.queryTimeoutMs` | 120,000 | Per-query `statement_timeout` |
| `policy.ignoreLastmodPrefixes` | `[]` | Path prefixes whose sitemap `<lastmod>` is not a content change (specpicks `/vs/`, `/compare/`: lastmod = last time the pair trended) — only new locs count; `--bulk` re-confirms them on the slow rotation |
| `verify.sources` | `["force","sitemap","static"]` | Sources that must pass a live fetch |
| `verify.maxPerRun` / `concurrency` / `budgetSeconds` / `timeoutMs` | 300 / 4 / 300 / 20,000 | Fetch bounds |
| `verify.sampleTrusted` / `sampleTrustedBulk` | 5 / 50 | Drift-canary sample of trusted DB URLs |
| `verify.minTextChars` | 0 (off) | Reject 200 pages with less visible text |
| `origin`, `ledgerFile`, `snapshotFile`, `forceSubmitFile` | derived | Paths / scheme overrides (tests) |

`submit.ts` CLI flags: `--site=<name>`, `--bulk`, `--dry-run` (logs, no
POST, **writes no state** — ledger, snapshot, queue and watermark are
untouched), `--no-sitemap`. `agent.py` only ever passes `--site` and `--bulk`.

**URL templates** (`submit.ts` `buildUrl`):

- `slug` → `row.slug`.
- `slugify:title|-|id` → `slugify(title)` + `-` + `slugify(id)`.
- `slugify:slug` → a slugified free-text column.
- `compose:a|<sep>|b` → `a` + `<sep>` + `b`, verbatim.

A part that is a column of the row is substituted; an identifier-shaped part
the row lacks means the SQL forgot a column and the URL is skipped; anything
else (`-`, `/`, `-vs-`) is a literal. `lit:<text>` forces a literal.
`slugify` strips diacritics first.

Tests: `framework/tests/test_indexnow_submitter.py` runs `submit.test.ts`
(unit + an end-to-end run against a mock site and mock IndexNow endpoint).

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

- `--dry-run` writes no state (since 2026-09-25), but it still live-fetches
  up to `verify.maxPerRun` pages. Point `SITE_CONFIG_PATHS` at a copy of the
  site config with a smaller `verify.maxPerRun` to keep it light.
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
| `submit.ts timed out after 1500s` | Very large candidate set, or a slow DB | Raise `INDEXNOW_TIMEOUT_S` for bulk, or check the DB |
| `npx/ts-node not found`, or `TypeError: Cannot read properties of undefined (reading 'fileExists')` | Fresh host: `npx` fetched ts-node without its typescript peer | `npm install` in the specpicks repo, or install `ts-node typescript@5 tsx pg` globally, as the `setup-fleet-host` skill's `deps` step lists. `install/standup-fleet-host.sh deps` does not install these node globals |
| `[indexnow:<set>] query failed: …` in the temp log | One query set's SQL failed. It contributes 0 URLs silently, and the run still succeeds | Fix the SQL in `site-indexnow.json` |
| `batch N: FAIL HTTP …` / `failed=N` | IndexNow rejected the batch (bad key file, foreign-host URL) | The watermark holds, so the next tick retries. Check the key file URL |
| `sitemap pings ok=8/15` (aisleprompt) with `GSC ERR 401/403` | A token was minted but all 7 GSC PUTs were rejected (usually read-only scope). The reachability checks and robots.txt still pass | Follow `.claude/skills/refresh-gsc-token/SKILL.md` (write scope) |
| `sitemap pings ok=8/8` (aisleprompt) with `=NO_OAUTH` | No access token: the OAuth file is missing, or the refresh failed (for example `invalid_grant`), so the GSC PUTs are not attempted | Same skill (re-mint the token) |
| `sitemap pings ok=1/2` (specpicks, 03:11 UTC on 2026-09-23) | The sitemap HEAD failed (`sitemap.xml=HTTP 0` in that run's `decisions.jsonl`), so the GSC PUT was skipped; robots.txt passed | Check `curl -sI https://specpicks.com/sitemap.xml`. It recovered on the next tick |
| Queued URLs never submitted, via a claude-pool `HOME` | Historical, fixed 2026-09-11 (`19a8e27`): `queue-publish.py` wrote under the profile's shadow `~/.reusable-agents` | none |

### Known issues

Fixed 2026-09-25 (were issues 1, 3 and 4a below the 09-23 audit): the
force-submit file is now drained and the coverage writer appends instead of
overwriting; `compose:` keeps non-column parts as literals (`-vs-`); dry runs
no longer advance the watermark.

Still open:
1. **`gsc-coverage-unknown` / `indexnow-submit` handoffs are stranded.** These
   rec types route to the generic id `indexnow-submitter`
   (`work_types.DEFAULT_REC_ROUTING`), which no registered agent drains.
2. **The 30-day goal is not cumulative** (see Goals & metrics). `signals()`
   never fires (see Short-circuit). `/tmp/tmp*.log` files accumulate until
   the next reboot.
3. **The ledger, snapshot and queue are host-local** (like the watermark).
   A fleet-host move without `~/.reusable-agents/indexnow-submitter/`
   costs one baseline: the first incremental run re-sends changed rows and
   the first bulks re-send the catalog on the `bulkMaxPerRun` rotation.

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
