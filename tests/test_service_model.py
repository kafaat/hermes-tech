"""Model calls (service/model.py, spec 28.37, 28.47, P29): the budget is reserved and the circuit asked BEFORE the call, the
actual cost settled and the outcome recorded AFTER it, one agent_calls row each time and never the message text;
every refusal or failure leaves the keyword result; the live request is the Responses API with a strict schema.
The cursor below tests orchestration only: it does not implement financial counters, RLS or transaction locking.
Those assertions run on PostgreSQL in db/tests/rls_isolation_test.sql and db/tests/ops_cli_pg.py."""
import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.model import (INSTRUCTIONS, SCHEMA, Answer, ModelError, OpenAIModel, SimulatedModel, Triage,  # noqa: E402
                           Usage, checked_usage, priced_cost, triage_from_env)
from service.worker import Task  # noqa: E402

TASK = Task("00000000-0000-0000-0000-0000000000a1", "tok", 1, "00000000-0000-0000-0000-0000000000c1")
SECRET_TEXT = "رقم بطاقتي 4111 والأكل وصل بارد"


class FakeDb:
    """Answers the enforcement functions as scripted; records every statement and the bind it ran under."""

    def __init__(self, reserve="OK", probe="CLOSED", exceeded=False):
        self.answers = {"reserve_budget": reserve, "claim_probe": probe, "settle_budget": exceeded,
                        "record_outcome": "closed", "record_reservation_exceeded": False}
        self.sql = []
        self.holds = {}

    @contextmanager
    def tx(self, bind=None):
        db = self

        class Cur:
            last = None

            def execute(self, q, args=()):
                db.sql.append((bind, q, args))
                self.last = next(((v,) for k, v in db.answers.items() if f"app.{k}(" in q), None)
                if q.startswith("insert into app.model_usage_holds"):
                    key, digest = args[:2]
                    if key in db.holds:
                        self.last = None
                    else:
                        db.holds[key] = {"state": "reserved", "result": None, "request_sha256": digest}
                        self.last = (key,)
                elif q.startswith("select state, result, request_sha256"):
                    h = db.holds.get(args[0])
                    self.last = (h["state"], h["result"], h["request_sha256"]) if h else None
                elif q.startswith("update app.model_usage_holds"):
                    db.holds[args["key"]].update(args, result=json.loads(args["result"]))

            def fetchone(self):
                return self.last
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
            raise self.error if isinstance(self.error, Exception) else ModelError(self.error)
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

    def test_missing_or_invalid_usage_keeps_the_reservation_and_nullable_observation(self):
        for state, tin, tout, reason in (("unknown", None, None, "MISSING_USAGE"),
                                        ("measured", -1, 2, "INVALID_TOKEN_COUNT"),
                                        ("measured", True, 2, "INVALID_TOKEN_COUNT"),
                                        ("measured", 1.0, 2, "INVALID_TOKEN_COUNT")):
            with self.subTest(state=state, tin=tin):
                db = FakeDb()
                answer = Answer({"complaint": True, "category": "quality", "confidence": 0.9}, tin, tout, state)
                self.assertEqual(self.run_one(db, FakeModel(answer)), (False, None, 0.0))
                self.assertEqual(db.calls("settle_budget"), [])
                self.assertEqual(db.rows()[0][5:8], (None, None, None))
                self.assertEqual(db.rows()[0][14], reason)
                self.assertEqual(next(iter(db.holds.values()))["state"], "pending_usage")

    def test_a_real_explicit_zero_is_measured_and_settled_after_a_provider_call(self):
        db, model = FakeDb(), FakeModel(Answer({"complaint": False, "category": None, "confidence": 0.1}, 0, 0))
        self.run_one(db, model)
        self.assertEqual(len(model.seen), 1)
        self.assertEqual(db.rows()[0][5:8], (0, 0, 0.0))
        self.assertEqual(db.rows()[0][12], "measured")
        self.assertEqual(db.calls("settle_budget")[0][4], 0.0)

    def test_provider_errors_do_not_invent_a_zero_cost(self):
        for error in (ModelError("HTTP_429"), TimeoutError("private provider text")):
            db = FakeDb()
            self.run_one(db, FakeModel(error=error))
            self.assertEqual(db.calls("settle_budget"), [])
            self.assertEqual(db.rows()[0][5:8], (None, None, None))
            self.assertEqual(db.rows()[0][12], "unknown")
            self.assertNotIn("private provider text", str(db.sql))

    def test_a_failed_answer_with_valid_measured_usage_is_still_costed(self):
        db = FakeDb()
        usage = Usage(210, 18, "measured", "openai.responses.usage", None)
        self.run_one(db, FakeModel(error=ModelError("MALFORMED", usage)))
        self.assertEqual(db.calls("settle_budget")[0][4], 0.00007)
        self.assertEqual(db.rows()[0][5:7], (210, 18))
        self.assertEqual(db.rows()[0][9:11], ("failure", "MALFORMED"))

    def test_replaying_settled_or_pending_calls_does_not_reserve_or_invoke_again(self):
        for state, tin, tout in (("measured", 210, 18), ("unknown", None, None)):
            db = FakeDb()
            model = FakeModel(Answer({"complaint": True, "category": "quality", "confidence": 0.8}, tin, tout, state))
            first = self.run_one(db, model)
            self.assertEqual(self.run_one(db, model), first)
            self.assertEqual(len(model.seen), 1)
            self.assertEqual(len(db.calls("reserve_budget")), 1)
            self.assertEqual(len(db.rows()), 1)
            self.assertEqual(len(db.calls("settle_budget")), 1 if state == "measured" else 0)

    def test_a_changed_request_for_an_existing_task_is_a_conflict_without_a_second_call(self):
        db, model = FakeDb(), FakeModel(Answer({"complaint": True, "category": "quality", "confidence": 0.8}, 10, 5))
        self.run_one(db, model)
        with self.assertLogs("hermes.model", "WARNING") as logs:
            self.assertEqual(self.run_one(db, model, "different intent"), (False, None, 0.0))
        self.assertIn("MODEL_REQUEST_CONFLICT", logs.output[0])
        self.assertEqual(len(model.seen), 1)
        self.assertEqual(len(db.calls("reserve_budget")), 1)

    def test_simulated_usage_is_explicitly_estimated(self):
        db = FakeDb()
        self.run_one(db, SimulatedModel())
        self.assertEqual(db.rows()[0][12:15], ("estimated", "simulation.character_estimate", None))

    def test_an_external_adapters_estimate_cannot_release_the_real_reservation(self):
        db = FakeDb()
        model = FakeModel(Answer({"complaint": True, "category": "quality", "confidence": 0.9}, 300, 20,
                                 "estimated", "adapter.token_estimate"))
        self.assertEqual(self.run_one(db, model), (False, None, 0.0))
        self.assertEqual(db.calls("settle_budget"), [])
        self.assertEqual(db.rows()[0][5:8], (300, 20, .00009))
        self.assertEqual(db.rows()[0][10], "MODEL_USAGE_ESTIMATED")
        self.assertEqual(next(iter(db.holds.values()))["state"], "pending_usage")

    def test_cost_validation_refuses_invalid_counts_rates_and_unrepresentable_amounts(self):
        for tin, tout, pi, po in ((True, 0, 1, 1), (1.0, 0, 1, 1), ("1", 0, 1, 1),
                                  (-1, 0, 1, 1), (2**31, 0, 1, 1), (1, 1, float("nan"), 1),
                                  (1, 1, float("inf"), 1), (1, 1, -1, 1), (2, 2, 99999, 99999)):
            with self.subTest(tin=tin, pi=pi), self.assertRaises(ModelError):
                priced_cost(tin, tout, pi, po)
        self.assertEqual(priced_cost(0, 0, 1, 1), 0.0)
        self.assertEqual(priced_cost(1, 1, "0.0000002", "0.00000125"), 0.00001)

    def test_adapter_usage_metadata_is_validated_without_coercion(self):
        self.assertEqual(checked_usage(1, 2, [], "adapter").usage_state, "invalid")
        self.assertEqual(checked_usage(1, 2, "measured", "private response\n").usage_reason, "INVALID_USAGE_SOURCE")
        self.assertEqual(checked_usage(None, None, "unknown", "adapter", "private text").usage_reason, "MISSING_USAGE")


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

    def test_missing_empty_or_partial_usage_is_unknown_not_zero(self):
        for raw in (None, {}, {"input_tokens": 0}, {"output_tokens": 5}, {"input_tokens": None, "output_tokens": 5}):
            body = responses("{}")
            body["usage"] = raw
            answer = OpenAIModel("sk-test", FakeApi(data=body)).complete("m", INSTRUCTIONS, "x", SCHEMA, 64)
            self.assertEqual((answer.tokens_in, answer.tokens_out, answer.usage_state), (None, None, "unknown"))
        body.pop("usage")
        answer = OpenAIModel("sk-test", FakeApi(data=body)).complete("m", INSTRUCTIONS, "x", SCHEMA, 64)
        self.assertEqual(answer.usage_state, "unknown")

    def test_invalid_provider_counts_and_usage_shapes_are_not_coerced(self):
        for raw in ([], "", 0, True, {"input_tokens": -1, "output_tokens": 0},
                    *({"input_tokens": x, "output_tokens": 0} for x in (True, 1.0, "1", 2**31, float("nan"), float("inf")))):
            with self.subTest(raw=raw):
                body = responses("{}")
                body["usage"] = raw
                answer = OpenAIModel("sk-test", FakeApi(data=body)).complete("m", INSTRUCTIONS, "x", SCHEMA, 64)
                self.assertEqual((answer.tokens_in, answer.tokens_out, answer.usage_state), (None, None, "invalid"))

    def test_explicit_provider_zero_is_a_measurement(self):
        body = responses("{}")
        body["usage"] = {"input_tokens": 0, "output_tokens": 0}
        answer = OpenAIModel("sk-test", FakeApi(data=body)).complete("m", INSTRUCTIONS, "x", SCHEMA, 64)
        self.assertEqual(answer.usage, Usage(0, 0, "measured", "openai.responses.usage", None))

    def test_malformed_or_incomplete_output_preserves_the_provider_measurement(self):
        for body in (responses("not json"), responses("{}", "incomplete")):
            with self.assertRaises(ModelError) as ctx:
                OpenAIModel("sk-test", FakeApi(data=body)).complete("m", INSTRUCTIONS, "x", SCHEMA, 64)
            self.assertEqual(ctx.exception.usage, Usage(210, 18, "measured", "openai.responses.usage", None))


if __name__ == "__main__":
    unittest.main()
