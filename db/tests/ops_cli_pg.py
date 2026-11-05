#!/usr/bin/env python3
"""The hermes command line end to end on a real Postgres (spec 28.35): cli/hermes.py as a process -> HTTP ->
service/ops_api.py -> service/ops_store.py in the operator's own `authenticated` session -> the policies.

    DATABASE_URL=... python db/tests/ops_cli_pg.py        (after the migrations: db/tests/run_isolation.sh)

One pilot business from nothing to an active subscription, with the operator's commands only:
  customers add -> owners link -> channels add + verify -> facts add -> competitors add (the third refused)
  -> billing subscribe / invoice / payment / match / activate (activation refused before a matched payment)
  -> freeze (no active channel: nothing new is routed) -> unfreeze (exactly the channels the freeze stopped)
and around it what must be refused: no reason outside an incident, the wrong role, no second factor, not an
operator, an incident closed without cause and prevention. Every read command runs once (its SQL is checked here,
nowhere else), every write leaves its audit row with the operator and the reason, and the chain still verifies.
"""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from service.auth import issue_staging_token  # noqa: E402
from service.ops_api import OpsApi  # noqa: E402
from service.ops_store import OpsStore  # noqa: E402
from service.pg import Database  # noqa: E402
from service.model import OpenAIModel, Triage  # noqa: E402
from service.worker import Task  # noqa: E402

DB = os.environ["DATABASE_URL"]
SECRET = uuid.uuid4().hex * 2
FOUNDER, OPS, FINANCE, STRANGER, OWNER = (str(uuid.uuid4()) for _ in range(5))
TMP = Path(tempfile.mkdtemp())
failures = []


def check(ok, what):
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def q(sql, args=()):
    with psycopg.connect(DB, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else cur.rowcount


def serve():
    api = OpsApi(OpsStore(Database(DB, "authenticated"), "https://hermes.example"), SECRET)

    class H(BaseHTTPRequestHandler):                       # the /ops/api/ branch of service.app's handler
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            status, headers, data = api.handle("POST", self.path, dict(self.headers.items()), self.rfile.read(n))
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def session(who, url, aal="aal2"):
    path = TMP / f"{who}-{aal}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url, "access_token": issue_staging_token(who, SECRET, ttl_seconds=900, aal=aal)}, f)
    return path


def hermes(sess, *argv):
    """(exit code, parsed JSON answer or None, stderr): the command line as the operator runs it."""
    p = subprocess.run([sys.executable, "-m", "cli.hermes", "--json", *argv], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "HERMES_SESSION_FILE": str(sess)}, timeout=60)
    try:
        doc = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        doc = None
    return p.returncode, doc, p.stderr


def audit(action, target=None):
    return q("select actor_type::text, actor_id, details from app.audit_log where action = %s"
             " and (%s::text is null or target = %s) order by id desc limit 1", (action, target, target))


def model_usage_journey(cid, ref, founder, ops, fin):
    """P29: REAL PostgreSQL accounting/RLS/transactions and real CLI->HTTP->store.
    Only the provider transport is synthetic; loss of the second DB transaction is an explicit fault injection.
    No paid model or external provider endpoint is called. Fixture leases do not replace app.bind_task checks.
    """
    db = Database(DB, "hermes_worker")

    def task():
        tid, tok = str(uuid.uuid4()), str(uuid.uuid4())
        q("insert into app.tasks(id,public_ref,customer_id,agent_id,kind,idempotency_key,status)"
          " values (%s,%s,%s,'agent_triage','usage-fixture',%s,'running')", (tid, "t_" + uuid.uuid4().hex, cid, "usage:" + tid))
        fencing = q("insert into app.task_leases(task_id,customer_id,agent_id,token,worker,fencing,lease_until)"
                    " values (%s,%s,'agent_triage',%s,'usage-fixture',nextval('app.fencing_seq'),clock_timestamp()+interval '1 hour')"
                    " returning fencing", (tid, cid, tok))[0][0]
        return Task(tid, tok, fencing, cid)

    class Transport:
        def __init__(self, usage):
            self.usage, self.calls = usage, 0

        def request(self, method, url, body, headers):
            self.calls += 1
            return 200, {"status": "completed", "usage": self.usage,
                         "output": [{"type": "message", "content": [{"type": "output_text", "text":
                                     '{"complaint": true, "category": "quality", "confidence": 0.9}'}]}]}

    def balance(t):
        return q("select reserved_usd,spent_usd from app.task_budget where task_id=%s and agent_id='agent_triage'", (t.id,))[0]

    def receipt_args(key):
        return ["usage", "reconcile", "--call-key", key, "--tokens-in", "300", "--tokens-out", "20",
                "--evidence-sha256", hashlib.sha256(b"synthetic provider receipt 300/20").hexdigest(),
                "--provider-ref", "fixture-" + key[:12], "--reason", "synthetic provider receipt checked"]

    measured = task()
    transport = Transport({"input_tokens": 300, "output_tokens": 20})
    triage = Triage(db, OpenAIModel("fixture-not-a-credential", transport))
    first = triage.classify(measured, "fixture normal complaint")
    again = triage.classify(measured, "fixture normal complaint")
    check(first == again == (True, "quality", .9) and transport.calls == 1 and balance(measured) == (0, Decimal(".00009")),
          "P29 measured usage settles once; replay reuses the answer without a second provider call or reservation")

    unknown = task()
    missing_transport = Transport(None)
    missing = Triage(db, OpenAIModel("fixture-not-a-credential", missing_transport))
    check(missing.classify(unknown, "fixture missing usage") == (False, None, 0.0),
          "P29 missing provider usage leaves the keyword route")
    held = balance(unknown)
    missing.classify(unknown, "fixture missing usage")
    check(held[0] > 0 and held[1] == 0 and balance(unknown) == held and missing_transport.calls == 1,
          "P29 missing usage retains real reserved money; retry cannot release it or call again")
    key = q("select call_key from app.model_usage_holds where task_id=%s", (unknown.id,))[0][0]
    obs = q("select tokens_in,tokens_out,cost_usd,usage_state from app.agent_calls where model_call_key=%s", (key,))[0]
    check(obs == (None, None, None, "unknown"), "P29 missing usage is a nullable observation, never a zero receipt")
    code, pending, _ = hermes(fin, "usage", "pending", "--customer", ref)
    check(code == 0 and any(r["call_key"] == key for r in pending["rows"]), "P29 pending reservations are visible to operators")
    for session_file in (ops, fin):
        code, _, err = hermes(session_file, *receipt_args(key))
        check(code == 3 and "role" in err, "P29 only founder/tech can reconcile through the operator API")
    with db.tx() as cur:
        cur.execute("select app.complete_task(%s,%s,%s,'succeeded',null)", (unknown.id, unknown.token, unknown.fencing))
        check(cur.fetchone()[0], "P29 original task can finish while its usage awaits evidence")
    code, reconciled, err = hermes(founder, *receipt_args(key))
    check(code == 0 and reconciled.get("changed") is True and balance(unknown) == (0, Decimal(".00009")),
          "P29 operator receipt settles all accounting after the worker lease ends" + (": " + err if code else ""))
    code, duplicate, _ = hermes(founder, *receipt_args(key))
    check(code == 0 and duplicate.get("changed") is False and balance(unknown) == (0, Decimal(".00009")),
          "P29 repeated operator receipt does not charge twice")
    conflicting = receipt_args(key)
    conflicting[conflicting.index("--tokens-in") + 1] = "301"
    code, _, err = hermes(founder, *conflicting)
    check(code == 5 and "MODEL_RECONCILIATION_CONFLICT" in err and balance(unknown) == (0, Decimal(".00009")),
          "P29 conflicting receipt is rejected atomically")

    class LoseSettlementTransaction:
        def __init__(self):
            self.calls = 0

        @contextmanager
        def tx(self, bind=None):
            self.calls += 1
            if self.calls == 2:
                raise ConnectionError("fixture: provider answered but the result transaction was lost")
            with db.tx(bind) as cur:
                yield cur

    lost = task()
    lost_transport = Transport({"input_tokens": 300, "output_tokens": 20})
    model = OpenAIModel("fixture-not-a-credential", lost_transport)
    try:
        Triage(LoseSettlementTransaction(), model).classify(lost, "fixture lost response")
        check(False, "P29 injected database response loss reached the caller")
    except ConnectionError:
        check(True, "P29 injected database response loss reached the caller")
    lost_hold = balance(lost)
    Triage(db, model).classify(lost, "fixture lost response")
    lost_key = q("select call_key from app.model_usage_holds where task_id=%s", (lost.id,))[0][0]
    check(lost_transport.calls == 1 and lost_hold[0] > 0 and lost_hold[1] == 0 and balance(lost) == lost_hold
          and q("select count(*) from app.agent_calls where model_call_key=%s", (lost_key,))[0][0] == 0,
          "P29 response loss leaves a durable hold; retry neither replays the provider nor invents a log")
    q("update app.task_leases set lease_until=clock_timestamp()-interval '1 second' where task_id=%s", (lost.id,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: hermes(founder, *receipt_args(lost_key)), range(2)))
    check(all(r[0] == 0 for r in results) and sorted(r[1].get("changed") for r in results) == [False, True]
          and balance(lost) == (0, Decimal(".00009")),
          "P29 two concurrent CLI reconciliations settle a lost response exactly once")
    check(q("select count(*) from app.audit_log where action='model_usage.reconciled' and target=%s", (lost_key,))[0][0] == 1,
          "P29 concurrent reconciliation has one authenticated effect audit")
    effective = q("select cost_usd,usage_state,observation_present from app.v_agent_calls_effective where model_call_key=%s",
                  (lost_key,))
    check(effective == [(Decimal(".00009"), "measured", False)],
          "P29 a reconciled lost response appears once in cost reports without an invented observation")

    zero = task()
    zero_transport = Transport({"input_tokens": 0, "output_tokens": 0})
    Triage(db, OpenAIModel("fixture-not-a-credential", zero_transport)).classify(zero, "fixture measured zero")
    check(zero_transport.calls == 1 and balance(zero) == (0, 0), "P29 explicit measured zero releases its reservation")
    check(q("select reserved_usd,spent_usd from app.customer_ai_budget where customer_id=%s and month_id=to_char(now(),'YYYY-MM')",
            (cid,)) == [(0, Decimal(".00027"))]
          and q("select reserved_usd,spent_usd from app.agent_state where customer_id=%s and agent_id='agent_triage'"
                " and month_id=to_char(now(),'YYYY-MM')", (cid,)) == [(0, Decimal(".00027"))],
          "P29 customer and agent accounting match the three measured calls and the measured zero")


def main():
    q("insert into app.operators (auth_user_id, display_name, role, mfa_enrolled, active) values"
      " (%s, 'cli founder', 'founder', true, true), (%s, 'cli ops', 'ops', true, true),"
      " (%s, 'cli finance', 'finance', true, true)", (FOUNDER, OPS, FINANCE))
    q("update app.incidents set root_cause = coalesce(root_cause, 'test'), prevention = coalesce(prevention, 'test'),"
      " closed_at = now() where closed_at is null")      # an incident left open elsewhere would stand in for every reason
    srv, url = serve()
    ops, fin, founder = session(OPS, url), session(FINANCE, url), session(FOUNDER, url)
    try:
        code, me, _ = hermes(ops, "whoami")
        check(code == 0 and me["role"] == "ops" and me["aal"] == "aal2", "whoami: the operator's own session and role")
        check(hermes(session(STRANGER, url), "whoami")[0] == 3, "a signed-in user who is not an operator: exit 3")
        code, _, err = hermes(session(OPS, url, "aal1"), "whoami")
        check(code == 3 and "aal2_required" in err, "an operator without the second factor: exit 3 (aal2_required)")

        # ---- onboarding
        add = ["customers", "add", "--name", "مطعم التجربة", "--sector", "restaurant", "--city", "Sanaa", "--zone", "zone_a"]
        code, _, err = hermes(ops, *add)
        check(code == 2 and "reason_required" in err, "a write without a reason outside an incident: exit 2")
        code, cust, _ = hermes(ops, *add, "--reason", "pilot customer one")
        check(code == 0 and cust["public_ref"].startswith("cust_"), "customers add: a new business, status lead")
        ref, cid = cust["public_ref"], cust["id"]
        row = audit("customer.created", ref)
        check(bool(row) and row[0][0] == "operator" and row[0][1] == OPS and row[0][2]["reason"] == "pilot customer one",
              "customers add: audit row by this operator with the reason")
        check(hermes(ops, "owners", "link", "--customer", ref, "--user-id", OWNER, "--reason", "owner login")[0] == 0,
              "owners link: the owner's Supabase user reaches the portal for this business")
        check(q("select count(*) from app.customer_users where customer_id = %s and auth_user_id = %s and active",
                (cid, OWNER))[0][0] == 1, "owners link: one active owner row")
        phone = str(uuid.uuid4().int)[:15]
        code, wa, _ = hermes(ops, "channels", "add", "--customer", ref, "--kind", "whatsapp_cloud", "--external-id", phone,
                             "--reason", "pilot number")
        code2, form, _ = hermes(ops, "channels", "add", "--customer", ref, "--kind", "site_form", "--reason", "site form")
        check(code == 0 and code2 == 0 and wa["status"] == "pending_verification" and len(form["external_id"]) >= 24,
              "channels add: pending until verified; a site form gets a generated key")
        check(q("select count(*) from app.channel_accounts where customer_id = %s and status = 'active'", (cid,))[0][0] == 0,
              "a channel nobody verified routes nothing")
        for ch in (wa, form):
            hermes(ops, "channels", "verify", "--id", ch["id"], "--reason", "verified with Meta")
        check(q("select count(*) from app.channel_accounts where customer_id = %s and status = 'active'"
                " and verified_at is not null", (cid,))[0][0] == 2, "channels verify: both active with a verification time")
        code, _, err = hermes(ops, "channels", "verify", "--id", wa["id"], "--reason", "again")
        check(code == 5 and "state" in err, "verifying an active channel again: exit 5")
        code, fact, _ = hermes(ops, "facts", "add", "--customer", ref, "--topic", "hours", "--fact", "من ٩ إلى ١١",
                               "--reason", "from the owner by phone")
        check(code == 0 and fact["approved_by_owner"] is False, "facts add: waits for the owner's approval in the portal")
        for i in (1, 2):
            hermes(ops, "competitors", "add", "--customer", ref, "--url", f"https://rival{i}.example.com", "--label",
                   f"rival {i}", "--reason", "named by the owner")
        code, _, err = hermes(ops, "competitors", "add", "--customer", ref, "--url", "https://rival3.example.com",
                              "--label", "rival 3", "--reason", "a third")
        check(code == 5 and "COMPETITOR_LIMIT_REACHED" in err, "a third active competitor: exit 5 with the database's code")

        # ---- the site (28.39): built from what the owner approved, nothing else
        site_dir = TMP / "site"
        code, built, _ = hermes(ops, "site", "build", "--customer", ref, "--base-url", "https://pilot.example", "--to", str(site_dir))
        page = (site_dir / "index.html").read_text() if site_dir.exists() else ""
        check(code == 0 and "من ٩ إلى ١١" not in page and built["contact_form"] is True
              and f"/forms/{form['external_id']}" in page, "site build: an unapproved fact is not published; the form posts here")
        with Database(DB, "authenticated").tx(claims={"sub": OWNER, "role": "authenticated", "aal": "aal1"}) as cur:
            cur.execute("update app.kb_facts set approved_by_owner = true where id = %s", (fact["id"],))   # the owner's act
        code, again, _ = hermes(ops, "site", "build", "--customer", ref, "--base-url", "https://pilot.example",
                                "--to", str(TMP / "site2"))
        check(code == 0 and "من ٩ إلى ١١" in (TMP / "site2" / "index.html").read_text()
              and again["content_hash"] != built["content_hash"], "site build: the approved fact appears; a new content hash")
        check(hermes(ops, "site", "build", "--customer", ref, "--base-url", "https://pilot.example", "--to", str(site_dir))[0] == 2,
              "site build: an existing directory is never overwritten")

        # ---- billing: a human verifies the payment before anything is active (spec 8.1)
        sub_args = ["billing", "subscribe", "--customer", ref, "--price-usd", "240", "--start", "2026-11-01",
                    "--end", "2027-11-01", "--reason", "annual plan"]
        code, _, err = hermes(ops, *sub_args)
        check(code == 3 and "role" in err, "billing is not the ops role: exit 3")
        code, sub, _ = hermes(fin, *sub_args)
        check(code == 0 and q("select status::text from app.customers where id = %s", (cid,))[0][0] == "pending_payment",
              "billing subscribe: subscription pending, customer pending payment")
        code, _, err = hermes(fin, "billing", "activate", "--subscription", sub["id"], "--reason", "too early")
        check(code == 5 and "ACTIVATION_WITHOUT_MATCHED_PAYMENT" in err, "activation before a matched payment: refused")
        code, inv, _ = hermes(fin, "billing", "invoice", "--subscription", sub["id"], "--amount", "240", "--currency", "USD",
                              "--reason", "first invoice")
        check(code == 0 and inv["number"].startswith("INV-"), "billing invoice: issued with its yearly number")
        code, pay, _ = hermes(fin, "billing", "payment", "--invoice", inv["id"], "--channel", "exchange", "--receipt",
                              f"rcpt-{uuid.uuid4().hex[:8]}", "--amount", "240", "--currency", "USD",
                              "--received-at", "2026-11-01", "--reason", "receipt photo received")
        check(code == 0 and pay["status"] == "pending", "billing payment: recorded, pending verification")
        code, m, _ = hermes(fin, "billing", "match", "--payment", pay["id"], "--reason", "checked with the exchange")
        check(code == 0 and m["invoice_paid"] is True, "billing match: verified by this operator; the invoice is paid")
        check(q("select verified_by::text from app.payments where id = %s", (pay["id"],))[0][0] == FINANCE,
              "the payment names the operator who verified it")
        code, _, _ = hermes(fin, "billing", "activate", "--subscription", sub["id"], "--reason", "paid in full")
        check(code == 0 and q("select status::text from app.customers where id = %s", (cid,))[0][0] == "active",
              "billing activate: subscription and customer active")

        # ---- freeze and unfreeze
        code, fr, _ = hermes(ops, "freeze", "--customer", ref, "--reason", "owner asked to stop")
        check(code == 0 and fr["channels_suspended"] == 2 and
              q("select count(*) from app.channel_accounts where customer_id = %s and status = 'active'", (cid,))[0][0] == 0,
              "freeze: customer suspended and no active channel, so nothing new is routed")
        check(hermes(ops, "freeze", "--customer", ref, "--reason", "twice")[0] == 5, "freezing a frozen customer: exit 5")
        code, un, _ = hermes(ops, "unfreeze", "--customer", ref, "--reason", "owner back")
        check(code == 0 and un["status"] == "active" and un["channels_restored"] == 2,
              "unfreeze: the earlier status and exactly the channels the freeze stopped")

        # ---- incidents: an open one stands in for the reason; it closes only with cause and prevention
        code, inc, _ = hermes(ops, "incident", "open", "--severity", "P2", "--category", "vendor", "--customers", ref,
                              "--reason", "Meta outage")
        check(code == 0 and inc["id"].startswith("INC-"), "incident open: numbered for the day")
        check(hermes(ops, "competitors", "remove", "--id", q("select id::text from app.competitors where customer_id = %s"
                                                               " and active limit 1", (cid,))[0][0])[0] == 0,
              "inside an open incident a write needs no separate reason")
        code, _, err = hermes(ops, "incident", "close", "--id", inc["id"], "--reason", "done for now")
        check(code == 5 and "check_failed" in err, "closing without root cause and prevention: refused by the table")
        hermes(ops, "incident", "update", "--id", inc["id"], "--field", "contained_at", "--value", "now")
        hermes(ops, "incident", "update", "--id", inc["id"], "--field", "root_cause", "--value", "provider outage")
        hermes(ops, "incident", "update", "--id", inc["id"], "--field", "prevention", "--value", "alert on provider status")
        check(hermes(ops, "incident", "close", "--id", inc["id"])[0] == 0, "incident close with cause and prevention")

        # ---- agents, roles
        check(hermes(ops, "agent", "disable", "--agent", "agent_cli_probe", "--reason", "test pause")[0] == 0,
              "agent disable: paused by the operator")
        check(hermes(ops, "agent", "disable", "--agent", "agent_cli_probe", "--reason", "again")[0] == 5,
              "agent disable twice: exit 5")
        check(hermes(ops, "agent", "enable", "--agent", "agent_cli_probe")[0] == 2, "agent enable without a reason: exit 2")
        check(hermes(ops, "agent", "enable", "--agent", "agent_cli_probe", "--reason", "test over")[0] == 0,
              "agent enable with a reason: released, the reason recorded")
        check(hermes(ops, "agent", "reset-circuit", "--agent", "agent_replies", "--customer", ref,
                     "--reason", "test reset")[0] == 3, "reset-circuit is tech: exit 3 for ops")
        check(hermes(ops, "gates", "record", "--gate", "0.1", "--value", "0.97", "--source", "test")[0] == 3,
              "gates record is founder: exit 3 for ops")
        check(hermes(founder, "gates", "record", "--gate", "0.1", "--value", "0.97", "--source", "cli test",
                     "--reason", "measured")[0] == 0, "gates record by the founder")

        # ---- usage accounting: synthetic provider transport, actual PostgreSQL and CLI transactions (P29)
        model_usage_journey(cid, ref, founder, ops, fin)

        # ---- quality flags
        flag = q("insert into app.quality_flags (customer_id, source, rule_id, severity) values (%s, 'content_guard',"
                 " 'cli_test', 'review') returning id::text", (cid,))[0][0]
        check(hermes(ops, "flags", "resolve", "--id", flag, "--status", "dismissed", "--reason", "false alarm")[0] == 0,
              "flags resolve: dismissed by the operator")
        check(hermes(ops, "flags", "resolve", "--id", flag, "--status", "confirmed", "--reason", "again")[0] == 4,
              "a resolved flag is not open: exit 4")

        # ---- every read command, once (their SQL runs nowhere else)
        reads = [["customers", "list"], ["customers", "show", "--customer", ref], ["facts", "list", "--customer", ref],
                 ["channels", "list"], ["channels", "list", "--customer", ref], ["competitors", "list", "--customer", ref],
                 ["agent", "status"], ["agent", "status", "--agent", "agent_replies", "--customer", ref], ["agent", "health"],
                 ["calls"], ["calls", "--outcome", "!success", "--since", "7d", "--limit", "5"], ["cost"],
                 ["cost", "--group-by", "agent", "--customer", ref], ["queue", "breakdown"], ["approvals", "list", "--pending"],
                 ["approvals", "list", "--customer", ref], ["outbox", "attention"], ["flags", "list", "--open"],
                 ["incident", "list"], ["billing", "list", "--customer", ref], ["usage", "pending"]]
        bad = [r for r in reads if hermes(fin, *r)[0] != 0]
        check(not bad, f"every read command answers for any operator role ({len(reads)} commands){bad or ''}")
        code, shown, _ = hermes(ops, "customers", "show", "--customer", ref)
        check(code == 0 and shown["owners"] == 1 and shown["facts"] == {"approved": 1, "total": 1}
              and shown["subscription"]["status"] == "active", "customers show: the onboarding state in one answer")

        # ---- evidence and the chain
        out = TMP / "evidence.json"
        code, _, _ = hermes(founder, "logs", "export", "--customer", ref, "--since", "1h", "--to", str(out),
                            "--reason", "review of the test")
        actions = {r["action"] for r in json.loads(out.read_text())["audit"]} if out.exists() else set()
        check(code == 0 and {"customer.created", "owner.linked", "channel.verified", "payment.matched",
                             "subscription.activated", "customer.frozen", "customer.unfrozen"} <= actions,
              "logs export: the customer's audit trail, written to a new file")
        check(bool(audit("logs.exported", cid)), "the export is itself audited")
        check(q("select count(*) from app.audit_verify()")[0][0] == 0, "the audit chain still verifies")
    finally:
        srv.shutdown()
    if failures:
        print(f"ops cli: {len(failures)} FAILED")
        sys.exit(1)
    print("ops cli: all PASS")


if __name__ == "__main__":
    main()
