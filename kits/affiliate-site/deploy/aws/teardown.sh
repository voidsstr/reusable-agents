#!/usr/bin/env bash
# aws/teardown.sh: remove one affiliate site's AWS resources (parity with azure/teardown.sh).
#
#   bash vendor/affiliate-kit/deploy/aws/teardown.sh --env deploy.env --confirm APP_NAME [OPTIONS] [--dry-run]
#
#   (always)             scale the ECS service to 0 and delete it, deregister its
#                        auto scaling target and task definitions, delete its
#                        host-header rule, take its certificate off the HTTPS
#                        listener, delete its target group, and schedule the
#                        Secrets Manager secret for deletion (7-day recovery window)
#   --with-certificates  also delete the ACM certificate
#   --with-images        also delete the ECR repository and its images
#   --with-database      also DROP DATABASE DB_NAME (WITH FORCE) and its four roles
#                        on the RDS instance, as the admin. Irreversible.
#   --confirm NAME       must equal APP_NAME (not needed with --dry-run)
#   --confirm-database DB  with --with-database: must equal DB_NAME (not
#                        needed with --dry-run)
#
# Only this site's resources go: a service, target group or secret without
# this site's affiliate-kit-app tag stops the teardown before anything is
# deleted, and the database and roles are dropped only when each one that
# exists carries this site's affiliate-kit marker. The shared foundation (VPC,
# ALB and its listeners, cluster, RDS instance, IAM roles, log group) is never
# touched.
# Refuses to run under bash -x (it would print secret values).
#
# Exit: 0 ok; 1 a step failed; 2 usage or config error (or another site's resource).
set -euo pipefail
# shellcheck source=../lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../lib/common.sh"
# shellcheck source=aws.sh
source "$KD_DEPLOY_DIR/aws/aws.sh"

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
aws_validate_config
aws_load_state
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
((!with_db)) || kd_require_keys AWS_DB_ADMIN_USER AWS_DB_ADMIN_PASSWORD
kd_banner "aws/teardown.sh"
kd_need_tool aws
aws_login_check

# Everything this teardown deletes by name must be this site's: check all of it
# before the first delete.
kd_phase "ownership"
aws_service_exists svc
[[ -z $svc ]] || aws_require_service_owned
aws_target_group tg
[[ -z $tg ]] || aws_require_tg_owned "$tg"
kd_probe_exists sec secret aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id "$AWS_SECRET_ID" \
  --query ARN --output text
[[ -z $sec ]] || aws_require_secret_owned

kd_phase "ECS service $APP_NAME"
if [[ -n $svc ]]; then
  kd_run aws ecs update-service --region "$AWS_REGION" --cluster "$AWS_CLUSTER" --service "$APP_NAME" --desired-count 0 \
    --query service.serviceName --output text || kd_die "scaling the service to 0 failed" 1
  kd_run aws ecs delete-service --region "$AWS_REGION" --cluster "$AWS_CLUSTER" --service "$APP_NAME" --force \
    --query service.serviceName --output text || kd_die "delete-service failed" 1
else
  kd_log "ECS service $APP_NAME does not exist"
fi
kd_probe_exists scal scalable-target aws application-autoscaling describe-scalable-targets --region "$AWS_REGION" \
  --service-namespace ecs --resource-ids "$AWS_RESOURCE_ID" --query 'ScalableTargets[0].ResourceId' --output text
if [[ -n $scal ]]; then
  kd_run aws application-autoscaling deregister-scalable-target --region "$AWS_REGION" --service-namespace ecs \
    --scalable-dimension ecs:service:DesiredCount --resource-id "$AWS_RESOURCE_ID" || kd_die "deregister-scalable-target failed" 1
fi
tds=""
kd_probe_exists tds taskdefs aws ecs list-task-definitions --region "$AWS_REGION" --family-prefix "$APP_NAME" \
  --status ACTIVE --query 'taskDefinitionArns' --output text
read -ra td_list <<< "${tds//$'\n'/ }"
for td in "${td_list[@]}"; do
  kd_run aws ecs deregister-task-definition --region "$AWS_REGION" --task-definition "$td" \
    --query taskDefinition.taskDefinitionArn --output text || kd_die "deregister-task-definition failed" 1
done

kd_phase "load balancer"
aws_listener l443 443
aws_certificate cert
if [[ -n $l443 ]]; then
  aws_host_rule rule "$l443"
  if [[ -n $rule ]]; then
    kd_run aws elbv2 delete-rule --region "$AWS_REGION" --rule-arn "$rule" || kd_die "delete-rule failed" 1
  fi
  if [[ -n $cert ]]; then
    kd_run aws elbv2 remove-listener-certificates --region "$AWS_REGION" --listener-arn "$l443" \
      --certificates "CertificateArn=$cert" || kd_warn "the certificate is not on the listener (or is its default certificate)"
  fi
fi
if [[ -n $tg ]]; then
  kd_run aws elbv2 delete-target-group --region "$AWS_REGION" --target-group-arn "$tg" || kd_die "delete-target-group failed" 1
fi

kd_phase "secrets"
if [[ -n $sec ]]; then
  kd_run aws secretsmanager delete-secret --region "$AWS_REGION" --secret-id "$AWS_SECRET_ID" \
    --recovery-window-in-days 7 --query DeletionDate --output text || kd_die "delete-secret failed" 1
fi

if ((with_certs)) && [[ -n $cert ]]; then
  kd_phase "certificate"
  kd_run aws acm delete-certificate --region "$AWS_REGION" --certificate-arn "$cert" \
    || kd_die "delete-certificate failed (still the HTTPS listener's default certificate?)" 1
fi

if ((with_images)); then
  kd_phase "images"
  kd_probe_exists repo ecr-repository aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$AWS_REPO" \
    --query 'repositories[0].repositoryUri' --output text
  if [[ -n $repo ]]; then
    kd_run aws ecr delete-repository --region "$AWS_REGION" --repository-name "$AWS_REPO" --force \
      --query repository.repositoryName --output text || kd_die "delete-repository failed" 1
  fi
fi

if ((with_db)); then
  kd_phase "database"
  host=${AWS_DB_HOST:-${AWSS[RDS_ENDPOINT]-}}
  [[ -n $host ]] || kd_die "no database host: set AWS_DB_HOST or RDS_ENDPOINT in $AWS_STATE_FILE" 2
  sql=$(kd_sql_drop)
  kd_psql_exec "$host" "$AWS_DB_ADMIN_USER" postgres "$AWS_DB_ADMIN_PASSWORD" "drop database and roles" <<< "$sql" \
    || kd_die "dropping the database failed" 1
fi
kd_log "aws/teardown.sh: done"
