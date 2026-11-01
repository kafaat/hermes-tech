"""The operator API behind the `hermes` command line (cli/hermes.py, spec 14 and 28.35).

    OpsApi(store, secret, keys).handle(method, path, headers, body) -> (status, headers, body)

The command line never holds a database credential. It sends the operator's own Supabase access token as a Bearer
header; this process verifies it (service/auth.py, the portal's check), and the store runs the command in that
operator's database session, where app.is_operator() (an active operator AND aal2) and the operator policies decide.

  POST /ops/api/<command>          Authorization: Bearer <token>, body {"args": {...}}  -> {"ok": true, "data": ...}
  POST /ops/api/session/staging    {"code": <HERMES_STAGING_LOGIN_CODE>}  -> {"access_token": ...} for
                                   HERMES_STAGING_OPERATOR_ID (aal2); staging only, as the portal's staging login

Answers: 400 invalid input, 401 no valid session, 403 not an operator / not this role / not aal2, 404 not found,
409 the state refuses it (the database's reason as the code), 500 server. The command line maps them to the exit
codes of spec 14 (2, 3, 3, 4, 5, 10). No answer carries an exception text.

Rules this module enforces before the database is touched (each command in COMMANDS):
  * every argument is typed and bounded; an unknown argument is refused, not ignored;
  * the operator's role is at least the command's role (founder > tech > ops; finance for billing alone);
  * a write needs a written reason unless an incident is open (cli/hermes_cli_spec.md, rule 2), checked by the store;
  * a write and its audit row (app.audit_log, the event named here) commit in one transaction.
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from service.auth import AuthError, issue_staging_token, verify_session_token

log = logging.getLogger("hermes.ops")

MAX_BODY_BYTES = 16 * 1024
PREFIX = "/ops/api/"

# who may run what: a command names the lowest role; finance is a separate line (money), not a rank
ROLES = {
    "read":    frozenset({"founder", "tech", "ops", "finance"}),
    "ops":     frozenset({"founder", "tech", "ops"}),
    "tech":    frozenset({"founder", "tech"}),
    "founder": frozenset({"founder"}),
    "finance": frozenset({"founder", "finance"}),
}


class OpsError(Exception):
    """An answer the caller acts on: status and a short code, never a stack or a database message."""

    def __init__(self, status: int, code: str, field_name: str | None = None):
        super().__init__(code)
        self.status, self.code, self.field = status, code, field_name


# ------------------------------------------------------------------ argument types (each returns the clean value)
def _text(limit: int, minimum: int = 1):
    def check(v):
        if not isinstance(v, str) or not (minimum <= len(v.strip()) <= limit) or "\x00" in v:
            raise ValueError
        return v.strip()
    return check


def _pattern(regex: str):
    rx = re.compile(regex)

    def check(v):
        if not isinstance(v, str) or not rx.fullmatch(v):
            raise ValueError
        return v
    return check


def _enum(*values):
    def check(v):
        if v not in values:
            raise ValueError
        return v
    return check


def _uuid(v):
    return str(uuid.UUID(str(v))) if isinstance(v, str) and len(v) <= 40 else _fail()


def _fail():
    raise ValueError


def _int(lo: int, hi: int):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError
        return v
    return check


def _money(v):
    """A positive amount with at most two decimals, as text: the database keeps numeric, never a float."""
    s = str(v) if isinstance(v, (int, str)) and not isinstance(v, bool) else _fail()
    if not re.fullmatch(r"\d{1,10}(\.\d{1,2})?", s) or float(s) <= 0:
        raise ValueError
    return s


def _rate(v):
    s = str(v) if isinstance(v, (int, str)) and not isinstance(v, bool) else _fail()
    if not re.fullmatch(r"\d{1,8}(\.\d{1,6})?", s) or float(s) <= 0:
        raise ValueError
    return s


def _date(v):
    return date.fromisoformat(v).isoformat() if isinstance(v, str) and len(v) == 10 else _fail()


def _moment(v):
    """'now' or an ISO timestamp with its offset: an incident time is never guessed in the server's zone."""
    if v == "now":
        return v
    if not isinstance(v, str) or len(v) > 40:
        raise ValueError
    t = datetime.fromisoformat(v)
    if t.tzinfo is None:
        raise ValueError
    return t.isoformat()


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError
    return v


def _customers(v):
    if not isinstance(v, list) or len(v) > 50:
        raise ValueError
    return [CUSTOMER(x) for x in v]


CUSTOMER = _pattern(r"cust_[0-9a-z]{4,40}|[0-9a-fA-F-]{36}")      # public reference or id; the store resolves it
AGENT = _pattern(r"agent_[a-z][a-z0-9_]{2,31}")
SINCE = _pattern(r"\d{1,3}[hd]")
REASON = _text(500, 5)
EXTERNAL_ID = {                                    # the identity each channel kind is routed by (webhook, form, mail)
    "whatsapp_cloud": re.compile(r"\d{5,30}"),
    "facebook_page": re.compile(r"\d{5,30}"),
    "instagram_business": re.compile(r"\d{5,30}"),
    "site_form": re.compile(r"[A-Za-z0-9_-]{24,64}"),                                   # service/site_form.py KEY
    "email": re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9-]+(\.[a-z0-9-]+)+"),              # service/email_inbound.py
}
OUTCOMES = ("success", "failure", "escalated", "rejected", "budget_exceeded", "circuit_open")


@dataclass(frozen=True)
class Command:
    role: str
    write: bool = False
    event: str | None = None                       # the audit action a write records with its reason
    args: dict = field(default_factory=dict)       # name -> (check, required)
    reason_required: bool = False                  # true: an open incident does not stand in for the reason


def _a(**spec):
    return {k: (v if isinstance(v, tuple) else (v, False)) for k, v in spec.items()}


R = True                                           # marks a required argument: name=(check, R)
COMMANDS = {
    "whoami":              Command("read"),
    # ---- onboarding: a business, its owner's login, its channels, its facts and competitors
    "customers.list":      Command("read", args=_a(status=_enum("lead", "pending_payment", "active", "suspended",
                                                                "expired", "cancelled"))),
    "customers.show":      Command("read", args=_a(customer=(CUSTOMER, R))),
    "customers.add":       Command("ops", True, "customer.created", _a(
                               name=(_text(120), R), sector=(_enum("restaurant", "home_services", "retail", "education",
                                                                   "other"), R),
                               city=(_text(60), R), zone=(_enum("zone_a", "zone_b"), R),
                               ref=_pattern(r"cust_[0-9a-z]{4,40}"))),
    "owners.link":         Command("ops", True, "owner.linked", _a(customer=(CUSTOMER, R), user_id=(_uuid, R),
                                                                   role=_enum("owner", "staff"))),
    "owners.unlink":       Command("ops", True, "owner.unlinked", _a(customer=(CUSTOMER, R), user_id=(_uuid, R))),
    "facts.list":          Command("read", args=_a(customer=(CUSTOMER, R))),
    "facts.add":           Command("ops", True, "fact.added", _a(customer=(CUSTOMER, R),
                                                                 topic=(_pattern(r"[a-z][a-z_]{1,39}"), R),
                                                                 fact=(_text(1000), R), valid_until=_date)),
    "channels.list":       Command("read", args=_a(customer=CUSTOMER)),
    "channels.add":        Command("ops", True, "channel.added", _a(customer=(CUSTOMER, R),
                                                                    kind=(_enum(*EXTERNAL_ID), R),
                                                                    external_id=_text(200), name=_text(120))),
    "channels.verify":     Command("ops", True, "channel.verified", _a(id=(_uuid, R))),
    "channels.suspend":    Command("ops", True, "channel.suspended", _a(id=(_uuid, R))),
    "competitors.list":    Command("read", args=_a(customer=(CUSTOMER, R))),
    "competitors.add":     Command("ops", True, "competitor.added", _a(customer=(CUSTOMER, R),
                                                                       url=(_pattern(r"https://[^\s]{4,300}"), R),
                                                                       label=(_text(60), R))),
    "competitors.remove":  Command("ops", True, "competitor.removed", _a(id=(_uuid, R))),
    # ---- agents, calls, cost, queue (spec 14)
    "agent.status":        Command("read", args=_a(agent=AGENT, customer=CUSTOMER)),
    "agent.health":        Command("read"),
    "agent.disable":       Command("ops", True, "agent.disabled", _a(agent=(AGENT, R))),
    "agent.enable":        Command("ops", True, "agent.enabled", _a(agent=(AGENT, R)), reason_required=True),
    "agent.reset_circuit": Command("tech", True, "agent.circuit_reset", _a(agent=(AGENT, R), customer=(CUSTOMER, R))),
    "calls":               Command("read", args=_a(agent=AGENT, customer=CUSTOMER, since=SINCE,
                                                   outcome=_pattern(r"!?(" + "|".join(OUTCOMES) + ")"),
                                                   limit=_int(1, 500))),
    "cost":                Command("read", args=_a(customer=CUSTOMER, since=SINCE,
                                                   group_by=_enum("customer", "agent"))),
    "queue.breakdown":     Command("read"),
    # ---- approvals, outbox, quality flags
    "approvals.list":      Command("read", args=_a(customer=CUSTOMER, pending=_bool)),
    "outbox.attention":    Command("read"),
    "outbox.resolve":      Command("ops", True, None, _a(id=(_int(1, 2**62), R),
                                                         resolution=(_enum("confirmed_sent", "resend", "abandon"), R)),
                                   reason_required=True),          # app.resolve_outbox writes its own audit row
    "flags.list":          Command("read", args=_a(customer=CUSTOMER, open=_bool)),
    "flags.resolve":       Command("ops", True, "flag.resolved", _a(id=(_uuid, R),
                                                                    status=(_enum("confirmed", "dismissed"), R))),
    # ---- a customer stopped as a whole, incidents, gates, evidence
    "freeze":              Command("ops", True, "customer.frozen", _a(customer=(CUSTOMER, R))),
    "unfreeze":            Command("ops", True, "customer.unfrozen", _a(customer=(CUSTOMER, R))),
    "incident.list":       Command("read", args=_a(open=_bool)),
    "incident.open":       Command("ops", True, "incident.opened", _a(
                               severity=(_enum("P0", "P1", "P2", "P3"), R),
                               category=(_enum("circuit", "queue", "budget", "leak", "vendor", "other"), R),
                               customers=_customers)),
    "incident.update":     Command("ops", True, "incident.updated", _a(
                               id=(_pattern(r"INC-\d{4}-\d{2}-\d{2}-\d{3}"), R),
                               field=(_enum("contained_at", "mitigated_at", "diagnosed_at", "fixed_at", "root_cause",
                                            "prevention"), R),
                               value=(_text(2000), R))),
    "incident.close":      Command("ops", True, "incident.closed", _a(id=(_pattern(r"INC-\d{4}-\d{2}-\d{2}-\d{3}"), R))),
    "gates.record":        Command("founder", True, "gate.recorded", _a(gate=(_pattern(r"[0-3]\.[0-9]{1,2}[أبت]?"), R),
                                                                        value=(_text(200), R), source=(_text(200), R),
                                                                        note=_text(500))),
    "logs.export":         Command("founder", True, "logs.exported", _a(customer=(CUSTOMER, R), since=SINCE)),
    # ---- from payment to an active subscription (spec 8.1): a human verifies every payment
    # ---- the customer's site, built from the approved facts (service/site_builder.py, 28.39)
    "site.build":          Command("ops", args=_a(customer=(CUSTOMER, R), base_url=(_pattern(r"https://[a-z0-9-]+(\.[a-z0-9-]+)+/?"), R))),
    "billing.list":        Command("read", args=_a(customer=(CUSTOMER, R))),
    "billing.subscribe":   Command("finance", True, "subscription.created", _a(
                               customer=(CUSTOMER, R), price_usd=(_money, R), start=(_date, R), end=(_date, R),
                               plan=_pattern(r"[a-z][a-z0-9_]{2,39}"))),
    "billing.invoice":     Command("finance", True, "invoice.issued", _a(subscription=(_uuid, R), amount=(_money, R),
                                                                         currency=(_enum("USD", "YER", "SAR"), R),
                                                                         fx_rate_to_usd=_rate)),
    "billing.payment":     Command("finance", True, "payment.recorded", _a(
                               invoice=(_uuid, R), channel=(_enum("bank", "wallet", "exchange", "cash"), R),
                               receipt=(_text(120), R), amount=(_money, R), currency=(_enum("USD", "YER", "SAR"), R),
                               received_at=(_date, R), fee=_money)),
    "billing.match":       Command("finance", True, "payment.matched", _a(payment=(_uuid, R))),
    "billing.reject":      Command("finance", True, "payment.rejected", _a(payment=(_uuid, R))),
    "billing.activate":    Command("finance", True, "subscription.activated", _a(subscription=(_uuid, R))),
}
TIME_FIELDS = ("contained_at", "mitigated_at", "diagnosed_at", "fixed_at")


def clean_args(name: str, raw) -> dict:
    """The command's arguments, typed and bounded, plus 'reason' for a write; OpsError(400) names the first bad one."""
    cmd = COMMANDS.get(name)
    if cmd is None:
        raise OpsError(404, "unknown_command")
    if not isinstance(raw, dict):
        raise OpsError(400, "invalid", "args")
    allowed = set(cmd.args) | ({"reason"} if cmd.write else set())
    for k in raw:
        if k not in allowed:
            raise OpsError(400, "unknown_argument", str(k)[:40])
    out = {}
    for k, (check, required) in cmd.args.items():
        v = raw.get(k)
        if v is None:
            if required:
                raise OpsError(400, "missing", k)
            continue
        try:
            out[k] = check(v)
        except (ValueError, TypeError):
            raise OpsError(400, "invalid", k) from None
    if cmd.write and raw.get("reason") is not None:
        try:
            out["reason"] = REASON(raw["reason"])
        except ValueError:
            raise OpsError(400, "invalid", "reason") from None
    if cmd.reason_required and "reason" not in out:
        raise OpsError(400, "reason_required", "reason")
    if name == "channels.add":
        ext = out.get("external_id")
        if ext is None and out["kind"] != "site_form":         # a site key is generated; every other id is Meta's or a mailbox
            raise OpsError(400, "missing", "external_id")
        if ext is not None and not EXTERNAL_ID[out["kind"]].fullmatch(ext):
            raise OpsError(400, "invalid", "external_id")
    if name == "billing.subscribe" and out["end"] <= out["start"]:
        raise OpsError(400, "invalid", "end")
    if name == "billing.invoice" and out["currency"] != "USD" and "fx_rate_to_usd" not in out:
        raise OpsError(400, "missing", "fx_rate_to_usd")
    if name == "incident.update" and out["field"] in TIME_FIELDS:
        try:
            out["value"] = _moment(out["value"])
        except ValueError:
            raise OpsError(400, "invalid", "value") from None
    return out


def _json(status: int, payload: dict):
    return status, [("Content-Type", "application/json"), ("Cache-Control", "no-store")], \
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class OpsApi:
    def __init__(self, store, secret: str, keys=None, *, staging_login_code: str = "", staging_operator_id: str = "",
                 now=time.time):
        self.store, self.secret, self.keys, self.now = store, secret, keys, now
        self.staging_code, self.staging_operator = staging_login_code, staging_operator_id

    def _claims(self, headers: dict) -> dict:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not auth.startswith("Bearer ") or len(auth) > 8192:
            raise OpsError(401, "no_session")
        try:
            claims = verify_session_token(auth[len("Bearer "):].strip(), self.secret, self.now(), self.keys)
        except AuthError:
            raise OpsError(401, "no_session") from None
        if claims.get("aal") != "aal2":            # the database refuses it too; this answer tells the CLI why
            raise OpsError(403, "aal2_required")
        return claims

    def handle(self, method: str, path: str, headers: dict, body: bytes):
        try:
            if method != "POST" or not path.startswith(PREFIX):
                raise OpsError(404, "not_found")
            if len(body) > MAX_BODY_BYTES:
                raise OpsError(413, "too_large")
            try:
                doc = json.loads(body or b"{}")
            except (ValueError, UnicodeDecodeError):
                raise OpsError(400, "invalid", "body") from None
            if not isinstance(doc, dict):
                raise OpsError(400, "invalid", "body")
            name = path[len(PREFIX):]
            if name == "session/staging":
                return self._staging(doc.get("code"))
            if name not in COMMANDS:
                raise OpsError(404, "unknown_command")
            claims = self._claims(headers)
            args = clean_args(name, doc.get("args") or {})
            data = self.store.run(claims, name, args)
            log.info("ops %s by operator", name)
            return _json(200, {"ok": True, "data": data})
        except OpsError as exc:
            payload = {"ok": False, "error": exc.code}
            if exc.field:
                payload["field"] = exc.field
            return _json(exc.status, payload)
        except Exception as exc:                   # noqa: BLE001 - never the exception's text to the caller
            log.error("ops command failed: %s", type(exc).__name__)
            return _json(500, {"ok": False, "error": "server"})

    def _staging(self, code):
        if not self.staging_code or not self.staging_operator:
            raise OpsError(404, "not_found")
        if not isinstance(code, str) or not hmac.compare_digest(code.encode(), self.staging_code.encode()):
            log.info("ops staging login refused")
            raise OpsError(403, "refused")
        token = issue_staging_token(self.staging_operator, self.secret, now=self.now(), aal="aal2")
        log.info("ops staging login as operator")
        return _json(200, {"ok": True, "access_token": token})
