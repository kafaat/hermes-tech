"""GET /deps: the verdict on app.health_signals() for an external uptime monitor (numbers in, numbers out).
Kept apart from service.app so it is tested without a database driver (tests/test_service_telemetry.py)."""
from __future__ import annotations
import hmac

SIGNALS = ("retention_age_seconds", "retention_due_at", "retention_stale", "overdue_bodies", "outbox_attention",
           "webhook_backlog", "webhook_unrouted")


def assess(signals: dict) -> tuple[int, dict]:
    """Signals from app.health_signals() -> (status, body). The database decides when the purge is due (0012: last
    success, or the start of monitoring, + 26 h), so a new environment does not alert before its first run and no
    reader of /deps needs to know the schedule. Unrouted events wait for an operator by design and never fail."""
    failing = [name for name, bad in (
        ("retention_stale", bool(signals["retention_stale"])),
        ("overdue_bodies", signals["overdue_bodies"] > 0),
        ("outbox_attention", signals["outbox_attention"] > 0),
        ("webhook_backlog", signals["webhook_backlog"] > 0)) if bad]
    due = signals["retention_due_at"]
    return (503 if failing else 200), {"status": "degraded" if failing else "ok", "failing": failing,
                                       **{k: signals[k] for k in SIGNALS},
                                       "retention_due_at": due.isoformat() if hasattr(due, "isoformat") else due}


def authorized(header: str | None, token: str) -> bool:
    """The numbers are an operating pulse (backlog, overdue personal data): only a caller holding HERMES_MONITOR_TOKEN
    in X-Monitor-Token gets them. Without a configured token nobody does. Everyone else gets the status code and one
    word, which is all a basic uptime check reads."""
    return bool(token) and hmac.compare_digest((header or "").encode(), token.encode())
