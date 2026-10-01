"""Outbox dispatcher (claim C8.5): one send per claim; an unconfirmed send is never repeated automatically.

    Dispatcher(db, adapters).run_once(outbox_id) -> outcome string

Adapters report WHERE a failure happened, because that alone decides whether a retry is safe:
  BeforeSend  the request provably never left (DNS, connect refused, TLS handshake, local validation,
              provider 429 "not accepted")                        -> released, claimable again later
  Rejected    the provider answered and refused (4xx except 408/429) -> failed, final, explained
  any other exception, a timeout after the request was written, 5xx, 408, a malformed success
                                                                   -> AMBIGUOUS: an operator decides
Unknown means ambiguous. The database enforces the same rule (0010 outbox_before_write), so a bug here
cannot re-send: an expired unconfirmed claim of a non-idempotent topic cannot be taken again.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Protocol

SENT, FAILED_PERMANENT, FAILED_BEFORE_SEND, AMBIGUOUS = "sent", "failed_permanent", "failed_before_send", "ambiguous"

# Every send is bounded, and the bound sits inside every lease and shutdown window around it. A send that outlived
# its leases would return the provider's id to a worker the database no longer lets write (fencing), and the
# id would be lost (staging rehearsal, spec 28.7): SEND_TIMEOUT < app shutdown wait (25 s) < Railway drain (30 s),
# and SEND_TIMEOUT < dispatch lease and task lease. tests/test_service_dispatcher.py holds the chain.
SEND_TIMEOUT_SECONDS = 15
DISPATCH_LEASE_SECONDS = 120


class BeforeSend(Exception):
    """The request did not leave this host (or the provider says it did not accept it)."""


class Rejected(Exception):
    def __init__(self, status: int, code: str = ""):
        super().__init__(f"{status} {code}".strip())
        self.status, self.code = status, code


@dataclass
class Result:
    outcome: str
    provider_ref: str | None = None
    error: str | None = None


class Adapter(Protocol):
    def send(self, row: dict) -> str | None:
        """Perform the effect; return the provider's id for it (wamid, post id, deploy id) or None."""


class DbPort(Protocol):
    def claim(self, outbox_id: int) -> str | None: ...                                   # app.claim_outbox_dispatch
    def row(self, outbox_id: int) -> dict: ...
    def finish(self, outbox_id: int, token: str, outcome: str, provider_ref=None, error=None) -> bool: ...  # finish_outbox_dispatch
    def reconcile(self, outbox_id: int, provider_ref: str) -> bool: ...                  # app.reconcile_outbox_sent


def classify(exc: BaseException) -> Result:
    if isinstance(exc, BeforeSend):
        return Result(FAILED_BEFORE_SEND, error=f"{type(exc).__name__}: {exc}"[:180])
    if isinstance(exc, Rejected) and 400 <= exc.status < 500 and exc.status not in (408, 429):
        return Result(FAILED_PERMANENT, error=f"HTTP_{exc.status}:{exc.code}"[:180])
    if isinstance(exc, Rejected) and exc.status == 429:
        return Result(FAILED_BEFORE_SEND, error="HTTP_429")
    return Result(AMBIGUOUS, error=f"{type(exc).__name__}"[:180])


class Dispatcher:
    def __init__(self, db: DbPort, adapters: dict[str, Adapter]):
        self.db, self.adapters = db, adapters

    def run_once(self, outbox_id: int) -> str:
        token = self.db.claim(outbox_id)
        if token is None:
            return "not_claimed"
        row = self.db.row(outbox_id)
        adapter = self.adapters.get(row["topic"])
        if adapter is None:
            result = Result(FAILED_BEFORE_SEND, error="NO_ADAPTER")
        else:
            try:
                ref = adapter.send(row)
                result = Result(SENT, provider_ref=ref)
            except BaseException as exc:          # noqa: BLE001  - every failure must be classified
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    self._record(outbox_id, token, Result(AMBIGUOUS, error="INTERRUPTED"))
                    raise
                result = classify(exc)
        return self._record(outbox_id, token, result)

    def _record(self, outbox_id: int, token: str, result: Result) -> str:
        try:
            if self.db.finish(outbox_id, token, result.outcome, result.provider_ref, result.error):
                return result.outcome
        except Exception:                          # e.g. OUTBOX_AMBIGUOUS_NEEDS_HUMAN: the lease ran out meanwhile
            pass
        if result.outcome == SENT and result.provider_ref:
            if self.db.reconcile(outbox_id, result.provider_ref):
                return SENT
        try:                                       # could not record the result under the claim: a human decides
            self.db.finish(outbox_id, token, AMBIGUOUS, None, f"UNRECORDED:{result.outcome}")
        except Exception:
            pass
        return AMBIGUOUS


class WhatsAppCloudAdapter:
    """reply.send over the WhatsApp Cloud API. `post(url, json, headers, timeout)` is injected; it must raise
    BeforeSend for failures before the request was written, return (status, json_body) otherwise, and let
    timeouts after writing propagate (they classify as AMBIGUOUS)."""

    def __init__(self, post, token_for_customer, api_version: str = "v21.0"):
        self.post, self.token_for_customer, self.api_version = post, token_for_customer, api_version

    def send(self, row: dict) -> str:
        p = row["payload"]
        if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", str(p.get("phone_number_id", ""))) or not re.fullmatch(r"v\d{1,2}\.\d", self.api_version):
            raise BeforeSend("BAD_TARGET")                    # no "/", "?" or ".." from a payload shapes the URL path
        url = f"https://graph.facebook.com/{self.api_version}/{p['phone_number_id']}/messages"
        body = {"messaging_product": "whatsapp", "to": p["to"], "type": "text", "text": {"body": p["body"]}}
        status, data = self.post(url, body, {"Authorization": f"Bearer {self.token_for_customer(row['customer_id'])}"},
                                 SEND_TIMEOUT_SECONDS)
        if 200 <= status < 300:
            try:
                return str(data["messages"][0]["id"])
            except (KeyError, IndexError, TypeError):
                raise RuntimeError("success without a message id")    # accepted but unidentifiable: ambiguous
        code = str(((data or {}).get("error") or {}).get("code", ""))
        raise Rejected(status, code)


class MetaMessagingAdapter:
    """reply.send over Messenger (graph.facebook.com/<page id>/messages) or Instagram Direct (graph.instagram.com/
    <Instagram account id>/messages), standard messaging inside the 24-hour window that every reply proposal already
    respects (approvals_reply_window). The token is the account's own (a page or Instagram access token); a missing
    one is a clean failure before sending."""

    HOSTS = {"facebook_page": "graph.facebook.com", "instagram_business": "graph.instagram.com"}

    def __init__(self, post, token_for_account, api_version: str = "v21.0"):
        self.post, self.token_for_account, self.api_version = post, token_for_account, api_version

    def send(self, row: dict) -> str:
        p = row["payload"]
        host, account, to = self.HOSTS.get(p.get("channel")), str(p.get("account_id", "")), str(p.get("to", ""))
        if host is None or not re.fullmatch(r"[0-9]{5,25}", account) or not re.fullmatch(r"[0-9]{5,40}", to) \
           or not re.fullmatch(r"v\d{1,2}\.\d", self.api_version):
            raise BeforeSend("BAD_TARGET")
        token = self.token_for_account(account)
        if not token:
            raise BeforeSend("NO_ACCOUNT_TOKEN")
        url = f"https://{host}/{self.api_version}/{account}/messages"
        body = {"recipient": {"id": to}, "messaging_type": "RESPONSE", "message": {"text": p["body"]}}
        status, data = self.post(url, body, {"Authorization": f"Bearer {token}"}, SEND_TIMEOUT_SECONDS)
        if 200 <= status < 300:
            mid = (data or {}).get("message_id")
            if not mid:
                raise RuntimeError("success without a message id")      # accepted but unidentifiable: ambiguous
            return str(mid)
        code = str(((data or {}).get("error") or {}).get("code", ""))
        raise Rejected(status, code)


EMAIL_ADDRESS = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+")


class PostmarkEmailAdapter:
    """reply.send by email (spec 28.25): POST api.postmarkapp.com/email with the server token, From the business's
    verified sender, Reply-To its inbound address (the customer's answer comes back through the platform), and
    In-Reply-To / References so the reply joins the customer's thread. The sender per inbound address is
    configuration (HERMES_EMAIL_SENDERS), never the payload; a missing one is a clean failure before sending."""

    URL = "https://api.postmarkapp.com/email"

    def __init__(self, post, token: str, sender_for_account):
        self.post, self.token, self.sender_for_account = post, token, sender_for_account

    def send(self, row: dict) -> str:
        p = row["payload"]
        account, to, body = str(p.get("account_id") or ""), str(p.get("to") or ""), str(p.get("body") or "")
        subject, message_id = str(p.get("subject") or ""), p.get("message_id")
        if not EMAIL_ADDRESS.fullmatch(account) or not EMAIL_ADDRESS.fullmatch(to) or not body or len(body) > 10000 \
           or len(subject) > 200 or not subject.isprintable() \
           or (message_id is not None and not re.fullmatch(r"<[^<>\s]{3,250}>", str(message_id))):
            raise BeforeSend("BAD_TARGET")
        sender = self.sender_for_account(account)
        if not sender:
            raise BeforeSend("NO_SENDER")
        if not self.token:
            raise BeforeSend("NO_ACCOUNT_TOKEN")
        request = {"From": sender, "To": to, "ReplyTo": account, "Subject": subject or "رد", "TextBody": body,
                   "MessageStream": "outbound"}
        if message_id:
            request["Headers"] = [{"Name": "In-Reply-To", "Value": message_id}, {"Name": "References", "Value": message_id}]
        status, data = self.post(self.URL, request, {"X-Postmark-Server-Token": self.token}, SEND_TIMEOUT_SECONDS)
        code = (data or {}).get("ErrorCode")
        if 200 <= status < 300 and code in (0, None):
            ref = (data or {}).get("MessageID")
            if not ref:
                raise RuntimeError("success without a message id")      # accepted but unidentifiable: ambiguous
            return str(ref)
        raise Rejected(status if status >= 400 else 422, str(code or ""))


class ReplyRouter:
    """reply.send goes back on the channel the customer wrote on: WhatsApp, Messenger / Instagram Direct, or email."""

    def __init__(self, whatsapp, meta, email=None):
        self.whatsapp, self.meta, self.email = whatsapp, meta, email

    def send(self, row: dict) -> str:
        channel = row["payload"].get("channel")
        if channel in MetaMessagingAdapter.HOSTS:
            return self.meta.send(row)
        if channel == "email":
            if self.email is None:
                raise BeforeSend("NO_ADAPTER")
            return self.email.send(row)
        return self.whatsapp.send(row)


class MetaPublishAdapter:
    """content.publish: exactly the approved payload (text, account, image) to a Facebook page or an Instagram account.

    Facebook  text: POST graph.facebook.com/<page>/feed {message}; with an image: /<page>/photos {url, caption}
    Instagram two steps: POST graph.instagram.com/<account>/media {image_url, caption} creates an unpublished container
              (nothing is public yet, so any failure there is a clean failure), then /<account>/media_publish
              {creation_id} publishes it; a failure after that request was written is ambiguous, as for every send."""

    def __init__(self, post, token_for_account, api_version: str = "v21.0"):
        self.post, self.token_for_account, self.api_version = post, token_for_account, api_version

    def _call(self, url, body, token):
        status, data = self.post(url, body, {"Authorization": f"Bearer {token}"}, SEND_TIMEOUT_SECONDS)
        if 200 <= status < 300:
            return data or {}
        raise Rejected(status, str(((data or {}).get("error") or {}).get("code", "")))

    def send(self, row: dict) -> str:
        p = row["payload"]
        platform, account, body, image = p.get("platform"), str(p.get("account_id") or ""), p.get("body") or "", p.get("image_url")
        if platform not in ("facebook", "instagram") or not re.fullmatch(r"[0-9]{5,25}", account) \
           or not re.fullmatch(r"v\d{1,2}\.\d", self.api_version) or not (body or image) or len(body) > 2200 \
           or (image is not None and not re.fullmatch(r"https://[^\s\"<>\\]{8,2000}", str(image))):
            raise BeforeSend("BAD_TARGET")
        token = self.token_for_account(account)
        if not token:
            raise BeforeSend("NO_ACCOUNT_TOKEN")
        if platform == "facebook":
            base = f"https://graph.facebook.com/{self.api_version}/{account}"
            data = self._call(f"{base}/photos", {"url": image, "caption": body}, token) if image \
                else self._call(f"{base}/feed", {"message": body}, token)
            ref = data.get("post_id") or data.get("id")
        else:
            if not image:
                raise BeforeSend("CONTENT_IMAGE_REQUIRED")
            base = f"https://graph.instagram.com/{self.api_version}/{account}"
            try:
                container = self._call(f"{base}/media", {"image_url": image, "caption": body}, token).get("id")
            except Exception as exc:                    # noqa: BLE001 - only a container: nothing was published
                raise BeforeSend(f"IG_CONTAINER:{type(exc).__name__}") from None
            if not container:
                raise BeforeSend("IG_CONTAINER:NO_ID")
            ref = self._call(f"{base}/media_publish", {"creation_id": str(container)}, token).get("id")
        if not ref:
            raise RuntimeError("success without a post id")             # accepted but unidentifiable: ambiguous
        return str(ref)


class TikTokPublishAdapter:
    """content.publish to TikTok: a photo post through the Content Posting API (Direct Post, PULL_FROM_URL).
    POST open.tiktokapis.com/v2/post/publish/content/init/ returns a publish id and TikTok publishes asynchronously;
    that id is the provider reference. The image must sit on a domain the app verified with TikTok. The privacy level
    is configuration only: an app TikTok has not audited may post SELF_ONLY, so that is the default."""

    URL = "https://open.tiktokapis.com/v2/post/publish/content/init/"
    PRIVACY = ("SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE")

    def __init__(self, post, token_for_account, privacy: str = "SELF_ONLY"):
        if privacy not in self.PRIVACY:
            raise ValueError("privacy")
        self.post, self.token_for_account, self.privacy = post, token_for_account, privacy

    def send(self, row: dict) -> str:
        p = row["payload"]
        account, body, image = str(p.get("account_id") or ""), p.get("body") or "", p.get("image_url")
        if p.get("platform") != "tiktok" or not re.fullmatch(r"[A-Za-z0-9_.-]{5,64}", account) or len(body) > 2200 \
           or not image or not re.fullmatch(r"https://[^\s\"<>\\]{8,2000}", str(image)):
            raise BeforeSend("BAD_TARGET")
        token = self.token_for_account(account)
        if not token:
            raise BeforeSend("NO_ACCOUNT_TOKEN")
        request = {"post_info": {"title": body[:90], "description": body, "privacy_level": self.privacy,
                                 "disable_comment": False, "auto_add_music": True},
                   "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": [image]},
                   "post_mode": "DIRECT_POST", "media_type": "PHOTO"}
        status, data = self.post(self.URL, request, {"Authorization": f"Bearer {token}"}, SEND_TIMEOUT_SECONDS)
        error = str(((data or {}).get("error") or {}).get("code", ""))
        if 200 <= status < 300 and error in ("", "ok"):
            ref = ((data or {}).get("data") or {}).get("publish_id")
            if not ref:
                raise RuntimeError("success without a publish id")       # accepted but unidentifiable: ambiguous
            return str(ref)
        raise Rejected(status if status >= 400 else 400, error)


class PublishRouter:
    """content.publish goes to the platform the owner approved: Facebook / Instagram (Meta) or TikTok."""

    def __init__(self, meta, tiktok):
        self.meta, self.tiktok = meta, tiktok

    def send(self, row: dict) -> str:
        return (self.tiktok if row["payload"].get("platform") == "tiktok" else self.meta).send(row)
