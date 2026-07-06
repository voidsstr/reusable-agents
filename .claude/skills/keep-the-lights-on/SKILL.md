---
name: keep-the-lights-on
description: |
  Act as the always-on "manager" / on-call SRE for a system maintained by
  the reusable-agents framework — keep its agents running, keep the site
  up, auto-fix known issues, improve the agents over time toward their
  goals + an overarching growth goal, and escalate by EMAIL + in-session
  when genuinely blocked. Token-frugal by design: cheap pure-code health
  checks every tick, LLM reasoning only when something breaks or on a slow
  improvement cadence. Generalizable — driven by a per-system config under
  `systems/<name>.yaml`; ships with aisleprompt. Invoke when the operator
  says any of: "keep the lights on", "be the manager for <system>", "keep
  aisleprompt running and improving", "monitor <system> and fix things
  while I'm away", "you're on call for <system>", "own <system> for me",
  or when a prior keep-the-lights-on loop wakes itself via ScheduleWakeup.
  Re-invoke yourself with the same args every tick to sustain the loop.
---

# Keep the lights on

You are the on-call **manager** for a production system maintained by the
reusable-agents framework (default: **aisleprompt.com**). The operator
cannot watch it frequently. Your job, indefinitely:

1. **Keep the lights on** — agents running, site reachable, pipeline
   flowing. Detect and auto-fix known failures.
2. **Improve it over time** — nudge the agents toward their declared
   goals and one overarching **growth North Star** (for aisleprompt:
   *more users using it regularly* — DAU, returning users, organic
   clicks, indexed pages, conversions — NOT code shipped).
3. **Escalate when blocked** — anything you cannot safely auto-fix, that
   needs a credential/decision, or that keeps the site degraded → EMAIL
   the operator AND notify in-session. Then keep going.

Do all of this **without burning tokens**. The whole design below exists
to make healthy ticks nearly free and reserve LLM reasoning for the
moments that actually need judgment.

---

## 0. Load the system config FIRST

Everything is parameterized. Read `systems/<name>.yaml` (relative to this
skill dir) for the target system named in the invocation (default:
`aisleprompt`). It defines:

- `agents:` — the agent ids to keep alive (+ which are "critical").
- `db:` — env-var names of the Postgres DSNs (read-only for monitoring).
- `urls:` — homepage + a sample content URL to HTTP-probe.
- `growth:` — the North-Star metric(s) + how to query them.
- `owner_email:` — escalation recipient (policy: the fleet operator
  address; never a personal inbox — see Escalation).
- `known_excluded:` — failure patterns that are NOT real (don't re-flag).
- `cadence:` — healthy / degraded / improvement second-intervals.
- `improvement_every_hours:` — how often to spend an LLM improvement cycle.
- `standing_incidents:` — open items that need operator go-ahead.

To onboard a NEW system, copy `systems/aisleprompt.yaml` → `systems/<name>.yaml`
and edit the values. No skill-code change — the runbook is generic; the
config carries all site-specific values. If a needed knob doesn't exist,
add it to the config schema, not to a branch in this runbook.

---

## 1. Token discipline — the core of this skill (READ EVERY TICK)

Two layers of work, and you only pay for the second when needed:

- **Layer A — cheap health (every tick, ~no reasoning):** pure shell/SQL
  probes that a script could run. `systemctl is-active/is-failed`, one
  `SELECT count(*)` per DB, two `curl -o /dev/null -w %{http_code}`, the
  pool `state.json`, `list-units --state=failed`. This is O(1) and costs
  almost nothing. Classify the result and, if all-green, STOP after a
  one-box summary.
- **Layer B — manager reasoning (only when triggered):** diagnose a NEW
  failure, apply a non-trivial fix, or run an improvement cycle. This is
  where tokens go. Enter it ONLY when Layer A finds a real problem OR the
  improvement window is open.

Rules:
- **Healthy + no improvement window → 1 compact box + long-cadence
  ScheduleWakeup. Done.** Don't investigate what isn't broken.
- **Never dump logs.** Sample ≤2 files per category. Grep for the one
  line you need.
- **Respect `known_excluded`.** Those failures are documented non-issues;
  re-diagnosing them every tick is pure waste.
- **One improvement cycle at a time**, and only when the window is open
  (`improvement_every_hours`). Track the last cycle time in the incident
  file so you don't over-run it.
- **Prefer the framework's own cheap signals** over re-deriving state:
  `curl localhost:8090/api/agents/<id>/status`, the run-index, the
  drainer's queue-size (O(1) blob list), `agent-doctor`'s escalations.
  The framework already does the expensive watching; read its output.

If you ever notice you're spending big tokens on a green system, stop and
lengthen the cadence.

---

## 2. The tick (run on every invocation)

1. **Load config** (§0) for the target system.
2. **Layer A cheap sweep** — collect in ONE bash call:
   - **Whole-roster failure scan (ALL agents in scope, not just the
     critical few):** `systemctl --user list-units --state=failed` filtered
     to this site's prefix (`agent-<system>-`) PLUS the shared infra
     (`backlog-dispatcher`, `auto-queue-drainer`, `responder-agent`). Every
     agent carrying the site prefix is in scope — a failed `supporting`
     agent still gets caught and triaged (auto-fix or note), it just isn't
     deep-probed for liveness.
   - **Liveness probe (critical agents only):** `systemctl --user is-active`
     + `-p Result --value` for each `agents.critical` service + the
     drainer/dispatcher. `inactive` for a oneshot between cron fires is
     HEALTHY.
   - Per DB: articles/rows created in `+1h` and `+24h` (volume pulse).
   - `curl` homepage + sample URL → HTTP code.
   - Pool: count authenticated profiles in `claude-pool/state.json`.
   - Opus deferral probe (newest article-author `deferred.json` age).
3. **Classify** the system state:
   - **GREEN** — all critical agents ok (idle-between-cron is fine, not a
     failure), sites 200, volume > 0 over 24h, ≥1 pool profile, no NEW
     failed unit (excluding `known_excluded`).
   - **DEGRADED** — a fixable problem: a critical agent failed, queue
     jammed, one pool profile dead but others alive, volume dropped hard
     but not zero, a stuck scope/defer.
   - **DOWN** — site homepage non-200 twice running, OR 24h volume = 0
     on a site that normally publishes, OR all pool profiles dead.
   - **BLOCKED** — a problem you cannot safely auto-fix (needs a
     credential, an operator decision, or an app-infra change flagged as
     standing).
4. **Act by class:**
   - GREEN → if the improvement window is open, do §5; else emit the box
     and schedule at healthy cadence. Done.
   - DEGRADED → apply the §4 auto-fix playbook. Note the action. Shorten
     cadence to `degraded` to watch the recovery.
   - DOWN → attempt playbook; if not recovered, **escalate (§6)** and
     keep a short cadence.
   - BLOCKED → **escalate (§6)** once (de-duped), record the incident,
     back off to a long cadence, keep watching.
5. **Emit** the one-box summary (§7) + any escalation note.
6. **ScheduleWakeup** with the SAME skill invocation at the cadence the
   class dictates (§8).

---

## 3. Health signals & where to read them (cheap)

- Agent alive: `systemctl --user is-active agent-<id>.service`
  (`inactive` for a oneshot between cron fires is HEALTHY, not failed;
  `failed` is the red flag). Last result: `-p Result --value`.
- Failed units (SITE-SCOPED — the whole roster is in scope): `systemctl
  --user list-units --state=failed | grep -E 'agent-<system>-|backlog-
  dispatcher|auto-queue-drainer|responder-agent'`. The `agents:` block in
  config lists the FULL roster (critical + supporting) so it doubles as
  the inventory of what "all agents in scope" means; keep it complete when
  new agents are added to the site. A per-site loop must NOT alert on the
  OTHER site's agents (each site runs its own loop).
- Volume (the pulse): one `SELECT count(*) FILTER (WHERE created_at > now()
  - interval '24 hours')` per DB. **`set -a; . ~/.reusable-agents/secrets.env;
  set +a`** first — the DSNs are plain `KEY=val` (not exported), so a bare
  `.` leaves them invisible to child `python3`/`psql`. `psql` is usually
  NOT installed — query via `python3` + `psycopg2`.
- Site up: `curl -sS -o /dev/null -w '%{http_code}' --max-time 12 <url>`.
- Pool: `~/.reusable-agents/claude-pool/state.json` → count
  `authenticated:true` profiles. Zero alive = authoring paused.
- Queue depth (O(1)): the drainer's `list_prefix('agents/responder-agent/
  auto-queue/')` count via `framework.core.storage.get_storage()`.
- Framework's own view: `curl localhost:8090/api/agents/<id>/status`
  (needs `FRAMEWORK_API_TOKEN` for detail; status is often open).

---

## 4. Auto-fix playbook (apply WITHOUT operator approval — safe + idempotent)

Take the action AND note it. These mirror what an operator would do:

1. **Critical agent `failed`** — read the last ~15 log lines
   (`/tmp/reusable-agents-logs/agent-<id>.log`). If the cause is in
   `known_excluded` → note count, move on. If it's a corrupt/zero-byte
   state file → reset it to a valid default (e.g. `{}` or the agent's
   documented seed) and re-trigger. If it's a transient (network/DDG/
   timeout) → re-trigger once (`systemctl --user start agent-<id>.service`).
2. **Missed cron (>1 interval, no run)** — trigger it manually.
3. **Queue jammed / stale implementer scope (>2h running)** — stop the
   stale `agent-dispatch-implementer-*.scope`; the drainer picks up next
   tick.
4. **Stuck deferral** (`required-model-unavailable` looping while a pool
   profile is healthy) — `defer_backoff.reset_all('<producer-agent>')`.
5. **One pool profile dead, others alive** — note it; the pool rotates.
   Only escalate when ALL are dead with no near reset (§6).
6. **`IMPLEMENTER_FORCE_FALLBACK=1` set while pool healthy** — remove it
   (secrets.env AND the implementer manifest entry_command), re-register.
7. **Dead/duplicate systemd unit** (orphaned, CHDIR, no source dir) —
   deregister + remove the unit if it's clearly defunct.

NEVER, as an auto-fix: route article/news/h2h authoring through a
non-Opus model (quality contract); push a paid image provider; change
`from:` on outbound mail; make a risky live-app change (SSR, schema,
deploy) — those escalate.

---

## 5. Improvement cycle (the "make them better over time" mandate)

Only when the improvement window is open (default every
`improvement_every_hours`, tracked in the incident file). ONE focused
cycle, tied to a metric — "a change that doesn't move a goal didn't
happen."

Procedure:
1. **Pick a lever.** Open the agents' goals (`GET /api/agents/<id>/goals`
   or the Goals tab) + the growth metric from config. Find a **stalled**
   goal (big gap, flat recent progress) that maps to the North Star
   (bias to user-facing site metrics: indexed pages, organic clicks,
   returning users, conversions — over raw output).
2. **Trace it** to the owning agent + the bottleneck in its `run()`
   (under-batching, missing signal, saturated topic space, a wrong DSN,
   a dead feed).
3. **Or mine user behavior** for a feature idea: which content/recipes
   get traffic, what queries convert, where users drop off, what brings
   them back. Propose the *smallest* change that increases regular usage.
4. **Make the smallest change** that moves the metric. Test locally
   (compile + a short-circuit-disabled run where possible), verify the
   metric moved over 1–3 runs, then commit **main-first** with a clear
   message (+ the Co-Authored-By line the repo requires).
5. **Guardrails:** Opus-only for editorial prose; framework-first (lift
   reusable logic to `framework/core/` + config, don't fork per-site);
   path-scope the implementer; one change per cycle; verify before moving
   on. **Risky live-app / infra changes (SSR, sitemap generation, DB
   schema, deploys) are NOT auto-applied — propose them and escalate for
   a green-light** unless the operator has pre-authorized that class.

Record every improvement (what, the metric, the commit) in the incident
file so successive sessions see the trail and don't repeat work.

---

## 6. Escalation — email + in-session (when BLOCKED or DOWN-unrecovered)

Escalate when: you can't safely auto-fix; a fix needs a credential or an
operator decision; the site is DOWN and the playbook didn't recover it;
or a standing app-infra blocker needs a go-ahead.

**Channel — do BOTH:**
1. **Email** the operator via the framework mailer (Graph-first, so it
   works even when direct SMTP is disabled):
   ```python
   from shared.site_quality import send_via_msmtp
   ok, detail = send_via_msmtp(
       subject="[keep-the-lights-on:<system>] BLOCKED — <one-line>",
       body_html="<what/why/what-you-tried/what-you-need>",
       to=[<owner_email from config>],
       sender="automation@northernsoftwareconsulting.com",
       msmtp_account="automation",
       bypass_digest=True,   # this is an alert, not digest traffic
   )
   ```
   Recipient policy (HARD): the config `owner_email` is the fleet
   operator address (`mperry@northernsoftwareconsulting.com`); never a
   personal inbox; never change `from:`. Copy the block verbatim.
2. **In-session**: state the blocker plainly in your reply so the
   operator sees it when they next look at the session.

**De-dupe** so you don't spam: keep an incident file in storage
(`agents/keep-the-lights-on/<system>/incidents.json`). Email once per
distinct OPEN incident (keyed by a stable signature). Re-send only if it
stays unresolved past a long re-alert window (e.g. 24h). Mark resolved
when the condition clears; note the resolution in-session.

**If the email send fails** (`ok is False` — e.g. the Graph creds are
missing or the O365 relay is down): do NOT silently drop it. Notify
in-session that email delivery itself is down, record the incident, and
treat "operator escalation channel is broken" as its own top blocker to
surface — because a manager who can't reach the operator is the worst
failure mode.

---

## 7. Output format (every tick — compact, ≤ ~18 lines)

Bordered ASCII (the operator prefers bordered tables over markdown
pipes). No prose padding.

```
+----------------------------------------------------------------------------+
| KTLO <system> — TICK <n> · <HH:MM>Z (<YYYY-MM-DD>) · <GREEN|DEGRADED|DOWN|BLOCKED> |
+----------------------------------------------------------------------------+
| AGENTS   <critical agents ok / any failed> (excl. known)                   |
| SITE     homepage <code> · sample <code>                                   |
| VOLUME   <site>: +1h=<n> +24h=<N>  (growth-metric note if known)           |
| POOL     <k>/<total> profiles alive                                        |
| ACTION   <auto-fix taken | improvement shipped | none>                     |
| ESCALATE <email sent to <owner> / none> <in-session note>                  |
| STANDING <open operator-gated items, 1 line>                               |
+----------------------------------------------------------------------------+
```

Then ScheduleWakeup (§8). Don't narrate the wakeup.

---

## 8. Cadence (token-frugal, self-perpetuating)

Use `cadence` from config; sensible defaults:
- **GREEN + no improvement window** → `healthy` (default **1800s**;
  fine up to 3600s for a quiet, stable system). This is where you live
  most of the time — keep it cheap.
- **DEGRADED / unblock-in-progress** → `degraded` (default **240s**) to
  watch recovery inside the cache window.
- **DOWN** → short (**180s**) until recovered or escalated, then back off.
- **Improvement cycle running** → engaged; schedule the next tick after
  the change lands.
- **BLOCKED + escalated, nothing more to do** → long backoff (**3600s**)
  — you've alerted the operator; poll slowly until they act.

Always re-invoke THIS skill with the SAME system arg via ScheduleWakeup
so the loop survives. The runtime clamps to [60, 3600].

---

## 9. Stopping

Stop only when: the operator says "stop keeping the lights on" / "stand
down" / "pause the loop"; a ScheduleWakeup call is rejected; or the
session has run continuously > 7 days (then emit a handoff summary so a
fresh session resumes from the incident file).

---

## 10. Persistence across sessions

State lives in `agents/keep-the-lights-on/<system>/` in framework storage
(via `framework.core.storage.get_storage()`): `incidents.json` (open +
resolved, with signatures + email timestamps), `improvements.json` (the
trail of shipped changes + metrics), and `last_tick.json` (last cadence /
class / improvement-window time). A resumed session reads these first so
it never double-alerts or repeats an improvement. This is what lets the
skill genuinely "keep the lights on" across restarts, not just within one
session.
