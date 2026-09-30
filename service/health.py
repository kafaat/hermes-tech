"""GET /deps: the verdict on app.health_signals() for an external uptime monitor (numbers in, numbers out).
Kept apart from service.app so it is tested without a database driver (tests/test_service_telemetry.py)."""
from __future__ import annotations

SIGNALS = ("retention_age_seconds", "overdue_bodies", "outbox_attention", "webhook_backlog", "webhook_unrouted")


def assess(signals: dict, retention_max_age_hours: float = 26) -> tuple[int, dict]:
    """Signals from app.health_signals() -> (status, body). The purge runs daily: 26 h leaves two hours of slack
    before a missed or skipped run alerts. Unrouted events wait for an operator by design and never fail."""
    age = signals["retention_age_seconds"]
    failing = [name for name, bad in (
        ("retention_stale", age is None or age > retention_max_age_hours * 3600),
        ("overdue_bodies", signals["overdue_bodies"] > 0),
        ("outbox_attention", signals["outbox_attention"] > 0),
        ("webhook_backlog", signals["webhook_backlog"] > 0)) if bad]
    return (503 if failing else 200), {"status": "degraded" if failing else "ok", "failing": failing,
                                       **{k: signals[k] for k in SIGNALS}}
