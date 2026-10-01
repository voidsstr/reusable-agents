# Event Refresh Agent — Runbook

> The day after a retail sale ends, queue one Opus article-refresh rec per
> published article tagged `holiday:<id>`, so sale pages stop telling readers
> to buy in a sale that is over.

## Why it exists

Audit issue `seasonal-no-post-event-state` (2026-10-01). Deal pages are
written before the sale: "buy now", "worth buying on October 6–7",
"(Prime Big Deal Days Edition)" in the H1, and countdowns like "nine days to
decide". `featured_rotation` only drops the homepage flag, so from the
morning after the sale these pages give stale buying advice to readers and
to the AI assistants that quote them. Once the event's search demand
collapses they also earn nothing unless they are re-angled to the next sale.

## Schedule

- Cron `30 6 * * *` America/New_York. Amazon's October event ends 11:59 pm PT
  on its last day (02:59 ET), so the 06:30 run the next morning is the first
  one that sees it as ended. For Prime Big Deal Days 2026 (Oct 6–7) that is
  **2026-10-08 06:30 ET**.
- Manual: `curl -X POST http://localhost:8090/api/agents/event-refresh-agent/trigger -H "Authorization: Bearer $FRAMEWORK_API_TOKEN"`.

## Per-run flow

1. Load `config/event-refresh-config.json` (storage copy replaces the repo
   default wholesale) and the calendar (`seasonal_calendar.load_occasions`,
   so `config/seasonal-calendar.json` date overrides apply).
2. `seasonal_calendar.recently_ended(today, kinds=["retail-deal"])`: the
   occasions whose strict window (start .. start + length_days − 1, no NOW
   slack) ended 1..`lookback_days` days ago. None in the 60-day metric
   horizon: the run short-circuits with no DB traffic (`signals()`).
3. Per configured site (DSN from the env var named by `dsn_env`): one
   read-only SELECT of published rows whose `tags` hold any
   `holiday:<ended-id>`.
4. For each article not in the ledger (or due a retry, see below), build
   one `article-refresh` rec
   (`required_model_tier: opus`, `dispatch_kind: article-refresh`). The
   re-angle target is `seasonal_calendar.next_occasion(audience, today,
   kind=<same kind>)`: Black Friday (Nov 27–30, 2026) for both Prime Big Deal
   Days occasions.
5. Write `runs/<run_ts>/recommendations.json`, then `self.queue_recs(site=…)`
   per site (responder auto-queue → drainer → implementer), then record each
   article in the ledger.

## What each rec asks the implementer to do

In place (same row, slug and URL):
1. Event section + FAQ in the past tense with absolute dates ("Prime Big Deal
   Days ran October 6–7, 2026"), kept to one bounded H2 plus the FAQ.
2. Remove countdowns and present-tense urgency ("buy now", "nine days").
3. Re-angle to the next sale without inventing its prices; replace the
   `holiday:<ended>` tag with `holiday:<next>` (homepage rotation and the next
   post-event refresh follow the new framing, and an SSR "ended" note keyed on
   the old tag goes away once the refresh lands).
4. Drop the event name from the title (H1, headline, meta title); keep the
   slug, no redirect.
5. Honesty rules: no invented discounts, prices, ratings, testing or stock.
6. Bump `updated_at` (dateModified) only in the same UPDATE as the content
   change.
7. DB-only evidence: commit the apply script and write `applied-recs.json`.

## Idempotency and retries

Ledger at `agents/event-refresh-agent/state/event-refresh-ledger.json`, keyed
`<site>|<occasion-id>|<event-start>|<slug>`, value `{queued_at, attempts,
rec_id, first_queued_at}`. Written per site right after that site's queue
write succeeds. Next year's event has a new start date and is a new key.

The ledger is a retry gate, not a one-shot mark. The refresh moves the
article's tag off `holiday:<ended-id>`, so an article the SELECT still returns
has not been refreshed: the implementer deferred it (Opus unavailable writes
`deferred.json` and exits 0, and nothing else re-dispatches this agent's recs)
or the run changed nothing. It is queued again when all of these hold:

- its last queue is at least `retry_after_hours` old;
- `attempts < max_attempts`;
- no file in the responder auto-queue still holds its rec id (slow drain);
- `defer_backoff.should_skip(rec_id, "event-refresh-agent")` is false.

The rec id is stable per ledger key (`evt-<sha1[:12]>`), so the implementer's
`record_defer(rec_id, source_agent)` and this check refer to the same entry.
Retries cover every event in the 60-day metric horizon; the first queue stays
limited to `lookback_days`. Articles out of attempts count in
`refresh_attempts_exhausted` and are named in a `warning` decision.

## Config (`config/event-refresh-config.json`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | true | master switch |
| `kinds` | `["retail-deal"]` | occasion kinds that trigger a refresh. Holiday recipe pages stay framed for next year, so they are off by default |
| `lookback_days` | 7 | catch-up window after a missed run |
| `metric_horizon_days` | 60 | window for `ended_event_articles_live` |
| `max_recs_per_site` | 25 | per-run cap per site (first queues and retries) |
| `retry_after_hours` | 24 | min age of the last queue before a still-tagged article is re-queued |
| `max_attempts` | 5 | queues per article per event, first one included |
| `next_occasion_horizon_days` | 120 | how far ahead the re-angle target may be |
| `default` / `sites.<id>` | | `dsn_env`, `article_url_template` (`{slug}`), table/column names |

Empty `sites`: every `DATABASE_URL_<SITE>` env var is a site.

Operator env: `EVENT_REFRESH_TODAY=2026-10-08` evaluates as of that date;
`EVENT_REFRESH_DRY_RUN=1` reports what would be queued and writes nothing.
Rehearse with both before the event:

```bash
set -a; . ~/.reusable-agents/secrets.env; set +a
EVENT_REFRESH_TODAY=2026-10-08 EVENT_REFRESH_DRY_RUN=1 AGENT_FORCE_RUN=1 \
  python3 /home/voidsstr/development/reusable-agents/agents/event-refresh-agent/agent.py
```

## Metrics → goals

`ended_event_articles_live`, `unqueued_ended_event_articles`, `site_errors`
(see `goals.json`), plus `events_ended`, `articles_tagged`, `recs_queued`,
`recs_requeued`, `already_queued`, `refresh_attempts_exhausted`.

## When something breaks

- **Recs queued, pages unchanged after a day**: the implementer deferred them
  (`required-model-unavailable`, Opus pool exhausted). The agent re-queues
  them daily (`recs_requeued`) until `max_attempts`. If
  `refresh_attempts_exhausted` > 0, check `deferred.json` under the
  implementer runs and `claude_pool status`, then raise `max_attempts` in the
  storage config or delete those ledger keys to start over.
- **A site shows in `site_errors`**: its DSN env var or table/columns are
  wrong. The next run retries it (the short-circuit hash is not kept after a
  partial run).
- **Event fired a day late or early**: the calendar date is wrong. Fix it with
  `date_overrides` in `config/seasonal-calendar.json`, no commit needed.
