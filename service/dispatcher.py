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
