"""Supabase JWT signing keys (spec 28.26): verify ES256 / RS256 access tokens with the project's published public keys.

    keys = Jwks(fetch)                 fetch() -> the JSON of https://<project>/auth/v1/.well-known/jwks.json
    keys.verify(kid, alg, signing_input, signature)     raises AuthError

New Supabase projects sign access tokens with an asymmetric key (ES256 by default, RS256 possible) instead of the shared
HS256 secret. The public keys are published as a JWKS; the private key never leaves Supabase. Rules:
  - the token's alg must equal the key's own alg (and the key's type and curve must fit it): a header cannot pick a
    weaker check or reuse a key for another algorithm; only ES256 (P-256) and RS256 (at least 2048 bits) are taken;
  - keys whose use is not "sig" are ignored; a key without kid is ignored;
  - the set is cached for KEYS_TTL seconds (Supabase caches it 10 minutes at its edge); an unknown kid refetches the
    set at most once per REFETCH_MIN seconds, so tokens with invented kids cannot turn into a flood of requests;
  - a failed fetch keeps the keys already known; with none known, every asymmetric token is refused (never accepted).
The fetch goes through crawler.ApiClient (the one network path, spec 28.12) to the project host only.
"""
from __future__ import annotations
import base64, threading, time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from service.auth import AuthError

KEYS_TTL, REFETCH_MIN = 600, 60
ALGORITHMS = ("ES256", "RS256")


def _b64(value) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("member")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _int(value) -> int:
    return int.from_bytes(_b64(value), "big")


def parse(doc) -> dict:
    """{kid: (alg, public key)} for the usable signing keys of a JWKS document; anything else is skipped."""
    out = {}
    keys = doc.get("keys") if isinstance(doc, dict) else None
    for k in keys if isinstance(keys, list) else []:
        if not isinstance(k, dict) or not isinstance(k.get("kid"), str) or not k["kid"] or k.get("use", "sig") != "sig":
            continue
        try:
            if k.get("alg") == "ES256" and k.get("kty") == "EC" and k.get("crv") == "P-256":
                x, y = _b64(k.get("x")), _b64(k.get("y"))
                if len(x) != 32 or len(y) != 32:
                    continue
                key = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
            elif k.get("alg") == "RS256" and k.get("kty") == "RSA":
                key = rsa.RSAPublicNumbers(_int(k.get("e")), _int(k.get("n"))).public_key()
                if key.key_size < 2048:
                    continue
            else:
                continue
        except (ValueError, TypeError):                 # not on the curve, bad encoding: not a key
            continue
        out[k["kid"]] = (k["alg"], key)
    return out


class Jwks:
    def __init__(self, fetch, now=time.monotonic):
        self.fetch, self.now, self.lock = fetch, now, threading.Lock()
        self.keys, self.fetched_at, self.tried_at = {}, None, None

    def _refresh(self, force: bool):
        t = self.now()
        fresh = self.fetched_at is not None and t - self.fetched_at < KEYS_TTL
        if (fresh and not force) or (self.tried_at is not None and t - self.tried_at < REFETCH_MIN):
            return
        self.tried_at = t
        try:
            keys = parse(self.fetch())
        except Exception:                               # noqa: BLE001 - keep what is known; refuse what is not
            return
        if keys:
            self.keys, self.fetched_at = keys, t

    def key(self, kid, alg):
        if alg not in ALGORITHMS or not isinstance(kid, str) or not kid:
            raise AuthError("algorithm")
        with self.lock:
            self._refresh(force=False)
            if kid not in self.keys:
                self._refresh(force=True)               # a rotation: at most once per REFETCH_MIN
            entry = self.keys.get(kid)
        if entry is None:
            raise AuthError("unknown key")
        if entry[0] != alg:
            raise AuthError("algorithm")
        return entry[1]

    def verify(self, kid, alg, signing_input: bytes, signature: bytes):
        key = self.key(kid, alg)
        try:
            if alg == "ES256":
                if len(signature) != 64:                # JOSE: r || s, 32 bytes each
                    raise AuthError("signature")
                der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
                key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
            else:
                key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise AuthError("signature") from None


def from_url(url: str, anon_key: str, api) -> Jwks:
    """The project's JWKS over ApiClient (project host only); the anon key is public and identifies the project."""
    from service.supabase_auth import project_host
    target = f"https://{project_host(url)}/auth/v1/.well-known/jwks.json"

    def fetch():
        status, data = api.request("GET", target, None, {"apikey": anon_key})
        if status != 200:
            raise RuntimeError(f"jwks {status}")
        return data
    return Jwks(fetch)
