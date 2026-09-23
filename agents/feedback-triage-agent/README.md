# Feedback Triage Agent

Turns user feedback into fixes. Reads a site's feedback table, decides what is
actionable, and hands real defects to the **implementer** (via the backlog
dispatcher, which is the only thing that edits code and deploys).

> **Runbook:** [AGENT.md](AGENT.md) has the operational detail: schedule,
> inputs and outputs, config, the short-circuit caveat and failure modes. This
> file is the design narrative. Per-site instances are at
> `aisleprompt: agents/feedback-triage-agent/` and
> `specpicks: agents/feedback-triage-agent/`, each with its own README.md.

## The status machine is the point

Feedback arrives as `new`. The agent **claims a row atomically** before looking
at it, so two ticks — or two agents — cannot both start on the same report:

```
new ──claim──> triaging ──actionable──> in_progress ──(manual)──> resolved
                   │
                   ├── not a defect ──> wont_fix
                   └── too vague ─────> needs_info
```

Only `new` is ever selected. The claim is a single
`UPDATE … SET status=triaging WHERE id=? AND status=new RETURNING …`, so a
second worker that read the same id loses the race and moves on. A row sitting
in `in_progress` is work in flight and is left alone. Verified: a second run
immediately after the first re-claimed **nothing** already triaged.

The `resolved` step is not automated. No agent sets it (checked 2026-09-23).
A shipped fix leaves its row in `in_progress`. The vendored widget's admin
API (`PATCH /admin/:id`) only accepts `NEW | TRIAGED | LINKED | CLOSED`, so it
cannot set `RESOLVED` either. That takes a manual SQL `UPDATE`.

## Triage is deterministic, not an LLM call

Deciding whether feedback is actionable is a filter. The LLM only earns its cost
on the survivors — investigating and writing the fix — and that happens in the
implementer, not here. `triage.py` is pure (no DB, network or LLM), so it can
be unit-tested in isolation. No test file for it exists in the repo as of
2026-09-23.

It rejects:
- **our own test exhaust** — 84 of the 86 rows in aisleprompt's table were
  "shape test" / "regression-1787523300149" / "Testing bug category submission".
  Without this filter the queue is the agent's own smoke tests.
- **praise and feature requests** — real feedback, not defects.
- **reports too vague to chase** — "it broke" gets `needs_info`, not a deploy.

It accepts a defect with a concrete failure signal, and scores confidence higher
when the report quotes a runtime error and names a URL.

## Reaching a deploy

Actionable defects are written to `recommendations.json` in the agent's run-dir.
`backlog-dispatcher-agent` walks producer run-dirs and dispatches to the
implementer — which is why both agent ids are in its `PRODUCER_AGENT_IDS`. A rec
that never reaches that list is never implemented.

## Site-agnostic by config

The two feedback-table shapes disagree on both table name and column casing:
the vendored widget's `"Feedback"."createdAt"` vs aisleprompt's older
`feedback.created_at`. aisleprompt reads both tables, and specpicks reads only
`"Feedback"`. The mapping lives in each site's `site.yaml`, so this file needs
no per-site branches.

| env | default | meaning |
|---|---|---|
| `FEEDBACK_TRIAGE_CONFIG` | — | required; path to the site's `site.yaml` |
| `FEEDBACK_MAX_PER_RUN` | `20` | reports claimed per tick |
| `AGENT_ID` | `feedback-triage-agent` | instance id; the per-site manifests set it |

The DSN is read from the env var named by `site.yaml: db_env`, with
`DATABASE_URL` as the fallback.

## Proven on a real report

2026-08-30, `https://aisleprompt.com/chat`: *"redirecting to instacart failed,
then i see 'require is not defined'"*. Classified **investigate** at confidence
1.0, moved to `IN_PROGRESS`, queued for the implementer. The underlying defect is
real — `frontend/src/utils/analytics.ts:398` calls `require('./instacart')` in
browser code while all seven sibling call sites use `import('./instacart')`.

The loop then closed. The implementer shipped the rec as aisleprompt commit
`809c8f54` ("implementer: fb-20260830T025702Z-001: Fix require is not defined
when redirecting to Instacart"). On 2026-09-23 `analytics.ts` no longer
contains `require('./instacart')`.
