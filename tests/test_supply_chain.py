import shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def run(mutate):
    tmp = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / ".github", tmp / ".github")
    shutil.copytree(ROOT / "tools", tmp / "tools")
    shutil.copy(ROOT / "requirements-ci.txt", tmp / "requirements-ci.txt")
    mutate(tmp)
    p = subprocess.run([sys.executable, str(tmp / "tools/check_supply_chain.py")], capture_output=True, text=True)
    shutil.rmtree(tmp)
    return p.returncode, p.stdout


class TestSupplyChain(unittest.TestCase):
    def test_baseline_has_no_fail(self):
        code, out = run(lambda t: None)
        self.assertEqual(code, 0, out)

    def test_missing_codeowners_fails(self):
        code, out = run(lambda t: (t / ".github/CODEOWNERS").unlink())
        self.assertEqual(code, 1)
        self.assertIn("CODEOWNERS does not protect /.github/", out)

    def test_pull_request_target_fails(self):
        def m(t):
            p = t / ".github/workflows/validate.yml"
            p.write_text(p.read_text().replace("on: [push, pull_request]", "on: [push, pull_request_target]"))
        self.assertEqual(run(m)[0], 1)


class TestSupplyChainParsing(unittest.TestCase):
    def test_comment_mentioning_uses_is_not_an_action(self):
        code, out = run(lambda t: None)
        self.assertNotIn("SHA: `", out)
        self.assertNotIn("SHA: lines", out)


if __name__ == "__main__":
    unittest.main()
