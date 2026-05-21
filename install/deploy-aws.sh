#!/usr/bin/env bash
# Idempotent AWS deployer for the reusable-agents framework + per-site apps.
#
# Mirrors install/deploy-azure.sh but targets:
#   ECR (image registry), ECS Fargate (compute), RDS PostgreSQL (db),
#   S3 (blob storage), Secrets Manager, ALB (ingress), Route 53 (dns).
#
# Phases (run individually or all):
#   provision  — VPC, subnets, IGW/NAT, SGs, ECS cluster, ALB, RDS, S3, ECR
#   secrets    — populate Secrets Manager from local recipes
#   images     — build + push framework api/ui images
#   services   — create/update ECS task defs + services for all apps
#   dns        — Route 53 records pointing custom domains at ALB
#   all        — provision → secrets → images → services → dns
#
# State persisted at ~/.aws-deploy/state.env (resource ids).
#
# Usage:
#   bash install/deploy-aws.sh provision
#   bash install/deploy-aws.sh images [tag]   # default: utc timestamp
#   bash install/deploy-aws.sh services
#   bash install/deploy-aws.sh all
#
# Prereqs:
#   aws cli v2 configured (us-east-1), docker, IAM user with AdministratorAccess
#   ~/.aws-rds-pw exists (mode 0600) containing RDS master password
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="${AWS_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
STATE_DIR="$HOME/.aws-deploy"
STATE="$STATE_DIR/state.env"
mkdir -p "$STATE_DIR/logs"
touch "$STATE"

# --- helpers ---
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
put_state() { grep -v "^$1=" "$STATE" > "$STATE.tmp" || true; echo "$1=$2" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
source_state() { [ -f "$STATE" ] && source "$STATE" || true; }
require() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }

source_state
put_state REGION "$REGION"
put_state ACCOUNT "$ACCOUNT"

# --- PHASE: provision ---
phase_provision() {
  require aws
  source_state
  log "phase: provision"

  # VPC (10.20.0.0/16)
  if [ -z "${VPC_ID:-}" ]; then
    VPC_ID=$(aws ec2 create-vpc --cidr-block 10.20.0.0/16 \
      --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=nsc-apps-vpc}]' \
      --query Vpc.VpcId --output text)
    aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames
    aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support
    put_state VPC_ID "$VPC_ID"
    log "  VPC_ID=$VPC_ID"
  fi
  source_state

  # IGW
  if [ -z "${IGW_ID:-}" ]; then
    IGW_ID=$(aws ec2 create-internet-gateway \
      --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=nsc-apps-igw}]' \
      --query InternetGateway.InternetGatewayId --output text)
    aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
    put_state IGW_ID "$IGW_ID"
  fi
  source_state

  # 2 public, 2 private subnets across 1a/1b
  if [ -z "${PUB_SUBNET_0:-}" ]; then
    for i in 0 1; do
      AZ="us-east-1$([ $i -eq 0 ] && echo a || echo b)"
      PUB=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "10.20.$((i*16+1)).0/24" \
        --availability-zone "$AZ" \
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=nsc-public-$AZ}]" \
        --query Subnet.SubnetId --output text)
      aws ec2 modify-subnet-attribute --subnet-id "$PUB" --map-public-ip-on-launch
      put_state "PUB_SUBNET_$i" "$PUB"
      PRIV=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "10.20.$((i*16+2)).0/24" \
        --availability-zone "$AZ" \
        --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=nsc-private-$AZ}]" \
        --query Subnet.SubnetId --output text)
      put_state "PRIV_SUBNET_$i" "$PRIV"
    done
  fi
  source_state

  # Public route table → IGW
  if [ -z "${PUB_RT:-}" ]; then
    PUB_RT=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
      --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=nsc-public-rt}]' \
      --query RouteTable.RouteTableId --output text)
    aws ec2 create-route --route-table-id "$PUB_RT" --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW_ID" >/dev/null
    aws ec2 associate-route-table --route-table-id "$PUB_RT" --subnet-id "$PUB_SUBNET_0" >/dev/null
    aws ec2 associate-route-table --route-table-id "$PUB_RT" --subnet-id "$PUB_SUBNET_1" >/dev/null
    put_state PUB_RT "$PUB_RT"
  fi

  # NAT gateway + private route
  if [ -z "${NAT_ID:-}" ]; then
    EIP_ALLOC=$(aws ec2 allocate-address --domain vpc \
      --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Name,Value=nsc-nat-eip}]' \
      --query AllocationId --output text)
    put_state EIP_ALLOC "$EIP_ALLOC"
    NAT_ID=$(aws ec2 create-nat-gateway --subnet-id "$PUB_SUBNET_0" --allocation-id "$EIP_ALLOC" \
      --tag-specifications 'ResourceType=natgateway,Tags=[{Key=Name,Value=nsc-nat}]' \
      --query NatGateway.NatGatewayId --output text)
    put_state NAT_ID "$NAT_ID"
    log "  waiting for NAT $NAT_ID..."
    aws ec2 wait nat-gateway-available --nat-gateway-ids "$NAT_ID"
    PRIV_RT=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
      --tag-specifications 'ResourceType=route-table,Tags=[{Key=Name,Value=nsc-private-rt}]' \
      --query RouteTable.RouteTableId --output text)
    aws ec2 create-route --route-table-id "$PRIV_RT" --destination-cidr-block 0.0.0.0/0 --nat-gateway-id "$NAT_ID" >/dev/null
    aws ec2 associate-route-table --route-table-id "$PRIV_RT" --subnet-id "$PRIV_SUBNET_0" >/dev/null
    aws ec2 associate-route-table --route-table-id "$PRIV_RT" --subnet-id "$PRIV_SUBNET_1" >/dev/null
    put_state PRIV_RT "$PRIV_RT"
  fi

  # Security groups (ALB ← internet 80/443; ECS ← ALB any; RDS ← ECS 5432)
  if [ -z "${ALB_SG:-}" ]; then
    ALB_SG=$(aws ec2 create-security-group --group-name nsc-alb-sg --description "ALB ingress" \
      --vpc-id "$VPC_ID" --tag-specifications 'ResourceType=security-group,Tags=[{Key=Name,Value=nsc-alb-sg}]' \
      --query GroupId --output text)
    aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" --protocol tcp --port 80  --cidr 0.0.0.0/0 >/dev/null
    aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null
    put_state ALB_SG "$ALB_SG"
  fi
  if [ -z "${ECS_SG:-}" ]; then
    ECS_SG=$(aws ec2 create-security-group --group-name nsc-ecs-sg --description "ECS tasks" \
      --vpc-id "$VPC_ID" --tag-specifications 'ResourceType=security-group,Tags=[{Key=Name,Value=nsc-ecs-sg}]' \
      --query GroupId --output text)
    aws ec2 authorize-security-group-ingress --group-id "$ECS_SG" --protocol tcp --port 1-65535 --source-group "$ALB_SG" >/dev/null
    put_state ECS_SG "$ECS_SG"
  fi
  if [ -z "${RDS_SG:-}" ]; then
    RDS_SG=$(aws ec2 create-security-group --group-name nsc-rds-sg --description "RDS" \
      --vpc-id "$VPC_ID" --tag-specifications 'ResourceType=security-group,Tags=[{Key=Name,Value=nsc-rds-sg}]' \
      --query GroupId --output text)
    aws ec2 authorize-security-group-ingress --group-id "$RDS_SG" --protocol tcp --port 5432 --source-group "$ECS_SG" >/dev/null
    MY_IP=$(curl -s https://checkip.amazonaws.com)
    aws ec2 authorize-security-group-ingress --group-id "$RDS_SG" --protocol tcp --port 5432 --cidr "${MY_IP}/32" >/dev/null
    put_state RDS_SG "$RDS_SG"
  fi

  # IAM service-linked role for ECS (idempotent — error if exists is fine)
  aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com >/dev/null 2>&1 || true

  # ECS cluster
  if ! aws ecs describe-clusters --clusters nsc-apps --query "clusters[0].clusterName" --output text 2>/dev/null | grep -q nsc-apps; then
    aws ecs create-cluster --cluster-name nsc-apps \
      --capacity-providers FARGATE FARGATE_SPOT \
      --default-capacity-provider-strategy capacityProvider=FARGATE,weight=1 >/dev/null
  fi
  put_state ECS_CLUSTER nsc-apps

  # Log group
  aws logs create-log-group --log-group-name /ecs/nsc-apps 2>/dev/null || true
  put_state LOG_GROUP /ecs/nsc-apps

  # IAM roles (task execution + task)
  for ROLE in ecsTaskExecutionRole:AmazonECSTaskExecutionRolePolicy:service-role nscAppsTaskRole:AmazonS3FullAccess:AWS_MANAGED; do
    NAME=$(echo $ROLE | cut -d: -f1)
    POL=$(echo $ROLE | cut -d: -f2)
    PFX=$(echo $ROLE | cut -d: -f3)
    if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
      aws iam create-role --role-name "$NAME" \
        --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
      if [ "$PFX" = service-role ]; then
        aws iam attach-role-policy --role-name "$NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/$POL"
      else
        aws iam attach-role-policy --role-name "$NAME" --policy-arn "arn:aws:iam::aws:policy/$POL"
      fi
      aws iam attach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/SecretsManagerReadWrite 2>/dev/null || true
    fi
  done
  put_state EXEC_ROLE  "arn:aws:iam::$ACCOUNT:role/ecsTaskExecutionRole"
  put_state TASK_ROLE  "arn:aws:iam::$ACCOUNT:role/nscAppsTaskRole"

  # ECR repos
  for r in agents-api agents-ui aisleprompt specpicks hearthnote nsc-website application-research; do
    aws ecr describe-repositories --repository-names "$r" >/dev/null 2>&1 \
      || aws ecr create-repository --repository-name "$r" --image-scanning-configuration scanOnPush=true >/dev/null
  done

  # S3 buckets (private, versioned, encrypted)
  for name in agents recipe-images; do
    b="nsc-${name}-${ACCOUNT}"
    aws s3api head-bucket --bucket "$b" 2>/dev/null || aws s3api create-bucket --bucket "$b" --region "$REGION"
    aws s3api put-bucket-versioning --bucket "$b" --versioning-configuration Status=Enabled
    aws s3api put-public-access-block --bucket "$b" \
      --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    aws s3api put-bucket-encryption --bucket "$b" \
      --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
    put_state "S3_${name//-/_}" "$b"
  done

  # ALB
  if [ -z "${ALB_ARN:-}" ]; then
    ALB_ARN=$(aws elbv2 create-load-balancer --name nsc-apps-alb \
      --subnets "$PUB_SUBNET_0" "$PUB_SUBNET_1" --security-groups "$ALB_SG" \
      --scheme internet-facing --type application --ip-address-type ipv4 \
      --query "LoadBalancers[0].LoadBalancerArn" --output text)
    put_state ALB_ARN "$ALB_ARN"
  fi
  ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
    --query "LoadBalancers[0].DNSName" --output text)
  put_state ALB_DNS "$ALB_DNS"

  # RDS (db.t4g.small, postgres 15, public for restore — flip to private after)
  if [ -z "${RDS_INSTANCE:-}" ]; then
    [ -f ~/.aws-rds-pw ] || { echo "missing ~/.aws-rds-pw"; exit 1; }
    PW=$(cat ~/.aws-rds-pw)
    # subnet group with public subnets for restore phase
    aws rds describe-db-subnet-groups --db-subnet-group-name nsc-db-subnets-public >/dev/null 2>&1 \
      || aws rds create-db-subnet-group --db-subnet-group-name nsc-db-subnets-public \
           --db-subnet-group-description "public subnets for restore" \
           --subnet-ids "$PUB_SUBNET_0" "$PUB_SUBNET_1" >/dev/null
    aws rds describe-db-instances --db-instance-identifier nsc-apps-db >/dev/null 2>&1 \
      || aws rds create-db-instance \
           --db-instance-identifier nsc-apps-db --db-instance-class db.t4g.small \
           --engine postgres --engine-version 15.7 \
           --allocated-storage 20 --storage-type gp3 \
           --master-username nscadmin --master-user-password "$PW" \
           --backup-retention-period 7 --db-subnet-group-name nsc-db-subnets-public \
           --vpc-security-group-ids "$RDS_SG" --publicly-accessible --no-multi-az \
           --storage-encrypted --auto-minor-version-upgrade >/dev/null
    put_state RDS_INSTANCE nsc-apps-db
    log "  RDS creating (~10 min)…"
    aws rds wait db-instance-available --db-instance-identifier nsc-apps-db
  fi
  RDS_ENDPOINT=$(aws rds describe-db-instances --db-instance-identifier nsc-apps-db \
    --query "DBInstances[0].Endpoint.Address" --output text)
  put_state RDS_ENDPOINT "$RDS_ENDPOINT"

  log "phase: provision complete"
}

# --- PHASE: secrets ---
phase_secrets() {
  source_state
  log "phase: secrets"
  for s in agents aisleprompt specpicks hearthnote nsc-website application-research; do
    aws secretsmanager describe-secret --secret-id "nsc/$s" >/dev/null 2>&1 \
      || aws secretsmanager create-secret --name "nsc/$s" \
           --secret-string '{"_placeholder":"populate via aws secretsmanager update-secret"}' >/dev/null
  done
  # RDS master
  if [ -f ~/.aws-rds-pw ]; then
    PW=$(cat ~/.aws-rds-pw)
    aws secretsmanager describe-secret --secret-id nsc/rds-master >/dev/null 2>&1 \
      || aws secretsmanager create-secret --name nsc/rds-master \
           --secret-string "{\"username\":\"nscadmin\",\"password\":\"$PW\"}" >/dev/null
  fi
  log "phase: secrets complete"
}

# --- PHASE: images (framework api + ui) ---
phase_images() {
  require docker
  source_state
  TAG="${1:-$(date -u +%Y%m%d-%H%M)}"
  REG="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
  log "phase: images (tag=$TAG)"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  for app in agents-api:framework/api/Dockerfile agents-ui:framework/ui/Dockerfile; do
    name=$(echo $app | cut -d: -f1)
    dockerfile=$(echo $app | cut -d: -f2)
    img="${REG}/${name}:${TAG}"
    log "  build $img"
    docker build -f "$REPO_ROOT/$dockerfile" -t "$img" "$REPO_ROOT" >/dev/null
    docker push "$img" >/dev/null
    docker tag "$img" "${REG}/${name}:latest"
    docker push "${REG}/${name}:latest" >/dev/null
  done
  put_state LAST_IMAGE_TAG "$TAG"
  log "phase: images complete"
}

# --- PHASE: restore (DB dumps + blob snapshot → S3+RDS via Fargate task) ---
# Runs a one-shot Fargate task that pulls dumps from S3 and pg_restores
# into RDS. Requires AWS account compute be unblocked (new accounts may
# need verification before EC2/Fargate work). Idempotent — re-running
# replays --clean --if-exists pg_restore which is safe.
phase_restore() {
  source_state
  log "phase: restore"
  REG="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
  : "${RDS_ENDPOINT:?run provision first}" "${ECS_CLUSTER:?}" "${PRIV_SUBNET_0:?}" "${ECS_SG:?}"
  PW=$(cat ~/.aws-rds-pw)
  BUCKET="nsc-agents-${ACCOUNT}"

  # Stage dumps to S3 if backup dir is present locally
  BACKUP="${BACKUP_DIR:-$(ls -td /home/voidsstr/azure-backup-* 2>/dev/null | head -1)}"
  if [ -n "$BACKUP" ] && [ -d "$BACKUP/db" ]; then
    log "  uploading dumps from $BACKUP/db/ to s3://$BUCKET/_restore/db/"
    aws s3 sync "$BACKUP/db/" "s3://$BUCKET/_restore/db/" --no-progress
  fi

  # Cache postgres:15 in ECR (Fargate pulls from ECR faster + works in private subnet)
  if ! aws ecr describe-images --repository-name postgres --image-ids imageTag=15 >/dev/null 2>&1; then
    aws ecr describe-repositories --repository-names postgres >/dev/null 2>&1 \
      || aws ecr create-repository --repository-name postgres >/dev/null
    docker pull postgres:15
    docker tag postgres:15 "$REG/postgres:15"
    aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REG"
    docker push "$REG/postgres:15"
  fi

  # Build the restore script (encoded as base64 env var on the task)
  cat > /tmp/_restore-cmd.sh <<INNER
#!/bin/bash
set -e
apt-get update -qq >/dev/null && apt-get install -y --no-install-recommends awscli >/dev/null
mkdir /tmp/db && cd /tmp/db
for db in aisleprompt specpicks affiliateflow dealradar hearthnote; do
  echo "[\$db] downloading"
  aws s3 cp "s3://${BUCKET}/_restore/db/\${db}.pgdump" ./
  echo "[\$db] create database"
  PGPASSWORD='${PW}' psql -h ${RDS_ENDPOINT} -U nscadmin -d postgres -c "CREATE DATABASE \"\$db\";" 2>&1 | tail -1
  echo "[\$db] pg_restore"
  PGPASSWORD='${PW}' pg_restore -h ${RDS_ENDPOINT} -U nscadmin -d "\$db" \\
    --no-owner --no-privileges --clean --if-exists --jobs=4 ./\${db}.pgdump
done
echo "ALL_DONE"
INNER
  B64=$(base64 -w0 /tmp/_restore-cmd.sh)

  # Register task def
  cat > /tmp/_td-restore.json <<TD
{
  "family": "nsc-db-restore",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512", "memory": "1024",
  "executionRoleArn": "$EXEC_ROLE",
  "taskRoleArn": "$TASK_ROLE",
  "containerDefinitions": [{
    "name": "restore",
    "image": "$REG/postgres:15",
    "essential": true,
    "command": ["bash","-c","echo \$SCRIPT_B64 | base64 -d > /tmp/r.sh && bash /tmp/r.sh"],
    "environment": [{"name": "SCRIPT_B64", "value": "$B64"}],
    "logConfiguration": {"logDriver": "awslogs",
      "options": {"awslogs-group": "$LOG_GROUP", "awslogs-region": "$REGION", "awslogs-stream-prefix": "restore"}}
  }]
}
TD
  TD_ARN=$(aws ecs register-task-definition --cli-input-json file:///tmp/_td-restore.json \
    --query "taskDefinition.taskDefinitionArn" --output text)
  log "  registered task def: $TD_ARN"

  TASK_ARN=$(aws ecs run-task --cluster "$ECS_CLUSTER" --task-definition "$TD_ARN" \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$PRIV_SUBNET_0],securityGroups=[$ECS_SG],assignPublicIp=DISABLED}" \
    --query "tasks[0].taskArn" --output text)
  [ -z "$TASK_ARN" ] || [ "$TASK_ARN" = None ] && { echo "  failed to start task — likely BlockedException on new account"; exit 1; }
  log "  task started: $TASK_ARN — waiting (max 10 min)…"
  aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN"

  # Inspect exit code
  EXIT=$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN" \
    --query "tasks[0].containers[0].exitCode" --output text)
  log "  task exit: $EXIT"
  [ "$EXIT" = "0" ] || { echo "  restore failed — check logs at /ecs/nsc-apps"; exit 1; }
  log "phase: restore complete"
}

# --- PHASE: services (ECS task defs + services per app) ---
# This is the heaviest phase — emits a task-def JSON per app from a
# template, then create-or-update the service.
phase_services() {
  source_state
  log "phase: services"
  REG="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

  # Apps with their listener ports + target group rules
  # Format: appname:container_port:host_pattern
  APPS=(
    "agents-api:8090:api.agents.nsc.tools"
    "agents-ui:80:agents.nsc.tools"
    "aisleprompt:3000:aisleprompt.com"
    "specpicks:3000:specpicks.com"
    "hearthnote:3000:hearthnote.com"
    "nsc-website:80:northernsoftwareconsulting.com"
  )

  for spec in "${APPS[@]}"; do
    IFS=':' read -r app port host <<< "$spec"
    log "  service $app (port $port, host $host)"

    # Target group
    tg_arn=$(aws elbv2 describe-target-groups --names "tg-$app" --query "TargetGroups[0].TargetGroupArn" --output text 2>/dev/null || true)
    if [ -z "$tg_arn" ] || [ "$tg_arn" = None ]; then
      tg_arn=$(aws elbv2 create-target-group --name "tg-$app" \
        --protocol HTTP --port "$port" --vpc-id "$VPC_ID" \
        --target-type ip --health-check-path / \
        --query "TargetGroups[0].TargetGroupArn" --output text)
    fi

    # Task def
    SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "nsc/${app%-api}" --query ARN --output text 2>/dev/null || echo "")
    cat > /tmp/taskdef-$app.json <<TASKDEF
{
  "family": "$app",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512", "memory": "1024",
  "executionRoleArn": "$EXEC_ROLE",
  "taskRoleArn": "$TASK_ROLE",
  "containerDefinitions": [{
    "name": "$app",
    "image": "${REG}/${app}:latest",
    "essential": true,
    "portMappings": [{"containerPort": $port, "protocol": "tcp"}],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "$LOG_GROUP",
        "awslogs-region": "$REGION",
        "awslogs-stream-prefix": "$app"
      }
    }
  }]
}
TASKDEF
    TD_ARN=$(aws ecs register-task-definition --cli-input-json file:///tmp/taskdef-$app.json \
      --query "taskDefinition.taskDefinitionArn" --output text)

    # Service (create or update)
    if aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$app" --query "services[0].status" --output text 2>/dev/null | grep -q ACTIVE; then
      aws ecs update-service --cluster "$ECS_CLUSTER" --service "$app" --task-definition "$TD_ARN" --force-new-deployment >/dev/null
    else
      aws ecs create-service --cluster "$ECS_CLUSTER" --service-name "$app" \
        --task-definition "$TD_ARN" --desired-count 1 --launch-type FARGATE \
        --network-configuration "awsvpcConfiguration={subnets=[$PRIV_SUBNET_0,$PRIV_SUBNET_1],securityGroups=[$ECS_SG],assignPublicIp=DISABLED}" \
        --load-balancers "targetGroupArn=$tg_arn,containerName=$app,containerPort=$port" >/dev/null
    fi
  done
  log "phase: services complete"
}

# --- PHASE: dns (Route 53) ---
phase_dns() {
  source_state
  log "phase: dns (manual step — add records pointing to $ALB_DNS)"
  echo "  CNAME records to create in your DNS provider:"
  for h in aisleprompt.com specpicks.com hearthnote.com northernsoftwareconsulting.com agents.nsc.tools api.agents.nsc.tools; do
    echo "    $h → $ALB_DNS"
  done
}

CMD="${1:-all}"
case "$CMD" in
  provision) phase_provision ;;
  secrets) phase_secrets ;;
  images) shift; phase_images "$@" ;;
  restore) phase_restore ;;
  services) phase_services ;;
  dns) phase_dns ;;
  all) phase_provision; phase_secrets; phase_images; phase_restore; phase_services; phase_dns ;;
  *) echo "usage: $0 {provision|secrets|images|restore|services|dns|all}"; exit 1 ;;
esac
