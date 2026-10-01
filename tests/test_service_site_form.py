"""The contact form of a customer's site: only an active site key stores anything; the trap field, the size and the
rate limits stop bots without telling them; the visitor gets a thank-you page and no automatic reply."""
import sys, unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.site_form import FORM_MAX_BYTES, FormHandler, Limiter, contact_form  # noqa: E402

KEY = "sitekeyAAAAAAAAAAAAAAAAAAAAA01"
FORM = {"Content-Type": "application/x-www-form-urlencoded", "X-Forwarded-For": "203.0.113.7, 10.0.0.1"}


class Ingest:
    def __init__(self, active=(KEY,)):
        self.active, self.rows = set(active), []

    def channel_active(self, kind, key):
        return kind == "site_form" and key in self.active

    def insert_event(self, kind, ext, valid, payload, channel):
        self.rows.append((kind, ext, valid, payload, channel))
        return "inserted"


def body(**f):
    return urlencode({"message": "عندكم توصيل لعدن؟", "name": "سالم", "phone": "+967 712 345 678", **f}).encode()


class TestForm(unittest.TestCase):
    def test_a_message_from_an_active_site_is_stored_for_the_owner_and_thanked(self):
        ingest = Ingest()
        status, headers, page = FormHandler(ingest).handle(KEY, FORM, body())
        self.assertEqual(status, 200)
        self.assertIn("وصلت رسالتك", page.decode())
        self.assertIn(("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"), headers)
        (kind, ext, valid, payload, channel), = ingest.rows
        self.assertEqual((kind, valid, channel), ("site_form", True, KEY))
        self.assertTrue(ext.startswith("form:"))
        self.assertEqual(payload, {"form": {"message": "عندكم توصيل لعدن؟", "name": "سالم", "phone": "+967 712 345 678"}})

    def test_unknown_keys_bots_and_bad_input_store_nothing(self):
        ingest = Ingest()
        h = FormHandler(ingest)
        self.assertEqual(h.handle("unknownkeyBBBBBBBBBBBBBBBBBBB0", FORM, body())[0], 404)
        self.assertEqual(h.handle("../portal", FORM, body())[0], 404)
        self.assertEqual(h.handle(KEY, FORM, body(website="http://spam.example"))[0], 200)      # the trap: same answer
        self.assertEqual(h.handle(KEY, FORM, body(message="  "))[0], 400)
        self.assertEqual(h.handle(KEY, FORM, body(phone="call me"))[0], 400)
        self.assertEqual(h.handle(KEY, {"Content-Type": "application/json"}, b"{}")[0], 415)
        self.assertEqual(h.handle(KEY, FORM, b"m=" + b"x" * FORM_MAX_BYTES)[0], 413)
        self.assertEqual(ingest.rows, [])

    def test_one_address_is_limited_per_site(self):
        ingest = Ingest()
        h = FormHandler(ingest, Limiter(limit=2, window=600, now=lambda: 1000.0))
        codes = [h.handle(KEY, FORM, body())[0] for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
        self.assertEqual(h.handle(KEY, {**FORM, "X-Forwarded-For": "198.51.100.9"}, body())[0], 200)   # another visitor
        self.assertEqual(len(ingest.rows), 3)

    def test_the_form_for_a_site_template_posts_to_this_service_and_escapes(self):
        html = contact_form("https://hermes-app.example", KEY)
        self.assertIn(f'action="https://hermes-app.example/forms/{KEY}"', html)
        self.assertIn('name="website"', html)
        self.assertNotIn("<script", html)
        for bad in (("http://hermes-app.example", KEY), ("https://hermes-app.example", "short"), ('https://x"onload=1', KEY)):
            with self.subTest(bad), self.assertRaises(ValueError):
                contact_form(*bad)


if __name__ == "__main__":
    unittest.main()
