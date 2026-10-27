#!/usr/bin/env bash
# Railway service load-test (watch pattern ops/load/**, never restarts; runs only when armed, see below): k6 against the staging webhook, then the
# database checks. PROFILE=pilot|scale (docs/load_targets.md). Needs DATABASE_URL, HERMES_APP_URL, HERMES_WEBHOOK_SECRETS.
set -euo pipefail
cd "$(dirname "$0")/../.."
# Armed runs only (2026-10-27): a "redeploy all" on 12 October started this load against an app that was still coming
# up, alongside the db-migrate e2e, and both failed. A run happens only with HERMES_LOAD_RUN=1 on this service; unset
# it after the run. Unarmed, the service says so and exits 0.
if [ "${HERMES_LOAD_RUN:-}" != 1 ]; then
  echo "load: not armed (set HERMES_LOAD_RUN=1 on the load-test service for one run, then unset it); nothing sent"
  exit 0
fi
# and never against an app that is not up: three healthy answers in a row, within 5 minutes
PYTHONUNBUFFERED=1 python3 - "$HERMES_APP_URL" <<'PY' || { echo "load: FAIL the app did not answer /healthz three times in a row"; exit 1; }
import sys, time, urllib.request
url, ok, end = sys.argv[1].rstrip("/") + "/healthz", 0, time.time() + 300
while time.time() < end and ok < 3:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            ok = ok + 1 if r.status == 200 else 0
    except OSError:
        ok = 0
    time.sleep(2)
print(f"load: app healthy ({ok} answers in a row)")
sys.exit(0 if ok >= 3 else 1)
PY
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
