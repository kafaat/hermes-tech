"""The owner's escalations by email (spec 28.27): to the owners' verified addresses, from the platform's sender, the
reason and a link to the portal only, never the customer's message; without an address the notice stays in the portal."""
import json, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.dispatcher import AMBIGUOUS, FAILED_BEFORE_SEND, FAILED_PERMANENT, classify  # noqa: E402
from service.dispatcher import OwnerEmailNotice  # noqa: E402
from service.worker import live_adapters  # noqa: E402

PORTAL = "https://hermes.example/portal"
SENDER = "Hermes <notify@hermes.example>"


def post(status=200, data=None):
    calls = []

    def p(url, body, headers, timeout):
        calls.append((url, body, headers))
        return status, data if data is not None else {"MessageID": "pm-n1", "ErrorCode": 0}
    return p, calls


def row(**payload):
    return {"id": 7, "customer_id": "c", "payload": {"event": "wamid.X", "reason": "complaint", "categories": ["late"],
                                                     "sla_minutes": 30, **payload}}


class TestOwnerEmailNotice(unittest.TestCase):
    def test_the_notice_names_the_reason_and_links_to_the_portal_never_the_message(self):
        p, calls = post()
        n = OwnerEmailNotice(p, "server-token", SENDER, PORTAL)
        self.assertEqual(n.send(row(notify_to=["owner@shop.example", "partner@shop.example"])), "pm-n1")
        (url, body, headers), = calls
        self.assertEqual((url, headers), ("https://api.postmarkapp.com/email", {"X-Postmark-Server-Token": "server-token"}))
        self.assertEqual((body["From"], body["To"], body["MessageStream"]), (SENDER, "owner@shop.example, partner@shop.example", "outbound"))
        self.assertIn("شكوى من عميل", body["Subject"])
        self.assertIn(PORTAL, body["TextBody"])
        self.assertNotIn("wamid", json.dumps(body))                     # no provider ids, no customer text: a reason and a link
        p, calls = post()
        OwnerEmailNotice(p, "t", SENDER, PORTAL).send(row(reason="non_text:audio", notify_to=["o@s.example"]))
        self.assertIn("بلا نص", calls[0][1]["Subject"])

    def test_without_an_address_the_notice_stays_in_the_portal(self):
        p, calls = post()
        self.assertEqual(OwnerEmailNotice(p, "t", SENDER, PORTAL).send(row()), "portal.7")
        self.assertEqual(calls, [])

    def test_failures_are_classified_where_they_happened(self):
        def outcome(n, r):
            try:
                n.send(r)
                return "sent"
            except BaseException as exc:          # noqa: BLE001
                return classify(exc).outcome
        ok = row(notify_to=["o@s.example"])
        self.assertEqual(outcome(OwnerEmailNotice(post()[0], "", SENDER, PORTAL), ok), FAILED_BEFORE_SEND)
        self.assertEqual(outcome(OwnerEmailNotice(post()[0], "t", SENDER, PORTAL), row(notify_to=["o@s.example\r\nBcc: x@y.example"])),
                         FAILED_BEFORE_SEND)
        self.assertEqual(outcome(OwnerEmailNotice(post()[0], "t", SENDER, PORTAL), row(notify_to=[f"o{i}@s.example" for i in range(11)])),
                         FAILED_BEFORE_SEND)
        self.assertEqual(outcome(OwnerEmailNotice(post(422, {"ErrorCode": 406})[0], "t", SENDER, PORTAL), ok), FAILED_PERMANENT)
        self.assertEqual(outcome(OwnerEmailNotice(post(503, {})[0], "t", SENDER, PORTAL), ok), AMBIGUOUS)

    def test_live_configuration_all_or_nothing(self):
        env = {"HERMES_GRAPH_TOKEN": "tok", "HERMES_POSTMARK_TOKEN": "pm", "HERMES_NOTIFY_SENDER": SENDER, "HERMES_PORTAL_URL": PORTAL}
        self.assertIsInstance(live_adapters(env)["notify.owner"], OwnerEmailNotice)
        self.assertNotIsInstance(live_adapters({"HERMES_GRAPH_TOKEN": "tok"})["notify.owner"], OwnerEmailNotice)
        for bad in ({"HERMES_POSTMARK_TOKEN": ""}, {"HERMES_PORTAL_URL": "http://hermes.example/portal"},
                    {"HERMES_NOTIFY_SENDER": "a <b@c.example>\r\nBcc: d@e.example"}, {"HERMES_NOTIFY_SENDER": ""}):
            with self.subTest(bad), self.assertRaises(RuntimeError):
                live_adapters({**env, **bad})


if __name__ == "__main__":
    unittest.main()
