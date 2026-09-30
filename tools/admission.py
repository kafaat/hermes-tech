"""Second-model admission (ADR-0009): status is computed from attempts, never hand-written.

Rules
- An attempt passes only if its metrics meet every floor; a hand-set "passed" that
  disagrees with the metrics makes the record invalid.
- The complaint criterion is policies/acceptance_policy.json via tools/acceptance.py (v1.8): one rule for
  admission, run_evals and the claims matrix. Every attempt runs on at least `min_messages` messages and
  `min_complaints` complaints; thin datasets are invalid, not "passing".
- A version has at most `max_attempts_per_version` attempts (the Bonferroni family of the policy); a
  further attempt makes the record invalid: a new version is a new candidate.
- A development dataset (used to tune keywords or thresholds) is never an acceptance attempt.
- After a failure, a retry needs a different dataset with at least `min_new_share_of_dataset`
  new messages AND absolute minimums: `min_new_messages`, `min_new_complaints`, and
  `min_new_complaints_per_category` in each of the five categories; and at least `cooldown_days`.
- Two consecutive failures of the same model VERSION reject that version for good.
  A new version is a new candidate with its own attempts.
- Admitted = last valid attempt passed.
"""
from __future__ import annotations
from datetime import date
import acceptance


def attempt_passes(m: dict, fl: dict) -> bool:
    ok, _ = acceptance.judge(m.get("complaints_missed", 10**9), m.get("complaints_total", 0), m.get("critical_missed", 10**9),
                             m.get("complaints_precision", 0), m.get("false_alarms_per_100", 0))
    return (ok
            and m.get("quality_precision", 0) >= fl["quality_precision_min"]
            and m.get("draft_first_acceptance", 0) >= fl["draft_first_acceptance_min"]
            and m.get("draft_drop_pp", 10**9) <= fl["draft_first_acceptance_drop_max_pp"]
            and m.get("draft_rated_samples", 0) >= fl["draft_min_rated_samples"]
            and m.get("latency_p95_seconds", 10**9) <= fl["latency_p95_seconds_max"])


def evaluate(candidate: dict, floors: dict, policy: dict):
    """Return (status, problems). status: admitted | candidate | rejected | invalid."""
    problems, streak, prev = [], 0, None
    attempts = sorted(candidate.get("attempts", []), key=lambda a: a["date"])
    ap = acceptance.POLICY
    if len(attempts) > ap["max_attempts_per_version"]:
        problems.append(f"{len(attempts)} attempts > {ap['max_attempts_per_version']} per version (acceptance_policy family)")
    for a in attempts:
        if a.get("dataset_id") in ap["development_datasets"]:
            problems.append(f"{a['date']}: development dataset {a['dataset_id']} used as an acceptance attempt")
        if a.get("dataset_messages", 0) < ap["min_messages"] or a.get("dataset_complaints", 0) < ap["min_complaints"]:
            problems.append(f"{a['date']}: dataset too small ({a.get('dataset_messages', 0)} messages, {a.get('dataset_complaints', 0)} complaints)")
        ok = attempt_passes(a["metrics"], floors)
        if a.get("passed") is not None and a["passed"] != ok:
            problems.append(f"{a['date']}: recorded passed={a['passed']} disagrees with metrics")
        if prev is not None and not prev["_ok"]:
            if a["dataset_id"] == prev["dataset_id"]:
                problems.append(f"{a['date']}: retry reuses dataset {a['dataset_id']}")
            if a.get("new_share", 0) < policy["min_new_share_of_dataset"]:
                problems.append(f"{a['date']}: retry dataset has {a.get('new_share', 0):.0%} new messages")
            if a.get("new_messages", 0) < policy["min_new_messages"] or a.get("new_complaints", 0) < policy["min_new_complaints"]:
                problems.append(f"{a['date']}: retry adds {a.get('new_messages', 0)} messages / {a.get('new_complaints', 0)} complaints")
            by_cat = a.get("new_complaints_by_category", {})
            thin = [c for c in ("pricing", "quality", "legal", "safety", "refund") if by_cat.get(c, 0) < policy["min_new_complaints_per_category"]]
            if thin:
                problems.append(f"{a['date']}: retry too thin in {thin}")
            gap = (date.fromisoformat(a["date"]) - date.fromisoformat(prev["date"])).days
            if gap < policy["cooldown_days"]:
                problems.append(f"{a['date']}: retry after {gap} days (< {policy['cooldown_days']})")
        streak = 0 if ok else streak + 1
        if streak >= policy["max_consecutive_failures_per_version"]:
            return "rejected", problems
        prev = dict(a, _ok=ok)
    if problems:
        return "invalid", problems
    if prev is None:
        return "candidate", problems
    return ("admitted" if prev["_ok"] else "candidate"), problems
