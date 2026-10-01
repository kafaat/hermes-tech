"""Inbound email (spec 28.25): a customer's email reaches the business like a WhatsApp message.

    POST /email/inbound      Postmark's inbound webhook, JSON; HTTP basic auth (the credentials sit in the webhook URL)

Mail to the business's inbound address arrives through the provider, which POSTs the parsed message here. Order:
  1. size limit on the raw body (attachments arrive inline; anything above EMAIL_MAX_BYTES is refused);
  2. basic auth against HERMES_EMAIL_INBOUND_SECRETS ("user:password", comma-separated during a rotation), compared
     in constant time BEFORE any parsing; a wrong secret gets 401, which the provider retries (a misconfiguration
     can be fixed without losing mail);
  3. the recipient must be an active email channel (its inbound address); otherwise 403 and nothing is stored;
  4. automated mail (auto-replies, bounces, mailing lists, no-reply senders) is acknowledged and NOT stored: no
     reply may ever answer it (RFC 3834, no mail loops) and the owner is not paged for it;
  5. one event, keyed by the provider's message id, with only what the owner and a reply need: sender, name,
     subject, the text without the quoted thread, the RFC Message-ID, a spam verdict and the number of attachments.
     Attachments and HTML are never stored.
Permanent refusals answer 403: the provider stops retrying on 403 and shows the message as failed.
"""
from __future__ import annotations
import base64, binascii, hmac, json, re
from dataclasses import dataclass, field

EMAIL_MAX_BYTES = 10 * 1024 * 1024
TEXT_MAX = 4000
ADDRESS = re.compile(r"^[a-z0-9._%+-]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+$")
MESSAGE_ID = re.compile(r"^<[^<>\s]{3,250}>$")
PROVIDER_ID = re.compile(r"^[A-Za-z0-9-]{8,100}$")
NO_REPLY = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounces?)([+.-].*)?@")
QUOTE_START = re.compile(r"^\s*(on .{3,200} wrote:|في .{3,200}كتب.{0,20}:|-{2,}\s*original message\s*-{2,}|from:\s|من:\s)",
                         re.IGNORECASE)


@dataclass
class Counters:
    rejected_auth: int = 0
    refused: int = 0
    automated: int = 0
    inserted: int = 0
    duplicates: int = 0


def address(value) -> str:
    """'Name <A@B.example>' or 'a@b.example' -> 'a@b.example'; '' when it is not one plain address."""
    s = str(value or "").strip()
    m = re.search(r"<([^<>]*)>\s*$", s)
    s = (m.group(1) if m else s).strip().lower()
    return s if len(s) <= 254 and ADDRESS.match(s) else ""


def strip_quoted(text: str) -> str:
    """The new part of a reply: lines up to the quoted thread ("On ... wrote:", "> ..."), whitespace tidied."""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if QUOTE_START.match(line):
            break
        if not line.lstrip().startswith(">"):
            out.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def automated(headers: dict, sender: str) -> bool:
    auto = headers.get("auto-submitted", "no").strip().lower()
    return (auto != "no" or headers.get("precedence", "").strip().lower() in ("bulk", "junk", "list", "auto_reply")
            or "list-id" in headers or "list-unsubscribe" in headers or "x-autoreply" in headers
            or "x-autorespond" in headers or not sender or bool(NO_REPLY.match(sender)))


def parse(payload: dict) -> tuple[str, str, dict, bool]:
    """(provider message id, recipient, stored item, automated) from Postmark's inbound JSON; ValueError if unusable."""
    ext = str(payload.get("MessageID") or "")
    recipient = address(payload.get("OriginalRecipient"))
    if not PROVIDER_ID.match(ext) or not recipient:
        raise ValueError("no message id or recipient")
    headers = {}
    for h in payload.get("Headers") or []:
        if isinstance(h, dict) and isinstance(h.get("Name"), str):
            headers[h["Name"].strip().lower()] = str(h.get("Value") or "")
    sender = address((payload.get("FromFull") or {}).get("Email") if isinstance(payload.get("FromFull"), dict) else payload.get("From"))
    name = " ".join(str((payload.get("FromFull") or {}).get("Name") or "").split())[:80] if isinstance(payload.get("FromFull"), dict) else ""
    message_id = headers.get("message-id", "").strip()
    stripped = str(payload.get("StrippedTextReply") or "").strip()      # the provider's own cut (English clients only)
    text = stripped or strip_quoted(str(payload.get("TextBody") or ""))
    item = {"email": {
        "from": sender, "name": name,
        "subject": " ".join(str(payload.get("Subject") or "").split())[:200],
        "text": text[:TEXT_MAX],
        "message_id": message_id if MESSAGE_ID.match(message_id) else None,
        "spam": headers.get("x-spam-status", "").strip().lower().startswith("yes"),
        "attachments": len(payload.get("Attachments") or []) if isinstance(payload.get("Attachments"), list) else 0,
    }}
    return ext, recipient, item, automated(headers, sender) or sender == recipient


def auth_ok(header: str | None, secrets: list[str]) -> bool:
    if not header or not header.startswith("Basic ") or not secrets:
        return False
    try:
        given = base64.b64decode(header[6:].strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    ok = False
    for s in secrets:                               # no early exit: timing does not reveal which secret matched
        ok |= hmac.compare_digest(given, s.encode("utf-8"))
    return ok


@dataclass
class EmailHandler:
    secrets: list[str]
    ingest: object                                  # webhook.IngestPort plus channel_active (pg.Ingest)
    counters: Counters = field(default_factory=Counters)

    def handle(self, headers: dict, raw: bytes) -> tuple[int, str]:
        if len(raw) > EMAIL_MAX_BYTES:
            self.counters.refused += 1
            return 403, "too large"
        h = {k.lower(): v for k, v in headers.items()}
        if not auth_ok(h.get("authorization"), self.secrets):
            self.counters.rejected_auth += 1
            return 401, "unauthorized"
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
            ext, recipient, item, is_auto = parse(payload)
        except (ValueError, UnicodeDecodeError):
            self.counters.refused += 1
            return 403, "unusable"
        if not self.ingest.channel_active("email", recipient):
            self.counters.refused += 1
            return 403, "unknown recipient"
        if is_auto:
            self.counters.automated += 1
            return 200, "ignored"
        r = self.ingest.insert_event("email", ext, True, item, recipient)
        if r == "inserted":
            self.counters.inserted += 1
        elif r == "duplicate":
            self.counters.duplicates += 1
        else:
            raise RuntimeError(f"ingest returned {r!r}")
        return 200, "ok"
