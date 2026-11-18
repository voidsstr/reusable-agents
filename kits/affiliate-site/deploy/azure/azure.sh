# shellcheck shell=bash
#
# azure/azure.sh: Azure helpers shared by provision.sh, deploy.sh and
# teardown.sh. Source it after ../lib/common.sh and kd_load_config.
#
# Resources (all names from deploy.env):
#   Container App APP_NAME in AZ_RESOURCE_GROUP / AZ_ENVIRONMENT
#   image         AZ_ACR.azurecr.io/APP_NAME:<tag>
#   database      on the flexible server AZ_PG_SERVER (or AZ_DB_HOST)

az_validate_config() {
  kd_require_keys AZ_RESOURCE_GROUP AZ_ENVIRONMENT AZ_ACR
  kd_check_re AZ_RESOURCE_GROUP '^[-A-Za-z0-9._()]{1,90}$' 'an Azure resource group name'
  kd_check_re AZ_ENVIRONMENT '^[A-Za-z0-9][A-Za-z0-9-]{0,58}[A-Za-z0-9]$' 'a Container Apps environment name'
  kd_check_re AZ_ACR '^[A-Za-z0-9]{5,50}$' 'an ACR name (5-50 letters and digits)'
  [[ -z $AZ_PG_SERVER ]] || kd_check_re AZ_PG_SERVER '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$' 'a Postgres flexible server name'
  [[ -z $AZ_DB_HOST ]] || kd_check_re AZ_DB_HOST '^[A-Za-z0-9.-]+$' 'a host name'
  [[ -z $AZ_SUBSCRIPTION ]] || kd_check_re AZ_SUBSCRIPTION '^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$' 'a subscription id'
  [[ -z $AZ_LOCATION ]] || kd_check_re AZ_LOCATION '^[a-z0-9]+$' 'an Azure region name (eastus)'
  [[ -z $AZ_LOG_WORKSPACE ]] || kd_check_re AZ_LOG_WORKSPACE '^[A-Za-z0-9][A-Za-z0-9-]{2,62}$' 'a Log Analytics workspace name'
  AZ_REGISTRY="${AZ_ACR,,}.azurecr.io"
  AZ_REPO=$APP_NAME
}

az_image() { printf '%s/%s:%s' "$AZ_REGISTRY" "$AZ_REPO" "$1"; }

az_login_check() {
  local id
  kd_probe_value id subscription az account show --query id -o tsv
  [[ -n $id ]] || kd_die "the Azure CLI is not logged in: run az login" 2
  if ((!KD_DRY_RUN)) && [[ -n $AZ_SUBSCRIPTION && ${id,,} != "${AZ_SUBSCRIPTION,,}" ]]; then
    kd_die "az is on subscription $id but deploy.env says $AZ_SUBSCRIPTION (az account set --subscription $AZ_SUBSCRIPTION)" 2
  fi
}

# az_app_exists VAR: non-empty when the Container App exists.
az_app_exists() {
  kd_probe_exists "$1" app az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" --query name -o tsv
}

# az_require_app_owned [adopt]: the existing Container App must carry this
# site's affiliate-kit-app tag (provision.sh app sets it at create time), so a
# deploy.env naming another site's app (the resource group is shared) changes
# nothing. With "adopt", provision.sh --adopt-existing tags an untagged app.
AZ_APP_OWNED=0
az_require_app_owned() {
  ((AZ_APP_OWNED)) && return 0
  local tag adopt_fn=""
  [[ ${1:-} == adopt ]] && adopt_fn=az_adopt_app
  kd_probe_strict tag app-owner-tag az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
    --query "tags.\"$KD_OWNER_TAG\"" -o tsv
  kd_require_owner "Container App $APP_NAME in $AZ_RESOURCE_GROUP" "$tag" "$adopt_fn"
  AZ_APP_OWNED=1
}

az_adopt_app() {
  kd_run az containerapp update --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
    --tags "affiliate-kit=$KD_KIT_VERSION" "$KD_OWNER_TAG=$APP_NAME" -o none || kd_die "tagging the Container App failed" 1
}

# az_db_host VAR: AZ_DB_HOST, else the flexible server's FQDN.
az_db_host() {
  local _dh_host=$AZ_DB_HOST
  if [[ -z $_dh_host ]]; then
    [[ -n $AZ_PG_SERVER ]] || kd_die "set AZ_PG_SERVER (or AZ_DB_HOST) in $KD_ENV_FILE" 2
    kd_probe_value _dh_host db-host az postgres flexible-server show --resource-group "$AZ_RESOURCE_GROUP" \
      --name "$AZ_PG_SERVER" --query fullyQualifiedDomainName -o tsv
    [[ -n $_dh_host ]] || kd_die "Postgres flexible server $AZ_PG_SERVER not found in $AZ_RESOURCE_GROUP" 2
  fi
  printf -v "$1" '%s' "$_dh_host"
}

# az_secret_args ARRAY: "name=@<0600 file>" for every APP_SECRETS entry. The az
# CLI reads `name=@path` from the file, so the value is never on a command line.
# (If the file were missing az would take the text literally, so it is checked.)
az_secret_args() {
  local -n _sa_out=$1
  local e name ref path
  local -a items=()
  _sa_out=()
  read -ra items <<< "$APP_SECRETS"
  for e in "${items[@]}"; do
    name=${e%%=*}
    ref=${e#*=}
    kd_secret_file "$name" "$ref" path || kd_die "container secret '$name' ($ref) is not set: add it to the environment or $KD_SECRETS_FILE" 3
    if ((!KD_DRY_RUN)); then
      [[ -s $path ]] || kd_die "internal error: secret file for '$name' is missing" 70
    fi
    _sa_out+=("$name=@$path")
  done
}

# az_stale_secrets ARRAY: the app's secrets that APP_SECRETS no longer lists,
# to remove so the app converges to deploy.env (as the AWS secret document,
# rewritten whole, does). The registry password secret az stored at create time
# is kept. Read before anything is set, so a failed query changes nothing.
az_stale_secrets() {
  local -n _ss_out=$1
  local _ss_names _ss_regs _ss_n _ss_e _ss_keep=" "
  local -a _ss_items=() _ss_all=() _ss_reg=()
  _ss_out=()
  kd_probe_strict _ss_names secret-names az containerapp secret list --name "$APP_NAME" \
    --resource-group "$AZ_RESOURCE_GROUP" --query "[].name" -o tsv
  kd_probe_strict _ss_regs registry-secrets az containerapp show --name "$APP_NAME" \
    --resource-group "$AZ_RESOURCE_GROUP" --query "properties.configuration.registries[].passwordSecretRef" -o tsv
  if ((KD_DRY_RUN)); then
    kd_log "a real run removes the secrets on the app that APP_SECRETS no longer lists (the registry password secret stays)"
    return 0
  fi
  read -ra _ss_items <<< "$APP_SECRETS"
  for _ss_e in "${_ss_items[@]}"; do _ss_keep+="${_ss_e%%=*} "; done
  read -ra _ss_reg <<< "${_ss_regs//$'\n'/ }"
  for _ss_n in "${_ss_reg[@]}"; do _ss_keep+="$_ss_n "; done
  read -ra _ss_all <<< "${_ss_names//$'\n'/ }"
  for _ss_n in "${_ss_all[@]}"; do
    [[ $_ss_keep == *" $_ss_n "* ]] || _ss_out+=("$_ss_n")
  done
  ((${#_ss_out[@]} == 0)) || kd_log "secrets on the app that APP_SECRETS no longer lists (to remove): ${_ss_out[*]}"
}

# az_env_args ARRAY: plain env plus ENV=secretref:<secret> mappings: the whole
# container env, passed with --replace-env-vars so that a variable removed from
# deploy.env is removed from the app too (the AWS task definition is rebuilt
# from deploy.env the same way).
az_env_args() {
  local -n _ea_out=$1
  local m
  _ea_out=("${KD_CENV[@]}")
  for m in "${KD_CSECRET_ENV[@]}"; do _ea_out+=("${m%%=*}=secretref:${m#*=}"); done
}

# az_fqdn VAR: the app's generated domain (the DNS target for subdomains).
az_fqdn() {
  kd_probe_value "$1" fqdn az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
    --query properties.configuration.ingress.fqdn -o tsv
}
