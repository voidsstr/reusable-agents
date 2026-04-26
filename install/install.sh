#!/usr/bin/env bash
# One-shot installer for the reusable-agents framework.
#
# Brings up the full stack:
#   1. Validate .env exists + required vars are set
#   2. docker compose up -d --build (api + ui)
#   3. Wait for /api/health to return 200
#   4. Seed AI providers (skeleton or local-tailored)
#   5. Install the host-worker as a systemd --user service
#   6. Print "next steps" pointing at register-agents.sh in your repos
#
# Re-run is idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ── 1. .env validation ──────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "No .env yet. Copying .env.example → .env."
    cp .env.example .env
    echo ""
    echo "⚠  Edit .env and set:"
    echo "    AZURE_STORAGE_CONNECTION_STRING (or set STORAGE_BACKEND=local)"
    echo "    FRAMEWORK_API_TOKEN              (recommended for production)"
    echo "    ANTHROPIC_API_KEY                (if you'll use Anthropic)"
    echo "    AZURE_OPENAI_API_KEY             (if you'll use Azure OpenAI)"
    echo ""
    echo "Then re-run: bash install/install.sh"
    exit 0
fi

# Source .env without exporting it permanently
set -a; . ./.env; set +a

# Validate critical settings
STORAGE_BACKEND="${STORAGE_BACKEND:-azure}"
if [ "$STORAGE_BACKEND" = "azure" ] && [ -z "${AZURE_STORAGE_CONNECTION_STRING:-}" ]; then
    echo "ERROR: STORAGE_BACKEND=azure but AZURE_STORAGE_CONNECTION_STRING is empty." >&2
    echo "  Set it in .env, or set STORAGE_BACKEND=local for offline dev." >&2
    exit 2
fi

# ── 2. Docker compose ───────────────────────────────────────────────────────
echo "→ Building + starting docker stack…"
docker compose up -d --build 2>&1 | tail -8

# ── 3. Wait for API health ──────────────────────────────────────────────────
echo "→ Waiting for API to be healthy…"
HEALTH_URL="http://localhost:${FRAMEWORK_API_PORT:-8090}/api/health"
for i in $(seq 1 30); do
    if curl -fsS "$HEALTH_URL" > /dev/null 2>&1; then
        echo "  ✓ API healthy at $HEALTH_URL"
        break
    fi
    sleep 1
    if [ "$i" -eq 30 ]; then
        echo "  ✗ API didn't come up. Check: docker compose logs agent-api" >&2
        exit 1
    fi
done

# ── 4. Seed providers ───────────────────────────────────────────────────────
SEEDER="${SEED_PROVIDERS:-skeleton}"  # skeleton | local | none
case "$SEEDER" in
    skeleton)
        echo "→ Seeding provider skeletons (re-run with SEED_PROVIDERS=local for the dev-host preset)…"
        FRAMEWORK_API_URL="http://localhost:${FRAMEWORK_API_PORT:-8090}" \
            bash "$REPO_ROOT/install/seed-providers.sh"
        ;;
    local)
        echo "→ Seeding host-tailored provider config (Anthropic + Ollama + Copilot defaults)…"
        FRAMEWORK_API_URL="http://localhost:${FRAMEWORK_API_PORT:-8090}" \
            bash "$REPO_ROOT/install/seed-providers-local.sh"
        ;;
    none)
        echo "→ Skipping provider seed (SEED_PROVIDERS=none)"
        ;;
esac

# ── 5. Host-worker ──────────────────────────────────────────────────────────
echo "→ Installing host-worker systemd unit…"
bash "$REPO_ROOT/install/install-host-worker.sh" 2>&1 | tail -3

# ── 6. Next steps ──────────────────────────────────────────────────────────
cat <<NEXT

✓ Framework is up.
  API: http://localhost:${FRAMEWORK_API_PORT:-8090}/api/health
  UI:  http://localhost:${FRAMEWORK_UI_PORT:-8091}

Next steps:

  # Register agents from your repos:
  cd /home/voidsstr/development/nsc-assistant
  bash scripts/register-agents.sh

  cd /home/voidsstr/development/specpicks
  bash agents/register-with-framework.sh

  # Or scaffold a brand new agent:
  bash $REPO_ROOT/install/create-agent.sh \\
      my-new-agent /path/to/your-repo/agents \\
      --description "..." --kind python --register

  # If you want the host-tailored AI provider config (Anthropic default,
  # Copilot for market-research, Ollama for email-monitor):
  SEED_PROVIDERS=local bash install/install.sh

NEXT
