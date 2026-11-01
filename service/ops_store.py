"""The operator API's store (service/ops_api.py): one command, one transaction, in the operator's own session.

The session claims make auth.uid() and app.jwt_aal() (service/pg.py Database.tx); app.is_operator() and the
operator policies of 0002 and later decide every row. This module adds what the database does not know:
the command's role (app.operators.role), the written reason for a write outside an open incident, and the audit
row, inserted in the same transaction as the change (policy audit_insert_authenticated: actor_type 'operator',
actor_id = the session user). A refused write leaves neither the change nor its audit row.
"""
from __future__ import annotations

import json
import secrets
import string

from service.ops_api import COMMANDS, ROLES, OpsError

FIELDS = {                                         # the columns each listing returns, in order
    "customers": ("id", "public_ref", "business_name", "sector", "city", "status", "created_at"),
}


def _rows(cur, keys=None) -> list[dict]:
    names = keys or [d.name for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _since(value: str | None, default: str) -> str:
    v = value or default
    return f"{int(v[:-1])} {'hours' if v[-1] == 'h' else 'days'}"


def _db_error(exc) -> OpsError:
    """psycopg's error, as an answer: SQLSTATE class and the database's own code word, never its message."""
    import psycopg.errors as e
    word = (getattr(exc.diag, "message_primary", "") or "").split(" ")[0]
    code = word if word.isupper() and word.replace("_", "").isalpha() and len(word) <= 60 else None
    if isinstance(exc, e.UniqueViolation):
        return OpsError(409, "exists")
    if isinstance(exc, e.ForeignKeyViolation):
        return OpsError(404, "not_found")
    if isinstance(exc, e.CheckViolation):
        return OpsError(409, "check_failed")
    if isinstance(exc, e.InsufficientPrivilege):
        return OpsError(403, code or "denied")
    if isinstance(exc, e.RaiseException):
        return OpsError(409, code or "refused")
    if isinstance(exc, (e.DataError, e.InvalidTextRepresentation)):
        return OpsError(400, "invalid")
    return OpsError(500, "server")


class OpsStore:
    def __init__(self, db, public_url: str | None = None):
        self.db, self.public_url = db, public_url          # this service's https address: the site form posts here

    def run(self, claims: dict, name: str, args: dict):
        import psycopg
        cmd = COMMANDS[name]
        try:
            with self.db.tx(claims=claims) as cur:
                cur.execute("select app.is_operator(), (select role::text from app.operators"
                            " where auth_user_id = auth.uid() and active)")
                ok, role = cur.fetchone()
                if not ok:
                    raise OpsError(403, "not_operator")
                if role not in ROLES[cmd.role]:
                    raise OpsError(403, "role")
                if cmd.write and "reason" not in args:
                    cur.execute("select exists (select 1 from app.incidents where closed_at is null)")
                    if not cur.fetchone()[0]:
                        raise OpsError(400, "reason_required", "reason")
                out = getattr(self, "_" + name.replace(".", "_"))(cur, claims, args, role)
                if cmd.event:
                    details = {k: v for k, v in args.items() if k != "reason"}
                    details.update(out.pop("_details", {}))
                    if "reason" in args:
                        details["reason"] = args["reason"]
                    cur.execute("insert into app.audit_log (actor_type, actor_id, customer_id, action, target, details)"
                                " values ('operator', %s, %s, %s, %s, %s)",
                                (claims["sub"], out.pop("_customer", None), cmd.event, out.pop("_target", None),
                                 json.dumps(details, default=str)))
                for k in [k for k in out if k.startswith("_")]:
                    out.pop(k)
                return out
        except psycopg.Error as exc:
            raise _db_error(exc) from None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _customer(cur, value: str) -> str:
        cur.execute("select id from app.customers where public_ref = %s or id::text = lower(%s)", (value, value))
        row = cur.fetchone()
        if row is None:
            raise OpsError(404, "not_found", "customer")
        return str(row[0])

    @staticmethod
    def _one(cur, field_name: str = "id"):
        row = cur.fetchone()
        if row is None:
            raise OpsError(404, "not_found", field_name)
        return row

    # ------------------------------------------------------------------ identity
    def _whoami(self, cur, claims, args, role):
        cur.execute("select display_name from app.operators where auth_user_id = auth.uid()")
        return {"sub": claims["sub"], "role": role, "display_name": cur.fetchone()[0], "aal": claims.get("aal")}

    # ------------------------------------------------------------------ onboarding
    def _customers_list(self, cur, claims, args, role):
        cur.execute("select " + ", ".join(FIELDS["customers"]) + " from app.customers"
                    " where %(s)s::app.customer_status is null or status = %(s)s::app.customer_status"
                    " order by created_at desc limit 500", {"s": args.get("status")})
        return {"rows": _rows(cur)}

    def _customers_show(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("select " + ", ".join(FIELDS["customers"]) + " from app.customers where id = %s", (cid,))
        out = _rows(cur)[0]
        cur.execute("select count(*) filter (where active) from app.customer_users where customer_id = %s", (cid,))
        out["owners"] = cur.fetchone()[0]
        cur.execute("select kind::text || ':' || status::text, count(*) from app.channel_accounts where customer_id = %s"
                    " group by 1 order by 1", (cid,))
        out["channels"] = dict(cur.fetchall())
        cur.execute("select count(*) filter (where approved_by_owner), count(*) from app.kb_facts where customer_id = %s", (cid,))
        approved, total = cur.fetchone()
        out["facts"] = {"approved": approved, "total": total}
        cur.execute("select count(*) from app.competitors where customer_id = %s and active", (cid,))
        out["competitors"] = cur.fetchone()[0]
        cur.execute("select count(*) from app.approvals where customer_id = %s and decision = 'pending'"
                    " and expires_at > now()", (cid,))
        out["pending_approvals"] = cur.fetchone()[0]
        cur.execute("select status::text, period_end from app.subscriptions where customer_id = %s"
                    " order by created_at desc limit 1", (cid,))
        sub = cur.fetchone()
        out["subscription"] = {"status": sub[0], "period_end": sub[1]} if sub else None
        return out

    def _customers_add(self, cur, claims, args, role):
        ref = args.get("ref") or "cust_" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        cur.execute("insert into app.customers (public_ref, business_name, sector, city, currency_zone)"
                    " values (%s, %s, %s, %s, %s) returning id",
                    (ref, args["name"], args["sector"], args["city"], args["zone"]))
        cid = str(cur.fetchone()[0])
        return {"id": cid, "public_ref": ref, "_customer": cid, "_target": ref}

    def _owners_link(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("insert into app.customer_users (customer_id, auth_user_id, role) values (%s, %s, %s)"
                    " on conflict (customer_id, auth_user_id) do update set active = true, role = excluded.role"
                    " returning id", (cid, args["user_id"], args.get("role", "owner")))
        return {"id": str(cur.fetchone()[0]), "_customer": cid, "_target": args["user_id"]}

    def _owners_unlink(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("update app.customer_users set active = false where customer_id = %s and auth_user_id = %s"
                    " and active returning id", (cid, args["user_id"]))
        self._one(cur, "user_id")
        return {"unlinked": True, "_customer": cid, "_target": args["user_id"]}

    def _facts_list(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("select id, topic, fact, approved_by_owner, valid_until, updated_at from app.kb_facts"
                    " where customer_id = %s order by topic, updated_at desc", (cid,))
        return {"rows": _rows(cur)}

    def _facts_add(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("insert into app.kb_facts (customer_id, topic, fact, valid_until) values (%s, %s, %s, %s) returning id",
                    (cid, args["topic"], args["fact"], args.get("valid_until")))
        fid = str(cur.fetchone()[0])           # approved_by_owner stays false: the owner approves it in the portal
        return {"id": fid, "approved_by_owner": False, "_customer": cid, "_target": fid}

    def _channels_list(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"]) if args.get("customer") else None
        cur.execute("select a.id, c.public_ref as customer, a.kind::text as kind, a.external_id, a.display_name,"
                    " a.status::text as status, a.verified_at from app.channel_accounts a join app.customers c on c.id = a.customer_id"
                    " where %(c)s::uuid is null or a.customer_id = %(c)s::uuid order by c.public_ref, a.kind limit 500", {"c": cid})
        return {"rows": _rows(cur)}

    def _channels_add(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        ext = args.get("external_id") or secrets.token_urlsafe(24)          # site_form: a new site key (32 characters)
        cur.execute("insert into app.channel_accounts (customer_id, kind, external_id, display_name) values (%s, %s, %s, %s)"
                    " returning id", (cid, args["kind"], ext, args.get("name")))
        chid = str(cur.fetchone()[0])
        return {"id": chid, "external_id": ext, "status": "pending_verification", "_customer": cid, "_target": chid,
                "_details": {"external_id": ext}}

    def _channel_state(self, cur, chid: str, to: str, allowed: tuple):
        cur.execute("select customer_id, status::text from app.channel_accounts where id = %s for update", (chid,))
        cid, status = self._one(cur)
        if status not in allowed:
            raise OpsError(409, "state", status)
        cur.execute("update app.channel_accounts set status = %s, verified_at = case when %s = 'active' then now()"
                    " else verified_at end where id = %s", (to, to, chid))
        return {"id": chid, "status": to, "_customer": str(cid), "_target": chid, "_details": {"from": status}}

    def _channels_verify(self, cur, claims, args, role):
        return self._channel_state(cur, args["id"], "active", ("pending_verification", "suspended"))

    def _channels_suspend(self, cur, claims, args, role):
        return self._channel_state(cur, args["id"], "suspended", ("pending_verification", "active"))

    def _competitors_list(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("select c.id, c.label, c.url, c.active, (select max(fetched_at) from app.competitor_snapshots s"
                    " where s.competitor_id = c.id) as last_check from app.competitors c where c.customer_id = %s"
                    " order by c.active desc, c.label", (cid,))
        return {"rows": _rows(cur)}

    def _competitors_add(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("insert into app.competitors (customer_id, url, label) values (%s, %s, %s)"
                    " on conflict (customer_id, url) do update set active = true, label = excluded.label returning id",
                    (cid, args["url"], args["label"]))
        kid = str(cur.fetchone()[0])
        return {"id": kid, "_customer": cid, "_target": kid}

    def _competitors_remove(self, cur, claims, args, role):
        cur.execute("update app.competitors set active = false where id = %s and active returning customer_id", (args["id"],))
        cid = self._one(cur)[0]
        return {"removed": True, "_customer": str(cid), "_target": args["id"]}

    # ------------------------------------------------------------------ agents, calls, cost, queue
    def _agent_status(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"]) if args.get("customer") else None
        cur.execute("select s.agent_id, c.public_ref as customer, s.month_id, s.calls, s.spent_usd, s.reserved_usd,"
                    " s.consecutive_failures, s.circuit::text as circuit, s.circuit_opened_at from app.agent_state s"
                    " join app.customers c on c.id = s.customer_id where s.month_id = to_char(now(), 'YYYY-MM')"
                    " and (%(a)s::text is null or s.agent_id = %(a)s) and (%(c)s::uuid is null or s.customer_id = %(c)s::uuid)"
                    " order by s.agent_id, c.public_ref limit 500", {"a": args.get("agent"), "c": cid})
        rows = _rows(cur)
        cur.execute("select agent_id, reason, paused_at from app.agent_pauses where released_at is null"
                    " and (%(a)s::text is null or agent_id = %(a)s) order by agent_id", {"a": args.get("agent")})
        return {"rows": rows, "paused": _rows(cur)}

    def _agent_health(self, cur, claims, args, role):
        cur.execute("with s as (select agent_id, count(*) filter (where circuit <> 'closed') as open_circuits,"
                    " sum(calls) as calls, sum(spent_usd) as spent_usd from app.agent_state"
                    " where month_id = to_char(now(), 'YYYY-MM') group by agent_id),"
                    " p as (select agent_id, reason from app.agent_pauses where released_at is null)"
                    " select coalesce(s.agent_id, p.agent_id) as agent_id, coalesce(s.open_circuits, 0) as open_circuits,"
                    " coalesce(s.calls, 0) as calls, coalesce(s.spent_usd, 0) as spent_usd, p.reason as paused"
                    " from s full join p on p.agent_id = s.agent_id order by 1")
        return {"rows": _rows(cur)}

    def _agent_disable(self, cur, claims, args, role):
        cur.execute("insert into app.agent_pauses as p (agent_id, reason) values (%s, 'OPERATOR')"
                    " on conflict (agent_id) do update set reason = 'OPERATOR', paused_at = now(), released_by = null,"
                    " released_at = null, release_reason = null where p.released_at is not null"
                    " returning agent_id", (args["agent"],))
        if cur.fetchone() is None:
            raise OpsError(409, "already_paused")
        return {"agent": args["agent"], "paused": True, "_target": args["agent"]}

    def _agent_enable(self, cur, claims, args, role):
        cur.execute("update app.agent_pauses set released_by = auth.uid(), released_at = now(), release_reason = %s"
                    " where agent_id = %s and released_at is null returning reason", (args["reason"], args["agent"]))
        row = cur.fetchone()
        if row is None:
            raise OpsError(409, "not_paused")
        return {"agent": args["agent"], "paused": False, "_target": args["agent"], "_details": {"pause_reason": row[0]}}

    def _agent_reset_circuit(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("update app.agent_state set circuit = 'closed', consecutive_failures = 0, circuit_opened_at = null,"
                    " probe_in_flight = false, probe_until = null where customer_id = %s and agent_id = %s"
                    " and month_id = to_char(now(), 'YYYY-MM') returning circuit", (cid, args["agent"]))
        self._one(cur, "agent")
        return {"agent": args["agent"], "circuit": "closed", "_customer": cid, "_target": args["agent"]}

    def _calls(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"]) if args.get("customer") else None
        outcome = args.get("outcome")
        neg = bool(outcome and outcome.startswith("!"))
        cur.execute("select k.ts, k.agent_id, c.public_ref as customer, k.tool, k.model, k.tokens_in, k.tokens_out,"
                    " k.cost_usd, k.duration_ms, k.outcome::text as outcome, k.error_code from app.agent_calls k"
                    " left join app.customers c on c.id = k.customer_id"
                    " where k.ts > now() - %(since)s::interval and (%(a)s::text is null or k.agent_id = %(a)s)"
                    " and (%(c)s::uuid is null or k.customer_id = %(c)s::uuid)"
                    " and (%(o)s::text is null or (k.outcome::text = %(o)s) <> %(neg)s)"
                    " order by k.ts desc limit %(n)s",
                    {"since": _since(args.get("since"), "24h"), "a": args.get("agent"), "c": cid,
                     "o": outcome.lstrip("!") if outcome else None, "neg": neg, "n": args.get("limit", 50)})
        return {"rows": _rows(cur)}

    def _cost(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"]) if args.get("customer") else None
        key = "k.agent_id" if args.get("group_by") == "agent" else "coalesce(c.public_ref, '(acquisition)')"
        cur.execute("select " + key + " as key, count(*) as calls, sum(k.cost_usd) as cost_usd from app.agent_calls k"
                    " left join app.customers c on c.id = k.customer_id where k.ts > now() - %(since)s::interval"
                    " and (%(c)s::uuid is null or k.customer_id = %(c)s::uuid) group by 1 order by 3 desc nulls last",
                    {"since": _since(args.get("since"), "30d"), "c": cid})
        return {"rows": _rows(cur)}

    def _queue_breakdown(self, cur, claims, args, role):
        cur.execute("select agent_id, kind, status::text as status, priority, count(*) as tasks,"
                    " min(created_at) as oldest, count(*) filter (where run_after > now()) as deferred from app.tasks"
                    " where status in ('queued', 'running') group by 1, 2, 3, 4 order by 4, 1, 2")
        rows = _rows(cur)
        cur.execute("select status::text, count(*) from app.tasks where status in ('failed', 'escalated')"
                    " and coalesce(finished_at, created_at) > now() - interval '24 hours' group by 1")
        return {"rows": rows, "last_24h": dict(cur.fetchall())}

    # ------------------------------------------------------------------ approvals, outbox, flags
    def _approvals_list(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"]) if args.get("customer") else None
        cur.execute("select a.id, c.public_ref as customer, a.proposal_action, a.decision::text as decision, a.decided_via,"
                    " a.requested_at, a.expires_at, a.consumed_at from app.approvals a"
                    " left join app.customers c on c.id = a.customer_id"
                    " where (%(c)s::uuid is null or a.customer_id = %(c)s::uuid)"
                    " and (not %(p)s or (a.decision = 'pending' and a.expires_at > now()))"
                    " order by a.requested_at desc limit 200", {"c": cid, "p": bool(args.get("pending"))})
        return {"rows": _rows(cur)}

    def _outbox_attention(self, cur, claims, args, role):
        cur.execute("select v.id, c.public_ref as customer, v.topic, v.attempts, v.last_error, v.needs_human_check"
                    " from app.v_outbox_attention v left join app.customers c on c.id = v.customer_id"
                    " order by v.needs_human_check desc, v.id limit 200")
        return {"rows": _rows(cur)}

    def _outbox_resolve(self, cur, claims, args, role):
        cur.execute("select app.resolve_outbox(%s, %s, %s)", (args["id"], args["resolution"], args["reason"]))
        return {"id": args["id"], "resolution": args["resolution"]}

    def _flags_list(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"]) if args.get("customer") else None
        cur.execute("select f.id, c.public_ref as customer, f.source, f.rule_id, f.severity::text as severity,"
                    " f.status::text as status, f.detail, f.created_at from app.quality_flags f"
                    " join app.customers c on c.id = f.customer_id where (%(c)s::uuid is null or f.customer_id = %(c)s::uuid)"
                    " and (not %(o)s or f.status = 'open') order by f.created_at desc limit 200",
                    {"c": cid, "o": bool(args.get("open"))})
        return {"rows": _rows(cur)}

    def _flags_resolve(self, cur, claims, args, role):
        cur.execute("update app.quality_flags set status = %s, resolved_by = auth.uid(), resolved_at = now()"
                    " where id = %s and status = 'open' returning customer_id", (args["status"], args["id"]))
        cid = self._one(cur)[0]
        return {"id": args["id"], "status": args["status"], "_customer": str(cid), "_target": args["id"]}

    # ------------------------------------------------------------------ freeze: no new message reaches the business
    def _freeze(self, cur, claims, args, role):
        """The customer is suspended and so is every active channel: a webhook, a form or a mail is routed to an
        active channel only (0010), so nothing new reaches a task. Effects already approved stay in the outbox,
        where `outbox attention` shows them; the owner's standing switch is the owner's (0022), not touched."""
        cid = self._customer(cur, args["customer"])
        cur.execute("select status::text from app.customers where id = %s for update", (cid,))
        before = cur.fetchone()[0]
        if before == "suspended":
            raise OpsError(409, "already_frozen")
        cur.execute("update app.customers set status = 'suspended' where id = %s", (cid,))
        cur.execute("update app.channel_accounts set status = 'suspended' where customer_id = %s and status = 'active'"
                    " returning id", (cid,))
        channels = sorted(str(r[0]) for r in cur.fetchall())
        return {"customer": args["customer"], "channels_suspended": len(channels), "_customer": cid, "_target": cid,
                "_details": {"previous_status": before, "channels": channels}}

    def _unfreeze(self, cur, claims, args, role):
        """Undoes exactly the last freeze: its audit row names the earlier status and the channels it suspended."""
        cid = self._customer(cur, args["customer"])
        cur.execute("select status::text from app.customers where id = %s for update", (cid,))
        if cur.fetchone()[0] != "suspended":
            raise OpsError(409, "not_frozen")
        cur.execute("select action, details from app.audit_log where customer_id = %s"
                    " and action in ('customer.frozen', 'customer.unfrozen') order by id desc limit 1", (cid,))
        last = cur.fetchone()
        if last is None or last[0] != "customer.frozen":
            raise OpsError(409, "not_frozen_here")             # suspended some other way: decided by a person, not undone here
        details = last[1]
        cur.execute("update app.customers set status = %s where id = %s", (details["previous_status"], cid))
        cur.execute("update app.channel_accounts set status = 'active' where id = any(%s::uuid[]) and status = 'suspended'"
                    " and verified_at is not null returning id", (details["channels"],))
        restored = len(cur.fetchall())
        return {"customer": args["customer"], "status": details["previous_status"], "channels_restored": restored,
                "_customer": cid, "_target": cid}

    # ------------------------------------------------------------------ incidents and gates
    def _incident_list(self, cur, claims, args, role):
        cur.execute("select id, severity::text as severity, category, reported_at, contained_at, fixed_at, closed_at,"
                    " root_cause, prevention from app.incidents where not %(o)s or closed_at is null"
                    " order by reported_at desc limit 200", {"o": bool(args.get("open"))})
        return {"rows": _rows(cur)}

    def _incident_open(self, cur, claims, args, role):
        ids = [self._customer(cur, c) for c in args.get("customers", [])]
        cur.execute("select pg_advisory_xact_lock(hashtext('app.incidents.id'))")      # one number per incident a day
        cur.execute("select 'INC-' || to_char(now(), 'YYYY-MM-DD') || '-' || lpad((count(*) + 1)::text, 3, '0')"
                    " from app.incidents where id like 'INC-' || to_char(now(), 'YYYY-MM-DD') || '-%'")
        iid = cur.fetchone()[0]
        cur.execute("insert into app.incidents (id, severity, category, affected_customers) values (%s, %s, %s, %s::uuid[])",
                    (iid, args["severity"], args["category"], ids))
        return {"id": iid, "_target": iid}

    def _incident_update(self, cur, claims, args, role):
        f, v = args["field"], args["value"]
        expr = "now()" if v == "now" else "%(v)s::timestamptz" if f.endswith("_at") else "%(v)s"
        cur.execute(f"update app.incidents set {f} = {expr} where id = %(id)s and closed_at is null returning id",
                    {"v": v, "id": args["id"]})
        self._one(cur)
        return {"id": args["id"], "field": f, "_target": args["id"]}

    def _incident_close(self, cur, claims, args, role):
        cur.execute("update app.incidents set closed_at = now() where id = %s and closed_at is null returning id", (args["id"],))
        self._one(cur)                         # the table's check refuses a close without root cause and prevention
        return {"id": args["id"], "closed": True, "_target": args["id"]}

    def _gates_record(self, cur, claims, args, role):
        value = args["value"]
        num = value if value.replace(".", "", 1).lstrip("-").isdigit() else None
        cur.execute("insert into app.gate_measurements (gate_id, value_num, value_text, source, note)"
                    " values (%s, %s, %s, %s, %s) returning id",
                    (args["gate"], num, None if num is not None else value, args["source"], args.get("note")))
        gid = str(cur.fetchone()[0])
        return {"id": gid, "_target": args["gate"]}

    def _logs_export(self, cur, claims, args, role):
        """Evidence for one customer: the audit rows and the agent calls of the window. The command line writes them
        to the operator's file; this export is itself an audit row."""
        cid = self._customer(cur, args["customer"])
        since = _since(args.get("since"), "72h")
        cur.execute("select id, ts, actor_type::text as actor_type, actor_id, action, target, details, hash from app.audit_log"
                    " where customer_id = %s and ts > now() - %s::interval order by id", (cid, since))
        audit = _rows(cur)
        cur.execute("select ts, task_id, agent_id, tool, model, tokens_in, tokens_out, cost_usd, duration_ms,"
                    " outcome::text as outcome, error_code from app.agent_calls where customer_id = %s"
                    " and ts > now() - %s::interval order by ts", (cid, since))
        return {"audit": audit, "agent_calls": _rows(cur), "_customer": cid, "_target": cid,
                "_details": {"audit_rows": len(audit)}}

    # ------------------------------------------------------------------ the site (28.39)
    def _site_build(self, cur, claims, args, role):
        """The files to publish, from the facts the owner approved; the operator uploads them (Cloudflare Pages). A read:
        nothing is written, and the same facts give the same content hash."""
        from datetime import date
        from service.pg import site_inputs
        from service.site_builder import content_hash, site_files
        cid = self._customer(cur, args["customer"])
        business, facts, key = site_inputs(cur, cid)
        try:
            files = site_files(business, facts, base_url=args["base_url"], today=date.today(),
                               app_url=self.public_url, site_key=key if self.public_url else None)
        except ValueError:
            raise OpsError(409, "site_invalid") from None
        return {"content_hash": content_hash(files), "bytes": sum(len(v) for v in files.values()),
                "topics": sorted(facts), "contact_form": bool(key and self.public_url),
                "files": {p: v.decode("utf-8") for p, v in files.items()}}

    # ------------------------------------------------------------------ billing (spec 8.1)
    def _billing_list(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("select id, plan_code, price_usd, period_start, period_end, status::text as status from app.subscriptions"
                    " where customer_id = %s order by created_at desc", (cid,))
        subs = _rows(cur)
        cur.execute("select id, subscription_id, number, amount, currency, status::text as status, issued_at, paid_at"
                    " from app.invoices where customer_id = %s order by created_at desc", (cid,))
        invoices = _rows(cur)
        cur.execute("select id, invoice_id, channel::text as channel, receipt_ref, amount, currency, received_at,"
                    " status::text as status, verified_at from app.payments where customer_id = %s order by created_at desc", (cid,))
        return {"subscriptions": subs, "invoices": invoices, "payments": _rows(cur)}

    def _billing_subscribe(self, cur, claims, args, role):
        cid = self._customer(cur, args["customer"])
        cur.execute("insert into app.subscriptions (customer_id, plan_code, price_usd, period_start, period_end)"
                    " values (%s, %s, %s, %s, %s) returning id",
                    (cid, args.get("plan", "base_annual"), args["price_usd"], args["start"], args["end"]))
        sid = str(cur.fetchone()[0])
        cur.execute("update app.customers set status = 'pending_payment' where id = %s and status = 'lead'", (cid,))
        return {"id": sid, "status": "pending", "_customer": cid, "_target": sid}

    def _billing_invoice(self, cur, claims, args, role):
        cur.execute("select customer_id from app.subscriptions where id = %s", (args["subscription"],))
        cid = self._one(cur, "subscription")[0]
        cur.execute("select pg_advisory_xact_lock(hashtext('app.invoices.number'))")   # one number per invoice a year
        cur.execute("select 'INV-' || to_char(now(), 'YYYY') || '-' || lpad((count(*) + 1)::text, 4, '0')"
                    " from app.invoices where number like 'INV-' || to_char(now(), 'YYYY') || '-%'")
        number = cur.fetchone()[0]
        cur.execute("insert into app.invoices (customer_id, subscription_id, number, amount, currency, fx_rate_to_usd, status,"
                    " issued_at) values (%s, %s, %s, %s, %s, %s, 'issued', now()) returning id",
                    (cid, args["subscription"], number, args["amount"], args["currency"], args.get("fx_rate_to_usd")))
        iid = str(cur.fetchone()[0])
        return {"id": iid, "number": number, "_customer": str(cid), "_target": number}

    def _billing_payment(self, cur, claims, args, role):
        cur.execute("select customer_id, currency, status::text from app.invoices where id = %s", (args["invoice"],))
        cid, currency, status = self._one(cur, "invoice")
        if status != "issued":
            raise OpsError(409, "state", status)
        if args["currency"] != currency:
            raise OpsError(400, "invalid", "currency")         # a payment is recorded in its invoice's currency
        cur.execute("insert into app.payments (customer_id, invoice_id, channel, receipt_ref, amount, currency, fee_amount,"
                    " received_at) values (%s, %s, %s, %s, %s, %s, %s, %s) returning id",
                    (cid, args["invoice"], args["channel"], args["receipt"], args["amount"], args["currency"],
                     args.get("fee", "0"), args["received_at"]))
        pid = str(cur.fetchone()[0])
        return {"id": pid, "status": "pending", "_customer": str(cid), "_target": pid}

    def _billing_match(self, cur, claims, args, role):
        """The human check of spec 8.1: this operator verified the receipt. The invoice is paid once the matched
        payments reach its amount."""
        cur.execute("update app.payments set status = 'matched', verified_by = auth.uid(), verified_at = now()"
                    " where id = %s and status = 'pending' returning customer_id, invoice_id", (args["payment"],))
        row = cur.fetchone()
        if row is None:
            raise OpsError(409, "not_pending")
        cid, iid = row
        cur.execute("update app.invoices i set status = 'paid', paid_at = now() where i.id = %s and i.status = 'issued'"
                    " and (select sum(p.amount) from app.payments p where p.invoice_id = i.id and p.status = 'matched') >= i.amount"
                    " returning number", (iid,))
        paid = cur.fetchone()
        return {"payment": args["payment"], "invoice_paid": paid is not None, "_customer": str(cid), "_target": args["payment"]}

    def _billing_reject(self, cur, claims, args, role):
        cur.execute("update app.payments set status = 'rejected' where id = %s and status = 'pending' returning customer_id",
                    (args["payment"],))
        row = cur.fetchone()
        if row is None:
            raise OpsError(409, "not_pending")
        return {"payment": args["payment"], "status": "rejected", "_customer": str(row[0]), "_target": args["payment"]}

    def _billing_activate(self, cur, claims, args, role):
        cur.execute("update app.subscriptions set status = 'active' where id = %s and status = 'pending'"
                    " returning customer_id", (args["subscription"],))     # the database refuses it without a matched payment
        row = cur.fetchone()
        if row is None:
            raise OpsError(409, "not_pending")
        cid = row[0]
        cur.execute("update app.customers set status = 'active' where id = %s"
                    " and status in ('lead', 'pending_payment', 'expired')", (cid,))
        return {"subscription": args["subscription"], "status": "active", "_customer": str(cid),
                "_target": args["subscription"]}
