"""Inbound webhook handler for Meta channels (claim C8.3).

    status, body = handle(headers, raw_body, secrets=[...], ingest=port)

Order is part of the contract and is tested:
  1. size limit on the RAW body;
  2. X-Hub-Signature-256 must equal "sha256=" + hex(HMAC-SHA256(app_secret, raw_body)), compared in
     constant time, BEFORE any JSON parsing (a parser bug is unreachable by an unsigned request); several
     secrets are accepted only during a rotation window;
  3. parse; one event per message / status / Messenger or Instagram Direct message (echoes of our own replies are
     skipped), keyed by the provider's own id;
  4. insert through the ingest role (hermes_ingest) with the ADDRESSED channel id; the database routes the
     tenant from channel_accounts (0010 webhook_route) and enqueues the task. The handler never chooses a tenant;
  5. 200 only after every insert committed; a duplicate delivery is a success, not an error.
Unsigned or badly signed requests get 401 and are NOT stored: the payload is untrusted and may carry personal
data. The counter `rejected_signatures` feeds an alert (a burst means a probe or a secret mismatch).
"""
from __future__ import annotations
import hashlib, hmac, json
from dataclasses import dataclass, field
from typing import Iterable, Protocol

MAX_BODY_BYTES = 256 * 1024


class IngestPort(Protocol):
    def insert_event(self, kind: str, external_event_id: str, signature_valid: bool, payload: dict,
                     channel_external_id: str | None) -> str:
        """'inserted' or 'duplicate' (unique (kind, external_event_id)); raises on any other failure."""


@dataclass
class Counters:
    rejected_signatures: int = 0
    oversize: int = 0
    unparseable: int = 0
    inserted: int = 0
    duplicates: int = 0


def signature_ok(raw_body: bytes, header: str | None, secrets: Iterable[bytes]) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    given = header[len("sha256="):].strip().lower()
    if len(given) != 64 or any(c not in "0123456789abcdef" for c in given):
        return False
    ok = False
    for secret in secrets:                       # no early exit: timing does not reveal which secret matched
        mac = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
        ok |= hmac.compare_digest(mac, given)
    return ok


def verify_subscription(query: dict, verify_token: str) -> tuple[int, str]:
    """GET handshake: echo hub.challenge only for mode=subscribe and the configured token."""
    if query.get("hub.mode") == "subscribe" and hmac.compare_digest(str(query.get("hub.verify_token", "")), verify_token):
        return 200, str(query.get("hub.challenge", ""))
    return 403, ""


def events_from(payload: dict) -> list[tuple[str, str, str | None, dict]]:
    """(kind, external_event_id, channel_external_id, item) for every item the provider batched."""
    out = []
    obj = payload.get("object")
    for entry in payload.get("entry") or []:
        if obj == "whatsapp_business_account":
            for ch in entry.get("changes") or []:
                v = ch.get("value") or {}
                pn = (v.get("metadata") or {}).get("phone_number_id")
                for m in v.get("messages") or []:
                    if m.get("id"):
                        out.append(("whatsapp_cloud", str(m["id"]), pn, {"message": m, "field": ch.get("field")}))
                for st in v.get("statuses") or []:          # delivery receipts reconcile the outbox by wamid
                    if st.get("id") and st.get("status"):
                        out.append(("whatsapp_cloud", f"status:{st['id']}:{st['status']}", pn, {"status": st}))
        elif obj in ("page", "instagram"):                 # Messenger and Instagram Direct share the messaging shape
            kind = "facebook_page" if obj == "page" else "instagram_business"
            account = entry.get("id")                        # the page id, or the Instagram professional account id
            for m in entry.get("messaging") or []:
                msg = m.get("message") or {}
                if msg.get("is_echo"):                       # our own reply coming back: not a customer message
                    continue
                if msg.get("mid"):
                    out.append((kind, str(msg["mid"]), str(account) if account else None, {"messaging": m}))
    return out


@dataclass
class Handler:
    secrets: list[bytes]
    ingest: IngestPort
    counters: Counters = field(default_factory=Counters)

    def handle(self, headers: dict, raw_body: bytes) -> tuple[int, str]:
        if len(raw_body) > MAX_BODY_BYTES:
            self.counters.oversize += 1
            return 413, "too large"
        h = {k.lower(): v for k, v in headers.items()}
        if not signature_ok(raw_body, h.get("x-hub-signature-256"), self.secrets):
            self.counters.rejected_signatures += 1
            return 401, "bad signature"
        try:
            payload = json.loads(raw_body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError):
            self.counters.unparseable += 1
            return 400, "unparseable"
        items = events_from(payload)
        if not items:                            # signed but nothing we route: keep it for an operator, unrouted
            items = [({"whatsapp_business_account": "whatsapp_cloud", "instagram": "instagram_business"}.get(payload.get("object"), "facebook_page"),
                      "raw:" + hashlib.sha256(raw_body).hexdigest(), None, {"unrecognised": True})]
        for kind, ext_id, channel, item in items:
            r = self.ingest.insert_event(kind, ext_id, True, item, channel)
            if r == "inserted":
                self.counters.inserted += 1
            elif r == "duplicate":
                self.counters.duplicates += 1
            else:
                raise RuntimeError(f"ingest returned {r!r}")
        return 200, "ok"
