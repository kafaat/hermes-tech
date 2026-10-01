"""Supabase JWT signing keys (spec 28.26): ES256 and RS256 tokens verified with the project's published keys; the key
decides the algorithm, never the token; unknown keys refetch the set at most once a minute; nothing unverified passes."""
import base64, json, sys, time, unittest, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402

from service.auth import AuthError, issue_staging_token, verify_session_token  # noqa: E402
from service.jwks import REFETCH_MIN, Jwks, from_url, parse  # noqa: E402

NOW = 1_800_000_000
SUB = str(uuid.uuid4())


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def n2b(n: int, size: int | None = None) -> bytes:
    return n.to_bytes(size or (n.bit_length() + 7) // 8, "big")


def jwk(kid, key, alg, **extra):
    pub = key.public_key().public_numbers()
    if alg == "ES256":
        return {"kid": kid, "alg": alg, "kty": "EC", "crv": "P-256", "x": b64(n2b(pub.x, 32)), "y": b64(n2b(pub.y, 32)), **extra}
    return {"kid": kid, "alg": alg, "kty": "RSA", "n": b64(n2b(pub.n)), "e": b64(n2b(pub.e)), **extra}


def token(key, alg, kid, claims=None, header=None):
    h = b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT", **(header or {})}).encode())
    c = b64(json.dumps({"sub": SUB, "aud": "authenticated", "role": "authenticated", "aal": "aal1", "iat": NOW,
                        "exp": NOW + 3600, **(claims or {})}).encode())
    data = f"{h}.{c}".encode()
    if alg == "ES256":
        r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
        sig = n2b(r, 32) + n2b(s, 32)
    else:
        sig = key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{c}.{b64(sig)}"


EC = ec.generate_private_key(ec.SECP256R1())
RS = rsa.generate_private_key(public_exponent=65537, key_size=2048)
WEAK = rsa.generate_private_key(public_exponent=65537, key_size=1024)
DOC = {"keys": [jwk("ec1", EC, "ES256"), jwk("rs1", RS, "RS256"), jwk("weak", WEAK, "RS256"),
                jwk("enc1", EC, "ES256", use="enc"), {"kid": "bad", "alg": "ES256", "kty": "EC", "crv": "P-256",
                                                      "x": b64(b"\x01" * 32), "y": b64(b"\x02" * 32)}]}


class Fetch:
    def __init__(self, *docs):
        self.docs, self.calls = list(docs), 0

    def __call__(self):
        self.calls += 1
        doc = self.docs[min(self.calls, len(self.docs)) - 1]
        if isinstance(doc, Exception):
            raise doc
        return doc


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestJwks(unittest.TestCase):
    def test_es256_and_rs256_tokens_verify_with_the_published_keys(self):
        keys = Jwks(Fetch(DOC))
        for key, alg, kid in ((EC, "ES256", "ec1"), (RS, "RS256", "rs1")):
            with self.subTest(alg):
                claims = verify_session_token(token(key, alg, kid), "", NOW, keys)
                self.assertEqual((claims["sub"], claims["aal"]), (SUB, "aal1"))
        self.assertEqual(sorted(parse(DOC)), ["ec1", "rs1"])          # weak RSA, encryption key, off-curve point: skipped

    def test_the_key_decides_the_algorithm_and_nothing_unverified_passes(self):
        keys = Jwks(Fetch(DOC))
        other = ec.generate_private_key(ec.SECP256R1())
        h, c, s = token(EC, "ES256", "ec1").split(".")
        cases = {
            "algorithm": [token(EC, "ES256", "rs1"),                                  # an EC token naming the RSA key
                          token(RS, "RS256", "ec1"),
                          f"{b64(json.dumps({'alg': 'none', 'kid': 'ec1'}).encode())}.{c}.",
                          token(EC, "ES256", "ec1", header={"alg": "HS256"}),        # no secret: HS256 is never checked
                          token(EC, "ES256", "")],
            "signature": [token(other, "ES256", "ec1"),                               # signed by someone else's key
                          f"{h}.{b64(json.dumps({'sub': SUB, 'aud': 'authenticated', 'role': 'authenticated', 'exp': NOW + 9e5}).encode())}.{s}",
                          f"{h}.{c}.{b64(base64.urlsafe_b64decode(s + '==')[:63])}"],
            "unknown key": [token(WEAK, "RS256", "weak"), token(EC, "ES256", "nope")],
            "expired": [token(EC, "ES256", "ec1", claims={"exp": NOW - 3600})],
            "role": [token(EC, "ES256", "ec1", claims={"role": "service_role"})],
        }
        for reason, tokens in cases.items():
            for t in tokens:
                with self.subTest(reason=reason, token=t[:40]), self.assertRaises(AuthError) as cm:
                    verify_session_token(t, "", NOW, keys)
                self.assertEqual(str(cm.exception), reason)
        with self.assertRaises(AuthError):
            verify_session_token(token(EC, "ES256", "ec1"), "", NOW, None)          # no keys configured: refused

    def test_hs256_stays_with_the_secret_and_only_with_it(self):
        secret = "s" * 40
        t = issue_staging_token(SUB, secret, now=NOW)
        self.assertEqual(verify_session_token(t, secret, NOW, Jwks(Fetch(DOC)))["sub"], SUB)
        with self.assertRaises(AuthError):
            verify_session_token(t, "", NOW, Jwks(Fetch(DOC)))

    def test_unknown_keys_refetch_at_most_once_a_minute_and_failures_keep_known_keys(self):
        new = ec.generate_private_key(ec.SECP256R1())
        clock = Clock()
        fetch = Fetch(DOC, {"keys": [jwk("ec2", new, "ES256")]}, RuntimeError("down"))
        keys = Jwks(fetch, now=clock)
        verify_session_token(token(EC, "ES256", "ec1"), "", NOW, keys)
        self.assertEqual(fetch.calls, 1)
        for kid in ("x1", "x2", "x3"):                                   # invented kids: not one request each
            with self.assertRaises(AuthError):
                verify_session_token(token(EC, "ES256", kid), "", NOW, keys)
        self.assertEqual(fetch.calls, 1)                                 # the first fetch was less than a minute ago
        clock.t += REFETCH_MIN + 1
        self.assertEqual(verify_session_token(token(new, "ES256", "ec2"), "", NOW, keys)["sub"], SUB)  # a rotation
        self.assertEqual(fetch.calls, 2)
        clock.t += 3600
        self.assertEqual(verify_session_token(token(new, "ES256", "ec2"), "", NOW, keys)["sub"], SUB)  # stale, fetch fails
        self.assertEqual(fetch.calls, 3)
        with self.assertRaises(AuthError):
            verify_session_token(token(EC, "ES256", "ec1"), "", NOW, Jwks(Fetch(RuntimeError("down"))))

    def test_the_set_comes_from_the_project_host(self):
        calls = []

        class Api:
            def request(self, method, url, body, headers):
                calls.append((method, url, body, headers))
                return 200, DOC
        keys = from_url("https://abcd.supabase.co", "anon-key", Api())
        verify_session_token(token(EC, "ES256", "ec1"), "", NOW, keys)
        self.assertEqual(calls, [("GET", "https://abcd.supabase.co/auth/v1/.well-known/jwks.json", None, {"apikey": "anon-key"})])
        with self.assertRaises(ValueError):
            from_url("http://abcd.supabase.co/x", "k", Api())


if __name__ == "__main__":
    unittest.main()
