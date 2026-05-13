#!/usr/bin/env bash
# bootstrap.sh — single-command setup for a fresh reusable-agents clone.
#
# Walks the operator through:
#   1. Prereqs (docker, python, az/gh CLIs — best-effort detect)
#   2. .env creation from .env.example with prompts for each setting
#   3. Storage backend choice (local FS by default; Azure / S3 if available)
#   4. AI provider auth (claude-pool / Copilot / API keys)
#   5. Optional: email OAuth (Microsoft Graph)
#   6. docker compose up — starts API + UI
#   7. Reference-agent registration (optional)
#
# Idempotent: re-running picks up where you left off; existing config
# files are not overwritten without confirmation.
#
# Usage:
#   bash install/bootstrap.sh                  interactive
#   bash install/bootstrap.sh --non-interactive   read everything from .env
#                                                  fail fast if anything missing
#   bash install/bootstrap.sh --skip-docker    skip the docker-compose step
#                                                  (use when running API on
#                                                  the host directly)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

INTERACTIVE=1
SKIP_DOCKER=0
for arg in "$@"; do
    case "$arg" in
        --non-interactive)  INTERACTIVE=0 ;;
        --skip-docker)      SKIP_DOCKER=1 ;;
        --help|-h)
            sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# //;s/^#//'
            exit 0 ;;
    esac
done

# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
red()    { printf "\033[31m%s\033[0m\n" "$*" >&2; }
section() { echo; bold "── $* ──"; }
ask()    {
    local prompt="$1"; local default="${2:-}"; local var
    if [[ $INTERACTIVE -eq 0 ]]; then
        echo "$default"
        return
    fi
    if [[ -n "$default" ]]; then
        read -r -p "$prompt [$default]: " var
        echo "${var:-$default}"
    else
        read -r -p "$prompt: " var
        echo "$var"
    fi
}

# ---------------------------------------------------------------------------
# 1. Prereq detection
# ---------------------------------------------------------------------------
section "[1/7] Prerequisite detection"

have() { command -v "$1" >/dev/null 2>&1; }

PREREQ_FAIL=0
if have python3; then
    PYV=$(python3 --version 2>&1 | awk '{print $2}')
    green "  ✓ python3 ($PYV)"
else
    red "  ✗ python3 not found — install Python 3.10+"
    PREREQ_FAIL=1
fi

if [[ $SKIP_DOCKER -eq 0 ]]; then
    if have docker; then
        green "  ✓ docker"
    else
        red "  ✗ docker not found — install Docker Desktop or Docker Engine"
        red "    (or run with --skip-docker to skip the container step)"
        PREREQ_FAIL=1
    fi
    if docker compose version >/dev/null 2>&1; then
        green "  ✓ docker compose"
    else
        red "  ✗ docker compose plugin not found"
        PREREQ_FAIL=1
    fi
fi

if have az; then green "  ✓ az CLI (Azure provisioning + deploy support)"
else            yellow "  ⚠ az CLI not installed — Azure provisioning + cloud deploy unavailable (local FS storage works)"
fi

if have claude; then green "  ✓ claude (Claude Code CLI — agent LLM driver)"
else                yellow "  ⚠ claude CLI not installed — agents will fall back to Copilot/Ollama or fail without an API key"
fi

if [[ $PREREQ_FAIL -eq 1 ]]; then
    red "Prerequisites missing. Install them and re-run."; exit 64
fi

# ---------------------------------------------------------------------------
# 2. .env scaffold
# ---------------------------------------------------------------------------
section "[2/7] Environment file"

if [[ -f .env ]]; then
    green "  ✓ .env exists — keeping it. Edit by hand to change values."
else
    if [[ -f .env.example ]]; then
        cp .env.example .env
        green "  ✓ Copied .env.example → .env"
    else
        cat > .env <<'EOF'
# reusable-agents configuration. See README § "Configuration" for full reference.
FRAMEWORK_API_TOKEN=change-me-to-a-random-32-byte-hex
STORAGE_BACKEND=local
# Set if STORAGE_BACKEND=azure:
# AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;...
EOF
        green "  ✓ Wrote a minimal .env (no .env.example found)"
    fi
fi

# Generate a random API token if it's still the placeholder
if grep -q "change-me-to-a-random-32-byte-hex" .env; then
    if have python3; then
        TOKEN=$(python3 -c "import secrets;print(secrets.token_hex(32))")
        sed -i "s|change-me-to-a-random-32-byte-hex|$TOKEN|" .env
        green "  ✓ Generated a random FRAMEWORK_API_TOKEN"
    fi
fi

# ---------------------------------------------------------------------------
# 3. Storage backend choice
# ---------------------------------------------------------------------------
section "[3/7] Storage backend"

CURRENT_BACKEND=$(grep -E "^STORAGE_BACKEND=" .env | cut -d= -f2- || echo "")
echo "  Current STORAGE_BACKEND: ${CURRENT_BACKEND:-unset}"
echo
echo "  Built-in backends:"
echo "    • local — local filesystem at \$AGENT_STORAGE_LOCAL_PATH (default ~/.reusable-agents/data)"
echo "              Best for dev / single-host deployments. No external deps."
echo "    • azure — Azure Blob Storage. Best for production / multi-host."
echo "              Needs AZURE_STORAGE_CONNECTION_STRING + AZURE_STORAGE_CONTAINER."
echo
echo "  Pluggable: register custom backends (S3, GCS, R2, MinIO) via"
echo "  framework.core.storage.register_backend(name, factory). See README."
echo

CHOICE=$(ask "Storage backend (local/azure/skip)" "${CURRENT_BACKEND:-local}")
case "$CHOICE" in
    local|skip)
        # Update .env if needed
        if grep -qE "^STORAGE_BACKEND=" .env; then
            sed -i "s|^STORAGE_BACKEND=.*|STORAGE_BACKEND=local|" .env
        else
            echo "STORAGE_BACKEND=local" >> .env
        fi
        green "  ✓ STORAGE_BACKEND=local"
        ;;
    azure)
        if ! grep -qE "^STORAGE_BACKEND=" .env; then
            echo "STORAGE_BACKEND=azure" >> .env
        else
            sed -i "s|^STORAGE_BACKEND=.*|STORAGE_BACKEND=azure|" .env
        fi
        if ! grep -qE "^AZURE_STORAGE_CONNECTION_STRING=" .env; then
            CONN=$(ask "AZURE_STORAGE_CONNECTION_STRING (paste from Azure portal)")
            if [[ -n "$CONN" ]]; then
                # Quote in case of special chars
                printf 'AZURE_STORAGE_CONNECTION_STRING="%s"\n' "$CONN" >> .env
            fi
        fi
        if ! grep -qE "^AZURE_STORAGE_CONTAINER=" .env; then
            CTR=$(ask "AZURE_STORAGE_CONTAINER" "agents")
            echo "AZURE_STORAGE_CONTAINER=$CTR" >> .env
        fi
        green "  ✓ STORAGE_BACKEND=azure"
        ;;
    *)
        yellow "  (skipping storage config)"
        ;;
esac

# ---------------------------------------------------------------------------
# 4. AI provider auth
# ---------------------------------------------------------------------------
section "[4/7] AI provider auth"

echo "  Pick at least one. Agents fall over in this order:"
echo "    claude-cli (free under Claude Max) → copilot → azure_openai → openai → anthropic → ollama"
echo

if have claude; then
    POOL_DIR="$HOME/.reusable-agents/claude-pool"
    if [[ -d "$POOL_DIR/profile-1/.claude" ]]; then
        green "  ✓ claude-pool already initialized at $POOL_DIR"
    else
        if [[ "$(ask 'Initialize claude-pool now? (y/N)' 'N')" =~ ^[yY] ]]; then
            mkdir -p "$POOL_DIR/profile-1" "$POOL_DIR/bin"
            cat > "$POOL_DIR/bin/claude" <<EOF
#!/usr/bin/env bash
# Auto-generated claude-pool shim
exec python3 $REPO/framework/cli/claude_pool.py exec -- "\$@"
EOF
            chmod +x "$POOL_DIR/bin/claude"
            green "  ✓ Created pool shim at $POOL_DIR/bin/claude"
            echo "  Sign in once per profile: HOME=$POOL_DIR/profile-1 claude /login"
        fi
    fi
fi

# OpenAI / Azure / Anthropic — set if env wants them
for var in OPENAI_API_KEY ANTHROPIC_API_KEY AZURE_OPENAI_API_KEY AZURE_OPENAI_ENDPOINT; do
    if grep -qE "^$var=" .env && ! grep -qE "^$var=$" .env; then
        green "  ✓ $var set"
    elif [[ $INTERACTIVE -eq 1 ]]; then
        VAL=$(ask "$var (blank to skip)")
        if [[ -n "$VAL" ]]; then
            grep -qE "^$var=" .env && sed -i "s|^$var=.*|$var=$VAL|" .env || echo "$var=$VAL" >> .env
        fi
    fi
done

# ---------------------------------------------------------------------------
# 5. Email OAuth (optional)
# ---------------------------------------------------------------------------
section "[5/7] Email OAuth (optional)"

echo "  Agents send operator email + read replies via Microsoft Graph + IMAP."
echo "  If you want this:"
echo "    a. Create an Azure App Registration with Mail.Send + IMAP.AccessAsUser.All"
echo "       delegated scopes (see install/setup-microsoft-oauth.md)"
echo "    b. Add MS_GRAPH_CLIENT_ID, MS_GRAPH_TENANT_ID, MS_GRAPH_SIGNIN_HINT to .env"
echo "    c. Run install/setup-microsoft-oauth.sh + install/setup-imap-oauth.sh"
echo
echo "  Skipping for now is fine — agents will run without email; you can"
echo "  enable later by running those scripts."

# ---------------------------------------------------------------------------
# 6. Docker compose
# ---------------------------------------------------------------------------
section "[6/7] Docker compose (API + UI)"

if [[ $SKIP_DOCKER -eq 1 ]]; then
    yellow "  (skipping per --skip-docker)"
elif [[ -f docker-compose.yml ]]; then
    if [[ "$(ask 'Build + start the API + UI now? (Y/n)' 'Y')" =~ ^[nN] ]]; then
        yellow "  (skipping; run \`docker compose up -d --build\` later)"
    else
        docker compose up -d --build
        sleep 3
        if curl -fsS http://localhost:8093/api/health >/dev/null 2>&1; then
            green "  ✓ API healthy at http://localhost:8093"
            green "  ✓ UI at http://localhost:8091"
        else
            yellow "  (API not responding yet — give it a few more seconds, then check 'docker compose logs')"
        fi
    fi
else
    yellow "  No docker-compose.yml found"
fi

# ---------------------------------------------------------------------------
# 7. Host worker (out-of-container agent execution)
# ---------------------------------------------------------------------------
section "[7/7] Host worker"

UNIT="$HOME/.config/systemd/user/reusable-agents-host-worker.service"
if [[ -f "$UNIT" ]] && systemctl --user is-active --quiet reusable-agents-host-worker.service; then
    green "  ✓ host-worker already running"
elif [[ -f install/install-host-worker.sh ]]; then
    if [[ "$(ask 'Install + start host-worker systemd unit? (Y/n)' 'Y')" =~ ^[nN] ]]; then
        yellow "  (skipping; agents triggered via API will queue but not execute)"
    else
        bash install/install-host-worker.sh
        green "  ✓ host-worker installed"
    fi
fi

echo
bold "── Bootstrap complete ──"
green "Open the dashboard:    http://localhost:8091"
green "API health:            http://localhost:8093/api/health"
green "Token (in .env):       FRAMEWORK_API_TOKEN=$(grep ^FRAMEWORK_API_TOKEN= .env | cut -d= -f2-)"
echo
echo "Next steps:"
echo "  1. Browse the dashboard, see the 7 reference agents listed."
echo "  2. Trigger one (Run now button) to confirm execution works."
echo "  3. Create your own agent: bash install/create-agent.sh my-agent ./agents \\"
echo "       --name 'My Agent' --description 'Does X' --cron '0 * * * *'"
echo "  4. Read README § 'Customizing for your sites' to wire site.yaml + manifest."
