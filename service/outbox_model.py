"""Executable mirror of the outbox state machine in 0010 (app.outbox_before_write + dispatch functions).

Used by the dispatcher tests as the fake database, so the Python tests exercise the SAME transition rules
the trigger enforces. tests/test_service_dispatcher.py checks that every error code raised here also exists
in 0010; the SQL itself is exercised by db/tests (cases 40, 41) and db/tests/concurrency_outbox.sh.
"""
from __future__ import annotations
import itertools, uuid
from dataclasses import dataclass, field


class DbError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class Row:
    id: int
    topic: str
    payload: dict
    customer_id: str = "c1"
    sending_until: float | None = None
    sending_token: str | None = None
    attempts: int = 0
    dispatched_at: float | None = None
    provider_message_id: str | None = None
    failed_at: float | None = None
    last_error: str | None = None
    needs_human_check: bool = False
    resolution: str | None = None


@dataclass
class OutboxModel:
    idempotent_topics: set = field(default_factory=lambda: {"site.deploy_prod"})
    now: float = 1000.0
    rows: dict = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    max_attempts: int = 5

    def add(self, topic, payload):
        r = Row(next(self._ids), topic, payload)
        self.rows[r.id] = r
        return r.id

    # --------------------------------------------------------- the trigger (UPDATE branch)
    def _update(self, r: Row, operator: bool = False, **new):
        old = Row(**r.__dict__)
        n = Row(**{**r.__dict__, **new})
        if old.dispatched_at is not None or old.failed_at is not None:
            if any(getattr(n, k) != getattr(old, k) for k in ("dispatched_at", "failed_at", "provider_message_id",
                                                                "sending_until", "sending_token", "needs_human_check", "resolution")):
                raise DbError("OUTBOX_FINAL")
        elif old.needs_human_check:
            if not n.needs_human_check:
                if not operator:
                    raise DbError("OUTBOX_NEEDS_HUMAN")
                if n.resolution is None:
                    raise DbError("RESOLUTION_REQUIRED")
                if n.resolution == "confirmed_sent":
                    n.dispatched_at = n.dispatched_at or self.now
                if n.resolution == "abandon":
                    n.failed_at, n.last_error = n.failed_at or self.now, n.last_error or "ABANDONED"
                if n.resolution == "resend":
                    n.sending_until = n.sending_token = None
            elif any(getattr(n, k) != getattr(old, k) for k in ("dispatched_at", "failed_at", "sending_until", "sending_token")):
                raise DbError("OUTBOX_NEEDS_HUMAN")
        elif n.needs_human_check:
            if n.dispatched_at is not None or n.failed_at is not None:
                raise DbError("OUTBOX_BAD_TRANSITION")
        else:
            live = old.sending_until is not None and old.sending_until > self.now
            if (n.sending_until, n.sending_token) != (old.sending_until, old.sending_token):
                if n.sending_until is not None and n.sending_until > self.now + 600:
                    raise DbError("OUTBOX_LEASE_TOO_LONG")
                if old.sending_until is None:
                    if n.sending_token is None or n.sending_until is None or n.sending_until <= self.now:
                        raise DbError("OUTBOX_BAD_CLAIM")
                elif live:
                    heartbeat = n.sending_token == old.sending_token and n.sending_until is not None and n.sending_until > old.sending_until
                    release = n.sending_until is None and n.sending_token is None and (n.last_error or "").startswith("BEFORE_SEND")
                    if not (heartbeat or release):
                        raise DbError("OUTBOX_LEASE_HELD")
                elif not (n.topic in self.idempotent_topics and n.sending_token is not None and n.sending_until is not None
                          and n.sending_until > self.now):
                    raise DbError("OUTBOX_AMBIGUOUS_NEEDS_HUMAN")
            if n.dispatched_at is not None and old.dispatched_at is None:
                if old.sending_until is None:
                    raise DbError("OUTBOX_NOT_CLAIMED")
                if not live and n.provider_message_id is None:
                    raise DbError("OUTBOX_AMBIGUOUS_NEEDS_HUMAN")
            if n.failed_at is not None and old.failed_at is None and old.sending_until is not None and not live:
                raise DbError("OUTBOX_AMBIGUOUS_NEEDS_HUMAN")
        if n.dispatched_at is not None and n.failed_at is not None:
            raise DbError("outbox_one_outcome")
        self.rows[r.id] = n

    # --------------------------------------------------------- the functions (DbPort)
    def claim(self, outbox_id, lease=120):
        o = self.rows[outbox_id]
        if o.dispatched_at is not None or o.failed_at is not None or o.needs_human_check:
            return None
        if o.sending_until is not None:
            if o.sending_until > self.now:
                return None
            if o.topic not in self.idempotent_topics:
                self._update(o, needs_human_check=True, last_error="AMBIGUOUS_LEASE_EXPIRED")
                return None
        if o.attempts >= self.max_attempts:
            self._update(o, needs_human_check=True, last_error="MAX_ATTEMPTS")
            return None
        tok = str(uuid.uuid4())
        self._update(o, sending_until=self.now + min(max(lease, 10), 600), sending_token=tok, attempts=o.attempts + 1)
        return tok

    def row(self, outbox_id):
        o = self.rows[outbox_id]
        return {"id": o.id, "topic": o.topic, "payload": o.payload, "customer_id": o.customer_id}

    def finish(self, outbox_id, token, outcome, provider_ref=None, error=None):
        if outcome not in ("sent", "failed_permanent", "failed_before_send", "ambiguous"):
            raise DbError("OUTBOX_OUTCOME_UNKNOWN")
        o = self.rows[outbox_id]
        if o.sending_token != token:
            return False
        if outcome == "sent":
            self._update(o, dispatched_at=self.now, provider_message_id=provider_ref)
        elif outcome == "failed_permanent":
            self._update(o, failed_at=self.now, last_error=(error or "FAILED_PERMANENT")[:200])
        elif outcome == "failed_before_send":
            self._update(o, sending_until=None, sending_token=None, last_error="BEFORE_SEND:" + (error or "")[:180])
        else:
            self._update(o, needs_human_check=True, last_error="AMBIGUOUS:" + (error or "")[:180])
        return True

    def reconcile(self, outbox_id, provider_ref):
        if not provider_ref or len(provider_ref) < 4:
            raise DbError("PROVIDER_REF_REQUIRED")
        o = self.rows[outbox_id]
        if o.dispatched_at is None and o.failed_at is None and not o.needs_human_check and o.sending_until is not None:
            self._update(o, dispatched_at=self.now, provider_message_id=provider_ref)
            return True
        return False

    def resolve(self, outbox_id, resolution, reason, operator=True):
        if not operator:
            raise DbError("OPERATOR_AAL2_ONLY")
        if len((reason or "").strip()) < 5:
            raise DbError("REASON_REQUIRED")
        o = self.rows[outbox_id]
        if not o.needs_human_check:
            raise DbError("OUTBOX_NOT_WAITING_FOR_HUMAN")
        self._update(o, operator=True, needs_human_check=False, resolution=resolution, last_error="RESOLVED: " + reason[:180])
