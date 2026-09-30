import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from anonymize import Anonymizer


class TestAnonymize(unittest.TestCase):
    def test_phone_forms_become_one_stable_token(self):
        a = Anonymizer()
        t1 = a("كلمني على 777 123 456")
        t2 = a("او +967-777123456")
        self.assertNotIn("777", t1 + t2)
        self.assertIn("<PHONE_1>", t1)
        self.assertIn("<PHONE_1>", t2)

    def test_arabic_indic_digits(self):
        self.assertIn("<PHONE_1>", Anonymizer()("رقمي ٧٧١٢٣٤٥٦٧"))

    def test_email_url_and_long_numbers(self):
        t = Anonymizer()("راسلني a.b@mail.com او https://x.example/p رقم الحوالة 99887766")
        self.assertIn("<EMAIL_1>", t)
        self.assertIn("<URL_1>", t)
        self.assertIn("<NUMBER_1>", t)

    def test_text_otherwise_unchanged(self):
        self.assertEqual(Anonymizer()("الأكل وصل بارد"), "الأكل وصل بارد")


if __name__ == "__main__":
    unittest.main()
