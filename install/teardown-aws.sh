#!/usr/bin/env bash
# Idempotent teardown for the AWS infrastructure provisioned by
# install/deploy-aws.sh. Mirrors that script's phases in reverse.
#
# Phases:
#   services   — stop + delete ECS services + task defs + target groups
#   alb        — delete the ALB + listeners
#   rds        — delete the RDS instance (final snapshot optional)
#   s3         — empty + delete S3 buckets (DANGEROUS — requires --confirm)
#   secrets    — delete Secrets Manager entries (scheduled, 7-day recovery)
#   ecr        — delete all images + ECR repos (DANGEROUS)
#   ecs        — delete the ECS cluster
#   iam        — detach + delete IAM roles
#   network    — release NAT + EIP, delete VPC + SGs + subnets + IGW
#   state      — wipe ~/.aws-deploy/state.env
#   all        — full teardown (requires --confirm)
#
# Default behavior is DRY RUN — pass `--confirm` to actually delete.
#
# Usage:
#   bash install/teardown-aws.sh services --confirm
#   bash install/teardown-aws.sh all --confirm
#   bash install/teardown-aws.sh all                # dry-run preview
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="${AWS_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
STATE="$HOME/.aws-deploy/state.env"
[ -f "$STATE" ] || { echo "no state file at $STATE — nothing to tear down"; exit 0; }
source "$STATE"

CONFIRM=0
for arg in "$@"; do [ "$arg" = "--confirm" ] && CONFIRM=1; done
PHASE="${1:-all}"

run() {
  if [ "$CONFIRM" = 1 ]; then "$@"; else echo "  DRYRUN: $*"; fi
}
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# --- services ---
teardown_services() {
  log "phase: services"
  for app in agents-api agents-ui aisleprompt specpicks hearthnote nsc-website application-research nsc-db-restore; do
    if aws ecs describe-services --cluster "${ECS_CLUSTER:-nsc-apps}" --services "$app" --query "services[0].status" --output text 2>/dev/null | grep -q ACTIVE; then
      run aws ecs update-service --cluster "$ECS_CLUSTER" --service "$app" --desired-count 0 >/dev/null
      run aws ecs delete-service --cluster "$ECS_CLUSTER" --service "$app" --force >/dev/null
      log "  deleted service: $app"
    fi
    # Deregister task definitions
    for td in $(aws ecs list-task-definitions --family-prefix "$app" --query 'taskDefinitionArns[]' --output text 2>/dev/null); do
      run aws ecs deregister-task-definition --task-definition "$td" >/dev/null
    done
  done
  # Target groups
  for tg in $(aws elbv2 describe-target-groups --query "TargetGroups[?starts_with(TargetGroupName,'tg-')].TargetGroupArn" --output text 2>/dev/null); do
    run aws elbv2 delete-target-group --target-group-arn "$tg" >/dev/null
    log "  deleted target group: $(basename $tg)"
  done
}

# --- alb ---
teardown_alb() {
  log "phase: alb"
  if [ -n "${ALB_ARN:-}" ]; then
    for l in $(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --query "Listeners[].ListenerArn" --output text 2>/dev/null); do
      run aws elbv2 delete-listener --listener-arn "$l"
    done
    run aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN"
    log "  deleted ALB"
  fi
}

# --- rds ---
teardown_rds() {
  log "phase: rds"
  if aws rds describe-db-instances --db-instance-identifier "${RDS_INSTANCE:-nsc-apps-db}" >/dev/null 2>&1; then
    run aws rds delete-db-instance --db-instance-identifier "$RDS_INSTANCE" \
      --skip-final-snapshot --delete-automated-backups
    log "  rds deletion triggered (async ~5min)"
    if [ "$CONFIRM" = 1 ]; then
      aws rds wait db-instance-deleted --db-instance-identifier "$RDS_INSTANCE" 2>&1 | head -1 || true
    fi
  fi
  for g in nsc-db-subnets nsc-db-subnets-public; do
    aws rds describe-db-subnet-groups --db-subnet-group-name "$g" >/dev/null 2>&1 \
      && run aws rds delete-db-subnet-group --db-subnet-group-name "$g"
  done
}

# --- s3 ---
teardown_s3() {
  log "phase: s3 (will EMPTY + DELETE buckets)"
  [ "$CONFIRM" = 1 ] || { log "  DRYRUN — would empty + delete nsc-agents + nsc-recipe-images"; return; }
  for b in "nsc-agents-${ACCOUNT}" "nsc-recipe-images-${ACCOUNT}"; do
    if aws s3api head-bucket --bucket "$b" 2>/dev/null; then
      log "  emptying s3://$b/ (versioned)"
      # delete all object versions including delete markers
      aws s3api list-object-versions --bucket "$b" --output json --query 'Versions[].{Key:Key,VersionId:VersionId}' 2>/dev/null \
        | python3 -c "
import json,sys,subprocess,os
b=os.environ['B']
items=json.load(sys.stdin) or []
for chunk in [items[i:i+1000] for i in range(0,len(items),1000)]:
    objs={'Objects':[{'Key':o['Key'],'VersionId':o['VersionId']} for o in chunk]}
    subprocess.run(['aws','s3api','delete-objects','--bucket',b,'--delete',json.dumps(objs)],check=True,capture_output=True)
" B="$b" || true
      aws s3api list-object-versions --bucket "$b" --output json --query 'DeleteMarkers[].{Key:Key,VersionId:VersionId}' 2>/dev/null \
        | python3 -c "
import json,sys,subprocess,os
b=os.environ['B']
items=json.load(sys.stdin) or []
for chunk in [items[i:i+1000] for i in range(0,len(items),1000)]:
    objs={'Objects':[{'Key':o['Key'],'VersionId':o['VersionId']} for o in chunk]}
    subprocess.run(['aws','s3api','delete-objects','--bucket',b,'--delete',json.dumps(objs)],check=True,capture_output=True)
" B="$b" || true
      aws s3 rb "s3://$b" --force >/dev/null
      log "  deleted: $b"
    fi
  done
}

# --- secrets ---
teardown_secrets() {
  log "phase: secrets (7-day recovery window)"
  for s in nsc/agents nsc/aisleprompt nsc/specpicks nsc/hearthnote nsc/nsc-website nsc/application-research nsc/rds-master; do
    aws secretsmanager describe-secret --secret-id "$s" >/dev/null 2>&1 \
      && run aws secretsmanager delete-secret --secret-id "$s" --recovery-window-in-days 7 >/dev/null \
      && log "  scheduled deletion: $s"
  done
}

# --- ecr ---
teardown_ecr() {
  log "phase: ecr"
  for r in agents-api agents-ui aisleprompt specpicks hearthnote nsc-website application-research postgres; do
    aws ecr describe-repositories --repository-names "$r" >/dev/null 2>&1 \
      && run aws ecr delete-repository --repository-name "$r" --force >/dev/null \
      && log "  deleted: $r"
  done
}

# --- ecs cluster ---
teardown_ecs_cluster() {
  log "phase: ecs cluster"
  aws ecs describe-clusters --clusters "${ECS_CLUSTER:-nsc-apps}" --query "clusters[0].status" --output text 2>/dev/null | grep -q ACTIVE \
    && run aws ecs delete-cluster --cluster "$ECS_CLUSTER" >/dev/null \
    && log "  deleted cluster: $ECS_CLUSTER"
  aws logs describe-log-groups --log-group-name-prefix "${LOG_GROUP:-/ecs/nsc-apps}" --query 'logGroups[0]' --output text 2>/dev/null | grep -q nsc-apps \
    && run aws logs delete-log-group --log-group-name "$LOG_GROUP"
}

# --- iam ---
teardown_iam() {
  log "phase: iam"
  for role in ecsTaskExecutionRole nscAppsTaskRole nsc-db-restore-tmp; do
    aws iam get-role --role-name "$role" >/dev/null 2>&1 || continue
    for p in $(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
      run aws iam detach-role-policy --role-name "$role" --policy-arn "$p"
    done
    run aws iam delete-role --role-name "$role"
    log "  deleted role: $role"
  done
  # Instance profile (EC2 restore helper)
  aws iam get-instance-profile --instance-profile-name nsc-db-restore-tmp >/dev/null 2>&1 \
    && run aws iam delete-instance-profile --instance-profile-name nsc-db-restore-tmp
}

# --- network (VPC, subnets, NAT, IGW, SGs) ---
teardown_network() {
  log "phase: network"
  # NAT
  if [ -n "${NAT_ID:-}" ] && aws ec2 describe-nat-gateways --nat-gateway-ids "$NAT_ID" --query "NatGateways[0].State" --output text 2>/dev/null | grep -qE "available|pending"; then
    run aws ec2 delete-nat-gateway --nat-gateway-id "$NAT_ID" >/dev/null
    if [ "$CONFIRM" = 1 ]; then
      log "  waiting for NAT delete…"
      aws ec2 wait nat-gateway-deleted --nat-gateway-ids "$NAT_ID" 2>&1 | head -1 || true
    fi
  fi
  # EIP
  [ -n "${EIP_ALLOC:-}" ] && run aws ec2 release-address --allocation-id "$EIP_ALLOC" 2>/dev/null || true

  # Route tables (delete non-main ones)
  for rt in "${PUB_RT:-}" "${PRIV_RT:-}"; do
    [ -z "$rt" ] && continue
    for assoc in $(aws ec2 describe-route-tables --route-table-ids "$rt" --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text 2>/dev/null); do
      run aws ec2 disassociate-route-table --association-id "$assoc"
    done
    run aws ec2 delete-route-table --route-table-id "$rt" 2>/dev/null || true
  done

  # IGW
  [ -n "${IGW_ID:-}" ] && [ -n "${VPC_ID:-}" ] && {
    run aws ec2 detach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" 2>/dev/null || true
    run aws ec2 delete-internet-gateway --internet-gateway-id "$IGW_ID" 2>/dev/null || true
  }

  # Subnets
  for s in "${PUB_SUBNET_0:-}" "${PUB_SUBNET_1:-}" "${PRIV_SUBNET_0:-}" "${PRIV_SUBNET_1:-}"; do
    [ -n "$s" ] && run aws ec2 delete-subnet --subnet-id "$s" 2>/dev/null || true
  done

  # SGs (must delete custom ones before VPC)
  for sg in "${ECS_SG:-}" "${RDS_SG:-}" "${ALB_SG:-}"; do
    [ -n "$sg" ] && run aws ec2 delete-security-group --group-id "$sg" 2>/dev/null || true
  done

  # VPC
  [ -n "${VPC_ID:-}" ] && run aws ec2 delete-vpc --vpc-id "$VPC_ID" 2>/dev/null || true
  log "  network teardown complete"
}

teardown_state() {
  log "phase: state"
  [ "$CONFIRM" = 1 ] && rm -f "$STATE" && log "  removed $STATE"
}

case "$PHASE" in
  services) teardown_services ;;
  alb) teardown_alb ;;
  rds) teardown_rds ;;
  s3) teardown_s3 ;;
  secrets) teardown_secrets ;;
  ecr) teardown_ecr ;;
  ecs|cluster) teardown_ecs_cluster ;;
  iam) teardown_iam ;;
  network) teardown_network ;;
  state) teardown_state ;;
  all|*)
    if [ "$CONFIRM" != 1 ]; then
      echo "=== DRY RUN — will not delete anything ==="
      echo "Run with --confirm to actually tear down."
      echo
    fi
    teardown_services
    teardown_alb
    teardown_rds
    teardown_s3
    teardown_secrets
    teardown_ecr
    teardown_ecs_cluster
    teardown_iam
    teardown_network
    teardown_state
    ;;
esac
log "teardown done"
