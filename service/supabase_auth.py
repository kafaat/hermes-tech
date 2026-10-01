"""Owner and operator sign-in through Supabase Auth (GoTrue REST), server side: the portal runs no script.

    auth = SupabaseAuth("https://<ref>.supabase.co", anon_key, api)     # api: crawler.ApiClient({"<ref>.supabase.co"})
    auth.send_code(email)              -> None      a 6-digit code by email; existing accounts only (no sign-up here)
    auth.verify_code(email, code)      -> Session   access token (HS256, verified by service/auth.py) + refresh token
    auth.refresh(refresh_token)        -> Session   a new pair; Supabase rotates the refresh token on every use
    auth.mfa_factor(access)            -> dict|None the user's TOTP factor (verified or not)
    auth.mfa_enroll(access)            -> dict      a new TOTP factor: its id and the secret to type into an app
    auth.mfa_verify(access, factor, code) -> Session  aal2: the operator console requires it (app.is_operator)
    auth.sign_out(access)              -> None      revokes the refresh tokens (best effort)

Every call goes through crawler.ApiClient to the project host only. Supabase's answer is never shown to the browser:
a refusal is an AuthFailed with a short code (rate_limited, invalid, unavailable), and the portal says one Arabic
sentence. Tokens and codes never enter a log line or an exception message.
"""
from __future__ import annotations
import re, sys
from dataclasses import dataclass
from urllib.parse import urlsplit

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
CODE = re.compile(r"^\d{6}$")


class AuthFailed(Exception):
    """code: invalid | rate_limited | unavailable. Never carries Supabase's text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str


def project_host(url: str) -> str:
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.path not in ("", "/") or u.query or u.port not in (None, 443):
        raise ValueError("HERMES_SUPABASE_URL must be https://<project host>")
    return u.hostname.lower()


class SupabaseAuth:
    def __init__(self, url: str, anon_key: str, api):
        self.base = "https://" + project_host(url)
        if not anon_key:
            raise ValueError("HERMES_SUPABASE_ANON_KEY is empty")
        self.key, self.api = anon_key, api

    def _call(self, method: str, path: str, body: dict | None = None, access: str | None = None) -> dict:
        headers = {"apikey": self.key, "Authorization": f"Bearer {access or self.key}"}
        try:
            status, data = self.api.request(method, self.base + path, body, headers)
        except ValueError:
            raise AuthFailed("invalid") from None                       # a value that cannot be a header
        except Exception:                                               # noqa: BLE001 - network, TLS, refused host
            raise AuthFailed("unavailable") from None
        if status == 429:
            raise AuthFailed("rate_limited")
        if status >= 500:
            raise AuthFailed("unavailable")
        if status >= 300:
            raise AuthFailed("invalid")
        return data

    @staticmethod
    def _session(data: dict) -> Session:
        a, r = data.get("access_token"), data.get("refresh_token")
        if not isinstance(a, str) or not isinstance(r, str) or not a or not r:
            raise AuthFailed("unavailable")
        return Session(a, r)

    def send_code(self, email: str) -> None:
        if not EMAIL.match(email or ""):
            raise AuthFailed("invalid")
        self._call("POST", "/auth/v1/otp", {"email": email.lower(), "create_user": False})

    def verify_code(self, email: str, code: str) -> Session:
        if not EMAIL.match(email or "") or not CODE.match(code or ""):
            raise AuthFailed("invalid")
        return self._session(self._call("POST", "/auth/v1/verify", {"type": "email", "email": email.lower(), "token": code}))

    def refresh(self, refresh_token: str) -> Session:
        if not refresh_token or len(refresh_token) > 512:
            raise AuthFailed("invalid")
        return self._session(self._call("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": refresh_token}))

    def mfa_factor(self, access: str) -> dict | None:
        """The user's TOTP factor, verified first; None when there is none."""
        user = self._call("GET", "/auth/v1/user", access=access)
        totp = [f for f in user.get("factors") or [] if isinstance(f, dict) and f.get("factor_type") == "totp"]
        totp.sort(key=lambda f: f.get("status") != "verified")
        return {"id": str(totp[0].get("id")), "verified": totp[0].get("status") == "verified"} if totp else None

    def mfa_enroll(self, access: str) -> dict:
        data = self._call("POST", "/auth/v1/factors", {"factor_type": "totp", "friendly_name": "hermes-operator"}, access=access)
        secret = (data.get("totp") or {}).get("secret")
        if not data.get("id") or not isinstance(secret, str):
            raise AuthFailed("unavailable")
        return {"id": str(data["id"]), "secret": secret}

    def mfa_verify(self, access: str, factor_id: str, code: str) -> Session:
        if not CODE.match(code or "") or not re.fullmatch(r"[0-9a-fA-F-]{36}", factor_id or ""):
            raise AuthFailed("invalid")
        challenge = self._call("POST", f"/auth/v1/factors/{factor_id}/challenge", {}, access=access)
        if not challenge.get("id"):
            raise AuthFailed("unavailable")
        return self._session(self._call("POST", f"/auth/v1/factors/{factor_id}/verify",
                                        {"challenge_id": str(challenge["id"]), "code": code}, access=access))

    def sign_out(self, access: str) -> None:
        try:
            self._call("POST", "/auth/v1/logout?scope=local", {}, access=access)
        except AuthFailed:
            pass                                                        # the cookies are cleared either way


def from_env(env) -> SupabaseAuth | None:
    """None when sign-in is not configured; a half configuration refuses to start rather than run without it."""
    url, key = env.get("HERMES_SUPABASE_URL", ""), env.get("HERMES_SUPABASE_ANON_KEY", "")
    if not url and not key:
        return None
    if not (url and key and env.get("HERMES_JWT_SECRET")):
        sys.exit("HERMES_SUPABASE_URL, HERMES_SUPABASE_ANON_KEY and HERMES_JWT_SECRET go together")
    from service.crawler import ApiClient                  # the one network path (spec 28.12)
    return SupabaseAuth(url, key, ApiClient({project_host(url)}))
