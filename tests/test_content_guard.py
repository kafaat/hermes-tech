import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from content_guard import ContentGuard


class TestContentGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g = ContentGuard()

    def rules(self, text, kind):
        return {f["rule"] for f in self.g.check(text, kind)}

    def test_price_blocked_in_reply_reviewed_in_post(self):
        self.assertEqual(self.g.verdict("وجبة المندي بـ 5000 ريال", "reply"), "block")
        self.assertEqual(self.g.verdict("وجبة المندي بـ 5000 ريال", "post"), "review")

    def test_arabic_indic_digits(self):
        self.assertIn("price_mention", self.rules("العرض ٣٥٠٠ ريال فقط", "post"))

    def test_price_word_with_proclitic(self):
        self.assertIn("price_word", self.rules("وبالسعر نفسه كل يوم", "reply"))

    def test_medical_claim_blocked_everywhere(self):
        for k in ("post", "reply", "site"):
            self.assertEqual(self.g.verdict("هذا العسل يعالج السكر", k), "block")

    def test_guarantee_reviewed_in_post(self):
        self.assertEqual(self.g.verdict("مطعمنا الأفضل في اليمن", "post"), "review")

    def test_phone_only_flagged_in_replies(self):
        self.assertIn("phone_number", self.rules("تواصل مع 777123456", "reply"))
        self.assertNotIn("phone_number", self.rules("تواصل مع 777123456", "post"))

    def test_links(self):
        self.assertIn("external_link", self.rules("شوف العرض https://bit-ly.example/x", "post"))
        self.assertNotIn("external_link", self.rules("صفحتنا https://www.facebook.com/ourpage", "post"))
        self.assertIn("external_link", self.rules("https://facebook.com.evil.io/login", "post"))

    def test_disparagement(self):
        self.assertEqual(self.g.verdict("المطعم المقابل محتالين", "post"), "block")

    def test_clean_post_passes(self):
        self.assertEqual(self.g.verdict("نرحب بكم يوميًا من الساعة العاشرة صباحًا", "post"), "pass")

    def test_no_false_positive_on_prices_word_inside_other_word(self):
        self.assertEqual(self.g.verdict("مسعرة الخدمات", "reply"), "pass")


if __name__ == "__main__":
    unittest.main()
