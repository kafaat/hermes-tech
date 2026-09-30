import shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def run(mutate, *args):
    tmp = Path(tempfile.mkdtemp())
    for d in ("docs", "tests", "db", "reports", ".github", "tools", "ops"):
        shutil.copytree(ROOT / d, tmp / d)
    shutil.copy(ROOT / "requirements-ci.txt", tmp / "requirements-ci.txt")
    mutate(tmp)
    p = subprocess.run([sys.executable, str(tmp / "tools/check_claims.py"), *args], capture_output=True, text=True)
    shutil.rmtree(tmp)
    return p.returncode, p.stdout


def append_claim(line):
    def m(t):
        p = t / "docs/claims.yaml"
        p.write_text(p.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")
    return m


class TestClaims(unittest.TestCase):
    def test_baseline_reports_without_broken_refs(self):
        code, out = run(lambda t: None)
        self.assertEqual(code, 0, out)
        self.assertNotIn("BROKEN REF", out)

    def test_ref_to_a_missing_test_is_broken(self):
        code, out = run(append_claim('  - {id: CX, section: "x", claim: "x", component: reference, refs: ["py:test_that_does_not_exist"]}'))
        self.assertEqual(code, 1)
        self.assertIn("BROKEN REF CX", out)

    def test_gap_without_decision_fails(self):
        code, out = run(append_claim('  - {id: CY, section: "x", claim: "x", component: database, refs: []}'))
        self.assertEqual(code, 1)
        self.assertIn("UNDECIDED CY", out)

    def test_release_is_blocked_by_fix_before_claims(self):
        code, out = run(lambda t: None, "--release")
        self.assertEqual(code, 1)
        self.assertIn("RELEASE BLOCKED", out)

    def test_status_is_computed_not_written(self):
        # a database claim backed only by the validator is structural, even if someone calls it verified
        code, out = run(append_claim('  - {id: CZ, section: "x", claim: "x", component: database, refs: ["val:RLS forced"], status: verified_here}'))
        line = next(l for l in out.splitlines() if l.startswith("CZ"))
        self.assertIn("structural", line)


if __name__ == "__main__":
    unittest.main()
