#!/usr/bin/env bash
# Put CloudFront in front of the Lambda Function URL using Origin Access Control.
#
# Why this rather than a public function URL: this account blocks public function
# URLs (every request returns 403 before the function is invoked, with a correct
# AuthType=NONE config and a correct resource policy). AWS has been defaulting new
# accounts to that. CloudFront + OAC is the recommended pattern anyway - the
# function URL moves to AWS_IAM auth and CloudFront signs each request with SigV4,
# so the function is never publicly reachable.
#
# It also pays for itself: CloudFront caches responses, so repeat traffic never
# reaches Lambda. The free tier (1TB out, 10M requests/month) is perpetual.
source "$(dirname "$0")/config.sh"

: "${OAC_NAME:=${APP_NAME}-oac}"
ACCOUNT=$(account_id)
FN_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT}:function:${FUNCTION_NAME}"

# --- 1. lock the function URL to IAM auth --------------------------------- #
say "setting function URL auth to AWS_IAM"
aws lambda update-function-url-config --function-name "${FUNCTION_NAME}" \
  --auth-type AWS_IAM --region "${AWS_REGION}" >/dev/null
aws lambda remove-permission --function-name "${FUNCTION_NAME}" \
  --statement-id FunctionURLAllowPublicAccess --region "${AWS_REGION}" 2>/dev/null || true

URL=$(aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" \
        --region "${AWS_REGION}" --query FunctionUrl --output text)
ORIGIN_HOST=$(echo "${URL}" | sed -E 's#^https://##; s#/$##')
say "origin ${ORIGIN_HOST}"

# --- 2. origin access control --------------------------------------------- #
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='${OAC_NAME}'].Id | [0]" \
  --output text 2>/dev/null)
if [ "${OAC_ID}" = "None" ] || [ -z "${OAC_ID}" ]; then
  say "creating origin access control"
  OAC_ID=$(aws cloudfront create-origin-access-control \
    --origin-access-control-config \
      "Name=${OAC_NAME},Description=OAC for ${FUNCTION_NAME},SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=lambda" \
    --query OriginAccessControl.Id --output text)
else
  say "origin access control exists (${OAC_ID})"
fi

# --- 3. distribution ------------------------------------------------------- #
DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='${APP_NAME}'].Id | [0]" --output text 2>/dev/null)

if [ "${DIST_ID}" = "None" ] || [ -z "${DIST_ID}" ]; then
  say "creating distribution"
  cat > /tmp/cf-dist.json <<JSON
{
  "CallerReference": "${APP_NAME}-$(date +%s)",
  "Comment": "${APP_NAME}",
  "Enabled": true,
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "lambda-origin",
      "DomainName": "${ORIGIN_HOST}",
      "OriginAccessControlId": "${OAC_ID}",
      "CustomOriginConfig": {
        "HTTPPort": 80,
        "HTTPSPort": 443,
        "OriginProtocolPolicy": "https-only",
        "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
        "OriginReadTimeout": 30,
        "OriginKeepaliveTimeout": 5
      }
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "lambda-origin",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {
      "Quantity": 7,
      "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
      "CachedMethods": {"Quantity": 2, "Items": ["GET","HEAD"]}
    },
    "Compress": true,
    "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
    "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac"
  },
  "PriceClass": "PriceClass_100"
}
JSON
  # CachePolicyId       = Managed-CachingDisabled. Correctness first; the API is
  #                       deterministic per (race, lap, driver) so caching can be
  #                       turned on later once the deployment is verified.
  # OriginRequestPolicy = Managed-AllViewerExceptHostHeader. Forwarding the
  #                       viewer's Host header would break SigV4 signing against
  #                       the Lambda URL origin, which is the classic OAC failure.
  # PriceClass_100      = North America + Europe edges only, the cheapest tier.
  DIST_ID=$(aws cloudfront create-distribution --distribution-config file:///tmp/cf-dist.json \
    --query Distribution.Id --output text)
else
  say "distribution exists (${DIST_ID})"
fi

DIST_ARN="arn:aws:cloudfront::${ACCOUNT}:distribution/${DIST_ID}"
DOMAIN=$(aws cloudfront get-distribution --id "${DIST_ID}" \
  --query Distribution.DomainName --output text)

# --- 4. let this distribution (and only it) invoke the function ------------ #
say "granting CloudFront invoke permission"
aws lambda remove-permission --function-name "${FUNCTION_NAME}" \
  --statement-id AllowCloudFrontInvoke --region "${AWS_REGION}" 2>/dev/null || true
aws lambda add-permission --function-name "${FUNCTION_NAME}" \
  --statement-id AllowCloudFrontInvoke \
  --action lambda:InvokeFunctionUrl \
  --principal cloudfront.amazonaws.com \
  --source-arn "${DIST_ARN}" \
  --function-url-auth-type AWS_IAM \
  --region "${AWS_REGION}" >/dev/null

say "distribution ${DIST_ID} deploying (usually 3-10 minutes)"
echo "  https://${DOMAIN}"
echo
echo "  watch status:  aws cloudfront get-distribution --id ${DIST_ID} --query Distribution.Status --output text"
echo "  smoke test:    curl -s https://${DOMAIN}/healthz"
