> **2026-08-15 — SUPERSEDED IN PART.** The operational runbook is now
> `.claude/skills/provision-fleet/SKILL.md`, which covers the FULL fleet:
> all five repos, the retro-chat daemon + brain, GPU-backed local services,
> website builds and deployments, and a producing-not-just-green verification
> gate. `setup-fleet-host` covered only the framework spine. This document
> remains accurate as the NARRATIVE of the 2026-08 rebuild and its incidents.
> Note the host described here (WSL2, RTX 4080 SUPER) is the OLD box; the
> fleet is moving to a native-Linux host with an RTX 5090.

# Fleet host standup — rebuilding the machine that runs the agents

**What a fleet host is.** The one machine that *executes* agents. Azure holds
the state (registry, run history, queues) and serves the read-only dashboard;
the fleet host holds execution — the systemd timers, the framework API that
writes them, the host-worker, the drainer, and the credential tree the agents
authenticate with.

**Why that distinction matters.** When the host dies, the sites stay up and
serve traffic normally. Nothing 500s. The only symptoms are *absences*:
publish volume goes to zero, IndexNow stops pinging, GSC feedback stops, and
organic traffic decays over days. It reads like "the agents are broken" when
in fact there is no host at all.

The operational runbook is the **`provision-fleet` skill**
(`.claude/skills/provision-fleet/SKILL.md`). This page is the narrative: how
the current host came to be, what a machine loses when it dies, and the
operator checklist that a script cannot do.

---

## The two scripts

```bash
bash install/standup-fleet-host.sh all        # preflight→repos→deps→secrets→api→register→spine→verify
bash install/recover-credentials.sh status    # what a human still has to supply
bash install/recover-credentials.sh backup    # push secrets to Key Vault so this never repeats
```

Both are idempotent. **If you fix something by hand, put it in the script** —
that is the standing rule for infra in this repo, and a host rebuild is the
moment it pays off.

---

## The 2026-08 incident (why this exists)

The fleet ran on an RTX 5090 dev box with repos at
`/home/voidsstr/development/`. On **2026-08-09T10:36Z** it went dark — the last
blob write was a clean `agent-doctor` run, no crash, no ENOSPC cascade. The
machine simply stopped, and its drive was later replaced.

Detected four days later by a `keep-the-lights-on` tick, from a *different*
machine, via three signals that needed no access to the host at all:

| Signal | Reading |
|---|---|
| Azure registry `last_run_at` | newest across all 68 agents = 2026-08-09T10:58Z |
| `editorial_articles` +24h | **0** on both sites (last: aisleprompt 04:35Z, specpicks 04:55Z) |
| specpicks `sync_jobs`/day | 106, 106, 48 → **0, 0, 0, 0** |
| `api_call_log`/day | aisleprompt 54.5k → 31.6k (−42%); specpicks 75.1k → 31.3k (−58%) |

Sites returned 200 the whole time. **Publish pulse, not HTTP status, is the
signal that a fleet is alive.**

### What was lost with the drive

`~/.reusable-agents/` existed nowhere else — not in git, not in blob:

- the Search Console OAuth pair *(later recovered — see below)*
- the claude-pool Max logins → all Opus authoring blocked
- MS Graph / IMAP tokens → responder + all operator email
- the eBay keyset and the BrightData key
- **~270 agent-authored commits** never pushed (GitHub for `aisleprompt` goes
  quiet after 2026-07-31 while the fleet demonstrably ran to 08-09)

Two lessons, both now automated: **push agent commits** (they are work
product), and **`recover-credentials.sh backup`** to Key Vault.

### What was recovered anyway

- Every secret in the Container App + the storage key — from Azure.
- Both Postgres DSNs — embedded in git-tracked manifests. Convenient here, but
  it violates the repo's own no-hardcoded-credentials rule; see the hygiene
  backlog below.
- **The GSC OAuth client_id + secret**, from
  `nsc-assistant/agents/_legacy-seo-opportunity-agent/refresh-token.py`
  (trusted only because that file requests the `webmasters.readonly` scope).
  Only the refresh token needed a browser.
- The complete agent roster — by re-registering from the repos, which also
  recovered 5 agents the stale blob registry had never heard of, including both
  `*-article-proposal-agent` publish engines.

---

## The current host (2026-08-13 →)

`whitebeast`, WSL2 Ubuntu 24.04, RTX 4080 SUPER. Repos at
`/home/voidsstr/development` on ext4 (892G free).

Why not `/mnt/c`: it is 9p, measured **~95× slower** for small writes (300
files: 0.01s vs 0.95s), and its free space swung 147G in twenty minutes — and a
fleet-wide ENOSPC is already in the incident library. Why not the NAS: no
reliable POSIX locking for git, and a share blip fails every agent at once.
Durability instead comes from **code → GitHub**, **state → Azure blob**, and an
optional rsync to the NAS.

Live units: `reusable-agents-api.service` (:8090) · 55 `agent-*.timer` ·
`reusable-agents-host-worker.service` · `auto-queue-drainer.service`.
The Azure dashboard needed **no wiring** — it reads the same blob container
this host writes to, so runs appear there within seconds.

### Fixes this standup forced into the repo

| Fix | Why |
|---|---|
| `install-host-worker.sh` gained `EnvironmentFile=` | it started the worker with an EMPTY storage connection string and passed that to every agent it spawned |
| `indexnow-submitter` `agent.py` + `submit.sh` fall back to global node modules | both assumed `specpicks/node_modules` exists; a fresh clone has none |
| `agent-doctor` manifest stopped pinning `FRAMEWORK_API_URL=:8093` | that port only existed on the old box; the URL is host config, not agent config |
| `sharp` added to aisleprompt `package.json` | imported by a hero-image script but never declared — installed ad-hoc on the old host |
| 7 manifests restored to the retired host's cadence | repo manifests had drifted *slower* (SEO 6h vs the 2h actually running) |
| registration order: framework repo LAST | duplicate ids (`agent-doctor`, `responder-agent`) are last-write-wins, and the `nsc-assistant` forks carry an empty cron |

---

## Operator checklist (a machine cannot do these)

Run `bash install/recover-credentials.sh status` for the live version.

1. **claude-pool** — browser login per Max profile
   (`python3 -m framework.cli.claude_pool login-help`, or
   `install/add-claude-profile.sh`). **Until this is done, publish volume stays
   0** — every Opus-only authoring kind hard-defers by design rather than
   dropping to a weaker model. Never point the fleet at a personal
   `~/.claude/.credentials.json`.
2. **GSC refresh token** — the client pair is already in `secrets.env`:
   `set -a; . ~/.reusable-agents/secrets.env; set +a; bash install/reauth-google-oauth.sh`.
   Then publish the consent screen to *In production* so it stops expiring
   every 7 days.
3. **msmtp** (`sudo`) — `sudo apt-get install -y msmtp msmtp-mta`, then a 0600
   `~/.msmtprc` with the `automation` account. Without it every KTLO escalation
   fails **silently**, which is worse than failing loudly.
4. **eBay keyset / BrightData** — vendor-console copy-paste into `secrets.env`.
5. **Windows boot task** (admin PowerShell) — linger is enabled, but the distro
   itself must start:
   ```
   schtasks /create /tn "WSL-Fleet" /tr "C:\Windows\System32\wsl.exe -d Ubuntu-24.04 -u voidsstr --exec /bin/true" /sc onstart /ru voidsstr /rl highest /delay 0000:30 /f
   ```
6. **Docker Desktop WSL integration** (optional) — unblocks the deployer, the
   dashboard image build, and `specpicks-scraper-watchdog`. Review the pending
   local implementer commits *before* enabling it, since deploys currently fail
   closed at `rc=127`.
7. **Key Vault backup** — `bash install/recover-credentials.sh backup`, so the
   next host loss is a file copy.

---

## Credential hygiene backlog

Real issues this rebuild surfaced. None block operation; all are worth fixing.

- **Plaintext prod Postgres DSNs in git-tracked manifests** (~114 files).
  Directly contradicts CLAUDE.md. They should move to `secrets.env` +
  `${DATABASE_URL_<SITE>}` references. Rotate the password when they do.
- **`registry/agents.json` drift** — 5 weeks stale at rebuild time. Registering
  from the repos is the correct default; the blob should follow, not lead.
- **Agents with no declared goals** — CLAUDE.md requires 3–7 each.
- **`specpicks-scraper-watchdog`** still shells `docker run` from a bash
  entry_command, violating the AgentBase mandate. Currently disabled here.

---

## Related

- `.claude/skills/provision-fleet/SKILL.md` — the phase-by-phase runbook and
  the trap list
- `.claude/skills/keep-the-lights-on/SKILL.md` + `docs/keep-the-lights-on.md` —
  day-to-day on-call once the host is up
- `docs/architecture.md` — why state lives in blob and the host only executes
- `docs/repo-boundaries.md` — what belongs in the framework vs a site repo vs
  `~/.reusable-agents/`
