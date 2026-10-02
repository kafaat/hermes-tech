"""Email over Postmark (spec 28.25): inbound mail is authenticated before it is read, stored only for an active
inbound address, never stored when automated; a reply goes back in the customer's thread, from a configured sender,
and only what the owner approved."""
import base64, json, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.dispatcher import AMBIGUOUS, FAILED_BEFORE_SEND, FAILED_PERMANENT, SENT, BeforeSend, classify  # noqa: E402
from service.dispatcher import PostmarkEmailAdapter, ReplyRouter  # noqa: E402
from service.email_inbound import EMAIL_MAX_BYTES, EmailHandler, address, parse, strip_quoted  # noqa: E402
from service.worker import email_content, email_senders, graph_post, live_adapters, reply_subject  # noqa: E402

INBOX = "shop-1@inbound.hermes.example"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"postmark:s3cret-one").decode(), "Content-Type": "application/json"}


class Ingest:
    def __init__(self, active=(INBOX,)):
        self.active, self.rows = set(active), []

    def channel_active(self, kind, key):
        return kind == "email" and key in self.active

    def insert_event(self, kind, ext, valid, payload, channel):
        if any(r[1] == ext for r in self.rows):
            return "duplicate"
        self.rows.append((kind, ext, valid, payload, channel))
        return "inserted"


def mail(**over):
    m = {"MessageID": "a8c1040e-db1f-4e9b-a5a6-9f1c3c0b1f21", "OriginalRecipient": f"Shop <{INBOX.upper()}>",
         "FromFull": {"Email": "Salem@Customer.example", "Name": "سالم  أحمد"}, "Subject": "  سؤال\r\n عن الدوام ",
         "TextBody": "متى تفتحون يوم الجمعة؟\n\nOn Tue, Sep 30, 2026 Shop wrote:\n> old text", "StrippedTextReply": "",
         "Headers": [{"Name": "Message-ID", "Value": "<CAF123@mail.customer.example>"}, {"Name": "X-Spam-Status", "Value": "No"},
                     {"Name": "Received-SPF", "Value": "Pass (sender SPF authorized) identity=mailfrom; client-ip=192.0.2.1;"
                                                       " helo=mail.customer.example; envelope-from=bounces@mail.customer.example;"}],
         "Attachments": [{"Name": "a.pdf", "Content": "JVBER..."}], "HtmlBody": "<p>secret html</p>"}
    m.update(over)
    return json.dumps(m).encode()


class TestInbound(unittest.TestCase):
    def test_a_customer_email_is_stored_with_only_what_a_reply_needs(self):
        ingest = Ingest()
        self.assertEqual(EmailHandler(["postmark:s3cret-one"], ingest).handle(AUTH, mail()), (200, "ok"))
        (kind, ext, valid, payload, channel), = ingest.rows
        self.assertEqual((kind, ext, valid, channel), ("email", "a8c1040e-db1f-4e9b-a5a6-9f1c3c0b1f21", True, INBOX))
        self.assertEqual(payload, {"email": {"from": "salem@customer.example", "name": "سالم أحمد", "subject": "سؤال عن الدوام",
                                             "text": "متى تفتحون يوم الجمعة؟", "message_id": "<CAF123@mail.customer.example>",
                                             "spam": False, "authenticated": True, "attachments": 1}})
        self.assertNotIn("html", json.dumps(payload))                 # neither the HTML nor the attachments are kept
        self.assertEqual(EmailHandler(["postmark:s3cret-one"], ingest).handle(AUTH, mail())[0], 200)   # a redelivery
        self.assertEqual(len(ingest.rows), 1)

    def test_authentication_comes_before_reading_and_refusals_store_nothing(self):
        ingest = Ingest()
        h = EmailHandler(["old:pw", "postmark:s3cret-one"], ingest)
        bad = {**AUTH, "Authorization": "Basic " + base64.b64encode(b"postmark:wrong").decode()}
        self.assertEqual(h.handle(bad, b"not json")[0], 401)                      # retried by the provider: fixable
        self.assertEqual(h.handle({**AUTH, "Authorization": "Bearer x"}, mail())[0], 401)
        self.assertEqual(EmailHandler([], ingest).handle(AUTH, mail())[0], 401)   # no secret configured: nobody
        self.assertEqual(h.handle(AUTH, b"x" * (EMAIL_MAX_BYTES + 1))[0], 403)
        self.assertEqual(h.handle(AUTH, b"[1]")[0], 403)
        self.assertEqual(h.handle(AUTH, mail(OriginalRecipient="other@inbound.hermes.example"))[0], 403)  # unknown inbox
        self.assertEqual(h.handle(AUTH, mail(MessageID="../x"))[0], 403)
        self.assertEqual(ingest.rows, [])
        self.assertEqual((h.counters.rejected_auth, h.counters.refused), (2, 4))

    def test_automated_mail_is_acknowledged_and_never_stored(self):
        ingest = Ingest()
        h = EmailHandler(["postmark:s3cret-one"], ingest)
        for over in ({"Headers": [{"Name": "Auto-Submitted", "Value": "auto-replied"}]},
                     {"Headers": [{"Name": "Precedence", "Value": "bulk"}]},
                     {"Headers": [{"Name": "List-Unsubscribe", "Value": "<mailto:u@x.example>"}]},
                     {"FromFull": {"Email": "MAILER-DAEMON@mx.example", "Name": ""}},
                     {"FromFull": {"Email": "no-reply@shop.example", "Name": ""}},
                     {"FromFull": {"Email": INBOX, "Name": ""}},                   # our own address: a loop
                     {"FromFull": {"Email": "not an address", "Name": ""}}):
            with self.subTest(over):
                self.assertEqual(h.handle(AUTH, mail(**over)), (200, "ignored"))
        self.assertEqual(ingest.rows, [])

    def test_parsing(self):
        self.assertEqual(address("Shop <A.B@X.Example>"), "a.b@x.example")
        self.assertEqual(address("a@b.example>\r\nBcc: c@d.example"), "")
        self.assertEqual(strip_quoted("شكرًا\r\n> قديم\nجديد\n\nفي الثلاثاء، كتب المتجر:\nالقديم"), "شكرًا\nجديد")
        _, _, item, auto = parse(json.loads(mail(StrippedTextReply="Is it open?",
                                                 Headers=[{"Name": "X-Spam-Status", "Value": "Yes, score=7"},
                                                          {"Name": "Message-ID", "Value": "<bad id with spaces>"}])))
        self.assertEqual((item["email"]["text"], item["email"]["spam"], item["email"]["message_id"], auto), ("Is it open?", True, None, False))


class TestReviewFindings(unittest.TestCase):
    """The review of 2026-10-02: loops, forged senders, a sender's own spam verdict, invisible characters."""

    def test_our_own_mail_coming_back_is_never_stored(self):
        ingest = Ingest()
        h = EmailHandler(["postmark:s3cret-one"], ingest, own=frozenset({"notify@hermes.example", "info@shop.example"}))
        for over in ({"FromFull": {"Email": "Notify@Hermes.example", "Name": "Hermes"}},     # a notice forwarded back in
                     {"FromFull": {"Email": "info@shop.example", "Name": "Shop"}},          # a reply forwarded back in
                     {"Headers": [{"Name": "Return-Path", "Value": "<>"}]}):                 # a bounce
            with self.subTest(over):
                self.assertEqual(h.handle(AUTH, mail(**over)), (200, "ignored"))
        self.assertEqual(ingest.rows, [])

    def test_only_a_vouched_for_sender_is_authenticated(self):
        def auth(headers, sender="salem@customer.example"):
            return parse(json.loads(mail(Headers=headers, FromFull={"Email": sender, "Name": ""})))[2]["email"]["authenticated"]
        spf = lambda env, r="Pass": {"Name": "Received-SPF", "Value": f"{r} (x) identity=mailfrom; envelope-from={env};"}  # noqa: E731
        self.assertTrue(auth([spf("bounce@customer.example")]))
        self.assertTrue(auth([{"Name": "Authentication-Results", "Value": "mx.p; dkim=pass header.d=customer.example; spf=none"}]))
        self.assertFalse(auth([spf("attacker@evil.example")]))                # SPF passed for another domain: forged From
        self.assertFalse(auth([spf("bounce@customer.example", "Fail")]))
        self.assertFalse(auth([{"Name": "Authentication-Results", "Value": "mx.p; dkim=pass header.d=evil.example"}]))
        self.assertFalse(auth([]))

    def test_any_spam_verdict_counts_and_subjects_lose_invisible_characters(self):
        item = parse(json.loads(mail(Headers=[{"Name": "X-Spam-Status", "Value": "Yes, score=8"},
                                              {"Name": "X-Spam-Status", "Value": "No"}],
                                     Subject="\u200fاستفسار\u200c عن\u0007 الدوام")))[2]["email"]
        self.assertTrue(item["spam"])
        self.assertEqual(item["subject"], "استفسار عن الدوام")
        self.assertTrue(reply_subject("\u200fاستفسار").isprintable())


class TestReply(unittest.TestCase):
    ROW = {"customer_id": "c", "payload": {"channel": "email", "account_id": INBOX, "to": "salem@customer.example",
                                           "subject": "Re: سؤال عن الدوام", "body": "نفتح من 8 إلى 10", "in_reply_to": "x",
                                           "message_id": "<CAF123@mail.customer.example>", "topic": "hours"}}

    def post(self, status=200, data=None):
        calls = []

        def p(url, body, headers, timeout):
            calls.append((url, body, headers))
            return status, data if data is not None else {"MessageID": "pm-1", "ErrorCode": 0, "Message": "OK"}
        return p, calls

    def test_the_reply_joins_the_customer_thread_from_the_configured_sender(self):
        post, calls = self.post()
        a = PostmarkEmailAdapter(post, "server-token", {INBOX: "المتجر <info@shop.example>"}.get)
        self.assertEqual(ReplyRouter(None, None, a).send(self.ROW), "pm-1")
        (url, body, headers), = calls
        self.assertEqual(url, "https://api.postmarkapp.com/email")
        self.assertEqual(headers, {"X-Postmark-Server-Token": "server-token"})
        self.assertEqual(body, {"From": "المتجر <info@shop.example>", "To": "salem@customer.example", "ReplyTo": INBOX,
                                "Subject": "Re: سؤال عن الدوام", "TextBody": "نفتح من 8 إلى 10", "MessageStream": "outbound",
                                "Metadata": {"hermes_channel": INBOX},
                                "Headers": [{"Name": "Auto-Submitted", "Value": "auto-replied"},
                                            {"Name": "In-Reply-To", "Value": "<CAF123@mail.customer.example>"},
                                            {"Name": "References", "Value": "<CAF123@mail.customer.example>"}]})

    def test_failures_are_classified_where_they_happened(self):
        def outcome(adapter, row=None):
            try:
                adapter.send(row or self.ROW)
                return SENT
            except BaseException as exc:          # noqa: BLE001
                return classify(exc).outcome
        senders = {INBOX: "info@shop.example"}.get
        self.assertEqual(outcome(PostmarkEmailAdapter(self.post()[0], "t", lambda a: None)), FAILED_BEFORE_SEND)   # NO_SENDER
        self.assertEqual(outcome(PostmarkEmailAdapter(self.post()[0], "", senders)), FAILED_BEFORE_SEND)
        self.assertEqual(outcome(PostmarkEmailAdapter(self.post(422, {"ErrorCode": 406})[0], "t", senders)), FAILED_PERMANENT)
        self.assertEqual(outcome(PostmarkEmailAdapter(self.post(200, {"ErrorCode": 300})[0], "t", senders)), FAILED_PERMANENT)
        self.assertEqual(outcome(PostmarkEmailAdapter(self.post(500, {})[0], "t", senders)), AMBIGUOUS)
        self.assertEqual(outcome(PostmarkEmailAdapter(self.post(200, {"ErrorCode": 0})[0], "t", senders)), AMBIGUOUS)
        for bad in ({"to": "a@b.example\r\nBcc: x@y.example"}, {"to": "Salem <s@c.example>"}, {"subject": "x\r\nBcc: y"},
                    {"message_id": "<a>\r\nBcc: <b>"}, {"account_id": "../x"}, {"body": ""}):
            with self.subTest(bad):
                self.assertEqual(outcome(PostmarkEmailAdapter(self.post()[0], "t", senders),
                                         {**self.ROW, "payload": {**self.ROW["payload"], **bad}}), FAILED_BEFORE_SEND)
        with self.assertRaises(BeforeSend):
            ReplyRouter(None, None, None).send(self.ROW)                       # no email adapter configured

    def test_live_configuration(self):
        env = {"HERMES_GRAPH_TOKEN": "tok", "HERMES_POSTMARK_TOKEN": "pm-token",
               "HERMES_EMAIL_SENDERS": json.dumps({INBOX: "المتجر <info@shop.example>"})}
        self.assertIn("reply.send", live_adapters(env))
        self.assertEqual(email_senders(env["HERMES_EMAIL_SENDERS"]), {INBOX: "المتجر <info@shop.example>"})
        for raw in ('{"x": "info@shop.example"}', json.dumps({INBOX: "a <b@c.example>\r\nBcc: d@e.example"}), "[]"):
            with self.subTest(raw), self.assertRaises(RuntimeError):
                email_senders(raw)
        with self.assertRaises(RuntimeError):
            live_adapters({**env, "HERMES_POSTMARK_TOKEN": "has space"})
        calls = []

        class Api:
            def request(self, method, url, body, headers):
                calls.append(url)
                return 200, {}
        post = graph_post(Api())
        post("https://api.postmarkapp.com/email", {}, {}, 5)
        with self.assertRaises(BeforeSend):
            post("https://api.postmarkapp.com/email/batch", {}, {}, 5)
        self.assertEqual(calls, ["https://api.postmarkapp.com/email"])


class TestWorkerPieces(unittest.TestCase):
    def test_content_and_subject(self):
        self.assertEqual(email_content({"from": "s@c.example", "name": "سالم", "subject": "الدوام", "text": "متى؟", "attachments": 0}),
                         ("text", "الدوام\nمتى؟", "s@c.example", "سالم · s@c.example"))
        self.assertEqual(email_content({"from": "s@c.example", "subject": "فاتورة", "text": "", "attachments": 2})[0], "document")
        self.assertEqual(email_content({"from": "s@c.example"})[0], "unsupported")
        self.assertEqual(reply_subject("الدوام"), "Re: الدوام")
        self.assertEqual(reply_subject("RE: الدوام"), "RE: الدوام")
        self.assertEqual(reply_subject(""), "")


if __name__ == "__main__":
    unittest.main()
