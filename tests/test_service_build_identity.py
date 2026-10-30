import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.app import build_identity

class BuildIdentityTests(unittest.TestCase):
    def test_exact_identity(self):
        self.assertEqual(build_identity({'RAILWAY_GIT_COMMIT_SHA': 'A'*40, 'HERMES_BUILD_GENERATION': '2'}), ('a'*40, 2))

    def test_missing_partial_or_invalid_identity_rejected(self):
        for commit, generation in [('', '1'), ('abcd', '1'), ('a'*40, ''), ('a'*40, '0'), ('a'*40, '-1'), ('a'*40, '9223372036854775808')]:
            with self.subTest(commit=commit, generation=generation), self.assertRaises(ValueError):
                build_identity({'RAILWAY_GIT_COMMIT_SHA': commit, 'HERMES_BUILD_GENERATION': generation})
