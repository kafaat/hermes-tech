"""A green CI must not be read as 'the complaint acceptance criterion passed' (independent review, §5)."""
import json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def run(*args, mutate=None):
    tmp = Path(tempfile.mkdtemp())
    for d in ("tools", "policies", "evals"):
        shutil.copytree(ROOT / d, tmp / d)
    if mutate:
        mutate(tmp)
    p = subprocess.run([sys.executable, str(tmp / "tools/run_evals.py"), *args], capture_output=True, text=True)
    shutil.rmtree(tmp)
    return p.returncode, p.stdout


class TestRunEvalsExit(unittest.TestCase):
    def test_ci_mode_reports_complaint_failure_without_passing_it_off(self):
        code, out = run()
        self.assertEqual(code, 0)
        self.assertIn("REPORT ONLY", out)
        self.assertIn("verdict: FAIL", out)

    def test_standing_send_gate_fails_on_the_same_data(self):
        self.assertEqual(run("--gate", "standing-send")[0], 1)

    def test_content_guard_mismatch_always_fails_ci(self):
        def m(t):
            p = t / "evals/content_guard_golden.jsonl"
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            rows[0]["expected"] = "block" if rows[0]["expected"] != "block" else "pass"
            p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        self.assertEqual(run(mutate=m)[0], 1)


if __name__ == "__main__":
    unittest.main()
