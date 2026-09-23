# GSC Coverage Auditor (`gsc-coverage-auditor`)

> Runs a daily Google Search Console **URL Inspection** sweep per site. Each
> run inspects the least-recently-inspected URLs and records Google's
> `coverageState`, verdict, and last crawl time for each one. North Star
> link: **indexed pages → organic clicks**. The data explains *why* pages
> are not indexed, and `seo-opportunity-agent` turns it into fix recs.

**One runbook for the whole family.** Both per-site `AGENT.md` files are
symlinks to this file:
`aisleprompt: agents/gsc-coverage-auditor/AGENT.md` and
`nsc-assistant: agents/specpicks-gsc-coverage-auditor/AGENT.md`. The
`SKILL.md` files are linked the same way. Edit this file only.

## At a glance

| | |
|---|---|
| Engine id | `gsc-coverage-auditor` (class default). Runs use the per-site `AGENT_ID` |
| Home | `reusable-agents: agents/gsc-coverage-auditor/` |
| Kind | AgentBase python engine (`agent.py` → `GscCoverageAuditor`). It subprocess-runs the worker `inspect.py`. There is no engine `manifest.json`; the dir is **not registered** itself |
| Entry (instances) | `agent_run_wrapper.sh <site>-gsc-coverage-auditor bash <instance>/run.sh`. `run.sh` exports `GSC_INSPECT_SITE=<site>` and `AGENT_ID=<site>-gsc-coverage-auditor`, then `exec python3 …/agents/gsc-coverage-auditor/agent.py` |
| Category | `seo` |
| Deep references | `reusable-agents: agents/seo-opportunity-agent/lib/analyzer/analyzer.py` (`_add_index_coverage_recs`); token runbook `.claude/skills/refresh-gsc-token/SKILL.md` |

### Instances

| Instance id | Home | Schedule | Timer | Latest run (2026-09-23) |
|---|---|---|---|---|
| `aisleprompt-gsc-coverage-auditor` | `aisleprompt: agents/gsc-coverage-auditor/` (`manifest.json`, `run.sh`) | Manifest `30 11 * * *` **UTC**. systemd `OnCalendar=*-*-* 11:30:00` has no TZ, so it fires 11:30 **America/Detroit** (15:30 UTC in EDT) | enabled | success 15:48 UTC: `inspected 1049 URLs in last 7d, indexed=0.04%, unknown=70.36% (universe=5003)` |
| `specpicks-gsc-coverage-auditor` | `nsc-assistant: agents/specpicks-gsc-coverage-auditor/` | Manifest `0 12 * * *` UTC. systemd `*-*-* 12:00:00` host-local (16:00 UTC in EDT) | enabled | success 16:18 UTC: `inspected 1039 URLs in last 7d, indexed=29.79%, unknown=30.78% (universe=5428)` |

Both instance manifests still describe "500 oldest-inspected URLs" and a
"~100K" (aisleprompt) or "~50K" (specpicks) URL universe. The code default
is now 150 per run (see History). `inspect.py` prints the real universe
size to stderr, but `agent.py` captures and discards that output, so it is
not recorded anywhere.

## What it does

`agent.py` → `run()`:

1. **Check the site.** Fails immediately if `GSC_INSPECT_SITE` is unset.
2. **Run the worker.** Executes `python3 inspect.py` as a subprocess with timeout `GSC_INSPECT_TIMEOUT_S` (1800 s). stdout and stderr are **captured, not re-logged**; only a decision entry with their byte counts is kept.
3. **inspect.py:**
   1. Locates `refresh-token.py`, checking in order: `GSC_REFRESH_SCRIPT`, then `agents/seo-opportunity-agent/lib/collector/refresh-token.py`, then the legacy `agents/seo-data-collector/`, then the nsc-assistant `_legacy-seo-opportunity-agent/`. It exits if none exists.
   2. Loads the site config, checking in order: `SITE_CONFIG_PATHS`, then `~/development/{aisleprompt,specpicks}/agents/seo-config/site-indexnow.json`, then this dir's `sites.json`. `sites.json` is a symlink to `indexnow-submitter/sites.json` and is only the legacy fallback. On whitebeast, the per-site `site-indexnow.json` file wins.
   3. **Step 0, sitemap submit.** Skipped on dry run or when `GSC_INSPECT_SKIP_SITEMAP_SUBMIT=1`. Otherwise it PUTs each `sitemapUrls[]` entry to the Search Console Sitemaps API for `gscSiteUrl` or `sc-domain:<host>`. A 401/403 means the token lacks write scope, and the step is skipped.
   4. **URL universe.** Runs every querySet's `bulkSql` (psycopg2) and renders the URLs. It also adds every `<loc>` in the sitemaps, following sitemap-index children up to 20 per index.
   5. **Pick targets.** Sorts URLs by last-inspected time (never inspected sorts first) and takes the oldest `GSC_INSPECT_LIMIT` (150).
   6. **Inspect.** POSTs `searchconsole.googleapis.com/v1/urlInspection/index:inspect` for each URL at `GSC_INSPECT_QPS` (2.0). A 429 gets one retry after a 60 s sleep. Each success appends a flattened row to `<state>/<site>-coverage.jsonl` and updates `<state>/<site>-last-inspected.json` (saved every 50 URLs and at the end). A failed URL is not marked, so it is retried next run.
   7. **Layer-A metrics.** Records `goal-urls-inspected-7d`, `goal-indexed-pct`, and `goal-unknown-pct` for `<site>-gsc-coverage-auditor` via `metric_helper.record_many`.
4. **Check the worker result.** A non-zero rc → `RunResult(failure)` with the stderr tail. A missing coverage file → failure.
5. **Compute metrics.** Reads the coverage JSONL and keeps the latest row per URL (see Goals & metrics).
6. **Record Layer-A again.** Writes the same three goal ids a second time, so each run adds two points per goal.
7. **Return the result.** `RunResult(success)` with summary `"<site>: inspected N URLs in last 7d, indexed=X%, unknown=Y% (universe=U)"`.

## Inputs

| Input | Detail |
|---|---|
| Site config | `aisleprompt: agents/seo-config/site-indexnow.json` and `specpicks: agents/seo-config/site-indexnow.json`, shared with the IndexNow submitter. Fields used: `host`, `databaseUrlEnv` / `databaseUrlFallback`, `siteIds`, `querySets[].bulkSql` / `urlTemplate` / `urlPrefix`, `sitemapUrls`, `gscSiteUrl` (optional). Neither config sets `gscSiteUrl` (they carry `gsc_property`, which `inspect.py` does not read), so the GSC property is `sc-domain:<host>` |
| DB tables (bulk SELECTs) | aisleprompt: `recipe_catalog`, `kitchen_products`, `kitchen_categories`, `editorial_articles`. specpicks: `products`, `articles`, `categories`, `brands`, `editorial_articles`, `hardware_specs`, `trending_comparisons`, filtered by `site_id IN (retro-gaming, pc-hardware, retro-pc-building)` where the SQL uses `$SITE_IDS` |
| DB connection | The env var named by `databaseUrlEnv` (`AISLEPROMPT_DATABASE_URL` / `SPECPICKS_DATABASE_URL`). **Neither is set on whitebeast**: `secrets.env` defines `DATABASE_URL_<SITE>` instead. So the `databaseUrlFallback` DSN in the config file is what connects. That is a credential in a tracked file; never copy it |
| OAuth | `~/.reusable-agents/seo/.oauth.json` (`GSC_OAUTH_FILE`), the same refresh token used by `*-seo-opportunity-agent` and the IndexNow sitemap submit |
| Google APIs | URL Inspection (quota 2,000/day/property; the code comment observes about 8 inspections/min) and the Sitemaps API (needs write scope) |

## Outputs

| Output | Detail |
|---|---|
| `~/.reusable-agents/gsc-coverage-auditor/<site>-coverage.jsonl` | Append-only, one row per inspection: `url`, `inspected_at`, `verdict`, `coverageState`, `lastCrawlTime`, `indexingState`, `robotsTxtState`, `pageFetchState`, `googleCanonical`, `userCanonical`, and `raw` (the full response). About 6 MB per site on 2026-09-23. This is **local disk on the fleet host**, not blob storage |
| `~/.reusable-agents/gsc-coverage-auditor/<site>-last-inspected.json` | `{url: iso_ts}`. Drives the round-robin |
| Goal points | Layer-A (`metric_helper`), twice per run, plus Layer-B through `RunResult.metrics` |
| GSC side effect | Sitemap (re)submission on every run |
| Recs / handoffs | None directly. The analyzer turns the JSONL into recs (below) |
| Email | Only the AgentBase run summary (framework default), addressed to the manifest `owner`. Under the wrapper's `DIGEST_ONLY=1` both kinds end up in the digest queue: successful runs through `queue_for_digest`, and failed runs through the digest gate inside `send_via_msmtp` |

### How the JSONL becomes recs

`seo-opportunity-agent` (`lib/analyzer/analyzer.py::_add_index_coverage_recs`)
reads the latest row per URL and emits one rec per non-indexed state bucket
with sample URLs. Routing comes from `framework/core/work_types.py`
(`DEFAULT_REC_ROUTING`):

| coverageState (prefix match) | rec_type | priority | work_type → handler |
|---|---|---|---|
| Crawled - currently not indexed (also the em-dash form) | `gsc-coverage-not-indexed` | high | `body_md_edit` → `article-proposal-agent` |
| Discovered - currently not indexed | `gsc-coverage-discovered` | medium | `internal_link_addition` → implementer |
| Page with redirect | `gsc-coverage-redirect` | medium | `code_edit` → implementer |
| URL is unknown to Google | `gsc-coverage-unknown` | low | `index_submission` → `indexnow-submitter` |
| Submitted and indexed, but issues found | `gsc-coverage-issues` | medium | `schema_markup_fix` → implementer |
| Excluded by 'noindex' tag | `gsc-coverage-noindex` | low | `code_edit` → implementer |
| Duplicate, Google chose different canonical than user | `gsc-coverage-canonical-mismatch` | medium | `code_edit` → implementer |
| Soft 404 | `gsc-coverage-soft-404` | high | `code_edit` → implementer |

Two caveats, both seen in data on 2026-09-23:

- The JSONL records the noindex state with curly quotes (`Excluded by ‘noindex’ tag`). The ASCII-quoted rule prefix does not match it, so that bucket emits no rec.
- `gsc-coverage-unknown` handoffs are addressed to the generic
  `indexnow-submitter` id, which no registered agent drains. Its
  `handoff-queue/` held 93 items from 2026-05-05 to 2026-09-23 (the 68
  older ones are in the Azure Archive tier and cannot be read).

`aisleprompt-article-proposal-agent` also reads the aisleprompt JSONL to
compute an articles-indexed %.

## Goals & metrics

`RunResult.metrics` keys:

| Key | Meaning |
|---|---|
| `urls_inspected_7d` | JSONL rows with `inspected_at` in the last 7 days |
| `indexed_pct` | % of distinct inspected URLs whose latest state is `Submitted and indexed` |
| `unknown_pct` | % whose latest state is `URL is unknown to Google` |
| `crawled_not_indexed_count` | Count whose latest state is `Crawled - currently not indexed` |
| `urls_in_universe` | Distinct URLs **ever inspected**. Not the site's full URL universe |

Goals (same ids on both instances, from `GET /api/agents/<id>/goals` on 2026-09-23):

| Goal id | target_metric | aisleprompt | specpicks | Target |
|---|---|---|---|---|
| `goal-urls-inspected-7d` | `urls_inspected_7d` | 1,049 | 1,039 | 3,500 |
| `goal-indexed-pct` | `indexed_pct` | 0.04 % | 29.79 % | 60 % |
| `goal-unknown-pct` | `unknown_pct` | 70.36 % | 30.78 % | ≤ 10 % |

The 3,500/7d target cannot be reached at the current 150 URLs/run × 1
run/day (≈ 1,050 per 7 days). The aisleprompt manifest's `target_metric`
(`goal-indexed-pages-pct`) matches no goal id.

Latest state per URL (coverage JSONL, 2026-09-23):

| State | aisleprompt (5,003 URLs) | specpicks (5,428 URLs) |
|---|---|---|
| URL is unknown to Google | 3,520 | 1,671 |
| Crawled - currently not indexed | 1,456 | 404 |
| Discovered - currently not indexed | 10 | 1,116 |
| Submitted and indexed | 2 | 1,617 |
| Duplicate, Google chose different canonical | 4 | 359 |
| Soft 404 | 10 | 247 |
| Page with redirect / noindex / alternate | 0 / 1 / 0 | 7 / 6 / 1 |

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `GSC_INSPECT_SITE` | required | Site name. Set by the instance `run.sh` |
| `AGENT_ID` | engine id | Per-site id. Set by `run.sh` and the unit |
| `GSC_INSPECT_LIMIT` | `150` | URLs per run. Raise it only together with `GSC_INSPECT_TIMEOUT_S` |
| `GSC_INSPECT_QPS` | `2.0` | Request rate cap. Google's per-minute limit is the real ceiling |
| `GSC_INSPECT_TIMEOUT_S` | `1800` | `agent.py` timeout for the `inspect.py` subprocess |
| `GSC_INSPECT_STATE_DIR` | `~/.reusable-agents/gsc-coverage-auditor` | Honoured by `inspect.py` and the analyzer. **`agent.py` always reads the default dir** when computing metrics |
| `GSC_INSPECT_DRY_RUN` | `0` | `1` prints the first 10 target URLs; no API calls and no sitemap submit |
| `GSC_INSPECT_SKIP_SITEMAP_SUBMIT` | unset | `1` skips step 0 |
| `GSC_OAUTH_FILE` | `~/.reusable-agents/seo/.oauth.json` | OAuth refresh-token file |
| `GSC_REFRESH_SCRIPT` | search order above | Explicit path to `refresh-token.py` |
| `SITE_CONFIG_PATHS` | unset | Comma-separated config files that override discovery |
| `AGENT_FORCE_RUN` | unset | Framework escape hatch. `1` bypasses the auto short-circuit for one run (no effect today, because the short-circuit never fires; see below) |

## Short-circuit & idempotency

- `signals()` returns `{site, coverage_mtime, coverage_size}` for the
  coverage JSONL (or `None` if the file is missing), but **the short-circuit
  never fires**. `run()` returns no `next_state`, so the hash is never
  persisted: `AgentBase._check_short_circuit()` writes
  `_auto_signals_hash` into `self.state`, while `post_run()` saves
  `result.next_state`, which defaults to `{}`. `state/latest.json` holds
  `"state": {}` for both `aisleprompt-gsc-coverage-auditor` and
  `specpicks-gsc-coverage-auditor` (checked 2026-09-23). Every tick runs
  `inspect.py`.
- **Warning for whoever fixes persistence:** the hash is taken *before*
  `inspect.py` runs, and only `inspect.py` changes the coverage file. Once
  the hash is persisted, a successful run that appends nothing (for example,
  every inspection fails individually, or the universe is empty and
  `inspect.py` exits early) leaves the file unchanged, so every later run
  short-circuits indefinitely. Change `signals()` in the same change (for
  example, hash the URL universe or the last-inspected file).
  - If an instance does get stuck that way, run it by hand with
    `AGENT_FORCE_RUN=1` in the process environment (source
    `~/.reusable-agents/secrets.env`, then
    `AGENT_FORCE_RUN=1 bash <instance>/run.sh`). Prefixing
    `systemctl --user start` with the variable does not pass it to the unit.
- Re-running is safe. The round-robin picks different URLs, and the JSONL is
  append-only. Consumers take the latest row per URL.

## Running & inspecting

```bash
systemctl --user start agent-aisleprompt-gsc-coverage-auditor.service   # ~18 min
systemctl --user list-timers | grep gsc-coverage
tail -5 /tmp/reusable-agents-logs/agent-aisleprompt-gsc-coverage-auditor.log   # status lines only
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/aisleprompt-gsc-coverage-auditor

# Current state distribution (latest row per URL)
python3 - <<'PY'
import json, collections
latest = {}
for raw in open('/home/voidsstr/.reusable-agents/gsc-coverage-auditor/aisleprompt-coverage.jsonl'):
    r = json.loads(raw); u = r.get('url'); t = r.get('inspected_at', '')
    if u and t > latest.get(u, ('', ''))[1]: latest[u] = (r.get('coverageState'), t)
print(collections.Counter(s for s, _ in latest.values()).most_common())
PY
```

The unit log only carries AgentBase status lines, because `agent.py`
captures `inspect.py`'s own progress output and does not print it. To see
it, run the worker by hand with a dry run first:

```bash
cd /home/voidsstr/development/reusable-agents/agents/gsc-coverage-auditor
GSC_INSPECT_SITE=specpicks GSC_INSPECT_LIMIT=10 GSC_INSPECT_DRY_RUN=1 python3 inspect.py
```

## Failure modes & troubleshooting

| Symptom | Cause / evidence | Action |
|---|---|---|
| `inspect.py exited rc=1 …` with `invalid_grant` / token errors in `error_text` | GSC refresh token expired or revoked. The weekly revocation (OAuth app in *Testing*) stopped when the app was published on 2026-09-04, per the skill. If tokens start dying weekly again, check that first | Follow `.claude/skills/refresh-gsc-token/SKILL.md` (re-auth with `install/reauth-gsc.sh`) |
| `refresh-token.py not found. Looked in: …` | The token minter moved again | Set `GSC_REFRESH_SCRIPT` or fix the search list |
| `inspect.py timed out after 1800s` | Too many URLs per run. Historical: on 2026-08-14 the 500 default died at the timeout with 248/250 rows written, which led to the change to 150 (`6fbae7a`) | Keep `GSC_INSPECT_LIMIT × ~7.5 s` under the timeout |
| `inspect.py exited 0 but wrote no coverage file` | Empty universe (DB and sitemap both returned nothing) or wrong state dir | Check the DB fallback DSN and the sitemap URLs in `site-indexnow.json` |
| `[gsc-sitemap-submit] skipped: 401/403` (worker stderr) | Token has read-only scope | Harmless for inspection. Re-mint with write scope per the skill |
| Green runs with zero data | Historical: about 90 consecutive runs, fixed 2026-08-14 (`4cf975f`). The dead `seo-data-collector/refresh-token.py` path plus `inspect.py` shadowing the stdlib `inspect` module made every run a "successful first run". Both are fixed, and a non-zero rc now fails the run | If it recurs, check the sys.path guards at the top of `agent.py` / `inspect.py` |
| `module 'inspect' has no attribute …` | Stdlib shadowing by this dir's `inspect.py` | Never add this dir to `PYTHONPATH`. Engine `run.sh` computes metrics from `cd /tmp` for the same reason |

## Files

| File | Role |
|---|---|
| `agent.py` | AgentBase wrapper (converted 2026-05-11). This is what the instances run |
| `inspect.py` | Worker: universe build, round-robin pick, URL Inspection calls, JSONL writes |
| `run.sh` | **Legacy** bash driver, not used by the instances. It runs `inspect.py` and appends to `/tmp/reusable-agents-gsc-coverage.log` |
| `submit-sitemap.py` | One-shot manual sitemap submit. **Broken as of 2026-09-23**: it hardcodes the deleted `agents/seo-data-collector/refresh-token.py` and reads only `sites.json`. Step 0 of `inspect.py` and the IndexNow submitter already submit sitemaps |
| `sites.json` | Symlink to `../indexnow-submitter/sites.json`. Legacy fallback config |
| `SKILL.md` | Short skill description (symlinked into the instances) |

## History

- 2026-05-04: agent created, with seo-analyzer integration (`fd48907`). OAuth scope expanded to webmasters write + sitemap submit (`a224f02`).
- 2026-05-05: 2 RPS and 429 backoff (`f346d66`).
- 2026-05-11: converted to AgentBase (`agent.py`), per its docstring. The file was first committed on 2026-05-13 (`ab2fc5e`).
- 2026-08-14: revived after about 90 zero-output runs (`4cf975f`). Default limit 500 → 150 to fit the 1800 s timeout (`6fbae7a`).

## Related agents

- **Consumer:** `*-seo-opportunity-agent` (analyzer, rec table above) and `aisleprompt-article-proposal-agent` (articles-indexed %).
- **Sibling:** `*-indexnow-submitter` shares the per-site `site-indexnow.json` and the OAuth token (`reusable-agents: agents/indexnow-submitter/AGENT.md`).
- **Token:** shared with `*-seo-opportunity-agent` (see the `refresh-gsc-token` skill).
