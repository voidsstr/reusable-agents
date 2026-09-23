# OAuth refresh-token heartbeat (`oauth-heartbeat-agent`)

> **Retired 2026-09-04** (`enabled: false`, timer disabled). Kept in the repo as
> a documented fallback. It pinged Google OAuth credential files daily so that a
> refresh token issued by an OAuth app in *Testing* status never hit Google's
> 7-day expiry. That protected the GSC/GA4 data behind **organic clicks and
> indexed pages** reporting while the SEO agents were paused.

## At a glance

| | |
|---|---|
| Agent id | `oauth-heartbeat-agent` |
| Home | `reusable-agents/agents/oauth-heartbeat-agent/` (`agent.py`, `goals.json`, `manifest.json`, this runbook) |
| Kind | AgentBase python (manifest `metadata.blueprint: scheduled-task`) |
| Schedule | Manifest `37 8 * * *` (`America/Detroit`). Unit timer `OnCalendar=*-*-* 8:37:00` exists but is **disabled / inactive** (checked 2026-09-23) |
| Entry command | `agent_run_wrapper.sh oauth-heartbeat-agent python3 /home/voidsstr/development/reusable-agents/agents/oauth-heartbeat-agent/agent.py` |
| Category | `ops` |
| Status | **registered-but-disabled**. See `metadata.disabled_reason` in `manifest.json` and commit `c924f0a` |
| Deep reference | `.claude/skills/refresh-gsc-token/SKILL.md` §6 (why it was retired) |

## Why it was retired

The OAuth consent screen for the GCP project named in the manifest's
`disabled_reason` was published **In production** on 2026-09-04. Refresh tokens for a published app don't
expire on the Testing-mode 7-day clock, so a daily ping was pure cost. From the
manifest's `disabled_reason`:

> If refresh tokens start dying weekly again, check the publishing status
> FIRST -- re-enabling this only hides it.

The GSC skill names the real test: the token minted 2026-09-02 16:33 would have
been revoked 2026-09-09 under Testing, so its surviving past that date is the
proof (not re-checked for this runbook). The app requests only `analytics.readonly` and `webmasters`
(neither is a restricted scope), so published-but-unverified is fine.

## What it does (when run)

1. Resolve the watch list: `OAUTH_HEARTBEAT_FILES` (colon-separated paths), or by
   default `~/.reusable-agents/seo/.oauth.json`.
2. For each file:
   - A missing file is recorded as `failed` with "file does not exist".
   - Otherwise, run `python3 agents/seo-opportunity-agent/lib/collector/refresh-token.py --oauth-file <path>`
     (timeout `OAUTH_REFRESH_TIMEOUT_S`, default 60 s). Exit 0 with non-empty stdout
     (an access token) means `ok`, and only the token *length* is recorded. Anything else is a
     failure, and the last 300 chars of output are recorded.
   - Each pinged file's outcome is logged via `self.decide("observation" | "warning", …)`.
     The missing-file case skips this log line.
3. Write `agents/oauth-heartbeat-agent/runs/<run-ts>/results.json` to framework storage.
4. If any failed, call `framework.core.resilience.notify_operator(...)` with
   `severity="high"` and `cooldown_s=86400` (one alert per agent and error class per day;
   since 2026-09-15 `resilience.py` may also fold it into the daily digest). The recovery
   hint is: re-run `install/reauth-google-oauth.sh` from a graphical session.
   The recipient comes from `FRAMEWORK_OPERATOR_EMAIL` / `OPERATOR_EMAIL`.
5. Return `failure` if any file failed, else `success`.

Minting an access token is what resets Google's idle clock. The agent never rewrites the
OAuth file.

## Inputs / outputs

- **Reads**: the OAuth JSON file(s) and their mtime (reported as `age_days`), plus Google's token
  endpoint via `refresh-token.py`.
- **Writes**: `runs/<run-ts>/results.json` (per-file `ok`, `error`, `mtime`, `age_days`,
  `token_len`) and the standard run artifacts. It emails through `notify_operator` only on failure.
- No DB, recs, or handoffs.

## Goals & metrics

`RunResult.metrics`: `oauth_files_watched`, `oauth_files_ok`, `oauth_files_failed`, `success_pct`.

| Goal id (`goals.json`) | Metric key | Seeded current → target |
|---|---|---|
| `goal-token-liveness` | `success_pct` | 100 → 100 |
| `goal-failed-pings-zero` | `oauth_files_failed` | 0 → 0 |
| `goal-oauth-file-coverage` | `oauth_files_watched` | 1 → 1 |

These goals are frozen while the agent is disabled.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `OAUTH_HEARTBEAT_FILES` | `~/.reusable-agents/seo/.oauth.json` | colon-separated OAuth files to ping (`metadata.needs_env`) |
| `OAUTH_REFRESH_TIMEOUT_S` | `60` | per-file timeout for `refresh-token.py` |

The unit sources `~/.reusable-agents/secrets.env`.

## Short-circuit & idempotency

`signals()` deliberately returns `None`. The whole point was to fire daily even
when nothing else changes. Each run is independent and side-effect-free apart from the
token mint.

## Running & inspecting

```bash
# One-off manual check (safe: it only mints an access token):
python3 /home/voidsstr/development/reusable-agents/agents/oauth-heartbeat-agent/agent.py
# Or validate the token directly:
python3 /home/voidsstr/development/reusable-agents/agents/seo-opportunity-agent/lib/collector/refresh-token.py \
    --oauth-file ~/.reusable-agents/seo/.oauth.json --check
systemctl --user is-enabled agent-oauth-heartbeat-agent.timer   # expect: disabled
```

**Re-enabling** is a last resort, only after the consent screen has been confirmed
(again) as *In production*. Set `"enabled": true` in `manifest.json`, then run
`bash /home/voidsstr/development/reusable-agents/install/register-agent.sh /home/voidsstr/development/reusable-agents/agents/oauth-heartbeat-agent`
(API default `http://localhost:8090`).

## Failure modes & troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `invalid_grant` / "Token has been expired or revoked" | Refresh token dead. Re-mint per `.claude/skills/refresh-gsc-token/SKILL.md` (`install/reauth-gsc.sh`, which wraps `install/reauth-google-oauth.sh`; see also `install/fix-gsc-now.sh`). If this recurs weekly, check the consent-screen publishing status first |
| `file does not exist` | The watched path moved. Fix `OAUTH_HEARTBEAT_FILES` |

## Related

- `seo-opportunity-agent` (engine) and its per-site instances: the collector
  (`lib/collector/refresh-token.py`) and the main consumer of the token.
- `*-gsc-coverage-auditor`, `*-indexnow-submitter`: other consumers of the same token, named in the GSC skill.
- Skill `/refresh-gsc-token`: the operator runbook for re-minting.
