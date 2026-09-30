import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from audit_checkpoint import make, verify

K = b"k" * 32
H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64


class TestAuditCheckpoint(unittest.TestCase):
    def setUp(self):
        self.cps = [make(10, H1, K, "2026-10-01T00:00:00Z"), make(25, H2, K, "2026-10-02T00:00:00Z")]

    def test_extended_chain_verifies(self):
        self.assertEqual(verify(self.cps, {10: H1, 25: H2, 40: H3}, K), [])

    def test_rebuilt_chain_is_detected(self):
        probs = verify(self.cps, {10: H1, 25: "d" * 64}, K)
        self.assertTrue(any("chain rebuilt" in p for p in probs))

    def test_removed_rows_and_forged_lines_are_detected(self):
        self.assertTrue(any("no longer exists" in p for p in verify(self.cps, {10: H1}, K)))
        forged = dict(self.cps[1], hash="e" * 64)
        self.assertTrue(any("MAC" in p for p in verify([self.cps[0], forged], {10: H1, 25: "e" * 64}, K)))

    def test_head_never_goes_backwards(self):
        self.assertTrue(any("backwards" in p for p in verify(self.cps + [make(12, H1, K, "2026-10-03T00:00:00Z")], {10: H1, 12: H1, 25: H2}, K)))

    def test_bad_head_rejected(self):
        with self.assertRaises(ValueError):
            make(0, H1, K)


if __name__ == "__main__":
    unittest.main()
