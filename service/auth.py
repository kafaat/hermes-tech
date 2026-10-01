"""Owner identity for the portal: a Supabase Auth access token, verified here, never trusted from the browser.

    claims = verify_session_token(token, secret, now=time.time())      # raises AuthError

Supabase signs its access tokens (JWT) with the project's JWT secret (HS256, legacy) or, in projects with JWT signing
keys, with an asymmetric key whose public half it publishes (ES256 / RS256, checked by service/jwks.py, spec 28.26);
audience "authenticated". The portal accepts exactly that and nothing looser:
  - alg must be HS256 with a configured secret, or ES256 / RS256 with the project's keys, the key's own algorithm
    (no "none", no swap: the header cannot choose how it is checked)
  - the signature is compared in constant time over the exact bytes received
  - exp is required and must be in the future (60 s leeway for clocks); nbf/iat, when present, not in the future
  - aud must be "authenticated", role "authenticated", sub a UUID
The claims are then handed to Postgres (request.jwt.claims) and every row the owner sees or changes is decided by
the policies there (current_user_customer_ids, approvals_owner_decide, DECIDER_MUST_BE_SESSION_USER).

issue_staging_token() signs a token the same way for the staging login only (service/portal.py refuses it unless
HERMES_GRAPH=simulate and a staging login code is configured). Production tokens come from Supabase Auth.
"""
from __future__ import annotations
import base64, hashlib, hmac, json, time, uuid

LEEWAY_SECONDS = 60
MAX_TOKEN_BYTES = 8192


class AuthError(Exception):
    """The token is not a valid owner session. The message names the reason; it is never shown to the browser."""


def _b64d(part: str) -> bytes:
    if not part or len(part) > MAX_TOKEN_BYTES:
        raise AuthError("malformed")
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (ValueError, TypeError):
        raise AuthError("malformed") from None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def verify_session_token(token: str, secret: str, now: float | None = None, keys=None) -> dict:
    """keys: a jwks.Jwks for asymmetric tokens (verify(kid, alg, signing_input, signature) raises AuthError), or None."""
    if not secret and keys is None:
        raise AuthError("no secret configured")
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES or token.count(".") != 2:
        raise AuthError("malformed")
    head_b64, body_b64, sig_b64 = token.split(".")
    try:
        header = json.loads(_b64d(head_b64))
        claims = json.loads(_b64d(body_b64))
    except ValueError:
        raise AuthError("malformed") from None
    if not isinstance(header, dict):
        raise AuthError("algorithm")
    signing_input = f"{head_b64}.{body_b64}".encode()
    if header.get("alg") == "HS256" and secret:
        expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(sig_b64)):
            raise AuthError("signature")
    elif header.get("alg") in ("ES256", "RS256") and keys is not None:
        keys.verify(header.get("kid"), header["alg"], signing_input, _b64d(sig_b64))
    else:
        raise AuthError("algorithm")
    if not isinstance(claims, dict):
        raise AuthError("claims")
    now = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp + LEEWAY_SECONDS < now:
        raise AuthError("expired")
    for k in ("nbf", "iat"):
        v = claims.get(k)
        if v is not None and (not isinstance(v, (int, float)) or v - LEEWAY_SECONDS > now):
            raise AuthError(f"{k} in the future")
    aud = claims.get("aud")
    if not (aud == "authenticated" or (isinstance(aud, list) and "authenticated" in aud)):
        raise AuthError("audience")
    if claims.get("role") != "authenticated":
        raise AuthError("role")
    try:
        claims["sub"] = str(uuid.UUID(str(claims.get("sub"))))
    except ValueError:
        raise AuthError("subject") from None
    if claims.get("aal") not in (None, "aal1", "aal2"):
        raise AuthError("aal")
    return claims


def issue_staging_token(sub: str, secret: str, ttl_seconds: int = 3600, now: float | None = None, aal: str = "aal1") -> str:
    """Staging only: the same format Supabase issues, so the portal path is identical. aal2 stands for a completed
    second factor, which Supabase MFA grants in production; staging grants it to the staging operator only."""
    if aal not in ("aal1", "aal2"):
        raise ValueError("aal")
    now = int(time.time() if now is None else now)
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64e(json.dumps({"sub": str(uuid.UUID(sub)), "aud": "authenticated", "role": "authenticated", "aal": aal,
                             "iat": now, "exp": now + ttl_seconds}, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def csrf_token(session_token: str, secret: str) -> str:
    """Bound to this session: a form from another session, or from another site, does not carry it."""
    return hmac.new(("csrf:" + secret).encode(), session_token.encode(), hashlib.sha256).hexdigest()[:40]
