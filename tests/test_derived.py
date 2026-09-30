import subprocess, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


class TestDerived(unittest.TestCase):
    def test_derived_files_are_current(self):
        p = subprocess.run([sys.executable, str(ROOT / "tools/derive.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)


if __name__ == "__main__":
    unittest.main()
