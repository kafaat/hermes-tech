"""FI-NO-SKIP (spec 28.43): no test is skipped, disabled or removed to get green. Each case builds a small git
repository, changes its tests the way a change would, and runs tools/test_guard.py on it as CI does."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "tools/test_guard.py"

HEADER = "import unittest\nimport pytest\n{imports}\n\n"
CLASS = ("class TestA(unittest.TestCase):\n{decorator}    def test_one(self):\n{body}        self.assertTrue(True)\n\n"
         "    def test_two(self):\n        self.assertTrue(True)\n\n\n")
FREE = "def test_free():\n    assert True\n"


def module(imports="", decorator="", body="", cls=CLASS, free=FREE):
    return HEADER.format(imports=imports) + cls.format(decorator=decorator, body=body) + free


# every marker the rule names, written the way it is used: (what the guard must name, the change)
MARKER_KINDS = [
    ("unittest.skip", dict(decorator="    @unittest.skip('flaky')\n")),
    ("unittest.skipIf", dict(decorator="    @unittest.skipIf(True, 'not today')\n")),
    ("unittest.skipUnless", dict(decorator="    @unittest.skipUnless(False, 'not here')\n")),
    ("self.skipTest", dict(body="        self.skipTest('later')\n")),
    ("unittest.SkipTest", dict(body="        raise unittest.SkipTest('later')\n")),
    ("unittest.expectedFailure", dict(decorator="    @unittest.expectedFailure\n")),
    ("import skip", dict(imports="from unittest import skip\n")),
    ("pytest.skip", dict(body="        pytest.skip('later')\n")),
    ("pytest.xfail", dict(body="        pytest.xfail('known')\n")),
    ("pytest.importorskip", dict(body="        pytest.importorskip('numpy')\n")),
    ("pytest.mark.skip", dict(decorator="    @pytest.mark.skip(reason='flaky')\n")),
    ("pytest.mark.skipif", dict(decorator="    @pytest.mark.skipif(True, reason='x')\n")),
    ("pytest.mark.xfail", dict(decorator="    @pytest.mark.xfail\n")),
    ("pytest.mark.skip_on_ci", dict(decorator="    @pytest.mark.skip_on_ci\n")),
]


class Repo:
    def __init__(self):
        self.path = Path(tempfile.mkdtemp(prefix="test-guard-"))
        # no user or system git config, and no repository above this one (the not-a-repository case below)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CEILING_DIRECTORIES=str(self.path.parent))
        self.git("init", "-q")
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            self.git("config", k, v)
        self.write("tests/test_a.py", module())
        self.base = self.commit("base")

    def git(self, *args):
        p = subprocess.run(["git", *args], cwd=self.path, env=self.env, capture_output=True, text=True)
        if p.returncode:
            raise AssertionError(f"git {args}: {p.stderr}")
        return p.stdout.strip()

    def write(self, rel, text):
        (self.path / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.path / rel).write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def reset(self):
        self.git("checkout", "-q", "-f", self.base)
        self.git("clean", "-qfdx")

    def guard(self, base=None, *extra):
        args = [sys.executable, str(GUARD), "--repo", str(self.path)] + (["--base", base] if base else []) + list(extra)
        return subprocess.run(args, cwd=self.path, env=self.env, capture_output=True, text=True)


class TestTestGuard(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(shutil.rmtree, self.repo.path, True)

    def assertFails(self, p, *names):
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        for n in names:
            self.assertIn(n, p.stdout)

    def assertPasses(self, p, *names):
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for n in names:
            self.assertIn(n, p.stdout)

    def test_the_repository_itself_has_no_marker_and_no_redefinition(self):
        p = subprocess.run([sys.executable, str(GUARD), "--repo", str(ROOT)], capture_output=True, text=True)
        self.assertPasses(p, "no skip or disable marker")

    def test_an_untouched_tree_passes(self):
        self.assertPasses(self.repo.guard(), "3 tests in 1 modules")
        self.repo.write("README.md", "unrelated\n")
        self.repo.commit("unrelated change")
        self.assertPasses(self.repo.guard(self.repo.base), "none removed")

    def test_every_skip_marker_kind_fails(self):
        for name, change in MARKER_KINDS:
            with self.subTest(name):
                self.repo.reset()
                self.repo.write("tests/test_a.py", module(**change))
                self.repo.commit(f"add {name}")
                self.assertFails(self.repo.guard(), "tests/test_a.py:", f"`{name}", "FI-NO-SKIP")
                self.assertFails(self.repo.guard(self.repo.base), f"`{name}")

    def test_a_marker_anywhere_under_tests_fails_and_ordinary_words_do_not(self):
        self.repo.write("tests/helpers.py", "import unittest\n\ndef later():\n    raise unittest.SkipTest('x')\n")
        self.assertFails(self.repo.guard(), "tests/helpers.py:4: skip or disable marker `unittest.SkipTest`")
        self.repo.reset()
        self.repo.write("tests/test_a.py", module(body="        skipped = ['skip', 'xfail']\n        self.assertEqual(len(skipped), 2)\n")
                        + "\n\ndef test_the_echoes_are_skipped():\n    assert 'skipTest'\n")
        self.assertPasses(self.repo.guard(), "4 tests")

    def test_a_test_defined_twice_is_disabled(self):
        twice = CLASS.replace("    def test_two(self):", "    def test_one(self):", 1)
        self.repo.write("tests/test_a.py", module(cls=twice))
        self.assertFails(self.repo.guard(), "disabled by redefinition: tests/test_a.py::TestA::test_one is defined 2 times")
        self.repo.write("tests/test_a.py", module() + "\n\nclass TestA:\n    pass\n")
        self.assertFails(self.repo.guard(), "tests/test_a.py::TestA is defined 2 times")

    def test_a_removed_test_needs_a_trailer_with_a_reason(self):
        gone = "tests/test_a.py::TestA::test_two"
        without_two = CLASS.split("    def test_two")[0].rstrip() + "\n\n\n"
        self.repo.write("tests/test_a.py", module(cls=without_two))
        self.repo.commit("tidy the tests")
        self.assertFails(self.repo.guard(self.repo.base), f"removed without a Tests-Removed trailer: {gone}",
                         "Tests-Removed: <test id> — <reason>")
        self.assertPasses(self.repo.guard(), "2 tests")              # no base given: removals are not judged
        self.repo.commit(f"name another test\n\nTests-Removed: tests/test_a.py::TestA::test_one — wrong one")
        self.assertFails(self.repo.guard(self.repo.base), gone)
        self.repo.commit(f"no reason\n\nTests-Removed: {gone}")
        self.assertFails(self.repo.guard(self.repo.base), f"'Tests-Removed: {gone}' is not '<test id> — <reason>'", gone)

    def test_a_removed_test_named_in_a_trailer_with_a_reason_passes(self):
        gone = "tests/test_a.py::TestA::test_two"
        self.repo.write("tests/test_a.py", module(cls=CLASS.split("    def test_two")[0].rstrip() + "\n\n\n"))
        self.repo.commit(f"drop a duplicate\n\nTests-Removed: {gone} — test_one asserts the same thing\n"
                         "Co-Authored-By: t <t@t>")
        self.assertPasses(self.repo.guard(self.repo.base), "1 removed since", f"{gone} — test_one asserts the same thing")

    def test_a_trailer_outside_the_range_does_not_count(self):
        gone = "tests/test_a.py::test_free"
        early = self.repo.commit(f"announce\n\nTests-Removed: {gone} — later")
        self.repo.write("tests/test_a.py", module(free=""))
        self.repo.commit("drop it")
        self.assertFails(self.repo.guard(early), f"removed without a Tests-Removed trailer: {gone}")

    def test_a_deleted_module_names_each_of_its_tests(self):
        (self.repo.path / "tests/test_a.py").unlink()
        self.repo.write("tests/test_b.py", module())
        self.repo.commit("move")
        p = self.repo.guard(self.repo.base)
        self.assertFails(p, "tests/test_a.py::TestA::test_one", "tests/test_a.py::TestA::test_two", "tests/test_a.py::test_free")
        self.assertEqual(p.stdout.count("removed without a Tests-Removed trailer"), 3, p.stdout)

    def test_a_rename_is_a_removal_plus_an_addition(self):
        old = "tests/test_a.py::TestA::test_two"
        self.repo.write("tests/test_a.py", module(cls=CLASS.replace("def test_two", "def test_two_renamed")))
        self.repo.commit("rename")
        p = self.repo.guard(self.repo.base)
        self.assertFails(p, f"removed without a Tests-Removed trailer: {old}")
        self.assertNotIn("test_two_renamed", p.stdout)
        self.repo.commit(f"name the rename\n\nTests-Removed: {old} — renamed to test_two_renamed")
        self.assertPasses(self.repo.guard(self.repo.base), "3 tests", "1 removed since")

    def test_unreadable_input_fails_closed(self):
        self.repo.write("tests/test_a.py", module() + "\ndef test_broken(:\n")
        self.assertFails(self.repo.guard(), "unreadable: tests/test_a.py: does not parse")
        self.repo.reset()
        (self.repo.path / "tests/test_c.py").write_bytes(b"\xff\xfe not text")
        self.assertFails(self.repo.guard(), "unreadable: tests/test_c.py")
        self.repo.reset()
        self.assertFails(self.repo.guard("0" * 40), "is not a commit")
        self.assertFails(self.repo.guard("no-such-ref"), "is not a commit")
        shutil.rmtree(self.repo.path / "tests")
        self.assertFails(self.repo.guard(), "unreadable: no tests/ directory")
        shutil.rmtree(self.repo.path / ".git")
        self.repo.write("tests/test_a.py", module())
        self.assertFails(self.repo.guard("HEAD"), "unreadable")


class TestTestGuardElsewhere(unittest.TestCase):
    """Another repository reuses the guard at a pinned commit (spec 28.45): its own test directories, and an exact
    list of the markers it accepts, each with a reason."""

    def setUp(self):
        self.repo = Repo()
        self.addCleanup(shutil.rmtree, self.repo.path, True)
        for d in ("backend/tests", "adapters/tests"):
            self.repo.write(f"{d}/test_x.py", module())
        self.repo.base = self.repo.commit("two more test directories")
        self.dirs = ("--tests", "backend/tests", "--tests", "adapters/tests")

    def allow(self, *entries):
        self.repo.write("allow.json", json.dumps({"allowed_markers": list(entries)}))
        return ("--allow", str(self.repo.path / "allow.json"))

    def test_other_test_directories_are_scanned_and_judged(self):
        self.assertPasses(self.repo.guard(None, *self.dirs), "6 tests in 2 modules")
        self.repo.write("adapters/tests/test_x.py", module(decorator="    @pytest.mark.skip(reason='flaky')\n"))
        self.assertFails(self.repo.guard(None, *self.dirs), "adapters/tests/test_x.py:", "`pytest.mark.skip`")
        self.repo.reset()
        self.repo.write("backend/tests/test_x.py", module(free=""))
        self.repo.commit("drop one")
        self.assertFails(self.repo.guard(self.repo.base, *self.dirs),
                         "removed without a Tests-Removed trailer: backend/tests/test_x.py::test_free")
        self.assertFails(self.repo.guard(None, "--tests", "missing/tests"), "unreadable: no missing/tests/ directory")
        for bad in ("../outside", "/etc"):
            self.assertFails(self.repo.guard(None, "--tests", bad), "is not a directory inside the repository")

    def test_an_allowance_excuses_exactly_its_markers(self):
        skipping = "        pytest.skip('Docker absent')\n"
        entry = {"file": "adapters/tests/test_x.py", "marker": "pytest.skip", "count": 1, "reason": "CI requires Docker"}
        self.repo.write("adapters/tests/test_x.py", module(body=skipping))
        self.assertFails(self.repo.guard(None, *self.dirs), "`pytest.skip`")
        self.assertPasses(self.repo.guard(None, *self.dirs, *self.allow(entry)),
                          "allowed marker adapters/tests/test_x.py:", "CI requires Docker", "no unallowed skip")
        self.repo.write("adapters/tests/test_x.py", module(body=skipping) + "\n\ndef test_more():\n    pytest.skip('x')\n")
        self.assertFails(self.repo.guard(None, *self.dirs, *self.allow(entry)), "2 `pytest.skip` marker(s)", "names 1")
        self.repo.write("adapters/tests/test_x.py", module())
        self.assertFails(self.repo.guard(None, *self.dirs, *self.allow(entry)), "stale allowance: no `pytest.skip`")

    def test_an_unusable_allowance_fails_closed(self):
        for entry in ({"file": "a.py", "marker": "pytest.skip", "reason": " "},
                      {"file": "a.py", "marker": "pytest.skip", "reason": "x", "count": 0},
                      {"marker": "pytest.skip", "reason": "x"}):
            with self.subTest(entry=entry):
                self.assertFails(self.repo.guard(None, *self.dirs, *self.allow(entry)), "needs file, marker, a reason")
        self.assertFails(self.repo.guard(None, "--allow", str(self.repo.path / "absent.json")), "unreadable: --allow")

    assertFails = TestTestGuard.assertFails
    assertPasses = TestTestGuard.assertPasses


class TestTestGuardInCI(unittest.TestCase):
    JOBS = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8"))["jobs"]

    def test_ci_runs_the_base_guard_on_every_change(self):
        guard = [s["run"] for s in self.JOBS["workflow-guard"]["steps"] if "test_guard.py" in s.get("run", "")]
        self.assertEqual(len(guard), 1, "workflow-guard runs the test guard in a step of its own")
        self.assertIn('git show "$base:tools/test_guard.py"', guard[0])       # the program comes from the base
        self.assertIn('--base "$base"', guard[0])                             # removals are judged
        self.assertNotIn("gate_guard", guard[0])                              # not only when a gate path changed
        runs = [s.get("run", "") for s in self.JOBS["contracts-and-policy"]["steps"]]
        self.assertIn("python tools/test_guard.py", [r.split("#")[0].strip() for r in runs])  # markers, even weekly


if __name__ == "__main__":
    unittest.main()
