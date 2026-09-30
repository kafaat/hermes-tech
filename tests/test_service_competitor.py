"""Daily competitor check (a workflow, no model): the three states the owner can tell apart, the computed difference
on a realistic Arabic restaurant page, and one competitor's failure never stopping the others. The database side
(monthly cap, tenant of the snapshot, the job's narrow role) is SQL case 52."""
import sys, unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from safe_fetch import FetchRefused  # noqa: E402
from service.competitor import NO_FACTS, check, run, text_hash  # noqa: E402

PAGE = (ROOT / "tests/fixtures/competitor_restaurant_ar.html").read_bytes()
REDESIGNED = PAGE.replace(b'<main>', '<main class="v2"><div class="banner">عروض رمضان</div>'.encode())
REPRICED = PAGE.replace(b'"price": "4500"', b'"price": "5000"').replace(
    '{"@type": "MenuItem", "name": "قشر"'.encode(), '{"@type": "MenuItem", "name": "قهوة"'.encode())
PLAIN = "<html><body><h1>مطعم</h1><p>مندي 7000</p></body></html>".encode()


@dataclass
class P:
    status: int
    body: bytes


class Fetcher:
    def __init__(self, result):
        self.result = result

    def fetch(self, url):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


COMP = {"id": "c1", "customer_id": "u1", "url": "https://example-restaurant.test/", "label": "بيت الريف",
        "last_facts": None, "last_page_hash": None}


class TestCheck(unittest.TestCase):
    def test_first_check_takes_an_inventory(self):
        s = check(Fetcher(P(200, PAGE)), COMP)
        self.assertEqual(s["status"], "ok")
        self.assertIn("أول لقطة: 9 صنفًا بأسعار", s["diff_summary"])
        self.assertIn("التقييم 4.4 (212 تقييمًا)", s["diff_summary"])
        self.assertEqual(s["structured_facts"]["business"]["type"], "restaurant")
        self.assertRegex(s["content_hash"], r"^[0-9a-f]{64}$")

    def test_a_redesign_is_no_change_and_a_price_change_is_said(self):
        first = check(Fetcher(P(200, PAGE)), COMP)
        after = {**COMP, "last_facts": first["structured_facts"], "last_page_hash": first["page_hash"]}
        same = check(Fetcher(P(200, REDESIGNED)), after)
        self.assertEqual((same["status"], same["content_hash"]), ("ok", first["content_hash"]))
        self.assertIn("لا تغيير", same["diff_summary"])
        moved = check(Fetcher(P(200, REPRICED)), after)
        self.assertIn("تغيّر سعر مندي دجاج - نفر: 4500 ← 5000 YER", moved["diff_summary"])
        self.assertIn("صنف جديد: قهوة بسعر 400 YER", moved["diff_summary"])
        self.assertIn("أُزيل صنف: قشر", moved["diff_summary"])

    def test_a_page_without_structured_data_says_tracking_does_not_work(self):
        s = check(Fetcher(P(200, PLAIN)), COMP)
        self.assertEqual((s["status"], s["content_hash"], s["structured_facts"]), ("unverifiable", None, None))
        self.assertEqual(s["diff_summary"], NO_FACTS)
        changed = check(Fetcher(P(200, PLAIN.replace(b"7000", b"7500"))), {**COMP, "last_page_hash": s["page_hash"]})
        self.assertTrue(changed["diff_summary"].startswith("تغيّر نص الصفحة منذ الفحص السابق"))
        again = check(Fetcher(P(200, PLAIN)), {**COMP, "last_page_hash": s["page_hash"]})
        self.assertEqual(again["diff_summary"], NO_FACTS)

    def test_robots_and_login_pages_are_blocked_other_failures_unverifiable(self):
        self.assertEqual(check(Fetcher(FetchRefused("ROBOTS_DISALLOW")), COMP)["status"], "blocked")
        self.assertEqual(check(Fetcher(FetchRefused("LOGIN_PAGE")), COMP)["status"], "blocked")
        for result in (FetchRefused("TOO_LARGE"), FetchRefused("ROBOTS_UNREADABLE"), TimeoutError("x"), P(404, b"not found")):
            with self.subTest(result=repr(result)):
                s = check(Fetcher(result), COMP)
                self.assertEqual((s["status"], s["structured_facts"], s["page_hash"]), ("unverifiable", None, None))
                self.assertIn("يُعاد الفحص", s["diff_summary"])

    def test_the_text_hash_ignores_scripts_markup_and_whitespace(self):
        self.assertEqual(text_hash(b"<p>a  b</p><script>x=1</script>"), text_hash(b"<div>a\nb</div><script>x=2</script>"))
        self.assertNotEqual(text_hash(b"<p>a b</p>"), text_hash(b"<p>a c</p>"))


class Store:
    def __init__(self, comps, refuse=()):
        self.comps, self.refuse, self.rows = comps, set(refuse), []

    def due(self, limit):
        return self.comps[:limit]

    def insert(self, snap):
        if snap["competitor_id"] in self.refuse:
            return False
        self.rows.append(snap)
        return True


class TestRun(unittest.TestCase):
    def test_every_competitor_gets_a_snapshot_and_refusals_are_counted(self):
        comps = [{**COMP, "id": f"c{i}"} for i in range(3)]
        store = Store(comps, refuse={"c2"})
        self.assertEqual(run(store, Fetcher(P(200, PAGE))), {"ok": 2, "unverifiable": 0, "blocked": 0, "refused": 1, "error": 0})

    def test_each_competitor_reports_an_id_a_state_and_a_code_only(self):
        lines = []
        comps = [{**COMP, "id": "c0"}, {**COMP, "id": "c1"}]

        class Two:
            def __init__(self):
                self.results = [P(200, PAGE), FetchRefused("UNRESOLVED")]

            def fetch(self, url):
                r = self.results.pop(0)
                if isinstance(r, BaseException):
                    raise r
                return r
        run(Store(comps), Two(), report=lambda *a: lines.append(a))
        self.assertEqual(lines, [("c0", "ok", "FIRST"), ("c1", "unverifiable", "UNRESOLVED")])

    def test_a_failure_on_our_side_does_not_stop_the_rest(self):
        class Flaky(Store):
            def insert(self, snap):
                if snap["competitor_id"] == "c0":
                    raise RuntimeError("db hiccup")
                return super().insert(snap)
        store = Flaky([{**COMP, "id": f"c{i}"} for i in range(3)])
        self.assertEqual(run(store, Fetcher(P(200, PAGE)))["error"], 1)
        self.assertEqual([r["competitor_id"] for r in store.rows], ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
