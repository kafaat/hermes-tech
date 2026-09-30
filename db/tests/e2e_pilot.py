#!/usr/bin/env python3
"""End-to-end run of the pilot path (P1) against a real Postgres and a running service.app over HTTP.

    DATABASE_URL=... python db/tests/e2e_pilot.py                 starts service.app itself on a free port
    DATABASE_URL=... HERMES_APP_URL=http://host:port HERMES_WEBHOOK_SECRETS=... python db/tests/e2e_pilot.py
                                                                  drives an already running service (staging)

The driver speaks only the outside interfaces: signed webhooks over HTTP, and the owner's decision in an
`authenticated` session carrying the owner's JWT claims (what the portal would send). It reads the database to
check each step. Seeding is idempotent and uses a fixed staging tenant; every run uses fresh message ids.
  1. a question with an approved answer  -> inquiry, reply proposal (pending), task waiting
  2. a complaint                          -> escalated to the owner (notify.owner sent), NO reply proposal
  3. redelivery of the same webhook       -> 200, no second event or task
  4. a bad signature                      -> 401, nothing stored
  5. the owner approves                   -> outbox consumes the approval once, the reply is sent, task succeeded
  6. the delivery receipt webhook         -> routed, processed, task succeeded
  7. audit: enqueue by the leased agent and the owner's decision, in the chain
"""
from __future__ import annotations
import hashlib, hmac, json, os, socket, subprocess, sys, time, urllib.error, urllib.request, uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
DB = os.environ["DATABASE_URL"]
CUSTOMER = "00000000-0000-0000-0000-0000000e2e01"
OWNER = "00000000-0000-0000-0000-0000000e2e0a"
PHONE_ID = "pn-e2e-staging"
HOURS = "نفتح يوميًا من ٩ صباحًا إلى ١١ مساءً"
failures = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def q(sql, args=(), role=None, claims=None):
    with psycopg.connect(DB, autocommit=False) as conn, conn.cursor() as cur:
        if claims:
            cur.execute("select set_config('request.jwt.claim.sub', %s, true), set_config('request.jwt.claims', %s, true)",
                        (claims["sub"], json.dumps(claims)))
        if role:
            cur.execute(f"set local role {role}")
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else cur.rowcount


def seed():
    q("insert into app.customers (id, public_ref, business_name, sector, city, currency_zone, status)"
      " values (%s, 'cust_e2e1', 'staging e2e', 'restaurant', 'city_1', 'zone_a', 'active') on conflict (id) do nothing", (CUSTOMER,))
    q("insert into app.customer_users (customer_id, auth_user_id, role) values (%s, %s, 'owner')"
      " on conflict (customer_id, auth_user_id) do nothing", (CUSTOMER, OWNER))
    q("insert into app.channel_accounts (customer_id, kind, external_id, status, verified_at)"
      " values (%s, 'whatsapp_cloud', %s, 'active', now()) on conflict (kind, external_id) do nothing", (CUSTOMER, PHONE_ID))
    if not q("select 1 from app.kb_facts where customer_id = %s and topic = 'hours' and approved_by_owner", (CUSTOMER,)):
        q("insert into app.kb_facts (customer_id, topic, fact, approved_by_owner) values (%s, 'hours', %s, true)",
          (CUSTOMER, HOURS), claims={"sub": OWNER, "aal": "aal1"})       # an approved fact is the owner's act (kb_facts_guard)


def message_payload(items):
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba-e2e", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": PHONE_ID}, **items}}]}]}


def post(url, secret, payload, signature=None):
    raw = json.dumps(payload, ensure_ascii=False).encode()
    sig = signature or "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url + "/webhook", data=raw, method="POST",
                                 headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def wait(what, fn, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(1)
    check(False, f"{what} (timed out after {timeout}s)")
    return None


def task_status(ext):
    r = q("select status::text, error_code from app.tasks where idempotency_key = %s", (f"wh:whatsapp_cloud:{ext}",))
    return r[0] if r else None


def start_app():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    secret = uuid.uuid4().hex
    env = {**os.environ, "HERMES_WEBHOOK_SECRETS": secret, "HERMES_GRAPH": "simulate", "PORT": str(port),
           "HERMES_APPROVAL_POLL_SECONDS": "1", "HERMES_VERIFY_TOKEN": "e2e"}
    proc = subprocess.Popen([sys.executable, "-m", "service.app"], cwd=ROOT, env=env)
    return f"http://127.0.0.1:{port}", secret, proc


def main():
    proc = None
    url = os.environ.get("HERMES_APP_URL")
    if url:
        secret = os.environ["HERMES_WEBHOOK_SECRETS"].split(",")[0].strip()
    else:
        url, secret, proc = start_app()
    secret = secret.encode()
    try:
        def healthy():
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=5) as r:
                    return r.status == 200
            except OSError:
                return False
        check(bool(wait("service healthy", healthy, 180)), f"service answers /healthz at {url}")
        seed()
        run = uuid.uuid4().hex[:10]
        q_ext, c_ext = f"wamid.E2E.{run}.q", f"wamid.E2E.{run}.c"
        batch = message_payload({"messages": [
            {"from": "967700000001", "id": q_ext, "type": "text", "text": {"body": "متى تفتحون اليوم؟"}},
            {"from": "967700000002", "id": c_ext, "type": "text", "text": {"body": "الفاتورة غلط ودفعت مرتين"}}]})

        check(post(url, secret, batch) == 200, "signed webhook accepted (200)")
        check(post(url, secret, batch) == 200, "redelivery accepted (200)")
        n = q("select count(*) from app.webhook_events where external_event_id in (%s, %s)", (q_ext, c_ext))[0][0]
        t = q("select count(*) from app.tasks where idempotency_key in (%s, %s)", (f"wh:whatsapp_cloud:{q_ext}", f"wh:whatsapp_cloud:{c_ext}"))[0][0]
        check(n == 2 and t == 2, f"redelivery stored nothing twice (events={n}, tasks={t})")
        routed = q("select count(*) from app.webhook_events where external_event_id in (%s, %s) and customer_id = %s", (q_ext, c_ext, CUSTOMER))[0][0]
        check(routed == 2, "events routed to the tenant by the addressed channel (webhook_route)")
        bad = message_payload({"messages": [{"from": "967700000003", "id": f"wamid.E2E.{run}.bad", "type": "text", "text": {"body": "x"}}]})
        check(post(url, secret, bad, signature="sha256=" + "0" * 64) == 401, "bad signature refused (401)")
        check(not q("select 1 from app.webhook_events where external_event_id = %s", (f"wamid.E2E.{run}.bad",)), "badly signed request stored nothing")

        ap = wait("reply proposal", lambda: q("select id, decision::text, payload from app.approvals where target_id = %s", (q_ext,)))
        if ap:
            ap_id, decision, payload = ap[0]
            check(decision == "pending" and payload["body"] == HOURS and payload["to"] == "967700000001",
                  "question -> reply proposal from the owner-approved fact, pending")
        check(bool(wait("complaint escalated", lambda: (task_status(c_ext) or ("",))[0] == "escalated")), "complaint -> task escalated")
        check(not q("select 1 from app.approvals where target_id = %s", (c_ext,)), "complaint -> no reply was proposed (C9.1)")
        check(bool(wait("owner notice sent", lambda: q("select 1 from app.outbox where topic = 'notify.owner' and target_id = %s"
                                                          " and dispatched_at is not null", (c_ext,)))), "complaint -> owner notified (notify.owner sent)")
        inq = q("select matched_category::text from app.inquiries where customer_id = %s and body in (%s, %s)",
                (CUSTOMER, "متى تفتحون اليوم؟", "الفاتورة غلط ودفعت مرتين"))
        check("pricing" in {r[0] for r in inq}, "inquiries recorded with the matched category")
        check(not q("select 1 from app.outbox where topic = 'reply.send' and target_id = %s", (q_ext,)), "nothing sent before the owner decides")

        if ap:
            q("update app.approvals set decision = 'approved', decided_by = %s where id = %s", (OWNER, ap_id),
              role="authenticated", claims={"sub": OWNER, "aal": "aal1"})
            sent = wait("reply sent", lambda: q("select id, provider_message_id, approval_id from app.outbox where topic = 'reply.send'"
                                                 " and target_id = %s and dispatched_at is not null", (q_ext,)))
            if sent:
                ob_id, wamid, used = sent[0]
                check(wamid.startswith("wamid.SIM.") and str(used) == str(ap_id), "owner approval -> reply sent once through the outbox")
                cons = q("select consumed_by_ref from app.approvals where id = %s", (ap_id,))[0][0]
                check(cons == f"outbox:{ob_id}", "approval consumed by exactly that outbox row")
                check(bool(wait("question task done", lambda: (task_status(q_ext) or ("",))[0] == "succeeded")), "question task succeeded")
                receipt = message_payload({"statuses": [{"id": wamid, "status": "delivered", "recipient_id": "967700000001",
                                                         "timestamp": str(int(time.time()))}]})
                check(post(url, secret, receipt) == 200, "delivery receipt accepted (200)")
                r_ext = f"status:{wamid}:delivered"
                check(bool(wait("receipt processed", lambda: q("select 1 from app.webhook_events where external_event_id = %s"
                                                                " and processed_at is not null", (r_ext,)))), "receipt routed and processed")
                check(bool(wait("receipt task", lambda: (task_status(r_ext) or ("",))[0] == "succeeded")), "receipt task succeeded")
                check(bool(q("select 1 from app.audit_log where action = 'outbox.enqueued' and target = %s and actor_type = 'agent'",
                             (f"outbox:{ob_id}",))), "enqueue audited as the leased agent")
                check(bool(q("select 1 from app.audit_log where action = 'approval.approved' and actor_id = %s", (OWNER,))),
                      "owner decision audited")
        unprocessed = q("select count(*) from app.webhook_events where external_event_id like %s and processed_at is null",
                        (f"%E2E.{run}%",))[0][0]
        check(unprocessed == 0, "every event of this run processed")
        check(not q("select 1 from app.audit_verify()"), "audit chain intact")
    finally:
        if proc:
            proc.terminate(); proc.wait(10)
    print(f"e2e: {'PASS' if not failures else 'FAIL'} ({len(failures)} failed)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
