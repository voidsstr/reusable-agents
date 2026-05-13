#!/usr/bin/env bash
# Install + start `copilot-api` — a local OpenAI-compatible proxy in front of
# GitHub Copilot's chat backend. Once running, the reusable-agents framework's
# `_CopilotClient` (kind=copilot) routes through it.
#
# Why this matters: GitHub Copilot Pro has its own quota, separate from
# Anthropic Max accounts. Heavy agents (article-author, benchmark-research,
# competitor-research) can flip provider to "copilot" and stop competing for
# the pool's 5h Max budget.
#
# Models available through Copilot Pro (when this proxy is up):
#   - gpt-4o, gpt-4o-mini   (unlimited under Pro)
#   - claude-sonnet-4, claude-3.7-sonnet, claude-3.5-sonnet
#   - o1-preview, o1-mini
#   - gemini-2.0-flash
#
# Usage:
#   bash setup-copilot-api.sh           # install + auth + start (idempotent)
#   bash setup-copilot-api.sh --status  # show what's running
#   bash setup-copilot-api.sh --stop    # stop the proxy
#   bash setup-copilot-api.sh --logs    # tail logs
#
# Runs as a user-level systemd service — survives logout, autorestart on crash.

set -euo pipefail

PROXY_PORT="${COPILOT_API_PORT:-4141}"
UNIT_NAME="copilot-api.service"
UNIT_DIR="$HOME/.config/systemd/user"
LOG_FILE="/tmp/copilot-api.log"
NPM_PREFIX="$HOME/.npm-global"

log() { printf '[setup-copilot-api] %s\n' "$*" >&2 ; }

case "${1:-}" in
    --status)
        systemctl --user status "$UNIT_NAME" --no-pager 2>&1 | head -10 || true
        echo
        echo "Listening on :$PROXY_PORT?"
        ss -tlnp 2>/dev/null | grep -E ":$PROXY_PORT\s" || echo "  no — not listening"
        echo
        echo "Quick test:"
        curl -sf --max-time 5 "http://localhost:$PROXY_PORT/v1/models" \
            | python3 -c "import sys,json; m=json.load(sys.stdin); print(f'  models available: {len(m.get(\"data\",[]))}')" \
            || echo "  no response (proxy down or auth not done)"
        exit 0
        ;;
    --stop)
        systemctl --user stop "$UNIT_NAME" 2>/dev/null || true
        log "stopped"
        exit 0
        ;;
    --logs)
        tail -n 80 "$LOG_FILE" 2>/dev/null || journalctl --user -u "$UNIT_NAME" -n 80 --no-pager
        exit 0
        ;;
esac

# ── 1. Ensure node + npm available ────────────────────────────────────────
if ! command -v npm >/dev/null 2>&1; then
    log "ERROR: npm not found. Install node.js first:"
    log "  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -"
    log "  sudo apt install -y nodejs"
    exit 2
fi

# Use a userland npm prefix so we don't need sudo
mkdir -p "$NPM_PREFIX"
npm config set prefix "$NPM_PREFIX" >/dev/null 2>&1
export PATH="$NPM_PREFIX/bin:$PATH"

# ── 2. Install copilot-api if missing ─────────────────────────────────────
if ! command -v copilot-api >/dev/null 2>&1; then
    log "installing copilot-api globally (user prefix: $NPM_PREFIX)"
    npm install -g copilot-api
else
    log "copilot-api already installed: $(copilot-api --version 2>&1 | head -1)"
fi

if ! command -v copilot-api >/dev/null 2>&1; then
    log "ERROR: copilot-api still not on PATH after install"
    log "  add this to your shell profile: export PATH=\"$NPM_PREFIX/bin:\$PATH\""
    exit 3
fi

COPILOT_API_BIN="$(command -v copilot-api)"

# ── 3. Auth: opens browser to GitHub OAuth (one-time) ─────────────────────
# copilot-api stores auth in $HOME/.local/share/copilot-api/ or similar.
AUTH_DIR_CANDIDATES=(
    "$HOME/.local/share/copilot-api"
    "$HOME/.config/copilot-api"
    "$HOME/.copilot-api"
)
AUTHED=0
for d in "${AUTH_DIR_CANDIDATES[@]}"; do
    if [ -f "$d/github-token.json" ] || [ -f "$d/auth.json" ]; then
        log "found existing copilot-api auth at $d"
        AUTHED=1
        break
    fi
done

if [ "$AUTHED" = "0" ]; then
    log "authenticating with GitHub Copilot — a device-flow code will appear"
    log "  the script will print a code; visit https://github.com/login/device, paste the code"
    log ""
    "$COPILOT_API_BIN" auth || {
        log "auth failed. You can re-run: $COPILOT_API_BIN auth"
        log "Then re-run this script to install the systemd unit."
        exit 4
    }
fi

# ── 4. systemd --user unit so it stays up across reboots ─────────────────
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$UNIT_NAME" <<UNIT
[Unit]
Description=GitHub Copilot API proxy (OpenAI-compatible) on :$PROXY_PORT
After=network.target

[Service]
Type=simple
Environment=PATH=$NPM_PREFIX/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$COPILOT_API_BIN start --port $PROXY_PORT
Restart=always
RestartSec=5
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
UNIT

# enable lingering so it survives logout
loginctl enable-linger "$USER" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

log "waiting for proxy to come up on :$PROXY_PORT"
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf --max-time 3 "http://localhost:$PROXY_PORT/v1/models" >/dev/null 2>&1; then
        log "✓ proxy is up"
        break
    fi
    sleep 2
done

# Smoke test
log "model list:"
curl -sf --max-time 5 "http://localhost:$PROXY_PORT/v1/models" 2>/dev/null \
    | python3 -c "
import sys, json
m = json.load(sys.stdin).get('data', [])
for x in m[:25]:
    print(f'  - {x.get(\"id\")}')" \
    || log "  (couldn't reach proxy — check: $LOG_FILE)"

cat <<EOF

──────────────────────────────────────────────────────────────────────
✓ copilot-api running

Proxy URL : http://localhost:$PROXY_PORT/v1
Service   : $UNIT_NAME (systemd --user, auto-restart)
Logs      : $LOG_FILE
Status    : bash $(realpath "$0") --status
Stop      : bash $(realpath "$0") --stop

──── Flip a rate-limited agent to use Copilot ────────────────────────

The framework already has the 'copilot' provider registered. To route
an agent through it instead of claude-cli/anthropic, PATCH its
agent_overrides entry. Example for aisleprompt-article-author-agent:

  curl -X POST -H "Authorization: Bearer \$FRAMEWORK_API_TOKEN" \\
    -H "Content-Type: application/json" \\
    http://localhost:8093/api/providers/agent-override \\
    -d '{"agent_id":"aisleprompt-article-author-agent","provider":"copilot","model":"claude-sonnet-4"}'

Or via the dashboard's Providers page (http://localhost:8091/providers)
— pick the agent, set provider=copilot, model=claude-sonnet-4 (or
gpt-4o for unlimited Pro budget).

Recommended assignments to ease pool pressure:
  - aisleprompt-article-author-agent  → copilot/claude-sonnet-4
  - specpicks-benchmark-research      → copilot/gpt-4o (high-volume)
  - reusable-agents-competitor-research-agent → already copilot? check
  - leave specpicks-article-author + h2h on claude-cli (they're working)

──── How rate limits compare ──────────────────────────────────────────

Anthropic Max (current pool):
  ~225 messages / 5h rolling window per account, 3 accounts authed
Copilot Pro (after this is up):
  300 premium-model calls/MONTH (Sonnet 4, Claude 3.7, o1)
  UNLIMITED GPT-4o
  No 5h rolling window
──────────────────────────────────────────────────────────────────────
EOF
