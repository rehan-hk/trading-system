# AWS Deployment Guide

## Prerequisites

- AWS CLI configured with appropriate credentials
- Docker installed and authenticated to ECR
- An ECR repository for the trading engine image

## One-time setup

### 1. Deploy infrastructure

```bash
aws cloudformation deploy \
  --template-file deploy/aws/cloudformation.yml \
  --stack-name trading-engine \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides AlertEmail=you@example.com
```

### 2. Store secrets in AWS Secrets Manager

```bash
aws secretsmanager create-secret --name trading-engine/api-key     --secret-string "YOUR_API_KEY"
aws secretsmanager create-secret --name trading-engine/api-secret  --secret-string "YOUR_API_SECRET"
aws secretsmanager create-secret --name trading-engine/kite-username --secret-string "YOUR_USERNAME"
aws secretsmanager create-secret --name trading-engine/kite-password --secret-string "YOUR_PASSWORD"
aws secretsmanager create-secret --name trading-engine/totp-secret  --secret-string "YOUR_TOTP_BASE32"
```

### 3. Create an ECR repository

```bash
aws ecr create-repository --repository-name trading-engine --region ap-south-1
```

## Deploying a new version

### 1. Build and push the Docker image

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-south-1
IMAGE_URI=${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/trading-engine:latest

aws ecr get-login-password --region ${REGION} | \
  docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

docker build -t trading-engine .
docker tag trading-engine:latest ${IMAGE_URI}
docker push ${IMAGE_URI}
```

### 2. Register the ECS task definition

```bash
# Substitute placeholders
sed "s/\${AWS_ACCOUNT_ID}/${ACCOUNT_ID}/g" deploy/aws/task-definition.json > /tmp/task-def.json

aws ecs register-task-definition --cli-input-json file:///tmp/task-def.json
```

### 3. Run a scheduled ECS task (one-shot, market hours)

```bash
CLUSTER=your-ecs-cluster
SUBNET=subnet-xxxxxxxx
SECURITY_GROUP=sg-xxxxxxxx

aws ecs run-task \
  --cluster ${CLUSTER} \
  --task-definition trading-engine \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNET}],securityGroups=[${SECURITY_GROUP}],assignPublicIp=ENABLED}"
```

## Health check

Once the task is running, check the health endpoint:

```bash
curl http://<task-public-ip>:8080/health
```

Expected response: `{"status":"ok"}`

## Monitoring

- Logs: CloudWatch Logs → `/ecs/trading-engine`
- Alerts: Subscribe to the `trading-alerts` SNS topic via the CloudFormation output
- DynamoDB: `trading-engine-state` and `trading-engine-trades` tables
- S3: `option-trading-data-<account-id>` bucket under `trade-data/` prefix
