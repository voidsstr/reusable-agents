# Goals Tracker (`goals-tracker`)

> A daily HTML email that rolls up every enabled agent's declared goals:
> baseline, current and target values, trend, and a sparkline for each goal,
> plus a stale-metrics alert. It serves the North Star indirectly by making
> the Goals system readable in one mail. The two site KPI cards on top show a
> hero KPI (AislePrompt: Instacart cart creates; SpecPicks: Amazon clicks)
> plus organic clicks and impressions, conversions and % indexed.

## At a glance

| | |
|---|---|
| Agent id | `goals-tracker` |
| Code | `reusable-agents: agents/goals-tracker/agent.py` (this dir: code + docs, **no manifest**) |
| Registered instance | `nsc-assistant: agents/goals-tracker/` (`manifest.json`, `goals.json`, `run.sh`). Its `AGENT.md` and `SKILL.md` are **symlinks** to the files in this dir. |
| Kind | Plain python script run through a bash wrapper. **Not** an AgentBase subclass. |
| Schedule | Manifest `0 12 * * *` with `timezone: UTC`. The framework scheduler ignores `timezone`, so the systemd timer is `OnCalendar=*-*-* 12:00:00` in **host local time (America/Detroit)**. It fires at **12:00 EDT / 16:00 UTC**, not the "7am" that older docs, `run.sh` comments and the digest footer claim. Timer enabled. |
| Entry command | `bash /home/voidsstr/development/nsc-assistant/agents/goals-tracker/run.sh`, which runs `python3 $RA_REPO/agents/goals-tracker/agent.py` |
| Category | ops |
| Status | live |
| Logs | `/tmp/reusable-agents-goals-tracker.log` (from `run.sh`, which holds the real output). The wrapper log `/tmp/reusable-agents-logs/agent-goals-tracker.log` stays empty because `run.sh` redirects everything. |

Not to be confused with `site-goals-tracker`
(`reusable-agents: agents/site-goals-tracker/`, instances
`aisleprompt-site-goals-tracker` and `specpicks-site-goals-tracker`, daily at
13:00). That agent **records** the site metrics that this agent **reports**.

## What it does

`main()` in `agent.py`:

1. `collect_all_agents()` calls `framework.core.registry.list_agents()` and
   **skips agents with `enabled: false`**. For each remaining agent it reads
   `agents/<id>/goals/active.json` (definitions) and the metric timeseries
   cache through `framework.core.metric_helper.read_cache()`. Agents with no
   goals are dropped.
2. Per goal it computes the baseline (first cached point), current (latest
   cached value, else `metric.current`), trend % and a stale flag. A goal is
   stale if its latest point is older than `GOALS_TRACKER_STALE_HOURS` or it
   has no point at all.
3. It renders the HTML:
   - site KPI cards for `aisleprompt-site-goals-tracker` and
     `specpicks-site-goals-tracker` (goal ids hard-coded in
     `render_site_kpi_cards`)
   - a stale-agents alert
   - per-site sections with a goal table per agent and inline SVG
     sparklines (last 30 points)
4. It writes the HTML to `--out` (default `agents/goals-tracker/last-digest.html`
   in this dir, working tree only; the agent does not commit it).
5. It sends the mail through Microsoft Graph `sendMail`, using
   `GOALS_TRACKER_OAUTH_FILE` and `agents/responder-agent/mint-token.py`.
   If that fails it tries msmtp account `GOALS_TRACKER_MSMTP_ACCOUNT`, then
   msmtp account `personal` as a last resort.
6. It prints `OK sent: N agents, N goals, N stale` (or `FAIL send: …` and
   exit 1).

## Inputs

- Framework registry (`registry/agents.json` in storage)
- `agents/<id>/goals/active.json` and the metric_helper timeseries cache, for every enabled agent
- `~/.reusable-agents/responder/.oauth.json` (Graph send)

## Outputs

- **Email** to `GOALS_TRACKER_TO`, falling back to `OPERATOR_EMAIL`. On this
  host that is `mperry@northernsoftwareconsulting.com`. It is sent as
  `GOALS_TRACKER_FROM`, falling back to `OPERATOR_FROM_EMAIL`
  (automation@northernsoftwareconsulting.com). Subject template:
  `[Goals Tracker] {date} — {n_agents} agents, {n_goals} goals, {n_stale} stale`.
  It sends directly and does **not** go through the `DIGEST_ONLY` gate.
- **File:** `last-digest.html` in this dir. It is occasionally committed by
  hand as "goals-tracker: latest digest artifact". Treat it as generated
  output.
- **No storage writes, no recs, no handoffs.**

## Goals & metrics

Goals are declared in `nsc-assistant: agents/goals-tracker/goals.json`
(read-only reference) and stored at `agents/goals-tracker/goals/active.json`:

| Goal id | target_metric | Target |
|---|---|---|
| `goal-stale-agents-zero` | `n_stale` | 0 agents |
| `goal-goal-coverage-fleetwide` | `n_goals` | 250 goals |
| `goal-daily-digest-delivery` | `n_agents` | 74 agents |

**These are never recorded.** The script is not AgentBase and emits no
`RunResult.metrics`, so every goal still shows `current 0` and the tracker
lists itself as stale ("last never"). The real numbers are only in the log
line. The run on 2026-09-23 reported **55 agents, 222 goals, 37 stale**.
Recording them requires the AgentBase conversion.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `GOALS_TRACKER_TO` | `$OPERATOR_EMAIL`, else empty | Recipient |
| `GOALS_TRACKER_FROM` | `$OPERATOR_FROM_EMAIL`, else empty | Sender |
| `GOALS_TRACKER_OAUTH_FILE` | `~/.reusable-agents/responder/.oauth.json` | Graph credentials |
| `GOALS_TRACKER_MSMTP_ACCOUNT` | `automation` | msmtp fallback account |
| `GOALS_TRACKER_STALE_HOURS` | `30` | Staleness threshold |
| `GOALS_TRACKER_SUBJECT` | see above | Subject template (`{date}`, `{n_agents}`, `{n_goals}`, `{n_stale}`) |
| `GOALS_TRACKER_LOG` | `/tmp/reusable-agents-goals-tracker.log` | `run.sh` log path |
| `RA_REPO` | `/home/voidsstr/development/reusable-agents` | Where `run.sh` finds `agent.py` |

CLI: `--no-email` (render only), `--out <path>`, `--to <addr>`.

## Short-circuit & idempotency

None. Every run re-reads everything and sends one email. Running it twice
sends two emails.

## Running & inspecting

```bash
# Render only, no mail (writes last-digest.html)
python3 /home/voidsstr/development/reusable-agents/agents/goals-tracker/agent.py --no-email

# Scheduled path (sends mail)
systemctl --user start agent-goals-tracker.service
tail -30 /tmp/reusable-agents-goals-tracker.log
systemctl --user list-timers | grep goals-tracker
curl -s -H "Authorization: Bearer $FRAMEWORK_API_TOKEN" http://localhost:8090/api/agents/goals-tracker
```

## Failure modes & troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard shows the unit green but no mail arrived | `run.sh` wraps the python call in `\|\| echo "(goals-tracker failed or timed out)"`, so the unit always exits 0. Read `/tmp/reusable-agents-goals-tracker.log` for `FAIL send` or Graph/msmtp errors. |
| `azure read_bytes agents/<id>/goals/…: … BlobArchived` | The blob is in the Azure **Archive** tier and cannot be read. The tracker keeps running. On 2026-09-23: `timeseries-cache.json` for `specpicks-benchmark-research-agent`, `specpicks-head-to-head-agent` and `specpicks-product-hydration-agent` (those agents show as stale / "last never"), and `goals/active.json` for `market-research-pipeline` (that agent has no readable goals, so it drops out of the digest). Rehydrate the blob or change its tier. |
| An agent is missing from the digest | It is `enabled: false` in the registry (for example `digest-rollup-agent` and `implementer`, even though both run), or it has no goals. |
| Graph send fails | Check `~/.reusable-agents/responder/.oauth.json` (see commit `2c91764`, which moved the send to Graph). |

## Related agents

- `site-goals-tracker` engine + per-site instances: they record the site KPI goals shown in the top cards.
- `digest-rollup-agent`: the other daily operator email (`reusable-agents: agents/digest-rollup-agent/README.md`).
- Every agent with goals: the tracker only reports what each agent records through `record_goal_progress` / `RunResult.metrics`.
