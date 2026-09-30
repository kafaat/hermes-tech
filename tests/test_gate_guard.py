"""The release gate protects itself (review of 1.7, §4): a weakened checker is judged by the base checker,
and the aggregate `gate` job never reads a skipped, cancelled or failed job as a success."""
import json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent


def sh(*args, cwd):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)


def refresh(tree):
    """What a PR author does before committing: reports and manifest for the tree as it is."""
    (tree / "reports/validation_report.txt").write_text(sh(sys.executable, "tools/validate.py", cwd=tree).stdout, encoding="utf-8")
    sh(sys.executable, "tools/build_manifest.py", cwd=tree)


class TestGateGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="guard-test-"))
        cls.repo = cls.tmp / "repo"
        shutil.copytree(ROOT, cls.repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        refresh(cls.repo)
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t"), ("add", "-A"), ("commit", "-qm", "base")):
            sh("git", *a, cwd=cls.repo)
        cls.base = sh("git", "rev-parse", "HEAD", cwd=cls.repo).stdout.strip()
        cls.guard = cls.tmp / "gate_guard.py"
        cls.guard.write_text(sh("git", "show", f"{cls.base}:tools/gate_guard.py", cwd=cls.repo).stdout, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def change(self, edit):
        sh("git", "checkout", "-q", "-B", f"pr{id(edit)}", self.base, cwd=self.repo)
        edit(self.repo)
        refresh(self.repo)
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-qm", "pr", cwd=self.repo)
        return sh(sys.executable, str(self.guard), "--base", self.base, "--repo", str(self.repo), cwd=self.repo)

    def test_weakened_checker_outside_github_is_caught_by_the_base_checker(self):
        def weaken(r):
            p = r / "tools/check_claims.py"
            p.write_text(p.read_text(encoding="utf-8").replace('if __name__ == "__main__":', 'if __name__ == "__main__":\n    sys.exit(0)'), encoding="utf-8")
            c = r / "docs/claims.yaml"
            c.write_text(c.read_text(encoding="utf-8") + '  - {id: X1, section: "0", claim: "x", component: reference, refs: ["py:test_that_does_not_exist"]}\n',
                         encoding="utf-8")
        head_alone = None
        out = self.change(weaken)
        head_alone = sh(sys.executable, "tools/check_claims.py", cwd=self.repo)
        self.assertEqual(head_alone.returncode, 0, "the weakened checker passes its own change")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("base tools/check_claims.py -> FAIL", out.stdout)

    def test_harmless_gate_change_is_accepted_and_other_changes_are_not_judged(self):
        out = self.change(lambda r: (r / "tools/stats.py").write_text((r / "tools/stats.py").read_text(encoding="utf-8") + "\n# note\n", encoding="utf-8"))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        out2 = self.change(lambda r: (r / "README.md").write_text((r / "README.md").read_text(encoding="utf-8") + "\n", encoding="utf-8"))
        self.assertIn("no gate path changed", out2.stdout)


class TestGateJob(unittest.TestCase):
    WF = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8"))

    def run_gate(self, results, tag=False):
        step = self.WF["jobs"]["gate"]["steps"][0]["run"]
        code = re.search(r"<<'PY'\n(.*?)\n\s*PY\s*$", step, re.S).group(1)
        env = dict(os.environ, RESULTS=json.dumps({k: {"result": v} for k, v in results.items()}),
                   IS_TAG="true" if tag else "false", TESTED_SHA="a", HEAD_SHA="a")
        return subprocess.run([sys.executable, "-c", "import textwrap,sys;exec(textwrap.dedent(sys.stdin.read()))"],
                              input=code, env=env, capture_output=True, text=True).returncode

    def test_gate_job_accepts_only_success_of_every_job(self):
        gate = self.WF["jobs"]["gate"]
        self.assertEqual(gate["if"], "always()")
        self.assertEqual(set(gate["needs"]), set(self.WF["jobs"]) - {"gate"})
        ok = {"contracts-and-policy": "success", "database": "success", "workflow-guard": "success", "release-gate": "skipped"}
        self.assertEqual(self.run_gate(ok), 0)
        for job in ("contracts-and-policy", "database", "workflow-guard"):
            for bad in ("skipped", "cancelled", "failure"):
                self.assertEqual(self.run_gate(dict(ok, **{job: bad})), 1, f"{job}={bad}")
        self.assertEqual(self.run_gate(ok, tag=True), 1)                                   # a tag needs the release gate
        self.assertEqual(self.run_gate(dict(ok, **{"release-gate": "success"}), tag=True), 0)
        self.assertEqual(self.run_gate(dict(ok, **{"release-gate": "failure"})), 1)

    def test_every_database_script_runs_in_ci(self):
        steps = " ".join(s.get("run", "") for s in self.WF["jobs"]["database"]["steps"])
        for f in (ROOT / "db/tests").glob("*.sh"):
            if f.name != "run_local.sh":
                self.assertIn(f.name, steps)


if __name__ == "__main__":
    unittest.main()
