"""Linking a TikTok account and keeping its tokens alive (spec 28.30).

  linking   the owner presses "link TikTok" in the portal: a signed state (business, the session's user, expiry) goes
            to TikTok's consent page; TikTok sends the browser back with a code; the portal exchanges the code
            (POST open.tiktokapis.com/v2/oauth/token/, grant_type=authorization_code), checks the video.publish scope
            was granted, seals both tokens (token_box) and calls app.link_provider_account as that owner (0028)
  renewing  TikTokTokens is the TikTok publisher's token source: under the publishing task's lease it reads the row
            FOR UPDATE, returns the access token while it has 30 minutes left, otherwise renews it
            (grant_type=refresh_token) and stores both new tokens (TikTok may rotate the refresh token); a failure is
            recorded on the row (the owner sees it) and the send fails cleanly before sending (NO_ACCOUNT_TOKEN)
Access tokens live 24 hours, refresh tokens 365 days (TikTok's documentation, user access token management).
Every call goes through crawler.ApiClient to open.tiktokapis.com only.
"""
from __future__ import annotations
import base64, hashlib, hmac, json, re, secrets, time, uuid
from dataclasses import dataclass
from urllib.parse import urlencode

from service.token_box import SealError, aad

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name"
SCOPES = "user.info.basic,video.publish"
STATE_TTL, RENEW_BEFORE = 600, 1800
OPEN_ID = re.compile(r"^[A-Za-z0-9_.-]{5,64}$")
CODE = re.compile(r"^[A-Za-z0-9*!._~%-]{8,1024}$")


class OAuthError(Exception):
    """TikTok refused (invalid_grant, a scope not granted, ...) or answered with something unusable."""


@dataclass
class Tokens:
    open_id: str
    access: str
    refresh: str
    access_expires_in: int
    refresh_expires_in: int
    scope: str


def _tokens(status: int, data: dict) -> Tokens:
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError(str((data or {}).get("error") or f"http_{status}")[:60])
    open_id = str(data.get("open_id") or "")
    try:
        t = Tokens(open_id, str(data["access_token"]), str(data["refresh_token"]), int(data["expires_in"]),
                   int(data["refresh_expires_in"]), str(data.get("scope") or ""))
    except (KeyError, TypeError, ValueError):
        raise OAuthError("malformed") from None
    if not OPEN_ID.match(open_id) or not (60 <= t.access_expires_in <= 400 * 86400) or not (60 <= t.refresh_expires_in <= 400 * 86400):
        raise OAuthError("malformed")
    return t


class TikTokOAuth:
    def __init__(self, client_key: str, client_secret: str, redirect_uri: str, api):
        if not (client_key and client_secret and re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?/portal/connect/tiktok/callback", redirect_uri)):
            raise ValueError("HERMES_TIKTOK_CLIENT_KEY, HERMES_TIKTOK_CLIENT_SECRET and an https HERMES_TIKTOK_REDIRECT_URI"
                             " ending in /portal/connect/tiktok/callback")
        self.key, self.secret, self.redirect, self.api = client_key, client_secret, redirect_uri, api

    def authorize_url(self, state: str) -> str:
        return AUTHORIZE_URL + "?" + urlencode({"client_key": self.key, "scope": SCOPES, "response_type": "code",
                                                 "redirect_uri": self.redirect, "state": state})

    def exchange(self, code: str) -> Tokens:
        return _tokens(*self.api.request("POST", TOKEN_URL, form={
            "client_key": self.key, "client_secret": self.secret, "code": code, "grant_type": "authorization_code",
            "redirect_uri": self.redirect}))

    def refresh(self, refresh_token: str) -> Tokens:
        return _tokens(*self.api.request("POST", TOKEN_URL, form={
            "client_key": self.key, "client_secret": self.secret, "grant_type": "refresh_token",
            "refresh_token": refresh_token}))

    def display_name(self, access: str) -> str:
        try:
            status, data = self.api.request("GET", USER_URL, None, {"Authorization": f"Bearer {access}"})
            name = str((((data or {}).get("data") or {}).get("user") or {}).get("display_name") or "") if status == 200 else ""
        except Exception:                               # noqa: BLE001 - a name is a courtesy, never a reason to fail
            name = ""
        return " ".join(name.split())[:120] or "TikTok"


class SimulatedTikTokOAuth:
    """Staging: TikTok's answers without TikTok. The consent page is the callback itself, with a code."""

    def __init__(self, redirect_path: str = "/portal/connect/tiktok/callback"):
        self.redirect, self.refreshed = redirect_path, []

    def authorize_url(self, state: str) -> str:
        return self.redirect + "?" + urlencode({"code": "SIMcode" + secrets.token_hex(8), "state": state})

    def exchange(self, code: str) -> Tokens:
        h = hashlib.sha256(code.encode()).hexdigest()[:12]
        return Tokens(f"_sim{h}", f"sim-access-{h}", f"sim-refresh-{h}", 86400, 365 * 86400, SCOPES)

    def refresh(self, refresh_token: str) -> Tokens:
        self.refreshed.append(refresh_token)
        h = refresh_token.rsplit("-", 1)[-1]
        return Tokens(f"_sim{h}", f"sim-access-{h}-{len(self.refreshed)}", refresh_token, 86400, 365 * 86400, SCOPES)

    def display_name(self, access: str) -> str:
        return "TikTok (staging)"


# ---------------------------------------------------------------- the state carried through TikTok's consent page
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_state(secret: str, customer: str, sub: str, now: float | None = None) -> str:
    body = _b64(json.dumps({"c": customer, "s": sub, "n": secrets.token_hex(8),
                            "e": int((time.time() if now is None else now) + STATE_TTL)}, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(("tiktok-state:" + secret).encode(), body.encode(), hashlib.sha256).digest())


def verify_state(secret: str, state: str, sub: str, now: float | None = None) -> str:
    """The business id the state was issued for, if it is ours, unexpired and issued to this same user; else ''."""
    try:
        body, sig = state.split(".")
        good = _b64(hmac.new(("tiktok-state:" + secret).encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return ""
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data.get("s") != sub or int(data.get("e", 0)) < (time.time() if now is None else now):
            return ""
        return str(uuid.UUID(str(data.get("c"))))
    except (ValueError, TypeError, AttributeError):
        return ""


# ---------------------------------------------------------------- the publisher's token source
class TikTokTokens:
    """token_for_account for TikTokPublishAdapter: the sealed token of the publishing task's business (lease from
    dispatcher.CURRENT_BIND), renewed when it has less than RENEW_BEFORE seconds left. None: no usable token."""

    def __init__(self, db, box, oauth):
        self.db, self.box, self.oauth = db, box, oauth

    def __call__(self, account: str):
        from service.dispatcher import CURRENT_BIND
        bind = CURRENT_BIND.get()
        if bind is None or not OPEN_ID.match(account or ""):
            return None
        with self.db.tx(bind) as cur:                   # the row lock serialises two renewals of one account
            cur.execute("select access_ct, refresh_ct, access_expires_at > now() + make_interval(secs => %s),"
                        " refresh_expires_at > now() from app.provider_tokens where provider = 'tiktok' and account_id = %s"
                        " for update", (RENEW_BEFORE, account))
            row = cur.fetchone()
            if row is None:
                return None
            access_ct, refresh_ct, fresh, renewable = row
            try:
                if fresh:
                    return self.box.open(access_ct, aad("tiktok", account, "access"))
                if not renewable:
                    raise OAuthError("refresh_expired")
                t = self.oauth.refresh(self.box.open(refresh_ct, aad("tiktok", account, "refresh")))
                if t.open_id != account:
                    raise OAuthError("another_account")
            except Exception as exc:                    # noqa: BLE001 - before any post: a clean failure, recorded
                if not isinstance(exc, (SealError, OAuthError)):
                    exc = OAuthError(type(exc).__name__)
                cur.execute("update app.provider_tokens set failures = failures + 1, last_error = %s"
                            " where provider = 'tiktok' and account_id = %s", (f"{type(exc).__name__}:{exc}"[:120], account))
                return None
            cur.execute("update app.provider_tokens set access_ct = %s, refresh_ct = %s,"
                        " access_expires_at = now() + make_interval(secs => %s), refresh_expires_at = now() + make_interval(secs => %s),"
                        " rotated_at = now(), failures = 0, last_error = null where provider = 'tiktok' and account_id = %s",
                        (self.box.seal(t.access, aad("tiktok", account, "access")),
                         self.box.seal(t.refresh, aad("tiktok", account, "refresh")),
                         t.access_expires_in, t.refresh_expires_in, account))
            return t.access
