import re, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from service.dispatcher import (Dispatcher, BeforeSend, Rejected, WhatsAppCloudAdapter, classify,
                                SENT, FAILED_PERMANENT, FAILED_BEFORE_SEND, AMBIGUOUS)
from service.outbox_model import OutboxModel, DbError


class Counting:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, 0

    def send(self, row):
        self.calls += 1
        b = self.behaviour(self.calls)
        if isinstance(b, BaseException):
            raise b
        return b


class TestDispatcher(unittest.TestCase):
    def setUp(self):
        self.db = OutboxModel()
        self.id = self.db.add("reply.send", {"body": "مرحبا"})

    def run_with(self, behaviour, topic="reply.send"):
        a = Counting(behaviour)
        return Dispatcher(self.db, {topic: a}).run_once(self.id), a

    def test_success_records_the_provider_id_once(self):
        out, a = self.run_with(lambda n: "wamid.X")
        self.assertEqual((out, a.calls, self.db.rows[self.id].provider_message_id), (SENT, 1, "wamid.X"))
        self.assertEqual(Dispatcher(self.db, {"reply.send": a}).run_once(self.id), "not_claimed")
        self.assertEqual(a.calls, 1)

    def test_timeout_after_writing_is_ambiguous_and_never_retried_automatically(self):
        out, a = self.run_with(lambda n: TimeoutError("read timed out"))
        self.assertEqual(out, AMBIGUOUS)
        self.assertTrue(self.db.rows[self.id].needs_human_check)
        for _ in range(3):
            self.db.now += 10_000
            self.assertEqual(Dispatcher(self.db, {"reply.send": a}).run_once(self.id), "not_claimed")
        self.assertEqual(a.calls, 1)

    def test_unknown_exception_and_5xx_are_ambiguous(self):
        for exc in (RuntimeError("?"), Rejected(500), Rejected(503), Rejected(408), ConnectionResetError()):
            self.assertEqual(classify(exc).outcome, AMBIGUOUS, exc)

    def test_before_send_releases_for_a_later_attempt(self):
        out, a = self.run_with(lambda n: BeforeSend("connect refused") if n == 1 else "wamid.Y")
        self.assertEqual(out, FAILED_BEFORE_SEND)
        self.assertIsNone(self.db.rows[self.id].sending_until)
        out2 = Dispatcher(self.db, {"reply.send": a}).run_once(self.id)
        self.assertEqual((out2, a.calls), (SENT, 2))

    def test_provider_refusal_is_final_and_explained(self):
        out, _ = self.run_with(lambda n: Rejected(400, "131026"))
        r = self.db.rows[self.id]
        self.assertEqual((out, r.last_error), (FAILED_PERMANENT, "HTTP_400:131026"))
        with self.assertRaises(DbError):
            self.db._update(r, dispatched_at=self.db.now)

    def test_crash_mid_send_leaves_an_expired_claim_that_is_never_reclaimed(self):
        tok = self.db.claim(self.id)                     # dispatcher claims, then the process dies
        self.assertIsNotNone(tok)
        self.db.now += 121
        a = Counting(lambda n: "wamid.Z")
        self.assertEqual(Dispatcher(self.db, {"reply.send": a}).run_once(self.id), "not_claimed")
        self.assertEqual((a.calls, self.db.rows[self.id].last_error), (0, "AMBIGUOUS_LEASE_EXPIRED"))

    def test_expired_claim_of_an_idempotent_topic_is_reclaimed(self):
        i = self.db.add("site.deploy_prod", {"content_hash": "h"})
        self.db.claim(i)
        self.db.now += 121
        a = Counting(lambda n: "deploy-1")
        self.assertEqual(Dispatcher(self.db, {"site.deploy_prod": a}).run_once(i), SENT)

    def test_late_success_after_expiry_is_kept_only_with_provider_evidence(self):
        def slow(n):
            self.db.now += 500                          # the send outlives the claim
            return "wamid.LATE"
        out, _ = self.run_with(slow)
        self.assertEqual((out, self.db.rows[self.id].provider_message_id), (SENT, "wamid.LATE"))
        j = self.db.add("reply.send", {"body": "x"})

        def slow_no_id(n):
            self.db.now += 500
            return None
        out2 = Dispatcher(self.db, {"reply.send": Counting(slow_no_id)}).run_once(j)
        self.assertEqual(out2, AMBIGUOUS)
        self.assertTrue(self.db.rows[j].needs_human_check)

    def test_only_an_operator_with_a_reason_clears_the_human_check(self):
        self.run_with(lambda n: TimeoutError())
        with self.assertRaises(DbError) as e:
            self.db._update(self.db.rows[self.id], needs_human_check=False)
        self.assertEqual(e.exception.code, "OUTBOX_NEEDS_HUMAN")
        with self.assertRaises(DbError):
            self.db.resolve(self.id, "resend", "ok", operator=True)          # reason too short
        self.db.resolve(self.id, "resend", "customer says nothing arrived", operator=True)
        out, _ = self.run_with(lambda n: "wamid.R")
        self.assertEqual(out, SENT)

    def test_attempts_are_bounded(self):
        a = Counting(lambda n: BeforeSend("dns"))
        for _ in range(5):
            Dispatcher(self.db, {"reply.send": a}).run_once(self.id)
        self.assertEqual(Dispatcher(self.db, {"reply.send": a}).run_once(self.id), "not_claimed")
        self.assertEqual((a.calls, self.db.rows[self.id].last_error), (5, "MAX_ATTEMPTS"))

    def test_whatsapp_adapter_maps_responses(self):
        ok = WhatsAppCloudAdapter(lambda u, j, h, t: (200, {"messages": [{"id": "wamid.A"}]}), lambda c: "tok")
        row = {"customer_id": "c1", "payload": {"phone_number_id": "pn", "to": "9677", "body": "x"}}
        self.assertEqual(ok.send(row), "wamid.A")
        bad = WhatsAppCloudAdapter(lambda u, j, h, t: (400, {"error": {"code": 131047}}), lambda c: "tok")
        self.assertEqual(classify(self._raises(bad, row)).outcome, FAILED_PERMANENT)
        odd = WhatsAppCloudAdapter(lambda u, j, h, t: (200, {}), lambda c: "tok")
        self.assertEqual(classify(self._raises(odd, row)).outcome, AMBIGUOUS)

    @staticmethod
    def _raises(adapter, row):
        try:
            adapter.send(row)
        except Exception as e:
            return e
        raise AssertionError("no exception")

    def test_model_error_codes_exist_in_the_migration(self):
        sql = (ROOT / "db/migrations/0010_v18_closure.sql").read_text()
        src = (ROOT / "service/outbox_model.py").read_text()
        for code in set(re.findall(r'DbError\("([A-Z_0-9]+)"\)', src)):
            self.assertIn(f"'{code}'", sql, code)


class TestSendBoundsInsideLeases(unittest.TestCase):
    """A send that outlives its leases returns the provider's id to a worker the database no longer lets write
    (fencing), and the id is lost (staging rehearsal, spec 28.7). The chain that prevents it."""

    def test_the_send_timeout_sits_inside_every_lease_and_shutdown_window(self):
        import inspect, re
        from service import dispatcher
        from service.worker import Worker
        root = Path(__file__).resolve().parent.parent
        shutdown = int(re.search(r"^SHUTDOWN_WAIT_SECONDS = (\d+)", (root / "service/app.py").read_text(encoding="utf-8"), re.M).group(1))
        task_lease = inspect.signature(Worker.__init__).parameters["lease_seconds"].default
        railway_drain = 30                                    # drainingSeconds on hermes-app (spec 28.7)
        self.assertLess(dispatcher.SEND_TIMEOUT_SECONDS, shutdown)
        self.assertLess(shutdown, railway_drain)
        self.assertLess(2 * dispatcher.SEND_TIMEOUT_SECONDS, dispatcher.DISPATCH_LEASE_SECONDS)
        self.assertLess(2 * dispatcher.SEND_TIMEOUT_SECONDS, task_lease)
        self.assertIn("claim_outbox_dispatch(%s, %s)", (root / "service/pg.py").read_text(encoding="utf-8"))

    def test_the_whatsapp_adapter_passes_the_bound(self):
        seen = {}
        def post(url, body, headers, timeout):
            seen["timeout"] = timeout
            return 200, {"messages": [{"id": "wamid.X"}]}
        from service.dispatcher import SEND_TIMEOUT_SECONDS, WhatsAppCloudAdapter
        WhatsAppCloudAdapter(post, lambda c: "t").send({"customer_id": "c", "payload": {"phone_number_id": "p", "to": "1", "body": "b"}})
        self.assertEqual(seen["timeout"], SEND_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
