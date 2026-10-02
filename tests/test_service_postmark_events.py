"""Postmark's delivery events (spec 28.28): authenticated before they are read; a delivery, a bounce that means "not
delivered" and a spam complaint are stored as a status of the business the reply came from; anything else is
acknowledged and not stored."""
import base64, json, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.email_inbound import EmailEventHandler, event_from  # noqa: E402

INBOX = "shop-1@inbound.hermes.example"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"postmark:s3cret").decode()}
MID = "0a129aee-e1cd-480d-b08d-4f48548ff48d"


class Ingest:
    def __init__(self):
        self.rows = []

    def insert_event(self, kind, ext, valid, payload, channel):
        if any(r[1] == ext for r in self.rows):
            return "duplicate"
        self.rows.append((kind, ext, valid, payload, channel))
        return "inserted"


def ev(**over):
    e = {"RecordType": "Bounce", "Type": "HardBounce", "TypeCode": 1, "MessageID": MID, "Email": "salem@customer.example",
         "Metadata": {"hermes_channel": INBOX}, "Description": "The server was unable to deliver your message"}
    e.update(over)
    return e


class TestEvents(unittest.TestCase):
    def test_delivery_bounce_and_complaint_become_statuses_of_the_business(self):
        self.assertEqual(event_from(ev(RecordType="Delivery")),
                         (f"status:{MID}:delivered", INBOX, {"status": {"id": MID, "status": "delivered", "detail": "Delivery"}}))
        self.assertEqual(event_from(ev()), (f"status:{MID}:failed", INBOX, {"status": {"id": MID, "status": "failed", "detail": "HardBounce"}}))
        self.assertEqual(event_from(ev(RecordType="SpamComplaint", Type="SpamComplaint"))[0], f"status:{MID}:complained")
        stored = json.dumps(event_from(ev()))
        self.assertNotIn("salem", stored)                               # the recipient and the description are not kept

    def test_what_is_not_ours_to_act_on_is_not_stored(self):
        for e in (ev(Type="AutoResponder"), ev(Type="Transient"), ev(RecordType="Open"), ev(RecordType="Click"),
                  ev(Metadata={}), ev(Metadata={"hermes_channel": "not an address"}), ev(MessageID="../x"), ev(Metadata="x")):
            with self.subTest(e):
                self.assertIsNone(event_from(e))

    def test_the_handler_authenticates_first_and_stores_once(self):
        ingest = Ingest()
        h = EmailEventHandler(["postmark:s3cret"], ingest)
        self.assertEqual(h.handle({"Authorization": "Basic " + base64.b64encode(b"postmark:no").decode()}, b"{}")[0], 401)
        self.assertEqual(h.handle(AUTH, b"[]")[0], 403)
        self.assertEqual(h.handle(AUTH, json.dumps(ev(RecordType="Open")).encode()), (200, "ignored"))
        self.assertEqual(h.handle(AUTH, json.dumps(ev()).encode()), (200, "ok"))
        self.assertEqual(h.handle(AUTH, json.dumps(ev()).encode()), (200, "ok"))       # a retry: one row
        self.assertEqual([(k, e, c) for k, e, _, _, c in ingest.rows], [("email", f"status:{MID}:failed", INBOX)])
        self.assertEqual((h.counters.inserted, h.counters.duplicates, h.counters.rejected_auth), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
