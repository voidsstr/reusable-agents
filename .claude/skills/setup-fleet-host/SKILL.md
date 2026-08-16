---
name: setup-fleet-host
description: >
  Stand up a machine as THE reusable-agents fleet host — the box that runs
  every agent timer, the framework API, the host-worker and the auto-queue
  drainer, and that owns the credential tree the agents authenticate with.
  Invoke when the operator says any of: "stand up the fleet host", "set up
  the agents on this computer", "the old box died / is getting a new drive",
  "migrate the fleet to this machine", "why are no agents running on this
  host", "rebuild the host", or when a keep-the-lights-on tick discovers the
  fleet host is gone rather than merely broken. Also covers the reverse
  question — "what is this host responsible for?" — because the phases below
  ARE the responsibility list.
---

> ⚠️ **SUPERSEDED by `/provision-fleet`.** This skill covers only the
> framework spine (API, host-worker, drainer, agent registration). It does
> NOT cover the retro-chat daemon + brain, GPU-backed local services,
> website builds/deployments, or the five-repo layout. Use
> `.claude/skills/provision-fleet/SKILL.md` to stand up a host.


# Set up the fleet host

A **fleet host** is the single machine that actually executes agents. Azure
holds the *state* (registry, run history, queues) and the read-only dashboard;
the fleet host holds the *execution*. If it is off, the sites keep serving but
nothing publishes, nothing gets indexed, and organic traffic decays — that is
exactly what a dark host looks like from the outside, and it is easy to
misread as "the agents are broken."

Everything below is implemented by two scripts. Prefer running them over doing
steps by hand — if you fix something by hand, put it in the script:

```bash
bash install/standup-fleet-host.sh all          # every phase, idempotent
bash install/recover-credentials.sh status      # what's still missing
```

---

## 0. The path contract — decide this FIRST

`DEV_ROOT` must be **`/home/<user>/development`** on a **local POSIX
filesystem**. This is not a preference:

- All 68+ registry `repo_dir` values, every manifest `entry_command`, and
  `framework/core/scheduler.py`'s `EnvironmentFile=` line hardcode
  `/home/voidsstr/development/...`. Match it and you rewrite nothing.
- It must NOT live on `/mnt/c` (WSL 9p) — measured **~95× slower** on small
  writes (300 files: 0.01s ext4 vs 0.95s 9p), and agents do thousands of small
  reads/writes per run.
- It must NOT live on a NAS/SMB/NFS share, however well-protected the drives
  are. No reliable POSIX locking for git; if the share blips, *every* agent
  fails at once. Durability comes from three layers that don't involve the
  working tree: **code → GitHub**, **agent state → Azure blob** (by design),
  and an optional rsync of `~/.reusable-agents` + repos to the NAS.

Repos required at `$DEV_ROOT`: `reusable-agents` (framework), plus each site
repo (`aisleprompt`, `specpicks`) and `nsc-assistant` (holds shared + per-site
wrapper agents). A missing site repo silently removes that site's agents.

---

## 1. What the host is responsible for

Six things. The `verify` phase checks all of them.

| # | Responsibility | Unit / artifact |
|---|---|---|
| 1 | **Framework API** — registry, status, triggers, and it WRITES the systemd units | `reusable-agents-api.service` on :8090 |
| 2 | **Agent timers** — one `.timer` + `.service` per scheduled agent | `agent-<id>.timer` (55+) |
| 3 | **Host-worker** — executes dashboard "Run now" triggers on the host | `reusable-agents-host-worker.service` |
| 4 | **Drainer** — fires the implementer when producers queue recs | `auto-queue-drainer.service` |
| 5 | **Credential tree** — every secret the agents authenticate with | `~/.reusable-agents/` (0700) |
| 6 | **Language runtimes** — python user-site + node/nvm reachable *from systemd* | `~/.config/pip/pip.conf`, `~/.config/systemd/user/service.d/10-fleet-path.conf` |

The API must run **on the host**, not in docker: registration is what writes
`~/.config/systemd/user/`, and a container cannot reach it. docker is often
unavailable in WSL anyway — run uvicorn directly (the `api` phase does).

---

## 2. Phases

Run `all`, or individually while iterating:

| Phase | Does | Watch for |
|---|---|---|
| `preflight` | systemd, linger, fs type, python/node/git/az/gh, git identity, sudo, msmtp, ports, GPU, ollama | Anything printed under OPERATOR ACTION REQUIRED |
| `repos` | clone/refresh the 4 repos at `$DEV_ROOT` | a repo with 0 manifests = that site's agents vanish |
| `deps` | python → user site; node globals (`ts-node typescript@5 tsx pg`); log dir | PEP 668, see §4 |
| `secrets` | delegates to `recover-credentials.sh harvest` | 3 tiers, see §3 |
| `api` | writes + starts the API unit, waits for `/api/health` | must report `storage_backend: azure` |
| `register` | registers every agent from the repos → timers | duplicate ids + no-cron warnings |
| `spine` | host-worker + drainer (+ seeds the responder config the drainer needs) | drainer refuses to start without that config |
| `verify` | timers, failed units, the 3 services, a storage round-trip, publish pulse | a clean `systemctl start` is NOT proof — see §4 |

**Register from the REPOS, not the blob registry.** The blob registry drifts:
in the 2026-08 rebuild it was 5 weeks stale — it still carried deleted agents
(`*-article-author-agent`, dead 64 days) and was missing the live ones
(`*-article-proposal-agent`, the actual publish engines). Repos are truth.
Afterwards, diff the new roster against the old registry to confirm nothing
that *was* enabled got dropped, and to catch cadence drift (§5).

---

## 3. Credentials — three tiers

`install/recover-credentials.sh harvest` writes `~/.reusable-agents/secrets.env`
(0600) and prints a checklist of what only a human can do.

- **TIER 1 — Azure** (machine-recoverable): `AZURE_STORAGE_CONNECTION_STRING`,
  `FRAMEWORK_API_TOKEN`, `GOOGLE_OAUTH_CLIENT_SECRET`, `SESSION_SECRET`,
  storage account key. Requires `az` logged into the fleet subscription —
  **always pass `--subscription`**, the CLI default on a shared box is some
  other tenant.
- **TIER 2 — repos**: both Postgres DSNs, `AZURE_OPENAI_*`, `SEARXNG_URL`,
  IndexNow keys, and the **Search Console OAuth client pair** (it survives in
  the legacy SEO collector's `refresh-token.py` — only trust a pair from a file
  that requests the `webmasters` scope; other clients on the same GCP project
  are not interchangeable).
- **TIER 3 — human only**: claude-pool Max logins, the GSC *refresh token*,
  msmtp, eBay keyset, BrightData, MS Graph/IMAP. Browser flows or vendor-console
  copy-paste.

**Never let a host be the only copy again.** `recover-credentials.sh backup`
pushes `secrets.env` and the `seo/`, `responder/`, `claude-pool/` dirs to Key
Vault. The 2026-08 loss cost the GSC client pair, the Max logins, the eBay
keyset and ~270 unpushed agent commits, because that directory existed nowhere
else.

**Impact of each gap, so you can triage honestly:** no claude-pool → every
Opus-only authoring kind hard-defers and **publish volume stays 0**; no GSC
token → both `*-seo-opportunity-agent` + coverage auditors fail; no msmtp →
every escalation email fails *silently*, which is the worst failure mode.

---

## 4. Traps that cost hours (all hit in the 2026-08 rebuild)

1. **`secrets.env` values MUST be single-quoted.** Consumers do
   `set -a; . secrets.env; set +a`, and an Azure connection string is full of
   `;` — unquoted, the shell truncates at the first one and you get
   `ValueError: Connection string missing required connection details`, which
   reads like a bad secret rather than a quoting bug. systemd's
   `EnvironmentFile=` strips the same quotes, so quoting is safe for both.
2. **PEP 668.** Ubuntu 24.04 marks the interpreter externally-managed. Agents
   invoke bare `python3`, so a venv would mean rewriting every
   `entry_command` — instead write `~/.config/pip/pip.conf` with
   `user = true` + `break-system-packages = true`. `--user` never touches
   system packages.
3. **nvm is invisible to systemd.** node installed via nvm is not on the
   systemd user PATH, so every node-dependent agent dies with a bare
   `FileNotFoundError: [Errno 2] No such file or directory: 'npx'` — 17 agent
   failures traced to this. Fix with a **global drop-in**
   `~/.config/systemd/user/service.d/10-fleet-path.conf` setting `PATH` (nvm
   bin + `~/.local/bin`) and `NODE_PATH` (`npm root -g`). `environment.d` did
   NOT take effect even after `daemon-reexec`; the drop-in does.
4. **`npx <tool>` without the tool installed** fetches it into `~/.npm/_npx`
   *without peers* → `TypeError: Cannot read properties of undefined (reading
   'fileExists')`. Install `ts-node typescript@5 tsx pg` globally and prefer
   `npx --no-install`. Pin **typescript@5** — ts-node 10 breaks on TS 7.
5. **Site repos need `npm install`.** A fresh clone has no `node_modules`, and
   several agents borrow the site app's modules. Also watch for **undeclared
   deps**: `sharp` was imported by an aisleprompt script but absent from its
   `package.json` — it had been installed ad-hoc on the old host. Fix by
   installing it *into the repo* so `package.json` records it.
6. **Duplicate agent ids across repos.** `agent-doctor` and `responder-agent`
   exist in both `reusable-agents` and `nsc-assistant`, the latter as stale
   forks with an EMPTY `cron_expr`. Registration is last-write-wins, so the
   framework repo must register **LAST** or those agents end up scheduled
   never. The `register` phase prints duplicates and any enabled-but-cronless
   agent.
7. **A clean `systemctl start` proves nothing.** Generated units use
   `EnvironmentFile=-...` (leading `-` = tolerate absence), so a unit starts
   GREEN and fails later on a missing DSN. Verify by reading the agent's final
   status line in `/tmp/reusable-agents-logs/agent-<id>.log`.
8. **`install-host-worker.sh` shipped without an `EnvironmentFile`** — the
   worker started with an empty storage connection string and handed it to
   every agent it spawned. Fixed in-repo; check it survived.
9. **No docker in WSL** → the deployer's stages fail `rc=127`, so agent code
   commits land locally but never deploy. That is a *safety net*, not a
   success: review those commits before enabling Docker Desktop WSL
   integration.

---

## 5. Preserve the proven cadence

The retired host's registry is the record of **what was actually working**.
Repo manifests had drifted *slower* (SEO every 6h vs the 2h that was running;
ebay sync 4-hourly vs hourly) — adopting them silently cuts publish throughput
several-fold. After registering, diff new vs old cron per agent and restore the
proven values **into the repo manifests** (so the fix is durable and
committed), then re-register. Treat the previous host's behaviour as the
baseline to beat, not a draft to redesign.

---

## 6. Definition of done

- `verify` shows: timers > 0, the three services active, storage round-trip OK.
- Every agent has actually RUN once with a `success` final status line — not
  merely "started". Trigger the ones whose cron is hours away.
- The Azure dashboard shows fresh `last_run_at` for this host's agents (same
  blob backend — no extra wiring needed).
- The publish pulse is non-zero, OR the reason it is zero is a named TIER-3
  credential gap.
- Boot persistence: `loginctl show-user <user> -p Linger` = yes, **and** on WSL
  a Windows-side scheduled task starts the distro at boot (needs an admin
  PowerShell; WSL interop may be disabled inside the distro, so the operator
  must run it):
  ```
  schtasks /create /tn "WSL-Fleet" /tr "C:\Windows\System32\wsl.exe -d <distro> -u <user> --exec /bin/true" /sc onstart /ru <user> /rl highest /delay 0000:30 /f
  ```
- Hand off to **`keep-the-lights-on`** for ongoing operation. Standup ends
  where KTLO begins.

---

## 7. Related

- `docs/fleet-host-standup.md` — the narrative, the 2026-08 incident, and the
  post-standup operator checklist.
- `.claude/skills/keep-the-lights-on/SKILL.md` — day-to-day on-call once the
  host is up; its §11 incident library assumes a host that exists.
- `docs/architecture.md` — why state lives in blob and the host only executes.
