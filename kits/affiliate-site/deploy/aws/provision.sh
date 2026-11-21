#!/usr/bin/env bash
# aws/provision.sh: idempotent AWS setup for one affiliate site (the parallel
# target; the same deploy.env as azure/provision.sh).
#
#   bash vendor/affiliate-kit/deploy/aws/provision.sh --env deploy.env [--dry-run] PHASE... [OPTIONS]
#
# Phases run in the order given; `all` = check database secrets domain app
# (ECS can only attach a service to a target group that is behind a listener,
# so the domain phase comes before app here).
#   foundation  verify AWS_STATE_FILE has the shared foundation's ids; it is
#               created by reusable-agents/install/deploy-aws.sh provision, never here
#   check       credentials and account, the ALB and cluster, the config, and
#               which secrets resolve (by name; values are never shown)
#   database    the same roles, database and grants as azure/provision.sh, on
#               the RDS instance (AWS_DB_HOST, else RDS_ENDPOINT), with the same
#               ownership markers and the same refusal of another site's objects
#   secrets     Secrets Manager secret AWS_SECRET_ID = {secret-name: value} from
#               AWS_APP_SECRETS (default APP_SECRETS), written from a 0600 file
#               (file://); --restart forces a new deployment so tasks read it
#   domain      an ACM certificate (DNS validation) for SITE_HOSTNAMES; prints the
#               DNS records (validation CNAMEs and the ALB target) and exits 3
#               until ACM reports ISSUED; then the target group, the HTTPS:443
#               listener (certificate added) and HTTP:80 redirect listener when
#               missing, and a host-header rule forwarding SITE_HOSTNAMES
#   app         ECR repository, task definition (CPU/MEMORY as Fargate units,
#               env, secrets by ARN), the ECS service (MIN_REPLICAS tasks,
#               private subnets, circuit breaker with rollback) and Application
#               Auto Scaling MIN..MAX on CPU (AWS_CPU_TARGET_PERCENT)
#
# The service, the secret, the target group, the ACM certificate, the
# host-header rule and the ECR repository are tagged affiliate-kit-app=
# APP_NAME when created here; an existing one without that tag (another
# site's) stops the phase before it is changed. The certificate and the rule
# are found by SITE_DOMAIN (the rule on the shared HTTPS listener).
#
# Options:
#   --image-tag TAG        app: the image to start the service with (default: latest)
#   --write-new-dsns FILE  database: as in azure/provision.sh
#   --restart              secrets: force a new deployment afterwards
#   --adopt-existing       database, secrets, domain, app: take over roles, a
#                          database, a service, secret, target group, ACM
#                          certificate, host-header rule or ECR repository that
#                          exist without the kit's marker, marking them as this
#                          site's (as in azure/provision.sh). Never another kit site's
#   --dry-run              print every aws / psql command instead of running it
#
# Refuses to run under bash -x (it would print secret values).
#
# Exit: 0 ok; 1 a step failed; 2 usage or config error; 3 the operator has to
# act (DNS records, certificate validation, a missing secret or image), then re-run.
# shellcheck disable=SC2329  # phase_* functions are called as "phase_$p"
set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../lib/common.sh"
# shellcheck source=aws.sh
source "$KD_DEPLOY_DIR/aws/aws.sh"

KD_DRY_DEFAULT=absent
IMAGE_TAG=""
NEW_DSNS=""
RESTART=0
phases=()

kd_common_args "$@"
set -- "${KD_REST[@]}"
while (($#)); do
  case $1 in
    foundation|check|database|secrets|domain|app|all) phases+=("$1"); shift ;;
    --image-tag) (($# >= 2)) || kd_die "--image-tag needs a value" 2; IMAGE_TAG=$2; shift 2 ;;
    --image-tag=*) IMAGE_TAG=${1#*=}; shift ;;
    --write-new-dsns) (($# >= 2)) || kd_die "--write-new-dsns needs a file" 2; NEW_DSNS=$2; shift 2 ;;
    --write-new-dsns=*) NEW_DSNS=${1#*=}; shift ;;
    --restart) RESTART=1; shift ;;
    --adopt-existing) KD_ADOPT=1; shift ;;
    -h|--help) kd_usage "${BASH_SOURCE[0]}"; exit 0 ;;
    *) kd_die "unknown argument: $1 (see --help)" 2 ;;
  esac
done
((${#phases[@]})) || kd_die "name at least one phase: foundation check database secrets domain app all (see --help)" 2

kd_load_config
aws_validate_config
aws_load_state
[[ -z $IMAGE_TAG ]] || kd_check_tag "$IMAGE_TAG"
kd_banner "aws/provision.sh ${phases[*]}"
kd_need_tool aws

# ─── phases ───────────────────────────────────────────────────────────────────

phase_foundation() {
  kd_phase "foundation (owned by reusable-agents/install/deploy-aws.sh provision)"
  local k
  for k in "${AWS_STATE_REQUIRED[@]}" RDS_ENDPOINT; do
    kd_log "$k=${AWSS[$k]:-<missing>}"
  done
  [[ -n ${AWSS[RDS_ENDPOINT]-} || -n $AWS_DB_HOST ]] || kd_warn "no RDS_ENDPOINT in $AWS_STATE_FILE and no AWS_DB_HOST: the database phase has no server"
}

phase_check() {
  kd_phase "check"
  aws_login_check
  local v e kind ref state
  kd_probe_value v load-balancer aws elbv2 describe-load-balancers --region "$AWS_REGION" \
    --load-balancer-arns "${AWSS[ALB_ARN]}" --query 'LoadBalancers[0].DNSName' --output text
  [[ -n $v ]] || kd_die "the ALB in $AWS_STATE_FILE does not exist" 2
  kd_probe_value v cluster aws ecs describe-clusters --region "$AWS_REGION" --clusters "$AWS_CLUSTER" \
    --query "clusters[?status=='ACTIVE'].clusterName | [0]" --output text
  [[ -n $v ]] || kd_die "ECS cluster $AWS_CLUSTER is not active" 2
  kd_log "app $APP_NAME on $AWS_CLUSTER; image $AWS_REGISTRY/$AWS_REPO; ALB ${AWSS[ALB_DNS]}"
  kd_log "compute ${KD_CPU_UNITS} CPU units / ${KD_MEMORY_MIB} MiB, tasks $MIN_REPLICAS..$MAX_REPLICAS; hostnames $SITE_HOSTNAMES"
  local -a items=()
  read -ra items <<< "$AWS_APP_SECRETS"
  for e in "${items[@]}"; do
    state=missing
    kd_secret_get "${e#*=}" v && state="set"
    kd_log "container secret ${e%%=*} (${e#*=}): $state"
  done
  for kind in "${KD_ROLE_KINDS[@]}"; do
    ref=$(kd_var "AWS_DB_${kind}_DSN")
    state=missing
    [[ -n $ref ]] && kd_secret_get "$ref" v && state="set"
    kd_log "AWS_DB_${kind}_DSN (${ref:-unset}): $state"
  done
  state=missing
  [[ -n $AWS_DB_ADMIN_PASSWORD ]] && kd_secret_get "$AWS_DB_ADMIN_PASSWORD" v && state="set"
  kd_log "AWS_DB_ADMIN_PASSWORD (${AWS_DB_ADMIN_PASSWORD:-unset}): $state"
}

phase_database() {
  kd_phase "database"
  kd_require_keys AWS_DB_ADMIN_USER AWS_DB_ADMIN_PASSWORD
  local host=${AWS_DB_HOST:-${AWSS[RDS_ENDPOINT]-}}
  [[ -n $host ]] || kd_die "no database host: set AWS_DB_HOST or RDS_ENDPOINT in $AWS_STATE_FILE" 2
  kd_db_provision "$host" "$AWS_DB_ADMIN_USER" "$AWS_DB_ADMIN_PASSWORD" "$NEW_DSNS" AWS_DB
}

phase_secrets() {
  kd_phase "secrets"
  local e name ref path file arn svc
  local -a items=() pairs=()
  read -ra items <<< "$AWS_APP_SECRETS"
  if ((${#items[@]} == 0)); then
    kd_log "no container secrets to store"
    return 0
  fi
  kd_probe_exists arn secret-arn aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id "$AWS_SECRET_ID" \
    --query ARN --output text
  [[ -z $arn ]] || aws_require_secret_owned adopt
  for e in "${items[@]}"; do
    name=${e%%=*}
    ref=${e#*=}
    kd_secret_file "$name" "$ref" path || kd_die "container secret '$name' ($ref) is not set: add it to the environment or $KD_SECRETS_FILE" 3
    pairs+=("$name=$path")
  done
  kd_work_file app-secrets.json file
  if ((KD_DRY_RUN)); then
    printf '[dry-run] (file %s, 0600) a JSON object with the keys: %s\n' "$file" "${pairs[*]%%=*}"
  else
    "$KD_PYTHON" -I "$KD_TOOL" json-from-files --out "$file" "${pairs[@]}" || kd_die "cannot build the secret document" 1
  fi
  if [[ -z $arn ]]; then
    kd_run aws secretsmanager create-secret --region "$AWS_REGION" --name "$AWS_SECRET_ID" \
      --description "affiliate-kit container secrets for $APP_NAME" --secret-string "file://$file" \
      --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" --query ARN --output text || kd_die "create-secret failed" 1
    kd_dry_created secret-arn
  else
    kd_run aws secretsmanager put-secret-value --region "$AWS_REGION" --secret-id "$AWS_SECRET_ID" \
      --secret-string "file://$file" --query VersionId --output text || kd_die "put-secret-value failed" 1
  fi
  if ((RESTART)); then
    aws_service_exists svc
    if [[ -n $svc ]]; then
      aws_require_service_owned adopt
      kd_run aws ecs update-service --region "$AWS_REGION" --cluster "$AWS_CLUSTER" --service "$APP_NAME" \
        --force-new-deployment --query service.serviceName --output text || kd_die "update-service failed" 1
    else
      kd_log "no ECS service yet; nothing to restart"
    fi
  else
    kd_log "running tasks read new secret values only when replaced: re-run with --restart, or deploy"
  fi
}

DNS_WAIT_ARN=""
cert_records_ready() {
  local recs
  kd_probe_value recs validation-records aws acm describe-certificate --region "$AWS_REGION" \
    --certificate-arn "$DNS_WAIT_ARN" --query 'Certificate.DomainValidationOptions[].ResourceRecord.Name' --output text
  [[ -n $recs ]]
}

phase_domain() {
  kd_phase "domain"
  local cert status records line tg l443 l80 rule prio p token
  local -a hosts=() sans=() fields=()
  read -ra hosts <<< "$SITE_HOSTNAMES"
  for p in "${hosts[@]}"; do [[ $p == "$SITE_DOMAIN" ]] || sans+=("$p"); done
  # An existing target group, certificate and host-header rule must be this
  # site's before anything is requested or changed: the certificate goes onto
  # the shared listener, and modify-rule would point the rule at this site.
  aws_target_group tg
  [[ -z $tg ]] || aws_require_tg_owned "$tg" adopt
  aws_certificate cert
  [[ -z $cert ]] || aws_require_cert_owned "$cert" adopt
  aws_listener l443 443
  if [[ -n $l443 ]]; then
    aws_host_rule rule "$l443"
    [[ -z $rule ]] || aws_require_rule_owned "$rule" adopt
  fi
  if [[ -z $cert ]]; then
    token="kit_${APP_NAME//-/_}"
    local -a req=(aws acm request-certificate --region "$AWS_REGION" --domain-name "$SITE_DOMAIN"
      --validation-method DNS --idempotency-token "${token:0:32}" --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME"
      --query CertificateArn --output text)
    ((${#sans[@]})) && req+=(--subject-alternative-names "${sans[@]}")
    kd_run_capture cert certificate-arn "${req[@]}" || kd_die "request-certificate failed" 1
    DNS_WAIT_ARN=$cert
    kd_wait_until "ACM publishes the validation records" 120 5 cert_records_ready || true
  fi
  kd_probe_value status certificate-status aws acm describe-certificate --region "$AWS_REGION" --certificate-arn "$cert" \
    --query Certificate.Status --output text
  kd_probe_value records validation-records aws acm describe-certificate --region "$AWS_REGION" --certificate-arn "$cert" \
    --query 'Certificate.DomainValidationOptions[].[ResourceRecord.Name, ResourceRecord.Type, ResourceRecord.Value]' \
    --output text

  printf '\nDNS records (create them at the DNS provider):\n'
  printf '  %-6s %-60s %s\n' TYPE NAME VALUE
  if ((KD_DRY_RUN)); then
    printf '  %-6s %-60s %s\n' CNAME "<ACM validation name per hostname>" "<ACM validation value> (keep it: ACM renews over it)"
  else
    while IFS= read -r line; do
      [[ -n $line ]] || continue
      read -ra fields <<< "$line"
      printf '  %-6s %-60s %s\n' "${fields[1]:-CNAME}" "${fields[0]}" "${fields[2]:-} (ACM validation; keep it for renewals)"
    done < <(printf '%s\n' "$records" | sort -u)
  fi
  for p in "${hosts[@]}"; do
    if [[ $p == "$SITE_DOMAIN" ]]; then
      printf '  %-6s %-60s %s\n' ALIAS "$p" "${AWSS[ALB_DNS]}   (an apex CNAME flattened by the DNS provider, or a Route 53 alias)"
    else
      printf '  %-6s %-60s %s\n' CNAME "$p" "${AWSS[ALB_DNS]}"
    fi
  done
  printf '\n'
  if ((!KD_DRY_RUN)) && [[ $status != ISSUED ]]; then
    kd_warn "certificate $cert is ${status:-unknown}: create the validation records above, wait until ACM shows ISSUED, then re-run the domain phase"
    return 3
  fi

  if [[ -z $tg ]]; then
    kd_run_capture tg target-group-arn aws elbv2 create-target-group --region "$AWS_REGION" --name "$AWS_TARGET_GROUP" \
      --protocol HTTP --port "$TARGET_PORT" --vpc-id "${AWSS[VPC_ID]}" --target-type ip \
      --health-check-protocol HTTP --health-check-path "$HEALTH_PATH" --health-check-interval-seconds 15 \
      --healthy-threshold-count 2 --unhealthy-threshold-count 3 --matcher HttpCode=200 \
      --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" --query 'TargetGroups[0].TargetGroupArn' --output text \
      || kd_die "create-target-group failed" 1
    kd_dry_created target-group
  fi
  aws_listener l443 443
  if [[ -z $l443 ]]; then
    kd_run_capture l443 listener-arn aws elbv2 create-listener --region "$AWS_REGION" \
      --load-balancer-arn "${AWSS[ALB_ARN]}" --protocol HTTPS --port 443 --ssl-policy "$AWS_SSL_POLICY" \
      --certificates "CertificateArn=$cert" \
      --default-actions '[{"Type":"fixed-response","FixedResponseConfig":{"StatusCode":"404","ContentType":"text/plain","MessageBody":"Not found"}}]' \
      --query 'Listeners[0].ListenerArn' --output text || kd_die "create-listener 443 failed" 1
    kd_dry_created listener-443
  else
    kd_run aws elbv2 add-listener-certificates --region "$AWS_REGION" --listener-arn "$l443" \
      --certificates "CertificateArn=$cert" --query 'Certificates[0].CertificateArn' --output text \
      || kd_die "add-listener-certificates failed" 1
  fi
  aws_listener l80 80
  if [[ -z $l80 ]]; then
    kd_run aws elbv2 create-listener --region "$AWS_REGION" --load-balancer-arn "${AWSS[ALB_ARN]}" --protocol HTTP --port 80 \
      --default-actions '[{"Type":"redirect","RedirectConfig":{"Protocol":"HTTPS","Port":"443","StatusCode":"HTTP_301"}}]' \
      --query 'Listeners[0].ListenerArn' --output text || kd_die "create-listener 80 failed" 1
  fi
  local conditions actions
  conditions=$(printf '"%s",' "${hosts[@]}")
  conditions="[{\"Field\":\"host-header\",\"HostHeaderConfig\":{\"Values\":[${conditions%,}]}}]"
  actions="[{\"Type\":\"forward\",\"TargetGroupArn\":\"$tg\"}]"
  aws_host_rule rule "$l443"
  if [[ -z $rule ]]; then
    kd_probe_value prio rule-priorities aws elbv2 describe-rules --region "$AWS_REGION" --listener-arn "$l443" \
      --query 'Rules[].Priority' --output text
    p=0
    local -a prios=()
    read -ra prios <<< "$prio"
    for line in "${prios[@]}"; do
      if [[ $line =~ ^[0-9]+$ ]] && ((10#$line > p)); then p=$((10#$line)); fi
    done
    kd_run aws elbv2 create-rule --region "$AWS_REGION" --listener-arn "$l443" --priority $((p + 1)) \
      --conditions "$conditions" --actions "$actions" --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" \
      --query 'Rules[0].RuleArn' --output text || kd_die "create-rule failed" 1
  else
    aws_require_rule_owned "$rule" adopt
    kd_run aws elbv2 modify-rule --region "$AWS_REGION" --rule-arn "$rule" --conditions "$conditions" \
      --actions "$actions" --query 'Rules[0].RuleArn' --output text || kd_die "modify-rule failed" 1
  fi
}

phase_app() {
  kd_phase "app"
  local repo digest tg attached sarn td svc policy tag=${IMAGE_TAG:-latest}
  aws_login_check
  # What already exists must be this site's before anything is created or changed.
  aws_target_group tg
  [[ -n $tg ]] || kd_die "target group $AWS_TARGET_GROUP does not exist: run the domain phase first" 3
  aws_require_tg_owned "$tg" adopt
  aws_service_exists svc
  [[ -z $svc ]] || aws_require_service_owned adopt
  kd_probe_exists repo ecr-repository aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$AWS_REPO" \
    --query 'repositories[0].repositoryUri' --output text
  if [[ -z $repo ]]; then
    kd_run aws ecr create-repository --region "$AWS_REGION" --repository-name "$AWS_REPO" \
      --image-scanning-configuration scanOnPush=true --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" \
      --query repository.repositoryUri --output text || kd_die "create-repository failed" 1
    kd_dry_created ecr-repository
  else
    aws_require_repo_owned adopt
  fi
  kd_probe_value digest image aws ecr describe-images --region "$AWS_REGION" --repository-name "$AWS_REPO" \
    --image-ids "imageTag=$tag" --query 'imageDetails[0].imageDigest' --output text
  [[ -n $digest ]] || kd_die "$(aws_image "$tag") is not in ECR yet: push it first (aws/deploy.sh --env $KD_ENV_FILE --build-only), then re-run" 3
  kd_probe_value attached target-group-listener aws elbv2 describe-target-groups --region "$AWS_REGION" \
    --target-group-arns "$tg" --query 'TargetGroups[0].LoadBalancerArns[0]' --output text
  [[ -n $attached ]] || kd_die "target group $AWS_TARGET_GROUP is not behind a listener yet: finish the domain phase first" 3
  kd_resolve_ai_exclude
  kd_container_env
  aws_secret_arn sarn
  aws_taskdef td "$(aws_image "$tag")" "$sarn"
  local deploycfg="deploymentCircuitBreaker={enable=true,rollback=true},maximumPercent=200,minimumHealthyPercent=100"
  if [[ -z $svc ]]; then
    kd_run aws ecs create-service --region "$AWS_REGION" --cluster "$AWS_CLUSTER" --service-name "$APP_NAME" \
      --task-definition "$td" --desired-count "$MIN_REPLICAS" --launch-type FARGATE --platform-version LATEST \
      --network-configuration "awsvpcConfiguration={subnets=[${AWSS[PRIV_SUBNET_0]},${AWSS[PRIV_SUBNET_1]}],securityGroups=[${AWSS[ECS_SG]}],assignPublicIp=DISABLED}" \
      --load-balancers "targetGroupArn=$tg,containerName=$APP_NAME,containerPort=$TARGET_PORT" \
      --health-check-grace-period-seconds 60 --deployment-configuration "$deploycfg" \
      --propagate-tags SERVICE --enable-ecs-managed-tags --tags "key=$KD_OWNER_TAG,value=$APP_NAME" \
      --query service.serviceArn --output text || kd_die "create-service failed" 1
  else
    kd_run aws ecs update-service --region "$AWS_REGION" --cluster "$AWS_CLUSTER" --service "$APP_NAME" \
      --task-definition "$td" --deployment-configuration "$deploycfg" --health-check-grace-period-seconds 60 \
      --query service.serviceArn --output text || kd_die "update-service failed" 1
  fi
  aws_scaling_target
  kd_work_file scaling-policy.json policy
  if ((KD_DRY_RUN)); then
    kd_show_file "$policy" "$("$KD_PYTHON" -I "$KD_TOOL" scaling-policy --target-cpu "$AWS_CPU_TARGET_PERCENT" --out -)"
  else
    "$KD_PYTHON" -I "$KD_TOOL" scaling-policy --target-cpu "$AWS_CPU_TARGET_PERCENT" --out "$policy" \
      || kd_die "cannot build the scaling policy" 2
  fi
  kd_run aws application-autoscaling put-scaling-policy --region "$AWS_REGION" --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount --resource-id "$AWS_RESOURCE_ID" \
    --policy-name "$APP_NAME-cpu-target" --policy-type TargetTrackingScaling \
    --target-tracking-scaling-policy-configuration "file://$policy" --query PolicyARN --output text \
    || kd_die "put-scaling-policy failed" 1
  kd_log "service $APP_NAME: $MIN_REPLICAS..$MAX_REPLICAS tasks behind https://$SITE_DOMAIN (ALB ${AWSS[ALB_DNS]})"
}

# ─── main ─────────────────────────────────────────────────────────────────────

expanded=()
for p in "${phases[@]}"; do
  if [[ $p == all ]]; then expanded+=(check database secrets domain app); else expanded+=("$p"); fi
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
kd_log "aws/provision.sh: ${expanded[*]} done"
exit "$final"
