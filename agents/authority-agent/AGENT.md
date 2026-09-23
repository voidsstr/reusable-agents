# Authority Agent (`authority-agent`)

> Works the off-page lever the content agents don't: each day it reads the
> site's indexation snapshot, ranks the most link-worthy published pages, and
> emails the operator a link-building worklist (off-site targets + on-site
> link-equity moves). North Star: **indexed pages** → organic clicks.

## At a glance

| | |
|---|---|
| Agent id | `authority-agent` |
| Home | `reusable-agents/agents/authority-agent/` (`agent.py`, `run.sh`, `manifest.json`, `SKILL.md`) |
| Kind | AgentBase python behind a thin bash env wrapper (`run.sh` sources `secrets.env`, sets defaults, `exec python3 agent.py`) |
| Schedule | manifest `0 13 * * *` (America/Detroit) → systemd `OnCalendar=*-*-* 13:0:00`, `Persistent=true`; timer **enabled** |
| Entry command | `bash …/agents/authority-agent/run.sh` |
| Category | seo |
| Status | live — one deployment, targeting **specpicks** via `run.sh` defaults (added 2026-07-24, commit `23a5ecd`) |
| Other docs | [`README.md`](README.md) (quick reference), [`SKILL.md`](SKILL.md) (task stub pointing here) |

## What it does

1. **Short-circuit check** — `signals()` (see below).
2. **Indexation audit** — latest `gsc_crawl_progress` row for
   `site = AUTHORITY_SITE_ID`: `indexed_pct = 100 × sample_indexed /
   sample_total`, plus `states`, `clicks_28d`, `impr_28d` logged as a
   decision.
3. **Citable assets** — top 40 `status='published'` rows with a `body_md`
   from `AUTHORITY_ASSET_TABLE`, ranked by
   `length(body_md) + 400 × #related_product_asins + 3000 if the slug matches
   vs|comparison|benchmark|best-`. URL is built as
   `https://<AUTHORITY_SITE_DOMAIN>/reviews/<slug>`.
4. **Worklist** — plain-text list of the top `AUTHORITY_WORKLIST_SIZE`
   assets with a suggested outreach angle each, suggested channels
   (subreddits, HARO/Qwoted, resource pages), and the on-site move
   (2–3 contextual inline links from already-indexed hubs).
5. **Email** — `send_via_msmtp(bypass_digest=True)`, subject
   `[AUTHORITY:<site_id>] link-building worklist`, to
   `mperry@northernsoftwareconsulting.com` from
   `automation@northernsoftwareconsulting.com` (msmtp account `automation`).
6. **Goal progress** — `record_goal_progress` for `surface-authority-targets`
   every run and for `lift-indexation-rate` only when a snapshot row exists,
   then `RunResult`.

What it does **not** do (despite older wording in the module docstring and
manifest description): it does not queue internal-link recs to the
implementer (the code comment calls that "a v2"), step 3's "near-miss"
list is just the top of the asset ranking (no per-query GSC data), and there
is no unlinked-mention search. `send_external_outreach()` exists only as a
`@requires_confirmation` stub and is never called.

## Inputs

| Source | Detail |
|---|---|
| `gsc_crawl_progress` (site DB) | written by the site's `scripts/gsc-crawl-tracker.js` (specpicks) — see *Failure modes* |
| `editorial_articles` (default `AUTHORITY_ASSET_TABLE`) | `slug`, `title`, `body_md`, `related_product_asins`, `category`, `status` |
| `~/.reusable-agents/secrets.env` | sourced by `run.sh` (also the unit's `EnvironmentFile`) |

## Outputs

- One operator email per non-short-circuited run. Today that means every
  run, because the short-circuit never fires (see *Short-circuit &
  idempotency*).
- `RunResult.metrics`: `indexed_pct` (0 when no snapshot),
  `citable_assets_found`, `authority_targets_surfaced`
  (`min(#assets, worklist_size)`), `worklist_emailed`.
- `next_state.last_indexed_pct`.
- No recs, no handoffs, no DB writes.
- Missing DSN → `RunResult(status="error", summary="DATABASE_URL not set")`.

## Goals & metrics

No goals file in the dir; goals live in framework storage. Live after the
2026-09-23 17:00 UTC run:

| Goal id | Metric | Current | Target |
|---|---|---|---|
| `lift-indexation-rate` | `indexed_pct` | 8.3 % | 60 % |
| `surface-authority-targets` | `authority_targets_surfaced` | 10 | 10 |

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `AUTHORITY_SITE_ID` | `specpicks` (set in `run.sh` and code) | `gsc_crawl_progress.site` filter; email subject |
| `AUTHORITY_SITE_DOMAIN` | `specpicks.com` | used to build asset URLs |
| `DATABASE_URL` | `run.sh` falls back to `DATABASE_URL_SPECPICKS` | site DSN |
| `AUTHORITY_ASSET_TABLE` | `editorial_articles` | asset source table |
| `AUTHORITY_WORKLIST_SIZE` | `10` | assets in the worklist |
| `STORAGE_BACKEND` | `azure` (set in `run.sh`) | so goals/status land where the dashboard reads them |

A second site = a second manifest/instance overriding the first three env
vars (per the `run.sh` comment); none exists today.

## Short-circuit & idempotency

`signals()` hashes `{gsc_snapshot_id: max(gsc_crawl_progress.id) for the
site, newest_asset_id: max(<asset_table>.id)}` (returns `None`, meaning no
short-circuit, when the DSN is missing or the query fails). **The
short-circuit never fires, though.**

- `AgentBase._check_short_circuit()` keeps the hash in
  `self.state["_auto_signals_hash"]`, but `run()` returns
  `next_state={"last_indexed_pct": ...}` without it.
- `post_run()` writes only `result.next_state` to
  `agents/authority-agent/state/latest.json`, so the hash is dropped after
  every run.
- Evidence: after the 2026-09-23 17:00 UTC run, `state/latest.json` held only
  `{"last_indexed_pct": 8.3}` (iteration 56). The 50 most recent runs in the
  API history are all full `success` runs, with no `short-circuited` summary.
- Result: every 13:00 (America/Detroit) run executes and sends the email,
  even when both inputs are unchanged.
- Fix (not yet applied): merge `self.state` into `next_state` in `run()`
  (for example `next_state={**self.state, "last_indexed_pct": indexed_pct}`).

The run itself is read-only against the DB.

## Running & inspecting

```bash
systemctl --user start agent-authority-agent.service
tail -f /tmp/reusable-agents-logs/agent-authority-agent.log
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" \
  http://localhost:8090/api/agents/authority-agent/runs?limit=10
AGENT_FORCE_RUN=1 bash /home/voidsstr/development/reusable-agents/agents/authority-agent/run.sh   # bypass short-circuit once (currently unnecessary, see Short-circuit)
```

`AGENT_FORCE_RUN=1` is a framework-wide escape hatch in
`AgentBase._check_short_circuit()`. This agent does not need it today,
because the short-circuit never fires. It becomes relevant once the
`next_state` fix lands.

## Failure modes & troubleshooting

| Symptom | Evidence / action |
|---|---|
| `indexed_pct` flat at 8.3 % | The newest specpicks `gsc_crawl_progress` row is from **2026-07-28** (sample 1/12); aisleprompt has one row (2026-06-08). Nothing on whitebeast schedules `scripts/gsc-crawl-tracker-cron.sh` (no timer or crontab entry found 2026-09-23), so the goal metric is frozen and `gsc_snapshot_id` never changes. With a 12-URL sample the metric moves in ~8.3-point steps. Restore the tracker before reading this goal. |
| Runs (and emails) daily even with a stale snapshot | Expected until the `next_state` fix: the signals hash is never persisted (see *Short-circuit & idempotency*). Latest runs were 2026-09-23 03:11 UTC and 17:00 UTC, both `success` ("indexed 8.3% · 40 citable assets · 10 authority targets emailed"). Once the hash persists, note that `newest_asset_id` is `max(id)` of the whole asset table. Any inserted row (any status, not only published) will still trigger a re-run. |
| `citable-asset query fell back` | `AUTHORITY_ASSET_TABLE` lacks the expected columns; worklist will be empty. |
| `worklist email skipped` / `ok=False` | msmtp/`shared.site_quality` failure; see the run's decisions. |

## Guardrails

- `send_external_outreach` is `@requires_confirmation` (risk high,
  affects external/reputation). Off-site link building stays a human action;
  auto-pitching is spam.

## Related agents

- `specpicks-gsc-coverage-auditor` / `aisleprompt-gsc-coverage-auditor` —
  other indexation signals.
- `specpicks-internal-link-densifier` — automated article-to-article
  `related_article_slugs` linking (this agent only recommends on-site links
  in the email).
- `specpicks-seo-opportunity-agent` / `aisleprompt-seo-opportunity-agent` —
  on-page SEO recs.
