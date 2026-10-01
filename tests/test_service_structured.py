"""Structured competitor facts (schema.org JSON-LD): a redesign changes nothing, a price change is found and said in
plain Arabic, and hostile or malformed pages yield bounded, empty or partial facts, never a guess."""
import json, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.structured import MAX_ITEMS, content_hash, diff, extract, summarize  # noqa: E402

GRAPH = {"@context": "https://schema.org", "@graph": [
    {"@type": "Restaurant", "name": "مطعم الريف", "telephone": "+967 1 234567", "priceRange": "$$",
     "address": {"@type": "PostalAddress", "streetAddress": "شارع الزبيري", "addressLocality": "صنعاء"},
     "openingHoursSpecification": [{"@type": "OpeningHoursSpecification", "dayOfWeek": ["Saturday", "Sunday"],
                                    "opens": "09:00", "closes": "23:00"}],
     "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.3", "reviewCount": "128"},
     "hasMenu": {"@type": "Menu", "hasMenuSection": [{"@type": "MenuSection", "name": "مشويات", "hasMenuItem": [
         {"@type": "MenuItem", "name": "شاورما دجاج", "offers": {"@type": "Offer", "price": "1500", "priceCurrency": "yer"}},
         {"@type": "MenuItem", "name": "مندي لحم", "offers": {"@type": "Offer", "price": "6,000", "priceCurrency": "YER"}}]}]}}]}


def page(data, layout="<div class='v1'><h1>مطعم الريف</h1></div>"):
    return f"<html><head><script type=\"application/ld+json\">{json.dumps(data, ensure_ascii=False)}</script></head>" \
           f"<body>{layout}<script>var x = 1;</script></body></html>".encode()


class TestExtract(unittest.TestCase):
    def test_a_restaurant_graph_becomes_normalized_facts(self):
        f = extract(page(GRAPH))
        self.assertEqual(f["business"]["name"], "مطعم الريف")
        self.assertEqual((f["business"]["type"], f["business"]["locality"], f["business"]["price_range"]), ("restaurant", "صنعاء", "$$"))
        self.assertEqual(f["business"]["hours"], ["Sa,Su 09:00-23:00"])
        self.assertEqual(f["rating"], {"value": "4.3", "count": "128"})
        self.assertEqual(f["items"], [{"name": "شاورما دجاج", "price": "1500", "currency": "YER"},
                                      {"name": "مندي لحم", "price": "6000", "currency": "YER"}])

    def test_a_redesign_keeps_the_same_hash_and_no_changes(self):
        a, b = extract(page(GRAPH)), extract(page(GRAPH, layout="<main class='new'><section>تصميم جديد تمامًا</section></main>"))
        self.assertEqual(content_hash(a), content_hash(b))
        self.assertEqual(diff(a, b), [])

    def test_no_json_ld_means_no_facts_and_no_hash(self):
        f = extract(b"<html><body><h1>Menu</h1><p>Shawarma 1500 YER</p><script>var menu={price:1}</script></body></html>")
        self.assertEqual((f, content_hash(f)), ({}, None))

    def test_malformed_blocks_are_skipped_and_good_ones_kept(self):
        body = (b"<script type='application/ld+json'>{not json</script>"
                + page({"@type": "Product", "name": "Tea", "offers": {"price": 200, "priceCurrency": "SAR"}}))
        self.assertEqual(extract(body)["items"], [{"name": "Tea", "price": "200", "currency": "SAR"}])

    def test_prices_are_never_guessed(self):
        for raw in ("YER 1500", "1.500,00", "-5", "free", "1e99", True):
            with self.subTest(raw):
                f = extract(page({"@type": "Product", "name": "X", "offers": {"price": raw}}))
                self.assertIsNone(f["items"][0]["price"])

    def test_hostile_input_is_bounded_and_cleaned(self):
        many = {"@graph": [{"@type": "Product", "name": f"item {i}"} for i in range(MAX_ITEMS + 200)]}
        self.assertEqual(len(extract(page(many))["items"]), MAX_ITEMS)
        deep = {"@type": "Product", "name": "top"}
        cur = deep
        for _ in range(200):
            cur["isRelatedTo"] = {"@type": "Product", "name": "deeper"}
            cur = cur["isRelatedTo"]
        self.assertLessEqual(len(extract(page(deep))["items"]), 2)
        f = extract(page({"@type": "Product", "name": "a‮evil\u0000" + "x" * 500}))
        self.assertTrue(f["items"][0]["name"].startswith("aevil"))
        self.assertLessEqual(len(f["items"][0]["name"]), 200)
        huge = b"<script type='application/ld+json'>{\"@type\":\"Product\",\"name\":\"" + b"x" * 300_000 + b"\"}</script>"
        self.assertEqual(extract(huge), {})                                   # over the block limit: not parsed

    def test_a_catalog_of_offers_gives_each_product_its_price(self):
        catalog = {"@type": "Store", "name": "متجر", "hasOfferCatalog": {"@type": "OfferCatalog", "itemListElement": [
            {"@type": "Offer", "price": "2500", "priceCurrency": "YER", "itemOffered": {"@type": "Product", "name": "عسل"}},
            {"@type": "Offer", "price": "900", "priceCurrency": "YER", "itemOffered": {"@type": "Service", "name": "توصيل"}}]}}
        self.assertEqual(extract(page(catalog))["items"], [{"name": "توصيل", "price": "900", "currency": "YER"},
                                                         {"name": "عسل", "price": "2500", "currency": "YER"}])

    def test_scripts_that_are_not_json_ld_are_ignored(self):
        body = b"<script type='text/javascript'>{\"@type\":\"Product\",\"name\":\"trap\"}</script>"
        self.assertEqual(extract(body), {})


class TestDiff(unittest.TestCase):
    def test_price_changes_new_and_removed_items_hours_and_rating_in_order(self):
        old = extract(page(GRAPH))
        g = json.loads(json.dumps(GRAPH))
        r = g["@graph"][0]
        items = r["hasMenu"]["hasMenuSection"][0]["hasMenuItem"]
        items[0]["offers"]["price"] = "1700"
        del items[1]
        items.append({"@type": "MenuItem", "name": "فتة", "offers": {"price": "2500", "priceCurrency": "YER"}})
        r["openingHoursSpecification"][0]["closes"] = "01:00"
        r["aggregateRating"]["ratingValue"] = "4.1"
        changes = diff(old, extract(page(g)))
        self.assertEqual([c["kind"] for c in changes], ["item_added", "item_removed", "price_changed", "hours_changed", "rating_changed"])
        text = summarize(changes)
        self.assertIn("تغيّر سعر شاورما دجاج: 1500 ← 1700 YER", text)
        self.assertIn("صنف جديد: فتة بسعر 2500 YER", text)
        self.assertIn("أُزيل صنف: مندي لحم", text)
        self.assertIn("تغيّر التقييم: 4.3 ← 4.1 (128 تقييمًا)", text)

    def test_a_first_snapshot_lists_everything_as_new_and_the_summary_is_capped(self):
        many = {"@graph": [{"@type": "Product", "name": f"p{i:02d}", "offers": {"price": i}} for i in range(15)]}
        changes = diff({}, extract(page(many)))
        self.assertEqual(len(changes), 15)
        self.assertTrue(summarize(changes, limit=10).endswith("و5 تغييرات أخرى"))


if __name__ == "__main__":
    unittest.main()
