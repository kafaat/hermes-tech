import json, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from complaints import Matcher
from triage import combined_route


class TestTriage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Matcher()
        cls.seed = [json.loads(l) for l in (ROOT / "evals/complaints_seed.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_classifier_cannot_remove_keyword_match(self):
        never = lambda t: (False, None, 0.99)
        d = combined_route(self.m, "الفاتورة غلط", "client", never)
        self.assertEqual((d["kind"], d["source"]), ("complaint", "keywords"))

    def test_classifier_adds_escalation_above_threshold(self):
        yes = lambda t: (True, "quality", 0.8)
        d = combined_route(self.m, "الأكل وصل بارد والطلب ناقص", "patron", yes)
        self.assertEqual((d["kind"], d["source"]), ("complaint", "classifier"))
        self.assertEqual(d["routes"], ["owner_whatsapp", "owner_portal"])

    def test_below_threshold_keeps_stage1_decision(self):
        weak = lambda t: (True, "quality", 0.1)
        self.assertEqual(combined_route(self.m, "متى تفتحون؟", "patron", weak)["action"], "reply_allowed")

    def test_owner_inquiry_preserved(self):
        no = lambda t: (False, None, 0.9)
        self.assertEqual(combined_route(self.m, "في خصم للطلاب؟", "patron", no)["kind"], "owner_inquiry")

    def test_perfect_stage2_closes_seed_gap(self):
        # an oracle stage 2 shows the architecture reaches zero misses; the real model must be measured
        truth = {r["text"]: r for r in self.seed}
        oracle = lambda t: (truth[t]["is_complaint"], truth[t].get("category"), 1.0 if truth[t]["is_complaint"] else 0.0)
        missed = [r for r in self.seed if r["is_complaint"] and combined_route(self.m, r["text"], r["channel"], oracle)["kind"] != "complaint"]
        self.assertEqual(missed, [])


if __name__ == "__main__":
    unittest.main()
