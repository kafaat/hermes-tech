"""Scheduled system jobs. Each runs under the narrow role named here, never under the worker or service role
unless stated; the scheduler (orchestrator timer) records every run and an alert fires on a missed day.

  inquiry_body_30d   daily   hermes_jobs    select app.purge_inquiry_bodies()   (claim C4.7; SQL case 45)
  audit_checkpoint   daily   service_role   tools/audit_checkpoint.py append     (claim A15b)
  outbox_attention   5 min   operator view  app.v_outbox_attention -> alert when non-empty
"""
from __future__ import annotations

JOBS = {
    "inquiry_body_30d": {"every": "daily", "role": "hermes_jobs", "sql": "select app.purge_inquiry_bodies()"},
    "audit_checkpoint": {"every": "daily", "role": "service_role", "sql": "select chain_seq, hash from app.audit_head()"},
    "outbox_attention": {"every": "5 minutes", "role": "authenticated (aal2 operator session)",
                         "sql": "select count(*) from app.v_outbox_attention"},
}


def run(job: str, execute):
    """execute(role, sql) -> rows. Returns the rows; raises if the job is unknown."""
    spec = JOBS[job]
    return execute(spec["role"], spec["sql"])
