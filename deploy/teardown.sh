#!/usr/bin/env bash
# Remove everything the deploy scripts created, so nothing keeps billing.
# Deletes in dependency order and tolerates already-absent resources.
source "$(dirname "$0")/config.sh"

: "${API_NAME:=${APP_NAME}-http}"
: "${OAC_NAME:=${APP_NAME}-oac}"

say "deleting HTTP API ${API_NAME}"
API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}" \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text 2>/dev/null)
if [ -n "${API_ID}" ] && [ "${API_ID}" != "None" ]; then
  aws apigatewayv2 delete-api --api-id "${API_ID}" --region "${AWS_REGION}" || true
fi

# CloudFront cannot be deleted while enabled, and disabling takes a few minutes
# to propagate. Disable here and report; delete once it reports Deployed.
say "disabling CloudFront distribution (if any)"
DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='${APP_NAME}'].Id | [0]" --output text 2>/dev/null)
if [ -n "${DIST_ID}" ] && [ "${DIST_ID}" != "None" ]; then
  STATUS=$(aws cloudfront get-distribution --id "${DIST_ID}" --query Distribution.Status --output text)
  ENABLED=$(aws cloudfront get-distribution --id "${DIST_ID}" \
    --query Distribution.DistributionConfig.Enabled --output text)
  if [ "${ENABLED}" = "True" ]; then
    echo "  disable it first, then re-run this script once status is Deployed:"
    echo "    aws cloudfront get-distribution --id ${DIST_ID} --query Distribution.Status --output text"
  elif [ "${STATUS}" = "Deployed" ]; then
    ETAG=$(aws cloudfront get-distribution --id "${DIST_ID}" --query ETag --output text)
    aws cloudfront delete-distribution --id "${DIST_ID}" --if-match "${ETAG}" || true
    echo "  distribution deleted"
  else
    echo "  distribution ${DIST_ID} still ${STATUS}; re-run when Deployed"
  fi
fi

say "deleting function URL + function ${FUNCTION_NAME}"
aws lambda delete-function-url-config --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}" 2>/dev/null || true
aws lambda delete-function --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}" 2>/dev/null || true

say "deleting origin access control"
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='${OAC_NAME}'].Id | [0]" --output text 2>/dev/null)
if [ -n "${OAC_ID}" ] && [ "${OAC_ID}" != "None" ]; then
  ETAG=$(aws cloudfront get-origin-access-control --id "${OAC_ID}" --query ETag --output text 2>/dev/null)
  aws cloudfront delete-origin-access-control --id "${OAC_ID}" --if-match "${ETAG}" 2>/dev/null || true
fi

say "deleting ECR repo ${ECR_REPO} (including images)"
aws ecr delete-repository --repository-name "${ECR_REPO}" --force \
  --region "${AWS_REGION}" 2>/dev/null || true

say "deleting role ${ROLE_NAME}"
aws iam detach-role-policy --role-name "${ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
aws iam delete-role --role-name "${ROLE_NAME}" 2>/dev/null || true

say "done"
echo "  CloudWatch log group /aws/lambda/${FUNCTION_NAME} is kept (logs are tiny)."
echo "  Remove it with:"
echo "    aws logs delete-log-group --log-group-name /aws/lambda/${FUNCTION_NAME} --region ${AWS_REGION}"
