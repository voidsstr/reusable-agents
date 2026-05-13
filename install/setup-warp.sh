#!/usr/bin/env bash
# Install + configure Cloudflare WARP as a local SOCKS5 proxy on :40000,
# then wire it into the claude-pool's proxies.conf.
#
# WARP gives one alternate egress IP (Cloudflare's edge). Effect:
# implementer + batched-LLM agents stop competing with the interactive
# Claude Code session for the home IP's per-IP burst limit at Anthropic.
#
# Usage:
#   sudo bash setup-warp.sh                  # install + register + connect
#   sudo bash setup-warp.sh --uninstall      # tear it down
#
# Idempotent — safe to re-run. Detects existing WARP install + skips.
#
# What this gives you (one alt IP):
#   - Defense against 2026-05-02-style IP-burst incidents (parallel
#     dispatches from one IP tripping Anthropic's edge limiter).
# What this does NOT give you:
#   - More per-account capacity. The 5h Max per-account limit is the
#     dominant constraint right now. Add proxies for >1 alt IP only if
#     you authenticate more Max accounts (profile-4, profile-5).

set -euo pipefail

PROXY_PORT=40000
POOL_ROOT="${CLAUDE_POOL_ROOT:-/home/voidsstr/.reusable-agents/claude-pool}"
PROXIES_CONF="$POOL_ROOT/proxies.conf"
ASSIGN_TO_PROFILE="${ASSIGN_TO_PROFILE:-profile-1}"  # which pool profile gets WARP

require_root() {
    if [ "$EUID" -ne 0 ]; then
        echo "ERROR: must be run as root (use sudo)." >&2
        exit 1
    fi
}

log() { printf '[setup-warp] %s\n' "$*" >&2 ; }

uninstall() {
    require_root
    log "stopping WARP"
    systemctl stop warp-svc 2>/dev/null || true
    systemctl disable warp-svc 2>/dev/null || true
    log "removing package"
    apt-get remove -y cloudflare-warp 2>/dev/null || true
    rm -f /etc/apt/sources.list.d/cloudflare-client.list
    rm -f /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    log "WARP removed (proxies.conf left intact — edit manually if needed)"
}

if [ "${1:-}" = "--uninstall" ]; then
    uninstall
    exit 0
fi

require_root

# ── 1. Install Cloudflare WARP if missing ─────────────────────────────────
if ! command -v warp-cli >/dev/null 2>&1; then
    log "installing cloudflare-warp via apt"
    # Official Cloudflare repo
    install -d /usr/share/keyrings
    if [ ! -f /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg ]; then
        curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
            | gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    fi
    DISTRO_CODENAME="$(lsb_release -cs)"
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ ${DISTRO_CODENAME} main" \
        > /etc/apt/sources.list.d/cloudflare-client.list
    apt-get update
    apt-get install -y cloudflare-warp
else
    log "cloudflare-warp already installed: $(warp-cli --version 2>&1 | head -1)"
fi

# ── 2. Make sure warp-svc is running (the daemon warp-cli talks to) ──────
log "starting warp-svc daemon"
systemctl enable --now warp-svc 2>/dev/null || true
sleep 2

# warp-cli must be invoked as the user owning the WARP registration.
# When run via sudo, warp-cli auto-detects the SUDO_USER context.
WARP_USER="${SUDO_USER:-$USER}"

run_warp() {
    if [ -n "${SUDO_USER:-}" ]; then
        sudo -u "$SUDO_USER" warp-cli --accept-tos "$@"
    else
        warp-cli --accept-tos "$@"
    fi
}

# ── 3. Register if not already registered ────────────────────────────────
if run_warp account 2>&1 | grep -qE 'Missing registration|not registered|No account'; then
    log "registering with Cloudflare WARP (free tier)"
    run_warp registration new
else
    log "WARP already registered"
fi

# ── 4. Set proxy mode + port ─────────────────────────────────────────────
log "setting WARP to SOCKS5 proxy mode on 127.0.0.1:${PROXY_PORT}"
run_warp set-mode proxy 2>&1 || true
run_warp set-proxy-port "$PROXY_PORT" 2>&1 || true

# ── 5. Connect ───────────────────────────────────────────────────────────
log "connecting to WARP edge"
run_warp connect 2>&1 || true

# Wait briefly for the daemon to bring the proxy up.
for i in 1 2 3 4 5 6 7 8 9 10; do
    if (echo > /dev/tcp/127.0.0.1/${PROXY_PORT}) >/dev/null 2>&1; then
        log "✓ proxy listening on 127.0.0.1:${PROXY_PORT}"
        break
    fi
    sleep 2
done

# ── 6. Verify the egress IP differs from direct ──────────────────────────
log "verifying egress IPs"
DIRECT_IP=$(curl -s --max-time 5 https://api.ipify.org || echo "?")
WARP_IP=$(curl -s --max-time 10 --proxy "socks5h://127.0.0.1:${PROXY_PORT}" https://api.ipify.org || echo "?")
log "  direct egress IP : $DIRECT_IP"
log "  WARP   egress IP : $WARP_IP"
if [ "$DIRECT_IP" = "$WARP_IP" ]; then
    log "  ⚠ WARP IP matches direct IP — proxy may not be routing correctly"
else
    log "  ✓ WARP routing through a separate IP — good"
fi

# ── 7. Wire into proxies.conf for the assigned profile ───────────────────
if [ -f "$PROXIES_CONF" ]; then
    if ! grep -qE "^${ASSIGN_TO_PROFILE}=socks5h://127\.0\.0\.1:${PROXY_PORT}\$" "$PROXIES_CONF"; then
        log "adding ${ASSIGN_TO_PROFILE} → socks5h://127.0.0.1:${PROXY_PORT} to proxies.conf"
        # Strip any existing line for this profile
        sed -i "/^${ASSIGN_TO_PROFILE}=/d" "$PROXIES_CONF"
        echo "${ASSIGN_TO_PROFILE}=socks5h://127.0.0.1:${PROXY_PORT}" >> "$PROXIES_CONF"
        chown "$WARP_USER":"$WARP_USER" "$PROXIES_CONF" 2>/dev/null || true
    else
        log "${ASSIGN_TO_PROFILE} already wired to WARP in proxies.conf"
    fi
else
    log "WARNING: $PROXIES_CONF doesn't exist yet — claude-pool not initialized?"
    log "  After it's created, add this line manually:"
    log "    ${ASSIGN_TO_PROFILE}=socks5h://127.0.0.1:${PROXY_PORT}"
fi

cat <<EOF

──────────────────────────────────────────────────────────────────────
✓ WARP setup done

Status: warp-cli status
Egress: $WARP_IP (via WARP) vs $DIRECT_IP (direct)
Proxy:  127.0.0.1:${PROXY_PORT}
Wired:  ${ASSIGN_TO_PROFILE} → WARP in $PROXIES_CONF

To assign WARP to a different profile (or add it to a second one):
  edit $PROXIES_CONF
  add a line like:    profile-2=socks5h://127.0.0.1:${PROXY_PORT}

To verify the wiring is live, watch the next agent run's stderr for:
  [claude-via-proxy] profile=profile-N via socks5h://127.0.0.1:${PROXY_PORT}

To disconnect WARP without uninstalling:
  warp-cli disconnect

To uninstall:
  sudo bash $(realpath "$0") --uninstall
──────────────────────────────────────────────────────────────────────
EOF
