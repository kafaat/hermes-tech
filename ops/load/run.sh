#!/usr/bin/env bash
# Railway service load-test (watch pattern ops/load/**, never restarts): k6 against the staging webhook, then the
# database checks. PROFILE=pilot|scale (docs/load_targets.md). Needs DATABASE_URL, HERMES_APP_URL, HERMES_WEBHOOK_SECRETS.
set -euo pipefail
cd "$(dirname "$0")/../.."
run="$(date -u +%Y%m%d%H%M%S)"
k6 run --quiet -e PROFILE="${PROFILE:-pilot}" -e RUN_ID="$run" -e WEBHOOK_URL="$HERMES_APP_URL/webhook" \
  -e APP_SECRET="${HERMES_WEBHOOK_SECRETS%%,*}" -e SUMMARY=/tmp/k6.json ops/load/k6_webhook.js || echo "k6 thresholds crossed (judged below)"
python3 ops/load/verify_load.py "$run" /tmp/k6.json "${DRAIN_LIMIT_SECONDS:-600}"
