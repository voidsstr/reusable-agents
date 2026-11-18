#!/usr/bin/env bash
# azure-storage-lifecycle.sh — the framework's blob lifecycle rule on the agent
# storage account: tier and expire what nobody READS, never what agents use.
#
#   bash install/azure-storage-lifecycle.sh --account-name <acct> --resource-group <rg> \
#       [--container agents] [--dry-run]
#
# Idempotent. bootstrap-azure.sh runs it after creating the container; run it
# by hand on an existing account. AWS parity: the agents-bucket lifecycle block
# in install/deploy-aws.sh (phase provision).
#
# Why (2026-11-17). The rule this replaces, `agents-runhistory-tier-then-delete`,
# was made by hand with prefixMatch "agents/". In Azure's syntax that is the
# CONTAINER name, so it covered every key in it: Cool 30 days, Archive 120 days
# and Delete 365 days after the last MODIFICATION. Config docs that nobody
# rewrites went to Archive, where every read fails with BlobArchived and the
# readers silently fall back to their built-in defaults:
# config/priority-config.json, config/code-editor-config.json and
# config/implementer-allowed-handlers.json, plus live per-agent state such as
# goals/changes.jsonl and run-index.json. Soft delete is off on the account,
# so the 365-day step would then have deleted them for good.
#
# The rule written here instead:
#   * Conditions use LAST ACCESS (a read or a write), not last modification. A
#     doc an agent reads every run never ages; run history nobody reads still
#     goes Cool at 30 days, Archive at 120 and is deleted at 365. This needs
#     last-access tracking on the account, which the script turns on. A blob
#     with no recorded access ages from the day tracking was enabled (Microsoft
#     docs, "Access time tracking"), so the switch never tiers or deletes
#     anything sooner than the old rule would have.
#   * enableAutoTierToHotFromCool: a Cool blob that gets read moves back to Hot.
#   * config/ and registry/ are never tiered: they are simply not in prefixMatch
#     (an Azure filter can only include, not exclude). Every other framework
#     top-level prefix is listed in TIERED_PREFIXES. A NEW top-level prefix is
#     not tiered until it is added there — the safe default — and the script
#     warns about any it finds.
#
# Rehydrating does not reset a blob's access time, so a doc that was archived
# before this rule existed stays archived until someone rehydrates it (Set
# Blob Tier or a copy with --rehydrate-priority) or rewrites it.
#
# Azure stores the policy as one document with no partial update, so the
# script reads the live policy, replaces only this rule (and the legacy rule
# name above), and keeps every other rule as it is: site image-container
# rules can live in the same account.

set -euo pipefail

ACCOUNT=""
RG=""
CONTAINER="${AZURE_STORAGE_CONTAINER:-agents}"
DRY=0

usage() {
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --account-name)   ACCOUNT="$2"; shift 2 ;;
        --resource-group) RG="$2"; shift 2 ;;
        --container)      CONTAINER="$2"; shift 2 ;;
        --dry-run)        DRY=1; shift ;;
        -h|--help)        usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done
[ -n "$ACCOUNT" ] && [ -n "$RG" ] || { echo "--account-name and --resource-group are required" >&2; usage 1; }
command -v az >/dev/null || { echo "az CLI is required" >&2; exit 1; }

RULE_NAME="agents-tier-by-last-access"
LEGACY_RULE_NAMES="agents-runhistory-tier-then-delete"
# Framework top-level prefixes holding run history and agent state: tiered by
# last access. host-migration/ is the 2026-08-24 fleet-host cutover bundle.
TIERED_PREFIXES="agents framework digest-queue digest-archive decommissioned host-migration"
# Operator-edited config and the agent registry: never tiered.
NEVER_TIERED_PREFIXES="config registry"
COOL_DAYS=30
ARCHIVE_DAYS=120
DELETE_DAYS=365

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "→ $ACCOUNT ($RG), container '$CONTAINER'"

# Top-level prefixes the rule does not know about (best effort; needs data-plane
# read access through az, e.g. a key it can list or --auth-mode login rights).
if tops=$(az storage blob list --account-name "$ACCOUNT" --container-name "$CONTAINER" \
        --delimiter / --num-results 5000 --query "[].name" -o tsv --only-show-errors 2>/dev/null); then
    unknown=""
    for t in $tops; do
        case "$t" in */) ;; *) continue ;; esac
        p="${t%/}"
        case " $TIERED_PREFIXES $NEVER_TIERED_PREFIXES " in *" $p "*) ;; *) unknown="$unknown $p/" ;; esac
    done
    [ -z "$unknown" ] || echo "  WARNING: top-level prefixes in no list, left untiered:$unknown (add to TIERED_PREFIXES or NEVER_TIERED_PREFIXES)"
else
    echo "  (could not list the container's top-level prefixes; skipping the unknown-prefix check)"
fi

az storage account management-policy show --account-name "$ACCOUNT" --resource-group "$RG" \
    -o json --only-show-errors > "$TMP/current.json" 2>/dev/null || echo '{}' > "$TMP/current.json"

# Azure rejects tierToArchive on zone-redundant accounts (ZRS, GZRS, RA-GZRS).
SKU=$(az storage account show --name "$ACCOUNT" --resource-group "$RG" --query sku.name -o tsv --only-show-errors)
case "$SKU" in
    *ZRS*) ARCHIVE_DAYS=0; echo "  $SKU is zone-redundant: no Archive step (Cool, then delete)" ;;
esac

python3 - "$TMP/current.json" "$TMP/policy.json" "$CONTAINER" "$RULE_NAME" "$LEGACY_RULE_NAMES" \
    "$TIERED_PREFIXES" "$COOL_DAYS" "$ARCHIVE_DAYS" "$DELETE_DAYS" <<'PY'
import json, sys
cur_path, out_path, container, rule_name, legacy, tiered, cool, archive, delete = sys.argv[1:10]

def prune(v):
    """Drop the nulls `az ... show` prints for unset fields before writing back."""
    if isinstance(v, dict):
        return {k: prune(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [prune(x) for x in v]
    return v

cur = json.load(open(cur_path)) or {}
drop = {rule_name, *legacy.split()}
kept = [prune(r) for r in ((cur.get("policy") or {}).get("rules") or []) if r.get("name") not in drop]
prefixes = [f"{container}/{p}/" for p in tiered.split()]
assert prefixes and len(prefixes) <= 10, "Azure allows 1-10 prefixes per rule"
after_access = lambda d: {"daysAfterLastAccessTimeGreaterThan": int(d)}
base = {"tierToCool": after_access(cool), "enableAutoTierToHotFromCool": True}
if int(archive) > 0:
    base["tierToArchive"] = after_access(archive)
base["delete"] = after_access(delete)
rule = {
    "name": rule_name,
    "enabled": True,
    "type": "Lifecycle",
    "definition": {
        "filters": {"blobTypes": ["blockBlob"], "prefixMatch": prefixes},
        "actions": {"baseBlob": base},
    },
}
json.dump({"rules": kept + [rule]}, open(out_path, "w"), indent=2)
steps = [f"> {cool}d -> Cool (back to Hot when read)"]
if int(archive) > 0:
    steps.append(f"> {archive}d -> Archive")
steps.append(f"> {delete}d -> delete")
print(f"  rules kept as they were: {[r['name'] for r in kept] or 'none'}")
print(f"  rule {rule_name}: {prefixes}")
print(f"    last access {', '.join(steps)}")
PY

if [ "$DRY" -eq 1 ]; then
    echo "→ dry run: policy that would be written"
    cat "$TMP/policy.json"
    exit 0
fi

echo "→ last-access tracking"
az storage account blob-service-properties update --account-name "$ACCOUNT" --resource-group "$RG" \
    --enable-last-access-tracking true -o none --only-show-errors

echo "→ writing the lifecycle policy (Azure applies a change within 24 hours)"
az storage account management-policy create --account-name "$ACCOUNT" --resource-group "$RG" \
    --policy "@$TMP/policy.json" -o none --only-show-errors

tracking=$(az storage account blob-service-properties show --account-name "$ACCOUNT" --resource-group "$RG" \
    --query "lastAccessTimeTrackingPolicy.enable" -o tsv --only-show-errors)
names=$(az storage account management-policy show --account-name "$ACCOUNT" --resource-group "$RG" \
    --query "policy.rules[].name" -o tsv --only-show-errors | tr '\n' ' ')
echo "  ✓ last-access tracking: $tracking; rules: $names"
