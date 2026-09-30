import io, logging, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.redact import install, redact_text, RedactingFilter


class TestRedact(unittest.TestCase):
    def setUp(self):
        self.buf = io.StringIO()
        self.log = logging.getLogger(f"t{id(self)}")
        self.log.propagate = False
        h = logging.StreamHandler(self.buf)
        h.setFormatter(logging.Formatter("%(message)s|%(customer)s"))
        self.log.handlers = [h]
        self.log.setLevel(logging.INFO)
        install(self.log)

    def out(self):
        return self.buf.getvalue()

    def test_phones_emails_urls_and_long_numbers_in_messages(self):
        self.log.info("from %s <%s> see %s acct %s", "+967 771 234 567", "a.b@example.com", "https://x.example/?t=1", "12345678",
                      extra={"customer": "cust_ab12"})
        o = self.out()
        for leaked in ("771", "a.b@example.com", "x.example", "12345678"):
            self.assertNotIn(leaked, o)
        self.assertIn("cust_ab12", o)

    def test_arabic_indic_digits_are_phones_too(self):
        self.assertEqual(redact_text("رقمي ٧٧١٢٣٤٥٦٧"), "رقمي <PHONE>")
        self.assertEqual(redact_text("00967771234567"), "<PHONE>")

    def test_content_fields_are_removed_whatever_they_contain(self):
        self.log.info("inbound", extra={"customer": "c", "body": "أنا أحمد من شارع حدة", "payload": {"text": "x"}})
        rec = logging.LogRecord("x", 20, "x", 0, "m %s", ({"message_text": "سر", "id": "wamid.1"},), None)
        RedactingFilter().filter(rec)
        self.assertNotIn("سر", rec.getMessage())
        f = RedactingFilter()
        r2 = logging.LogRecord("x", 20, "x", 0, "m", (), None)
        r2.body, r2.payload, r2.task_id = "أنا أحمد", {"text": "x"}, "t_1"
        f.filter(r2)
        self.assertTrue(r2.body.startswith("[REDACTED"))
        self.assertTrue(r2.payload.startswith("[REDACTED"))
        self.assertEqual(r2.task_id, "t_1")

    def test_tokens_and_jwts(self):
        t = redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwx and EAAB" + "x" * 30 + " and eyJhbGciOi.eyJzdWIiOiIx.c2lnbmF0dXJl")
        self.assertNotIn("abcdefghijklmnop", t)
        self.assertNotIn("EAAB", t)
        self.assertIn("<JWT>", t)

    def test_exception_text_is_redacted(self):
        try:
            raise ValueError("failed for 967771234567")
        except ValueError:
            self.log.exception("boom", extra={"customer": "c"})
        self.assertNotIn("771234567", self.out())

    def test_ids_and_codes_survive(self):
        self.assertEqual(redact_text("task t_ab12 code BUDGET_EXCEEDED_AGENT wamid.HBgM"), "task t_ab12 code BUDGET_EXCEEDED_AGENT wamid.HBgM")


if __name__ == "__main__":
    unittest.main()
