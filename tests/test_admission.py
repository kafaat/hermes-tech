import json, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from admission import evaluate
import acceptance
from stats import upper_bound, acceptance_probability_zero_misses

ADM = json.loads((ROOT / "evals/model_admission.json").read_text(encoding="utf-8"))
FL, POL = ADM["floors"], ADM["retest_policy"]
GOOD = {"complaints_missed": 0, "complaints_total": 60, "critical_missed": 0, "complaints_precision": 0.93, "quality_precision": 0.96, "draft_first_acceptance": 0.74,
        "draft_drop_pp": 3, "draft_rated_samples": 120, "latency_p95_seconds": 5}
BAD = dict(GOOD, complaints_missed=1)                 # 1/60 -> one-sided 97.5% bound ~ 8.8% > 6%


def cand(*attempts):
    return {"model": "m", "version": "2026-09", "agent_ids": ["agent_content"], "attempts": list(attempts)}


CATS = {c: 5 for c in ("pricing", "quality", "legal", "safety", "refund")}


def att(d, ds, metrics, new_share=1.0, passed=None, new_messages=120, new_complaints=30, by_cat=None, size=(220, 60)):
    a = {"date": d, "dataset_id": ds, "new_share": new_share, "metrics": metrics, "new_messages": new_messages,
         "new_complaints": new_complaints, "new_complaints_by_category": dict(CATS) if by_cat is None else by_cat,
         "dataset_messages": size[0], "dataset_complaints": size[1]}
    if passed is not None:
        a["passed"] = passed
    return a


class TestAdmission(unittest.TestCase):
    def test_first_pass_is_admitted(self):
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", GOOD)), FL, POL)[0], "admitted")

    def test_one_miss_leaves_a_retest_path(self):
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", BAD)), FL, POL)[0], "candidate")

    def test_valid_retest_after_failure_admits(self):
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", BAD), att("2026-10-20", "B", GOOD, 0.3)), FL, POL)[0], "admitted")

    def test_retest_on_same_dataset_is_invalid(self):
        st, probs = evaluate(cand(att("2026-10-01", "A", BAD), att("2026-10-20", "A", GOOD, 0.3)), FL, POL)
        self.assertEqual(st, "invalid")

    def test_retest_too_soon_or_too_little_new_data_is_invalid(self):
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", BAD), att("2026-10-05", "B", GOOD, 0.3)), FL, POL)[0], "invalid")
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", BAD), att("2026-10-20", "B", GOOD, 0.1)), FL, POL)[0], "invalid")

    def test_two_consecutive_failures_reject_the_version(self):
        st, _ = evaluate(cand(att("2026-10-01", "A", BAD), att("2026-10-20", "B", BAD, 0.3), att("2026-11-10", "C", GOOD, 0.3)), FL, POL)
        self.assertEqual(st, "rejected")

    def test_thin_dataset_is_invalid_not_passing(self):
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", GOOD, size=(40, 10))), FL, POL)[0], "invalid")

    def test_retry_needs_absolute_new_data_in_every_category(self):
        # 25% of a small pool is not enough: 10 new messages fail the absolute minimum
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", BAD), att("2026-10-20", "B", GOOD, 0.3, new_messages=10, new_complaints=3)), FL, POL)[0], "invalid")
        no_legal = dict(CATS, legal=0)
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", BAD), att("2026-10-20", "B", GOOD, 0.3, by_cat=no_legal)), FL, POL)[0], "invalid")

    def test_one_miss_passes_only_with_enough_evidence(self):
        many = dict(GOOD, complaints_missed=1, complaints_total=120)      # upper bound ~3.9%
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", many, size=(400, 120))), FL, POL)[0], "admitted")

    def test_any_critical_miss_fails(self):
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", dict(GOOD, complaints_total=200, complaints_missed=1, critical_missed=1))), FL, POL)[0], "candidate")

    def test_a_third_attempt_is_outside_the_acceptance_family(self):
        st, probs = evaluate(cand(att("2026-10-01", "A", GOOD), att("2026-10-20", "B", BAD, 0.3), att("2026-11-10", "C", GOOD, 0.3)), FL, POL)
        self.assertEqual(st, "invalid")
        self.assertTrue(any("per version" in p for p in probs))

    def test_development_data_is_never_an_acceptance_attempt(self):
        st, probs = evaluate(cand(att("2026-10-01", "evals/complaints_seed.jsonl", GOOD)), FL, POL)
        self.assertEqual(st, "invalid")

    def test_hand_set_pass_that_contradicts_metrics_is_invalid(self):
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", BAD, passed=True)), FL, POL)[0], "invalid")


if __name__ == "__main__":
    unittest.main()


class TestOnePolicy(unittest.TestCase):
    """v1.8: one versioned rule; the review's counter-example now has one verdict everywhere."""

    def test_review_example_one_non_critical_miss_in_100(self):
        ok, reasons = acceptance.judge(1, 100, 0, 0.95, 2)
        self.assertTrue(ok, reasons)                                   # 5.45% <= 6% at one-sided 97.5%: no zero-total rule
        m = dict(GOOD, complaints_missed=1, complaints_total=100)
        self.assertEqual(evaluate(cand(att("2026-10-01", "A", m, size=(300, 100))), FL, POL)[0], "admitted")

    def test_named_method_and_per_attempt_confidence(self):
        self.assertAlmostEqual(acceptance.attempt_confidence(), 0.975)
        self.assertAlmostEqual(upper_bound(0, 50), 1 - 0.05 ** (1 / 50))             # one-sided closed form
        self.assertAlmostEqual(upper_bound(0, 50, 0.975), 0.0711, places=3)          # = upper end of a two-sided 95% interval
        self.assertAlmostEqual(acceptance.miss_bound(0, 60), 0.0596, places=3)

    def test_path_level_error_is_within_five_percent_at_the_threshold(self):
        p = acceptance.POLICY
        self.assertLessEqual(acceptance_probability_zero_misses(p["max_miss_rate_upper"], p["min_complaints"], p["max_attempts_per_version"]), 0.05)
        self.assertGreater(acceptance_probability_zero_misses(0.06, 50, 2), 0.05)    # the 1.7 rule (50, per attempt 95%) did not hold it

    def test_small_categories_are_reported_not_trusted(self):
        self.assertGreater(acceptance.category_bounds({}, {"legal": 5})["legal"], 0.45)
