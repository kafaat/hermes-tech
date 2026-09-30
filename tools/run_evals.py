#!/usr/bin/env python3
"""Offline evaluation harness.

  complaints : seed set scored with the acceptance-test metrics of the policy
               (missed complaints, precision, false alarms per 100 non-complaints)
  content    : golden verdicts for the deterministic content guard

The complaint seed set is synthetic and small. It exercises the harness and exposes
failure modes; it does NOT replace the acceptance test on 200 real messages.

Exit codes (v1.7): the deterministic content-guard suite ALWAYS gates (exit 1 on any mismatch).
The complaint suite is REPORT-ONLY in CI: a green CI never means the acceptance criterion passed.
`--gate standing-send` makes the complaint criterion gate; it is the check to run before any
policy that sends replies without per-reply approval.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from complaints import Matcher, load_policy
from content_guard import ContentGuard
import acceptance

ROOT = Path(__file__).resolve().parent.parent


def read(rel):
    return [json.loads(l) for l in (ROOT / rel).read_text(encoding="utf-8").splitlines() if l.strip()]


def complaints_suite():
    pol, m = load_policy(), Matcher()
    rows = read("evals/complaints_seed.jsonl")
    missed, fa, tp, lines = [], 0, 0, []
    for r in rows:
        d = m.route(r["text"], r["channel"])
        flagged = d["kind"] == "complaint"
        if r["is_complaint"]:
            if flagged:
                tp += 1
            else:
                missed.append(r)
        elif flagged:
            fa += 1
    n_c = sum(r["is_complaint"] for r in rows)
    n_n = len(rows) - n_c
    precision = tp / (tp + fa) if tp + fa else 1.0
    fa100 = 100 * fa / n_n if n_n else 0
    ap = acceptance.POLICY
    ub = acceptance.miss_bound(len(missed), n_c)
    crit = [r for r in missed if r["category"] in ap["critical_categories"]]
    ok, reasons = acceptance.judge(len(missed), n_c, len(crit), precision, fa100)
    by_cat_total, by_cat_missed = {}, {}
    for r in rows:
        if r["is_complaint"]:
            by_cat_total[r["category"]] = by_cat_total.get(r["category"], 0) + 1
    for r in missed:
        by_cat_missed[r["category"]] = by_cat_missed.get(r["category"], 0) + 1
    cat_ub = acceptance.category_bounds(by_cat_missed, by_cat_total)
    lines.append(f"complaints seed: {len(rows)} messages ({n_c} complaints, {n_n} non-complaints) · a DEVELOPMENT set: never an acceptance verdict")
    lines.append(f"  recall {tp}/{n_c} = {tp / n_c:.0%} · missed {len(missed)} · miss-rate bound {ub:.1%} "
                 f"(policy {ap['version']}: one-sided {acceptance.attempt_confidence():.1%} per attempt = {ap['path_confidence']:.0%} over "
                 f"{ap['max_attempts_per_version']} attempts; threshold {ap['max_miss_rate_upper']:.0%}; critical misses {len(crit)}; "
                 f"complaints {n_c}/{ap['min_complaints']} required)")
    lines.append(f"  precision {precision:.0%} (threshold {ap['min_precision']:.0%}) · false alarms {fa100:.0f}/100 (threshold {ap['max_false_alarms_per_100_non_complaints']})")
    lines.append("  per-category miss bound (reported, not gated): " + ", ".join(f"{c} {v:.0%}" for c, v in sorted(cat_ub.items())))
    lines.append(f"  verdict: {'PASS' if ok else 'FAIL'} (seed set; REPORT ONLY in CI; gates only with --gate standing-send)"
                 + ("" if ok else " · " + "; ".join(reasons)))
    for r in missed:
        lines.append(f"  missed [{r['category']}] {r['text']}")
    return ok, lines, {"recall": tp / n_c, "missed": len(missed), "miss_upper": ub, "precision": precision, "false_alarms_per_100": fa100}


def content_suite():
    g = ContentGuard()
    rows = read("evals/content_guard_golden.jsonl")
    wrong = [(r, g.verdict(r["text"], r["kind"])) for r in rows if g.verdict(r["text"], r["kind"]) != r["expected"]]
    lines = [f"content guard golden: {len(rows) - len(wrong)}/{len(rows)} verdicts match"]
    lines += [f"  mismatch {r['kind']}: expected {r['expected']} got {v} · {r['text']}" for r, v in wrong]
    return not wrong, lines, {"accuracy": (len(rows) - len(wrong)) / len(rows)}


if __name__ == "__main__":
    results = [complaints_suite(), content_suite()]
    print("\n".join(l for _, ls, _ in results for l in ls))
    if "--json" in sys.argv:
        print(json.dumps({"complaints": results[0][2], "content": results[1][2]}))
    complaints_ok, content_ok = results[0][0], results[1][0]
    code = 0 if content_ok else 1                               # the deterministic guard always gates
    if "--gate" in sys.argv and "standing-send" in sys.argv and not complaints_ok:
        code = 1
    if "--strict" in sys.argv and not (complaints_ok and content_ok):
        code = 1
    print("exit policy: content guard gates CI; complaint acceptance is report-only unless --gate standing-send")
    sys.exit(code)
