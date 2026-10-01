"""The worker loop (pilot condition P1): the inbound path from a routed webhook event to a reply or an escalation.

One task at a time, under the lease the database hands out (claim_task -> bind_task), in this order:
  1. read the task's event (webhook_route put it in the tenant; the lease decides which tenant this worker sees);
  2. a delivery receipt reconciles the outbox row that carries its provider id, and nothing else;
  3. a message is recorded as an inquiry, then routed: a complaint or an owner inquiry is ESCALATED to the owner
     (notify.owner, no reply is ever drafted for it: C9.1); a question with an owner-approved answer becomes a
     reply PROPOSAL (reply:send, decided by the owner inside the WhatsApp window: A13); anything else escalates;
  4. while the proposal is pending the task re-queues itself; once the owner approves, the effect goes through
     enqueue_outbox (consumes the approval once, freezes the payload) and the dispatcher (one send per claim);
     a rejected or expired proposal closes the task.
The reply text is an owner-approved fact (canned answer, §8.3): no model call on this path. A failure inside a
task is not caught into a status: the lease expires, recovery re-queues it, and the third expiry dead-letters it.
"""
from __future__ import annotations
import logging, os, re, sys, time, uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from complaints import Matcher, normalize  # noqa: E402
from content_guard import ContentGuard  # noqa: E402

from service.dispatcher import SEND_TIMEOUT_SECONDS, SENT, BeforeSend, Dispatcher, WhatsAppCloudAdapter  # noqa: E402

log = logging.getLogger("hermes.worker")
AGENT = "agent_triage"
KINDS = ("inbound.event", "outbox.resend")      # the task kinds this worker handles; claim_task leaves any other kind to a
                                                # worker that knows it (0016: an older instance took a new kind mid-deploy)
REPLY_WINDOW_HOURS = 19          # a reply:send proposal must be decidable inside the 20 h window (approvals_reply_window)

# Questions answered only from owner-approved facts (kb_facts.topic). Keywords are normalised like complaints.
CANNED_TOPICS = {
    "hours": ("متى", "ساعات", "دوام", "الدوام", "تفتحون", "تفتح", "مفتوح", "تقفلون", "تسكرون"),
    "location": ("وين", "موقع", "موقعكم", "عنوان", "العنوان", "عنوانكم", "مكانكم"),
    "payment": ("الدفع", "طريقة", "تحويل", "كاش"),
}


def decide(text: str, route: dict, facts: dict, guard: ContentGuard, norm_rules: dict) -> dict:
    """Pure decision for one message. facts: topic -> owner-approved, still valid fact."""
    if route["action"] == "escalate":
        return {"action": "escalate", "reason": route["kind"], "categories": route.get("categories", []),
                "sla_minutes": route.get("sla_minutes")}
    words = set(normalize(text, norm_rules).split())
    for topic, keys in CANNED_TOPICS.items():
        if words & {normalize(k, norm_rules) for k in keys} and topic in facts:
            body = facts[topic]
            verdict = guard.verdict(body, "reply")
            if verdict != "pass":
                return {"action": "escalate", "reason": f"guard_{verdict}", "categories": [], "sla_minutes": None}
            return {"action": "propose", "topic": topic, "body": body}
    return {"action": "escalate", "reason": "no_approved_answer", "categories": [], "sla_minutes": None}


@dataclass
class Task:
    id: str
    token: str
    fencing: int
    customer_id: str

    @property
    def bind(self):
        return (self.id, self.token)


class SimulatedGraph:
    """Staging stand-in for the Graph API POST: records the request, answers like the Cloud API. The live client is
    live_adapters() below (HERMES_GRAPH=live, spec 28.14); it needs a Meta app and business verification (P3)."""

    def __init__(self, delay: float = 0.0):
        self.sent, self.delay = [], delay

    def __call__(self, url, body, headers, timeout):
        wamid = "wamid.SIM." + uuid.uuid4().hex
        self.sent.append({"url": url, "to": body.get("to"), "id": wamid})
        accepted(wamid, self.delay, timeout=timeout)
        return 200, {"messages": [{"id": wamid}]}


class SimulatedOwnerNotice:
    def __init__(self, delay: float = 0.0):
        self.sent, self.delay = [], delay

    def send(self, row):
        ref = "notice.SIM." + uuid.uuid4().hex
        self.sent.append({"customer_id": row["customer_id"], "payload": row["payload"], "id": ref})
        accepted(ref, self.delay, row.get("id"), SEND_TIMEOUT_SECONDS)
        return ref


def accepted(ref: str, delay: float, outbox_id=None, timeout: float | None = None):
    """The simulated provider has the message from here on: logged BEFORE the reply is delayed, so a sender stopped
    during the delay leaves exactly the real ambiguity (delivered, sender never told), and the log counts deliveries.
    Like a real client, the sender stops waiting at its timeout: a reply later than that never reaches it."""
    log.info("sim-provider accepted %s outbox=%s", ref, outbox_id)
    if delay and timeout is not None and delay > timeout:
        time.sleep(timeout)
        raise TimeoutError(f"no reply within {timeout} s")
    if delay:
        time.sleep(delay)


class Worker:
    def __init__(self, db, name: str, adapters: dict, lease_seconds: int = 120, approval_poll_seconds: int = 60):
        from service.pg import OutboxPort
        self.db, self.name, self.adapters = db, name, adapters
        self.lease, self.poll = lease_seconds, approval_poll_seconds
        self.matcher, self.guard = Matcher(), ContentGuard()
        self._outbox_port = OutboxPort

    # ------------------------------------------------------------ lease plumbing
    def claim(self) -> Task | None:
        with self.db.tx() as cur:
            cur.execute("select task_id, token, fencing, customer_id from app.claim_task(%s, %s, %s, 3, %s)",
                        (AGENT, self.name, self.lease, list(KINDS)))
            r = cur.fetchone()
        return Task(str(r[0]), str(r[1]), r[2], str(r[3])) if r else None

    def _complete(self, t: Task, status: str, error: str | None = None) -> bool:
        with self.db.tx() as cur:
            cur.execute("select app.complete_task(%s, %s, %s, %s, %s)", (t.id, t.token, t.fencing, status, error))
            return cur.fetchone()[0]

    def _processed(self, t: Task, event_id: int):
        with self.db.tx(t.bind) as cur:
            cur.execute("update app.webhook_events set processed_at = now() where id = %s", (event_id,))

    def _dispatch(self, t: Task, outbox_id: int) -> str:
        port = self._outbox_port(self.db, t.bind)
        outcome = Dispatcher(port, self.adapters).run_once(outbox_id)
        if outcome == "not_claimed":             # a previous holder already finished it: report the recorded state
            with self.db.tx(t.bind) as cur:
                cur.execute("select dispatched_at is not null, needs_human_check from app.outbox where id = %s", (outbox_id,))
                sent, human = cur.fetchone()
            outcome = SENT if sent else "ambiguous" if human else "pending"
        return outcome

    # ------------------------------------------------------------ the loop
    def run_once(self) -> str | None:
        t = self.claim()
        if t is None:
            return None
        try:
            outcome = self.handle(t)
        except Exception as exc:                 # noqa: BLE001 - the lease decides what happens next
            log.error("task failed: %s", type(exc).__name__, extra={"task_id": t.id})
            return "error"
        log.info("task %s", outcome, extra={"task_id": t.id})
        return outcome

    def loop(self, idle_seconds: float = 2.0, stop=None):
        while stop is None or not stop.is_set():
            if self.run_once() is None:
                if stop is None:
                    time.sleep(idle_seconds)
                else:
                    stop.wait(idle_seconds)          # SIGTERM wakes the idle wait; a task in hand finishes first

    def handle(self, t: Task) -> str:
        with self.db.tx(t.bind) as cur:
            cur.execute("select idempotency_key from app.tasks where id = %s", (t.id,))
            key = cur.fetchone()[0]
        if key.startswith("resend:"):                     # an operator's resend (0015): 'resend:<outbox id>:<attempts>'
            return self._resend(t, int(key.split(":")[1]))
        with self.db.tx(t.bind) as cur:
            _, kind, ext = key.split(":", 2)                                   # 'wh:<kind>:<external id>'
            cur.execute("select id, payload, channel_external_id from app.webhook_events where kind = %s and external_event_id = %s",
                        (kind, ext))
            event_id, payload, channel = cur.fetchone()
            cur.execute("select id, decision, payload, expires_at <= now() from app.approvals"
                        " where proposal_action = 'reply:send' and target_id = %s", (ext,))
            proposal = cur.fetchone()
        if "status" in payload:
            return self._receipt(t, event_id, payload["status"])
        if proposal is not None:
            return self._after_proposal(t, event_id, ext, proposal)
        return self._message(t, event_id, ext, channel, payload.get("message") or {})

    def _resend(self, t: Task, outbox_id: int) -> str:
        """The same dispatcher and claim as any send: the database refuses a row that is not pending again, and a
        second ambiguous result goes back to the operator."""
        outcome = self._dispatch(t, outbox_id)
        self._complete(t, "succeeded" if outcome == SENT else "escalated", None if outcome == SENT else f"resend:{outcome}"[:60])
        return f"resend {outcome}"

    def _receipt(self, t: Task, event_id: int, status: dict) -> str:
        with self.db.tx(t.bind) as cur:
            cur.execute("select id, dispatched_at is not null from app.outbox where provider_message_id = %s", (str(status.get("id")),))
            row = cur.fetchone()
        if row and not row[1]:
            self._outbox_port(self.db, t.bind).reconcile(row[0], str(status["id"]))
        self._processed(t, event_id)
        self._complete(t, "succeeded")
        return "receipt"

    def _message(self, t: Task, event_id: int, ext: str, channel: str, msg: dict) -> str:
        text = ((msg.get("text") or {}).get("body") or "").strip()
        route = self.matcher.route(text, "patron")
        with self.db.tx(t.bind) as cur:
            cur.execute("select topic, fact from app.kb_facts where approved_by_owner"
                        " and (valid_until is null or valid_until >= current_date)")
            facts = dict(cur.fetchall())
            cats = route.get("categories") or []
            cur.execute("insert into app.inquiries (customer_id, source, body, matched_category, owner_inquiry, routed_to)"
                        " values (%s, 'whatsapp', %s, %s, %s, %s)",
                        (t.customer_id, text, cats[0] if cats else None, route["kind"] == "owner_inquiry", route.get("routes", [])))
        d = decide(text, route, facts, self.guard, self.matcher.rules)
        if d["action"] == "propose":
            reply = {"phone_number_id": channel, "to": str(msg.get("from", "")), "body": d["body"], "in_reply_to": ext}
            with self.db.tx(t.bind) as cur:
                cur.execute("insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)"
                            " values (%s, 'customer', 'reply:send', %s, 'agent_replies', %s, now() + make_interval(hours => %s))",
                            (t.customer_id, _json(reply), ext, REPLY_WINDOW_HOURS))
                cur.execute("select app.requeue_task(%s, %s, %s)", (t.id, t.token, self.poll))
            return "proposed"
        notice = {"event": ext, "reason": d["reason"], "categories": d["categories"], "sla_minutes": d["sla_minutes"]}
        with self.db.tx(t.bind) as cur:                  # a reference, never the message text: the owner reads it in the portal
            cur.execute("select app.enqueue_outbox('notify.owner', %s, null, %s)", (_json(notice), ext))
            outbox_id = cur.fetchone()[0]
        self._dispatch(t, outbox_id)
        self._processed(t, event_id)
        self._complete(t, "escalated", d["reason"][:60])
        return "escalated"

    def _after_proposal(self, t: Task, event_id: int, ext: str, proposal) -> str:
        approval_id, decision, payload, expired = proposal
        if decision == "pending" and not expired:
            with self.db.tx() as cur:
                cur.execute("select app.requeue_task(%s, %s, %s)", (t.id, t.token, self.poll))
            return "awaiting_approval"
        if decision != "approved":
            self._processed(t, event_id)
            self._complete(t, "cancelled", "PROPOSAL_" + ("EXPIRED" if expired else str(decision).upper()))
            return "closed"
        with self.db.tx(t.bind) as cur:
            cur.execute("select app.enqueue_outbox('reply.send', %s, %s, %s)", (_json(payload), approval_id, ext))
            outbox_id = cur.fetchone()[0]
        outcome = self._dispatch(t, outbox_id)
        self._processed(t, event_id)
        self._complete(t, "succeeded" if outcome == SENT else "escalated", None if outcome == SENT else "SEND_" + outcome.upper())
        return "sent" if outcome == SENT else outcome


def _json(v):
    from psycopg.types.json import Jsonb
    return Jsonb(v)


def simulated_adapters(delay: float | None = None):
    """HERMES_SIM_SEND_DELAY_SECONDS delays the provider's reply (staging experiments on interrupted sends; 0 by
    default). Only with HERMES_GRAPH=simulate: there is no real provider to slow down, and none may be."""
    if delay is None:
        delay = float(os.environ.get("HERMES_SIM_SEND_DELAY_SECONDS") or 0)
    if delay and os.environ.get("HERMES_GRAPH") != "simulate":
        raise RuntimeError("HERMES_SIM_SEND_DELAY_SECONDS needs HERMES_GRAPH=simulate")
    graph, notice = SimulatedGraph(delay), SimulatedOwnerNotice(delay)
    return {"reply.send": WhatsAppCloudAdapter(graph, lambda customer_id: "staging-simulated-token"), "notify.owner": notice}


GRAPH_HOST = "graph.facebook.com"
GRAPH_PATH = re.compile(r"^https://graph\.facebook\.com/v\d{1,2}\.\d/\d{5,20}/messages$")


def graph_post(api):
    """The WhatsAppCloudAdapter's post() over crawler.ApiClient: refusals before any byte left (an address or
    a URL refused here, a header refused, a connection or TLS that failed) are BeforeSend and may be retried. A
    failure after the request was written propagates and is AMBIGUOUS: an operator decides, nothing resends it."""
    from service.crawler import NotSent             # the one network path (spec 28.12); FetchRefused comes with it
    from safe_fetch import FetchRefused

    def post(url, body, headers, timeout):
        if not GRAPH_PATH.match(url):
            raise BeforeSend("BAD_TARGET")           # live: a real phone number id, digits only
        try:
            return api.request("POST", url, body, headers)
        except FetchRefused as exc:
            raise BeforeSend(exc.code) from None
        except NotSent as exc:
            raise BeforeSend(str(exc)) from None
        except ValueError:
            raise BeforeSend("BAD_HEADER") from None
    return post


class PortalNotice:
    """notify.owner in live mode: the owner reads escalations in the portal; no WhatsApp message to the owner until
    an approved owner-notice template and the owner's number exist (spec 28.14). The outbox row records it once."""

    def send(self, row):
        return f"portal.{row.get('id')}"


def live_adapters(env):
    """HERMES_GRAPH=live: reply.send goes to the Graph API with the system user token HERMES_GRAPH_TOKEN, to
    graph.facebook.com only (crawler.ApiClient). Missing configuration refuses to start."""
    token = env.get("HERMES_GRAPH_TOKEN", "")
    version = env.get("HERMES_GRAPH_API_VERSION", "v21.0")
    if not token or len(token) > 1024 or not token.isascii() or not token.isprintable() or " " in token:
        raise RuntimeError("HERMES_GRAPH=live needs HERMES_GRAPH_TOKEN (a Meta system user token)")
    if env.get("HERMES_SIM_SEND_DELAY_SECONDS"):
        raise RuntimeError("HERMES_SIM_SEND_DELAY_SECONDS needs HERMES_GRAPH=simulate")
    from service.crawler import ApiClient
    post = graph_post(ApiClient({GRAPH_HOST}))
    return {"reply.send": WhatsAppCloudAdapter(post, lambda customer_id: token, version), "notify.owner": PortalNotice()}


def adapters_for(env):
    """simulate (staging) or live; anything else refuses to start."""
    mode = env.get("HERMES_GRAPH")
    if mode == "simulate":
        return simulated_adapters()
    if mode == "live":
        return live_adapters(env)
    raise RuntimeError("HERMES_GRAPH must be 'simulate' or 'live'")


def worker_name() -> str:
    return f"{os.environ.get('RAILWAY_REPLICA_ID') or os.uname().nodename}-{os.getpid()}"
