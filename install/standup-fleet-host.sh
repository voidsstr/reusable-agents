#!/usr/bin/env bash
# standup-fleet-host.sh — bring a NEW machine up as THE reusable-agents fleet host.
#
# This is the script that rebuilds the host after a machine dies, a drive is
# replaced, or the fleet is migrated. It is the source of truth for "what a
# fleet host IS" — if you do something by hand to make the fleet work, it
# belongs in here.
#
# Phased, like install/deploy-azure.sh + install/deploy-aws.sh:
#
#   bash install/standup-fleet-host.sh preflight    # host capability check (read-only)
#   bash install/standup-fleet-host.sh repos        # clone/refresh the site repos at $DEV_ROOT
#   bash install/standup-fleet-host.sh deps         # python + node deps into the user site
#   bash install/standup-fleet-host.sh secrets      # recover what's recoverable -> secrets.env
#   bash install/standup-fleet-host.sh api          # framework API on :8090 as a user service
#   bash install/standup-fleet-host.sh register     # register every agent -> writes systemd timers
#   bash install/standup-fleet-host.sh spine        # host-worker + auto-queue-drainer
#   bash install/standup-fleet-host.sh verify       # prove it: timers, API, publish pulse
#   bash install/standup-fleet-host.sh all          # every phase in order
#
# IDEMPOTENT. Re-running is the normal way to converge; nothing is destructive.
#
# What this script CANNOT do (by design — they need a human):
#   * interactive browser logins  -> install/recover-credentials.sh
#   * anything needing sudo       -> printed as an operator checklist
#   * the Windows-side boot task  -> printed as an operator checklist
#
# See docs/fleet-host-standup.md for the narrative + the operator checklist.

set -uo pipefail

# ── Configuration ───────────────────────────────────────────────────────────
# DEV_ROOT is THE path contract. Every manifest entry_command, every registry
# repo_dir, scheduler.py's EnvironmentFile, and the KTLO systems registry all
# hardcode /home/voidsstr/development/... Keep it unless you enjoy rewriting
# 68 manifests. It must be on a LOCAL POSIX filesystem (ext4) — not /mnt/c
# (9p, ~95x slower on small writes) and not an SMB/NFS share (no reliable
# POSIX locking; a NAS blip fails every agent at once).
DEV_ROOT="${DEV_ROOT:-$HOME/development}"
REPO_DIR="${REPO_DIR:-$DEV_ROOT/reusable-agents}"
STATE_DIR="${STATE_DIR:-$HOME/.reusable-agents}"
SECRETS_FILE="$STATE_DIR/secrets.env"
LOG_DIR="${LOG_DIR:-/tmp/reusable-agents-logs}"

# Azure: the fleet's subscription is NOT the CLI default on a shared box, so
# every az call must pass --subscription explicitly.
AZ_SUB="${AZ_SUB:-125b8bc9-e8bb-4827-9d2a-f3492b983dcf}"
AZ_RG="${AZ_RG:-nsc-apps}"
AZ_CONTAINERAPP="${AZ_CONTAINERAPP:-agents}"
AZ_STORAGE_ACCOUNT="${AZ_STORAGE_ACCOUNT:-nscagentstorage}"

API_PORT="${API_PORT:-8090}"
API_UNIT="reusable-agents-api.service"
WORKER_UNIT="reusable-agents-host-worker.service"
DRAINER_UNIT="auto-queue-drainer.service"

# Repos the fleet needs: <name>:<branch>  (empty branch = remote default)
#
# ORDER MATTERS for the `register` phase: registration is last-write-wins, and
# several agent ids exist in TWO repos (agent-doctor + responder-agent live in
# both reusable-agents and nsc-assistant; nsc-assistant's copies are stale
# forks with an EMPTY cron_expr). The framework repo owns the shared agents, so
# it must register LAST or those agents end up registered with no schedule and
# silently never fire.
REPOS=(
    "aisleprompt:"
    "specpicks:master"
    "nsc-assistant:master"
    "reusable-agents:main"
)
GIT_HOST="${GIT_HOST:-git@github.com:voidsstr}"

# ── Pretty printing ─────────────────────────────────────────────────────────
bold()    { printf "\033[1m%s\033[0m\n" "$*"; }
green()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
yellow()  { printf "  \033[33m!\033[0m %s\n" "$*"; }
red()     { printf "  \033[31m✗\033[0m %s\n" "$*" >&2; }
section() { echo; bold "── $* ──"; }

GAPS=()
gap() { GAPS+=("$1"); yellow "$1"; }

have() { command -v "$1" >/dev/null 2>&1; }
az_() { az "$@" --subscription "$AZ_SUB" 2>/dev/null; }

# ── phase: preflight ────────────────────────────────────────────────────────
phase_preflight() {
    section "[preflight] host capability check"

    [ "$(ps -p 1 -o comm=)" = "systemd" ] && green "systemd is init ($(systemctl --version | head -1))" \
        || gap "systemd is NOT init — add '[boot] systemd=true' to /etc/wsl.conf and restart the distro"

    if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = "yes" ]; then
        green "linger enabled (user units survive logout)"
    else
        gap "linger OFF — run: loginctl enable-linger $USER"
    fi

    local fstype avail
    fstype=$(stat -f -c %T "$(dirname "$DEV_ROOT")" 2>/dev/null)
    avail=$(df -h "$(dirname "$DEV_ROOT")" 2>/dev/null | tail -1 | awk '{print $4}')
    case "$fstype" in
        ext2/ext3|ext4|xfs|btrfs) green "DEV_ROOT filesystem=$fstype avail=$avail" ;;
        v9fs|9p|cifs|nfs*) gap "DEV_ROOT is on $fstype — the fleet needs a local POSIX fs; move DEV_ROOT to ext4" ;;
        *) yellow "DEV_ROOT filesystem=$fstype (unrecognised) avail=$avail" ;;
    esac

    have python3 && green "python3 $(python3 --version 2>&1 | awk '{print $2}')" || gap "python3 missing"
    have node    && green "node $(node --version)" || gap "node missing (site builds + playwright need it)"
    have git     && green "git $(git --version | awk '{print $3}')" || gap "git missing"
    have az      && green "az cli present" || gap "az cli missing — credential recovery + deploys need it"
    have gh      && green "gh cli present" || gap "gh cli missing (repo clones fall back to plain git)"

    if [ -n "$(git config --global user.email)" ]; then
        green "git identity: $(git config --global user.name) <$(git config --global user.email)>"
    else
        gap "git identity UNSET — implementer/release_tagger commits will abort. git config --global user.email ..."
    fi

    if sudo -n true 2>/dev/null; then
        green "passwordless sudo available"
    else
        yellow "no passwordless sudo — apt installs are an operator step (see docs/fleet-host-standup.md)"
    fi

    have msmtp && green "msmtp present (operator email works)" \
        || gap "msmtp MISSING — every KTLO escalation + operator alert fails silently. sudo apt-get install -y msmtp msmtp-mta"

    if az_ account show >/dev/null 2>&1; then
        green "az can reach subscription $AZ_SUB"
    else
        gap "az cannot reach $AZ_SUB — run: az login (then re-run 'secrets')"
    fi

    for p in "$API_PORT" 8091 4141 7861; do
        if ss -ltn 2>/dev/null | grep -q ":$p "; then yellow "port $p already in use"; fi
    done

    have nvidia-smi && green "GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)" || true
    curl -sf --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
        && green "ollama reachable on 127.0.0.1:11434" \
        || yellow "ollama not reachable locally — OLLAMA_HOST must point somewhere real"
}

# ── phase: repos ────────────────────────────────────────────────────────────
phase_repos() {
    section "[repos] site repos at $DEV_ROOT"
    mkdir -p "$DEV_ROOT"
    for spec in "${REPOS[@]}"; do
        local name="${spec%%:*}" branch="${spec##*:}" dir="$DEV_ROOT/${spec%%:*}"
        if [ -d "$dir/.git" ]; then
            local head; head=$(git -C "$dir" log -1 --format='%h %cd' --date=short 2>/dev/null)
            green "$name present ($head)"
            continue
        fi
        echo "  cloning $name…"
        if [ -n "$branch" ]; then
            git clone --quiet --branch "$branch" "$GIT_HOST/$name.git" "$dir" 2>&1 | tail -1
        else
            git clone --quiet "$GIT_HOST/$name.git" "$dir" 2>&1 | tail -1
        fi
        [ -d "$dir/.git" ] && green "$name cloned" || gap "$name FAILED to clone from $GIT_HOST/$name.git"
    done

    # Agents resolve site configs relative to these; a missing agents/ tree
    # means that repo's agents silently never register.
    for spec in "${REPOS[@]}"; do
        local name="${spec%%:*}" dir="$DEV_ROOT/${spec%%:*}" n
        n=$(ls -d "$dir"/agents/*/manifest.json 2>/dev/null | wc -l)
        [ "$n" -gt 0 ] && green "$name: $n agent manifests" || yellow "$name: no agent manifests (ok for a pure site repo)"
    done
}

# ── phase: deps ─────────────────────────────────────────────────────────────
phase_deps() {
    section "[deps] python deps into the user site"
    # Convention: agents invoke bare `python3`, so deps live in ~/.local, NOT a
    # venv. Ubuntu 24.04 marks the interpreter externally-managed (PEP 668);
    # --user never touches system packages, so we override the marker.
    mkdir -p "$HOME/.config/pip"
    if ! grep -q 'break-system-packages' "$HOME/.config/pip/pip.conf" 2>/dev/null; then
        cat > "$HOME/.config/pip/pip.conf" <<'PIPCONF'
# reusable-agents fleet host: agents invoke bare `python3`, so all framework +
# agent deps live in the user site (~/.local/lib/python3.x/site-packages).
# PEP 668 marks the system interpreter externally-managed; --user installs
# never touch system packages, so we override the marker rather than adopt a
# venv (a venv would require rewriting every manifest entry_command).
[global]
user = true
break-system-packages = true
PIPCONF
        green "wrote ~/.config/pip/pip.conf (user-site convention)"
    else
        green "pip.conf already set for user-site installs"
    fi

    python3 -m pip install --quiet -r "$REPO_DIR/framework/api/requirements.txt" 2>&1 | tail -2
    # Union of what the agents import beyond the framework baseline.
    python3 -m pip install --quiet \
        beautifulsoup4 lxml feedparser croniter anthropic openai python-dateutil psycopg2-binary 2>&1 | tail -2

    python3 - <<'PY'
import importlib
mods = ['fastapi','uvicorn','pydantic','yaml','jsonschema','azure.storage.blob','httpx',
        'websockets','authlib','itsdangerous','starlette','bs4','lxml','feedparser',
        'croniter','psycopg2','requests','anthropic','openai','dateutil']
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        bad.append(m)
print("  \033[32m✓\033[0m all python deps import" if not bad
      else "  \033[31m✗\033[0m MISSING: " + ", ".join(bad))
PY
    mkdir -p "$LOG_DIR" && green "log dir $LOG_DIR"
}

# ── phase: secrets ──────────────────────────────────────────────────────────
phase_secrets() {
    section "[secrets] recover what's recoverable -> $SECRETS_FILE"
    mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
    bash "$(dirname "$0")/recover-credentials.sh" harvest
    phase_claude_pool
}

# ── phase: claude-pool shim ─────────────────────────────────────────────────
# The implementer decides whether Opus authoring is possible by EXECUTING
# $POOL_DIR/bin/claude. A missing shim fails that probe in a way that is
# indistinguishable from a dead pool: it sets IMPLEMENTER_FORCE_FALLBACK=1,
# caches that verdict for 15 minutes, and defers every article/news/h2h batch
# with "required model claude-opus-5 unavailable" -- while `claude --model
# claude-opus-5` works perfectly from the shell, which makes it look like an
# Anthropic-side outage rather than a missing file.
#
# This host had exactly that: profiles created by add-claude-profile.sh, no
# shim, and a full day of zero publish volume on 2026-08-13. Creating the
# shim is idempotent and costs nothing, so it runs on every standup.
phase_claude_pool() {
    section "[claude-pool] shim (implementer's Opus availability probe)"
    local pool_dir="$STATE_DIR/claude-pool"
    mkdir -p "$pool_dir/bin"
    cat > "$pool_dir/bin/claude" <<EOF
#!/usr/bin/env bash
# Auto-generated claude-pool shim -- do not edit; standup rewrites it.
exec python3 $REPO_DIR/framework/cli/claude_pool.py exec -- "\$@"
EOF
    chmod +x "$pool_dir/bin/claude"
    green "shim at $pool_dir/bin/claude"

    local authed
    authed=$(python3 - "$pool_dir/state.json" <<'PY' 2>/dev/null || echo 0
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(0); raise SystemExit
print(sum(1 for k, v in d.items()
          if not k.startswith("__") and v.get("authenticated")))
PY
)
    if [ "${authed:-0}" -gt 0 ]; then
        green "$authed authenticated profile(s)"
        # Prove the shim actually rotates to a live profile. The implementer
        # greps for this exact "-> profile-N (home=" line, so if it is absent
        # the probe fails no matter how healthy the accounts are.
        #
        # Capture to a variable rather than piping into `grep -q`: this script
        # runs under `set -o pipefail`, and grep -q exits at the first match,
        # closing the pipe so claude dies of SIGPIPE. pipefail then propagates
        # that non-zero status and a SUCCESSFUL probe reports as failed.
        local probe_out
        probe_out=$(timeout 60 "$pool_dir/bin/claude" \
            --dangerously-skip-permissions --print --output-format text \
            --model claude-haiku-4-5 --max-turns 1 'ping' </dev/null 2>&1) || true
        if printf '%s' "$probe_out" \
                | grep -qE '^\[claude-pool\] → profile-[0-9]+ \(home='; then
            green "shim probe OK — Opus authoring can dispatch"
        else
            gap "shim probe FAILED — implementer will defer all Opus batches"
            printf '      probe said: %s\n' "$(printf '%s' "$probe_out" | head -1)"
        fi
    else
        gap "no authenticated profiles — run install/add-claude-profile.sh in a REAL terminal"
    fi
    # A stale "pool is dead" verdict outlives the fix by up to 15 min.
    rm -f /tmp/claude-pool-disabled-probe 2>/dev/null || true
}

# ── phase: api ──────────────────────────────────────────────────────────────
# Registration POSTs to a LOCAL framework API, which is also what writes the
# systemd units — so the API must run ON the host (not in docker, which has no
# access to ~/.config/systemd/user). docker is frequently unavailable in WSL,
# so we run uvicorn directly as a user service.
phase_api() {
    section "[api] framework API on 127.0.0.1:$API_PORT"
    local unit="$HOME/.config/systemd/user/$API_UNIT"
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<UNIT
[Unit]
Description=reusable-agents framework API (registry, status, triggers, timer writer)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=PYTHONPATH=$REPO_DIR
Environment=STORAGE_BACKEND=azure
EnvironmentFile=-$SECRETS_FILE
# $LOG_DIR is under /tmp, wiped on reboot; systemd will not create the
# parent of an append: target and fails the unit at 209/STDOUT.
ExecStartPre=-/bin/mkdir -p $LOG_DIR
ExecStart=/usr/bin/python3 -m uvicorn framework.api.app.main:app --host 127.0.0.1 --port $API_PORT
Restart=always
RestartSec=5
StandardOutput=append:$LOG_DIR/framework-api.log
StandardError=append:$LOG_DIR/framework-api.log

[Install]
WantedBy=default.target
UNIT
    green "wrote $unit"
    systemctl --user daemon-reload
    systemctl --user enable --now "$API_UNIT" >/dev/null 2>&1
    for _ in $(seq 1 20); do
        curl -sf --max-time 3 "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1 && break
        sleep 1
    done
    if curl -sf --max-time 5 "http://127.0.0.1:$API_PORT/api/health" 2>/dev/null | grep -q '"status"'; then
        green "API healthy: $(curl -s --max-time 5 http://127.0.0.1:$API_PORT/api/health)"
    else
        red "API did not come up — tail $LOG_DIR/framework-api.log"
        gap "framework API failed to start on :$API_PORT"
    fi
}

# ── phase: register ─────────────────────────────────────────────────────────
# Registration is a RE-REGISTRATION FROM THE REPOS, deliberately — the blob
# registry drifts (it carries agents that were deleted and misses ones that
# were added). The repos are the source of truth for what exists.
phase_register() {
    section "[register] register every agent (writes systemd timers)"
    # shellcheck disable=SC1090
    [ -f "$SECRETS_FILE" ] && { set -a; . "$SECRETS_FILE"; set +a; }
    export FRAMEWORK_API_URL="http://127.0.0.1:$API_PORT"
    for spec in "${REPOS[@]}"; do
        local name="${spec%%:*}" dir="$DEV_ROOT/${spec%%:*}/agents"
        [ -d "$dir" ] || continue
        echo "  · $name"
        bash "$REPO_DIR/install/register-all-from-dir.sh" "$dir" 2>&1 | tail -3
    done
    local n; n=$(systemctl --user list-timers --all --no-legend 2>/dev/null | grep -c 'agent-')
    green "$n agent timers installed"

    # Surface duplicate ids + any enabled agent that ended up with no schedule.
    # Both are silent killers: the agent looks registered but never fires.
    python3 - "$DEV_ROOT" <<'PY'
import glob, json, sys, collections
seen = collections.defaultdict(list)
for f in glob.glob(f"{sys.argv[1]}/*/agents/*/manifest.json"):
    try:
        d = json.load(open(f, encoding='utf-8'))
    except Exception:
        continue
    if d.get('id'):
        seen[d['id']].append((f.split('/development/')[-1], d.get('cron_expr', '')))
dups = {k: v for k, v in seen.items() if len(v) > 1}
if dups:
    print("  \033[33m!\033[0m duplicate agent ids across repos (last registration wins):")
    for k, v in sorted(dups.items()):
        for p, c in v:
            print(f"      {k:38s} cron={c or '(none)':16s} {p}")
PY
    # shellcheck disable=SC1090
    [ -f "$SECRETS_FILE" ] && { set -a; . "$SECRETS_FILE"; set +a; }
    curl -sf --max-time 20 -H "Authorization: Bearer ${FRAMEWORK_API_TOKEN:-}" \
        "http://127.0.0.1:$API_PORT/api/agents" 2>/dev/null | python3 -c "
import json,sys
try: a=json.load(sys.stdin)
except Exception: sys.exit(0)
a = a if isinstance(a,list) else a.get('agents',[])
bad=[x['id'] for x in a if x.get('enabled') and not x.get('cron_expr') and x.get('id') not in ('implementer','deployer','agent-doctor')]
if bad: print('  \033[33m!\033[0m enabled but NO cron (will never fire): ' + ', '.join(sorted(bad)))
"
}

# ── phase: spine ────────────────────────────────────────────────────────────
# The three always-on units. Without the drainer nothing the producers queue is
# ever implemented — the fleet looks alive and ships nothing.
phase_spine() {
    section "[spine] host-worker + auto-queue-drainer"

    bash "$REPO_DIR/install/install-host-worker.sh" >/dev/null 2>&1 && green "host-worker installed" \
        || gap "host-worker install failed"

    # The drainer refuses to start without the responder config — it reads the
    # implementer dispatcher path from it. The IMAP half is inert until the
    # operator completes the mail OAuth, but the drainer half works today.
    local rcfg="$STATE_DIR/responder/config.yaml"
    if [ ! -f "$rcfg" ]; then
        mkdir -p "$(dirname "$rcfg")"
        sed -e "s|/home/voidsstr/development/reusable-agents|$REPO_DIR|g" \
            "$REPO_DIR/agents/responder-agent/config.example.yaml" > "$rcfg"
        chmod 600 "$rcfg"
        green "seeded $rcfg from config.example.yaml (imap block still needs OAuth)"
    else
        green "responder config present"
    fi

    local unit="$HOME/.config/systemd/user/$DRAINER_UNIT"
    cat > "$unit" <<UNIT
[Unit]
Description=reusable-agents auto-queue drainer (fires the implementer when recs are queued)
After=network-online.target $API_UNIT
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=PYTHONPATH=$REPO_DIR
Environment=STORAGE_BACKEND=azure
EnvironmentFile=-$SECRETS_FILE
# $LOG_DIR is under /tmp, wiped on reboot; systemd will not create the
# parent of an append: target and fails the unit at 209/STDOUT.
ExecStartPre=-/bin/mkdir -p $LOG_DIR
ExecStart=/usr/bin/python3 -m framework.cli.auto_queue_drainer --interval 15 --idle-backoff 60
Restart=always
RestartSec=10
StandardOutput=append:$LOG_DIR/auto-queue-drainer.log
StandardError=append:$LOG_DIR/auto-queue-drainer.log

[Install]
WantedBy=default.target
UNIT
    green "wrote $unit"
    systemctl --user daemon-reload
    systemctl --user enable --now "$DRAINER_UNIT" >/dev/null 2>&1
    sleep 2
    [ "$(systemctl --user is-active $DRAINER_UNIT)" = "active" ] && green "drainer active" \
        || gap "drainer not active — tail $LOG_DIR/auto-queue-drainer.log"
}

# ── phase: verify ───────────────────────────────────────────────────────────
phase_verify() {
    section "[verify] prove the fleet is actually alive"
    # shellcheck disable=SC1090
    [ -f "$SECRETS_FILE" ] && { set -a; . "$SECRETS_FILE"; set +a; }

    local timers failed
    timers=$(systemctl --user list-timers --all --no-legend 2>/dev/null | grep -c 'agent-')
    failed=$(systemctl --user list-units --state=failed --no-legend 2>/dev/null | grep -c 'agent-')
    echo "  timers=$timers  failed-units=$failed"
    [ "$timers" -gt 0 ] && green "$timers agent timers scheduled" || gap "NO agent timers — registration did not take"
    [ "$failed" -eq 0 ] && green "no failed agent units" || yellow "$failed failed agent units (triage with the KTLO skill)"

    for u in "$API_UNIT" "$WORKER_UNIT" "$DRAINER_UNIT"; do
        printf "  %-42s %s\n" "$u" "$(systemctl --user is-active "$u" 2>/dev/null)"
    done

    # Storage is the single point everything depends on — prove it round-trips.
    STORAGE_BACKEND=azure PYTHONPATH="$REPO_DIR" python3 - <<'PY'
import sys
try:
    from framework.core.storage import get_storage
    s = get_storage()
    n = len(s.list_prefix('registry/') or [])
    print(f"  \033[32m✓\033[0m storage backend reachable ({n} keys under registry/)")
except Exception as e:
    print(f"  \033[31m✗\033[0m storage backend FAILED: {type(e).__name__}: {e}")
PY

    # The pulse the KTLO skill watches: are the sites publishing again?
    python3 - <<'PY'
import os, datetime
try:
    import psycopg2
except ImportError:
    raise SystemExit("  ! psycopg2 missing")
for site, var in (("aisleprompt","DATABASE_URL_AISLEPROMPT"), ("specpicks","DATABASE_URL_SPECPICKS")):
    dsn = os.environ.get(var)
    if not dsn:
        print(f"  ! {site}: {var} not set — publish pulse unknown"); continue
    try:
        c = psycopg2.connect(dsn, connect_timeout=15); cur = c.cursor()
        cur.execute("select count(*) filter (where created_at > now() - interval '24 hours'), max(created_at) from editorial_articles")
        n, mx = cur.fetchone(); c.close()
        print(f"  · {site}: +24h={n} newest={mx}")
    except Exception as e:
        print(f"  ! {site}: {type(e).__name__}: {str(e)[:80]}")
PY
}

# ── driver ──────────────────────────────────────────────────────────────────
main() {
    local phase="${1:-all}"
    case "$phase" in
        preflight) phase_preflight ;;
        repos)     phase_repos ;;
        deps)      phase_deps ;;
        secrets)   phase_secrets ;;
        claude-pool) phase_claude_pool ;;
        api)       phase_api ;;
        register)  phase_register ;;
        spine)     phase_spine ;;
        verify)    phase_verify ;;
        all)
            phase_preflight; phase_repos; phase_deps; phase_secrets
            phase_api; phase_register; phase_spine; phase_verify ;;
        *) echo "usage: $0 {preflight|repos|deps|secrets|api|register|spine|verify|all}" >&2; exit 2 ;;
    esac

    if [ ${#GAPS[@]} -gt 0 ]; then
        section "OPERATOR ACTION REQUIRED (${#GAPS[@]})"
        printf '  • %s\n' "${GAPS[@]}"
        echo
        echo "  Interactive credential recovery: bash install/recover-credentials.sh status"
    fi
}

main "$@"
