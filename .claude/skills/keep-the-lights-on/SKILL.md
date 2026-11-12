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

Everything is parameterized. Each SITE owns its config in its OWN repo
(framework-first: the reusable-agents repo holds only the shared runbook +
template + registry). Resolve the config for the target system named in the
invocation (default: `aisleprompt`) in this order:

1. **`systems/registry.yaml`** in this skill dir → `systems.<name>.config`
   (the path inside the site's repo — authoritative).
2. Convention fallback: `/home/voidsstr/development/<name>/keep-the-lights-on.yaml`.
3. Dev/example fallback: `systems/<name>.yaml` in this skill dir.

The config (wherever it resolves) defines:

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
- `catalog_health:` — `checks:` (bounded `SELECT count(*)` probes, §5c; a
  check may bind the §5f money set with `params:` and name an `owner_class`)
  and `agent_metric_checks:` (bounds on an owner's own `RunResult.metrics`).
- `conversion:` — the money path (§5f): verified-human click spec + profile,
  templates, money placements, money set, floors, goals, definition drift,
  config reads, the money-page probe, and `owners:` (one owner per outcome).
- `shop_rollout:` — the staged rollout of buy-box recs (§5f): state key,
  template paths, the 48 h spacing, and an optional operator `freeze`.

To onboard a NEW system: copy `systems/_example.yaml` into the site's OWN
repo as `keep-the-lights-on.yaml`, edit the values, add a line to
`systems/registry.yaml`, and commit the config to THAT site's repo (the
site owns its config). No skill-code change — the runbook is generic; the
config carries all site-specific values. If a needed knob doesn't exist,
add it to the config schema (`_example.yaml`), not to a branch in this
runbook. When you change a live config, commit it in the SITE repo
(main-first), not here.

**System types (`type:` in the registry + config).** A system is either a
**`site`** (the default — agents + DB + homepage + publish volume + optional
Playwright suite) or a **`daemon`** (a long-running service with none of
those; health = "the process is active AND actively doing its work"). The
tick, health classes, and box adapt to the type:

- **site** — the full §2 tick (agent roster, DB volume pulse, homepage
  probe, pool, functional tests). GREEN needs volume > 0 + sites 200.
- **daemon** — a much smaller tick: `systemctl --user is-active <unit>` AND a
  liveness check that the daemon has done recent work (grep its journal for
  `daemon.liveness_log_grep` within `daemon.liveness_window_min`). A daemon
  can be **alive-but-hung ("zombie")** — the process stays `active` while its
  work loop is dead — so liveness-alone is not enough; the recent-work grep
  is what catches a zombie. GREEN = active + a fresh liveness line. DEGRADED =
  active but no liveness in the window → **restart** the unit (auto-fix). DOWN
  = inactive/failed and won't stay up. There is no DB/volume/Playwright for a
  daemon. `retro-chat` is the reference daemon (config: `systems/retro-chat.yaml`).

**Managing multiple systems in ONE loop.** A single combined session can keep
several systems lit at once (e.g. `aisleprompt + specpicks + retro-chat`).
Run each system's tick, emit ONE box per system, then a single ScheduleWakeup
at the shortest cadence any system needs. Keep per-system state separate
(each has its own `incidents.json`). A site loop must never alert on another
site's agents. See `docs/keep-the-lights-on.md` for the current live roster.

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
   - **Per-agent roster snapshot (EVERY agent in scope — ALL of
     `agents.critical` + `agents.supporting`, plus shared
     `backlog-dispatcher`/`auto-queue-drainer`/`responder-agent`):** loop
     over the config's FULL agent list and collect three fields per agent —
     this is the per-agent health table the operator sees every tick (§7):
       - **HEALTH** — `systemctl --user is-active agent-<id>.service` +
         `-p Result --value`. Map: `active`/`activating` → `run`;
         `inactive` + `result=success` → `idle` (a oneshot between cron
         fires is HEALTHY, not a failure); anything else / `failed` →
         `FAILED` (the red flag).
       - **LAST RUN** — `systemctl --user show -p ExecMainExitTimestamp
         --value agent-<id>.service` (when it last finished); fall back to
         the newest timestamp in its log if the unit was reset.
       - **LAST MAJOR WORK** — the final status line of
         `/tmp/reusable-agents-logs/agent-<id>.log`. The framework logs
         `[ts] [id] <status> (<pct>) <summary>`; the LAST such line is the
         run outcome (e.g. "shipped rec-008", "proposed 3 topics",
         "Refreshed 42 products; dead-link scan deactivated 5"). One
         `tail`+`grep` per agent — still O(1), no reasoning.
     This single snapshot **doubles as the failure scan**: any agent whose
     HEALTH is `FAILED` (and not in `known_excluded`) is triaged per §4.
     Critical agents are additionally deep-probed for liveness; a failed
     supporting agent is caught + triaged but not deep-probed. Keep the loop
     pure shell so the whole roster stays nearly free.
   - Per DB: articles/rows created in `+1h` and `+24h` (volume pulse).
   - `curl` homepage + sample URL → HTTP code.
   - Pool: count authenticated profiles in `claude-pool/state.json`.
   - Opus deferral probe (newest article-author `deferred.json` age).
   - If the config has `conversion:` / `shop_rollout:`: the §5f fields
     (`conversion`, `agent_metrics`, `placement_watch`, `shop_rollout`,
     `routes`). Same sweep, about 1 s per site from the hourly cache.
3. **Classify** the system state:
   - **GREEN** — all critical agents ok (idle-between-cron is fine, not a
     failure), sites 200, volume > 0 over 24h, ≥1 pool profile, no NEW
     failed unit (excluding `known_excluded`).
   - **DEGRADED** — a fixable problem: a critical agent failed, queue
     jammed, one pool profile dead but others alive, volume dropped hard
     but not zero, a stuck scope/defer — or **the site serves but the money
     leaks** (any §5f DEGRADED condition: floor, sustained drop, placement
     incident, dead money pick, unreadable config, definition drift).
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
1. **Pick a lever.** A §5f floor breach or sustained drop outranks every
   other goal in the improvement window: start there. Otherwise open the
   agents' goals (`GET /api/agents/<id>/goals`
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

## 5b. Functional verification — Playwright suite (daily + after prod changes)

If the config has a `functional_tests` block, running the site's full
end-to-end suite against PRODUCTION is part of keeping the lights on.
Running the tests is PURE SHELL (no LLM tokens); the LLM engages only to
triage/PATCH a failure or to deepen coverage.

**When to run (checked cheaply each tick, don't run every tick):**
- **After a prod change since the last run — the highest-value trigger,**
  because agents (implementer/deployer) commit + deploy code and publish
  content to prod. Detect via the site repo HEAD sha
  (`git -C <cwd/repo> rev-parse HEAD`) vs `last_tested_sha` in state, and/or
  new `editorial_articles` since the last run. On a change → run `smoke_cmd`
  now (fast) and mark a full run due.
- **At least daily:** if `now - last_full_test_run > run_every_hours`, run
  `full_cmd`.
- Otherwise skip.

**How:** run the cmd in `functional_tests.cwd` against `base_url` (prod).
Use `run_in_background: true` for the full suite (minutes) and read the
result when it completes. Parse the `--reporter=line` tail for
`N passed / M failed / K flaky`.

**On PASS (all green, or only flaky):** record `{ts, sha, passed, flaky}`
in state; add `TESTS <n> passed` to the tick box. Cheap, done.

**On real FAILURE (non-flaky) — a functionality regression, Layer B engages:**
1. Read the failing test name(s) + error (≤2 samples). Classify:
   - **Real prod bug** (SSR/hydration broke, a route 500s, a CTA/price/
     structured-data wrong): PATCH it. Find the cause (often a recent AGENT
     commit or a data issue), make the smallest fix in the correct repo,
     commit main-first, and RE-RUN the failing test to confirm green. If an
     agent's prod change caused it, fix the agent too so it can't recur.
   - **Stale / environment-specific test** (asserts local-only data or an
     intended prod difference): fix or `@fixme` the test with a why-note.
   - **Can't safely fix** (needs a deploy/decision/credential): ESCALATE
     (§6) with the failing test + error, de-duped.
2. Record the failure + resolution in state.

**Shop buy-box spec (when the config has `conversion:`).** The daily prod
@smoke run includes a prod-safe "shop buy box" spec at 390x844 and 1366x768,
sending the probe header and blocking the click-redirect path, amazon.com and
analytics. It asserts:
- the converting guide's first Amazon button is within the first 700 px on mobile;
- the primary button is Amazon on a product page that also has another retailer;
- the sticky buy button is not covered (`elementFromPoint`);
- an out-of-stock product page shows a priced alternative.

The structural assertions also belong in the site's gate suite. Fresh-price
assertions run in prod only, because gate fixtures have stale prices.

**Coverage-depth mandate (improvement-cycle lever):** the suite must cover
ALL functionality in depth. On an improvement cycle — or whenever a new
feature/route ships without a test, or you just patched a bug — audit
`coverage_areas` vs the live site and ADD tests for the gaps (new routes,
components, structured data, affiliate flows, and a regression test for any
bug patched). Commit new tests main-first in the site repo and bump
`total_tests`. **A patched bug without a new regression test is not done.**

**Token note:** a daily green run costs a shell invocation + a few tokens to
parse/report. Tokens are spent only when a test actually fails or during a
coverage audit — consistent with the token-discipline model. State keys:
`last_full_test_run`, `last_smoke_run`, `last_tested_sha`, and a
`functional_tests` pass/fail history in the incident file.

---

---

## 5c. Catalog health — monetisation rots silently (every tick, pure SQL)

If the config has a `catalog_health:` block, run its `checks:` every tick.
They are plain `SELECT count(*)` probes — Layer A cost, no reasoning — and
they guard the half of the North Star that the publish pulse cannot see:
**the site can be publishing perfectly while every buy-link is stale or
broken.**

Why this exists (2026-08-14): all 65,886 aisleprompt Amazon listings were
**four months stale** — the HTML scrape path had begun getting bot pages
from Amazon and 403s from eBay, and the agent kept exiting 0 the whole
time. Separately, 16,228 active specpicks eBay products (`asin` like
`EBAY_%`) had no `listing_preference`, so render-time routing fell through
to an Amazon CTA at `amazon.com/dp/EBAY_<id>` — a dead page, $0 commission,
and Offer structured data Google can flag as deceptive. **Neither showed up
as a failed unit.** Agents reporting success is not evidence the catalog is
sellable.

Evaluate each check against its bound:
- `expect_max: 0` breached → a correctness bug reaching users. Apply the
  check's `note:` auto-fix if it names one (they are idempotent UPDATEs),
  re-run the check to confirm 0, and report it.
- `expect_min: 1` breached → that refresh path is DEAD. Do not just note
  it: find the owning agent and read why (usually a missing credential, a
  blocked scrape, or a provider with no configured fallback).
- No bound (a backlog gauge like `*_stale_30d` / `*_never_priced`) → record
  the number and compare with the previous tick. **A flat backlog is a
  finding**: throughput is at or below catalog growth, so the cap
  (`KITCHEN_CREATORS_REFRESH_PER_RUN`, `max_refresh_per_run`) is too low to
  ever converge. Raising a cap is a safe tweak; raising it past what the
  provider will rate-limit is not, so move it and watch one cycle.

Add a `CATALOG` line to the tick box with the breaches, or `ok` when all
bounds hold. When a NEW class of catalog rot is found, add a check to the
site's config (and `systems/_example.yaml`) rather than a branch here — the
runbook stays generic, the config carries the site's specifics.

## 5d. Site consistency — the outside check (every tick, pure shell)

Agents write to these sites all day and each one validates only its own narrow
change. Nothing checked the RESULT the way a reader sees it, and that gap was
expensive: the article "Ah Yes, 'Gaming' Laptops: The Hype vs. Reality" ran a
gaming MOUSE product photo as its hero for MONTHS.

`site-consistency-audit.timer` (05:40 + 17:40 daily) crawls BOTH sites
breadth-first to depth 5 and writes:

    ~/.reusable-agents/site-consistency-audit/site-audit-specpicks.json
    ~/.reusable-agents/site-consistency-audit/site-audit-aisleprompt.json

**Not /tmp.** /tmp on this host is tmpfs (RAM-backed) and is wiped on every
boot. Reports AND the regression baseline lived there until 2026-09-12, when a
reboot silently erased the baseline — the next run would have reported
"baseline stored" instead of a regression. A monitor that forgets is not a
monitor. If you ever see "(baseline stored)" on a site that has run before,
suspect lost state before you believe the site is clean.

**READ THE `diff` BLOCK FIRST — it is the whole point.** The absolute finding
count is mostly noise (every site has a tail of known warts). What matters is
what CHANGED since the agents last shipped:

```bash
for s in specpicks aisleprompt; do
  python3 -c "
import json,os
d=json.load(open(os.path.expanduser('~/.reusable-agents/site-consistency-audit/site-audit-$s.json')))
f=d['diff']; sev=d['summary']['by_severity']
print('$s', d['pages_crawled'],'pages', sev, 'REGRESSION' if f['is_regression'] else 'stable')
if f['new_kinds']: print('   NEW:', ', '.join(f['new_kinds']))
if f['regressed']: print('   WORSE:', f['regressed'])
if f['improved']:  print('   BETTER:', f['improved'])
"
done
```

`is_regression: true` right after a deploy or a big agent batch means the fleet
just shipped something bad. Correlate with the newest `dispatch-implementer-*.log`
before assuming it is unrelated. Add a `CONSISTENCY` line to the tick box:
`<n> error · <n> warn · <REGRESSION|stable>`.

### What the checks mean, grouped by what they protect

**INTEGRITY — is it broken?**
- `thin-or-shell` / `no-h1` / `no-title` — a 200 rendering almost nothing.
  WORSE than a 404: Google indexes it, a reader sees an empty page, and no
  status-code monitor notices. Treat as DEGRADED and trace the route.
- `broken-image` / `tiny-image` — dead or 7KB hero. Names the page that renders
  it (`on_page`). Trigger the site's `*-article-hero-image-curator`.
- `invalid-jsonld` — rich results silently stop. Trace the recent commit.
- `placeholder-text` / `prompt-leak` — generator exhaust reached a reader. Fix
  the row AND the generator's ban list.

**RETENTION — will they read a second page?**
- `dead-end-page` — fewer than N internal links out; the session ends here.
  Hand to `internal-link-densifier`.
- `orphan-page` — nothing links to it; reachable only from the sitemap.
- `internal-link-to-redirect` — every hop costs crawl budget, leaks link equity
  and slows the reader. Carries `linked_from` so the source is fixable.
- `no-cta-on-monetized-page` — a product/buying-guide page with no buy link
  earns nothing and frustrates purchase intent. ERROR: it is lost revenue.

**ACQUISITION — will anyone arrive?**
- `no-meta-description` / `duplicate-meta-description` — the meta description
  IS the SERP snippet. Missing hands Google a random paragraph; duplicated
  makes two results look identical. Direct click-through loss.
- `duplicate-title` / `duplicate-h1` — cannibalisation; the pages compete with
  each other. Hand to the seo-opportunity-agent.
- `long-title` — truncated in results, so the differentiator is cut off.
- `sitemap-url-404` / `sitemap-url-redirects` — the sitemaps are AGENT-GENERATED,
  so nobody was checking them against the live site. A sitemap full of 404s
  burns crawl budget on every Googlebot visit.
- `stale-year-claim` — "Best X of 2026" whose dateModified is years old.

**TRUST — does it look professional and honest?**
- `fabricated-rating` — JSON-LD claims N reviews the page cannot show. Precedent:
  aisleprompt shipped "rec-010: null 17 fabricated AggregateRating counts".
  This is a Google manual-action risk, hence ERROR.
- `price-mismatch` — schema price nowhere near the displayed price (2% band, so
  rounding is fine). Usually a stale buy box.
- `locality-leak-title` / `known-leak-token` — prompt scenery in a title
  ("Mountain Home, Arkansas"). A BARE city name needs a gazetteer, so once the
  PI agent strips one, add it via `--leak-token` in the service unit.
- `no-byline` / `no-visible-date` — editorial without attribution reads as
  machine output (E-E-A-T).
- `text-baked-into-hero` — marketing badges burned into hero pixels. OFF by
  default (`--vision-hero-check`): it costs money per image via the Azure
  `vision-mini` deployment, whose TPM quota was deliberately cut. There is no
  OCR on this host and no sudo to install tesseract, so a pixel heuristic would
  be guesswork — this asks the same vision model the recipe-image-verifier uses.

**The auditor NEVER repairs anything.** Read-only by design so it can never
itself become a source of damage. Fixes go through the owning agent, or you.

---

## 5e. Email volume — routine status does NOT go to the inbox

Decided with the operator 2026-09-11 after run-summary mail hit ~2,400/day,
~80% of it no-ops ("no producer had unshipped recs", "short-circuited",
"No unverified rows", "throttled: N scope(s) in flight"). Burying a real
failure under 2,000 non-events is the practical equivalent of not sending it.

The contract now:

- **Email** = failures, shipped recs, and recommendations awaiting a reply.
- **Dashboard / API** = routine per-run status. It was always recorded there;
  email was duplicating it.
    GET /api/agents             — all 83 agents, status, last run
    GET /api/agents/<id>/status
    GET /api/runs               — run history
  (framework API, `Authorization: Bearer $FRAMEWORK_API_TOKEN`;
   nsc-dashboard is the UI, minReplicas=0 so first hit cold-starts.)

`AgentBase._maybe_send_run_summary_email` enforces it: a SUCCESSFUL run that
sets `result.short_circuited` or whose summary matches a no-op phrase sends
nothing. **Failures are never suppressed.** An agent that genuinely must report
every run sets `always_email_run_summary = True`.

If email volume climbs again, check that first — and do NOT reach for
`DIGEST_DISABLED=1`. That kill switch drops mail whose caller did not pass
`bypass_digest=True`, which silently breaks the escalation path (see the mail
transport note below). Fix what is emitting, not the transport.

---

## 5f. Conversion & shop health — watch the money, then adjust (every tick)

If the config has a `conversion:` block, the money path is part of keeping the
lights on. A site can serve 200s, publish on schedule and hold every catalog
bound while its buy buttons stop earning. On 2026-11-11 one reference site's
tick was GREEN while its verified-human Amazon clicks were down 42% week over
week, its converting guides were down 56%, and every Amazon-click goal read
"accomplished". Nothing in §2–§5e looked at clicks.

**Layer A (pure code; the host-local sweep `~/.reusable-agents/ktlo-work/sweep.py`
emits it per system).** All of it is read-only: SELECT-only sessions, storage
reads, read-only git. The only write is the cache at `conversion.watch.cache`,
which lives under `~/.reusable-agents/ktlo-work/` and never in /tmp.

1. **Verified-human clicks.** Count them with `framework/core/human_clicks.py`,
   using the spec and profile in `conversion.clicks`. These must be the same
   ones the site's goal tracker uses. The profile overrides come from storage
   `config/human-click-filter-config.json`, so KTLO, the goals and the
   strategist all count the same thing.
   - Every tick: totals d1, d7, prev7, d30, plus the non-human clicks still
     forwarded with the affiliate tag (`forwarded_24h`).
   - Each full pass (`watch.full_every_minutes`, cached): counts per template
     (`conversion.templates`, first regex wins) and per money placement
     (`conversion.money_placements`). The full pass also builds the **money
     set**: pages and products with at least `money_set.min_clicks` verified
     clicks. Catalog checks bind it as `params: [money_products | money_pages |
     money_slugs]`.
   - Never rank, justify or monitor money work with a weaker click view, raw
     GA4 sessions or GSC clicks. `conversion.definition_drift` lists the weaker
     views agents still read, and names those readers.
2. **Placement watch.** `framework/core/placement_watch.py` runs after every
   gated deploy and daily from the goal tracker. It opens one incident per money
   placement whose verified clicks fell after a deploy, in storage
   `framework/placement-watch/<system>.json` (`open` and `last_run`). The sweep
   maps each open incident back to a template through
   `money_placements.<name>.placement_watch`. The watch's own config is
   `config/placement-watch-config.json`; list it in `conversion.config_reads`
   so an archived doc is caught (§11.I).
3. **Goals.** For each `conversion.goals` entry, read that agent's goals
   (`GET /api/agents/<id>/goals`, or storage `agents/<id>/goals/active.json`).
   Compare the recorded value with the live count named by `live`. The sweep
   flags `value-drift` (more than 25% apart), `accomplished-not-steering`
   (an accomplished goal steers nothing, and doubly so when the live value is
   below target), and an archived or missing doc. The owners' `goals:` lists in
   `conversion.owners` are the goals each fix must move.
4. **Shop correctness.** The money-set-bound checks in `catalog_health.checks`
   (picks on converting guides that point at inactive or unpriced products,
   priced share of those picks, stale prices on clicked products, the trending
   rail's freshness, and so on), plus `catalog_health.agent_metric_checks`. The
   latter read an owner's own `RunResult.metrics` from its `run-index.json`
   (`sustain_runs`, `max_age_hours`), so KTLO reads what agents already measure
   instead of re-deriving it.
5. **Money-page probe.** Fetch the top `probe_money_pages.top` money pages the
   way a reader would, sending the probe header, and count CTA links and
   search-fallback CTAs. Fetch SSR pages only. Never fetch an API that bumps a
   counter (for example `/api/blog/:slug`). Never fetch a click-redirect path
   (`/k/`, `/r/`): each request logs a click and forwards a tagged redirect.

**DEGRADED when any of these holds:**
- a `floors` breach (these floors hold even while a goal reads accomplished);
- a drop on `watch.sustain_passes` consecutive full passes (`drops_sustained`);
- a post-deploy regression, or an open placement_watch incident;
- a breached check that has an `owner_class`, or a breached agent-metric check;
- a required `config_reads` doc that is not ok;
- a bad money-page probe;
- suspect share above `watch.max_suspect_share_pct`;
- a `definition_drift` breach.

"The site serves, but the money leaks" counts as DEGRADED.

**Route each breach to its OWNER, never to yourself.** The sweep's `routes`
list maps each breach to a failure class, and each class to
`conversion.owners.<class>`: `{agent, action, rec_kind?, knobs?, goals?}`.
Make at most ONE adjustment per class per pass, and use only the owner's
declared `action`:

| action | What KTLO does |
|---|---|
| `trigger` | Start the owner once (`systemctl --user start agent-<id>.service`, or `POST /api/agents/<id>/trigger`). Then read its next run. |
| `knob` | Read the owner's own throttle and quota metrics first. Then move ONE named knob ONE step and trigger once. Never move a knob outside `knobs`. |
| `rec` | Dispatch ONE rec of `rec_kind` that names the owner. Write a `recommendations.json` whose top-level `agent_id` is the owner, so the kind and priority mapping match, in a KTLO run dir under `~/.reusable-agents/ktlo-work/dispatch/<system>/<run_ts>/`, then call `framework.core.dispatch.dispatch_now`. The deploy-gate hold applies; never bypass it. DB-only kinds (`article-refresh`, `catalog-audit`) are not blocked by a red gate. |
| `escalate` | Email once per signature (§6 de-dupe), then keep watching. |

Class-specific rules:
- **measurement.** Rewrite an archived config from its last known content
  (§11.I). Re-run the tracker once on value-drift. Re-open a goal whose live
  value fell below target. Never change a target or the click filter without
  escalating, unless the config already marks the template `suspect`.
- **pick-dead.** Read the shelf audit's answer-picks output. File one
  article-refresh rec per page. The remedy order is: relink, then swap to an
  in-stock product the page reviews, then unlink last. Escalate only after two
  implementer deferrals.
- **price-stale.** Read the scraper's or verifier's throttle metrics, then move
  one knob step and trigger once.
- **template-drop or placement-drop.** Read `post_deploy` and placement_watch
  first.
  - If a deploy caused it: pause money-surface code kinds in storage
    `config/implementer-allowed-dispatch-kinds.json` (record `previous` and a
    reason in `_history`), dispatch a revert rec naming the commit, and escalate
    with the evidence.
  - Otherwise, split the drop. Sessions down means traffic-led
    (`owners.traffic-drop`). Clicks per session down means conversion-led
    (`owners.conversion-drop`).
- **rail or money-page-gone.** File a pi rec; it needs a green gate. Escalate
  if the gate has been red for more than 24 h.
- **forwarded-bots.** Fix our own crawler through its owner. Escalate the
  redirect handler once. Never block traffic or change affiliate tagging as an
  auto-fix.

**Close the loop.** Record each adjustment and its before-value in
`improvements.json`. Re-check after the owner's next two runs, or three full
passes. Revert any knob that did not move its metric. "A change that doesn't
move a goal didn't happen" applies to KTLO's own nudges too.

### Staged rollout of buy-box recs (`shop_rollout:` in the config)

Buy-box and money-surface changes ship ONE AT A TIME PER TEMPLATE, so
placement_watch and lever outcomes can attribute each one. In 2026-11
agents stacked 180 commits on one reference site's build, 61 of them on price,
CTA or hero surfaces, and nothing could say which one cost clicks.

- **State.** `agents/keep-the-lights-on/<system>/shop-rollout.json` (or
  `shop_rollout.state_key`) holds an ordered `queue`. Each item has `id`,
  `templates`, `owner_agent`, `dispatch_kind`, `depends_on` (queue ids),
  `external_prereqs` (`{id, done, done_when}`), `requires_gate_green`,
  `not_before`, `deadline`, `status`, and the `rec` body.
- **Status flow.** `queued` → `dispatched` (`rec_id`, `request_id`,
  `dispatched_at`) → `shipped` (`commit`) → `deployed` (`deployed_at`,
  `deploy_tag`) → `verified` or `reverted`. An item can also be `skipped` or
  `expired`. Append every transition to the item's `history`.
- **Eligibility.** The sweep's `shop_rollout` block computes it; the tick
  dispatches and writes the state. An item is eligible only when ALL of these
  hold:
  - for every template it touches:
    - no other change to that template deployed in the last
      `min_hours_between_template_deploys` (default 48). The sweep checks this
      by read-only `git diff` of each recent deploy (the deployer's gate-state
      deploy log) over `shop_rollout.templates.<t>.paths`, plus rollout items
      `deployed_at`.
    - no undeployed change to it on HEAD. It would share the next deploy:
      **never two changes to one template in one deploy**
      (`max_changes_per_template_per_deploy: 1`).
    - With `other_template_changes: confounder`, the two checks above count only
      rollout items. Other agents' deploys and commits on that template are listed
      under `confounders` instead of blocking. Use it on a site where other agents
      touch money templates daily: on 2026-11-12 four other deploys to one guide
      template left 0 of 12 items eligible. The default, `block`, counts every
      change.
    - no rollout item for it in flight.
    - no open placement_watch incident on a money placement of that template.
  - Its `depends_on` items are deployed.
  - Its `external_prereqs` are done. Flip `done: true` only after checking
    `done_when` yourself.
  - The gate is green (`require_gate_green`).
  - It is inside `not_before` / `deadline`.
  - No `shop_rollout.freeze` window (`{start, end, reason}`) is active.
    Freezes are operator-approved; put them in the site config, never in the
    state.
- **Per tick.** Dispatch at most `max_dispatch_per_tick` items (`dispatch_now`),
  one per template, through the owner, as in the `rec` row above.
- **After deploy.** Set `deployed_at` from the gate-state deploy log. After
  placement_watch has judged that deploy (24–48 h), mark the item `verified` if
  its placements read ok, or file the revert and mark it `reverted` if they
  regressed.
- **Deadlines.** An item that reaches `deadline_risk` (72 h before its deadline)
  while still queued behind its lane: escalate once so the operator can reorder.
  Never reorder the queue yourself.
- **Shared files.** Paths shared by every template (a monolith server or SSR
  renderer) are deliberately not template paths. The item's own deploy still
  blocks its template for the window.

**Box lines (§7).**
- `CONVERSION d7=<n> (prev7 <n>, <±%>) d30=<n> · <top money placement> d7=<n> · floors <ok|list> · drops <none|list> · post-deploy <tag ok|regressed>`
- `SHOP picks-dead=<n> · money-priced=<pct>% · probe <ok|path:n fallbacks> · rail <f>/8 fresh <d>/8 distinct · fwd-bots 24h=<n> · goals <ok|n flagged> · config <ok|ARCHIVED> · watch <0|n open>`
- `ROLLOUT <next id → template | waiting: reason> · in-flight <ids> · freeze <none|until>`

Onboard a new site by config alone: copy the commented `conversion:`,
`catalog_health.checks[].params` / `owner_class`, `agent_metric_checks` and
`shop_rollout:` blocks from `systems/_example.yaml`. If a needed knob is
missing, add it there and to the sweep, never as a branch in this runbook.

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

**Mail transport status (CORRECTED 2026-09-08 — the previous two notes were both
wrong).** Mail WORKS. `send_via_msmtp` is a misnomer: it tries **Microsoft Graph
`/sendMail` first**, minting a token from
`~/.reusable-agents/responder/.oauth.json` (scope `Mail.Send`); msmtp is only the
fallback and is sandboxed by AppArmor anyway. Verified 2026-09-08: the token mints,
and `digest-rollup-agent` delivered with `digest sent (graph:send_as)`.

What was actually broken was **`DIGEST_DISABLED=1`** in `secrets.env` (set
2026-08-14 on the mistaken belief that no transport existed). It made
`_maybe_queue_digest` DROP every message whose caller did not pass
`bypass_digest=True`. That is why some agent mail kept arriving while the rest
vanished — the senders split cleanly:

- **Passed `bypass_digest=True`, kept working the whole time:**
  `competitor-research-agent`, `app-store-opportunity-agent`, `authority-agent`,
  `digest-rollup-agent`.
- **Did not, so were silently dropped:** `agent_base` (the generic path most
  agents use), `catalog-audit-agent`, `agent-doctor`, `progressive-improvement-agent`,
  `product-hydration-agent`, `goals-tracker`, `ebay-product-sync-agent`, and
  `framework/core/resilience.py` — i.e. the fleet's ERROR ALERTS.

`DIGEST_DISABLED` is now unset (commented in `secrets.env` with the reasoning) and
`resilience.py` now passes `bypass_digest=True` so alerts can never be swallowed
again. Routine agent mail queues to `digest-queue/` and ships as ONE consolidated
email when `agent-digest-rollup-agent.timer` fires (every 5h, `0/5:16:00`).

So you MAY now claim an email was sent — but only when `detail` says `graph:…` or
`sent to`. Treat `ok=True` as delivery ONLY if `detail` does not start with
`digest disabled` or `suppressed:`.

**If the email send fails** (`ok is False` — e.g. the Graph creds are
missing or the O365 relay is down): do NOT silently drop it. Notify
in-session that email delivery itself is down, record the incident, and
treat "operator escalation channel is broken" as its own top blocker to
surface — because a manager who can't reach the operator is the worst
failure mode.

---

## 7. Output format (every tick)

Bordered ASCII (the operator prefers bordered tables over markdown pipes).
No prose padding. The **per-agent table is REQUIRED every tick** — the
operator wants to see, for EACH agent in scope, its health + what it last
did + when. List every agent from the config `agents` block (critical
first, then supporting, then shared infra), one row each. Health glyphs:
`idle` = healthy oneshot between crons, `run` = active/activating,
`FAILED` = failed (flag it), append `*` if the failure is in
`known_excluded`. Keep LAST MAJOR WORK to the log's final-status summary,
truncated to fit.

```
+--------------------------------------------------------------------------------------------+
| KTLO <system> — TICK <n> · <HH:MM>Z (<YYYY-MM-DD>) · <GREEN|DEGRADED|DOWN|BLOCKED>          |
+--------------------------------------------------------------------------------------------+
| AGENT                                  HEALTH   LAST RUN (UTC)    LAST MAJOR WORK            |
| <id-1>                                 idle     MM-DD HH:MM       <final status summary>     |
| <id-2>                                 run      MM-DD HH:MM       <final status summary>     |
| <id-3>                                 FAILED*  MM-DD HH:MM       <error summary> (excluded) |
| … one row PER agent in scope (critical → supporting → shared) …                             |
+--------------------------------------------------------------------------------------------+
| SITE     homepage <code> · sample <code>                                                   |
| VOLUME   <site>: +1h=<n> +24h=<N>  (growth-metric note if known)                            |
| CATALOG  <ok | check-name=<value> breached bound, action taken>                             |
| CONVERSION d7=<n> (prev7 <n>, <±%>) d30=<n> · <top placement> d7=<n> · floors · drops · post-deploy |
| SHOP     picks-dead · money-priced% · probe · rail fresh/distinct · fwd-bots · goals · config · watch |
| ROLLOUT  <next id → template | waiting: reason> · in-flight <ids> · freeze <none|until>          |
| POOL     <k>/<total> profiles alive                                                        |
| ACTION   <auto-fix taken | improvement shipped | none>                                     |
| ESCALATE <email sent to <owner> / none> <in-session note>                                  |
| STANDING <open operator-gated items, 1 line>                                               |
+--------------------------------------------------------------------------------------------+
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
trail of shipped changes + metrics, including every §5f adjustment and its
before-value), `last_tick.json` (last cadence / class / improvement-window
time), and `shop-rollout.json` (the §5f staged-rollout queue and its
transitions). A resumed session reads these first so
it never double-alerts or repeats an improvement. This is what lets the
skill genuinely "keep the lights on" across restarts, not just within one
session.

---

## 11. Incident playbook library (known failure modes + the exact fix)

These are recurring, hard-won incidents. When you see the signature, apply
the linked fix instead of re-diagnosing from scratch. Full narrative + the
current live status of each is in `docs/keep-the-lights-on.md` and the
per-incident memories; this is the fast-path index.

**A. Disk-full / ENOSPC fleet crash (host 2026-5090).** Signature: **many
agents (>5) failed at once**, logs show `[Errno 28] No space left on device`.
Root cause has been a **deleted-but-open leak** — `df` shows far more used
than `du -x /` can see (a root process holding unlinked files open), which
fills `/` and ENOSPC-kills the whole fleet + can zombie the retro-chat
daemon. Playbook:
1. **`df -h /` FIRST** on any mass-failure. If >90%, this is the cause.
2. Find the grower: `du -xhd1 / | sort -h | tail`. If `df used` ≫ `du /`
   total, it's the deleted-open leak → needs sudo `lsof +L1 | sort -k7 -h |
   tail` to find the holder, then restart that process (or reboot) — an
   OPERATOR action (agent has no sudo) → escalate with the exact commands.
3. The **standing preventive fix** is deleting the 265G *stale* ollama
   duplicate at `/usr/share/ollama/.ollama/models` (the active ollama runs
   from `/data/ollama` via `OLLAMA_MODELS` override — verify, then it's safe:
   `sudo rm -rf /usr/share/ollama/.ollama/models`). With that gone the base
   footprint is low enough that even a full leak can't refill `/`.
4. Once the disk is freed (operator acted / `df` dropped): **bulk-reset the
   stale failed units** — `systemctl --user list-units --state=failed
   --no-legend | grep -oE 'agent-[a-z0-9-]+\.service' | xargs -r systemctl
   --user reset-failed` — they were ENOSPC-stale and succeed again on their
   timers. Restart any zombied daemon (retro-chat). Mark the incident
   resolved.

**B. claude-pool all profiles dead (authoring blocked).** Signature: pool
`state.json` shows `0/6 authenticated`, profiles say `"OAuth session
expired"`; **publish volume goes to 0 and won't recover** (all Opus authors
defer). This is operator-interactive re-auth (like a credential). Per
profile: `HOME=/home/voidsstr/.reusable-agents/claude-pool/profile-<N>
/home/voidsstr/.local/bin/claude /login` (regenerate the exact list with
`python3 -m framework.cli.claude_pool login-help`). Verify with the authed
count in `state.json`. The pool auto-picks up newly-authed profiles on the
next drain tick — no restart needed. **One dead profile with others alive is
NOT this incident** (the pool rotates; §4.5). Escalate once if 0/6 persists.

**C. GSC OAuth token expired (SEO agents fail ~weekly).** Signature:
`*-seo-opportunity-agent` (+ `gsc-coverage-auditor`) fail; log shows
`refresh-token.py … HTTP Error 400`. It's the shared Google test-tier
`refresh_token` expiring ~every 7 days. EXPECTED + recurring: **reset the
failed unit each tick, do NOT re-email** past the first escalation. Fix is
operator-interactive: `bash install/fix-gsc-now.sh` (browser consent). Durable
fix: publish the OAuth consent screen to "In production" so it stops expiring.
Renewal check: `python3 agents/seo-opportunity-agent/lib/collector/refresh-token.py
--oauth-file ~/.reusable-agents/seo/.oauth.json` → a `ya29…` token (not
HTTP 400) = renewed → mark resolved.

**D. Red deploy gate (any site; replaces the old `:4001` playbook).** Since
2026-10-19 the deployer repairs its own red gate (`agents/deployer/gate_runner.py`,
`framework/core/deploy_repair.py`). Read it before acting:
`~/.reusable-agents/deployer/gate-state/<site>.json` holds `status`,
`signature` (the newest red), `last_reason`, `last_failures`, `red_since`, and
`failures.<signature>` with that failure's `kind`, `cycles` and `next_repair_at`
(backoff is per signature). The
newest dispatch log shows `[deployer:gate]` / `TEST GATE RED` lines. The run
dir's `test-failure-context.json` names the artifact dirs
(`~/.reusable-agents/deployer/gate-artifacts/<site>/…`: `report.json`,
`output.log`, `container.log`, each repair's `session.log`). Cases:
- `red`, `code`, and that signature's `cycles` is 0 or its `next_repair_at` is
  in the past → the loop will retry on the next deploy. Note it. Do nothing.
  "deferred, the claude-pool has no capacity" is the same: it retries in 15 min.
- `red`, `code`, and backing off after a spent cycle → the operator has been
  emailed (once per signature). Look at the failing test and the repair session
  logs. Fix by hand only if the fix is obvious and small (then run
  `tests/run-local-suite.sh` and commit). Otherwise wait for the backoff.
- `infra` (suite exit 2/4/6/7, Docker, lock wait) → nothing was sent to an LLM.
  Check `docker ps`, disk, `/tmp/agent-framework-locks/local-suite-<site>.lock`
  and `deploy-<app>.lock` holders.
- "repair is green but could not land" → cherry-pick
  `refs/deploy-repair/<site>/<tag>` by hand.
Never blind-revert, and never weaken a test to get green. Kill switch: storage
`config/deploy-repair-config.json` `{"enabled": false}` (the gate still blocks).
aisleprompt's gate no longer uses the standing `:4001` dev container: both
sites run `tests/run-local-suite.sh`, which builds the production image from the
deployer's clean checkout, under a host-wide flock.

**E. kitchen-scraper 2400s timeout (flaky).** Signature:
`agent-aisleprompt-kitchen-scraper` fails with `scraper timed out after
2400s`, parent at ~0.2% CPU (I/O-blocked on an external product fetch, not
ollama). FIXED by commit 9fb1a07 (a run-level wall-clock deadline
`KITCHEN_RUN_DEADLINE_S=1500` that stops new work before the wrapper SIGKILL →
clean partial-success). If it recurs at 2400s, the deadline regressed — check
the env + that `main.py` on disk still has the fix; otherwise note-only.

**F. Transient single-run failures.** A single failed run of an otherwise-
healthy agent that is I/O-shaped — one Azure-PG connection timeout
(`ebay-product-sync`), one homepage `000`, one `"claude returned 0
proposals"` — is a transient: `reset-failed` (or re-probe 2-3×) and note.
Escalate/investigate only on the **2nd consecutive** occurrence.

**G. Site boot migration runner dead (agent fixes silently never ship).**
Signature: agents keep committing `db/migrations/*.sql`, the site keeps
looking unfixed, and `SELECT count(*) FROM _migration_failures` is 0 while
far fewer rows exist in `_migrations` than files on disk. Container logs show
`[DB Init] Attempt N/30 failed: current transaction is aborted`. Cause
(aisleprompt, 2026-07-02 → 09-25, 816 files): a migration that failed inside
its own `BEGIN` left the connection aborted, so initDB died on every boot and
nothing after the loop (cache warmers) ran either. Fixed in the runner (COMMIT
after success, ROLLBACK before recording a failure, 15-min session timeout).
If it recurs: diff files vs `_migrations`, dry-run the pending set
cumulatively inside ONE rolled-back transaction, review before letting a
backlog auto-apply (old agent migrations can undo newer fixes), quarantine
into `db/migrations/_quarantine/` with a reason, and apply multi-minute
files by hand so the boot doesn't carry them.

**H. Host GPU crash (MCE panic / Xid 79 / instant power-off) and the
wedged-card boot.** Signature: `uptime` is minutes, `boots_24h` > 1 in the
sweep, publish volume and catalog freshness dip. The RTX 5090 host crashes under
agent GPU load even at its 400 W power floor (09-28: Xid 79 at 10:59, MCE panic
at 15:48). The dangerous follow-on: `kernel.panic=30` auto-reboots WARM, the card
can come back wedged, and the kernel then logs an IOMMU fault storm
(`gpu_iommu_faults_this_boot` > 0 in the sweep) while NOTHING above the kernel
runs: no agents, and not this KTLO loop either, since it runs on the same host.
09-28 lost 7 h that way. Playbook: if `gpu_iommu_faults_this_boot` > 0, the card
needs a **COLD power cycle** (power off, wait, power on; a warm reboot does not
revive it). That is an operator action, so escalate with the fault count. After
any unexpected reboot, re-arm the loop, read `/var/crash/<ts>/dmesg.*` (sudo) for
the panic line, and append the event to
`retro-agent/docs/host-issues-log.md`, whose rules require an entry per host
event. The history, signatures 1–6 and the open mitigations live in that log.

**The quiet variant: the card dies and the host stays up.** Signature: the sweep's
`gpu_ok` is `DOWN` or `gpu_xid_this_boot` > 0 while `uptime` is normal and
`gpu_iommu_faults_this_boot` is 0. An Xid 79 ("GPU has fallen off the bus") in
the middle of a boot leaves the box running on the iGPU, so no reboot or IOMMU
storm marks it. On 2026-11-11 the card died 5 h after a cold cycle and went 17 h
before a tick noticed. The remedy is the same cold power cycle (operator). Until
then, local image generation fails with CUDA errors and ollama runs on the CPU.
Escalate when that starts to cost the sites: the recipe-image refiller's backlog
grows, or a hero curator reports articles it could not fill. Do not escalate
while both read clean. Check the host log first. Another session may already
have recorded the event.

**The reboot restarts every timer you stopped, and the dispatch-kind allowlist
does not hold deploys.** It is enforced only in the backlog-dispatcher. Producers
that call `dispatch_now` themselves (`*-user-growth-strategist`,
`*-shelf-audit-agent`, catalog-audit, h2h, article-proposal) bypass it. So if a
recovery step must happen before the next deploy, stop those producers' timers
FIRST, the moment the box is back, and restart them only after the step is done.
2026-10-11: the aisleprompt growth strategist restarted with the boot, dispatched
a code rec 1 minute later and deployed at 12:47. The new image's boot applied
pending catalog-audit `recipe-image-present` migrations before the image refiller
had caught up, deactivating 71 recipes and pushing 5 cuisine hubs back to
noindex. Also check `REFILL_INCLUDE_INACTIVE`: the refiller's timer does NOT
fill inactive rows. Repair by pausing the timer, running
`REFILL_INCLUDE_INACTIVE=1 REFILL_TIME_BUDGET_S=240 bash run.sh` in passes (the
wrapper kills the script at 280 s), then reactivating only rows that now have an
image.

**I. Storage doc archived by the 120-day lifecycle rule.** Signature: the
sweep's `config_reads` (or a goal check) shows `ARCHIVED-or-unreadable`, or a
doc reads null while `exists()` is true; the log shows `BlobArchived`. Effect:
readers such as `human_clicks.load_config()` and `placement_watch.load_config()`
return `{}` and silently fall back to defaults. If the click filter is
archived, goal counts jump about 5x; if the placement-watch config is archived,
nothing is watched. Fix: rewrite the doc from its last known content (git, the
`improvements.json` trail, or its `_note`/`_history`), re-read it to confirm,
and escalate the lifecycle exemption once (operator: storage lifecycle policy).
Seen 2026-11-11 on `config/priority-config.json`, a conversion optimizer's
goals doc and a goal tracker's run-index.

When a NEW recurring incident is diagnosed and fixed, ADD it here so the
next session gets the fast path.
- `.claude/skills/refresh-gsc-token/SKILL.md` — re-mint the shared GSC/GA4 OAuth
  token when the collector fails with `token mint failed` / `invalid_grant`, or
  when sitemap pings report `ok=8/15` (readonly scope).
