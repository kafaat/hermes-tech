#!/usr/bin/env bash
# Railway service load-test (watch pattern ops/load/**, never restarts): k6 against the staging webhook, then the
# database checks. PROFILE=pilot|scale (docs/load_targets.md). Needs DATABASE_URL, HERMES_APP_URL, HERMES_WEBHOOK_SECRETS.
set -euo pipefail
cd "$(dirname "$0")/../.."
run="$(date -u +%Y%m%d%H%M%S)"
# Every step is bounded and says where it is: a run that hangs silently cannot be told from one still working
# (a deploy-while-load run on staging sat 20 minutes with no output, its results buffered in a live process).
echo "load: run $run, profile ${PROFILE:-pilot}, k6 starting"
rc=0
timeout --kill-after=30 "${K6_LIMIT_SECONDS:-1500}" k6 run --quiet -e PROFILE="${PROFILE:-pilot}" -e RUN_ID="$run" \
  -e WEBHOOK_URL="$HERMES_APP_URL/webhook" -e APP_SECRET="${HERMES_WEBHOOK_SECRETS%%,*}" -e SUMMARY=/tmp/k6.json \
  ops/load/k6_webhook.js || rc=$?
echo "load: k6 exited $rc (99 = thresholds crossed, judged below; 124/137 = over its time limit)"
[ -s /tmp/k6.json ] || { echo "load: FAIL no k6 summary"; exit 1; }
drain="${DRAIN_LIMIT_SECONDS:-600}"
PYTHONUNBUFFERED=1 timeout --kill-after=30 "$((drain + 120))" python3 ops/load/verify_load.py "$run" /tmp/k6.json "$drain"
