#!/usr/bin/env bash
# azure/deploy.sh: ship one affiliate site to Azure Container Apps.
#
#   bash vendor/affiliate-kit/deploy/azure/deploy.sh --env deploy.env [--dry-run] [OPTIONS]
#
# Stages, always in this order:
#   gate     deploy/predeploy-gate.sh: the site's gate suite on the tree about
#            to be built (PREDEPLOY_GATE_SUITE / PREDEPLOY_GATE_ARGS)
#   build    docker build of DOCKERFILE, after offline checks: site.json keeps
#            SSR HTML at s-maxage <= HTML_MAX_S_MAXAGE (600 at most), the
#            rendered Dockerfile matches the kit template, no secret in the image
#   push     az acr login; push AZ_ACR.azurecr.io/APP_NAME:<tag> and :latest
#   migrate  MIGRATE_COMMAND inside the new image, as the owner role (its DSN,
#            which must log in as DB_OWNER_ROLE to DB_NAME, in the container's
#            environment only); skipped when empty
#   release  az containerapp update: the new image, CPU/MEMORY, MIN..MAX
#            replicas, and the container env exactly as deploy.env has it
#            (plain values and secret references; secret values untouched);
#            then wait until the new revision is the latest ready revision
#   smoke    the origin (the custom domain through the app's own FQDN once it
#            is bound, else the FQDN) and the public domain when it resolves:
#            expected status codes, and the HTML cache rule on every HTML
#            response (s-maxage <= the limit, nothing served stale past it,
#            CDN-targeted headers included, no Age above it)
#
# Options:
#   --image-tag TAG        default: a new UTC timestamp when building; required
#                          when the run does not build. A full reference to this
#                          site's repository (REGISTRY/APP_NAME:TAG) is accepted too
#   --only STAGES          comma-separated subset, e.g. --only release,smoke
#   --build-only           = --only gate,build,push
#   --skip-build           = --only migrate,release,smoke (needs --image-tag)
#   --smoke-only           = --only smoke
#   --skip-tests[=REASON]  build without the pre-deploy gate (emergencies; loud)
#   --gated-by=WHO         build without the gate stage because WHO (an outer
#                          pipeline: the deployer runs the gate as its test
#                          stage) ran it on this tree. A run that builds needs
#                          the gate stage, --gated-by or --skip-tests
#   --rollback-on-smoke-failure
#                          when the smoke check fails after this run's release,
#                          release the previous image again before exiting 4
#   --dry-run              print every docker / az command instead of running it
#
# The Container App must exist (provision.sh app) and carry this site's
# affiliate-kit-app tag. Container secrets are never read or changed here. A
# release of an image this run did not build says so. On a failed release or
# smoke check the previous image and the command that redeploys it are printed.
# Refuses to run under bash -x (it would print secret values).
#
# Exit: 0 ok; 1 a stage failed; 2 usage or config error; 3 the app or the
# image does not exist yet; 4 the smoke check failed (after a release in this
# run: rolled back with --rollback-on-smoke-failure unless that rollback failed
# or had nothing to put back, when the new image may still serve; without the
# flag the release serves).
set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../lib/common.sh"
# shellcheck source=azure.sh
source "$KD_DEPLOY_DIR/azure/azure.sh"

KD_DRY_DEFAULT=present
TAG=""
only=""
only_given=0
ROLLBACK_ON_SMOKE=0

kd_common_args "$@"
set -- "${KD_REST[@]}"
while (($#)); do
  case $1 in
    --image-tag) (($# >= 2)) || kd_die "--image-tag needs a value" 2; TAG=$2; shift 2 ;;
    --image-tag=*) TAG=${1#*=}; shift ;;
    --only) (($# >= 2)) || kd_die "--only needs a stage list" 2; only=$2; only_given=1; shift 2 ;;
    --only=*) only=${1#*=}; only_given=1; shift ;;
    --build-only) only=gate,build,push; only_given=1; shift ;;
    --skip-build) only=migrate,release,smoke; only_given=1; shift ;;
    --smoke-only) only=smoke; only_given=1; shift ;;
    --skip-tests) KD_SKIP_TESTS=1; shift ;;
    --skip-tests=*) KD_SKIP_TESTS=1; KD_SKIP_TESTS_REASON=${1#*=}; shift ;;
    --gated-by) (($# >= 2)) || kd_die "--gated-by needs a name" 2; KD_GATED_BY=$2; shift 2 ;;
    --gated-by=*) KD_GATED_BY=${1#*=}; shift ;;
    --rollback-on-smoke-failure) ROLLBACK_ON_SMOKE=1; shift ;;
    -h|--help) kd_usage "${BASH_SOURCE[0]}"; exit 0 ;;
    *) kd_die "unknown argument: $1 (see --help)" 2 ;;
  esac
done

kd_select_stages "$only_given" "$only"
kd_gate_policy
has() { kd_has_stage "$1"; }

kd_load_config
az_validate_config
if [[ -z $TAG ]]; then
  has build || kd_die "--image-tag is required when the run does not build" 2
  TAG=$(kd_default_tag)
fi
TAG=$(kd_tag_from_ref "$TAG" "$AZ_REGISTRY/$AZ_REPO")
IMAGE=$(az_image "$TAG")
LATEST=$(az_image latest)
PRIOR_IMAGE=""
RELEASED=0
FQDN=""
stages=()
for s in "${KD_ALL_STAGES[@]}"; do has "$s" && stages+=("$s"); done
kd_banner "azure/deploy.sh (${stages[*]}) image $IMAGE"
if has build || has push || has migrate; then kd_need_tool docker; fi
if has push || has migrate || has release || has smoke; then kd_need_tool az; fi

rollback_hint() {
  [[ -n $PRIOR_IMAGE && $PRIOR_IMAGE != "$IMAGE" ]] || return 0
  kd_warn "the previous image was $PRIOR_IMAGE; to put it back:"
  kd_warn "  bash $KD_DEPLOY_DIR/azure/deploy.sh --env $KD_ENV_FILE --only release,smoke --image-tag ${PRIOR_IMAGE##*:}"
}

AZ_LATEST_REV=""
revision_ready() {
  local ready state
  kd_probe_value ready ready-revision az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
    --query properties.latestReadyRevisionName -o tsv
  [[ -n $ready && $ready == "$AZ_LATEST_REV" ]] && return 0
  kd_probe_value state revision-state az containerapp revision show --name "$APP_NAME" \
    --resource-group "$AZ_RESOURCE_GROUP" --revision "$AZ_LATEST_REV" \
    --query "[properties.provisioningState, properties.runningState]" -o tsv
  if [[ $state == *Failed* ]]; then
    kd_warn "revision $AZ_LATEST_REV: ${state//$'\t'/ }"
    return 2
  fi
  return 1
}

# az_release IMAGE: update the app to IMAGE with deploy.env's resources,
# replicas and env, then wait until its new revision is the latest ready one.
# Returns 1 (with a warning) when either fails.
az_release() {
  local image=$1
  local -a envargs=()
  az_env_args envargs
  kd_run az containerapp update --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" --image "$image" \
    --cpu "$CPU" --memory "$MEMORY" --min-replicas "$MIN_REPLICAS" --max-replicas "$MAX_REPLICAS" \
    --replace-env-vars "${envargs[@]}" -o none || { kd_warn "az containerapp update to $image failed"; return 1; }
  kd_probe_value AZ_LATEST_REV latest-revision az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
    --query properties.latestRevisionName -o tsv
  [[ -n $AZ_LATEST_REV ]] || { kd_warn "cannot read the latest revision of $APP_NAME"; return 1; }
  kd_wait_until "revision $AZ_LATEST_REV becomes the latest ready revision" "$READY_TIMEOUT_SECONDS" 10 revision_ready \
    || { kd_warn "revision $AZ_LATEST_REV ($image) did not become ready; the previous revision keeps serving"; return 1; }
  kd_log "revision $AZ_LATEST_REV is serving $image"
}

stage_release() {
  kd_phase "release $IMAGE"
  local exists port
  az_app_exists exists
  [[ -n $exists ]] || kd_die "Container App $APP_NAME does not exist: run provision.sh app first" 3
  az_require_app_owned
  kd_probe_value port ingress-port az containerapp ingress show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
    --query targetPort -o tsv
  if ((!KD_DRY_RUN)) && [[ $port != "$TARGET_PORT" ]]; then
    kd_die "ingress targetPort is ${port:-unset} but TARGET_PORT is $TARGET_PORT: the revision would never become ready; run provision.sh app to reconcile" 2
  fi
  if ((ROLLBACK_ON_SMOKE)); then
    # A failed smoke check releases this image again, so it must be known first.
    kd_probe_strict PRIOR_IMAGE prior-image az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
      --query "properties.template.containers[0].image" -o tsv
    [[ -n $PRIOR_IMAGE ]] || kd_die "cannot read the image $APP_NAME serves now, so a failed smoke check could not be rolled back; nothing was released" 1
  else
    kd_probe_value PRIOR_IMAGE prior-image az containerapp show --name "$APP_NAME" --resource-group "$AZ_RESOURCE_GROUP" \
      --query "properties.template.containers[0].image" -o tsv
  fi
  kd_resolve_ai_exclude
  kd_container_env
  az_release "$IMAGE" || { rollback_hint; kd_die "the release of $IMAGE failed" 1; }
  RELEASED=1
  az_fqdn FQDN
}

# smoke_failed MESSAGE: exit 4. After this run's release with
# --rollback-on-smoke-failure, release the previous image again first, so the
# serving image is no longer the one this run shipped (the deployer's
# post-flight check then sees the failure instead of the new tag).
smoke_failed() {
  local msg=$1
  if ((RELEASED && ROLLBACK_ON_SMOKE)) && [[ -n $PRIOR_IMAGE && $PRIOR_IMAGE != "$IMAGE" ]]; then
    kd_warn "$msg: releasing the previous image $PRIOR_IMAGE again (--rollback-on-smoke-failure)"
    if az_release "$PRIOR_IMAGE"; then
      kd_die "$msg; rolled back: $APP_NAME serves $PRIOR_IMAGE again" 4
    fi
    rollback_hint
    kd_die "$msg, and the rollback to $PRIOR_IMAGE failed: $IMAGE may still be serving" 4
  fi
  if ((RELEASED && ROLLBACK_ON_SMOKE)); then
    kd_die "$msg; nothing to roll back to (the app served ${PRIOR_IMAGE:-no readable image} before this release)" 4
  fi
  if ((RELEASED)); then
    rollback_hint
    kd_die "$msg; $IMAGE is serving (pass --rollback-on-smoke-failure to put the previous image back automatically)" 4
  fi
  kd_die "$msg" 4
}

stage_smoke() {
  kd_phase "smoke"
  local bound
  az_fqdn FQDN
  [[ -n $FQDN ]] || smoke_failed "cannot read the FQDN of $APP_NAME to smoke-check it"
  kd_probe_value bound hostname-bound az containerapp hostname list --name "$APP_NAME" \
    --resource-group "$AZ_RESOURCE_GROUP" --query "[?name=='$SITE_DOMAIN'].bindingType | [0]" -o tsv
  if [[ $bound == SniEnabled ]] || { ((KD_DRY_RUN)) && [[ -n $bound ]]; }; then
    kd_smoke origin "https://$SITE_DOMAIN" "$FQDN:443" 0 || smoke_failed "the smoke check failed at the origin"
  else
    kd_smoke origin "https://$FQDN" "" 0 || smoke_failed "the smoke check failed at the origin"
  fi
  kd_smoke public "https://$SITE_DOMAIN" "" 1 || smoke_failed "the smoke check failed on the public domain"
}

if has gate || has build; then
  kd_phase "checks"
  kd_prebuild_checks
fi
kd_gate_stage
if has build; then
  kd_phase "build $IMAGE"
  kd_build_image "$IMAGE"
fi
if has push; then
  kd_phase "push"
  kd_run az acr login --name "$AZ_ACR" || kd_die "az acr login failed" 1
  kd_push_image "$IMAGE" "$LATEST"
fi
if ! has build && { has migrate || has release; }; then
  kd_log "deploying the existing image $IMAGE (not built in this run): the gate tested whatever built it"
  kd_probe_value digest image az acr repository show --name "$AZ_ACR" --image "$AZ_REPO:$TAG" --query digest -o tsv
  [[ -n $digest ]] || kd_die "$IMAGE is not in $AZ_ACR" 3
fi
if has migrate; then
  kd_phase "migrate"
  kd_run_migrations "$IMAGE" "$DB_OWNER_DSN"
fi
if has release; then stage_release; fi
if has smoke; then stage_smoke; fi

# One JSON line for pipeline integration (the same shape SpecPicks' deploy.sh prints).
url=null
[[ -n $FQDN ]] && url="\"https://$FQDN\""
printf '{"app_name": "%s", "url": %s, "image": "%s"}\n' "$APP_NAME" "$url" "$IMAGE"
