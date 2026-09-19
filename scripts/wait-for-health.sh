#!/usr/bin/env bash
# Bounded readiness gate for CI services. The caller owns any failure cleanup/logging.
set -euo pipefail

url=${1:?usage: wait-for-health.sh URL [attempts] [label]}
attempts=${2:-30}
label=${3:-service}

for _ in $(seq "$attempts"); do
  if curl -fsS "$url"; then
    exit 0
  fi
  sleep 1
done

printf '::error::%s did not become healthy within %s seconds\n' "$label" "$attempts" >&2
exit 1
