#!/usr/bin/env bash
# aws/deploy.sh: ship one affiliate site to ECS Fargate (the parallel target).
# Same deploy.env, stages, flags, exit codes and env contract as azure/deploy.sh.
#
#   bash vendor/affiliate-kit/deploy/aws/deploy.sh --env deploy.env [--dry-run] [OPTIONS]
#
# Stages, always in this order:
#   gate     deploy/predeploy-gate.sh: the site's gate suite on the tree about to be built
#   build    docker build of DOCKERFILE (the same image Azure runs), after the
#            same offline checks (site.json s-maxage limit, template drift, no
#            secret in the image)
#   push     ECR login (password over stdin); push <account>.dkr.ecr.<region>
#            .amazonaws.com/APP_NAME:<tag> and :latest
#   migrate  MIGRATE_COMMAND inside the new image, as the owner role (its DSN,
#            which must log in as DB_OWNER_ROLE to DB_NAME, in the container's
#            environment only); skipped when empty
#   release  register the task definition rebuilt from deploy.env with the new
#            image (CPU/MEMORY, env, secrets by ARN), update the service,
#            re-assert MIN..MAX auto scaling, then wait until the new task set
#            is the completed primary deployment (the circuit breaker rolls a
#            failed one back)
#   smoke    the origin (the custom domain through the ALB) and the public
#            domain when it resolves: status codes, and the HTML cache rule on
#            every HTML response (as in azure/deploy.sh)
#
# Options: --image-tag TAG, --only STAGES, --build-only, --skip-build,
# --smoke-only, --skip-tests[=REASON], --gated-by=WHO,
# --rollback-on-smoke-failure (puts the previous task definition back),
# --dry-run (as in azure/deploy.sh).
#
# Needs AWS_STATE_FILE from reusable-agents/install/deploy-aws.sh provision and
# this site's provision.sh (secrets, domain, app); the service must carry this
# site's affiliate-kit-app tag. Secret values are never read or changed here.
# Refuses to run under bash -x (it would print secret values).
#
# Exit: 0 ok; 1 a stage failed; 2 usage or config error; 3 the service or the
# image does not exist yet; 4 the smoke check failed (after a release in this
# run: rolled back with --rollback-on-smoke-failure unless that rollback failed
# or had nothing to put back, when the new image may still serve; without the
# flag the release serves).
set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../lib/common.sh"
# shellcheck source=aws.sh
source "$KD_DEPLOY_DIR/aws/aws.sh"

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
aws_validate_config
aws_load_state
if [[ -z $TAG ]]; then
  has build || kd_die "--image-tag is required when the run does not build" 2
  TAG=$(kd_default_tag)
fi
TAG=$(kd_tag_from_ref "$TAG" "$AWS_REGISTRY/$AWS_REPO")
IMAGE=$(aws_image "$TAG")
LATEST=$(aws_image latest)
PRIOR_TD=""
PRIOR_IMAGE=""
NEW_TD=""
WAIT_TD=""
RELEASED=0
stages=()
for s in "${KD_ALL_STAGES[@]}"; do has "$s" && stages+=("$s"); done
kd_banner "aws/deploy.sh (${stages[*]}) image $IMAGE"
if has build || has push || has migrate; then kd_need_tool docker; fi
if has push || has migrate || has release || has smoke; then kd_need_tool aws; fi

# The way back goes through this script ("Infra scripts are the source of
# truth"): it registers a task definition from deploy.env with the old image.
rollback_hint() {
  [[ -n $PRIOR_TD && $PRIOR_TD != "$NEW_TD" ]] || return 0
  kd_warn "the previous task definition was $PRIOR_TD${PRIOR_IMAGE:+ (image $PRIOR_IMAGE)}; to put it back:"
  if [[ -n $PRIOR_IMAGE ]]; then
    kd_warn "  bash $KD_DEPLOY_DIR/aws/deploy.sh --env $KD_ENV_FILE --only release,smoke --image-tag ${PRIOR_IMAGE##*:}"
  else
    kd_warn "  bash $KD_DEPLOY_DIR/aws/deploy.sh --env $KD_ENV_FILE --only release,smoke --image-tag <its tag>"
  fi
}

rollout_done() {
  local state td st
  kd_probe_value state primary-deployment aws ecs describe-services --region "$AWS_REGION" --cluster "$AWS_CLUSTER" \
    --services "$APP_NAME" --query "services[0].deployments[?status=='PRIMARY'] | [0].[taskDefinition, rolloutState]" \
    --output text
  ((KD_DRY_RUN)) && return 0
  read -r td st <<< "$state"
  if [[ $td != "$WAIT_TD" ]]; then
    kd_warn "the primary deployment runs ${td:-nothing}, not $WAIT_TD (rolled back by the circuit breaker?)"
    return 2
  fi
  case $st in
    COMPLETED) return 0 ;;
    FAILED) kd_warn "deployment of $WAIT_TD FAILED"; return 2 ;;
    *) return 1 ;;
  esac
}

# aws_point_service TASKDEF: the service runs TASKDEF next (a rolling deploy).
aws_point_service() {
  WAIT_TD=$1
  kd_run aws ecs update-service --region "$AWS_REGION" --cluster "$AWS_CLUSTER" --service "$APP_NAME" \
    --task-definition "$1" \
    --deployment-configuration "deploymentCircuitBreaker={enable=true,rollback=true},maximumPercent=200,minimumHealthyPercent=100" \
    --query service.serviceName --output text || { kd_warn "update-service to $1 failed"; return 1; }
}

# aws_wait_rollout: wait until WAIT_TD is the completed primary deployment.
aws_wait_rollout() {
  kd_wait_until "$WAIT_TD becomes the completed primary deployment" "$READY_TIMEOUT_SECONDS" 15 rollout_done \
    || { kd_warn "the task set of $WAIT_TD did not become healthy; ECS keeps (or rolls back to) the previous one"; return 1; }
}

stage_release() {
  kd_phase "release $IMAGE"
  local svc sarn
  aws_service_exists svc
  [[ -n $svc ]] || kd_die "ECS service $APP_NAME does not exist: run provision.sh app first" 3
  aws_require_service_owned
  if ((ROLLBACK_ON_SMOKE)); then
    # A failed smoke check points the service back at it, so it must be known first.
    kd_probe_strict PRIOR_TD prior-taskdef aws ecs describe-services --region "$AWS_REGION" --cluster "$AWS_CLUSTER" \
      --services "$APP_NAME" --query 'services[0].taskDefinition' --output text
    [[ -n $PRIOR_TD ]] || kd_die "cannot read the task definition $APP_NAME runs now, so a failed smoke check could not be rolled back; nothing was released" 1
  else
    kd_probe_value PRIOR_TD prior-taskdef aws ecs describe-services --region "$AWS_REGION" --cluster "$AWS_CLUSTER" \
      --services "$APP_NAME" --query 'services[0].taskDefinition' --output text
  fi
  if [[ -n $PRIOR_TD ]]; then
    kd_probe_value PRIOR_IMAGE prior-image aws ecs describe-task-definition --region "$AWS_REGION" \
      --task-definition "$PRIOR_TD" --query 'taskDefinition.containerDefinitions[0].image' --output text
  fi
  kd_resolve_ai_exclude
  kd_container_env
  aws_secret_arn sarn
  aws_taskdef NEW_TD "$IMAGE" "$sarn"
  aws_point_service "$NEW_TD" || { rollback_hint; kd_die "the release of $IMAGE failed" 1; }
  aws_scaling_target
  aws_wait_rollout || { rollback_hint; kd_die "the release of $IMAGE failed" 1; }
  RELEASED=1
  kd_log "service $APP_NAME is running $IMAGE"
}

# smoke_failed MESSAGE: exit 4. After this run's release with
# --rollback-on-smoke-failure, point the service back at the previous task
# definition first (azure/deploy.sh does the same with the previous image).
smoke_failed() {
  local msg=$1
  if ((RELEASED && ROLLBACK_ON_SMOKE)) && [[ -n $PRIOR_TD && $PRIOR_TD != "$NEW_TD" ]]; then
    kd_warn "$msg: pointing the service back at $PRIOR_TD (--rollback-on-smoke-failure)"
    if aws_point_service "$PRIOR_TD" && aws_wait_rollout; then
      kd_die "$msg; rolled back: $APP_NAME runs $PRIOR_TD again" 4
    fi
    rollback_hint
    kd_die "$msg, and the rollback to $PRIOR_TD failed: $IMAGE may still be serving" 4
  fi
  if ((RELEASED && ROLLBACK_ON_SMOKE)); then
    kd_die "$msg; nothing to roll back to (the service ran ${PRIOR_TD:-no readable task definition} before this release)" 4
  fi
  if ((RELEASED)); then
    rollback_hint
    kd_die "$msg; $IMAGE is serving (pass --rollback-on-smoke-failure to put the previous task definition back automatically)" 4
  fi
  kd_die "$msg" 4
}

stage_smoke() {
  kd_phase "smoke"
  kd_smoke origin "https://$SITE_DOMAIN" "${AWSS[ALB_DNS]}:443" 0 || smoke_failed "the smoke check failed at the origin (ALB)"
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
  # ACR creates a repository on the first push; ECR needs it created (scan on push).
  kd_probe_exists repo ecr-repository aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$AWS_REPO" \
    --query 'repositories[0].repositoryUri' --output text
  if [[ -z $repo ]]; then
    kd_run aws ecr create-repository --region "$AWS_REGION" --repository-name "$AWS_REPO" \
      --image-scanning-configuration scanOnPush=true --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" \
      --query repository.repositoryUri --output text || kd_die "create-repository failed" 1
  fi
  aws_ecr_login || kd_die "ECR login failed" 1
  kd_push_image "$IMAGE" "$LATEST"
fi
if ! has build && { has migrate || has release; }; then
  kd_log "deploying the existing image $IMAGE (not built in this run): the gate tested whatever built it"
  kd_probe_value digest image aws ecr describe-images --region "$AWS_REGION" --repository-name "$AWS_REPO" \
    --image-ids "imageTag=$TAG" --query 'imageDetails[0].imageDigest' --output text
  [[ -n $digest ]] || kd_die "$IMAGE is not in ECR" 3
fi
if has migrate; then
  kd_phase "migrate"
  kd_run_migrations "$IMAGE" "$AWS_DB_OWNER_DSN"
fi
if has release; then stage_release; fi
if has smoke; then stage_smoke; fi

printf '{"app_name": "%s", "url": "https://%s", "image": "%s"}\n' "$APP_NAME" "$SITE_DOMAIN" "$IMAGE"
