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



class TestWorkflowGuardBase(unittest.TestCase):
    """The base whose checkers judge a change (spec 28.44) is trusted: never a branch's own previous head."""
    STEPS = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8"))["jobs"]["workflow-guard"]["steps"]

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="guard-base-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CEILING_DIRECTORIES=str(self.repo.parent))
        self.git("init", "-q", "-b", "main")
        for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
            self.git("config", k, v)
        # main: c0, then m1 (a checker change); feature: f1 from c0, then f2 = main merged in
        self.c0 = self.commit("c0")
        self.git("checkout", "-q", "-b", "feature")
        self.f1 = self.commit("f1")
        self.git("checkout", "-q", "main")
        self.m1 = self.commit("m1: a new checker")
        self.git("update-ref", "refs/remotes/origin/main", self.m1)
        self.git("checkout", "-q", "feature")
        self.git("merge", "-q", "--no-edit", "main")
        self.f2 = self.git("rev-parse", "HEAD")

    def git(self, *args):
        p = subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def commit(self, message):
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def base(self, ref, pr_base="", before="", default="main", head=None):
        self.git("checkout", "-q", head or self.f2)
        step = next(s for s in self.STEPS if s.get("id") == "base")
        out = self.repo / "github_output"
        out.write_text("")
        env = dict(self.env, PR_BASE=pr_base, BEFORE=before, DEFAULT=default, GITHUB_REF=ref, GITHUB_OUTPUT=str(out))
        p = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return dict(line.split("=", 1) for line in out.read_text().splitlines())["base"]

    def test_a_branch_push_after_merging_main_is_judged_from_main_not_from_its_previous_head(self):
        self.assertEqual(self.base("refs/heads/feature", before=self.f1), self.m1)

    def test_a_first_branch_push_is_judged_from_its_fork_point(self):
        self.assertEqual(self.base("refs/heads/feature", before="0" * 40, head=self.f1), self.c0)

    def test_a_pull_request_is_judged_from_its_base_and_a_push_to_main_from_the_commit_before(self):
        self.assertEqual(self.base("refs/pull/5/merge", pr_base=self.m1, before=self.f1), self.m1)
        self.assertEqual(self.base("refs/heads/main", before=self.c0, head=self.m1), self.c0)

    def test_a_tag_or_a_scheduled_run_has_no_base(self):
        self.assertEqual(self.base("refs/tags/v1.8", before="0" * 40), "")
        self.assertEqual(self.base("refs/heads/main", default=""), "")

    def test_every_guard_reads_that_base_and_nothing_else(self):
        runs = [s["run"] for s in self.STEPS if "_guard.py" in s.get("run", "")]
        self.assertEqual(len(runs), 3)                   # gate_guard, test_guard (28.43), single_writer_guard (28.48)
        for run in runs:
            self.assertIn('base="${{ steps.base.outputs.base }}"', run)
            self.assertNotIn("github.event", run)


if __name__ == "__main__":
    unittest.main()
