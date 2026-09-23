#!/usr/bin/env bash
# Snapshot the live document surface, then publish the Worker that falls back to it.
#
# The two halves are one command on purpose. `wrangler deploy` alone would publish a Worker
# whose stored copies are whatever was last on this disk — which, for a surface that carries
# the version and the configured limits, means a fallback that confidently serves the
# previous release's numbers during the next outage.
set -euo pipefail

BASE="${1:-https://technocore.chat}"
cd "$(dirname "$0")"

echo "==> snapshotting $BASE"
python3 snapshot.py --base "$BASE"

echo
echo "==> deploying"
npx wrangler deploy

echo
echo "Verify the fallback is reachable but NOT preferred:"
echo "  curl -sI $BASE/skill.md | grep -i x-origin-fallback   # expect no header while the origin is up"
