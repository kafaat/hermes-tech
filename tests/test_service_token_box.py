"""Provider tokens at rest (spec 28.30): AES-GCM with a key outside the database; a blob opens only for the row it was
sealed for, any change to it is refused, and an older key keeps opening old blobs during a rotation."""
import base64, os, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.token_box import SealError, TokenBox, aad  # noqa: E402

K1, K2 = os.urandom(32), os.urandom(32)
A = aad("tiktok", "_acct01", "access")


class TestTokenBox(unittest.TestCase):
    def test_a_sealed_token_opens_only_for_its_row(self):
        box = TokenBox([K1])
        blob = box.seal("act.secret-token", A)
        self.assertNotIn(b"secret", blob)
        self.assertEqual(box.open(blob, A), "act.secret-token")
        self.assertNotEqual(box.seal("act.secret-token", A), blob)                  # a fresh nonce every time
        for other in (aad("tiktok", "_acct02", "access"), aad("tiktok", "_acct01", "refresh")):
            with self.subTest(other), self.assertRaises(SealError):
                box.open(blob, other)                                              # moved to another account or kind

    def test_any_change_is_refused(self):
        box = TokenBox([K1])
        blob = bytearray(box.seal("t", A))
        for i in (0, 1, 9, 21, len(blob) - 1):
            bad = bytearray(blob)
            bad[i] ^= 1
            with self.subTest(i), self.assertRaises(SealError):
                box.open(bytes(bad), A)
        with self.assertRaises(SealError):
            box.open(b"short", A)
        with self.assertRaises(SealError):
            TokenBox([K2]).open(bytes(blob), A)                                    # another key

    def test_rotation_opens_old_blobs_and_seals_with_the_newest_key(self):
        old = TokenBox([K1]).seal("t", A)
        both = TokenBox([K2, K1])
        self.assertEqual(both.open(old, A), "t")
        self.assertEqual(TokenBox([K2]).open(both.seal("t", A), A), "t")

    def test_configuration(self):
        self.assertIsNone(TokenBox.from_env({}))
        env = {"HERMES_TOKEN_KEYS": f"{base64.b64encode(K2).decode()}, {base64.b64encode(K1).decode()}"}
        self.assertEqual(TokenBox.from_env(env).open(TokenBox([K1]).seal("t", A), A), "t")
        for raw in ("not base64!", base64.b64encode(b"short").decode()):
            with self.subTest(raw), self.assertRaises(RuntimeError):
                TokenBox.from_env({"HERMES_TOKEN_KEYS": raw})


if __name__ == "__main__":
    unittest.main()
