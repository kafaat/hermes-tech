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
  5. the owner approves IN THE PORTAL     -> signed in with a session token, sees the proposal (another user does
                                            not), a decision without the CSRF token is refused; the approval is
                                            consumed once by the outbox, the reply is sent, task succeeded
  6. the delivery receipt webhook         -> routed, processed, task succeeded
  7. audit: enqueue by the leased agent and the owner's decision, in the chain
  8. operator console: a row waiting for a human (the state an interrupted send leaves) is shown only to an aal2
     operator, not to the owner nor to the operator without a second factor; "resend" with a reason queues its
     task and the worker sends it
  9. competitor check (workflow, 28.11): the daily job as hermes_jobs finds the due competitor, reads the Arabic
     restaurant fixture (no network in CI), files an 'ok' snapshot with the inventory, is not due again the same week,
     and the owner sees it in the portal. HERMES_E2E_COMPETITOR_URL (staging) adds one real page for the daily job.
 10. GET /deps (external monitor): without X-Monitor-Token only the code and one word; with it, numbers only, and
     nothing failing after this run (a purge not yet due is not stale: the database knows when it is due)
"""
from __future__ import annotations
import base64, hashlib, hmac, json, os, socket, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from service.auth import csrf_token, issue_staging_token  # noqa: E402
DB = os.environ["DATABASE_URL"]
CUSTOMER = "00000000-0000-0000-0000-0000000e2e01"
OWNER = "00000000-0000-0000-0000-0000000e2e0a"
OPERATOR = "00000000-0000-0000-0000-0000000e2e0b"
PHONE_ID = "pn-e2e-staging"
FB_PAGE, IG_ACCOUNT, TT_ACCOUNT = "1069900000001", "178419900000001", "_000e2eTikTok01"
SITE_KEY = "e2esiteKEYAAAAAAAAAAAAAAAA01"
EMAIL_INBOX = "e2e-shop@inbound.hermes.example"
EMAIL_SECRET = (os.environ.get("HERMES_EMAIL_INBOUND_SECRETS") or "").split(",")[0].strip() or f"postmark:{uuid.uuid4().hex}"
HOURS = "نفتح يوميًا من ٩ صباحًا إلى ١١ مساءً"
MONITOR_TOKEN = os.environ.get("HERMES_MONITOR_TOKEN") or uuid.uuid4().hex   # staging: the service's own (shared var)
JWT_SECRET = os.environ.get("HERMES_JWT_SECRET") or uuid.uuid4().hex * 2       # staging: the service's own (shared var)
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
    for kind, ext_id in (("facebook_page", FB_PAGE), ("instagram_business", IG_ACCOUNT), ("tiktok_business", TT_ACCOUNT),
                         ("site_form", SITE_KEY), ("email", EMAIL_INBOX)):
        q("insert into app.channel_accounts (customer_id, kind, external_id, status, verified_at)"
          " values (%s, %s, %s, 'active', now()) on conflict (kind, external_id) do nothing", (CUSTOMER, kind, ext_id))
    q("update app.kb_facts set approved_by_owner = false where customer_id = %s and topic = 'hours' and fact <> %s"
      " and approved_by_owner", (CUSTOMER, HOURS))           # one approved hours answer: earlier runs leave none behind
    if not q("select 1 from app.kb_facts where customer_id = %s and topic = 'hours' and approved_by_owner", (CUSTOMER,)):
        q("insert into app.kb_facts (customer_id, topic, fact, approved_by_owner) values (%s, 'hours', %s, true)",
          (CUSTOMER, HOURS), claims={"sub": OWNER, "aal": "aal1"})       # an approved fact is the owner's act (kb_facts_guard)
    q("update app.standing_approvals set revoked_at = now(), revoked_by = %s where customer_id = %s and revoked_at is null",
      (OWNER, CUSTOMER), claims={"sub": OWNER, "aal": "aal1"})    # a run that died mid-way leaves no standing approval behind
    q("insert into app.standing_pause (customer_id, paused, changed_by) values (%s, false, %s)"
      " on conflict (customer_id) do update set paused = false, changed_by = excluded.changed_by",
      (CUSTOMER, OWNER), claims={"sub": OWNER, "aal": "aal1"})    # nor a paused switch


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def portal(url, token, method="GET", form=None):
    """(status, location, page) as a browser holding the owner's session cookie would see them."""
    target = {"GET": "/portal", "GET_OPS": "/portal/ops"}.get(method) or form.pop("_path")
    req = urllib.request.Request(url + target, method="GET" if method.startswith("GET") else method,
                                 data=urllib.parse.urlencode(form).encode() if form else None,
                                 headers={"Cookie": f"__Host-hermes_owner={token}"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=15) as r:
            return r.status, r.headers.get("Location"), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location"), e.read().decode()


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
           "HERMES_MONITOR_TOKEN": MONITOR_TOKEN, "HERMES_JWT_SECRET": JWT_SECRET,
           "HERMES_APPROVAL_POLL_SECONDS": "1", "HERMES_VERIFY_TOKEN": "e2e", "HERMES_EMAIL_INBOUND_SECRETS": EMAIL_SECRET}
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
        want = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")    # staging: both services deploy on the same push

        def healthy():
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=5) as r:
                    return r.status == 200 and (not want or json.load(r).get("commit") == want)
            except (OSError, ValueError):
                return False
        check(bool(wait("service healthy", healthy, 300)),
              f"service answers /healthz at {url}" + (f" on commit {want[:7]}" if want else ""))
        seed()
        # a run that crashed mid-way can leave a reply proposal pending (its event unprocessed counts as a backlog in
        # /deps): the test owner rejects its own leftovers, as a person would, and waits for those tasks to close
        leftovers = q("select a.id, a.target_id from app.approvals a where a.customer_id = %s and a.decision = 'pending'"
                      " and a.proposal_action = 'reply:send' and a.expires_at > now()", (CUSTOMER,))
        if leftovers:
            owner = issue_staging_token(OWNER, JWT_SECRET)
            for ap_left, _ in leftovers:
                portal(url, owner, "POST", {"_path": "/portal/decide", "approval": str(ap_left), "decision": "rejected",
                                            "csrf": csrf_token(owner, JWT_SECRET)})
            wait("leftover tasks closed", lambda: not q(
                "select 1 from app.webhook_events where customer_id = %s and processed_at is null and external_event_id = any(%s)",
                (CUSTOMER, [t for _, t in leftovers])), 150)
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
        check(q("select count(*) from app.inquiries where customer_id = %s and event_ref in (%s, %s)", (CUSTOMER, q_ext, c_ext))[0][0] == 2,
              "one inquiry per inbound message, keyed by its message id (0018)")
        check(not q("select 1 from app.outbox where topic = 'reply.send' and target_id = %s", (q_ext,)), "nothing sent before the owner decides")

        v_ext, r_ext0 = f"wamid.E2E.{run}.voice", f"wamid.E2E.{run}.react"     # a voice note and a reaction (0019)
        check(post(url, secret, message_payload({"messages": [
            {"from": "967700000004", "id": v_ext, "type": "audio", "audio": {"id": "media1", "voice": True}},
            {"from": "967700000004", "id": r_ext0, "type": "reaction", "reaction": {"message_id": q_ext, "emoji": "👍"}}]})) == 200,
              "a voice note and a reaction accepted (200)")
        check(bool(wait("voice note escalated", lambda: q("select 1 from app.outbox where topic = 'notify.owner' and target_id = %s"
                                                            " and payload->>'reason' = 'non_text:audio'", (v_ext,)))),
              "voice note -> the owner is told it is a voice note (non_text:audio), not 'no approved answer'")
        check(q("select message_type, body from app.inquiries where event_ref = %s", (v_ext,)) == [("audio", None)],
              "voice note -> inquiry typed audio, nothing of the media stored")
        check(bool(wait("reaction done", lambda: (task_status(r_ext0) or ("",))[0] == "succeeded"))
              and not q("select 1 from app.inquiries where event_ref = %s", (r_ext0,))
              and not q("select 1 from app.outbox where target_id = %s", (r_ext0,)), "reaction -> ignored: no inquiry, no notice")

        # Messenger and Instagram Direct: the same rules, the answer goes back on the channel the customer used
        fb_ext, ig_ext = f"m_E2E.{run}.fb", f"aWdf.E2E.{run}.ig"
        check(post(url, secret, {"object": "page", "entry": [{"id": FB_PAGE, "messaging": [
            {"sender": {"id": "2559900000001"}, "recipient": {"id": FB_PAGE}, "message": {"mid": fb_ext, "text": "متى تفتحون اليوم؟"}},
            {"sender": {"id": FB_PAGE}, "recipient": {"id": "2559900000001"},
             "message": {"mid": f"m_E2E.{run}.echo", "is_echo": True, "text": "x"}}]}]}) == 200
              and post(url, secret, {"object": "instagram", "entry": [{"id": IG_ACCOUNT, "messaging": [
                  {"sender": {"id": "9919900000001"}, "recipient": {"id": IG_ACCOUNT},
                   "message": {"mid": ig_ext, "text": "الفاتورة غلط ودفعت مرتين"}}]}]}) == 200,
              "Messenger and Instagram Direct messages accepted (200)")
        check(not q("select 1 from app.webhook_events where external_event_id = %s", (f"m_E2E.{run}.echo",)),
              "Messenger: the echo of our own message is not stored")
        fb_ap = wait("Messenger proposal", lambda: q("select id, payload from app.approvals where target_id = %s", (fb_ext,)))
        check(bool(fb_ap) and fb_ap[0][1].get("channel") == "facebook_page" and fb_ap[0][1].get("account_id") == FB_PAGE
              and fb_ap[0][1].get("to") == "2559900000001" and fb_ap[0][1].get("body") == HOURS,
              "Messenger question -> a reply proposal addressed back to the page conversation")
        check(bool(wait("Instagram complaint escalated", lambda: q(
            "select 1 from app.outbox where topic = 'notify.owner' and target_id = %s and dispatched_at is not null", (ig_ext,))))
              and q("select source::text from app.inquiries where event_ref = %s", (ig_ext,)) == [("instagram",)]
              and not q("select 1 from app.approvals where target_id = %s", (ig_ext,)),
              "Instagram complaint -> owner notified, recorded as instagram, never auto-answered")
        if fb_ap:
            owner_t = issue_staging_token(OWNER, JWT_SECRET)
            portal(url, owner_t, "POST", {"_path": "/portal/decide", "approval": str(fb_ap[0][0]), "decision": "approved",
                                          "csrf": csrf_token(owner_t, JWT_SECRET)})
            fb_sent = wait("Messenger reply sent", lambda: q("select provider_message_id from app.outbox where target_id = %s"
                                                            " and topic = 'reply.send' and dispatched_at is not null", (fb_ext,)))
            check(bool(fb_sent) and fb_sent[0][0].startswith("m_SIM."),
                  "Messenger: after the owner's approval the reply goes out through Messenger (message id m_...)")

        # posts (0023): the owner writes, the platform checks and proposes, the owner approves, then it is published
        owner_p = issue_staging_token(OWNER, JWT_SECRET)
        for kind, acct, body, image in (("facebook_page", FB_PAGE, f"عرض نهاية الأسبوع {run}", None),
                                        ("instagram_business", IG_ACCOUNT, f"منتج جديد {run}", "https://cdn.example.test/new.jpg"),
                                        ("tiktok_business", TT_ACCOUNT, f"صورة اليوم {run}", "https://cdn.example.test/day.jpg")):
            status, where, _ = portal(url, owner_p, "POST", {"_path": "/portal/posts/new", "account": f"{kind}:{acct}", "body": body,
                                                             **({"image_url": image} if image else {}), "csrf": csrf_token(owner_p, JWT_SECRET)})
            prop = wait(f"{kind} post proposal", lambda: q("select a.id, a.payload from app.approvals a join app.content_items c"
                                                          " on a.target_id = c.id::text where c.body = %s and a.proposal_action = 'content:publish'",
                                                          (body,)))
            check(where == "/portal?done=post" and bool(prop) and prop[0][1].get("account_id") == acct
                  and prop[0][1].get("image_url") == image,
                  f"post on {kind}: the owner's draft is proposed with exactly its account and image")
            if prop:
                portal(url, owner_p, "POST", {"_path": "/portal/decide", "approval": str(prop[0][0]), "decision": "approved",
                                              "csrf": csrf_token(owner_p, JWT_SECRET)})
                done = wait(f"{kind} post published", lambda: q(
                    "select c.status::text, o.provider_message_id from app.content_items c join app.outbox o on o.target_id = c.id::text"
                    " where c.body = %s and o.topic = 'content.publish' and o.dispatched_at is not null and c.status = 'published'", (body,)))
                check(bool(done) and done[0][1].startswith("SIM_tt_" if kind == "tiktok_business" else "SIM_post_"),
                      f"post on {kind}: published only after the owner's approval, marked published under it")
        # the contact form of the customer's site (28.24): stored for the owner, never answered automatically
        def form_post(key, fields):
            req = urllib.request.Request(f"{url}/forms/{key}", data=urllib.parse.urlencode(fields).encode(), method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded", "X-Forwarded-For": f"198.51.100.{run[:2]}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
        f_msg = f"عندكم توصيل لعدن؟ {run}"
        status, page = form_post(SITE_KEY, {"message": f_msg, "name": "سالم", "phone": "+967 712 345 678"})
        check(status == 200 and "وصلت رسالتك" in page, "site form: the visitor gets the thank-you page")
        f_inq = wait("site form inquiry", lambda: q("select event_ref, body from app.inquiries where source = 'site_form' and body like %s",
                                                    (f_msg + "%",)))
        check(bool(f_inq) and "سالم · +967 712 345 678" in f_inq[0][1], "site form: the owner gets the message with the visitor's name and number")
        check(bool(f_inq) and bool(wait("site form notice", lambda: q("select 1 from app.outbox where topic = 'notify.owner' and target_id = %s"
                                                                       " and dispatched_at is not null", (f_inq[0][0],))))
              and not q("select 1 from app.approvals where target_id = %s", (f_inq[0][0],)),
              "site form: the owner is notified; nothing is proposed or sent to the visitor")
        before = q("select count(*) from app.webhook_events where kind = 'site_form'")[0][0]
        status_trap, _ = form_post(SITE_KEY, {"message": "spam", "website": "http://spam.example"})
        status_unknown, _ = form_post("unknownKEYBBBBBBBBBBBBBBBBBB01", {"message": "x"})
        check((status_trap, status_unknown) == (200, 404) and q("select count(*) from app.webhook_events where kind = 'site_form'")[0][0] == before,
              "site form: a bot (trap field) and an unknown site key store nothing")

        # email (28.25): a customer's question becomes a reply proposal in the same thread; automated mail is never stored
        def email_post(payload, secret_=EMAIL_SECRET):
            req = urllib.request.Request(f"{url}/email/inbound", data=json.dumps(payload).encode(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Basic " + base64.b64encode(secret_.encode()).decode()})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        def email(mid, subject, text, sender="salem@customer.example", headers=(), to=EMAIL_INBOX):
            return {"MessageID": mid, "OriginalRecipient": to, "FromFull": {"Email": sender, "Name": "سالم"}, "Subject": subject,
                    "TextBody": text, "StrippedTextReply": "", "Attachments": [],
                    "Headers": [{"Name": "Message-ID", "Value": f"<{mid}@mail.customer.example>"}, *headers]}
        e_mid = f"e2e-mail-{run}-hours"
        check(email_post(email(e_mid, f"الدوام {run}", "متى تفتحون يوم الجمعة؟")) == 200, "email: the provider's delivery is accepted (200)")
        e_prop = wait("email proposal", lambda: q("select id, payload from app.approvals where target_id = %s and proposal_action = 'reply:send'",
                                                  (e_mid,)))
        check(bool(e_prop) and e_prop[0][1].get("channel") == "email" and e_prop[0][1].get("to") == "salem@customer.example"
              and e_prop[0][1].get("account_id") == EMAIL_INBOX and e_prop[0][1].get("subject") == f"Re: الدوام {run}"
              and e_prop[0][1].get("message_id") == f"<{e_mid}@mail.customer.example>" and e_prop[0][1].get("body") == HOURS,
              "email: an hours question is proposed as a reply to the sender, in the same thread, with the approved answer")
        check(bool(q("select 1 from app.inquiries where event_ref = %s and source = 'email' and body like %s", (e_mid, "%سالم · salem@customer.example"))),
              "email: the owner sees the email with the sender's name and address")
        if e_prop:
            _, _, page = portal(url, owner_p)
            check("s•••@customer.example بالبريد" in page, "email: the portal shows the proposal as a reply by email, the address masked")
            portal(url, owner_p, "POST", {"_path": "/portal/decide", "approval": str(e_prop[0][0]), "decision": "approved",
                                          "csrf": csrf_token(owner_p, JWT_SECRET)})
            e_sent = wait("email reply sent", lambda: q("select provider_message_id from app.outbox where topic = 'reply.send'"
                                                         " and target_id = %s and dispatched_at is not null", (e_mid,)))
            check(bool(e_sent) and e_sent[0][0].startswith("sim-email-"), "email: sent once through the outbox after the owner's approval")
        before = q("select count(*) from app.webhook_events where kind = 'email'")[0][0]
        auto = email_post(email(f"e2e-mail-{run}-auto", "Out of office", "I am away", headers=[{"Name": "Auto-Submitted", "Value": "auto-replied"}]))
        wrong = email_post(email(f"e2e-mail-{run}-wrong", "x", "x"), "postmark:wrong")
        unknown = email_post(email(f"e2e-mail-{run}-unknown", "x", "x", to="nobody@inbound.hermes.example"))
        check((auto, wrong, unknown) == (200, 401, 403) and q("select count(*) from app.webhook_events where kind = 'email'")[0][0] == before,
              f"email: an auto-reply, a wrong secret and an unknown inbox store nothing ({auto}, {wrong}, {unknown})")
        s_mid = f"e2e-mail-{run}-spam"
        email_post(email(s_mid, "Win", "متى تفتحون؟", headers=[{"Name": "X-Spam-Status", "Value": "Yes, score=9"}]))
        check(bool(wait("spam email notice", lambda: q("select 1 from app.outbox where topic = 'notify.owner' and target_id = %s"
                                                        " and dispatched_at is not null", (s_mid,))))
              and not q("select 1 from app.approvals where target_id = %s", (s_mid,)),
              "email: mail marked as spam reaches the owner and is never answered automatically")

        blocked = f"هذا العسل يعالج السكر {run}"                 # a health claim: content_guard blocks it in posts
        portal(url, owner_p, "POST", {"_path": "/portal/posts/new", "account": f"facebook_page:{FB_PAGE}", "body": blocked,
                                      "csrf": csrf_token(owner_p, JWT_SECRET)})
        check(bool(wait("blocked post rejected", lambda: q("select 1 from app.content_items where body = %s and status = 'rejected'", (blocked,))))
              and not q("select 1 from app.approvals a join app.content_items c on a.target_id = c.id::text where c.body = %s", (blocked,)),
              "post with a health claim: never proposed, never published")

        if ap:
            token = issue_staging_token(OWNER, JWT_SECRET)
            status, _, page = portal(url, token)
            check(status == 200 and HOURS in page and str(ap_id) in page, "portal: the owner sees the pending proposal")
            stranger = issue_staging_token(str(uuid.uuid4()), JWT_SECRET)
            status, _, page = portal(url, stranger)
            check(status == 200 and str(ap_id) not in page, "portal: a signed-in stranger sees no proposal of this tenant")
            status, _, _ = portal(url, stranger, "POST", {"_path": "/portal/decide", "approval": str(ap_id), "decision": "approved",
                                                          "csrf": csrf_token(stranger, JWT_SECRET)})
            check(q("select decision::text from app.approvals where id = %s", (ap_id,))[0][0] == "pending",
                  f"portal: a stranger's decision changes nothing ({status})")
            status, _, _ = portal(url, token, "POST", {"_path": "/portal/decide", "approval": str(ap_id), "decision": "approved"})
            check(status == 403, "portal: a decision without the session's CSRF token is refused")
            status, where, _ = portal(url, token, "POST", {"_path": "/portal/decide", "approval": str(ap_id), "decision": "approved",
                                                           "csrf": csrf_token(token, JWT_SECRET)})
            check(status == 303 and where == "/portal?done=approved", "portal: the owner approves")
            sent = wait("reply sent", lambda: q("select id, provider_message_id, approval_id from app.outbox where topic = 'reply.send'"
                                                 " and target_id = %s and dispatched_at is not null", (q_ext,)))
            if sent:
                ob_id, wamid, used = sent[0]
                check(wamid.startswith("wamid.SIM.") and str(used) == str(ap_id), "owner approval -> reply sent once through the outbox")
                cons = q("select consumed_by_ref from app.approvals where id = %s", (ap_id,))[0][0]
                check(cons == f"outbox:{ob_id}", "approval consumed by exactly that outbox row")
                check(bool(wait("question task done", lambda: (task_status(q_ext) or ("",))[0] == "succeeded")), "question task succeeded")

                # standing approval (0020): the owner lets the hours answer go out at once, then takes it back
                owner_claims = {"sub": OWNER, "role": "authenticated", "aal": "aal1"}
                revoke = ("update app.standing_approvals set revoked_at = now(), revoked_by = %s"
                          " where customer_id = %s and revoked_at is null")
                hours_id = str(q("select id from app.kb_facts where customer_id = %s and topic = 'hours' and approved_by_owner"
                                 " order by updated_at desc, id desc limit 1", (CUSTOMER,))[0][0])     # the one the worker answers with
                try:
                    status, where, _ = portal(url, token, "POST", {"_path": "/portal/facts/standing", "fact": hours_id, "on": "1",
                                                                   "csrf": csrf_token(token, JWT_SECRET)})
                    check(status == 303 and where == "/portal?done=standing_on", "standing approval: the owner grants it for hours")
                    s_ext = f"wamid.E2E.{run}.standing"
                    post(url, secret, message_payload({"messages": [
                        {"from": "967700000005", "id": s_ext, "type": "text", "text": {"body": "متى تفتحون بكرة؟"}}]}))
                    auto = wait("standing reply sent", lambda: q(
                        "select a.decided_via, a.decided_by, o.provider_message_id from app.outbox o join app.approvals a on a.id = o.approval_id"
                        " where o.target_id = %s and o.dispatched_at is not null", (s_ext,)))
                    check(bool(auto) and auto[0][0] == "standing" and str(auto[0][1]) == OWNER,
                          "standing approval: the next hours question is answered at once, decided as the owner's standing approval")
                    # the one switch (0022): paused, the same question waits; resumed, it goes out at once again
                    status, where, _ = portal(url, token, "POST", {"_path": "/portal/standing/pause", "customer": CUSTOMER, "paused": "1",
                                                                   "csrf": csrf_token(token, JWT_SECRET)})
                    check(status == 303 and where == "/portal?done=paused", "one switch: the owner pauses every instant reply")
                    p_ext = f"wamid.E2E.{run}.paused"
                    post(url, secret, message_payload({"messages": [
                        {"from": "967700000005", "id": p_ext, "type": "text", "text": {"body": "متى تفتحون الجمعة؟"}}]}))
                    held = wait("proposal while paused", lambda: q("select id, decision::text from app.approvals where target_id = %s", (p_ext,)))
                    check(bool(held) and held[0][1] == "pending" and not q("select 1 from app.outbox where target_id = %s", (p_ext,)),
                          "one switch: while paused, the granted answer waits for the owner")
                    if held:
                        portal(url, token, "POST", {"_path": "/portal/decide", "approval": str(held[0][0]), "decision": "rejected",
                                                    "csrf": csrf_token(token, JWT_SECRET)})
                    status, where, _ = portal(url, token, "POST", {"_path": "/portal/standing/pause", "customer": CUSTOMER, "paused": "0",
                                                                   "csrf": csrf_token(token, JWT_SECRET)})
                    u_ext = f"wamid.E2E.{run}.resumed"
                    post(url, secret, message_payload({"messages": [
                        {"from": "967700000005", "id": u_ext, "type": "text", "text": {"body": "متى تفتحون السبت؟"}}]}))
                    check(where == "/portal?done=resumed" and bool(wait("reply after resume", lambda: q(
                        "select 1 from app.outbox where target_id = %s and dispatched_at is not null", (u_ext,)))),
                          "one switch: resumed, the granted answer goes out at once again, no new grant needed")
                    status, where, _ = portal(url, token, "POST", {"_path": "/portal/facts/standing", "fact": hours_id, "on": "0",
                                                                   "csrf": csrf_token(token, JWT_SECRET)})
                    check(status == 303 and where == "/portal?done=standing_off", "standing approval: the owner revokes it")
                    r_ext = f"wamid.E2E.{run}.after_revoke"
                    post(url, secret, message_payload({"messages": [
                        {"from": "967700000005", "id": r_ext, "type": "text", "text": {"body": "متى تفتحون اليوم؟"}}]}))
                    back = wait("proposal after revoke", lambda: q("select decision::text from app.approvals where target_id = %s", (r_ext,)))
                    check(bool(back) and back[0][0] == "pending" and not q("select 1 from app.outbox where target_id = %s", (r_ext,)),
                          "standing approval: after revoking, the answer waits for the owner again")
                    pending_id = q("select id from app.approvals where target_id = %s", (r_ext,))[0][0]
                    portal(url, token, "POST", {"_path": "/portal/decide", "approval": str(pending_id), "decision": "rejected",
                                                "csrf": csrf_token(token, JWT_SECRET)})     # closes that task before the end checks
                finally:
                    q(revoke, (OWNER, CUSTOMER), claims=owner_claims)
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
        q("insert into app.operators (auth_user_id, display_name, role, mfa_enrolled, active)"
          " values (%s, 'staging operator', 'founder', true, true) on conflict (auth_user_id) do nothing", (OPERATOR,))
        target = f"e2e-ops-{run}"
        q("insert into app.outbox (customer_id, topic, payload, target_id) values (%s, 'notify.owner', %s, %s)",
          (CUSTOMER, json.dumps({"event": target, "reason": "complaint"}), target))
        q("update app.outbox set needs_human_check = true, last_error = 'AMBIGUOUS:E2E' where target_id = %s", (target,))
        ob = q("select id from app.outbox where target_id = %s", (target,))[0][0]
        op2 = issue_staging_token(OPERATOR, JWT_SECRET, aal="aal2")
        status, _, _ = portal(url, issue_staging_token(OWNER, JWT_SECRET) , "GET_OPS")
        check(status == 403, "operator console: refused to the owner")
        status, _, _ = portal(url, issue_staging_token(OPERATOR, JWT_SECRET), "GET_OPS")
        check(status == 403, "operator console: refused to the operator without a second factor (aal1)")
        status, _, page = portal(url, op2, "GET_OPS")
        check(status == 200 and f"#{ob}" in page, "operator console: the aal2 operator sees the row waiting for a human")
        check("المصادر الخارجية" in page and "الردود عبر Graph" in page and "مواقع المنافسين" in page,
              "operator console: one health line per external source (28.15)")
        status, where, _ = portal(url, op2, "POST", {"_path": "/portal/ops/resolve", "outbox": str(ob), "resolution": "resend",
                                                     "reason": "e2e: provider log shows no delivery", "csrf": csrf_token(op2, JWT_SECRET)})
        check(status == 303 and where == "/portal/ops?done=resolved", "operator console: resend with a reason is accepted")
        check(bool(wait("resend sent", lambda: q("select 1 from app.outbox where id = %s and dispatched_at is not null", (ob,)))),
              "operator resend -> the worker sent the row again")
        check(bool(wait("resend task done", lambda: q("select 1 from app.tasks where idempotency_key like %s and status = 'succeeded'",
                                                        (f"resend:{ob}:%",)))),       # sent first, completed in the next transaction
              "operator resend -> its task succeeded")

        from service import competitor
        from service.pg import CompetitorDb, Database

        class FixtureFetcher:
            def fetch(self, url):
                class P:
                    status, body = 200, (ROOT / "tests/fixtures/competitor_restaurant_ar.html").read_bytes()
                return P()
        comp_id = str(uuid.uuid4())
        # earlier runs' fixture snapshots count toward the customer's 10 a month (0017) and would refuse this one;
        # they are this test's own leftovers (never a real page's), so they go first
        q("delete from app.competitor_snapshots where customer_id = %s and competitor_id in"
          " (select id from app.competitors where customer_id = %s and url like 'https://e2e-%%.example.test/')", (CUSTOMER, CUSTOMER))
        q("insert into app.competitors (id, customer_id, url, label, active) values (%s, %s, %s, %s, true)",
          (comp_id, CUSTOMER, f"https://e2e-{run}.example.test/", f"e2e {run}"))
        jobs = CompetitorDb(Database(DB, "hermes_jobs"))
        try:
            due = [c for c in jobs.due(200) if c["id"] == comp_id]
            check(len(due) == 1, "competitor job: the new competitor is due")
            counts = competitor.run(type("S", (), {"due": lambda self, n: due, "insert": lambda self, snap: jobs.insert(snap)})(),
                                    FixtureFetcher())
            snap = q("select status::text, diff_summary, structured_facts->'business'->>'type', page_hash from app.competitor_snapshots"
                     " where competitor_id = %s", (comp_id,))
            check(counts["ok"] == 1 and len(snap) == 1 and snap[0][0] == "ok" and snap[0][2] == "restaurant"
                  and snap[0][1].startswith("أول لقطة: 9 من المنتجات والخدمات") and snap[0][3] is not None,
                  f"competitor job: an 'ok' snapshot with the inventory, facts and page hash, filed as hermes_jobs {counts}")
            check(not [c for c in jobs.due(200) if c["id"] == comp_id], "competitor job: not due again the same week")
            status, _, page = portal(url, issue_staging_token(OWNER, JWT_SECRET))
            check(status == 200 and f"e2e {run}" in page and "أول لقطة" in page, "portal: the owner sees the competitor snapshot")
        finally:
            q("update app.competitors set active = false where id = %s", (comp_id,))   # the daily job must not fetch it
        real = os.environ.get("HERMES_E2E_COMPETITOR_URL")
        if real:
            q("update app.competitors set active = false where customer_id = %s and label = 'staging real page' and url <> %s", (CUSTOMER, real))
            q("insert into app.competitors (customer_id, url, label, active) values (%s, %s, 'staging real page', true)"
              " on conflict (customer_id, url) do update set active = true", (CUSTOMER, real))

        def get_deps(headers):
            try:
                with urllib.request.urlopen(urllib.request.Request(url + "/deps", headers=headers), timeout=10) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
        bare_code, bare = get_deps({})
        code, raw = get_deps({"X-Monitor-Token": MONITOR_TOKEN})
        check(bare in ("ok", "degraded") and bare_code == code and get_deps({"X-Monitor-Token": "wrong"})[1] == bare,
              f"/deps without the monitor token: the code and one word only ({bare_code} {bare})")
        try:
            deps = json.loads(raw)
        except ValueError:
            deps = {}
        counts = {"retention_age_seconds", "overdue_bodies", "outbox_attention", "webhook_backlog", "webhook_unrouted"}
        check(set(deps) == {"status", "failing", "retention_due_at", "retention_stale"} | counts
              and all(deps[k] is None or isinstance(deps[k], int) for k in counts) and isinstance(deps["retention_stale"], bool)
              and (code == 200) == (deps["failing"] == []), f"/deps answers with numbers only ({code} {deps.get('failing')})")
        check(code == 200 and deps.get("failing") == [],
              f"/deps: nothing failing after the run, a purge not yet due included (due {deps.get('retention_due_at')})")
    finally:
        if proc:
            proc.terminate(); proc.wait(10)
    print(f"e2e: {'PASS' if not failures else 'FAIL'} ({len(failures)} failed)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
