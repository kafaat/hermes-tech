"""Postgres implementations of the service ports (P1: the ports in webhook.py and dispatcher.py, for real).

Every operation is ONE short transaction under ONE narrow role:
  Ingest       hermes_ingest   inserts a signed event; the database routes the tenant and enqueues the task
  WorkerDb     hermes_worker   task lease functions; every tenant read or write is bound to the live lease
  OutboxPort   hermes_worker   the dispatcher's DbPort, bound to the lease of the task that owns the effect
  Portal       authenticated   the owner's own session claims (service/auth.py verified them); policies decide rows
The role is taken with SET LOCAL ROLE, so the login used by a process must be a member of that role and
nothing else. Production: one login role per process (webhook process -> hermes_ingest only; worker process ->
hermes_worker only). Staging and CI log in as the migration owner and narrow per transaction: the policies
and grants that decide the outcome are the same, because they apply to the role in effect.
"""
from __future__ import annotations
import json
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from service.dispatcher import DISPATCH_LEASE_SECONDS


class Database:
    def __init__(self, url: str, role: str):
        self.url, self.role = url, role

    @contextmanager
    def tx(self, bind: tuple | None = None, claims: dict | None = None):
        """One transaction as self.role; bind=(task_id, token) binds the worker to its live task lease first;
        claims (verified session claims) become the session identity that auth.uid() and the policies read."""
        with psycopg.connect(self.url, autocommit=False) as conn:
            with conn.cursor() as cur:
                if claims is not None:
                    cur.execute("select set_config('request.jwt.claim.sub', %s, true), set_config('request.jwt.claims', %s, true)",
                                (claims["sub"], json.dumps(claims)))
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
        tok = self._one("select app.claim_outbox_dispatch(%s, %s)", (outbox_id, DISPATCH_LEASE_SECONDS))
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


class PortalDb:
    """portal.StorePort as the owner: every query runs in the owner's own session (role authenticated with the
    verified claims), so current_user_customer_ids() and the approvals/kb_facts policies decide what exists.
    Queries also filter by the owner's customer ids explicitly: the policies alone do not let the planner use
    the customer_id indexes (0013)."""

    def __init__(self, db: Database):
        self.db = db

    def overview(self, claims: dict) -> dict:
        with self.db.tx(claims=claims) as cur:
            cur.execute("select id, business_name from app.customers order by business_name")
            customers = [{"id": str(r[0]), "name": r[1]} for r in cur.fetchall()]
            ids = [c["id"] for c in customers]
            cur.execute("select id, customer_id, proposal_action, payload, expires_at from app.approvals"
                        " where customer_id = any(%s::uuid[]) and decision = 'pending'"
                        " and (expires_at is null or expires_at > now()) order by requested_at", (ids,))
            approvals = [{"id": str(r[0]), "customer_id": str(r[1]), "action": r[2], "payload": r[3] or {},
                          "expires_at": r[4]} for r in cur.fetchall()]
            cur.execute("select received_at, matched_category::text, owner_inquiry, body from app.inquiries"
                        " where customer_id = any(%s::uuid[]) and received_at > now() - interval '7 days'"
                        " order by received_at desc limit 50", (ids,))
            inquiries = [{"received_at": r[0], "category": r[1], "owner_inquiry": r[2], "body": r[3]} for r in cur.fetchall()]
            cur.execute("select id, topic, fact, approved_by_owner from app.kb_facts where customer_id = any(%s::uuid[])"
                        " order by approved_by_owner, topic limit 200", (ids,))
            facts = [{"id": str(r[0]), "topic": r[1], "fact": r[2], "approved": r[3]} for r in cur.fetchall()]
            cur.execute("select c.label, s.fetched_at, s.status::text, s.diff_summary from app.competitors c"
                        " left join lateral (select fetched_at, status, diff_summary from app.competitor_snapshots s"
                        "   where s.competitor_id = c.id and s.customer_id = c.customer_id order by fetched_at desc limit 1) s on true"
                        " where c.customer_id = any(%s::uuid[]) and c.active order by c.label", (ids,))
            competitors = [{"label": r[0], "fetched_at": r[1], "status": r[2], "summary": r[3]} for r in cur.fetchall()]
        return {"customers": customers, "approvals": approvals, "inquiries": inquiries, "facts": facts,
                "competitors": competitors}

    def decide(self, claims: dict, approval_id: str, decision: str) -> bool:
        with self.db.tx(claims=claims) as cur:
            cur.execute("update app.approvals set decision = %s::app.approval_decision, decided_by = %s"
                        " where id = %s and decision = 'pending'", (decision, claims["sub"], approval_id))
            return cur.rowcount == 1

    def approve_fact(self, claims: dict, fact_id: str) -> bool:
        with self.db.tx(claims=claims) as cur:
            cur.execute("update app.kb_facts set approved_by_owner = true where id = %s and not approved_by_owner", (fact_id,))
            return cur.rowcount == 1

    def ops_overview(self, claims: dict) -> dict | None:
        """None unless app.is_operator() holds in this session (an active operator AND aal2); the operator
        policies then open the outbox, customers, retention and webhook rows."""
        with self.db.tx(claims=claims) as cur:
            cur.execute("select app.is_operator()")
            if not cur.fetchone()[0]:
                return None
            cur.execute("select v.id, c.business_name, v.topic, v.attempts, v.last_error, v.needs_human_check"
                        " from app.v_outbox_attention v left join app.customers c on c.id = v.customer_id"
                        " order by v.needs_human_check desc, v.id limit 200")
            rows = [{"id": r[0], "customer": r[1], "topic": r[2], "attempts": r[3], "last_error": r[4],
                     "needs_human_check": r[5]} for r in cur.fetchall()]
            cur.execute("select last_run_at, overdue_bodies from app.v_retention_status")
            last_run, overdue = cur.fetchone()
            cur.execute("select count(*) from app.webhook_events where customer_id is null and signature_valid")
            unrouted = cur.fetchone()[0]
            cur.execute("select count(*) filter (where s.status = 'ok'), count(*) filter (where s.status = 'unverifiable'),"
                        " count(*) filter (where s.status = 'blocked'), count(*) from app.competitors c left join lateral"
                        " (select status from app.competitor_snapshots s where s.competitor_id = c.id order by fetched_at desc limit 1) s"
                        " on true where c.active")
            structured, unstructured, blocked, active = cur.fetchone()
        return {"rows": rows, "retention_last_run": last_run, "overdue_bodies": overdue, "unrouted": unrouted,
                "competitors": {"structured": structured, "unstructured": unstructured, "blocked": blocked, "active": active}}

    def resolve(self, claims: dict, outbox_id: str, resolution: str, reason: str) -> None:
        """app.resolve_outbox: the database checks the operator (aal2), the reason and that the row waits for a
        human; 'resend' also queues the task that sends it again (0015)."""
        with self.db.tx(claims=claims) as cur:
            cur.execute("select app.resolve_outbox(%s, %s, %s)", (int(outbox_id), resolution, reason))


class CompetitorDb:
    """competitor.Store as hermes_jobs (0017): reads active competitors and their snapshots, inserts one snapshot per
    check for the competitor's own customer; the database refuses more than 10 a customer a month."""

    def __init__(self, db: Database):
        self.db = db

    def due(self, limit: int) -> list[dict]:
        with self.db.tx() as cur:
            cur.execute("""
                select c.id, c.customer_id, c.url, c.label, f.structured_facts, h.page_hash
                  from app.competitors c
                  left join lateral (select max(fetched_at) as at from app.competitor_snapshots s
                                      where s.competitor_id = c.id) l on true
                  left join lateral (select structured_facts from app.competitor_snapshots s
                                      where s.competitor_id = c.id and s.status = 'ok'
                                      order by fetched_at desc limit 1) f on true
                  left join lateral (select page_hash from app.competitor_snapshots s
                                      where s.competitor_id = c.id and s.page_hash is not null
                                      order by fetched_at desc limit 1) h on true
                 where c.active and (l.at is null or l.at < now() - interval '7 days')
                 order by l.at nulls first
                 limit %s""", (limit,))
            return [{"id": str(r[0]), "customer_id": str(r[1]), "url": r[2], "label": r[3], "last_facts": r[4],
                     "last_page_hash": r[5]} for r in cur.fetchall()]

    def insert(self, snap: dict) -> bool:
        try:
            with self.db.tx() as cur:
                cur.execute("insert into app.competitor_snapshots (customer_id, competitor_id, content_hash, diff_summary,"
                            " status, structured_facts, page_hash) values (%s, %s, %s, %s, %s::app.snapshot_status, %s, %s)",
                            (snap["customer_id"], snap["competitor_id"], snap["content_hash"], snap["diff_summary"],
                             snap["status"], Jsonb(snap["structured_facts"]) if snap["structured_facts"] is not None else None,
                             snap["page_hash"]))
            return True
        except psycopg.errors.InsufficientPrivilege:       # the policy's refusal: monthly cap reached, or competitor inactive
            return False
