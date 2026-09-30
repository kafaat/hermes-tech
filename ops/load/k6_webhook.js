// k6 load test for webhook ingestion (the burstiest path). NOT executed in the build environment.
// Run against staging only, before the level-2 scale gate:  k6 run ops/load/k6_webhook.js
// Targets: p95 < 300 ms and < 0.1% errors; duplicates must be absorbed (count rows in app.webhook_events afterwards).
import http from 'k6/http';
import { check } from 'k6';
import crypto from 'k6/crypto';

// PROFILE=pilot (before the pilot) or PROFILE=scale (before level 2); see docs/load_targets.md
const PROFILES = {
  pilot: { rate: 2, duration: '5m', preAllocatedVUs: 10 },
  scale: { rate: 20, duration: '10m', preAllocatedVUs: 40 },
};
const P = PROFILES[__ENV.PROFILE || 'pilot'];
export const options = {
  scenarios: { steady: { executor: 'constant-arrival-rate', rate: P.rate, timeUnit: '1s', duration: P.duration, preAllocatedVUs: P.preAllocatedVUs } },
  thresholds: { http_req_duration: ['p(95)<300'], http_req_failed: ['rate<0.001'] },
};

const URL = __ENV.WEBHOOK_URL;          // staging endpoint
const SECRET = __ENV.APP_SECRET;        // staging app secret, never production

export default function () {
  const id = `wamid.LOAD${__VU}_${__ITER % 50}`; // ~2% duplicate deliveries on purpose
  const body = JSON.stringify({ entry: [{ changes: [{ value: { messages: [{ id, text: { body: 'متى تفتحون؟' } }] } }] }] });
  const sig = 'sha256=' + crypto.hmac('sha256', SECRET, body, 'hex');
  const res = http.post(URL, body, { headers: { 'Content-Type': 'application/json', 'X-Hub-Signature-256': sig } });
  check(res, { 'accepted': (r) => r.status === 200 });
}
