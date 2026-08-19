#!/usr/bin/env bash
# Deploy the read-path API as a container image on Lambda behind a Function URL.
#
# Idempotent: safe to re-run to ship a new image. Every step checks for an
# existing resource rather than assuming a clean account.
#
# Cost: Lambda's always-free tier covers 1M requests and 400,000 GB-seconds per
# month, so at portfolio traffic this runs at $0. ECR storage is the only real
# charge - roughly $0.05/month for a 500MB image once the 12-month 500MB
# allowance lapses. A lifecycle policy keeps only the last 3 images.
source "$(dirname "$0")/config.sh"

ACCOUNT=$(account_id)
URI=$(ecr_uri)
say "account ${ACCOUNT}, region ${AWS_REGION}"

# --- 1. ECR repository ----------------------------------------------------- #
if aws ecr describe-repositories --repository-names "${ECR_REPO}" \
     --region "${AWS_REGION}" >/dev/null 2>&1; then
  say "ECR repo ${ECR_REPO} exists"
else
  say "creating ECR repo ${ECR_REPO}"
  aws ecr create-repository --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true >/dev/null
  # Bound storage growth: each push orphans the previous 'latest', so expiring
  # untagged images is what actually reclaims space. An imageCountMoreThanN rule
  # would be the obvious choice but ECR rejects it here ("matched 0 out of 4"),
  # including in its tagPatternList form - so use the form the API accepts.
  #
  # Non-fatal: this is a ~$0.05/month optimisation and must never block a deploy.
  aws ecr put-lifecycle-policy --repository-name "${ECR_REPO}" \
    --region "${AWS_REGION}" --lifecycle-policy-text \
    '{"rules":[{"rulePriority":1,"description":"expire untagged after 14 days","selection":{"tagStatus":"untagged","countType":"sinceImagePushed","countUnit":"days","countNumber":14},"action":{"type":"expire"}}]}' >/dev/null \
    || say "lifecycle policy not applied (non-fatal)"
fi

# --- 2. build + push ------------------------------------------------------- #
say "logging docker into ECR"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin \
      "${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"

TAG=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)
say "building image (linux/amd64) tag ${TAG}"
# Platform is pinned: Lambda rejects an image whose architecture does not match
# the function's, and an accidental arm64 build fails in a confusing way.
#
# Attestations must be off. Buildx (the default builder since Docker 23) attaches
# provenance and SBOM attestations, which turn the push into a manifest list.
# Lambda rejects that with "image manifest, config or layer media type ... is not
# supported" - an error that points at the image rather than at the build flags.
docker build --platform linux/amd64 --provenance=false --sbom=false \
  -f Dockerfile.api -t "${ECR_REPO}:${TAG}" .
docker tag "${ECR_REPO}:${TAG}" "${URI}:${TAG}"
docker tag "${ECR_REPO}:${TAG}" "${URI}:latest"

say "pushing"
docker push "${URI}:${TAG}"
docker push "${URI}:latest"

# --- 3. execution role ----------------------------------------------------- #
if aws iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
  say "role ${ROLE_NAME} exists"
else
  say "creating role ${ROLE_NAME}"
  aws iam create-role --role-name "${ROLE_NAME}" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  # CloudWatch Logs only: the read path touches no other AWS service.
  aws iam attach-role-policy --role-name "${ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  say "waiting for IAM role propagation"
  sleep 12
fi
ROLE_ARN=$(aws iam get-role --role-name "${ROLE_NAME}" --query Role.Arn --output text)

# --- 4. function ----------------------------------------------------------- #
if aws lambda get-function --function-name "${FUNCTION_NAME}" \
     --region "${AWS_REGION}" >/dev/null 2>&1; then
  say "updating function code"
  aws lambda update-function-code --function-name "${FUNCTION_NAME}" \
    --image-uri "${URI}:${TAG}" --region "${AWS_REGION}" >/dev/null
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}" \
    --region "${AWS_REGION}"
  aws lambda update-function-configuration --function-name "${FUNCTION_NAME}" \
    --memory-size "${MEMORY_MB}" --timeout "${TIMEOUT_S}" \
    --environment "Variables={F1_INGEST=off}" --region "${AWS_REGION}" >/dev/null
else
  say "creating function ${FUNCTION_NAME}"
  aws lambda create-function --function-name "${FUNCTION_NAME}" \
    --package-type Image --code "ImageUri=${URI}:${TAG}" \
    --role "${ROLE_ARN}" --memory-size "${MEMORY_MB}" --timeout "${TIMEOUT_S}" \
    --environment "Variables={F1_INGEST=off}" \
    --architectures x86_64 --region "${AWS_REGION}" >/dev/null
fi
aws lambda wait function-updated --function-name "${FUNCTION_NAME}" \
  --region "${AWS_REGION}"

# --- 5. public URL --------------------------------------------------------- #
if aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" \
     --region "${AWS_REGION}" >/dev/null 2>&1; then
  say "function URL exists"
else
  say "creating public function URL"
  # AuthType NONE makes this publicly reachable, which is the point of a
  # portfolio deployment. The function is read-only and holds no secrets.
  aws lambda create-function-url-config --function-name "${FUNCTION_NAME}" \
    --auth-type NONE --region "${AWS_REGION}" >/dev/null
  aws lambda add-permission --function-name "${FUNCTION_NAME}" \
    --statement-id public-function-url --action lambda:InvokeFunctionUrl \
    --principal '*' --function-url-auth-type NONE --region "${AWS_REGION}" >/dev/null
fi

URL=$(aws lambda get-function-url-config --function-name "${FUNCTION_NAME}" \
        --region "${AWS_REGION}" --query FunctionUrl --output text)
say "deployed"
echo "  ${URL}"
echo
echo "  smoke test:  curl -s ${URL}healthz"
