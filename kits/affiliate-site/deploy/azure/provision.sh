#!/usr/bin/env bash
# azure/provision.sh: idempotent Azure setup for one affiliate site.
#
#   bash vendor/affiliate-kit/deploy/azure/provision.sh --env deploy.env [--dry-run] PHASE... [OPTIONS]
#
# Phases run in the order given; `all` = check database app secrets domain.
#   foundation  the shared resources, created only when missing: resource
#               providers, resource group, ACR (admin user on), Log Analytics
#               workspace, Container Apps environment. Not part of `all`: on
#               the fleet's subscription they exist, and a typo must not create
#               a second set. Never creates or sizes a Postgres server.
#   check       az login and subscription, the shared resources, the config, and
#               which secrets resolve (by name; values are never shown)
#   database    on the Postgres server: four login roles (owner, app, agents,
#               publisher), each with a CONNECTION LIMIT and a statement_timeout,
#               passwords taken from their DSNs and sent as SCRAM verifiers;
#               the database (owned by the owner role); CONNECT for the others;
#               and the DB_*_GRANTS, converged (re-run it after migrations).
#               Every role and the database are marked as this site's
#               (COMMENT 'affiliate-kit app=APP_NAME'); one that exists without
#               the marker (another site's) stops the phase before any change
#   app         the Container App: CPU/MEMORY (0.5 vCPU / 1 GiB), MIN..MAX
#               replicas (never 0), external ingress on TARGET_PORT, single
#               revision mode, APP_SECRETS as Container App secrets and
#               APP_ENV / APP_SECRET_ENV as env vars, tagged affiliate-kit-app=
#               APP_NAME. An existing app with that tag is reconciled (ingress
#               port, resources, replicas, env replaced by deploy.env's); one
#               without it (another site's) is refused
#   secrets     re-set the Container App secrets from their sources and remove
#               the ones APP_SECRETS no longer lists (az's registry password
#               secret stays); --restart restarts the active revision so it
#               reads them
#   domain      print the DNS records each SITE_HOSTNAMES entry needs, check
#               them over DNS-over-HTTPS, then `az containerapp hostname add`
#               and `hostname bind` with a free managed certificate
#
# Options:
#   --image-tag TAG        app: the image to create the app with (default: latest)
#   --write-new-dsns FILE  database: generate a password for every role whose DSN
#                          is not set and append its DSN to FILE (0600, created
#                          if missing) to merge into secrets.env
#   --restart              secrets: restart the active revision afterwards
#   --skip-dns-check       domain: bind without checking the DNS records first
#   --adopt-existing       database, app, secrets, domain: take over roles, a
#                          database or a Container App that exist without the
#                          kit's marker (created before the kit, by hand),
#                          marking them as this site's. Never another kit
#                          site's. Without it such objects stop the run
#   --dry-run              print every az / psql command instead of running it
#
# Secrets are read from the environment or SECRETS_FILE (env:NAME) or from files
# (file:PATH) and reach Azure only as `name=@file` (0600, removed on exit) or
# through psql's environment; no secret is printed or put on a command line.
# Refuses to run under bash -x (it would print secret values).
#
# Exit: 0 ok; 1 a step failed; 2 usage or config error; 3 the operator has to
# act (DNS records, a missing secret or image) and then re-run.
# shellcheck disable=SC2329  # phase_* functions are called as "phase_$p"
set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../lib/common.sh"
# shellcheck source=azure.sh
source "$KD_DEPLOY_DIR/azure/azure.sh"

KD_DRY_DEFAULT=absent
IMAGE_TAG=""
NEW_DSNS=""
RESTART=0
SKIP_DNS_CHECK=0
APP_CREATED=0
phases=()

kd_common_args "$@"
set -- "${KD_REST[@]}"
while (($#)); do
  case $1 in
    foundation|check|database|app|secrets|domain|all) phases+=("$1"); shift ;;
    --image-tag) (($# >= 2)) || kd_die "--image-tag needs a value" 2; IMAGE_TAG=$2; shift 2 ;;
    --image-tag=*) IMAGE_TAG=${1#*=}; shift ;;
    --write-new-dsns) (($# >= 2)) || kd_die "--write-new-dsns needs a file" 2; NEW_DSNS=$2; shift 2 ;;
    --write-new-dsns=*) NEW_DSNS=${1#*=}; shift ;;
    --restart) RESTART=1; shift ;;
    --skip-dns-check) SKIP_DNS_CHECK=1; shift ;;
    --adopt-existing) KD_ADOPT=1; shift ;;
    -h|--help) kd_usage "${BASH_SOURCE[0]}"; exit 0 ;;
    *) kd_die "unknown argument: $1 (see --help)" 2 ;;
  esac
done
((${#phases[@]})) || kd_die "name at least one phase: foundation check database app secrets domain all (see --help)" 2

kd_load_config
az_validate_config
[[ -z $IMAGE_TAG ]] || kd_check_tag "$IMAGE_TAG"
kd_banner "azure/provision.sh ${phases[*]}"
kd_need_tool az

# ─── phases ───────────────────────────────────────────────────────────────────

phase_foundation() {
  kd_phase "foundation (shared resources; each created only if missing)"
  kd_require_keys AZ_LOCATION AZ_LOG_WORKSPACE
  az_login_check
  local ns registered rg acr admin ws envname wsid keyfile srv key=""
  for ns in Microsoft.ContainerRegistry Microsoft.App Microsoft.OperationalInsights; do
    kd_probe_exists registered "provider:$ns" az provider show --namespace "$ns" \
      --query "[registrationState][?@=='Registered'] | [0]" -o tsv
    [[ -n $registered ]] || kd_run az provider register --namespace "$ns" --wait || kd_die "provider $ns failed to register" 1
  done
  kd_probe_exists rg resource-group az group show --name "$AZ_RESOURCE_GROUP" --query name -o tsv
  if [[ -z $rg ]]; then
    kd_run az group create --name "$AZ_RESOURCE_GROUP" --location "$AZ_LOCATION" -o none \
      || kd_die "resource group create failed" 1
    kd_dry_created resource-group
  fi
  kd_probe_exists acr acr az acr show --name "$AZ_ACR" --query name -o tsv
  if [[ -z $acr ]]; then
    kd_run az acr create --resource-group "$AZ_RESOURCE_GROUP" --name "$AZ_ACR" --sku Basic --location "$AZ_LOCATION" \
      --admin-enabled true -o none || kd_die "ACR create failed" 1
  else
    # Container Apps read the registry with its admin credentials (the CLI looks them up itself).
    kd_probe_exists admin acr-admin az acr show --name "$AZ_ACR" --query "[adminUserEnabled][?@] | [0]" -o tsv
    [[ -n $admin ]] || kd_run az acr update --name "$AZ_ACR" --admin-enabled true -o none || kd_die "ACR update failed" 1
  fi
  kd_probe_exists ws log-workspace az monitor log-analytics workspace show --resource-group "$AZ_RESOURCE_GROUP" \
    --workspace-name "$AZ_LOG_WORKSPACE" --query name -o tsv
  [[ -n $ws ]] || kd_run az monitor log-analytics workspace create --resource-group "$AZ_RESOURCE_GROUP" \
    --workspace-name "$AZ_LOG_WORKSPACE" --location "$AZ_LOCATION" -o none || kd_die "Log Analytics create failed" 1
  kd_probe_exists envname environment az containerapp env show --name "$AZ_ENVIRONMENT" --resource-group "$AZ_RESOURCE_GROUP" \
    --query name -o tsv
  if [[ -z $envname ]]; then
    kd_probe_value wsid log-workspace-id az monitor log-analytics workspace show --resource-group "$AZ_RESOURCE_GROUP" \
      --workspace-name "$AZ_LOG_WORKSPACE" --query customerId -o tsv
    [[ -n $wsid ]] || kd_die "cannot read the Log Analytics workspace id" 1
    # The workspace key is a secret: read it into memory, hand it to az as @file.
    kd_probe_value key log-workspace-key az monitor log-analytics workspace get-shared-keys \
      --resource-group "$AZ_RESOURCE_GROUP" --workspace-name "$AZ_LOG_WORKSPACE" --query primarySharedKey -o tsv
    if ((KD_DRY_RUN)); then
      keyfile="<secret-dir>/log-workspace-key"
    else
      [[ -n $key ]] || kd_die "cannot read the Log Analytics workspace key" 1
      kd_register_secret "$key"
      kd_secret_dir
      keyfile="$KD_SECRET_DIR/log-workspace-key"
      (umask 077 && printf '%s' "$key" > "$keyfile")
    fi
    kd_run az containerapp env create --name "$AZ_ENVIRONMENT" --resource-group "$AZ_RESOURCE_GROUP" \
      --location "$AZ_LOCATION" --logs-destination log-analytics --logs-workspace-id "$wsid" \
      --logs-workspace-key "@$keyfile" -o none || kd_die "Container Apps environment create failed" 1
  fi
  if [[ -n $AZ_PG_SERVER ]]; then
    kd_probe_exists srv pg-server az postgres flexible-server show --resource-group "$AZ_RESOURCE_GROUP" \
      --name "$AZ_PG_SERVER" --query name -o tsv
    [[ -n $srv ]] || kd_warn "Postgres flexible server $AZ_PG_SERVER does not exist. This script never creates one (size and cost are an operator decision); the database phase needs it."
  fi
}

phase_check() {
  kd_phase "check"
  az_login_check
  local v host kind ref e state
  kd_probe_value v resource-group az group show --name "$AZ_RESOURCE_GROUP" --query name -o tsv
  [[ -n $v ]] || kd_die "resource group $AZ_RESOURCE_GROUP not found (a fresh subscription needs the foundation phase)" 2
  kd_probe_value v environment az containerapp env show --name "$AZ_ENVIRONMENT" --resource-group "$AZ_RESOURCE_GROUP" --query name -o tsv
  [[ -n $v ]] || kd_die "Container Apps environment $AZ_ENVIRONMENT not found in $AZ_RESOURCE_GROUP (foundation phase?)" 2
  kd_probe_value v acr az acr show --name "$AZ_ACR" --query loginServer -o tsv
  [[ -n $v ]] || kd_die "container registry $AZ_ACR not found (foundation phase?)" 2
  az_db_host host
  kd_log "app $APP_NAME in $AZ_RESOURCE_GROUP/$AZ_ENVIRONMENT; image $AZ_REGISTRY/$AZ_REPO; database $DB_NAME on $host"
  kd_log "compute $CPU vCPU / $MEMORY, replicas $MIN_REPLICAS..$MAX_REPLICAS; hostnames $SITE_HOSTNAMES"
  local -a items=()
  read -ra items <<< "$APP_SECRETS"
  for e in "${items[@]}"; do
    state=missing
    kd_secret_get "${e#*=}" v && state="set"
    kd_log "container secret ${e%%=*} (${e#*=}): $state"
  done
  for kind in "${KD_ROLE_KINDS[@]}"; do
    ref=$(kd_var "DB_${kind}_DSN")
    state=missing
    [[ -n $ref ]] && kd_secret_get "$ref" v && state="set"
    kd_log "DB_${kind}_DSN (${ref:-unset}): $state"
  done
  state=missing
  [[ -n $AZ_DB_ADMIN_PASSWORD ]] && kd_secret_get "$AZ_DB_ADMIN_PASSWORD" v && state="set"
  kd_log "AZ_DB_ADMIN_PASSWORD (${AZ_DB_ADMIN_PASSWORD:-unset}): $state"
}

phase_database() {
  kd_phase "database"
  kd_require_keys AZ_DB_ADMIN_USER AZ_DB_ADMIN_PASSWORD
  local host
  az_db_host host
  kd_db_provision "$host" "$AZ_DB_ADMIN_USER" "$AZ_DB_ADMIN_PASSWORD" "$NEW_DSNS" DB
}

phase_app() {
  kd_phase "app"
  local exists image digest fqdn tag=${IMAGE_TAG:-latest}
  local -a envargs=() secretargs=() create=()
  az_app_exists exists
  [[ -z $exists ]] || az_require_app_owned adopt
  kd_resolve_ai_exclude
  kd_container_env
  az_env_args envargs
  if [[ -z $exists ]]; then
    image=$(az_image "$tag")
    kd_probe_value digest image az acr repository show --name "$AZ_ACR" --image "$AZ_REPO:$tag" --query digest -o tsv
    [[ -n $digest ]] || kd_die "$image is not in $AZ_ACR yet: push it first (deploy.sh --env $KD_ENV_FILE --build-only), then re-run" 3
    az_secret_args secretargs
    create=(az containerapp create --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP"
      --environment "$AZ_ENVIRONMENT" --image "$image" --registry-server "$AZ_REGISTRY"
      --ingress external --target-port "$TARGET_PORT" --transport auto --revisions-mode single
      --cpu "$CPU" --memory "$MEMORY" --min-replicas "$MIN_REPLICAS" --max-replicas "$MAX_REPLICAS")
    ((${#secretargs[@]})) && create+=(--secrets "${secretargs[@]}")
    create+=(--env-vars "${envargs[@]}" --tags "affiliate-kit=$KD_KIT_VERSION" "$KD_OWNER_TAG=$APP_NAME" -o none)
    kd_run "${create[@]}" || kd_die "az containerapp create failed" 1
    kd_dry_created app
    APP_CREATED=1
  else
    kd_log "Container App $APP_NAME exists: reconciling ingress, resources, replicas and env"
    kd_run az containerapp ingress update --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
      --type external --target-port "$TARGET_PORT" --transport auto -o none || kd_die "ingress update failed" 1
    kd_run az containerapp update --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
      --cpu "$CPU" --memory "$MEMORY" --min-replicas "$MIN_REPLICAS" --max-replicas "$MAX_REPLICAS" \
      --replace-env-vars "${envargs[@]}" -o none || kd_die "az containerapp update failed" 1
  fi
  az_fqdn fqdn
  kd_log "app: https://$fqdn (min $MIN_REPLICAS replica(s); scale-to-zero is off)"
}

phase_secrets() {
  kd_phase "secrets"
  if ((APP_CREATED)); then
    kd_log "the app was created with its secrets in this run; nothing to re-set"
    return 0
  fi
  local exists rev
  local -a secretargs=() stale=()
  az_app_exists exists
  [[ -n $exists ]] || kd_die "Container App $APP_NAME does not exist yet: run the app phase" 3
  az_require_app_owned adopt
  az_stale_secrets stale
  az_secret_args secretargs
  if ((${#secretargs[@]})); then
    kd_run az containerapp secret set --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
      --secrets "${secretargs[@]}" -o none || kd_die "az containerapp secret set failed" 1
  else
    kd_log "APP_SECRETS is empty; nothing to set"
  fi
  # Converge with deploy.env, as the AWS secret document does.
  if ((${#stale[@]})); then
    kd_run az containerapp secret remove --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
      --secret-names "${stale[@]}" -o none \
      || kd_die "az containerapp secret remove failed (does the app's env still reference one? the app phase replaces the env with deploy.env's)" 1
  fi
  if ((RESTART)); then
    kd_probe_value rev latest-revision az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
      --query properties.latestRevisionName -o tsv
    [[ -n $rev ]] || kd_die "cannot read the latest revision" 1
    kd_run az containerapp revision restart --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
      --revision "$rev" -o none || kd_die "revision restart failed" 1
  else
    kd_log "running replicas read new secret values only after a restart: re-run with --restart, or deploy"
  fi
}

phase_domain() {
  kd_phase "domain"
  local fqdn vid ip h binding method rc mismatch=0 unknown=0
  local -a hosts=() check=()
  read -ra hosts <<< "$SITE_HOSTNAMES"
  az_fqdn fqdn
  kd_probe_value vid verification-id az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
    --query properties.customDomainVerificationId -o tsv
  kd_probe_value ip static-ip az containerapp env show --name "$AZ_ENVIRONMENT" --resource-group "$AZ_RESOURCE_GROUP" \
    --query properties.staticIp -o tsv
  [[ -n $fqdn && -n $vid ]] || kd_die "Container App $APP_NAME has no ingress FQDN or verification id yet: run the app phase" 3
  az_require_app_owned adopt

  printf '\nDNS records (create them at the DNS provider; in Cloudflare set the proxy status to "DNS only"):\n'
  printf '  %-6s %-44s %s\n' TYPE NAME VALUE
  for h in "${hosts[@]}"; do
    if [[ $h == "$SITE_DOMAIN" ]]; then
      printf '  %-6s %-44s %s\n' A "$h" "$ip   (or a CNAME to $fqdn, flattened at the apex)"
    else
      printf '  %-6s %-44s %s\n' CNAME "$h" "$fqdn"
    fi
    printf '  %-6s %-44s %s\n' TXT "asuid.$h" "$vid"
  done
  printf '  If the zone has CAA records, also: CAA %s 0 issue "digicert.com"\n' "$SITE_DOMAIN"
  printf '  The free managed certificate is issued and renewed (by DigiCert) over these records: the A/CNAME\n'
  printf '  must point straight at the app (no CDN proxy in front during issuance and renewal) and the app\n'
  printf '  must stay running (MIN_REPLICAS >= 1).\n\n'

  if ((!SKIP_DNS_CHECK)); then
    for h in "${hosts[@]}"; do
      check=("$KD_PYTHON" -I "$KD_TOOL" dns-check --host "$h" --expect-txt "asuid.$h=$vid")
      if [[ $h == "$SITE_DOMAIN" ]]; then check+=(--expect-a "$ip"); else check+=(--expect-cname "$fqdn"); fi
      rc=0
      kd_run "${check[@]}" || rc=$?
      case $rc in
        0) ;;
        3) mismatch=1 ;;
        *) unknown=1 ;;
      esac
    done
    # Records known to be wrong stop the bind whatever else could not be checked.
    if ((mismatch)); then
      kd_warn "the DNS records above are not in place yet (or are proxied). Create them, wait for propagation, re-run the domain phase (or pass --skip-dns-check)."
      return 3
    elif ((unknown)); then
      kd_warn "could not check DNS for every hostname (resolver unreachable?); binding anyway, the bind validates the records itself"
    fi
  fi

  for h in "${hosts[@]}"; do
    kd_probe_exists binding "hostname:$h" az containerapp hostname list --name "$APP_NAME" \
      --resource-group "$AZ_RESOURCE_GROUP" --query "[?name=='$h'].bindingType | [0]" -o tsv
    if [[ $binding == SniEnabled ]]; then
      kd_log "$h: already bound with a certificate"
      continue
    fi
    if [[ -z $binding ]]; then
      kd_run az containerapp hostname add --hostname "$h" --resource-group "$AZ_RESOURCE_GROUP" \
        --name "$APP_NAME" -o none || kd_die "hostname add $h failed (the asuid TXT record must be visible)" 1
    fi
    method=CNAME
    [[ $h == "$SITE_DOMAIN" ]] && method=HTTP
    kd_run az containerapp hostname bind --hostname "$h" --resource-group "$AZ_RESOURCE_GROUP" --name "$APP_NAME" \
      --environment "$AZ_ENVIRONMENT" --validation-method "$method" -o none \
      || kd_die "managed certificate for $h failed; check the records above, then re-run the domain phase" 1
  done
  kd_run az containerapp hostname list --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" -o table || true
}

# ─── main ─────────────────────────────────────────────────────────────────────

expanded=()
for p in "${phases[@]}"; do
  if [[ $p == all ]]; then expanded+=(check database app secrets domain); else expanded+=("$p"); fi
done

final=0
for p in "${expanded[@]}"; do
  rc=0
  "phase_$p" || rc=$?
  if ((rc == 3)); then
    final=3
    if ((!KD_DRY_RUN)); then
      kd_warn "phase $p needs operator action (see above); stopping here. Re-run from '$p' when done."
      exit 3
    fi
    kd_warn "phase $p would stop a real run with exit 3 (see above); the preview continues"
  elif ((rc != 0)); then
    exit "$rc"
  fi
done
kd_log "azure/provision.sh: ${expanded[*]} done"
exit "$final"
