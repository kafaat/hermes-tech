import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from enforce import Orchestrator, Denied, ToolError


def ok(cost=0.01, result="done"):
    return lambda: (result, cost)


def fail(kind="timeout", retryable=True):
    def f():
        raise ToolError(kind, retryable)
    return f


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


class TestEnforcement(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.o = Orchestrator(clock=self.clock)
        self.task = {"task_id": "t_1", "customer_id": "cust_0421", "week_id": "2026-W40", "topic_hash": "a1", "message_id": "m1",
                     "competitor_url": "https://x", "month_id": "2026-10", "check_seq": 1}

    def test_disabled_agent(self):
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_billing", "receipt:read", "receipt.read", {"task_id": "t", "receipt_id": 1, "invoice_id": 2}, ok(), 0.001)
        self.assertEqual(e.exception.code, "AGENT_DISABLED")

    def test_tool_not_in_contract(self):
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_content", "content:draft", "meta.publish", self.task, ok(), 0.01)
        self.assertEqual(e.exception.code, "TOOL_NOT_IN_CONTRACT")

    def test_proposal_is_queued_not_executed(self):
        executed = []
        r = self.o.invoke("agent_content", "content:publish", "meta.create_draft", self.task, lambda: executed.append(1) or ("x", 0), 0.0)
        self.assertEqual(r["status"], "APPROVAL_QUEUED")
        self.assertEqual(executed, [])
        self.assertEqual(len(self.o.approvals), 1)

    def test_denied_action(self):
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_content", "billing:refund", "llm.generate", self.task, ok(), 0.01)
        self.assertEqual(e.exception.code, "PERMISSION_DENIED")

    def test_unlisted_action_denied_by_default(self):
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_replies", "kb:write", "kb.search", self.task, ok(), 0.01)
        self.assertEqual(e.exception.code, "PERMISSION_DENIED")

    def test_per_call_cap(self):
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", self.task, ok(), 0.5)
        self.assertEqual(e.exception.code, "BUDGET_EXCEEDED_CALL")

    def test_agent_class_cap(self):
        for i in range(20):
            # one task per message: the per-task cap (R6) must not mask the monthly class cap under test
            t = dict(self.task, task_id=f"t_{i}", message_id=f"m{i}")
            try:
                self.o.invoke("agent_replies", "reply:draft", "reply.draft", t, ok(0.02), 0.02)
            except Denied as e:
                self.assertEqual(e.code, "BUDGET_EXCEEDED_AGENT")
                self.assertAlmostEqual(self.o.state("cust_0421").agent("agent_replies").spent, 0.20, places=6)
                return
        self.fail("class cap never enforced")

    def test_customer_ai_cap_across_agents(self):
        st = self.o.state("cust_0421")
        st.ai_spent_total = 1.12
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", self.task, ok(0.02), 0.02)
        self.assertEqual(e.exception.code, "BUDGET_EXCEEDED_CUSTOMER")

    def test_agent_cap_compares_agent_spend_not_customer_total(self):
        # regression of v1.0 bug: total customer spend compared with a single agent's cap
        st = self.o.state("cust_0421")
        st.ai_spent_total = 0.90                    # other agents spent a lot
        r = self.o.invoke("agent_replies", "reply:draft", "reply.draft", self.task, ok(0.02), 0.02)
        self.assertEqual(r["status"], "OK")

    def test_idempotency(self):
        calls = []
        f = lambda: (calls.append(1) or "draft", 0.01)
        r1 = self.o.invoke("agent_replies", "reply:draft", "reply.draft", self.task, f, 0.01)
        r2 = self.o.invoke("agent_replies", "reply:draft", "reply.draft", self.task, f, 0.01)
        self.assertEqual(r1, r2)
        self.assertEqual(len(calls), 1)

    def test_retry_then_success(self):
        seq = [fail("timeout"), ok(0.01)]
        def f():
            return seq.pop(0)()
        r = self.o.invoke("agent_content", "content:draft", "llm.generate", self.task, f, 0.05)
        self.assertEqual(r["status"], "OK")

    def test_non_retryable_error_stops(self):
        with self.assertRaises(Denied):
            self.o.invoke("agent_content", "content:draft", "llm.generate", self.task, fail("upstream_5xx"), 0.05)
        failures = [c for c in self.o.calls if c["outcome"] == "failure"]
        self.assertEqual(len(failures), 1)          # upstream_5xx is not retryable for agent_content

    def test_circuit_opens_after_threshold_then_half_open(self):
        for i in range(3):
            with self.assertRaises(Denied):
                self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(self.task, message_id=f"x{i}"), fail("network"), 0.01)
        ag = self.o.state("cust_0421").agent("agent_replies")
        self.assertEqual(ag.circuit, "open")
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(self.task, message_id="y"), ok(), 0.01)
        self.assertEqual(e.exception.code, "CIRCUIT_OPEN")
        self.clock.t += 901
        r = self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(self.task, message_id="z"), ok(), 0.01)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(ag.circuit, "closed")

    def test_half_open_failure_reopens(self):
        ag = self.o.state("cust_0421").agent("agent_replies")
        ag.circuit, ag.opened_at = "open", self.clock.t
        self.clock.t += 901
        with self.assertRaises(Denied):
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", self.task, fail("network"), 0.01)
        self.assertEqual(ag.circuit, "open")

    def test_acquisition_does_not_touch_customer_cap(self):
        t = {"task_id": "t_9", "customer_id": None, "city": "c", "sector": "restaurant", "query_hash": "q", "day_id": "2026-10-01"}
        r = self.o.invoke("agent_search", "lead:draft", "lead.upsert", t, ok(0.01), 0.01)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(self.o.state(None).ai_spent_total, 0.0)


if __name__ == "__main__":
    unittest.main()
