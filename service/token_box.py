"""Provider tokens at rest (spec 28.30, ADR-0010 addendum): AES-256-GCM with a key that never enters the database.

    box = TokenBox.from_env(env)                      HERMES_TOKEN_KEYS: comma-separated base64 32-byte keys, newest first
    blob = box.seal(token, aad)                       aad: what the token belongs to, e.g. b"tiktok|<open_id>|access"
    token = box.open(blob, aad)                       raises SealError

The database holds the ciphertext; the key is a platform secret on the host (ADR-0010, item 2). A copy of the
database alone reveals no token. Each blob is bound to its row by the associated data (provider, account, kind): a
blob moved to another account, or an access blob used as a refresh blob, fails to open. Format: version byte 1, an
8-byte key id (SHA-256 of the key), a 12-byte random nonce, then the ciphertext with its 16-byte tag. Rotation: put
the new key first; blobs sealed with an older key still open while it is listed, and are resealed with the newest key
the next time they are written (every refresh writes them).
"""
from __future__ import annotations
import base64, hashlib, os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = 1


class SealError(Exception):
    """The blob does not open with any listed key for this associated data."""


def _key_id(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()[:8]


class TokenBox:
    def __init__(self, keys: list[bytes]):
        if not keys or any(len(k) != 32 for k in keys):
            raise ValueError("token keys must be 32 bytes each")
        self.keys = {_key_id(k): AESGCM(k) for k in keys}
        self.current = _key_id(keys[0])

    @classmethod
    def from_env(cls, env) -> "TokenBox | None":
        raw = env.get("HERMES_TOKEN_KEYS", "")
        if not raw:
            return None
        try:
            keys = [base64.b64decode(k.strip(), validate=True) for k in raw.split(",") if k.strip()]
            return cls(keys)
        except ValueError:
            raise RuntimeError("HERMES_TOKEN_KEYS: comma-separated base64 keys of 32 bytes") from None

    def seal(self, token: str, aad: bytes) -> bytes:
        nonce = os.urandom(12)
        return bytes([VERSION]) + self.current + nonce + self.keys[self.current].encrypt(nonce, token.encode("utf-8"), aad)

    def open(self, blob: bytes, aad: bytes) -> str:
        blob = bytes(blob)
        if len(blob) < 1 + 8 + 12 + 16 or blob[0] != VERSION:
            raise SealError("format")
        key = self.keys.get(blob[1:9])
        if key is None:
            raise SealError("unknown key")
        try:
            return key.decrypt(blob[9:21], blob[21:], aad).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            raise SealError("does not open") from None


def aad(provider: str, account: str, kind: str) -> bytes:
    return f"{provider}|{account}|{kind}".encode()
