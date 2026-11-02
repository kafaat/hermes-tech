#!/usr/bin/env python3
"""No test is skipped, disabled or removed to get green (rule FI-NO-SKIP of the failure-investigation skill).

  python tools/test_guard.py                 exit 1 if a file under tests/ carries a skip or disable marker
  python tools/test_guard.py --base <sha>    also exit 1 if a test present at <sha> is missing from the tree, unless
                                             a commit in <sha>..HEAD ends its message with the trailer
                                               Tests-Removed: <test id> — <reason>
Markers, as a name, an attribute or an imported name anywhere under tests/: unittest skip, skipIf, skipUnless,
skipTest, SkipTest, expectedFailure; pytest skip, skipif, xfail, importorskip and any mark.skip*. A test defined
twice in one scope is disabled too: Python keeps the last definition and the first never runs.
A test is identified statically (ast), never by importing it: tests/<file>.py::<Class>::<method> or
tests/<file>.py::<function>, for each function named test* at module level or in a module-level class of every
tests/**/test*.py (the unittest discovery pattern). A rename is a removal plus an addition.
Unreadable input (a file that does not parse, a base that is not a commit, git failing) fails closed: exit 1.
CI runs this file from the BASE commit in workflow-guard, the way it runs gate_guard.py, so a change cannot weaken
the guard that judges it; contracts-and-policy also runs the marker scan where there is no base (weekly runs).

Other repositories reuse this file at a pinned commit instead of copying it (spec 28.45):
  --tests DIR      a test directory relative to --repo, repeatable; default: tests
  --allow FILE     JSON {"allowed_markers": [{"file", "marker", "count", "reason"}]}: exactly `count` markers of that
                   kind in that file are excused and printed with their reason. More or fewer fails, so the list
                   cannot outlive the markers it names. An entry without a reason, or an unreadable file, fails.
Hermes itself runs it with neither option, so the behaviour above is unchanged here.
"""
from __future__ import annotations
import argparse, ast, json, re, subprocess, sys
from collections import Counter
from pathlib import Path

RULE = "FI-NO-SKIP: no test is skipped, disabled or removed to get green"
MARKERS = {"skip", "skipIf", "skipUnless", "skipTest", "SkipTest", "expectedFailure",   # unittest
           "skipif", "xfail", "importorskip"}                                             # pytest (and mark.skip*)
TRAILER = "Tests-Removed"
TRAILER_VALUE = re.compile(r"^(\S+)\s+(?:—|–|--?)\s+(\S.*)$")   # <test id> — <reason>; the reason is required


class Unreadable(Exception):
    """Input the guard cannot judge; it is a failure, never a pass."""


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise Unreadable(f"git {' '.join(args)}: {getattr(e, 'stderr', '') or e}".strip()) from e


def parse(rel: str, src: str) -> ast.Module:
    try:
        return ast.parse(src, filename=rel)
    except (SyntaxError, ValueError) as e:
        raise Unreadable(f"{rel}: does not parse ({e})") from e


def is_test_file(rel: str, dirs: tuple[str, ...] = ("tests",)) -> bool:
    return any(rel.startswith(d + "/") for d in dirs) and Path(rel).name.startswith("test") and rel.endswith(".py")


def test_dirs(values: list[str] | None) -> tuple[str, ...]:
    raw = [v.strip() for v in (values or ["tests"])]
    for v in raw:
        if not v.rstrip("/") or v.startswith("/") or ".." in Path(v).parts:
            raise Unreadable(f"--tests {v!r} is not a directory inside the repository")
    return tuple(dict.fromkeys(v.rstrip("/") for v in raw))


def allowances(path: str | None) -> list[dict]:
    """The exact markers a repository accepts, each with a reason and a count."""
    if not path:
        return []
    try:
        entries = json.loads(Path(path).read_text(encoding="utf-8"))["allowed_markers"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Unreadable(f"--allow {path}: {e}") from e
    out = []
    for i, e in enumerate(entries if isinstance(entries, list) else [None]):
        ok = (isinstance(e, dict) and isinstance(e.get("file"), str) and isinstance(e.get("marker"), str)
              and isinstance(e.get("reason"), str) and e["reason"].strip()
              and isinstance(e.get("count", 1), int) and e.get("count", 1) >= 1)
        if not ok:
            raise Unreadable(f"--allow {path}: entry {i} needs file, marker, a reason and a positive count")
        out.append({**e, "count": e.get("count", 1)})
    return out


def is_test(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def holds_tests(node: ast.AST) -> bool:
    return is_test(node) or (isinstance(node, ast.ClassDef) and any(is_test(b) for b in node.body))


def test_ids(rel: str, tree: ast.Module) -> list[str]:
    ids = []
    for node in tree.body:
        if is_test(node):
            ids.append(f"{rel}::{node.name}")
        elif isinstance(node, ast.ClassDef):
            ids += [f"{rel}::{node.name}::{b.name}" for b in node.body if is_test(b)]
    return ids


def redefinitions(rel: str, tree: ast.Module) -> list[str]:
    """A test (or a class holding tests) defined twice in one scope: only the last definition is ever run."""
    out = []
    scopes = [(rel, tree.body)] + [(f"{rel}::{n.name}", n.body) for n in tree.body if isinstance(n, ast.ClassDef)]
    for where, body in scopes:
        defs = [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        twice = {k for k, v in Counter(n.name for n in defs).items() if v > 1}
        for name in sorted(twice):
            same = [n for n in defs if n.name == name]
            if any(holds_tests(n) for n in same):
                out.append(f"{where}::{name} is defined {len(same)} times (lines "
                           f"{', '.join(str(n.lineno) for n in same)}); only the last one runs")
    return out


def markers(tree: ast.Module) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in MARKERS:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and (node.attr in MARKERS or (
                node.attr.startswith("skip") and isinstance(node.value, ast.Attribute) and node.value.attr == "mark")):
            found.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found += [(node.lineno, f"import {a.name}") for a in node.names if a.name.rsplit(".", 1)[-1] in MARKERS]
    return sorted(set(found))


def head_files(repo: Path, dirs: tuple[str, ...] = ("tests",)) -> dict[str, str]:
    """Every .py file under the test directories in the tree being judged, as text."""
    out = {}
    for d in dirs:
        tests = repo / d
        if not tests.is_dir():
            raise Unreadable(f"no {d}/ directory under {repo}")
        for p in sorted(tests.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(repo).as_posix()
            try:
                out[rel] = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise Unreadable(f"{rel}: {e}") from e
    return out


def base_ids(repo: Path, base: str, dirs: tuple[str, ...] = ("tests",)) -> list[str]:
    paths = [f"{d}/" for d in dirs]
    names = [n for n in git(repo, "ls-tree", "-r", "-z", "--name-only", "--full-name", base, "--", *paths).split("\0") if n]
    ids = []
    for rel in names:
        if is_test_file(rel, dirs):
            ids += test_ids(rel, parse(f"{base[:12]}:{rel}", git(repo, "show", f"{base}:{rel}")))
    return ids


def removal_trailers(repo: Path, base: str) -> tuple[dict[str, str], list[str]]:
    """Tests-Removed trailers of the commits in base..HEAD, read by git's own trailer parser (the message's last
    paragraph). Returns {test id: reason} and the trailers that cannot excuse anything."""
    out = git(repo, "log", f"--format=%h%x00%(trailers:key={TRAILER},valueonly,unfold)%x1e", f"{base}..HEAD")
    named, bad = {}, []
    for record in out.split("\x1e"):
        sha, _, values = record.strip("\n").partition("\0")
        for v in (v.strip() for v in values.splitlines()):
            if not v:
                continue
            m = TRAILER_VALUE.match(v)
            if m:
                named[m.group(1)] = m.group(2)
            else:
                bad.append(f"commit {sha}: '{TRAILER}: {v}' is not '<test id> — <reason>'")
    return named, bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=RULE)
    ap.add_argument("--base", help="a test present at this commit must still exist, or be named in a Tests-Removed trailer")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--tests", action="append", metavar="DIR", help="test directory relative to --repo, repeatable; default: tests")
    ap.add_argument("--allow", metavar="FILE", help="JSON list of exact markers this repository accepts, each with a reason")
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    fails: list[str] = []
    trees: dict = {}
    dirs: tuple[str, ...] = ("tests",)
    try:
        dirs, allowed = test_dirs(a.tests), allowances(a.allow)
        files = head_files(repo, dirs)
        ids = []
        for rel, src in files.items():
            try:
                trees[rel] = parse(rel, src)
            except Unreadable as e:
                fails.append(f"unreadable: {e}")
        found: dict[tuple[str, str], list[int]] = {}
        for rel, tree in trees.items():
            for line, what in markers(tree):
                found.setdefault((rel, what), []).append(line)
            if is_test_file(rel, dirs):
                ids += test_ids(rel, tree)
                fails += [f"disabled by redefinition: {r}" for r in redefinitions(rel, tree)]
        for e in allowed:
            lines = found.pop((e["file"], e["marker"]), [])
            if len(lines) == e["count"]:
                print(f"test guard: allowed marker {e['file']}:{','.join(map(str, lines))} `{e['marker']}` — {e['reason']}")
            elif not lines:
                fails.append(f"stale allowance: no `{e['marker']}` in {e['file']}; remove the entry")
            else:
                fails.append(f"{e['file']}: {len(lines)} `{e['marker']}` marker(s) at line(s) "
                             f"{', '.join(map(str, lines))}, the allowance names {e['count']}")
        fails += [f"{rel}:{line}: skip or disable marker `{what}`"
                  for (rel, what), lines in sorted(found.items()) for line in lines]
        note = ""
        if a.base:
            try:
                git(repo, "rev-parse", "--verify", "--quiet", f"{a.base}^{{commit}}")
            except Unreadable as e:
                raise Unreadable(f"--base {a.base} is not a commit in {repo}") from e
            gone = sorted(set(base_ids(repo, a.base, dirs)) - set(ids))
            named, bad = removal_trailers(repo, a.base)
            fails += [f"Tests-Removed trailer unusable, {b}" for b in bad]
            fails += [f"removed without a {TRAILER} trailer: {t}" for t in gone if t not in named]
            excused = [t for t in gone if t in named]
            note = (f"; {len(excused)} removed since {a.base[:12]}, each named in a {TRAILER} trailer" if excused
                    else f"; none removed since {a.base[:12]}")
            for t in excused:
                print(f"test guard: removed with a reason: {t} — {named[t]}")
    except Unreadable as e:
        fails.append(f"unreadable: {e}")
        note = ""
    for f in fails:
        print("FAIL", f)
    if fails:
        print(f"test guard: {len(fails)} violation(s) of {RULE}")
        if any(f.startswith("removed without") for f in fails):
            print(f"  a test removed or renamed on purpose is named, one per line, at the end of a commit message in "
                  f"base..HEAD:\n  {TRAILER}: <test id> — <reason>")
        return 1
    print(f"test guard: {len(ids)} tests in {sum(1 for r in trees if is_test_file(r, dirs))} modules, "
          f"no {'unallowed ' if a.allow else ''}skip or disable marker{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
