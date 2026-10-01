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
import json, logging, os, re, sys, time, uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from complaints import Matcher, normalize  # noqa: E402
from content_guard import ContentGuard  # noqa: E402

from service.dispatcher import (SEND_TIMEOUT_SECONDS, SENT, BeforeSend, Dispatcher, MetaMessagingAdapter,  # noqa: E402
                                MetaPublishAdapter, PublishRouter, ReplyRouter, TikTokPublishAdapter, WhatsAppCloudAdapter)

log = logging.getLogger("hermes.worker")
AGENT = "agent_triage"
KINDS = ("inbound.event", "outbox.resend", "content.propose")      # the task kinds this worker handles; claim_task leaves any other kind to a
                                                # worker that knows it (0016: an older instance took a new kind mid-deploy)
POST_DECISION_DAYS = 7          # a post proposal waits a week for the owner, then lapses
REPLY_WINDOW_HOURS = 19          # a reply:send proposal must be decidable inside the 20 h window (approvals_reply_window)

# Questions answered only from owner-approved facts (kb_facts.topic). Keywords are normalised like complaints.
CANNED_TOPICS = {
    "hours": ("متى", "ساعات", "دوام", "الدوام", "تفتحون", "تفتح", "مفتوح", "تقفلون", "تسكرون"),
    "location": ("وين", "موقع", "موقعكم", "عنوان", "العنوان", "عنوانكم", "مكانكم"),
    "payment": ("الدفع", "طريقة", "تحويل", "كاش"),
}


MEDIA_TYPES = ("audio", "image", "video", "document", "sticker", "location", "contacts")


def message_content(msg: dict) -> tuple[str, str]:
    """(type, text to route on) for a WhatsApp Cloud message (0019). Buttons and list replies carry their title; a
    photo, video or document its caption; a voice note, sticker, location or contact card no text at all."""
    kind = str(msg.get("type") or "text")
    if kind == "text":
        return "text", ((msg.get("text") or {}).get("body") or "").strip()
    if kind == "button":
        return "button", str((msg.get("button") or {}).get("text") or "").strip()
    if kind == "interactive":
        i = msg.get("interactive") or {}
        reply = i.get("button_reply") or i.get("list_reply") or {}
        return "interactive", str(reply.get("title") or "").strip()
    if kind == "reaction":
        return "reaction", ""
    if kind in MEDIA_TYPES:
        return kind, str((msg.get(kind) or {}).get("caption") or "").strip() if kind in ("image", "video", "document") else ""
    return "unsupported", ""


ATTACHMENT_TYPES = {"image": "image", "audio": "audio", "video": "video", "file": "document", "location": "location",
                    "ig_reel": "video", "reel": "video"}
SOURCE = {"whatsapp_cloud": "whatsapp", "facebook_page": "facebook", "instagram_business": "instagram"}


def messaging_content(m: dict) -> tuple[str, str, str]:
    """(type, text, sender id) for a Messenger or Instagram Direct event: text, a quick reply's text, or an
    attachment (its type; a caption-like text when the customer wrote one); a sticker or a reaction has no text."""
    sender = str((m.get("sender") or {}).get("id") or "")
    msg = m.get("message")
    if not isinstance(msg, dict):
        return ("reaction" if m.get("reaction") else "unsupported"), "", sender
    text = str(msg.get("text") or "").strip()
    if msg.get("sticker_id"):
        return "sticker", "", sender
    attachments = msg.get("attachments") or []
    if attachments:
        kind = ATTACHMENT_TYPES.get(str((attachments[0] or {}).get("type")), "unsupported")
        return kind, text, sender
    if msg.get("quick_reply") and text:
        return "interactive", text, sender
    return ("text", text, sender) if text else ("unsupported", "", sender)


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
        if url.startswith("https://open.tiktokapis.com/"):                          # TikTok photo post (0024)
            ref = "SIM_tt_" + uuid.uuid4().hex
            self.sent.append({"url": url, "to": None, "id": ref})
            accepted(ref, self.delay, timeout=timeout)
            return 200, {"data": {"publish_id": ref}, "error": {"code": "ok", "message": ""}}
        if url.rsplit("/", 1)[-1] in ("feed", "photos", "media", "media_publish"):   # publishing (0023)
            ref = "SIM_post_" + uuid.uuid4().hex
            self.sent.append({"url": url, "to": None, "id": ref})
            if url.endswith("/media"):
                return 200, {"id": "SIM_container_" + uuid.uuid4().hex}
            accepted(ref, self.delay, timeout=timeout)
            return 200, {"id": ref, **({"post_id": ref} if url.endswith("/photos") else {})}
        if "recipient" in body:                         # Messenger / Instagram Direct answer with a message_id
            mid = "m_SIM." + uuid.uuid4().hex
            self.sent.append({"url": url, "to": body["recipient"].get("id"), "id": mid})
            accepted(mid, self.delay, timeout=timeout)
            return 200, {"recipient_id": body["recipient"].get("id"), "message_id": mid}
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
        if key.startswith("content:"):                    # the owner's draft post (0023): 'content:<content id>'
            return self._content(t, key[len("content:"):])
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
        if "form" in payload:                             # the contact form of the customer's site (28.24)
            return self._form(t, event_id, ext, payload["form"])
        if proposal is not None:
            return self._after_proposal(t, event_id, ext, proposal)
        return self._message(t, event_id, ext, channel, payload.get("message") or {}, kind, payload.get("messaging"))

    def _content(self, t: Task, content_id: str) -> str:
        """A draft post: check the text, propose exactly what will be published (text, account, image), wait for the
        owner, then publish through the outbox and mark it published under that approval (0009 guard, 0023)."""
        with self.db.tx(t.bind) as cur:
            cur.execute("select c.status::text, c.body, app.content_payload(c) from app.content_items c where c.id = %s", (content_id,))
            row = cur.fetchone()
            cur.execute("select id, decision::text, expires_at <= now() from app.approvals where proposal_action = 'content:publish'"
                        " and target_id = %s order by requested_at desc limit 1", (content_id,))
            proposal = cur.fetchone()
        if row is None or row[0] in ("published", "rejected"):
            self._complete(t, "succeeded" if row and row[0] == "published" else "cancelled", None if row and row[0] == "published"
                           else "CONTENT_" + ("MISSING" if row is None else "REJECTED"))
            return "closed"
        status, body, payload = row
        if proposal is None:
            verdict = self.guard.verdict(body, "post")
            if verdict == "block":                      # a health claim, a disparagement, a masked link: never proposed
                with self.db.tx(t.bind) as cur:
                    cur.execute("update app.content_items set status = 'rejected' where id = %s", (content_id,))
                self._complete(t, "escalated", "guard_block")
                return "blocked"
            with self.db.tx(t.bind) as cur:
                cur.execute("insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)"
                            " values (%s, 'customer', 'content:publish', %s, 'agent_content', %s, now() + make_interval(days => %s))",
                            (t.customer_id, _json(payload), content_id, POST_DECISION_DAYS))
                cur.execute("update app.content_items set status = 'pending_approval' where id = %s", (content_id,))
                cur.execute("select app.requeue_task(%s, %s, %s)", (t.id, t.token, self.poll))
            return "proposed"
        approval_id, decision, expired = proposal
        if decision == "pending" and not expired:
            with self.db.tx() as cur:
                cur.execute("select app.requeue_task(%s, %s, %s)", (t.id, t.token, self.poll))
            return "awaiting_approval"
        if decision != "approved":
            with self.db.tx(t.bind) as cur:
                cur.execute("update app.content_items set status = 'rejected' where id = %s", (content_id,))
            self._complete(t, "cancelled", "PROPOSAL_" + ("EXPIRED" if expired else str(decision).upper()))
            return "closed"
        with self.db.tx(t.bind) as cur:
            cur.execute("select app.enqueue_outbox('content.publish', %s, %s, %s)", (_json(payload), approval_id, content_id))
            outbox_id = cur.fetchone()[0]
        outcome = self._dispatch(t, outbox_id)
        if outcome == SENT:
            with self.db.tx(t.bind) as cur:             # the 0009 guard checks this approval covers exactly this content
                cur.execute("update app.content_items set status = 'published', approval_id = %s where id = %s",
                            (approval_id, content_id))
        self._complete(t, "succeeded" if outcome == SENT else "escalated", None if outcome == SENT else "SEND_" + outcome.upper())
        return "published" if outcome == SENT else outcome

    def _form(self, t: Task, event_id: int, ext: str, form: dict) -> str:
        """A site visitor's message: recorded (with the name and number the visitor gave, purged after 30 days like
        every inquiry body) and always escalated to the owner, who answers the visitor; WhatsApp allows no business-
        initiated message without an approved template, so nothing is sent to the visitor automatically."""
        text = str(form.get("message") or "").strip()
        contact = " · ".join(x for x in (str(form.get("name") or "").strip(), str(form.get("phone") or "").strip()) if x)
        route = self.matcher.route(text, "patron")
        cats = route.get("categories") or []
        with self.db.tx(t.bind) as cur:
            cur.execute("insert into app.inquiries (customer_id, source, body, matched_category, owner_inquiry, routed_to, event_ref,"
                        " message_type) values (%s, 'site_form', %s, %s, %s, %s, %s, 'text')"
                        " on conflict (customer_id, event_ref) where event_ref is not null do nothing",
                        (t.customer_id, text + (f"\n— {contact}" if contact else ""), cats[0] if cats else None,
                         route["kind"] == "owner_inquiry", route.get("routes", []), ext[:200]))
        notice = {"event": ext, "reason": route["kind"] if route["action"] == "escalate" else "site_form", "categories": cats,
                  "sla_minutes": route.get("sla_minutes")}
        with self.db.tx(t.bind) as cur:
            cur.execute("select app.enqueue_outbox('notify.owner', %s, null, %s)", (_json(notice), ext))
            outbox_id = cur.fetchone()[0]
        self._dispatch(t, outbox_id)
        self._processed(t, event_id)
        self._complete(t, "escalated", notice["reason"][:60])
        return "escalated"

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

    def _message(self, t: Task, event_id: int, ext: str, channel: str, msg: dict, source_kind: str = "whatsapp_cloud",
                 messaging: dict | None = None) -> str:
        if source_kind in ("facebook_page", "instagram_business"):
            kind, text, sender = messaging_content(messaging or {})
        else:
            kind, text = message_content(msg)
            sender = str(msg.get("from", ""))
        if kind == "reaction":                          # an emoji on one of our messages: nothing to answer, nobody to page
            self._processed(t, event_id)
            self._complete(t, "succeeded")
            return "ignored"
        route = self.matcher.route(text, "patron")
        with self.db.tx(t.bind) as cur:
            cur.execute("select topic, fact from app.kb_facts where approved_by_owner"
                        " and (valid_until is null or valid_until >= current_date) order by updated_at, id")   # the latest wins
            facts = dict(cur.fetchall())
            cats = route.get("categories") or []
            cur.execute("insert into app.inquiries (customer_id, source, body, matched_category, owner_inquiry, routed_to, event_ref,"
                        " message_type) values (%s, %s, %s, %s, %s, %s, %s, %s)"
                        " on conflict (customer_id, event_ref) where event_ref is not null do nothing",   # a re-run task: once (0018)
                        (t.customer_id, SOURCE.get(source_kind, "other"), text or None, cats[0] if cats else None,
                         route["kind"] == "owner_inquiry",
                         route.get("routes", []), ext[:200] or None, kind))
        if not text and kind != "text":                 # a voice note, a sticker, a location: the owner opens it in WhatsApp
            d = {"action": "escalate", "reason": f"non_text:{kind}", "categories": [], "sla_minutes": None}
        else:
            d = decide(text, route, facts, self.guard, self.matcher.rules)
        if d["action"] == "propose":
            if source_kind == "whatsapp_cloud":
                reply = {"phone_number_id": channel, "to": sender, "body": d["body"], "in_reply_to": ext, "topic": d["topic"]}
            else:                                       # the same channel the customer wrote on (Messenger, Instagram Direct)
                reply = {"channel": source_kind, "account_id": channel, "to": sender, "body": d["body"], "in_reply_to": ext,
                         "topic": d["topic"]}
            with self.db.tx(t.bind) as cur:
                cur.execute("insert into app.approvals (customer_id, scope, proposal_action, payload, requested_by_agent, target_id, expires_at)"
                            " values (%s, 'customer', 'reply:send', %s, 'agent_replies', %s, now() + make_interval(hours => %s))"
                            " returning id", (t.customer_id, _json(reply), ext, REPLY_WINDOW_HOURS))
                approval_id = cur.fetchone()[0]
                cur.execute("select app.approve_by_standing(%s)", (approval_id,))   # the database decides, never the worker (0020)
                standing = cur.fetchone()[0]
                if not standing:
                    cur.execute("select app.requeue_task(%s, %s, %s)", (t.id, t.token, self.poll))
            if not standing:
                return "proposed"
            return self._after_proposal(t, event_id, ext, (approval_id, "approved", reply, False))
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
    return {"reply.send": ReplyRouter(WhatsAppCloudAdapter(graph, lambda customer_id: "staging-simulated-token"),
                                      MetaMessagingAdapter(graph, lambda account: "staging-simulated-account-token")),
            "content.publish": PublishRouter(MetaPublishAdapter(graph, lambda account: "staging-simulated-account-token"),
                                             TikTokPublishAdapter(graph, lambda account: "staging-simulated-tiktok-token")),
            "notify.owner": notice}


GRAPH_HOSTS = ("graph.facebook.com", "graph.instagram.com", "open.tiktokapis.com")
GRAPH_PATH = re.compile(r"^https://graph\.facebook\.com/v\d{1,2}\.\d/\d{5,25}/(messages|feed|photos)$"
                        r"|^https://graph\.instagram\.com/v\d{1,2}\.\d/\d{5,25}/(messages|media|media_publish)$"
                        r"|^https://open\.tiktokapis\.com/v2/post/publish/content/init/$")


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


def _token_ok(token: str) -> bool:
    return bool(token) and len(token) <= 1024 and token.isascii() and token.isprintable() and " " not in token


def account_tokens(raw: str, id_pattern: str = r"[0-9]{5,25}", name: str = "HERMES_GRAPH_ACCOUNT_TOKENS") -> dict:
    """HERMES_GRAPH_ACCOUNT_TOKENS: a JSON object {"<page id or Instagram account id>": "<its access token>"}; empty
    means Messenger and Instagram replies fail before sending (NO_ACCOUNT_TOKEN), WhatsApp is unaffected."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a JSON object of account id -> token") from None
    if not isinstance(data, dict) or not all(re.fullmatch(id_pattern, str(k)) and isinstance(v, str) and _token_ok(v)
                                             for k, v in data.items()):
        raise RuntimeError(f"{name} must be a JSON object of account id -> token")
    return {str(k): v for k, v in data.items()}


def live_adapters(env):
    """HERMES_GRAPH=live: reply.send goes to the Graph API on the customer's channel, through crawler.ApiClient to
    graph.facebook.com and graph.instagram.com only. WhatsApp uses the system user token HERMES_GRAPH_TOKEN;
    Messenger and Instagram Direct the account's own token (HERMES_GRAPH_ACCOUNT_TOKENS). Missing WhatsApp
    configuration refuses to start."""
    token = env.get("HERMES_GRAPH_TOKEN", "")
    version = env.get("HERMES_GRAPH_API_VERSION", "v21.0")
    if not _token_ok(token):
        raise RuntimeError("HERMES_GRAPH=live needs HERMES_GRAPH_TOKEN (a Meta system user token)")
    if env.get("HERMES_SIM_SEND_DELAY_SECONDS"):
        raise RuntimeError("HERMES_SIM_SEND_DELAY_SECONDS needs HERMES_GRAPH=simulate")
    accounts = account_tokens(env.get("HERMES_GRAPH_ACCOUNT_TOKENS", ""))
    tiktok = account_tokens(env.get("HERMES_TIKTOK_TOKENS", ""), r"[A-Za-z0-9_.-]{5,64}", "HERMES_TIKTOK_TOKENS")
    from service.crawler import ApiClient
    post = graph_post(ApiClient(set(GRAPH_HOSTS)))
    return {"reply.send": ReplyRouter(WhatsAppCloudAdapter(post, lambda customer_id: token, version),
                                      MetaMessagingAdapter(post, accounts.get, version)),
            "content.publish": PublishRouter(MetaPublishAdapter(post, accounts.get, version),
                                             TikTokPublishAdapter(post, tiktok.get, env.get("HERMES_TIKTOK_PRIVACY") or "SELF_ONLY")),
            "notify.owner": PortalNotice()}


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
