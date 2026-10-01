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
        self.assertEqual(a["reply.send"].token_for_customer("any"), TOKEN)

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
