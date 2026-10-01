"""The WhatsApp QR code is made here, not by an online generator: the matrix for each mask equals the one an
independent encoder (segno 1.6.6, with its extra zero codeword after a byte-aligned terminator removed) produced for
the same text, across versions 1 to 10; the code encodes the wa.me link itself, never a redirect."""
import hashlib, re, sys, unittest
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.qr import matrix, qr_svg, wa_me  # noqa: E402

GREETING = "مرحبا، أريد حجز طاولة لأربعة"
REFERENCE = [  # (text, mask, version, sha256 of the module bits row by row, 1 = dark)
    ("A", 0, 1, "3f594cfc1843904d93447f4958be7aa4114f4062fecb67b78c5ea20479846f04"),
    ("https://wa.me/967712345678", 3, 2, "7fb5315523db554580656469062f93f865dcd8d2c46ad163e5eca551993fe4fd"),
    (wa_me("967712345678", GREETING), 5, 10, "69da4fe36523991f09588bc2cbce3a4a910dde5449ad67aeaf860b97584237cc"),
    ("Q" * 120 + "/عربي", 6, 8, "2d009deaac13fc3fb22df5e8d9225738c216ab6d0e0f8dfe58f760bdc3372597"),
    ("z" * 213, 2, 10, "64244c8debe4f23144064b68dcbaa94e3634804d9bbf7b3bc968cd73c8ebd0bd")]


def digest(m):
    return hashlib.sha256("".join("1" if v else "0" for row in m for v in row).encode()).hexdigest()


class TestEncoder(unittest.TestCase):
    def test_each_matrix_equals_the_independent_reference(self):
        for text, mask, version, want in REFERENCE:
            with self.subTest(len=len(text), version=version):
                m = matrix(text, mask)
                self.assertEqual(len(m), version * 4 + 17)
                self.assertEqual(digest(m), want)

    def test_the_chosen_mask_is_one_of_the_eight_valid_codes(self):
        text = wa_me("967712345678")
        self.assertIn(digest(matrix(text)), {digest(matrix(text, k)) for k in range(8)})

    def test_too_long_is_refused_not_truncated(self):
        with self.assertRaises(ValueError):
            matrix("x" * 214)

    def test_the_svg_is_self_contained_with_a_quiet_zone(self):
        svg = qr_svg(wa_me("967712345678"))
        self.assertTrue(svg.startswith('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 33 33"'))   # 25 + 2 x 4
        self.assertNotRegex(svg, r"<script|href|xlink|on\w+=")
        self.assertIn('<rect width="33" height="33" fill="#fff"/>', svg)


class TestWaMe(unittest.TestCase):
    def test_the_link_is_wa_me_itself_with_an_encoded_greeting(self):
        self.assertEqual(wa_me("+967 712-345-678"), "https://wa.me/967712345678")
        self.assertEqual(wa_me("00967712345678"), "https://wa.me/967712345678")
        link = wa_me("967712345678", GREETING)
        self.assertRegex(link, r"^https://wa\.me/967712345678\?text=[A-Za-z0-9%._~-]+$")
        self.assertEqual(unquote(link.split("=", 1)[1]), GREETING)

    def test_numbers_and_greetings_that_would_mislead_are_refused(self):
        for phone in ("0712345678", "abc", "+967 71", "9677123456789012", "967712345678&text=x"):
            with self.subTest(phone), self.assertRaises(ValueError):
                wa_me(phone)
        with self.assertRaises(ValueError):
            wa_me("967712345678", "س" * 121)


if __name__ == "__main__":
    unittest.main()
