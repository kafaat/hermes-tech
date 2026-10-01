"""Structured data for customer sites: JSON-LD from approved facts only, a block that cannot close its script element,
read back completely by our own reader (service/structured.extract, the competitor check's), plus sitemap and robots."""
import json, re, sys, unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.site_seo import MARKER, inject, jsonld, jsonld_block, robots_txt, sitemap_xml  # noqa: E402
from service.structured import extract  # noqa: E402

PROFILE = {"approved": True, "name": "مطعم الريف", "sector": "restaurant", "url": "https://alreef.example",
           "city": "صنعاء", "street": "شارع الزبيري", "telephone": "+967 1 234567", "price_range": "$$",
           "hours": [{"days": ["Sa", "Su", "Mo"], "opens": "09:00", "closes": "23:00"}],
           "items": [{"approved": True, "name": "مندي لحم - نفر", "price": "6,000", "currency": "YER", "section": "مشويات"},
                     {"approved": True, "name": "شاي عدني", "price": 300, "currency": "YER", "section": "مشروبات"},
                     {"approved": True, "name": "سلطة اليوم", "section": "مقبلات"}]}
TEMPLATE = f"<html><head><title>x</title>{MARKER}</head><body><h1>مطعم الريف</h1></body></html>"


class TestJsonLd(unittest.TestCase):
    def test_our_reader_reads_back_everything_we_publish(self):
        facts = extract(inject(TEMPLATE, jsonld_block(PROFILE)).encode())
        self.assertEqual(facts["business"], {"name": "مطعم الريف", "type": "restaurant", "telephone": "+967 1 234567",
                                             "price_range": "$$", "locality": "صنعاء", "street": "شارع الزبيري",
                                             "hours": ["Sa,Su,Mo 09:00-23:00"]})
        self.assertEqual(facts["items"], [{"name": "سلطة اليوم", "price": None, "currency": None},
                                          {"name": "شاي عدني", "price": "300", "currency": "YER"},
                                          {"name": "مندي لحم - نفر", "price": "6000", "currency": "YER"}])

    def test_a_shop_publishes_a_catalog_that_reads_back_with_prices(self):
        shop = {**PROFILE, "sector": "retail", "items": [{"approved": True, "name": "عسل سدر", "price": "25000", "currency": "YER"}]}
        facts = extract(inject(TEMPLATE, jsonld_block(shop)).encode())
        self.assertEqual((facts["business"]["type"], facts["items"]), ("store", [{"name": "عسل سدر", "price": "25000", "currency": "YER"}]))

    def test_only_approved_facts_are_published(self):
        with self.assertRaises(ValueError):
            jsonld({**PROFILE, "approved": False})
        with self.assertRaises(ValueError):
            jsonld({**PROFILE, "items": [*PROFILE["items"], {"name": "صنف لم يعتمده المالك", "price": "1"}]})

    def test_the_block_cannot_close_its_script_element(self):
        hostile = {**PROFILE, "name": "</script><script>alert(1)</script> & <!--"}
        block = jsonld_block(hostile)
        self.assertEqual(block.count("</script>"), 1)
        self.assertTrue(block.endswith("</script>"))
        self.assertNotRegex(block[len('<script type="application/ld+json">'):-len("</script>")], r"[<>&]")
        body = json.loads(block[len('<script type="application/ld+json">'):-len("</script>")])
        self.assertEqual(body["name"], "</script><script>alert(1)</script> & <!--")

    def test_ambiguous_or_malformed_values_are_refused_not_guessed(self):
        for change in ({"url": "http://alreef.example"}, {"sector": "bank"}, {"telephone": "call us"},
                       {"price_range": "cheap"}, {"hours": [{"days": ["Saturday"], "opens": "9", "closes": "23:00"}]},
                       {"items": [{"approved": True, "name": "x", "price": "1.500,00", "currency": "YER"}]},
                       {"items": [{"approved": True, "name": "x", "price": "100", "currency": "ريال"}]}, {"name": "  "}):
            with self.subTest(change), self.assertRaises(ValueError):
                jsonld({**PROFILE, **change})

    def test_the_template_needs_one_marker_in_the_head(self):
        for bad in ("<html><head></head><body></body></html>", f"<head>{MARKER}{MARKER}</head>",
                    f"<html><head></head><body>{MARKER}</body></html>"):
            with self.subTest(bad), self.assertRaises(ValueError):
                inject(bad, jsonld_block(PROFILE))


class TestSitemapAndRobots(unittest.TestCase):
    def test_sitemap_lists_the_pages_escaped_and_bounded(self):
        xml = sitemap_xml("https://alreef.example/", [("/", date(2026, 10, 1)), ("/menu", date(2026, 9, 30))])
        self.assertIn("<loc>https://alreef.example/menu</loc><lastmod>2026-09-30</lastmod>", xml)
        self.assertEqual(len(re.findall("<url>", xml)), 2)
        for pages in ([], [("/p%d" % i, date(2026, 1, 1)) for i in range(6)], [("/../etc", date(2026, 1, 1))],
                      [("/a?b=<x>", date(2026, 1, 1))], [("menu", date(2026, 1, 1))]):
            with self.subTest(pages), self.assertRaises(ValueError):
                sitemap_xml("https://alreef.example", pages)

    def test_robots_points_to_the_sitemap(self):
        self.assertEqual(robots_txt("https://alreef.example"), "User-agent: *\nAllow: /\nSitemap: https://alreef.example/sitemap.xml\n")


if __name__ == "__main__":
    unittest.main()
