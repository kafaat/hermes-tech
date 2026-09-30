import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from complaints import Matcher, normalize, load_policy


class TestComplaints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Matcher()

    def test_normalization(self):
        r = load_policy()["normalization"]
        self.assertEqual(normalize("شَكْوَى", r), normalize("شكوي", r))
        self.assertEqual(normalize("الفاتورة", r), "الفاتوره")
        self.assertEqual(normalize("إلغاء", r), "الغاء")

    def test_patron_pricing_goes_to_owner_not_founder(self):
        d = self.m.route("الفاتورة غلط يا جماعة", "patron")
        self.assertEqual(d["kind"], "complaint")
        self.assertIn("owner_whatsapp", d["routes"])
        self.assertNotIn("founder_sms", d["routes"])
        self.assertNotIn("founder_slack", d["routes"])

    def test_patron_discount_question_is_owner_inquiry(self):
        d = self.m.route("في خصم على الوجبات اليوم؟", "patron")
        self.assertEqual(d["kind"], "owner_inquiry")
        self.assertEqual(d["routes"], ["owner_whatsapp"])

    def test_patron_legal_copies_founder(self):
        d = self.m.route("بشتكيكم للنيابة", "patron")
        self.assertEqual(d["severity"], "critical")
        self.assertIn("founder_slack", d["routes"])

    def test_client_refund_yemeni_dialect(self):
        d = self.m.route("ما اشتيش الخدمة رجعوا زلطي", "client")
        self.assertEqual(d["kind"], "complaint")
        self.assertEqual(set(d["categories"]), {"refund", "pricing"})
        self.assertEqual(d["routes"], ["founder_slack"])

    def test_client_safety_is_sms(self):
        d = self.m.route("حد دخل على الحساب ونشر منشور غريب", "client")
        self.assertEqual(d["severity"], "critical")
        self.assertIn("founder_sms", d["routes"])

    def test_exception_neutralizes_only_its_span(self):
        d = self.m.route("السعر مناسب بس الفاتورة غلط", "client")
        self.assertEqual(d["kind"], "complaint")
        self.assertIn("pricing", d["categories"])
        d2 = self.m.route("السعر مناسب شكرا", "client")
        self.assertEqual(d2["action"], "reply_allowed")

    def test_exception_promise_to_return(self):
        self.assertEqual(self.m.route("تمام أرجع لكم بكرة", "client")["action"], "reply_allowed")

    def test_token_boundary_no_false_positive(self):
        # "نصب" (fraud) must not match inside "منصب" (position)
        self.assertEqual(self.m.route("عندي سؤال عن منصب المدير", "client")["action"], "reply_allowed")

    def test_proclitic_attached(self):
        self.assertEqual(self.m.route("وبالفاتورة غلط", "client")["kind"], "complaint")
        self.assertEqual(self.m.route("والفاتورة غلط", "client")["kind"], "complaint")
        self.assertEqual(self.m.route("للفاتورة غلط واضح", "client")["kind"], "complaint")

    def test_diacritics_and_tatweel(self):
        self.assertEqual(self.m.route("اختـــراق الحِساب", "client")["kind"], "complaint")

    def test_plain_question_allowed(self):
        self.assertEqual(self.m.route("متى تفتحون يوم الجمعة؟", "patron")["action"], "reply_allowed")


if __name__ == "__main__":
    unittest.main()
