"""The complaint acceptance rule, from policies/acceptance_policy.json only (v1.8).

    judge(missed, total, critical_missed, precision, false_alarms_per_100) -> (ok, reasons)

Every consumer (admission of a second model, run_evals --gate standing-send, the validator, the claims
matrix) calls this module; none of them holds its own threshold.
"""
from __future__ import annotations
import json
from pathlib import Path
from stats import upper_bound, min_n_for_zero_misses

POLICY = json.loads((Path(__file__).resolve().parent.parent / "policies/acceptance_policy.json").read_text(encoding="utf-8"))


def attempt_confidence(p: dict = POLICY) -> float:
    """Bonferroni over the acceptance path: 95% for the path with 2 attempts -> 97.5% per attempt."""
    return 1 - (1 - p["path_confidence"]) / p["max_attempts_per_version"]


def miss_bound(missed: int, total: int, p: dict = POLICY) -> float:
    return upper_bound(missed, total, attempt_confidence(p))


def judge(missed: int, total: int, critical_missed: int, precision: float, false_alarms_per_100: float,
          p: dict = POLICY) -> tuple[bool, list[str]]:
    reasons = []
    if total < p["min_complaints"]:
        reasons.append(f"complaints {total} < {p['min_complaints']}")
    if total <= 0 or missed > total:
        reasons.append("no complaints or inconsistent counts")
    else:
        ub = miss_bound(missed, total, p)
        if ub > p["max_miss_rate_upper"] + 1e-12:
            reasons.append(f"miss-rate bound {ub:.2%} > {p['max_miss_rate_upper']:.0%} ({missed}/{total}, one-sided {attempt_confidence(p):.1%})")
    if critical_missed > p["critical_misses_max"]:
        reasons.append(f"critical misses {critical_missed} > {p['critical_misses_max']}")
    if precision < p["min_precision"]:
        reasons.append(f"precision {precision:.0%} < {p['min_precision']:.0%}")
    if false_alarms_per_100 > p["max_false_alarms_per_100_non_complaints"]:
        reasons.append(f"false alarms {false_alarms_per_100:.1f}/100 > {p['max_false_alarms_per_100_non_complaints']}")
    return not reasons, reasons


def category_bounds(missed_by_cat: dict, total_by_cat: dict, p: dict = POLICY) -> dict:
    """Reported, never gated: what the sample says about each category on its own."""
    return {c: upper_bound(missed_by_cat.get(c, 0), n, attempt_confidence(p)) for c, n in total_by_cat.items() if n > 0}


def minimum_complaints_consistent(p: dict = POLICY) -> bool:
    return min_n_for_zero_misses(p["max_miss_rate_upper"], attempt_confidence(p)) <= p["min_complaints"]
