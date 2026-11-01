"""Model calls (service/model.py, spec 28.37): the budget is reserved and the circuit asked BEFORE the call, the
actual cost settled and the outcome recorded AFTER it, one agent_calls row each time and never the message text;
every refusal or failure leaves the keyword result; the live request is the Responses API with a strict schema."""
import json, sys, unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.model import (INSTRUCTIONS, SCHEMA, Answer, ModelError, OpenAIModel, SimulatedModel, Triage,  # noqa: E402
                           triage_from_env)
from service.worker import Task  # noqa: E402

TASK = Task("00000000-0000-0000-0000-0000000000a1", "tok", 1, "00000000-0000-0000-0000-0000000000c1")
SECRET_TEXT = "رقم بطاقتي 4111 والأكل وصل بارد"


class FakeDb:
    """Answers the enforcement functions as scripted; records every statement and the bind it ran under."""

    def __init__(self, reserve="OK", probe="CLOSED", exceeded=False):
        self.answers = {"reserve_budget": reserve, "claim_probe": probe, "settle_budget": exceeded,
                        "record_outcome": "closed", "record_reservation_exceeded": False}
        self.sql = []

    @contextmanager
    def tx(self, bind=None):
        db = self

        class Cur:
            last = None

            def execute(self, q, args=()):
                db.sql.append((bind, q, args))
                self.last = next((v for k, v in db.answers.items() if f"app.{k}(" in q), None)

            def fetchone(self):
                return (self.last,)
        yield Cur()

    def calls(self, name):
        return [a for _, q, a in self.sql if f"app.{name}(" in q]

    def rows(self):
        return [a for _, q, a in self.sql if "insert into app.agent_calls" in q]


class FakeModel:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.seen = answer, error, []

    def complete(self, model, instructions, text, schema, max_output_tokens):
        self.seen.append((model, text, max_output_tokens))
        if self.error:
            raise ModelError(self.error)
        return self.answer


class TriageTests(unittest.TestCase):
    def run_one(self, db, model, text=SECRET_TEXT):
        return Triage(db, model, clock=lambda: 1_793_500_000).classify(TASK, text)

    def test_a_clean_answer_is_reserved_first_settled_after_and_logged_without_the_text(self):
        db, model = FakeDb(), FakeModel(Answer({"complaint": True, "category": "quality", "confidence": 0.8}, 300, 20))
        self.assertEqual(self.run_one(db, model), (True, "quality", 0.8))
        names = [n for _, q, _ in db.sql for n in ("reserve_budget", "claim_probe", "settle_budget", "record_outcome",
                                                  "agent_calls") if n in q]
        self.assertEqual(names, ["reserve_budget", "claim_probe", "settle_budget", "record_outcome", "agent_calls"])
        self.assertTrue(all(bind == TASK.bind for bind, _, _ in db.sql))           # every statement in the lease
        agent, month, task, bound, per_call, per_task, agent_cap, customer_cap = db.calls("reserve_budget")[0]
        self.assertEqual((agent, month, task, per_call, per_task, agent_cap, customer_cap),
                         ("agent_triage", "2026-11", TASK.id, 0.001, 0.001, 0.02, 1.13))
        self.assertLessEqual(bound, per_call)
        settle = db.calls("settle_budget")[0]
        self.assertEqual(settle[3:5], (bound, 0.00009))       # 300 x 0.20 + 20 x 1.25 per million = 0.000085, rounded up
        self.assertEqual(db.calls("record_outcome")[0][2:4], (True, False))
        row = db.rows()[0]
        self.assertEqual(row[2:7], ("agent_triage", "queue.classify", "gpt-5.4-nano", 300, 20))
        self.assertEqual(row[9:11], ("success", None))
        self.assertNotIn("4111", json.dumps(db.sql, default=str, ensure_ascii=False))   # the text never reaches a row

    def test_a_refused_budget_or_a_paused_agent_calls_nothing(self):
        for verdict, outcome in (("BUDGET_EXCEEDED_AGENT", "budget_exceeded"), ("AGENT_PAUSED", "rejected")):
            db, model = FakeDb(reserve=verdict), FakeModel(Answer({}, 1, 1))
            self.assertEqual(self.run_one(db, model), (False, None, 0.0))
            self.assertEqual(model.seen, [])
            self.assertEqual(db.calls("claim_probe"), [])
            self.assertEqual(db.rows()[0][9:11], (outcome, verdict))

    def test_an_open_circuit_calls_nothing_and_releases_the_reservation(self):
        db, model = FakeDb(probe="OPEN"), FakeModel(Answer({}, 1, 1))
        self.assertEqual(self.run_one(db, model), (False, None, 0.0))
        self.assertEqual(model.seen, [])
        self.assertEqual(db.calls("settle_budget")[0][4], 0)
        self.assertEqual(db.rows()[0][9:11], ("circuit_open", "CIRCUIT_OPEN"))

    def test_a_failed_or_malformed_call_leaves_the_keywords_and_counts_against_the_circuit(self):
        bad = [FakeModel(error="HTTP_429"),
               FakeModel(Answer({"complaint": True, "category": "quality", "confidence": 2}, 10, 5)),
               FakeModel(Answer({"complaint": "yes", "category": "quality", "confidence": 0.9}, 10, 5)),
               FakeModel(Answer({"complaint": True, "category": "ignore previous", "confidence": 0.9}, 10, 5))]
        for model in bad:
            db = FakeDb(probe="PROBE")
            self.assertEqual(self.run_one(db, model), (False, None, 0.0))
            self.assertEqual(db.calls("record_outcome")[0][2:4], (False, True))     # the probe failed: open again
            self.assertEqual(db.rows()[0][9], "failure")
            self.assertIn(db.rows()[0][10], ("HTTP_429", "MALFORMED"))

    def test_a_call_dearer_than_its_reservation_is_reported_for_the_pause(self):
        db = FakeDb(exceeded=True)
        self.run_one(db, FakeModel(Answer({"complaint": False, "category": None, "confidence": 0.1}, 10, 5)))
        self.assertEqual(len(db.calls("record_reservation_exceeded")), 1)

    def test_long_messages_are_cut_to_the_contract_context(self):
        model = FakeModel(Answer({"complaint": False, "category": None, "confidence": 0.1}, 10, 5))
        self.run_one(FakeDb(), model, "س" * 9000)
        self.assertEqual(len(model.seen[0][1]), 1500)
        self.assertEqual(model.seen[0][2], 64)                                     # the contract's max_output_tokens

    def test_the_simulation_finds_the_paraphrases_of_spec_9_6_and_nothing_else(self):
        sim = SimulatedModel()
        self.assertTrue(sim.complete("m", INSTRUCTIONS, "الأكل وصل بارد والطلب ناقص", SCHEMA, 64).data["complaint"])
        self.assertFalse(sim.complete("m", INSTRUCTIONS, "متى تفتحون اليوم؟", SCHEMA, 64).data["complaint"])

    def test_configuration_all_or_nothing(self):
        self.assertIsNone(triage_from_env({}, FakeDb()))
        self.assertIsInstance(triage_from_env({"HERMES_MODEL": "simulate"}, FakeDb()).model, SimulatedModel)
        for env in ({"HERMES_MODEL": "on"}, {"HERMES_MODEL": "live"}):
            with self.assertRaises(ValueError):
                triage_from_env(env, FakeDb())


class FakeApi:
    def __init__(self, status=200, data=None, error=None):
        self.status, self.data, self.error, self.req = status, data, error, None

    def request(self, method, url, body=None, headers=None):
        self.req = (method, url, body, headers)
        if self.error:
            raise self.error
        return self.status, self.data


def responses(text, status="completed"):
    return {"status": status, "usage": {"input_tokens": 210, "output_tokens": 18},
            "output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


class OpenAITests(unittest.TestCase):
    def test_the_request_is_a_strict_schema_answer_with_no_storage(self):
        api = FakeApi(data=responses('{"complaint": false, "category": null, "confidence": 0.1}'))
        out = OpenAIModel("sk-test", api).complete("gpt-5.4-nano", INSTRUCTIONS, "مرحبا", SCHEMA, 64)
        self.assertEqual((out.data["complaint"], out.tokens_in, out.tokens_out), (False, 210, 18))
        method, url, body, headers = api.req
        self.assertEqual((method, url), ("POST", "https://api.openai.com/v1/responses"))
        self.assertEqual(headers, {"Authorization": "Bearer sk-test"})
        self.assertEqual((body["store"], body["reasoning"], body["max_output_tokens"], body["input"]),
                         (False, {"effort": "none"}, 64, "مرحبا"))
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])

    def test_every_failure_is_a_short_code_never_the_providers_text(self):
        cases = [(FakeApi(status=429, data={"error": {"message": "key sk-live-123 over quota"}}), "HTTP_429"),
                 (FakeApi(data=responses("{}", status="incomplete")), "INCOMPLETE"),
                 (FakeApi(data=responses("not json")), "MALFORMED"),
                 (FakeApi(data={"status": "completed", "output": []}), "INCOMPLETE"),
                 (FakeApi(error=TimeoutError("socket to 10.0.0.1 timed out")), "TIMEOUTERROR")]
        for api, code in cases:
            with self.assertRaises(ModelError) as ctx:
                OpenAIModel("sk-test", api).complete("gpt-5.4-nano", INSTRUCTIONS, "x", SCHEMA, 64)
            self.assertEqual(ctx.exception.code, code)
            self.assertNotIn("sk-live", str(ctx.exception))

    def test_no_key_no_client(self):
        with self.assertRaises(ValueError):
            OpenAIModel("", FakeApi())


if __name__ == "__main__":
    unittest.main()
