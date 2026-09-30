// k6 load test for webhook ingestion and the inbound path behind it (docs/load_targets.md).
// Staging only:  ops/load/run.sh   (k6, then verify_load.py reads the database for what k6 cannot see)
// Payload: the WhatsApp Cloud format webhook.events_from() routes (object + metadata.phone_number_id), addressed
// to the staging test channel, so every event is routed, gets a task and runs through the worker. The text has no
// owner-approved answer: each task ends escalated with a (simulated) owner notice, and nothing stays pending.
// Targets: p95 < 300 ms and < 0.1% errors at the edge; duplicates absorbed; the queue drains (verify_load.py).
import http from 'k6/http';
import { check } from 'k6';
import crypto from 'k6/crypto';
import exec from 'k6/execution';

const PROFILES = {
  pilot: { rate: 2, duration: '5m', preAllocatedVUs: 10 },
  scale: { rate: 20, duration: '10m', preAllocatedVUs: 40 },
};
const P = PROFILES[__ENV.PROFILE || 'pilot'];
export const options = {
  scenarios: { steady: { executor: 'constant-arrival-rate', rate: P.rate, timeUnit: '1s',
                         duration: __ENV.DURATION || P.duration, preAllocatedVUs: P.preAllocatedVUs } },
  thresholds: { http_req_duration: ['p(95)<300'], http_req_failed: ['rate<0.001'] },
};

const URL = __ENV.WEBHOOK_URL;                 // staging endpoint (.../webhook)
const SECRET = __ENV.APP_SECRET;               // staging secret, never production
const PHONE = __ENV.PHONE_NUMBER_ID || 'pn-e2e-staging';
const RUN = __ENV.RUN_ID;                      // separates this run's events from every other run

export default function () {
  // one request in 50 repeats the previous id (a redelivery the unique key must absorb); the counter is global to
  // the run, so the share holds for any duration and number of VUs
  const i = exec.scenario.iterationInTest;
  const id = `wamid.LOAD.${RUN}.${i % 50 === 49 ? i - 1 : i}`;
  const body = JSON.stringify({ object: 'whatsapp_business_account', entry: [{ id: 'waba-load', changes: [{ field: 'messages',
    value: { messaging_product: 'whatsapp', metadata: { phone_number_id: PHONE },
             messages: [{ from: '967700000099', id, type: 'text', text: { body: 'مرحبا' } }] } }] }] });
  const sig = 'sha256=' + crypto.hmac('sha256', SECRET, body, 'hex');
  const res = http.post(URL, body, { headers: { 'Content-Type': 'application/json', 'X-Hub-Signature-256': sig } });
  check(res, { 'accepted': (r) => r.status === 200 });
}

export function handleSummary(data) {
  return { stdout: '', [__ENV.SUMMARY || 'k6-summary.json']: JSON.stringify(data) };
}
