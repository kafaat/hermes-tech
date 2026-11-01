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


class TestSiteBuilder(unittest.TestCase):
    """service/site_builder.py (28.39): the whole one-page site, from approved facts only."""
    BUSINESS = {"name": "مطعم \"الريف\"", "sector": "restaurant", "city": "صنعاء"}
    FACTS = {"hours": "يوميًا من ٩ إلى ١١", "location": "شارع الزبيري", "payment": "نقدًا", "phone": "+967 712 345 678",
             "internal_note": "رقم المورد السري 777"}

    def build(self, facts=None, **kw):
        from service.site_builder import site_files
        args = dict(base_url="https://alreef.example", today=date(2026, 11, 1), app_url="https://hermes.example",
                    site_key="K" * 32)
        args.update(kw)
        return site_files(self.BUSINESS, self.FACTS if facts is None else facts, **args)

    def test_only_known_approved_topics_are_published_and_the_build_is_deterministic(self):
        from service.site_builder import content_hash
        page = self.build()["index.html"].decode()
        for text in ("ساعات العمل", "يوميًا من ٩ إلى ١١", "شارع الزبيري", "نقدًا"):
            self.assertIn(text, page)
        self.assertNotIn("777", page)                         # a topic the site does not know is never shown
        self.assertEqual(content_hash(self.build()), content_hash(self.build()))
        self.assertNotEqual(content_hash(self.build()), content_hash(self.build({**self.FACTS, "payment": "تحويل"})))

    def test_what_the_site_publishes_reads_back_and_every_value_is_escaped(self):
        files = self.build({**self.FACTS, "about": "<script>alert(1)</script>"})
        page = files["index.html"].decode()
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("<title>مطعم &quot;الريف&quot; · صنعاء</title>", page)
        facts = extract(files["index.html"])
        self.assertEqual((facts["business"]["name"], facts["business"]["type"], facts["business"]["telephone"]),
                         ("مطعم \"الريف\"", "restaurant", "+967 712 345 678"))

    def test_a_light_page_with_no_script_no_plain_http_and_links_only_where_expected(self):
        files = self.build()
        page = files["index.html"].decode()
        self.assertTrue(page.startswith("<!doctype html>\n<html lang=\"ar\" dir=\"rtl\">"))
        self.assertLess(sum(len(v) for v in files.values()), 50_000)
        self.assertNotIn("http://", page)
        self.assertNotIn("<script src", page)
        self.assertNotIn("-9999", page)                       # the form's trap field widens no right-to-left page
        hosts = {re.sub(r"^https://([^/]+).*$", r"\1", u) for u in re.findall(r'(?:href|action)="(https://[^"]+)"', page)}
        self.assertEqual(hosts, {"alreef.example", "wa.me", "hermes.example"})
        self.assertIn('<a href="tel:+967712345678" dir="ltr">+967 712 345 678</a>', page)
        self.assertIn("form-action https://hermes.example", files["_headers"].decode())
        self.assertIn("<loc>https://alreef.example/</loc>", files["sitemap.xml"].decode())

    def test_no_form_without_this_service_and_no_link_to_a_number_whatsapp_cannot_dial(self):
        files = self.build({"hours": "٩-١١", "phone": "12345"}, app_url=None, site_key=None)
        page = files["index.html"].decode()
        self.assertNotIn("<form", page)
        self.assertNotIn("wa.me", page)
        self.assertIn("form-action 'none'", files["_headers"].decode())

    def test_bad_inputs_are_refused_not_guessed(self):
        for kw in ({"base_url": "http://alreef.example"}, {"base_url": "https://alreef.example/menu"}):
            with self.subTest(kw), self.assertRaises(ValueError):
                self.build(**kw)
        from service.site_builder import site_files
        with self.assertRaises(ValueError):
            site_files({**self.BUSINESS, "sector": "bank"}, self.FACTS, base_url="https://a.example", today=date(2026, 11, 1))
