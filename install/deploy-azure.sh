#!/usr/bin/env bash
# Deploy the reusable-agents framework (API + UI) to Azure Container Apps.
#
# Two containers in one Container App, sharing localhost:
#   - agent-api  (uvicorn, internal, port 8090)
#   - agent-ui   (nginx, public ingress, port 80, proxies /api → localhost:8090)
#
# Prerequisites:
#   - Logged in to az + ACR:  az login && az acr login --name nscappsacr
#   - Container Apps env exists:  nsc-apps-env in nsc-apps RG (already provisioned
#     for aisleprompt/specpicks/etc.)
#   - .env file populated with all required values (see "Required env" below)
#
# Required env (in .env, or shell):
#   AZURE_STORAGE_CONNECTION_STRING — points at agent storage account
#   FRAMEWORK_API_TOKEN             — bearer for service-to-service calls
#   GOOGLE_OAUTH_CLIENT_ID          — from Google Cloud Console
#   GOOGLE_OAUTH_CLIENT_SECRET      — from Google Cloud Console
#   ALLOWED_EMAILS                  — comma-separated allowlist
#   SESSION_SECRET                  — openssl rand -hex 32
#
# Usage:
#   bash install/deploy-azure.sh [tag]
#     tag defaults to a UTC timestamp
#
# After first deploy, the script prints the FQDN; that's the value you plug
# into Google Cloud Console as an Authorized redirect URI:
#   https://<fqdn>/api/auth/google/callback

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# Load .env if present. Use a hand-rolled loader instead of `source` because
# values with `;` (e.g. Azure connection strings) are interpreted as command
# separators by bash and get truncated at the first `;`.
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        # Skip blanks + comments
        case "$line" in ''|\#*) continue;; esac
        key="${line%%=*}"
        val="${line#*=}"
        # Strip surrounding quotes if any
        case "$val" in
            \"*\") val="${val#\"}"; val="${val%\"}";;
            \'*\') val="${val#\'}"; val="${val%\'}";;
        esac
        export "$key=$val"
    done < .env
fi

# ── Configuration ───────────────────────────────────────────────────────────
RG="${AZURE_RG:-nsc-apps}"
LOCATION="${AZURE_LOCATION:-eastus}"
ACR="${AZURE_ACR:-nscappsacr}"
ENV_NAME="${AZURE_CONTAINERAPP_ENV:-nsc-apps-env}"
APP_NAME="${AZURE_CONTAINERAPP_NAME:-agents}"
TAG="${1:-$(date -u +%Y%m%d-%H%M)}"
API_IMAGE="${ACR}.azurecr.io/agents-api:${TAG}"
UI_IMAGE="${ACR}.azurecr.io/agents-ui:${TAG}"

# ── Required-env check ──────────────────────────────────────────────────────
required_vars=(
    AZURE_STORAGE_CONNECTION_STRING
    FRAMEWORK_API_TOKEN
    GOOGLE_OAUTH_CLIENT_ID
    GOOGLE_OAUTH_CLIENT_SECRET
    ALLOWED_EMAILS
    SESSION_SECRET
)
missing=()
for v in "${required_vars[@]}"; do
    if [ -z "${!v:-}" ]; then missing+=("$v"); fi
done
if [ ${#missing[@]} -ne 0 ]; then
    echo "ERROR: missing required env vars:" >&2
    printf '  %s\n' "${missing[@]}" >&2
    exit 2
fi

if [ ${#SESSION_SECRET} -lt 32 ]; then
    echo "ERROR: SESSION_SECRET must be at least 32 chars (use: openssl rand -hex 32)" >&2
    exit 2
fi

# ── 1. Build images ─────────────────────────────────────────────────────────
echo "[1/5] Building images (tag=$TAG)"
docker build -f framework/api/Dockerfile -t "$API_IMAGE" .
docker build -f framework/ui/Dockerfile -t "$UI_IMAGE" .

# ── 2. Push to ACR ──────────────────────────────────────────────────────────
echo "[2/5] Pushing to $ACR.azurecr.io"
az acr login --name "$ACR" >/dev/null
docker push "$API_IMAGE"
docker push "$UI_IMAGE"

# ── 3. Look up Container Apps env default domain ────────────────────────────
echo "[3/5] Resolving env default domain"
DEFAULT_DOMAIN=$(az containerapp env show -n "$ENV_NAME" -g "$RG" \
    --query "properties.defaultDomain" -o tsv)
if [ -z "$DEFAULT_DOMAIN" ]; then
    echo "ERROR: Container Apps env '$ENV_NAME' not found in RG '$RG'" >&2
    exit 2
fi
FQDN="${APP_NAME}.${DEFAULT_DOMAIN}"
PUBLIC_BASE_URL="https://${FQDN}"
echo "  Will deploy to: $PUBLIC_BASE_URL"

# ── 4. Create or update the Container App ───────────────────────────────────
EXISTS=$(az containerapp show -n "$APP_NAME" -g "$RG" --query "name" -o tsv 2>/dev/null || true)

# Get ACR creds (admin enabled on nscappsacr — same pattern aisleprompt uses)
ACR_USER=$(az acr credential show --name "$ACR" --query username -o tsv)
ACR_PASS=$(az acr credential show --name "$ACR" --query "passwords[0].value" -o tsv)

if [ -z "$EXISTS" ]; then
    echo "[4/5] Creating Container App '$APP_NAME' (skeleton — full config applied in step 5)"
    # Skeleton create. The az CLI's --secrets flag handles values with `;`
    # correctly, so we set the storage connection string here.
    az containerapp create \
        --name "$APP_NAME" \
        --resource-group "$RG" \
        --environment "$ENV_NAME" \
        --image "$UI_IMAGE" \
        --target-port 80 \
        --ingress external \
        --transport auto \
        --min-replicas 1 \
        --max-replicas 1 \
        --cpu 0.25 --memory 0.5Gi \
        --registry-server "${ACR}.azurecr.io" \
        --registry-username "$ACR_USER" \
        --registry-password "$ACR_PASS" \
        --env-vars "AGENT_API_UPSTREAM=localhost:8090" \
        >/dev/null
fi

# Always update secrets via `az containerapp secret set` — bypasses YAML
# string parsing which mangles values containing `;` (Azure connection
# strings) or quotes.
echo "[5a/5] Updating secrets"
az containerapp secret set -n "$APP_NAME" -g "$RG" --secrets \
    "azure-storage-connection-string=$AZURE_STORAGE_CONNECTION_STRING" \
    "framework-api-token=$FRAMEWORK_API_TOKEN" \
    "google-oauth-client-secret=$GOOGLE_OAUTH_CLIENT_SECRET" \
    "session-secret=$SESSION_SECRET" \
    "acr-pwd=$ACR_PASS" \
    >/dev/null

# Build the YAML for image + env wiring. Secrets are referenced by ref
# (not embedded) so the YAML never carries values with special chars.
echo "[5b/5] Updating revision (multi-container API + UI)"
TMP_YAML=$(mktemp --suffix=.yaml)
trap 'rm -f $TMP_YAML' EXIT

cat > "$TMP_YAML" <<YAML
properties:
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      targetPort: 80
      transport: auto
      allowInsecure: false
    registries:
      - server: ${ACR}.azurecr.io
        username: ${ACR_USER}
        passwordSecretRef: acr-pwd
  template:
    containers:
      - name: agent-api
        image: ${API_IMAGE}
        resources:
          cpu: 0.5
          memory: 1Gi
        env:
          - {name: STORAGE_BACKEND, value: azure}
          - {name: AZURE_STORAGE_CONTAINER, value: agents}
          - {name: AZURE_STORAGE_CONNECTION_STRING, secretRef: azure-storage-connection-string}
          - {name: FRAMEWORK_API_TOKEN, secretRef: framework-api-token}
          - {name: GOOGLE_OAUTH_CLIENT_ID, value: "${GOOGLE_OAUTH_CLIENT_ID}"}
          - {name: GOOGLE_OAUTH_CLIENT_SECRET, secretRef: google-oauth-client-secret}
          - {name: ALLOWED_EMAILS, value: "${ALLOWED_EMAILS}"}
          - {name: SESSION_SECRET, secretRef: session-secret}
          - {name: PUBLIC_BASE_URL, value: "${PUBLIC_BASE_URL}"}
          - {name: FRAMEWORK_API_CORS, value: "${PUBLIC_BASE_URL}"}
          - {name: LOG_LEVEL, value: INFO}
      - name: agent-ui
        image: ${UI_IMAGE}
        resources:
          cpu: 0.25
          memory: 0.5Gi
        env:
          - {name: AGENT_API_UPSTREAM, value: "localhost:8090"}
    scale:
      minReplicas: 1
      maxReplicas: 1
YAML

az containerapp update \
    --name "$APP_NAME" \
    --resource-group "$RG" \
    --yaml "$TMP_YAML" \
    >/dev/null

echo
echo "✓ Deployed."
echo
echo "  URL:           $PUBLIC_BASE_URL"
echo "  Health:        $PUBLIC_BASE_URL/api/health"
echo
echo "──────────────────────────────────────────────────────────────────────"
echo "Authorize this redirect URI in Google Cloud Console"
echo "(APIs & Services → Credentials → OAuth 2.0 Client → Authorized redirect URIs):"
echo
echo "  $PUBLIC_BASE_URL/api/auth/google/callback"
echo
echo "Authorized JavaScript origins:"
echo "  $PUBLIC_BASE_URL"
echo "──────────────────────────────────────────────────────────────────────"
