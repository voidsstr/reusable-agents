# shellcheck shell=bash
#
# aws/aws.sh: AWS helpers shared by provision.sh, deploy.sh and teardown.sh.
# Source it after ../lib/common.sh and kd_load_config.
#
# The shared foundation (VPC, subnets, security groups, ECS cluster, ALB, RDS,
# IAM roles, log group) belongs to reusable-agents/install/deploy-aws.sh
# provision, which writes AWS_STATE_FILE (~/.aws-deploy/state.env). These
# scripts read it and add one site to it:
#   ECR repository APP_NAME, Secrets Manager secret AWS_SECRET_ID (APP_SECRETS
#   as JSON keys), ECS Fargate service + task family APP_NAME, target group
#   AWS_TARGET_GROUP, host-header rule(s) and an ACM certificate on the ALB.
#
# Parity with Azure (CLAUDE.md "keep AWS+Azure parallel"): the same deploy.env,
# the same image, the same container env names and secret refs, CPU/MEMORY as
# Fargate units (0.5 vCPU / 1 GiB = 512 / 1024), MIN..MAX tasks via Application
# Auto Scaling, the same gate, migrations and smoke check.

AWS_STATE_REQUIRED=(REGION ACCOUNT VPC_ID PRIV_SUBNET_0 PRIV_SUBNET_1 ECS_SG ECS_CLUSTER LOG_GROUP
  EXEC_ROLE TASK_ROLE ALB_ARN ALB_DNS)
declare -A AWSS=()

aws_validate_config() {
  kd_require_keys AWS_SECRET_ID
  kd_check_re AWS_SECRET_ID '^[A-Za-z0-9/_+=.@-]{1,512}$' 'a Secrets Manager secret name'
  kd_check_re AWS_TARGET_GROUP '^[A-Za-z0-9]([A-Za-z0-9-]{0,30}[A-Za-z0-9])?$' 'a target group name (at most 32 letters, digits and -)'
  kd_check_int AWS_CPU_TARGET_PERCENT 10 90
  kd_check_re AWS_SSL_POLICY '^[A-Za-z0-9-]+$' 'an ELB security policy name'
  [[ -z $AWS_DB_HOST ]] || kd_check_re AWS_DB_HOST '^[A-Za-z0-9.-]+$' 'a host name'
  case $KD_CPU_UNITS in
    256|512|1024|2048|4096) ;;
    *) kd_die "$KD_ENV_FILE: CPU=$CPU has no Fargate size (Fargate takes 0.25, 0.5, 1, 2 or 4 vCPU with MEMORY = 2 x CPU)" 2 ;;
  esac
  AWS_REPO=$APP_NAME
}

# aws_load_state: AWS_STATE_FILE -> AWSS[KEY]. Under --dry-run a missing file or
# key becomes a <KEY> placeholder.
aws_load_state() {
  local k v keys missing=()
  keys=$(IFS=,; printf '%s' "${AWS_STATE_REQUIRED[*]},RDS_ENDPOINT")
  if [[ -f $AWS_STATE_FILE ]]; then
    while IFS= read -r -d '' k && IFS= read -r -d '' v; do
      AWSS[$k]=$v
    done < <("$KD_PYTHON" -I "$KD_TOOL" env-dump "$AWS_STATE_FILE" --keys "$keys")
  elif ((!KD_DRY_RUN)); then
    kd_die "$AWS_STATE_FILE not found: run reusable-agents/install/deploy-aws.sh provision first (it writes the shared foundation's ids)" 2
  fi
  for k in "${AWS_STATE_REQUIRED[@]}"; do
    if [[ -z ${AWSS[$k]-} ]]; then
      missing+=("$k")
      AWSS[$k]="<$k>"
    fi
  done
  if ((${#missing[@]})); then
    if ((KD_DRY_RUN)); then
      kd_warn "$AWS_STATE_FILE lacks ${missing[*]} (placeholders in this preview)"
    else
      kd_die "$AWS_STATE_FILE lacks ${missing[*]}: re-run reusable-agents/install/deploy-aws.sh provision" 2
    fi
  fi
  AWS_REGION=${AWSS[REGION]}
  [[ $AWS_REGION == '<'* || $AWS_REGION =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || kd_die "REGION '$AWS_REGION' in $AWS_STATE_FILE is not a region" 2
  [[ ${AWSS[ACCOUNT]} == '<'* || ${AWSS[ACCOUNT]} =~ ^[0-9]{12}$ ]] || kd_die "ACCOUNT in $AWS_STATE_FILE is not an account id" 2
  AWS_REGISTRY="${AWSS[ACCOUNT]}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  AWS_CLUSTER=${AWSS[ECS_CLUSTER]}
  AWS_RESOURCE_ID="service/${AWS_CLUSTER}/${APP_NAME}"
}

aws_image() { printf '%s/%s:%s' "$AWS_REGISTRY" "$AWS_REPO" "$1"; }

aws_login_check() {
  local acct
  kd_probe_value acct account aws sts get-caller-identity --region "$AWS_REGION" --query Account --output text
  [[ -n $acct ]] || kd_die "the AWS CLI has no working credentials (aws configure / AWS_PROFILE)" 2
  if ((!KD_DRY_RUN)) && [[ $acct != "${AWSS[ACCOUNT]}" ]]; then
    kd_die "the AWS CLI is on account $acct but $AWS_STATE_FILE is for ${AWSS[ACCOUNT]}" 2
  fi
}

aws_service_exists() {  # VAR
  kd_probe_exists "$1" service aws ecs describe-services --region "$AWS_REGION" --cluster "$AWS_CLUSTER" \
    --services "$APP_NAME" --query "services[?status=='ACTIVE'].serviceName | [0]" --output text
}

# Ownership (parity with azure.sh az_require_app_owned): the ECS service, the
# Secrets Manager secret and the target group carry this site's
# affiliate-kit-app tag from the moment these scripts create them, so a
# deploy.env naming another site's resource (the foundation is shared) changes
# nothing. "adopt" lets provision.sh --adopt-existing tag an untagged one.
AWS_OWNED=" "
AWS_ADOPT_ARN=""

aws__owned() {  # KEY WHAT TAG ADOPT_FN
  kd_require_owner "$2" "$3" "$4"
  AWS_OWNED+="$1 "
}

aws_require_service_owned() {  # [adopt]
  [[ $AWS_OWNED != *" service "* ]] || return 0
  local tag fn=""
  [[ ${1:-} == adopt ]] && fn=aws_adopt_service
  kd_probe_strict tag service-owner-tag aws ecs describe-services --region "$AWS_REGION" --cluster "$AWS_CLUSTER" \
    --services "$APP_NAME" --include TAGS \
    --query "services[?status=='ACTIVE'] | [0].tags[?key=='$KD_OWNER_TAG'].value | [0]" --output text
  aws__owned service "ECS service $APP_NAME on $AWS_CLUSTER" "$tag" "$fn"
}

aws_adopt_service() {
  kd_probe_strict AWS_ADOPT_ARN service-arn aws ecs describe-services --region "$AWS_REGION" --cluster "$AWS_CLUSTER" \
    --services "$APP_NAME" --query "services[?status=='ACTIVE'] | [0].serviceArn" --output text
  kd_run aws ecs tag-resource --region "$AWS_REGION" --resource-arn "$AWS_ADOPT_ARN" \
    --tags "key=$KD_OWNER_TAG,value=$APP_NAME" || kd_die "tagging the ECS service failed" 1
}

aws_require_secret_owned() {  # [adopt]
  [[ $AWS_OWNED != *" secret "* ]] || return 0
  local tag fn=""
  [[ ${1:-} == adopt ]] && fn=aws_adopt_secret
  kd_probe_strict tag secret-owner-tag aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id "$AWS_SECRET_ID" \
    --query "Tags[?Key=='$KD_OWNER_TAG'].Value | [0]" --output text
  aws__owned secret "Secrets Manager secret $AWS_SECRET_ID" "$tag" "$fn"
}

aws_adopt_secret() {
  kd_run aws secretsmanager tag-resource --region "$AWS_REGION" --secret-id "$AWS_SECRET_ID" \
    --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" || kd_die "tagging the secret failed" 1
}

aws_require_tg_owned() {  # TARGET_GROUP_ARN [adopt]
  [[ $AWS_OWNED != *" target-group "* ]] || return 0
  local tag fn=""
  [[ ${2:-} == adopt ]] && fn=aws_adopt_tg
  AWS_ADOPT_ARN=$1
  kd_probe_strict tag tg-owner-tag aws elbv2 describe-tags --region "$AWS_REGION" --resource-arns "$1" \
    --query "TagDescriptions[0].Tags[?Key=='$KD_OWNER_TAG'].Value | [0]" --output text
  aws__owned target-group "target group $AWS_TARGET_GROUP" "$tag" "$fn"
}

aws_adopt_tg() {
  kd_run aws elbv2 add-tags --region "$AWS_REGION" --resource-arns "$AWS_ADOPT_ARN" \
    --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" || kd_die "tagging the target group failed" 1
}

# The ACM certificate and the host-header rule are found by SITE_DOMAIN, on the
# shared HTTPS listener, and the ECR repository by AWS_REPO: provision.sh tags
# each one when it creates it, so teardown deletes (and the domain phase
# changes) only this site's.
aws_require_cert_owned() {  # CERTIFICATE_ARN [adopt]
  [[ $AWS_OWNED != *" certificate "* ]] || return 0
  local tag fn=""
  [[ ${2:-} == adopt ]] && fn=aws_adopt_cert
  AWS_ADOPT_ARN=$1
  kd_probe_strict tag cert-owner-tag aws acm list-tags-for-certificate --region "$AWS_REGION" --certificate-arn "$1" \
    --query "Tags[?Key=='$KD_OWNER_TAG'].Value | [0]" --output text
  aws__owned certificate "ACM certificate $1 for $SITE_DOMAIN" "$tag" "$fn"
}

aws_adopt_cert() {
  kd_run aws acm add-tags-to-certificate --region "$AWS_REGION" --certificate-arn "$AWS_ADOPT_ARN" \
    --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" || kd_die "tagging the certificate failed" 1
}

aws_require_rule_owned() {  # RULE_ARN [adopt]
  [[ $AWS_OWNED != *" host-rule "* ]] || return 0
  local tag fn=""
  [[ ${2:-} == adopt ]] && fn=aws_adopt_rule
  AWS_ADOPT_ARN=$1
  kd_probe_strict tag rule-owner-tag aws elbv2 describe-tags --region "$AWS_REGION" --resource-arns "$1" \
    --query "TagDescriptions[0].Tags[?Key=='$KD_OWNER_TAG'].Value | [0]" --output text
  aws__owned host-rule "the HTTPS listener's host-header rule $1 for $SITE_DOMAIN" "$tag" "$fn"
}

aws_adopt_rule() {
  kd_run aws elbv2 add-tags --region "$AWS_REGION" --resource-arns "$AWS_ADOPT_ARN" \
    --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" || kd_die "tagging the host-header rule failed" 1
}

aws_require_repo_owned() {  # [adopt]
  [[ $AWS_OWNED != *" ecr-repository "* ]] || return 0
  local tag arn fn=""
  [[ ${1:-} == adopt ]] && fn=aws_adopt_repo
  kd_probe_strict arn ecr-repository-arn aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$AWS_REPO" \
    --query 'repositories[0].repositoryArn' --output text
  AWS_ADOPT_ARN=$arn
  # ListTagsForResource answers {"tags": [{"Key": ..., "Value": ...}]}: a lower-case list of capitalised pairs.
  kd_probe_strict tag ecr-owner-tag aws ecr list-tags-for-resource --region "$AWS_REGION" --resource-arn "$arn" \
    --query "tags[?Key=='$KD_OWNER_TAG'].Value | [0]" --output text
  aws__owned ecr-repository "ECR repository $AWS_REPO" "$tag" "$fn"
}

aws_adopt_repo() {
  kd_run aws ecr tag-resource --region "$AWS_REGION" --resource-arn "$AWS_ADOPT_ARN" \
    --tags "Key=$KD_OWNER_TAG,Value=$APP_NAME" || kd_die "tagging the ECR repository failed" 1
}

# aws_secret_arn VAR: the full ARN of AWS_SECRET_ID (an ARN-shaped placeholder under --dry-run).
aws_secret_arn() {
  local _sa_arn
  kd_probe_value _sa_arn secret-arn aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id "$AWS_SECRET_ID" \
    --query ARN --output text
  if ((KD_DRY_RUN)) && [[ -n $_sa_arn ]]; then
    _sa_arn="arn:aws:secretsmanager:${AWS_REGION}:${AWSS[ACCOUNT]}:secret:${AWS_SECRET_ID}-XXXXXX"
  fi
  printf -v "$1" '%s' "$_sa_arn"
}

# aws_taskdef OUTVAR IMAGE SECRET_ARN: register the task definition built from
# deploy.env (cpu/memory, env, secrets by ARN reference) and return its ARN.
aws_taskdef() {
  local _td_dst=$1 image=$2 sarn=$3 file json e m
  local -a args=(ecs-taskdef --family "$APP_NAME" --container-name "$APP_NAME" --image "$image"
    --cpu-units "$KD_CPU_UNITS" --memory-mib "$KD_MEMORY_MIB" --port "$TARGET_PORT" --health-path "$HEALTH_PATH"
    --exec-role-arn "${AWSS[EXEC_ROLE]}" --task-role-arn "${AWSS[TASK_ROLE]}" --log-group "${AWSS[LOG_GROUP]}"
    --region "$AWS_REGION" --stream-prefix "$APP_NAME")
  [[ $DOCKER_PLATFORM == linux/arm64 ]] && args+=(--arch ARM64)
  for e in "${KD_CENV[@]}"; do args+=(--env "$e"); done
  if ((${#KD_CSECRET_ENV[@]})); then
    [[ -n $sarn ]] || kd_die "secret $AWS_SECRET_ID does not exist: run provision.sh secrets first" 3
    for m in "${KD_CSECRET_ENV[@]}"; do args+=(--secret "${m%%=*}=${sarn}:${m#*=}::"); done
  fi
  kd_work_file taskdef.json file
  # The task definition's plain env ends up in AWS as plain text: no secret value may be in it.
  kd_guard_args "${args[@]}"
  if ((KD_DRY_RUN)); then
    json=$("$KD_PYTHON" -I "$KD_TOOL" "${args[@]}" --out -) || kd_die "cannot build the task definition" 2
    kd_show_file "$file" "$json"
  else
    "$KD_PYTHON" -I "$KD_TOOL" "${args[@]}" --out "$file" || kd_die "cannot build the task definition" 2
  fi
  kd_run_capture "$_td_dst" taskdef-arn aws ecs register-task-definition --region "$AWS_REGION" \
    --cli-input-json "file://$file" --query taskDefinition.taskDefinitionArn --output text \
    || kd_die "register-task-definition failed" 1
}

aws_scaling_target() {
  kd_run aws application-autoscaling register-scalable-target --region "$AWS_REGION" --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount --resource-id "$AWS_RESOURCE_ID" \
    --min-capacity "$MIN_REPLICAS" --max-capacity "$MAX_REPLICAS" || kd_die "register-scalable-target failed" 1
}

aws_ecr_login() {
  local cmd="aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_REGISTRY"
  if ((KD_DRY_RUN)); then
    printf '[dry-run] %s\n' "$cmd"
    return 0
  fi
  kd_log "+ $cmd"
  aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$AWS_REGISTRY" >/dev/null
}

# aws_target_group VAR: the target group ARN, empty when it does not exist.
aws_target_group() {
  kd_probe_exists "$1" target-group aws elbv2 describe-target-groups --region "$AWS_REGION" --names "$AWS_TARGET_GROUP" \
    --query 'TargetGroups[0].TargetGroupArn' --output text
}

aws_listener() {  # VAR PORT
  kd_probe_exists "$1" "listener-$2" aws elbv2 describe-listeners --region "$AWS_REGION" --load-balancer-arn "${AWSS[ALB_ARN]}" \
    --query "Listeners[?Port==\`$2\`].ListenerArn | [0]" --output text
}

aws_host_rule() {  # VAR LISTENER_ARN
  kd_probe_exists "$1" host-rule aws elbv2 describe-rules --region "$AWS_REGION" --listener-arn "$2" \
    --query "Rules[?Conditions[?Field=='host-header' && contains(not_null(HostHeaderConfig.Values, Values, \`[]\`), '$SITE_DOMAIN')]].RuleArn | [0]" \
    --output text
}

aws_certificate() {  # VAR
  kd_probe_exists "$1" certificate aws acm list-certificates --region "$AWS_REGION" \
    --certificate-statuses PENDING_VALIDATION ISSUED \
    --query "CertificateSummaryList[?DomainName=='$SITE_DOMAIN'].CertificateArn | [0]" --output text
}
