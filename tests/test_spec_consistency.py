"""The specification's current-state numbers and inventories match their sources (review of 1.7, §6)."""
import shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestSpecConsistency(unittest.TestCase):
    def test_every_generated_region_matches_its_source(self):
        p = subprocess.run([sys.executable, "tools/check_spec.py", "--check"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_hand_edited_count_is_caught(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            tree = tmp / "t"
            shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            spec = next(tree.glob("Hermes_Technical_Specification_v*.md"))
            text = spec.read_text(encoding="utf-8")
            start = text.index("<!-- gen:sql_cases -->")
            end = text.index("<!-- /gen:sql_cases -->")
            region = text[start:end]
            spec.write_text(text[:start] + region.replace("ينفّذ ", "ينفّذ 1", 1) + text[end:], encoding="utf-8")
            p = subprocess.run([sys.executable, "tools/check_spec.py", "--check"], cwd=tree, capture_output=True, text=True)
            self.assertEqual(p.returncode, 1)
            self.assertIn("region sql_cases: stale", p.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
