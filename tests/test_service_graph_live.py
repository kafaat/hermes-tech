"""The live Graph sender (HERMES_GRAPH=live): off unless configured explicitly, graph.facebook.com only through the
pinned path, a failure before the first byte is a clean failure, and anything after it is ambiguous and never resent
automatically. The provider is faked at the socket boundary (the connector); nothing here reaches the network."""
import os, sys, unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.crawler import ApiClient, NotSent  # noqa: E402
from service.dispatcher import AMBIGUOUS, FAILED_BEFORE_SEND, FAILED_PERMANENT, SENT, Dispatcher, WhatsAppCloudAdapter  # noqa: E402
from service.worker import adapters_for, graph_post, live_adapters  # noqa: E402

PUBLIC, TOKEN = "157.240.1.1", "EAAG-system-user-token"
ROW = {"id": 41, "topic": "reply.send", "customer_id": "c1",
       "payload": {"phone_number_id": "106540352242922", "to": "967700000001", "body": "نفتح 9 صباحًا"}}


def http(status, body=b"{}"):
    return f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n\r\n".encode() + body


class Db:
    def __init__(self):
        self.finished = []

    def claim(self, outbox_id):
        return "tok"

    def row(self, outbox_id):
        return ROW

    def finish(self, outbox_id, token, outcome, provider_ref=None, error=None):
        self.finished.append((outcome, provider_ref, error))
        return True

    def reconcile(self, outbox_id, ref):
        return False


def dispatch(connector, dns=None):
    sent = []

    def recording(plan, request):
        sent.append((plan["connect_to"], plan["host"], request))
        return connector(plan, request)
    api = ApiClient({"graph.facebook.com"}, lambda h: (dns or {"graph.facebook.com": [PUBLIC]})[h], recording)
    db = Db()
    adapter = WhatsAppCloudAdapter(graph_post(api), lambda c: TOKEN)
    return Dispatcher(db, {"reply.send": adapter}).run_once(ROW["id"]), db.finished, sent


class TestLiveSend(unittest.TestCase):
    def test_a_reply_goes_to_the_graph_api_with_the_token_and_returns_the_wamid(self):
        outcome, finished, sent = dispatch(lambda p, r: http(200, b'{"messages":[{"id":"wamid.HBgM"}]}'))
        self.assertEqual((outcome, finished[0][1]), (SENT, "wamid.HBgM"))
        addr, host, req = sent[0]
        self.assertEqual((addr, host), (PUBLIC, "graph.facebook.com"))
        self.assertTrue(req.startswith(b"POST /v21.0/106540352242922/messages HTTP/1.0\r\nHost: graph.facebook.com\r\n"))
        self.assertIn(f"Authorization: Bearer {TOKEN}\r\n".encode(), req)
        self.assertIn('"to":"967700000001"'.encode(), req)

    def test_refusals_before_the_first_byte_are_clean_failures(self):
        for name, connector, dns in (
                ("private address", lambda p, r: http(200), {"graph.facebook.com": ["10.0.0.1"]}),
                ("no such name", lambda p, r: http(200), {"graph.facebook.com": []}),
                ("connection refused", mock.Mock(side_effect=NotSent("ConnectionRefusedError")), None),
                ("bad certificate", mock.Mock(side_effect=NotSent("SSLCertVerificationError")), None)):
            with self.subTest(name):
                outcome, finished, _ = dispatch(connector, dns)
                self.assertEqual(outcome, FAILED_BEFORE_SEND)
                self.assertNotIn(TOKEN, str(finished))

    def test_after_the_first_byte_the_outcome_is_ambiguous_never_a_retry(self):
        for name, connector in (("read timeout", mock.Mock(side_effect=TimeoutError("timed out"))),
                                ("reset after write", mock.Mock(side_effect=ConnectionResetError())),
                                ("server error", lambda p, r: http(500)),
                                ("accepted without an id", lambda p, r: http(200, b'{"messages":[]}'))):
            with self.subTest(name):
                outcome, finished, sent = dispatch(connector)
                self.assertEqual((outcome, len(sent)), (AMBIGUOUS, 1))
                self.assertNotIn(TOKEN, str(finished))

    def test_meta_refusals_map_to_permanent_or_retry_later(self):
        outcome, finished, _ = dispatch(lambda p, r: http(400, b'{"error":{"code":131026,"message":"x"}}'))
        self.assertEqual((outcome, finished[0][2]), (FAILED_PERMANENT, "HTTP_400:131026"))
        self.assertEqual(dispatch(lambda p, r: http(429))[0], FAILED_BEFORE_SEND)

    def test_nothing_from_the_payload_shapes_the_url(self):
        post = graph_post(ApiClient({"graph.facebook.com"}, lambda h: [PUBLIC], mock.Mock()))
        for pnid in ("pn-e2e-staging", "1065403522/../me", "106540352242922?x=1"):
            with self.subTest(pnid), self.assertRaises(Exception) as e:
                WhatsAppCloudAdapter(post, lambda c: TOKEN).send({**ROW, "payload": {**ROW["payload"], "phone_number_id": pnid}})
            self.assertEqual(str(e.exception), "BAD_TARGET")


class TestMessengerAndInstagramSend(unittest.TestCase):
    def run_row(self, payload, connector, tokens=None):
        sent = []

        def recording(plan, request):
            sent.append((plan["host"], request))
            return connector(plan, request)
        api = ApiClient({"graph.facebook.com", "graph.instagram.com"}, lambda h: [PUBLIC], recording)
        from service.dispatcher import MetaMessagingAdapter
        adapter = MetaMessagingAdapter(graph_post(api), ({"1061234567": "PAGE-T", "178414000001": "IG-T"} if tokens is None else tokens).get)
        db = Db()
        db.row = lambda outbox_id: {"id": 9, "topic": "reply.send", "customer_id": "c1", "payload": payload}
        return Dispatcher(db, {"reply.send": adapter}).run_once(9), db.finished, sent

    def test_messenger_reply_goes_to_the_page_with_its_token(self):
        payload = {"channel": "facebook_page", "account_id": "1061234567", "to": "2551234567", "body": "نفتح 9", "in_reply_to": "m_A"}
        outcome, finished, sent = self.run_row(payload, lambda p, r: http(200, b'{"recipient_id":"2551234567","message_id":"m_OUT1"}'))
        self.assertEqual((outcome, finished[0][1]), (SENT, "m_OUT1"))
        host, req = sent[0]
        self.assertEqual(host, "graph.facebook.com")
        self.assertTrue(req.startswith(b"POST /v21.0/1061234567/messages HTTP/1.0"))
        self.assertIn(b"Authorization: Bearer PAGE-T", req)
        self.assertIn('"recipient":{"id":"2551234567"}'.encode(), req)
        self.assertIn(b'"messaging_type":"RESPONSE"', req)

    def test_instagram_reply_goes_to_graph_instagram_and_a_missing_token_is_a_clean_failure(self):
        payload = {"channel": "instagram_business", "account_id": "178414000001", "to": "9912345678", "body": "x", "in_reply_to": "a"}
        outcome, _, sent = self.run_row(payload, lambda p, r: http(200, b'{"message_id":"ig_1"}'))
        self.assertEqual((outcome, sent[0][0]), (SENT, "graph.instagram.com"))
        outcome, finished, sent = self.run_row(payload, lambda p, r: http(200), tokens={})
        self.assertEqual((outcome, finished[0][2], sent), (FAILED_BEFORE_SEND, "BeforeSend: NO_ACCOUNT_TOKEN", []))

    def test_ids_from_the_payload_cannot_shape_the_url(self):
        for bad in ({"account_id": "106/../me"}, {"to": "25?x=1"}, {"channel": "telegram"}):
            payload = {"channel": "facebook_page", "account_id": "1061234567", "to": "2551234567", "body": "x", **bad}
            outcome, finished, sent = self.run_row(payload, lambda p, r: http(200))
            self.assertEqual((outcome, sent), (FAILED_BEFORE_SEND, []), bad)

    def test_account_tokens_are_validated_at_start(self):
        from service.worker import account_tokens
        self.assertEqual(account_tokens('{"1061234567": "EAAG-page"}'), {"1061234567": "EAAG-page"})
        for raw in ("[1]", '{"page": "t"}', '{"1061234567": "a b"}', "not json"):
            with self.subTest(raw), self.assertRaises(RuntimeError):
                account_tokens(raw)


class TestPublishing(unittest.TestCase):
    def publish(self, payload, answers, tokens=None):
        sent, answers = [], list(answers)

        def connector(plan, request):
            sent.append((plan["host"], request.split(b" ")[1].decode()))
            a = answers.pop(0)
            if isinstance(a, BaseException):
                raise a
            return a
        api = ApiClient({"graph.facebook.com", "graph.instagram.com"}, lambda h: [PUBLIC], connector)
        from service.dispatcher import MetaPublishAdapter
        adapter = MetaPublishAdapter(graph_post(api), ({"1061234567": "PAGE-T", "178414000001": "IG-T"} if tokens is None else tokens).get)
        db = Db()
        db.row = lambda outbox_id: {"id": 11, "topic": "content.publish", "customer_id": "c1", "payload": payload}
        return Dispatcher(db, {"content.publish": adapter}).run_once(11), db.finished, sent

    def test_a_facebook_text_post_goes_to_the_feed_and_an_image_post_to_photos(self):
        text = {"content_id": "c", "body": "عرض الجمعة", "platform": "facebook", "account_id": "1061234567", "image_url": None, "media_ids": []}
        outcome, finished, sent = self.publish(text, [http(200, b'{"id":"106_1"}')])
        self.assertEqual((outcome, finished[0][1], sent[0]), (SENT, "106_1", ("graph.facebook.com", "/v21.0/1061234567/feed")))
        photo = {**text, "image_url": "https://cdn.example/offer.jpg"}
        outcome, finished, sent = self.publish(photo, [http(200, b'{"id":"9","post_id":"106_2"}')])
        self.assertEqual((outcome, finished[0][1], sent[0][1]), (SENT, "106_2", "/v21.0/1061234567/photos"))

    def test_instagram_publishes_in_two_steps_and_a_failed_container_publishes_nothing(self):
        post = {"content_id": "c", "body": "عرض", "platform": "instagram", "account_id": "178414000001",
                "image_url": "https://cdn.example/offer.jpg", "media_ids": []}
        outcome, finished, sent = self.publish(post, [http(200, b'{"id":"cont1"}'), http(200, b'{"id":"ig_media_1"}')])
        self.assertEqual((outcome, finished[0][1], [p for _, p in sent]),
                         (SENT, "ig_media_1", ["/v21.0/178414000001/media", "/v21.0/178414000001/media_publish"]))
        outcome, finished, sent = self.publish(post, [TimeoutError("read")])
        self.assertEqual((outcome, len(sent)), (FAILED_BEFORE_SEND, 1))              # only the container was attempted
        outcome, finished, sent = self.publish(post, [http(200, b'{"id":"cont2"}'), TimeoutError("read")])
        self.assertEqual(outcome, AMBIGUOUS)                                         # the publish itself: never retried

    def test_bad_targets_missing_tokens_and_instagram_without_image_fail_before_sending(self):
        base = {"content_id": "c", "body": "x", "platform": "facebook", "account_id": "1061234567", "image_url": None, "media_ids": []}
        for bad, tokens in (({"account_id": "106/../me"}, None), ({"platform": "tiktok"}, None), ({"image_url": "http://x/a.jpg"}, None),
                            ({"platform": "instagram", "account_id": "178414000001"}, None), ({}, {})):
            with self.subTest(bad=bad, tokens=tokens):
                outcome, _, sent = self.publish({**base, **bad}, [http(200)], tokens)
                self.assertEqual((outcome, sent), (FAILED_BEFORE_SEND, []))


class TestModes(unittest.TestCase):
    def test_live_needs_a_token_and_reaches_only_graph(self):
        with self.assertRaises(RuntimeError):
            live_adapters({"HERMES_GRAPH": "live"})
        with self.assertRaises(RuntimeError):
            live_adapters({"HERMES_GRAPH": "live", "HERMES_GRAPH_TOKEN": "a b"})
        with self.assertRaises(RuntimeError):
            live_adapters({"HERMES_GRAPH": "live", "HERMES_GRAPH_TOKEN": TOKEN, "HERMES_SIM_SEND_DELAY_SECONDS": "5"})
        a = live_adapters({"HERMES_GRAPH": "live", "HERMES_GRAPH_TOKEN": TOKEN})
        self.assertEqual(a["notify.owner"].send({"id": 7}), "portal.7")             # the owner reads it in the portal
        self.assertEqual(a["reply.send"].whatsapp.token_for_customer("any"), TOKEN)

    def test_only_simulate_or_live_and_the_staging_login_stays_simulate_only(self):
        for mode in (None, "", "Live", "real"):
            with self.subTest(mode), self.assertRaises(RuntimeError):
                adapters_for({} if mode is None else {"HERMES_GRAPH": mode})
        with mock.patch.dict(os.environ, {"HERMES_GRAPH": "simulate", "HERMES_SIM_SEND_DELAY_SECONDS": ""}):
            self.assertIn("reply.send", adapters_for(os.environ))
        from service.portal import Portal
        live = Portal(None, "s" * 40, staging_login_code="c", staging_owner_id="00000000-0000-0000-0000-0000000e2e0a",
                      simulate=False)
        self.assertFalse(live.staging)


if __name__ == "__main__":
    unittest.main()
