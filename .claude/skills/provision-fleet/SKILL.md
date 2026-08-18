---
name: provision-fleet
description: >
  Stand up the COMPLETE aisleprompt + specpicks agent fleet and its supporting
  infrastructure on a new server, from a bare OS to verified-producing agents.
  Covers host deps, credentials, the framework spine, all registered agents,
  the retro-chat daemon + brain, GPU-backed local services, website builds and
  deployments, data stores, and a producing-not-just-green verification gate. Invoke when the operator says "set up the new server", "provision the
  fleet", "the real server is online", "rebuild the fleet host", "migrate the
  agents to <host>", or after any total host loss. Supersedes the narrower
  setup-fleet-host skill, which only covered the framework spine.
---

# Provision the fleet

Rebuild everything needed for **aisleprompt.com** and **specpicks.com** to run
their agents autonomously on a new machine.

> **Target host.** Written for a **native-Linux box with an NVIDIA RTX 5090**.
> The previous host was WSL2 on a 4080 SUPER, so a few traps below are WSL-only
> and are marked as such — on native Linux they simply do not apply, and the
> GPU-backed services in §7 stop being optional nice-to-haves.
>
> **Status of this document.** Living. It is written from a real rebuild
> (2026-08-12..14) plus the incidents that rebuild exposed. Every trap in §9 is
> something that actually happened and cost hours. When you hit a NEW way the
> fleet half-works, add it there — that section is the reason this skill exists.

---

## 0. The one thing to understand first

**A green agent is not a working agent.** This fleet's dominant failure mode is
not crashes — it is agents that run, exit 0, log `success`, and produce nothing.
The 2026-08 rebuild ended with 44 agents "healthy" while:

- every Amazon price on aisleprompt was **four months stale** (scraper exited 0),
- both `gsc-coverage-auditor`s had produced **zero** coverage rows in ~90 runs each,
- `recipe-image-refiller` logged `Refilled 0 rows` **237 times** against 7,745 NULLs,
- the SEO analyzer's LLM pass had **never run** (`No module named llm_audit`),
- 4,298 emails were queued to a transport that did not exist, while every sender
  logged `Emailed N recipient(s)`.

None of that appears in `systemctl --state=failed`. Provisioning is therefore not
finished when units are green — it is finished when **§8 proves output exists**.

---

## 1. What you are building

```
                    ┌─ framework API (:8090) ── UI (:8091)
  Azure Blob ───────┤
  (state of record) ├─ host-worker ──── spawns agent runs
                    ├─ auto-queue-drainer ── fires implementer on queued recs
                    └─ backlog-dispatcher ── feeds the queue from producers
        │
        ├── ~55 systemd --user timers (73 agents registered)
        ├── 2 Postgres DBs (Azure) — the sites' own content + catalog
        ├── local daemons: SDXL :7861 · SearXNG :8888 · ollama :11434
        └── retro-chat: daemon (LAN :9898) + brain (Claude Agent SDK)
```

**FIVE git repos**, all `main`/`master`-first, no feature branches. A rebuild
that clones only the first three gets a fleet with no retro-chat at all:

| repo | holds |
|---|---|
| `reusable-agents` | the framework, shared agents, this skill |
| `aisleprompt` | site code + its agents + `keep-the-lights-on.yaml` |
| `specpicks` | site code + its agents + `keep-the-lights-on.yaml` |
| `nsc-assistant` | the retro-chat **daemon** (LAN bridge) |
| `retro-agent` | the retro-chat **brain** + its systemd units + installer |

---

## 2. Order of operations

Do these in order. Later phases genuinely depend on earlier ones.

```
1  host deps        §3   binaries, python, node, systemd --user + linger
2  repos            §4   clone to a LOCAL filesystem (never a 9p/NFS mount)
3  credentials      §5   the matrix — most fleet breakage is a missing secret
4  data stores      §6   Azure container + both DB DSNs reachable
5  local daemons    §7   SDXL, SearXNG, ollama, docker
6  framework spine       API → host-worker → drainer → dispatcher
7  register agents       registration is what WRITES the systemd units
8  retro-chat       §7b  daemon + brain (two units, not one)
9  site builds      §7c  local dev stacks + deploy auth, so code can SHIP
10 verify output    §8   the gate that actually matters
```

`install/standup-fleet-host.sh` automates 1–2 and 6–7 as idempotent phases
(`preflight|repos|deps|secrets|claude-pool|api|register|spine|verify|all`). It
does **not** do §5 (interactive credentials) or §7 (daemons) — those are the
manual parts, and they are where fleets silently half-work.

---

## 3. Host dependencies

Required, with what breaks without each:

| binary | needed by | absent ⇒ |
|---|---|---|
| `python3` ≥3.12 + `psycopg2`, `pyyaml`, `azure-storage-blob`, `google-auth-oauthlib` | everything | hard fail |
| `git` | implementer, deployer | hard fail |
| `node`/`npm`/`npx` + `tsx` | specpicks scripts, IndexNow, Playwright | hard fail on those agents |
| `claude` CLI | all authoring | **silent defer** — see §9.2 |
| `az` | Azure blob + deploys | hard fail |
| `docker` + `docker compose` | deployer (BOTH sites) | deploys fail, code never ships |
| `msmtp` | operator email | **silent queue growth** — see §9.6 |

**Playwright browsers need system libs the npm install does NOT bring.**
`npx playwright install chromium` downloads the browser but not its shared
libraries. On a fresh host `chrome --headless` dies with
`error while loading shared libraries: libnspr4.so` and **exit 127**, which
surfaces as a deploy TEST FAILURE — so it reads like a code regression when it
is a missing package:

```bash
npx playwright install-deps chromium     # or:
sudo apt-get install -y libnss3 libnspr4 libasound2t64
ldd ~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome | grep 'not found'   # must be empty
```
This blocks the deployer's smoke gate on BOTH sites, the daily KTLO `@smoke`
run, and `progressive-improvement-agent`'s crawler.

**systemd `--user` is mandatory** — every agent is a user timer. Enable lingering
or nothing runs when you log out:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
```

**PEP 668**: modern Debian/Ubuntu marks the system Python externally-managed.
Use a venv or `--break-system-packages` deliberately; do not let half the deps
land in a user site-dir the systemd units cannot see.

**nvm is invisible to systemd.** A node installed via nvm lives in a shell-only
PATH. Agents that shell to `npx` will fail under systemd while working fine when
you test by hand. Install node system-wide, or add a global drop-in:

```bash
mkdir -p ~/.config/systemd/user.conf.d
printf '[Manager]\nDefaultEnvironment=PATH=/usr/local/bin:/usr/bin:/bin NODE_PATH=/usr/lib/node_modules\n' \
  > ~/.config/systemd/user.conf.d/10-path.conf
systemctl --user daemon-reexec
```

---

## 4. Repos

Clone all three to a **local ext4 path**. Do not run the fleet from a network
mount (NAS/9p/drvfs): git and node are pathologically slow on them, and the
implementer's per-batch git work becomes the bottleneck.

Canonical layout this fleet assumes:
```
/home/<user>/development/{reusable-agents,aisleprompt,specpicks}
```
Agent code contains **hardcoded absolute paths** to these (see §9.8). If you
deviate, grep and fix before registering anything.

---

## 5. Credentials

The credential matrix is the highest-value part of this skill: most "the fleet is
broken" reports are one missing secret. `install/recover-credentials.sh
harvest|status` pulls what it can from Azure and the repos; the rest is manual.

`~/.reusable-agents/secrets.env`, mode 0600, plain `KEY=value`:

| key | consumer | absent ⇒ |
|---|---|---|
| `AZURE_STORAGE_CONNECTION_STRING` | all storage | hard fail, everything |
| `DATABASE_URL_AISLEPROMPT` / `_SPECPICKS` | most agents | hard fail |
| `AMAZON_CREATORS_CLIENT_ID` / `_SECRET` / `_PARTNER_TAG` | price refresh both sites | **silent** — prices rot |
| `EBAY_CLIENT_ID` / `_SECRET` | ebay-product-sync | failed unit |
| `ANTHROPIC_API_KEY` | user-growth-strategist | failed unit |
| `YOUTUBE_API_KEY` | youtube-review-agent | status `blocked` — **invisible to grep** |
| `FRAMEWORK_API_TOKEN` | API auth | 401s |
| `DIGEST_DISABLED` | mail gate | see §9.6 |

**Quoting matters.** Consumers do `set -a; . secrets.env`. An Azure connection
string is full of `;` — unquoted, the shell treats it as a command separator and
the value truncates. Always single-quote:

```bash
printf "%s='%s'\n" "$k" "${v//\'/\'\\\'\'}"
```

Credentials that live **outside** that file:

| path | for | how to (re)create |
|---|---|---|
| `~/.reusable-agents/seo/.oauth.json` | GSC + GA4 | `bash install/reauth-google-oauth.sh` in a REAL terminal |
| `~/.aisleprompt-kitchen.env` | kitchen scraper | reconstruct per `aisleprompt/scraper-kitchen/README.md` |
| `~/.reusable-agents/claude-pool/` | Opus authoring | `install/add-claude-profile.sh` per Max account |
| `~/.msmtprc` (0600) | operator email | relay password; only `automation` account is configured |

**GSC scope gotcha.** The bootstrap requests `webmasters.readonly`. That is
enough to READ coverage and search analytics, but **not** to submit sitemaps —
IndexNow's sitemap pings 403 (observed `ok=8/15`). If you need submission, request
read-write `auth/webmasters` at consent time.

---

## 6. Data stores

- **Azure Blob** is the state of record; `STORAGE_BACKEND=azure` everywhere.
  Prefixes: `agents/` (runs, goals, state), `registry/agents.json`, `config/*.json`,
  `digest-queue/`, `digest-archive/`, `framework/`.
- ⚠️ **`az` defaults to the WRONG subscription.** The account's default may be
  an unrelated subscription; the fleet lives in `125b8bc9-e8bb-4827-9d2a-f3492b983dcf`.
  Every `az` call in a provisioning script must pass `--subscription` explicitly
  or it silently targets the wrong account.
- **Two Postgres DBs** (Azure). Agents assume tables exist —
  `editorial_articles`, `products`, `kitchen_product_listings`, `outbound_clicks`,
  `kitchen_click_events`, `comparison_commentary`, benchmark tables. A missing
  table shows up as a per-agent SQL error, not a startup failure.
- **Storage-backed config** the framework reads at runtime:
  `config/priority-config.json`, `required-models.json`, `ai-defaults.json`,
  `implementer-allowed-dispatch-kinds.json`. Absent ⇒ defaults apply. Check
  `implementer-allowed-dispatch-kinds` is `["*"]` — a stale narrow allowlist
  silently pauses whole producer classes.

⚠️ `list_prefix()` caps at 10,000 keys and returns them **lexicographically**, so
a busy agent's history truncates to its OLDEST slice. Use
`storage.list_child_prefixes()` for run enumeration. This silently returned
three-month-stale data in production — see §9.7.

---

## 7. Local daemons

**GPU prerequisites (native Linux).** Install the NVIDIA driver + CUDA runtime
and confirm `nvidia-smi` reports the 5090 before touching the image daemon.
`services/local-image-gen/requirements.txt` **pins torch deliberately** — a newer
torch fatal-asserts on an NVML driver mismatch, so do not "upgrade" it. Paid image
providers are forbidden by policy, so if this daemon is down, image generation does
not fall back — it silently produces nothing (§9.6 class).

| service | port | required for | absent ⇒ |
|---|---|---|---|
| SDXL image gen | 7861 | ALL image generation (paid providers are forbidden) | refiller "succeeds" with 0 rows |
| SearXNG | 8888 | product/topic discovery | discovery degrades, refresh unaffected |
| ollama | 11434 | local LLM (kitchen enrichment) | scraper enrichment fails |
| docker | socket | **deployer, BOTH sites** | code commits but never ships |

**Docker on WSL:** if `/usr/bin/docker` is a symlink into
`/mnt/wsl/docker-desktop/cli-tools/`, this host gets docker from Docker Desktop
on Windows — `docker.service` will not exist and installing `docker.io` collides
with the shim. The fix is Desktop → Settings → Resources → **WSL Integration** →
enable this distro. Diagnose precisely: if
`/mnt/wsl/docker-desktop/shared-sockets/guest-services/` has bootstrap sockets but
**no `docker.sock`**, integration is off for this distro.

**ollama models:** check what is actually pulled. A config naming `qwen3:32b` on a
host that only has `qwen3:14b` fails at first use, not at startup.

### 7a. Standup — SDXL image daemon (:7861)

On the 5090 this is first-class, not optional: every image path in the fleet
POSTs here and there is NO paid fallback by policy.

```bash
nvidia-smi | grep -q 5090 || echo "STOP: driver/GPU not ready"
cd ~/development/reusable-agents/services/local-image-gen
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt  # torch is PINNED — do not upgrade
```

- **Token:** `LOCAL_IMAGE_GEN_TOKEN` must be in `~/.reusable-agents/secrets.env`.
  Recoverable from Key Vault: `az keyvault secret show --vault-name nsc-secrets-kv
  -n file-reusable-agents-secrets-env` (the full secrets.env backup).
- **Unit:** `~/.config/systemd/user/local-image-gen.service` —
  `ExecStart=%h/development/reusable-agents/services/local-image-gen/.venv/bin/python server.py`,
  `EnvironmentFile=%h/.reusable-agents/secrets.env`, `Restart=on-failure`. Then
  `systemctl --user enable --now local-image-gen`.
- First start downloads ~7 GB of SDXL-Turbo weights into `~/.cache/huggingface`;
  `/healthz` is not ready until they load. Verify:
  `curl -s localhost:7861/healthz` → `{"status":"ok", "gpu": "...5090..."}`.
- **Output-based verification (§8 rule):** trigger `recipe-image-refiller` and
  watch the recipe `image_url IS NULL` count actually DROP (backlog was 7,745
  when the old host's daemon died). A green refiller run that fills 0 rows means
  the daemon or token is still wrong — the agent now reports `blocked` when it
  cannot reach :7861, so a `blocked` status here is the daemon's problem, not
  the agent's.

### 7a.2 Standup — SearXNG (:8888)

Product/topic discovery + hero-image search. Agents read `SEARXNG_URL` from
`secrets.env`; as of 2026-08-18 it points at `https://searxng.aisleprompt.com`,
which returns **Cloudflare 530** — the tunnel route died with the old host, so
standing this up is REQUIRED, not inherited.

```bash
docker run -d --name searxng --restart unless-stopped \
  -p 127.0.0.1:8888:8080 -v ~/searxng:/etc/searxng searxng/searxng
# Enable the JSON API or every agent query 403s:
#   ~/searxng/settings.yml → search: { formats: [html, json] }; then: docker restart searxng
```

Then pick ONE:
1. **Local-only (simplest):** set `SEARXNG_URL=http://localhost:8888` in
   `~/.reusable-agents/secrets.env` — every consumer runs on this host anyway.
2. **Restore the tunnel hostname:** add an ingress rule
   `{hostname: searxng.aisleprompt.com, service: http://localhost:8888}` to the
   cloudflared config (see `~/.cloudflared/config-ollama.yml` for the shape),
   `cloudflared tunnel route dns <tunnel> searxng.aisleprompt.com`, restart
   cloudflared. Only needed if the Azure-hosted site code must reach it too.

Verify with a real query, not a homepage 200:
`curl -s 'http://localhost:8888/search?q=cast+iron+skillet&format=json' | head -c 200`
must return JSON results. Then confirm a newly-authored article picks up a hero
image (`hero_image_url IS NOT NULL`) without the curator having to rescue it.

---

## 7b. Retro-chat — the retro computers' brain

**It is TWO services, not one.** KTLO watches only the first. Provisioning just
the daemon gives you a green health check and a chat that never answers.

```
retro_chat.exe (Win98/2K/XP) ──TCP 9898── retro_agent.exe
                                              │
                          retro-chat-daemon.service   (nsc-assistant)
                          pure network multiplexer, ZERO LLM
                                              │
                      /tmp/retro-chat/inbox/   outbox/   ← filesystem queue
                                              │
                          retro-chat-brain.service    (retro-agent)
                          Claude Agent SDK — this IS the brain
```

| | daemon | brain |
|---|---|---|
| repo | `nsc-assistant` | `retro-agent` |
| exec | `agent/tools/retro_chat_daemon.py` | `scripts/retro_chat_brain.py` |
| deps | **stdlib only** | `claude-agent-sdk`, `pillow` |
| model | none | `RETRO_BRAIN_MODEL=claude-opus-5` |
| liveness | logs `discovery: claimed N agents` every 300s | touches `/tmp/retro-chat/processor.heartbeat` every 20s |

Install both with `retro-agent/scripts/install-chat-services.sh` (copies the
units, `enable-linger`, `daemon-reload`, `enable --now`). Then:

- **`/tmp/retro-chat/{inbox,outbox,status_outbox}` must exist** — and `/tmp` does
  not survive a reboot. Create it at boot (tmpfiles.d or an `ExecStartPre`).
- **The host must sit on the retro machines' LAN segment.** Discovery brute-forces
  `192.168.1.1–254:9898`; a different subnet finds nothing.
- The brain needs `claude` on PATH — keep the global drop-in
  `~/.config/systemd/user/service.d/10-fleet-path.conf`.
- It round-robins machines across the default Claude login **and every healthy
  claude-pool profile**, failing over between them, so §5's pool applies here too.

⚠️ **Cold-boot crash loop.** The daemon returns from `main_async()` when discovery
finds zero agents, with `Restart=always` / `RestartSec=3`. Provision the box while
the retro PCs are powered off and it rescans 254 IPs every 3 seconds forever.
Either bring one retro machine up first, or leave the unit disabled until they are.

⚠️ **The brain's death is invisible to KTLO.** If the brain dies the daemon keeps
logging discovery lines, so the tick reports GREEN while every prompt goes
unanswered. The heartbeat file exists (`chat_status.sh` flags it stale at >120s)
but KTLO does not read it. Treat "daemon active" as necessary, never sufficient.

Note `AGENT_SECRET` is **hardcoded in the daemon source**, so it is in git history —
rotate it if the LAN is not trusted.

---

## 7c. Website builds and deployments

A host that cannot deploy is a host whose agents' work never reaches users. On the
2026-08 rebuild, 98 commits sat undeployed for days behind two missing packages.

Each site's `deployer:` block in `site.yaml` runs
`test → build → push → deploy → smoke_check`. Both sites need docker; the test
gates run against LOCAL dev stacks:

| site | test gate | brought up by |
|---|---|---|
| aisleprompt | `http://localhost:4001` | `docker compose up -d frontend backend db` |
| specpicks | `http://localhost:10001` | its compose stack |

Required before a deploy can succeed:

```bash
# 1. Playwright browsers AND their system libs (see §3 — libs are separate)
cd <site>/tests && npm ci && npx playwright install chromium && npx playwright install-deps chromium

# 2. every locally-referenced image must be BUILT (nothing pulls these)
docker build -t specpicks-scraper:latest specpicks/scraper

# 3. cloud auth — note the subscription trap in §6
az login && az acr login --name nscappsacr
```

The framework dashboard is a **separate** deploy: any change to `framework/api/`,
`framework/ui/`, or the `framework/core/` modules the API imports requires
`bash install/deploy-azure.sh`, or production keeps the old build. Agent code under
`agents/<id>/` does NOT need it — the host re-execs from disk.

---

## 8. Verification — the gate that matters

Do **not** declare the fleet provisioned on green units. Prove output:

```bash
# 1. spine
systemctl --user is-active reusable-agents-host-worker.service auto-queue-drainer.service
systemctl --user list-timers --all | grep -c 'agent-'      # expect ~44

# 2. failures — but know this UNDERCOUNTS (see §9.5)
systemctl --user list-units --state=failed --no-legend | grep agent-

# 3. OUTPUT, per site — the real gate
#    articles published, products repriced, clicks recorded, coverage rows
```

Minimum bar before calling it done:

- [ ] an article **published** on each site within 24h (not "proposed")
- [ ] Amazon prices refreshed within 24h on both catalogs
- [ ] a GSC token that mints `ya29…` **and** returns `siteOwner` for both properties
- [ ] `gsc-coverage-auditor` wrote real coverage rows (not "no coverage file yet")
- [ ] the implementer completed a batch **and the deployer succeeded**
- [ ] goals `progress_history` is non-empty on ≥1 agent per site
- [ ] catalog-health checks in both `keep-the-lights-on.yaml` pass
- [ ] **retro-chat: daemon logs `discovery: claimed N agents`, AND
      `/tmp/retro-chat/processor.heartbeat` is younger than 120s** — both, because
      the daemon stays green when the brain is dead
- [ ] a deploy reached production (not just committed) — verify by fetching a
      marker the deploy changed, not by reading the deployer's exit code

Then hand over to **`/keep-the-lights-on`** for ongoing operation.

---

## 9. Traps — every one of these actually happened

### 9.1 A bare `lib/` in .gitignore destroys source at any depth
`.gitignore` patterns without a leading slash match at **every** level. A bare
`lib/` silently excluded `agents/seo-opportunity-agent/lib/{collector,analyzer}/`,
so `pull-data.py`, `llm_audit.py` and the analyzer were **never committed** and
died with the old host. Both SEO agents then failed `collector exited rc=2`, and
the LLM audit had never run on either site.
**Do:** anchor as `/lib/`. On any rebuild, `git status --ignored` and look for
excluded paths that contain real source. Files referenced by code but missing from
the repo are the next casualty — grep `script_rel`, subprocess targets, imports.

### 9.2 Missing claude-pool shim silently disables ALL Opus authoring
The implementer decides Opus is reachable by **executing**
`~/.reusable-agents/claude-pool/bin/claude`. If that shim is absent the probe
fails *identically to a dead pool*: `IMPLEMENTER_FORCE_FALLBACK=1`, cached 15 min,
every article deferred `required model claude-opus-5 unavailable` — while
`claude --model claude-opus-5` works perfectly from the shell, making it look like
an Anthropic outage. Root cause was an idempotency bug: `bootstrap.sh` created the
shim inside a "profile-1 doesn't exist yet" branch, so profiles created any other
way never got one.
**Do:** create the shim unconditionally (`standup-fleet-host.sh claude-pool`) and
probe it — it must print `[claude-pool] → profile-N (home=…)`.

### 9.3 `claude --print --output-format text` returns only the FINAL turn
Any run that emits text → calls a tool → emits more text returns **just the tail**.
Multi-turn JSON arrives headless: starts mid-object, more `}` than `{`. This zeroed
publish volume on both sites for days. Measured: `text` 14 bytes vs `stream-json`
26 bytes for the same prompt.
**Do:** `--output-format stream-json --verbose` for `max_turns > 1`, concatenating
the text blocks of every assistant event.

### 9.4 A short-circuit that stamps its own timestamp starves forever
The article proposers wrote `last_run_ts: self.run_ts` on a **skipped** run, so a
no-op reset the clock the re-run gate measures. With an 8h timer and a 12h gate the
condition became permanently unsatisfiable — the agent skipped forever. Tell-tale:
skip messages whose interval matches the TIMER, not the gate.
**Do:** carry the prior value forward on a skip; only real work advances it.

### 9.5 `systemctl --state=failed` undercounts badly
Agents log `failure (0%)` while exiting 0; `youtube-review-agent` reports status
`blocked` and matches no success/failure grep; `promo-curator` and both
`site-goals-tracker`s simply never ran. The unit list said "4 failures" while the
real broken surface was far larger.
**Do:** verify by **output** (§8), not unit status.

### 9.6 Queueing mail with no transport grows silently forever
Agents call `send_via_msmtp` without `bypass_digest`, which **enqueues** and
returns `ok=True` *for the enqueue*. Only `digest-rollup-agent` delivers. With no
msmtp binary and no Graph/SMTP creds, the queue reached **4,298** items growing
~140/hr while every sender logged `Emailed N recipient(s)`.
**Do:** either provision a transport, or set `DIGEST_DISABLED=1` (drops and says
so). Never let a summary claim delivery it did not perform.

### 9.7 `list_prefix` truncation returns wrong-but-plausible data
Caps at 10,000 keys, lexicographic. `peer_runs.latest_run_ts()` returned a **May**
run as "latest" in August. A consumer would forward three-month-old recommendations
as current. Truncation that yields plausible data is worse than an error.
**Do:** `list_child_prefixes()`; and filter run dirs strictly to `YYYYmmddTHHMMSSZ`
— debris like `rundir-…` and `test-…` sorts *after* real timestamps.

### 9.8 Docker credsStore points at a Windows .exe that WSL cannot run
`~/.docker/config.json` written by Docker Desktop contains
`{"credsStore": "desktop.exe"}`. That delegates every registry interaction to
`/usr/bin/docker-credential-desktop.exe` — a **Windows** binary. On a distro with
interop disabled it dies with:

```
error getting credentials - err: fork/exec /usr/bin/docker-credential-desktop.exe:
exec format error
```

This blocks even pulls of **public** images (`postgres:15`) that need no auth, so
`docker compose up` fails and the deployer's `ensure_running` step reports the
site as unhealthy. It looks like a network or registry problem; it is neither.
**Do:** drop the `credsStore` key (back the file up first). Plaintext `auths` in
the same file still work, and `az acr login` writes its own entry when a private
registry is actually needed. Verify with `docker compose pull <public-service>`.

### 9.9 A unit that interpolates an env var nothing sets
`digest-rollup-agent`'s manifest `entry_command` contained
`python3 ${RA_REPO_ROOT}/agents/.../agent.py`, but nothing ever defined
`RA_REPO_ROOT`. systemd expands the unset variable to an empty string, so the
command resolved to `/agents/.../agent.py` and exited `status=2`. The unit was
misdiagnosed for a long time as a mail/relay problem because the agent's PURPOSE
is email — the journal line that actually mattered was
`Referenced but unset environment variable evaluates to an empty string`.
**Do:** grep manifests for `${...}` in `entry_command` and confirm each name is
defined in `secrets.env` (which every unit sources via `EnvironmentFile`). When
an agent fails, read the journal for the *unset variable* warning before
assuming the failure relates to what the agent does.

### 9.10 Secrets inlined into systemd units and manifests
Agent units carry `Environment="AZURE_STORAGE_CONNECTION_STRING=…AccountKey=…"`
literally, and at least 8 manifest `entry_command` strings embed full production
Postgres DSNs **including passwords**. Those manifests are committed to the site
repos, and systemd echoes the command line into `journalctl` on every timer fire
— so the credentials are in git history and in the system journal.
**Do:** reference `EnvironmentFile=` and `DATABASE_URL_<SITE>` instead of
inlining. On a rebuild, treat any credential that has been through a repo or a
journal as compromised and rotate it.

### 9.11 A watchdog for an image that was never built
`specpicks-scraper-watchdog` runs `docker run … specpicks-scraper:latest` every
5 minutes. On a fresh host that image does not exist and is in no registry, so
every tick fails `exit 125` with `pull access denied … repository does not
exist`. It reads like an auth problem; it is a missing local build.
**Do:** build every locally-referenced image during provisioning
(`docker build -t <name>:latest <ctx>`, or `docker compose build`), and prefer
compose service names over bare `docker run` so the image is built by definition.

### 9.12 `systemctl --user disable` does not stick
Disabling an agent's timer by hand is reverted by the framework, which re-applies
timers from the registry/manifest. Measured 2026-08-17: a manual
`systemctl --user disable agent-digest-rollup-agent.timer` was undone **23
seconds later** — the journal shows `Stopped` then `Started` with no operator
action in between. An operator who disables a noisy agent and walks away will
find it running, and will not know why.
**Do:** disable at the MANIFEST (`"enabled": false`, or an empty `cron_expr` for
manual-only) and re-register. Verify by waiting ~60s and re-checking
`systemctl --user is-enabled`. The same applies to any timer property you tune by
hand — the manifest is the source of truth, systemd is downstream of it.

### 9.13 Host-specific assumptions
Hardcoded `/home/voidsstr/...` paths, `docker exec` into named containers,
`localhost` ports, EDT-vs-UTC confusion (the host reports EDT; ticks reason in
UTC — convert before calling anything "overdue"). Grep for all of these before
trusting a new host.

### 9.14 Shared API quotas across agents
`kitchen-scraper` and `product-hydration` draw on **one** Amazon Creators account
quota. Raising one agent's per-run cap in isolation triggered `HTTP 429
ThrottleException`, and because a 429 raised rather than backing off, the whole run
aborted and discarded completed work.
**Do:** treat per-run caps as a shared budget; back off and retry on 429; return
partial results rather than throwing the batch away.

### 9.15 Goals that look configured but never record
Goal auto-tracking binds on the goal's **top-level `target_metric`**, not
`metric.name`. Goals seeded with only `metric.name` sit at zero forever looking
healthy. Two further failures in the same pass: a `target_metric` naming a key the
agent never emits, and metrics nested inside a sub-dict (the tracker does a flat
lookup).
**Do:** bind to a key you have confirmed in that agent's real `RunResult.metrics`,
then **run the agent** and check `progress_history` is non-empty.

### 9.16 Agents may fabricate credentials to defeat safety guards
An SEO agent invented a "Registered Dietitian" — bio page, Person JSON-LD,
`reviewed_by` row — purely to satisfy a credential regex and switch off the site's
own YMYL noindex guard on cardiovascular content. Another added a false first-hand
product-testing claim to a monetized guide.
**Do:** review agent commits for invented people, credentials, or experience
claims before pushing. Reverting the commit is not enough — these also write to the
production DB.

---

## 10. Keep this current

When provisioning or operating turns up a new way the fleet half-works:

1. Add it to **§9** with the symptom, the root cause, and the fix — symptom first,
   because the next person meets the symptom.
2. If it is preventable in code, fix it in `install/standup-fleet-host.sh` so the
   next rebuild cannot hit it, and say so in the trap entry.
3. If it needs a credential or a human decision, add it to the **§5 matrix** with
   its failure mode (loud vs silent).

A trap that is only in someone's memory will be paid for again.
