#!/usr/bin/env python3
"""Judge a change to the release gate with the gate AS IT WAS before the change (v1.8, review of 1.7 §4).

CI runs THIS FILE FROM THE BASE COMMIT (git show "$base:tools/gate_guard.py"), never from the PR:

  1. list the files the change touches; if none is a gate path, stop (nothing to judge);
  2. build a temporary tree = the PR's files, with every checker program replaced by the BASE version;
  3. run the base checkers on the PR's data: a PR that weakens a checker (anywhere, not only under
     .github/) is judged by the checker it tried to weaken; policy/claims/inventory edits must still
     satisfy the unweakened rules;
  4. changes under .github/ or to dependencies also run the supply-chain check in --strict mode;
  5. the gate-path diff is written to the job summary for the owner to read.
A new checker introduced by the PR has no base version and is not trusted until it is on the base branch.
"""
from __future__ import annotations
import argparse, os, shutil, subprocess, sys, tempfile
from pathlib import Path

GATE_PATHS = (".github/", "tools/", "policies/", "evals/", "db/migrations/", "db/tests/", "db/local/", "docs/claims.yaml",
              "docs/error_codes.yaml", "docs/security_definer_inventory.md", "requirements-ci.txt", "VERSION")
# MANIFEST.json is not a gate path by itself (it changes with every commit); the guard checks it whenever it runs.
CHECKERS = ("validate.py", "validate_schema.py", "sql_state.py", "check_claims.py", "check_supply_chain.py", "check_spec.py",
            "run_evals.py", "acceptance.py", "stats.py", "admission.py", "build_manifest.py", "derive.py", "gate_guard.py")
RUNS = [["tools/validate.py"], ["tools/derive.py", "--check"], ["tools/check_claims.py"], ["tools/check_spec.py", "--check"],
        ["tools/run_evals.py"]]


def manifest_problems(tree: Path) -> list[str]:
    """The base guard's own manifest check, on the change's tree BEFORE any checker is swapped (a PR cannot
    weaken it by editing tools/build_manifest.py)."""
    import hashlib, json
    m = json.loads((tree / "MANIFEST.json").read_text(encoding="utf-8"))
    actual = {str(p.relative_to(tree)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(tree.rglob("*"))
              if p.is_file() and p.name != "MANIFEST.json" and not ({"__pycache__", ".git"} & set(p.relative_to(tree).parts))}
    sid = hashlib.sha256("\n".join(f"{k} {v}" for k, v in sorted(actual.items())).encode()).hexdigest()
    diff = sorted({k for k, _ in set(m["files"].items()) ^ set(actual.items())})
    return diff[:20] + (["source_id"] if m.get("source_id") != sid else [])


def git(*args, cwd="."):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    changed = [l for l in git("diff", "--name-only", a.base, "HEAD", cwd=repo).splitlines() if l]
    gate = [f for f in changed if f.startswith(GATE_PATHS) and "__pycache__" not in f]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not gate:
        print("gate guard: no gate path changed")
        return 0
    report = ["### Gate paths changed (judged by the base checkers)", *[f"- `{f}`" for f in gate]]
    if summary:
        Path(summary).open("a", encoding="utf-8").write("\n".join(report) + "\n")
    print("\n".join(report))
    tmp = Path(tempfile.mkdtemp(prefix="gate-"))
    try:
        shutil.copytree(repo, tmp / "tree", ignore=shutil.ignore_patterns(".git"))
        tree = tmp / "tree"
        failed = []
        mp = manifest_problems(tree)
        print("gate guard: base manifest check ->", "ok" if not mp else f"FAIL {mp}")
        if mp:
            failed.append("manifest")
        for name in CHECKERS:
            try:
                src = git("show", f"{a.base}:tools/{name}", cwd=repo)
            except subprocess.CalledProcessError:
                if (tree / "tools" / name).exists():
                    print(f"gate guard: tools/{name} is new in this change; it is not trusted until merged, removing it")
                    (tree / "tools" / name).unlink()
                continue
            (tree / "tools" / name).write_text(src, encoding="utf-8")
        runs = [r for r in RUNS if (tree / r[0]).exists()]
        if any(f.startswith((".github/", "requirements")) for f in gate):
            runs.append(["tools/check_supply_chain.py", "--strict"])
        for r in runs:
            p = subprocess.run([sys.executable, *r], cwd=tree, capture_output=True, text=True)
            status = "ok" if p.returncode == 0 else "FAIL"
            print(f"gate guard: base {' '.join(r)} -> {status}")
            if p.returncode:
                failed.append(" ".join(r))
                print(p.stdout[-3000:], p.stderr[-2000:])
        if failed:
            print(f"gate guard: FAILED ({len(failed)}): " + "; ".join(failed))
            return 1
        print("gate guard: the base checkers accept this change")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
