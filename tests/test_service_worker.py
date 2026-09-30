"""Worker decision (P1): complaints and owner inquiries escalate, replies only from owner-approved facts that pass
the content guard. The database path of the same loop runs in db/tests/e2e_pilot.py (CI and staging)."""
import os, sys, unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from complaints import Matcher  # noqa: E402
from content_guard import ContentGuard  # noqa: E402
from service.dispatcher import WhatsAppCloudAdapter  # noqa: E402
from service import worker  # noqa: E402
from service.worker import SimulatedGraph, decide, simulated_adapters  # noqa: E402

M, G = Matcher(), ContentGuard()
FACTS = {"hours": "نفتح يوميًا من ٩ صباحًا إلى ١١ مساءً"}


def d(text, facts=FACTS):
    return decide(text, M.route(text, "patron"), facts, G, M.rules)


class TestWorkerDecision(unittest.TestCase):
    def test_question_with_an_approved_answer_is_proposed_verbatim(self):
        r = d("متى تفتحون اليوم؟")
        self.assertEqual((r["action"], r["topic"], r["body"]), ("propose", "hours", FACTS["hours"]))

    def test_a_complaint_is_escalated_even_when_it_asks_an_answerable_question(self):
        r = d("متى ترجعون الفلوس؟ الفاتورة غلط")
        self.assertEqual((r["action"], r["reason"]), ("escalate", "complaint"))
        self.assertIn("pricing", r["categories"])

    def test_a_price_question_goes_to_the_owner(self):
        self.assertEqual(d("بكم الوجبة")["reason"], "owner_inquiry")

    def test_no_approved_fact_means_no_reply(self):
        self.assertEqual(d("وين موقعكم")["reason"], "no_approved_answer")
        self.assertEqual(d("متى تفتحون", facts={})["reason"], "no_approved_answer")

    def test_an_approved_fact_that_fails_the_guard_is_not_proposed(self):
        r = d("متى تفتحون", facts={"hours": "مفتوحون، والوجبة ب 5000 ريال"})
        self.assertEqual((r["action"], r["reason"]), ("escalate", "guard_block"))

    def test_the_whatsapp_adapter_sends_through_the_simulated_graph(self):
        g = SimulatedGraph()
        ref = WhatsAppCloudAdapter(g, lambda c: "tok").send(
            {"customer_id": "c", "payload": {"phone_number_id": "pn", "to": "967700000001", "body": "x"}})
        self.assertTrue(ref.startswith("wamid.SIM."))
        self.assertEqual((g.sent[0]["to"], g.sent[0]["id"]), ("967700000001", ref))
        self.assertIn("/pn/messages", g.sent[0]["url"])


class TestSimulatedSendDelay(unittest.TestCase):
    """Staging knob for interrupted-send experiments: the provider accepts first, the reply comes late."""

    def test_the_provider_accepts_before_the_delay(self):
        order = []
        with mock.patch.object(worker.log, "info", side_effect=lambda *a: order.append("accepted")), \
             mock.patch.object(worker.time, "sleep", side_effect=lambda s: order.append(f"sleep {s}")):
            ref = worker.SimulatedOwnerNotice(delay=60).send({"id": 7, "customer_id": "c", "payload": {}})
        self.assertEqual(order, ["accepted", "sleep 60"])
        self.assertTrue(ref.startswith("notice.SIM."))

    def test_no_delay_by_default_and_none_outside_simulation(self):
        with mock.patch.dict(os.environ, {"HERMES_GRAPH": "simulate", "HERMES_SIM_SEND_DELAY_SECONDS": ""}):
            self.assertEqual(simulated_adapters()["notify.owner"].delay, 0)
        with mock.patch.dict(os.environ, {"HERMES_GRAPH": "simulate", "HERMES_SIM_SEND_DELAY_SECONDS": "5"}):
            self.assertEqual(simulated_adapters()["notify.owner"].delay, 5)
        with mock.patch.dict(os.environ, {"HERMES_GRAPH": "live", "HERMES_SIM_SEND_DELAY_SECONDS": "5"}):
            with self.assertRaises(RuntimeError):
                simulated_adapters()


if __name__ == "__main__":
    unittest.main()
