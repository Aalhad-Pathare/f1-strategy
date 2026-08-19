#!/usr/bin/env bash
# Check everything deploy-api.sh needs before it spends money or time.
source "$(dirname "$0")/config.sh"

fail=0
check() { printf '  %-34s ' "$1"; }
ok()   { printf '\033[32mOK\033[0m  %s\n' "${1:-}"; }
bad()  { printf '\033[31mFAIL\033[0m  %s\n' "$1"; fail=1; }

say "preflight"

check "aws cli"
if command -v aws >/dev/null; then ok "$(aws --version 2>&1 | cut -d' ' -f1)"; else bad "not installed"; fi

check "credentials"
if IDENT=$(aws sts get-caller-identity --output text --query 'Arn' 2>&1); then
  ok "${IDENT}"
else
  bad "not configured - run: aws configure"
fi

check "region"
if [ -n "${AWS_REGION}" ]; then ok "${AWS_REGION}"; else bad "unset"; fi

check "docker daemon"
if docker info >/dev/null 2>&1; then ok "$(docker --version | cut -d, -f1)"; else
  bad "unreachable - enable WSL integration in Docker Desktop settings"
fi

check "Dockerfile.api"
[ -f Dockerfile.api ] && ok || bad "missing"

check "race data"
n=$(ls -1 data/*.parquet 2>/dev/null | wc -l)
[ "${n}" -gt 0 ] && ok "${n} races" || bad "no parquet in data/"

check "schedule cache"
m=$(ls -1 data/schedules/*.json 2>/dev/null | wc -l)
[ "${m}" -gt 0 ] && ok "${m} seasons" || bad "run: python -c 'import schedule; [schedule.season(y, True) for y in schedule.available_years()]'"

echo
if [ "${fail}" -eq 0 ]; then
  say "ready - run deploy/deploy-api.sh"
else
  say "not ready - fix the items above"
  exit 1
fi
