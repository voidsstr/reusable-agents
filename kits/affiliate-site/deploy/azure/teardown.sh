#!/usr/bin/env bash
# azure/teardown.sh: remove one affiliate site's Azure resources.
#
#   bash vendor/affiliate-kit/deploy/azure/teardown.sh --env deploy.env --confirm APP_NAME [OPTIONS] [--dry-run]
#
#   (always)             delete the Container App; its revisions, secrets and
#                        hostname bindings go with it
#   --with-certificates  also delete the environment's managed certificates for
#                        SITE_HOSTNAMES (including failed attempts)
#   --with-images        also delete the APP_NAME repository from AZ_ACR
#   --with-database      also DROP DATABASE DB_NAME (WITH FORCE) and its four
#                        roles, as the server admin. Irreversible.
#   --confirm NAME       must equal APP_NAME (not needed with --dry-run); an
#                        interactive run asks for it instead
#   --confirm-database DB  with --with-database: must equal DB_NAME (not
#                        needed with --dry-run); an interactive run asks for it
#
# Only this site's resources go: a Container App without this site's
# affiliate-kit-app tag stops the teardown before anything is deleted, and the
# database and roles are dropped only when each one that exists carries this
# site's affiliate-kit marker (the SQL checks before it drops anything).
# Shared resources are never touched: the resource group, the Container Apps
# environment, the registry and the Postgres server serve other sites.
# (SpecPicks' teardown.sh had `--all`, which deletes the whole resource group;
# on the fleet's shared group that is every site, so there is no such mode.)
# Refuses to run under bash -x (it would print secret values).
#
# Exit: 0 ok; 1 a step failed; 2 usage or config error (or another site's resource).
set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../lib/common.sh"
# shellcheck source=azure.sh
source "$KD_DEPLOY_DIR/azure/azure.sh"

KD_DRY_DEFAULT=present
confirm=""
confirm_db=""
with_certs=0
with_images=0
with_db=0

kd_common_args "$@"
set -- "${KD_REST[@]}"
while (($#)); do
  case $1 in
    --confirm) (($# >= 2)) || kd_die "--confirm needs the app name" 2; confirm=$2; shift 2 ;;
    --confirm=*) confirm=${1#*=}; shift ;;
    --confirm-database) (($# >= 2)) || kd_die "--confirm-database needs the database name" 2; confirm_db=$2; shift 2 ;;
    --confirm-database=*) confirm_db=${1#*=}; shift ;;
    --with-certificates) with_certs=1; shift ;;
    --with-images) with_images=1; shift ;;
    --with-database) with_db=1; shift ;;
    -h|--help) kd_usage "${BASH_SOURCE[0]}"; exit 0 ;;
    *) kd_die "unknown argument: $1 (see --help)" 2 ;;
  esac
done

kd_load_config
az_validate_config
if ((!KD_DRY_RUN)) && [[ $confirm != "$APP_NAME" ]]; then
  if [[ -z $confirm && -t 0 ]]; then
    read -r -p "Type the app name ($APP_NAME) to delete it: " confirm
  fi
  [[ $confirm == "$APP_NAME" ]] || kd_die "refusing: pass --confirm $APP_NAME" 2
fi
if ((with_db && !KD_DRY_RUN)) && [[ $confirm_db != "$DB_NAME" ]]; then
  if [[ -z $confirm_db && -t 0 ]]; then
    read -r -p "Type the database name ($DB_NAME) to drop it and its four roles: " confirm_db
  fi
  [[ $confirm_db == "$DB_NAME" ]] || kd_die "refusing to drop a database: pass --confirm-database $DB_NAME" 2
fi
((!with_db)) || kd_require_keys AZ_DB_ADMIN_USER AZ_DB_ADMIN_PASSWORD
kd_banner "azure/teardown.sh"
kd_need_tool az
az_login_check

kd_phase "Container App $APP_NAME"
az_app_exists exists
if [[ -n $exists ]]; then
  az_require_app_owned
  kd_run az containerapp delete --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" --yes -o none \
    || kd_die "az containerapp delete failed" 1
else
  kd_log "Container App $APP_NAME does not exist"
fi

if ((with_certs)); then
  kd_phase "managed certificates"
  read -ra hosts <<< "$SITE_HOSTNAMES"
  certs=""
  for h in "${hosts[@]}"; do
    kd_probe_exists certs "certificates:$h" az containerapp env certificate list --name "$AZ_ENVIRONMENT" \
      --resource-group "$AZ_RESOURCE_GROUP" --managed-certificates-only \
      --query "[?properties.subjectName=='$h'].name" -o tsv
    read -ra names <<< "${certs//$'\n'/ }"
    for c in "${names[@]}"; do
      kd_run az containerapp env certificate delete --name "$AZ_ENVIRONMENT" --resource-group "$AZ_RESOURCE_GROUP" \
        --certificate "$c" --yes -o none || kd_die "deleting certificate $c failed" 1
    done
    ((${#names[@]})) || kd_log "$h: no managed certificate"
  done
fi

if ((with_images)); then
  kd_phase "images"
  kd_probe_exists repo repository az acr repository show --name "$AZ_ACR" --repository "$AZ_REPO" --query imageName -o tsv
  if [[ -n $repo ]]; then
    kd_run az acr repository delete --name "$AZ_ACR" --repository "$AZ_REPO" --yes -o none \
      || kd_die "deleting repository $AZ_REPO failed" 1
  else
    kd_log "repository $AZ_REPO does not exist in $AZ_ACR"
  fi
fi

if ((with_db)); then
  kd_phase "database"
  az_db_host host
  sql=$(kd_sql_drop)
  kd_psql_exec "$host" "$AZ_DB_ADMIN_USER" postgres "$AZ_DB_ADMIN_PASSWORD" "drop database and roles" <<< "$sql" \
    || kd_die "dropping the database failed" 1
fi
kd_log "azure/teardown.sh: done"
