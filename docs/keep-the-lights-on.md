# Keep the lights on (KTLO) — operator + on-call runbook

**What it is.** An always-on "manager / on-call SRE" role, implemented as the
`keep-the-lights-on` Claude skill, that keeps a set of production systems
running, auto-fixes known failures, nudges the agents toward their declared
goals + a growth North Star, and escalates to the operator by email + in
the session when genuinely blocked. It is **token-frugal by design**: cheap
pure-shell/SQL health checks every tick, LLM reasoning only when something
breaks or on a slow improvement cadence. It self-perpetuates via
`ScheduleWakeup` and survives restarts via state in framework storage.

This page is the human-facing overview. The **operational runbook the skill
follows every tick** is `.claude/skills/keep-the-lights-on/SKILL.md` — read
that for the tick mechanics, health classes, output format, and the full
incident-playbook library (§11). This page adds: how to start it, the live
system roster, the re-auth runbooks, and the agent-optimization-toward-goals
workflow with worked examples.

---

## Starting a KTLO session (new session, from scratch)

1. Invoke the skill: **`/keep-the-lights-on`**, or just tell Claude
   *"keep the lights on for aisleprompt, specpicks and the retro-chat agent."*
2. It loads each system's config (via the registry, below), runs the cheap
   tick, emits one status box per system, and calls `ScheduleWakeup` to keep
   the loop alive. You don't need to do anything else — it re-invokes itself.
3. To stop: *"stop keeping the lights on" / "stand down" / "pause the loop."*

The loop is self-sustaining: each wakeup re-runs the same tick and reschedules.
State (open incidents, shipped improvements, last tick) persists in
`agents/keep-the-lights-on/<system>/` in framework storage, so a fresh session
resumes without double-alerting or repeating work.

---

## The three live systems (2026-08 roster)

Registry: `.claude/skills/keep-the-lights-on/systems/registry.yaml`. Each
system declares a `type` that drives its health model.

| System | Type | Config | What "lit" means |
|---|---|---|---|
| **aisleprompt** | site | `<aisleprompt>/keep-the-lights-on.yaml` | Agents running, `aisleprompt.com` 200, articles publishing (`editorial_articles`), pool alive, daily @smoke green |
| **specpicks** | site | `<specpicks>/keep-the-lights-on.yaml` | Same, for `specpicks.com` |
| **retro-chat** | daemon | `.claude/skills/keep-the-lights-on/systems/retro-chat.yaml` | `retro-chat-daemon.service` active AND actively discovering LAN retro agents (not a hung "zombie") |

**Site vs daemon health.** A *site* tick checks the agent roster, DB publish
volume, homepage, pool, and Playwright suite. A *daemon* tick is much smaller:
the unit is `active` **and** it has logged recent work (retro-chat scans the
LAN every ~5 min; no discovery line in 16 min while "active" = a zombie loop →
restart). A daemon has no DB/volume/homepage.

Onboard another system by copying `systems/_example.yaml` into that system's
repo (sites) or the skill dir (daemons), editing it, and adding a registry
line. No skill-code change — the runbook is generic; config carries the values.

---

## The tick, briefly (full detail in SKILL.md)

- **Layer A — cheap health (every tick):** `systemctl` state for the roster,
  one `SELECT count(*)` per DB for 24h publish volume, `curl` the homepages,
  count authed pool profiles, daemon liveness. O(1), ~no tokens. All green →
  one box per system + a long-cadence wakeup. Done.
- **Layer B — reasoning (only when triggered):** a NEW failure, a non-trivial
  fix, or the improvement window. This is where tokens go.
- **Classify** GREEN / DEGRADED / DOWN / BLOCKED and act: auto-fix (§4),
  improve (§5), or escalate (§6). **Cadence:** healthy 1800s, degraded 240s,
  down 180s, blocked-backoff 3600s.
- **Daily functional verification (§5b):** run the site's Playwright `@smoke`
  against prod at least daily and after any prod change; patch real
  regressions, escalate what needs a deploy/decision.

---

## Incident playbook library (fast-path index)

The signatures + exact fixes live in **SKILL.md §11**. Summary:

| # | Signature | Fix / owner |
|---|---|---|
| A | **>5 agents failed at once**, logs `[Errno 28] No space left on device` | Disk-full. `df -h /` first; deleted-but-open leak (df ≫ du) needs operator sudo `lsof +L1` → restart holder; standing preventive = delete 265G stale ollama dup. Then bulk `reset-failed` the stale units + restart zombied daemons. |
| B | pool `state.json` **0/6 authed**, "OAuth session expired"; volume → 0 | Re-auth each profile (operator): `HOME=…/profile-<N> …/claude /login`. Auto-recovers on next drain tick. |
| C | `*-seo-opportunity-agent` fails, `refresh-token.py … HTTP 400` | GSC token weekly expiry. Reset unit each tick (expected, no re-email); operator runs `install/fix-gsc-now.sh`. |
| D | `[deployer:test:smoke] rc=1` on app deploys, prod @smoke green | Stale `localhost:4001` gate fixture. Diagnose first (reproduce `:4001` @smoke), rebuild it — never blind-revert. |
| E | `kitchen-scraper … timed out after 2400s` | Fixed by 9fb1a07 (run-level deadline). Recurrence = deadline regressed; else note-only. |
| F | single transient run failure (Azure-PG timeout, one `000`, "0 proposals") | `reset-failed` / re-probe + note; act only on the 2nd consecutive. |

---

## Operator re-auth runbooks (credentials the agent can't rotate)

Both are **interactive** — run in your own terminal on the host.

### GSC (Google Search Console) token
```bash
bash /home/voidsstr/development/reusable-agents/install/fix-gsc-now.sh
```
Opens a Google consent tab (sign in with the account owning the aisleprompt +
specpicks GSC/GA4 properties; grant all scopes). Verifies + restarts the SEO
agents. Verify: `refresh-token.py … --oauth-file ~/.reusable-agents/seo/.oauth.json`
returns a `ya29…` token, not HTTP 400. Durable fix: publish the OAuth consent
screen to "In production" so the refresh_token stops expiring weekly.

### claude-pool (Max subscription profiles)
Regenerate the exact per-profile commands anytime:
```bash
cd /home/voidsstr/development/reusable-agents && python3 -m framework.cli.claude_pool login-help
```
Then run each (sign each profile into a *different* Max account):
```bash
HOME=/home/voidsstr/.reusable-agents/claude-pool/profile-1 /home/voidsstr/.local/bin/claude /login
# … profile-2 … profile-6
```
Verify (should climb from 0/6):
```bash
python3 -c "import json;s=json.load(open('/home/voidsstr/.reusable-agents/claude-pool/state.json'));print(sum(1 for k,v in s.items() if k.startswith('profile-') and v.get('authenticated')),'of 6')"
```
The pool auto-picks up newly-authed profiles on the next drain tick — no
restart. Even 1–2 profiles restores authoring.

## Secrets backup + host migration pull (Key Vault)

Every host secret is backed up to Azure Key Vault **`nsc-secrets-kv`**
(rg `nsc-apps`, subscription `125b8bc9-e8bb-4827-9d2a-f3492b983dcf`) under the
`fleet-*` prefix: `secrets.env`, SEO + responder OAuth, **each claude-pool
profile** (slim `.claude.json` + `.credentials.json`), pool state, the SSH
keypair, the cloudflared tunnel bundle, `fleet-migration-manifest` (the full
host-takeover inventory — retrieve it FIRST on the new host), and `fleet-backup-manifest` (the
index). Refresh it any time secrets change (new pool account, rotated key):

```bash
bash install/recover-credentials.sh backup     # idempotent; prints names/lengths only
```

**A new host pulls everything with:**

```bash
az login                                       # any account with vault access
git clone <reusable-agents> && cd reusable-agents
bash install/recover-credentials.sh restore    # rebuilds ~/.reusable-agents, ~/.ssh, ~/.cloudflared
```

The script pins `--subscription` itself, so a drifted `az` default does not
break it. Round-trip tested byte-identical. Run `backup` on the OLD host
immediately before cutover; full migration traps live in the
`fleet-host-migration-handoff` memory + `~/.reusable-agents/handoff/MIGRATION-MANIFEST.md`.

## Whitebeast retro-agent (interactive-session bridge)

The retro-agent also runs ON whitebeast itself (Windows side) — reach it at
`172.19.176.1:9898` from WSL / `192.168.1.82:9898` from the LAN, protocol
`shared/retro_protocol.py` in nsc-assistant. It is the ONLY safe launch
channel for hlds/UCC game servers (interactive session; WSL-interop launches
zombie-pin UDP ports). If 9898 times out, the game consoles usually died with
it — operator remedy: run
`C:\development\retro-agent\scripts\game-servers\start-game-servers.ps1`
(self-elevating, starts all three servers, skip-if-running) and restart the
agent.

---

## Improving the agents toward their goals (the "make them better" mandate)

Keeping the lights on is not just uptime — it includes moving the agents
toward their **declared goals** (each agent registers 3–7 goals with a
`metric` bound to a `RunResult.metrics` key) and the growth **North Star**
(regular usage of the sites — indexed pages, organic clicks, returning users,
conversions — *not* code shipped or articles produced). See the repo README
"Goals & metrics — the north star."

**The rule: a change that doesn't move a goal's metric didn't happen.**

Improvement-cycle procedure (SKILL.md §5): open the Goals tab / `GET
/api/agents/<id>/goals`, find a **stalled** goal that maps to a user-facing
metric, trace it to the owning agent's `run()`, make the *smallest* change
that moves the metric, verify it moved over 1–3 runs, commit **main-first**.
Guardrails: Opus-only for editorial prose; framework-first (lift reusable
logic to `framework/core/` + config, don't fork per-site); risky live-app /
infra changes (SSR, sitemap, schema, deploys) are proposed + escalated, not
auto-applied, unless pre-authorized. Record every improvement in the incident
trail so successive sessions don't repeat it.

### Worked examples (shipped while on call)

These are the canonical shape of a good KTLO improvement — a verified,
reversible change traced to a stalled metric:

- **Indexation review → specpicks content throttle** (`97a277d`, `408d0e5`,
  specpicks repo). The North-Star metric (organic clicks / indexed pages) was
  flat: specpicks had 2,684 articles but GSC indexed ~8% ("Discovered –
  currently not indexed"), 7 clicks/28d, and a month of 1,163 new articles
  moved indexed-count by **zero**. Diagnosis (verified, two false alarms ruled
  out): ~39 articles/day is itself the mass-production signal that *causes*
  "Discovered – not indexed." Fix: cut `daily_cap 20→3`, `per_bucket_cap 4→2`,
  drop the high-churn news/trending floors, keep the durable use-case /
  buying-guide surfaces → **~9/day**, reweighted to indexable long-tail.
  Config-only, reversible, no deploy.

- **New `authority-agent`** (`23a5ecd`, reusable-agents repo). The review
  found *no* agent worked the actual lever for the indexation wall — off-page
  authority. Scaffolded a generic, env-driven AgentBase agent that audits
  indexation from GSC, ranks the site's strongest *citable* assets, and emails
  a prioritized off-site link + on-site link-equity worklist. Goals declared
  (`lift-indexation-rate` 8.3%→60%, `surface-authority-targets`); systemd
  timer daily 13:00 EDT. Verified end-to-end (40 assets ranked, worklist
  emailed). Follow-up: an aisleprompt instance (env override).

Both illustrate the discipline: **trace to a stalled North-Star metric,
verify the diagnosis before acting (diagnose-first), make the smallest
reversible change, prove it, commit main-first.**

---

## Standing operator-action items (as of 2026-08-03)

The agent can't do these (need sudo / interactive auth / a decision):

1. **Re-auth the claude-pool** (0/6, OAuth expired) — unblocks all content on
   both sites. Highest North-Star impact. Runbook above.
2. **GSC token** — `install/fix-gsc-now.sh`. Unblocks SEO/indexation tracking.
3. **265G ollama-dup reclaim** — `sudo rm -rf /usr/share/ollama/.ollama/models`
   (active ollama on `/data`). Prevents the disk-full from recurring.

Check the live list in `agents/keep-the-lights-on/<system>/incidents.json`.

---

## Related

- `.claude/skills/keep-the-lights-on/SKILL.md` — the tick runbook + incident library.
- `.claude/skills/keep-the-lights-on/systems/` — registry + per-system configs + `_example.yaml`.
- `README.md` → "Goals & metrics — the north star" — the goals model the improvement cycle serves.
- `docs/agents-catalog.md` — the full agent roster.
- Memories: `aisleprompt-deploy-gate-tests-4001-fixture`, `os-disk-space-leak`,
  `specpicks-scraper-disk-leak` — incident detail.
