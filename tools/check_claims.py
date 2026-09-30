#!/usr/bin/env python3
"""Map every specification claim to what enforces it, and COMPUTE its status.

  python tools/check_claims.py            report; exit 1 only if a ref points at nothing
  python tools/check_claims.py --release  also exit 1 if any claim is gap/unbuilt/manual with a fix_before_* decision

A written procedure (doc:) never clears a fix_before_* decision: that a file exists proves nothing ran. A recorded
run does: run:<file>#<run id> needs the file to carry that run id on a line that reports a pass, and makes the claim
"evidenced" (a result that happened, with its id, not a behaviour re-checked on every push).
"""
import re, sys, yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tests_src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "tests").glob("test_*.py"))
sql_tests = (ROOT / "db/tests/rls_isolation_test.sql").read_text(encoding="utf-8")
val_report = (ROOT / "reports/validation_report.txt").read_text(encoding="utf-8") if (ROOT / "reports/validation_report.txt").exists() else ""
workflow = (ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")


def ref_exists(ref):
    kind, _, val = ref.partition(":")
    if kind == "py":
        return re.search(rf"def {re.escape(val)}\(", tests_src) is not None, "here"
    if kind == "sql":
        return re.search(rf"^-- {re.escape(val)}\. ", sql_tests, re.M) is not None and "run_isolation.sh" in workflow, "ci"
    if kind == "val":
        return any(l.startswith("PASS") and val in l for l in val_report.splitlines()), "here"
    if kind == "ci":
        return (ROOT / val).exists() and Path(val).name in workflow, "ci"
    if kind == "doc":
        return (ROOT / val).exists(), "doc"
    if kind == "run":
        path, _, run_id = val.partition("#")
        f = ROOT / path
        ok = bool(run_id) and f.exists() and any(run_id in l and ("PASS" in l or "نجح" in l)
                                                 for l in f.read_text(encoding="utf-8").splitlines())
        return ok, "run"
    return False, "?"


def status(claim, broken):
    """verified_here > ci_only > structural > evidenced > manual > gap; service claims without code are unbuilt."""
    kinds = []
    for r in claim.get("refs", []):
        ok, where = ref_exists(r)
        if not ok:
            broken.append(f"{claim['id']}: {r}")
            continue
        if r.startswith("val:") and claim.get("component") != "reference":
            where = "structural"          # the validator saw the SQL/file, not the behaviour
        kinds.append(where)
    claim["_ci_pending"] = "ci" in kinds       # a production (database/CI) counterpart still waits for its first run
    if claim.get("component") == "service" and not {"here", "ci"} & set(kinds):
        return "unbuilt"
    for k, st in (("here", "verified_here"), ("ci", "ci_only"), ("structural", "structural"), ("run", "evidenced"),
                  ("doc", "manual")):
        if k in kinds:
            return st
    return "gap"


if __name__ == "__main__":
    data = yaml.safe_load((ROOT / "docs/claims.yaml").read_text(encoding="utf-8"))
    broken, rows, counts = [], [], {}
    for c in data["claims"]:
        st = status(c, broken)
        counts[st] = counts.get(st, 0) + 1
        rows.append((c["id"], st, c.get("decision", ""), c["claim"], "ci_pending" if c.get("_ci_pending") else "-"))
    for r in rows:
        print(f"{r[0]:7} {r[1]:14} {r[4]:10} {r[2]:22} {r[3]}")
    print("summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f" · total {len(rows)}"
          + f" · with a CI-only production counterpart {sum(r[4] == 'ci_pending' for r in rows)}")
    if "--json" in sys.argv:
        import json
        out = sys.argv[sys.argv.index("--json") + 1]
        Path(out).write_text(json.dumps([{"id": r[0], "status": r[1], "decision": r[2], "claim": r[3], "ci_pending": r[4] == "ci_pending"}
                                         for r in rows], ensure_ascii=False, indent=1), encoding="utf-8")
    for b in broken:
        print("BROKEN REF", b)
    blocking = [r for r in rows if r[1] in ("gap", "unbuilt", "manual") and r[2].startswith("fix_before")]
    undecided = [r for r in rows if r[1] in ("gap", "unbuilt") and not r[2]]
    for r in undecided:
        print("UNDECIDED", r[0])
    code = 1 if broken or undecided else 0
    if "--release" in sys.argv and blocking:
        print(f"RELEASE BLOCKED by {len(blocking)} claims: " + ", ".join(r[0] for r in blocking))
        code = 1
    sys.exit(code)
