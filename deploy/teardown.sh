#!/usr/bin/env bash
# Remove everything deploy-api.sh created, so nothing keeps billing.
# Deletes in dependency order and tolerates already-absent resources.
source "$(dirname "$0")/config.sh"

say "deleting function URL + function ${FUNCTION_NAME}"
aws lambda delete-function-url-config --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}" 2>/dev/null || true
aws lambda delete-function --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}" 2>/dev/null || true

say "deleting ECR repo ${ECR_REPO} (including images)"
aws ecr delete-repository --repository-name "${ECR_REPO}" --force \
  --region "${AWS_REGION}" 2>/dev/null || true

say "deleting role ${ROLE_NAME}"
aws iam detach-role-policy --role-name "${ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
aws iam delete-role --role-name "${ROLE_NAME}" 2>/dev/null || true

say "done - remaining charges should be zero"
echo "  CloudWatch log group /aws/lambda/${FUNCTION_NAME} is kept (logs are tiny)."
echo "  Remove it with:"
echo "    aws logs delete-log-group --log-group-name /aws/lambda/${FUNCTION_NAME} --region ${AWS_REGION}"
