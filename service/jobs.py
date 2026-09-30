"""Scheduled system jobs. Each runs under the narrow role named here, never under the worker or service role
unless stated; the scheduler (orchestrator timer) records every run and an alert fires on a missed day.

  inquiry_body_30d   daily   hermes_jobs    select app.purge_inquiry_bodies()   (claim C4.7; SQL case 45)
  audit_checkpoint   daily   service_role   tools/audit_checkpoint.py append     (claim A15b)
  outbox_attention   5 min   operator view  app.v_outbox_attention -> alert when non-empty
Between runs, GET /deps on hermes-app (app.health_signals(), 0011) reports a missed purge and every backlog.
"""
from __future__ import annotations
import os, sys

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


def main(argv=None):
    """python -m service.jobs <job>   (Railway cron: runs once and exits; DATABASE_URL must allow SET ROLE <role>).
    Only jobs whose role is a plain database role run here; the operator view needs an aal2 session and does not."""
    import psycopg
    from psycopg import sql
    job = (argv or sys.argv[1:] or [""])[0]
    if job not in JOBS or " " in JOBS[job]["role"]:
        sys.exit(f"usage: python -m service.jobs <{'|'.join(j for j, s in JOBS.items() if ' ' not in s['role'])}>")

    def execute(role, query):                        # Railway cron never kills a hung run and skips the next one while
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=10,   # it is active: bound every wait here
                             options="-c statement_timeout=300000 -c lock_timeout=30000") as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            cur.execute(query)
            return cur.fetchall()
    rows = run(job, execute)
    print(f"{job}: {rows}")


if __name__ == "__main__":
    main()
