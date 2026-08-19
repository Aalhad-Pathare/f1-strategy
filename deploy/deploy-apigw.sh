#!/usr/bin/env bash
# Expose the function through an API Gateway HTTP API.
#
# Why not a Lambda Function URL: this account blocks public function URLs. Every
# request returns 403 before the function is invoked, with a provably correct
# AuthType=NONE config and resource policy. Putting CloudFront in front with
# Origin Access Control did not help either - CloudFront's signed requests were
# rejected the same way, while a manually SigV4-signed request from an IAM user
# returned 200. The distinguishing factor is that the IAM user is authorised by an
# identity policy while CloudFront relies solely on the resource policy, which the
# account-level block appears to override.
#
# An HTTP API sidesteps function URLs entirely: it invokes the function through
# lambda:InvokeFunction with the apigateway service principal, which is the most
# standard integration path in AWS.
#
# Cost: 1M requests/month free for the first 12 months, then $1.00 per million.
# At portfolio traffic that is cents.
source "$(dirname "$0")/config.sh"

: "${API_NAME:=${APP_NAME}-http}"
ACCOUNT=$(account_id)
FN_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT}:function:${FUNCTION_NAME}"

# --- 1. the API ------------------------------------------------------------ #
API_ID=$(aws apigatewayv2 get-apis --region "${AWS_REGION}" \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text 2>/dev/null)

if [ "${API_ID}" = "None" ] || [ -z "${API_ID}" ]; then
  say "creating HTTP API ${API_NAME}"
  # AUTO_DEPLOY on the $default stage means no explicit deployment step, and
  # a payload format of 2.0 matches what Mangum already parses.
  API_ID=$(aws apigatewayv2 create-api --name "${API_NAME}" \
    --protocol-type HTTP --target "${FN_ARN}" \
    --region "${AWS_REGION}" --query ApiId --output text)
else
  say "HTTP API exists (${API_ID})"
fi

# --- 2. allow API Gateway to invoke ---------------------------------------- #
say "granting invoke permission"
aws lambda remove-permission --function-name "${FUNCTION_NAME}" \
  --statement-id AllowApiGatewayInvoke --region "${AWS_REGION}" 2>/dev/null || true
aws lambda add-permission --function-name "${FUNCTION_NAME}" \
  --statement-id AllowApiGatewayInvoke \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT}:${API_ID}/*/*" \
  --region "${AWS_REGION}" >/dev/null

ENDPOINT=$(aws apigatewayv2 get-api --api-id "${API_ID}" --region "${AWS_REGION}" \
  --query ApiEndpoint --output text)
say "deployed"
echo "  ${ENDPOINT}"
echo
echo "  smoke test:  curl -s ${ENDPOINT}/healthz"
