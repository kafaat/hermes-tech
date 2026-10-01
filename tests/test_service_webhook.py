import hashlib, hmac, json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.webhook import Handler, signature_ok, verify_subscription, events_from, MAX_BODY_BYTES

SECRET = b"app-secret-for-tests-only"


def sign(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


class FakeIngest:
    def __init__(self):
        self.rows, self.parsed_before_verify = {}, False

    def insert_event(self, kind, ext, valid, payload, channel):
        if (kind, ext) in self.rows:
            return "duplicate"
        self.rows[(kind, ext)] = (valid, payload, channel)
        return "inserted"


WA = {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
    "metadata": {"phone_number_id": "pn-A"},
    "messages": [{"id": "wamid.1", "from": "967771234567", "text": {"body": "كم السعر؟"}},
                 {"id": "wamid.2", "from": "967771234567", "text": {"body": "وين موقعكم"}}],
    "statuses": [{"id": "wamid.out.9", "status": "delivered"}]}}]}]}


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.ingest = FakeIngest()
        self.h = Handler([SECRET], self.ingest)

    def test_valid_signature_stores_one_event_per_item_with_the_addressed_channel(self):
        body = json.dumps(WA).encode()
        self.assertEqual(self.h.handle({"X-Hub-Signature-256": sign(body)}, body), (200, "ok"))
        self.assertEqual(set(self.ingest.rows), {("whatsapp_cloud", "wamid.1"), ("whatsapp_cloud", "wamid.2"),
                                                 ("whatsapp_cloud", "status:wamid.out.9:delivered")})
        self.assertTrue(all(ch == "pn-A" and valid for valid, _, ch in self.ingest.rows.values()))

    def test_bad_missing_or_malformed_signature_is_401_and_nothing_is_stored(self):
        body = json.dumps(WA).encode()
        for hdr in ({}, {"X-Hub-Signature-256": sign(body, b"other")}, {"X-Hub-Signature-256": "sha1=abc"},
                    {"X-Hub-Signature-256": "sha256=" + "z" * 64}, {"X-Hub-Signature-256": sign(body + b" ")}):
            self.assertEqual(self.h.handle(hdr, body)[0], 401)
        self.assertEqual(self.ingest.rows, {})
        self.assertEqual(self.h.counters.rejected_signatures, 5)

    def test_signature_is_checked_on_raw_bytes_before_parsing(self):
        garbage = b"\xff\xfe not json"
        self.assertEqual(self.h.handle({}, garbage)[0], 401)                       # unsigned garbage never reaches the parser
        self.assertEqual(self.h.counters.unparseable, 0)
        self.assertEqual(self.h.handle({"x-hub-signature-256": sign(garbage)}, garbage)[0], 400)
        reencoded = json.dumps(json.loads(json.dumps(WA).encode()), indent=2).encode()
        self.assertEqual(self.h.handle({"X-Hub-Signature-256": sign(json.dumps(WA).encode())}, reencoded)[0], 401)

    def test_redelivery_is_a_success_not_a_second_event(self):
        body = json.dumps(WA).encode()
        self.h.handle({"X-Hub-Signature-256": sign(body)}, body)
        self.assertEqual(self.h.handle({"X-Hub-Signature-256": sign(body)}, body), (200, "ok"))
        self.assertEqual(len(self.ingest.rows), 3)
        self.assertEqual(self.h.counters.duplicates, 3)

    def test_rotation_accepts_either_secret_and_oversize_is_refused(self):
        h = Handler([b"old-secret", SECRET], self.ingest)
        body = json.dumps(WA).encode()
        self.assertEqual(h.handle({"X-Hub-Signature-256": sign(body, b"old-secret")}, body)[0], 200)
        big = b"{" + b" " * MAX_BODY_BYTES + b"}"
        self.assertEqual(h.handle({"X-Hub-Signature-256": sign(big)}, big)[0], 413)

    def test_handler_never_chooses_a_tenant(self):
        body = json.dumps(WA).encode()
        self.h.handle({"X-Hub-Signature-256": sign(body)}, body)
        for _, payload, _ in self.ingest.rows.values():
            self.assertNotIn("customer_id", json.dumps(payload))

    def test_subscription_handshake(self):
        self.assertEqual(verify_subscription({"hub.mode": "subscribe", "hub.verify_token": "t0k", "hub.challenge": "42"}, "t0k"), (200, "42"))
        self.assertEqual(verify_subscription({"hub.mode": "subscribe", "hub.verify_token": "bad", "hub.challenge": "42"}, "t0k")[0], 403)

    def test_facebook_page_messages(self):
        p = {"object": "page", "entry": [{"id": "page-9", "messaging": [{"message": {"mid": "m_1", "text": "hi"}}]}]}
        self.assertEqual(events_from(p), [("facebook_page", "m_1", "page-9", {"messaging": {"message": {"mid": "m_1", "text": "hi"}}})])

    def test_constant_time_compare_used(self):
        src = (Path(__file__).resolve().parent.parent / "service/webhook.py").read_text()
        self.assertIn("hmac.compare_digest", src)
        self.assertFalse(signature_ok(b"x", sign(b"x")[:-1] + "0" if sign(b"x")[-1] != "0" else sign(b"x")[:-1] + "1", [SECRET]))


class TestMessengerAndInstagram(unittest.TestCase):
    def test_messenger_and_instagram_direct_are_stored_per_account_and_echoes_skipped(self):
        page = {"object": "page", "entry": [{"id": "1061234567", "messaging": [
            {"sender": {"id": "2551234567"}, "recipient": {"id": "1061234567"}, "message": {"mid": "m_A", "text": "متى تفتحون؟"}},
            {"sender": {"id": "1061234567"}, "recipient": {"id": "2551234567"}, "message": {"mid": "m_ECHO", "is_echo": True, "text": "x"}},
            {"sender": {"id": "2551234567"}, "delivery": {"mids": ["m_OUT"]}}]}]}
        ig = {"object": "instagram", "entry": [{"id": "178414000001", "messaging": [
            {"sender": {"id": "9912345678"}, "recipient": {"id": "178414000001"}, "message": {"mid": "aWdf1", "text": "السعر؟"}}]}]}
        self.assertEqual([e[:3] for e in events_from(page)], [("facebook_page", "m_A", "1061234567")])
        self.assertEqual([e[:3] for e in events_from(ig)], [("instagram_business", "aWdf1", "178414000001")])
        ingest = FakeIngest()
        body = json.dumps(ig).encode()
        self.assertEqual(Handler([SECRET], ingest).handle({"X-Hub-Signature-256": sign(body)}, body), (200, "ok"))
        self.assertIn(("instagram_business", "aWdf1"), ingest.rows)

    def test_an_unrecognised_instagram_payload_is_kept_unrouted_as_instagram(self):
        ingest = FakeIngest()
        body = json.dumps({"object": "instagram", "entry": [{"id": "1", "changes": []}]}).encode()
        Handler([SECRET], ingest).handle({"X-Hub-Signature-256": sign(body)}, body)
        (kind, _), = ingest.rows
        self.assertEqual(kind, "instagram_business")


if __name__ == "__main__":
    unittest.main()
