#!/usr/bin/env bash
# One-shot bootstrap of the Azure Storage Account + container the
# framework needs. Uses `az` CLI; assumes you're logged in
# (`az login` if not).
#
# Idempotent: safe to re-run; existing resources are reused.
#
# Outputs the connection string + writes it to .env.

set -euo pipefail

usage() {
    cat <<EOF
Usage: bootstrap-azure.sh [options]

Options:
  --resource-group <name>     RG to create or reuse (default: reusable-agents-rg)
  --location <region>         Azure region (default: eastus)
  --account-name <name>       Storage account name (default: ragentsXXXXXX)
  --container <name>          Container name (default: agents)
  --sku <sku>                 SKU (default: Standard_LRS)
  --update-env                Append the connection string to ./.env
  -h | --help                 This help

After running, you'll be ready for: bash install/install.sh
EOF
}

RG="reusable-agents-rg"
LOCATION="eastus"
ACCOUNT_NAME=""
CONTAINER="agents"
SKU="Standard_LRS"
UPDATE_ENV=0

while [ $# -gt 0 ]; do
    case "$1" in
        --resource-group)  RG="$2"; shift 2 ;;
        --location)        LOCATION="$2"; shift 2 ;;
        --account-name)    ACCOUNT_NAME="$2"; shift 2 ;;
        --container)       CONTAINER="$2"; shift 2 ;;
        --sku)             SKU="$2"; shift 2 ;;
        --update-env)      UPDATE_ENV=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

if ! command -v az > /dev/null 2>&1; then
    echo "ERROR: az CLI not found. Install: https://learn.microsoft.com/cli/azure/install-azure-cli" >&2
    exit 1
fi

if ! az account show > /dev/null 2>&1; then
    echo "ERROR: not logged into Azure. Run: az login" >&2
    exit 1
fi

# Generate a unique account name if not provided
if [ -z "$ACCOUNT_NAME" ]; then
    ACCOUNT_NAME="ragents$(openssl rand -hex 4 2>/dev/null || head /dev/urandom | tr -dc 'a-z0-9' | head -c 8)"
fi

echo "→ Resource group: $RG ($LOCATION)"
az group create --name "$RG" --location "$LOCATION" --output none

echo "→ Storage account: $ACCOUNT_NAME"
if ! az storage account show --name "$ACCOUNT_NAME" --resource-group "$RG" > /dev/null 2>&1; then
    az storage account create \
        --name "$ACCOUNT_NAME" \
        --resource-group "$RG" \
        --location "$LOCATION" \
        --sku "$SKU" \
        --kind StorageV2 \
        --output none
    echo "  ✓ created"
else
    echo "  (already exists)"
fi

CONN_STRING=$(az storage account show-connection-string \
    --name "$ACCOUNT_NAME" --resource-group "$RG" \
    --query connectionString -o tsv)

echo "→ Container: $CONTAINER"
az storage container create \
    --name "$CONTAINER" \
    --connection-string "$CONN_STRING" \
    --output none 2>/dev/null || true
echo "  ✓ ready"

echo ""
echo "AZURE_STORAGE_CONNECTION_STRING:"
echo ""
echo "  $CONN_STRING"
echo ""
echo "AZURE_STORAGE_CONTAINER: $CONTAINER"

if [ "$UPDATE_ENV" -eq 1 ]; then
    REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
    if [ ! -f "$REPO_ROOT/.env" ]; then
        cp "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    fi
    # Update or append the two vars
    python3 - "$REPO_ROOT/.env" "$CONN_STRING" "$CONTAINER" <<'PY'
import sys, re
env_path, conn, container = sys.argv[1], sys.argv[2], sys.argv[3]
with open(env_path) as f: text = f.read()
def upsert(text, key, val):
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    line = f"{key}={val}"
    if pattern.search(text):
        return pattern.sub(line, text)
    return text.rstrip("\n") + "\n" + line + "\n"
text = upsert(text, "AZURE_STORAGE_CONNECTION_STRING", conn)
text = upsert(text, "AZURE_STORAGE_CONTAINER", container)
with open(env_path, "w") as f: f.write(text)
print(f"  → wrote to {env_path}")
PY
fi

echo ""
echo "Next: bash install/install.sh"
