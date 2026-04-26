#!/usr/bin/env bash
# Seed AI providers for THIS HOST's specific setup (the developer's
# own environment — voidsstr's RTX 5090 box). Tailored values:
#
#   - Ollama at localhost:11434 (already running on the dev GPU box)
#   - GitHub Copilot via the copilot-api proxy on :4141
#   - Anthropic API (key in ANTHROPIC_API_KEY env var)
#   - Azure OpenAI (set AZURE_OPENAI_RESOURCE + AZURE_OPENAI_API_KEY env vars)
#
# Defaults are set so:
#   - SEO sub-agents → claude (highest quality for code editing)
#   - market-research-pipeline → copilot/claude-opus-4.6 (matches existing setup)
#   - email-monitor → ollama-local/qwen3:8b (already-existing usage pattern)
#   - everyone else → anthropic
#
# Re-running this is destructive of inline configs — but providers' env-var
# refs survive. Re-run when the framework adds new agents you want to
# pre-configure.

set -euo pipefail

API="${FRAMEWORK_API_URL:-http://localhost:8090}"
TOKEN="${FRAMEWORK_API_TOKEN:-}"
AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

put_provider() {
    local name="$1"
    local body="$2"
    curl -fsS -X PUT "$API/api/providers/$name" \
        -H "Content-Type: application/json" "${AUTH[@]}" \
        -d "$body" > /dev/null
    echo "  ✓ $name"
}

set_default() {
    local provider="$1" model="$2"
    curl -fsS -X POST "$API/api/providers/defaults/set" \
        -H "Content-Type: application/json" "${AUTH[@]}" \
        -d "{\"provider_name\":\"$provider\",\"model\":\"$model\"}" > /dev/null
}

set_override() {
    local agent="$1" provider="$2" model="$3"
    curl -fsS -X POST "$API/api/providers/defaults/agent-override" \
        -H "Content-Type: application/json" "${AUTH[@]}" \
        -d "{\"agent_id\":\"$agent\",\"provider\":\"$provider\",\"model\":\"$model\"}" > /dev/null
    echo "  ↳ $agent → $provider/$model"
}

echo "Seeding host-specific provider config at $API …"

put_provider "anthropic" '{
    "name": "anthropic",
    "kind": "anthropic",
    "description": "Anthropic API (set ANTHROPIC_API_KEY env on the framework container)",
    "api_key_env": "ANTHROPIC_API_KEY",
    "available_models": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
    "default_model": "claude-opus-4-7"
}'

put_provider "ollama-local" '{
    "name": "ollama-local",
    "kind": "ollama",
    "description": "Local Ollama on :11434 (RTX 5090 dev box)",
    "base_url": "http://localhost:11434",
    "available_models": ["qwen3:8b","qwen3:14b","qwen3:32b","llama3.1:8b","llama3.1:70b"],
    "default_model": "qwen3:8b"
}'

put_provider "copilot" '{
    "name": "copilot",
    "kind": "copilot",
    "description": "GitHub Copilot via copilot-api proxy on :4141",
    "base_url": "http://localhost:4141/v1",
    "available_models": ["claude-opus-4.6", "claude-sonnet-4.6", "gpt-4o", "gpt-4o-mini"],
    "default_model": "claude-opus-4.6"
}'

put_provider "azure-openai" '{
    "name": "azure-openai",
    "kind": "azure_openai",
    "description": "Azure OpenAI (set AZURE_OPENAI_API_KEY + base_url + deployment)",
    "base_url": "https://YOUR-RESOURCE.openai.azure.com",
    "api_key_env": "AZURE_OPENAI_API_KEY",
    "api_version": "2024-08-01-preview",
    "deployment": "gpt-4o-mini",
    "available_models": ["gpt-4o","gpt-4o-mini","gpt-4-turbo"],
    "default_model": "gpt-4o-mini"
}'

echo ""
echo "Setting global default → anthropic/claude-opus-4-7"
set_default "anthropic" "claude-opus-4-7"

echo ""
echo "Per-agent overrides:"
# SEO sub-agents — Claude best at code editing
set_override "seo-implementer"          "anthropic"     "claude-opus-4-7"
set_override "seo-analyzer"             "anthropic"     "claude-sonnet-4-6"
set_override "seo-reporter"             "anthropic"     "claude-haiku-4-5"
# Existing pipelines that already use specific backends
set_override "market-research-pipeline" "copilot"       "claude-opus-4.6"
set_override "email-monitor"            "ollama-local"  "qwen3:8b"
set_override "daily-briefing-calendar-agent" "anthropic" "claude-haiku-4-5"
set_override "daily-status-briefing"    "ollama-local"  "qwen3:8b"

echo ""
echo "Done. View at:  http://localhost:8091/providers"
echo ""
echo "Required env vars on the framework container (see docker-compose.yml):"
echo "  ANTHROPIC_API_KEY       — for the 'anthropic' provider"
echo "  AZURE_OPENAI_API_KEY    — for the 'azure-openai' provider (optional)"
echo "  COPILOT_API_KEY         — typically '\$(cat ~/.config/github-copilot/token)'"
