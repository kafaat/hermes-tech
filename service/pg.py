"""Postgres implementations of the service ports (P1: the ports in webhook.py and dispatcher.py, for real).

Every operation is ONE short transaction under ONE narrow role:
  Ingest       hermes_ingest   inserts a signed event; the database routes the tenant and enqueues the task
  WorkerDb     hermes_worker   task lease functions; every tenant read or write is bound to the live lease
  OutboxPort   hermes_worker   the dispatcher's DbPort, bound to the lease of the task that owns the effect
The role is taken with SET LOCAL ROLE, so the login used by a process must be a member of that role and
nothing else. Production: one login role per process (webhook process -> hermes_ingest only; worker process ->
hermes_worker only). Staging and CI log in as the migration owner and narrow per transaction: the policies
and grants that decide the outcome are the same, because they apply to the role in effect.
"""
from __future__ import annotations
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb


class Database:
    def __init__(self, url: str, role: str):
        self.url, self.role = url, role

    @contextmanager
    def tx(self, bind: tuple | None = None):
        """One transaction as self.role; bind=(task_id, token) binds the worker to its live task lease first."""
        with psycopg.connect(self.url, autocommit=False) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("set local role {}").format(sql.Identifier(self.role)))
                if bind is not None:
                    cur.execute("select app.bind_task(%s, %s)", bind)
                yield cur


class Ingest:
    """webhook.IngestPort. A redelivery hits unique (kind, external_event_id) and is reported as a duplicate."""

    def __init__(self, db: Database):
        self.db = db

    def insert_event(self, kind, external_event_id, signature_valid, payload, channel_external_id):
        try:
            with self.db.tx() as cur:
                cur.execute("insert into app.webhook_events (kind, external_event_id, signature_valid, payload, channel_external_id)"
                            " values (%s, %s, %s, %s, %s)",
                            (kind, external_event_id, signature_valid, Jsonb(payload), channel_external_id))
            return "inserted"
        except psycopg.errors.UniqueViolation:
            return "duplicate"


class OutboxPort:
    """dispatcher.DbPort, bound to the lease of the task that owns the effect."""

    def __init__(self, db: Database, bind: tuple):
        self.db, self.bind = db, bind

    def _one(self, query, args):
        with self.db.tx(self.bind) as cur:
            cur.execute(query, args)
            return cur.fetchone()[0]

    def claim(self, outbox_id):
        tok = self._one("select app.claim_outbox_dispatch(%s, 120)", (outbox_id,))
        return str(tok) if tok else None

    def row(self, outbox_id):
        with self.db.tx(self.bind) as cur:
            cur.execute("select id, customer_id, topic, payload, target_id from app.outbox where id = %s", (outbox_id,))
            r = cur.fetchone()
        return {"id": r[0], "customer_id": str(r[1]), "topic": r[2], "payload": r[3], "target_id": r[4]}

    def finish(self, outbox_id, token, outcome, provider_ref=None, error=None):
        return self._one("select app.finish_outbox_dispatch(%s, %s, %s, %s, %s)", (outbox_id, token, outcome, provider_ref, error))

    def reconcile(self, outbox_id, provider_ref):
        return self._one("select app.reconcile_outbox_sent(%s, %s)", (outbox_id, provider_ref))
