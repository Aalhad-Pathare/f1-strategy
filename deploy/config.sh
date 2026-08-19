# Shared deployment settings. Override any of these in your shell before running.
set -euo pipefail

: "${AWS_REGION:=$(aws configure get region 2>/dev/null || echo us-east-1)}"
: "${APP_NAME:=f1-strategy}"
: "${ECR_REPO:=${APP_NAME}-api}"
: "${FUNCTION_NAME:=${APP_NAME}-api}"
: "${ROLE_NAME:=${APP_NAME}-lambda-role}"

# 1536MB is chosen for CPU rather than memory: Lambda scales vCPU with memory,
# and importing pandas dominates cold start. Warm requests use ~200MB.
: "${MEMORY_MB:=1536}"
: "${TIMEOUT_S:=30}"

export AWS_REGION APP_NAME ECR_REPO FUNCTION_NAME ROLE_NAME MEMORY_MB TIMEOUT_S

account_id() { aws sts get-caller-identity --query Account --output text; }
ecr_uri() { echo "$(account_id).dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"; }
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
