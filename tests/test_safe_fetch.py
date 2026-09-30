import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from safe_fetch import plan, follow, FetchRefused

DNS = {"competitor.example": ["93.184.216.34"], "rebind.example": ["93.184.216.34", "10.0.0.5"],
       "meta.example": ["169.254.169.254"], "decimal.example": ["127.0.0.1"], "v6.example": ["::ffff:192.168.1.1"],
       "nat64meta.example": ["64:ff9b::a9fe:a9fe"], "nat64ok.example": ["64:ff9b::5db8:d822"], "sixtofour.example": ["2002:a9fe:a9fe::1"],
       "teredo.example": ["2001:0:4136:e378:8000:63bf:3fff:fdd2"], "ula.example": ["fd12:3456::1"]}
R = lambda h: DNS.get(h, [])


class TestSafeFetch(unittest.TestCase):
    def refused(self, url, code):
        with self.assertRaises(FetchRefused) as e:
            plan(url, R)
        self.assertEqual(e.exception.code, code, url)

    def test_public_https_is_planned_with_a_pinned_address(self):
        p = plan("https://competitor.example/menu", R)
        self.assertEqual((p["connect_to"], p["port"], p["max_bytes"]), ("93.184.216.34", 443, 2000000))

    def test_metadata_loopback_private_and_mapped_addresses_are_refused(self):
        for url in ("https://169.254.169.254/latest/meta-data/", "https://meta.example/", "https://127.0.0.1/",
                    "https://decimal.example/", "https://[::1]/", "https://v6.example/", "https://10.1.2.3/"):
            self.refused(url, "NON_PUBLIC_ADDRESS")

    def test_ipv6_forms_carrying_ipv4_are_judged_by_that_ipv4(self):
        # v1.8: 64:ff9b::a9fe:a9fe is "global" to ipaddress, but a NAT64 gateway turns it into 169.254.169.254
        for url in ("https://nat64meta.example/", "https://[64:ff9b::a9fe:a9fe]/", "https://sixtofour.example/",
                    "https://teredo.example/", "https://ula.example/", "https://[fe80::1]/"):
            self.refused(url, "NON_PUBLIC_ADDRESS")
        self.assertEqual(plan("https://nat64ok.example/", R)["connect_to"], "64:ff9b::5db8:d822")

    def test_one_private_answer_among_public_ones_is_enough_to_refuse(self):
        self.refused("https://rebind.example/", "NON_PUBLIC_ADDRESS")

    def test_scheme_port_and_credentials(self):
        self.refused("http://competitor.example/", "SCHEME_NOT_ALLOWED")
        self.refused("file:///etc/passwd", "SCHEME_NOT_ALLOWED")
        self.refused("https://competitor.example:8443/", "PORT_NOT_ALLOWED")
        self.refused("https://user:pw@competitor.example/", "CREDENTIALS_IN_URL")

    def test_redirects_are_revalidated_and_bounded(self):
        with self.assertRaises(FetchRefused) as e:
            follow(["https://competitor.example/a", "https://meta.example/b"], R)
        self.assertEqual(e.exception.code, "NON_PUBLIC_ADDRESS")
        with self.assertRaises(FetchRefused) as e:
            follow(["https://competitor.example/"] * 5, R)
        self.assertEqual(e.exception.code, "TOO_MANY_REDIRECTS")


if __name__ == "__main__":
    unittest.main()
