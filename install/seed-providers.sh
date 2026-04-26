#!/usr/bin/env bash
# Seed the framework's AI provider registry with the four common backends:
# Azure OpenAI, Anthropic, Ollama (local), GitHub Copilot (via copilot-api).
#
# This populates skeletons — operators MUST set the env vars referenced
# by api_key_env (or fill in api_key inline) before agents can call them.
#
# Run after `docker compose up -d`:
#   bash install/seed-providers.sh
#
# Re-run is idempotent: providers with the same name get overwritten with
# fresh skeletons (use the UI to edit deployed providers).

set -euo pipefail

API="${FRAMEWORK_API_URL:-http://localhost:8090}"
TOKEN="${FRAMEWORK_API_TOKEN:-}"
AUTH=()
if [ -n "$TOKEN" ]; then AUTH=(-H "Authorization: Bearer $TOKEN"); fi

echo "Seeding AI providers at $API …"

# Azure OpenAI
curl -fsS -X PUT "$API/api/providers/azure-openai" \
    -H "Content-Type: application/json" "${AUTH[@]}" \
    -d '{
      "name": "azure-openai",
      "kind": "azure_openai",
      "description": "Azure OpenAI Service — fill in base_url + api_key_env + deployment.",
      "base_url": "https://YOUR-RESOURCE.openai.azure.com",
      "api_key_env": "AZURE_OPENAI_API_KEY",
      "api_version": "2024-08-01-preview",
      "deployment": "gpt-4o-mini",
      "available_models": ["gpt-4o", "gpt-4o-mini", "gpt-4", "gpt-3.5-turbo"],
      "default_model": "gpt-4o-mini",
      "metadata": {"seeded": true}
    }' > /dev/null
echo "  ✓ azure-openai"

# Anthropic (direct API)
curl -fsS -X PUT "$API/api/providers/anthropic" \
    -H "Content-Type: application/json" "${AUTH[@]}" \
    -d '{
      "name": "anthropic",
      "kind": "anthropic",
      "description": "Anthropic API — set ANTHROPIC_API_KEY env var on the framework container.",
      "api_key_env": "ANTHROPIC_API_KEY",
      "available_models": [
        "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"
      ],
      "default_model": "claude-opus-4-7",
      "metadata": {"seeded": true}
    }' > /dev/null
echo "  ✓ anthropic"

# Ollama (local)
curl -fsS -X PUT "$API/api/providers/ollama-local" \
    -H "Content-Type: application/json" "${AUTH[@]}" \
    -d '{
      "name": "ollama-local",
      "kind": "ollama",
      "description": "Local Ollama server — no API key needed. Update base_url if Ollama runs elsewhere.",
      "base_url": "http://localhost:11434",
      "available_models": ["qwen3:8b", "qwen3:32b", "llama3.1:8b", "llama3.1:70b", "mistral:7b"],
      "default_model": "qwen3:8b",
      "metadata": {"seeded": true}
    }' > /dev/null
echo "  ✓ ollama-local"

# GitHub Copilot (via copilot-api proxy)
curl -fsS -X PUT "$API/api/providers/copilot" \
    -H "Content-Type: application/json" "${AUTH[@]}" \
    -d '{
      "name": "copilot",
      "kind": "copilot",
      "description": "GitHub Copilot via copilot-api proxy — `npx copilot-api` listens on :4141.",
      "base_url": "http://localhost:4141/v1",
      "api_key_env": "COPILOT_API_KEY",
      "available_models": ["claude-opus-4.6", "claude-sonnet-4.6", "gpt-4o", "gpt-4o-mini"],
      "default_model": "claude-opus-4.6",
      "metadata": {"seeded": true}
    }' > /dev/null
echo "  ✓ copilot"

# OpenAI direct
curl -fsS -X PUT "$API/api/providers/openai" \
    -H "Content-Type: application/json" "${AUTH[@]}" \
    -d '{
      "name": "openai",
      "kind": "openai",
      "description": "OpenAI direct — set OPENAI_API_KEY env var.",
      "api_key_env": "OPENAI_API_KEY",
      "available_models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
      "default_model": "gpt-4o-mini",
      "metadata": {"seeded": true}
    }' > /dev/null
echo "  ✓ openai"

echo ""
echo "Done. Set env vars on the framework container, then visit:"
echo "  http://localhost:8091/providers"
echo "to flag one as default and configure per-agent overrides."
