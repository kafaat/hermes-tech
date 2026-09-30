"""Regression tests for the review of 1.5 (points on the fixes themselves).

N1 a half-open probe whose worker died stays claimed forever          -> probe lease
N2 an in-flight key whose worker died blocks duplicates forever        -> key lease + takeover
N3 a terminal failure (e.g. content rejected) is re-claimable          -> FAILED until an audited release
N4 a transient failure stays re-claimable
N5 repeated RESERVATION_EXCEEDED only alerts                           -> second one within 24 h pauses the agent
N6 the overshoot of a mis-bounded call is charged (cap stays hard for admission)
N7 an unknown provider error kind is not retried and fails the task to the human queue
"""
import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from enforce import Orchestrator, Denied, ToolError

TASK = {"task_id": "t_1", "customer_id": "cust_0421", "week_id": "2026-W40", "topic_hash": "a1", "message_id": "m1"}


class Clock:
    def __init__(self): self.t = 100_000.0
    def __call__(self): return self.t


def fail(kind, retryable=True):
    def f():
        raise ToolError(kind, retryable)
    return f


class TestRegressionsV16(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.o = Orchestrator(clock=self.clock)

    def test_N1_dead_probe_holder_does_not_freeze_the_circuit(self):
        ag = self.o.state("cust_0421").agent("agent_replies")
        ag.circuit, ag.opened_at = "open", self.clock.t - 10_000
        ag.probe_in_flight, ag.probe_until = True, self.clock.t + 5          # a live probe: refuse
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", TASK, lambda: ("x", 0.01), 0.01)
        self.assertEqual(e.exception.code, "CIRCUIT_OPEN")
        self.clock.t += 10                                                    # holder died, lease expired
        r = self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(TASK, message_id="m2"), lambda: ("x", 0.01), 0.01)
        self.assertEqual((r["status"], ag.circuit, ag.probe_in_flight), ("OK", "closed", False))

    def test_N2_dead_key_holder_is_taken_over_after_its_lease(self):
        c = self.o.contracts["agent_replies"]
        key = self.o.idempotency_key(c, TASK, "reply:draft", "reply.draft")
        self.o.idem[key] = {"status": "IN_PROGRESS", "lease_until": self.clock.t + 20}
        r = self.o.invoke("agent_replies", "reply:draft", "reply.draft", TASK, lambda: ("x", 0.01), 0.01)
        self.assertEqual(r["status"], "IN_PROGRESS")
        self.assertIn("retry_after", r)
        self.clock.t += 21
        r = self.o.invoke("agent_replies", "reply:draft", "reply.draft", TASK, lambda: ("x", 0.01), 0.01)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(self.o.flags[-1]["error_code"], "LEASE_TAKEOVER")

    def test_N3_terminal_failure_is_not_reclaimed_until_released(self):
        with self.assertRaises(Denied):
            self.o.invoke("agent_content", "content:draft", "llm.generate", TASK, fail("content_rejected", retryable=False), 0.03)
        ran = []
        r = self.o.invoke("agent_content", "content:draft", "llm.generate", TASK, lambda: (ran.append(1) or "x", 0.01), 0.03)
        self.assertEqual((r["status"], r["error"], ran), ("FAILED", "content_rejected", []))
        key = self.o.idempotency_key(self.o.contracts["agent_content"], TASK, "content:draft", "llm.generate")
        self.o.release_terminal(key, "founder", "owner revised the brief")
        r = self.o.invoke("agent_content", "content:draft", "llm.generate", TASK, lambda: (ran.append(1) or "x", 0.01), 0.03)
        self.assertEqual((r["status"], ran), ("OK", [1]))
        self.assertEqual(self.o.audit[-1]["event"], "idempotency.released")

    def test_N4_transient_failure_stays_reclaimable(self):
        with self.assertRaises(Denied):
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", TASK, fail("timeout"), 0.01)
        r = self.o.invoke("agent_replies", "reply:draft", "reply.draft", TASK, lambda: ("x", 0.01), 0.01)
        self.assertEqual(r["status"], "OK")

    def test_N5_second_reservation_exceeded_pauses_the_agent(self):
        for m in ("a", "b"):
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(TASK, task_id=f"t_{m}", message_id=m), lambda: ("x", 0.019), 0.01)
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(TASK, task_id="t_c", message_id="c"), lambda: ("x", 0.01), 0.01)
        self.assertEqual(e.exception.code, "AGENT_PAUSED")
        self.o.unpause("agent_replies", "founder", "registry prices updated")
        r = self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(TASK, task_id="t_c", message_id="c"), lambda: ("x", 0.01), 0.01)
        self.assertEqual(r["status"], "OK")

    def test_N6_overshoot_is_charged(self):
        ag = self.o.state("cust_0421").agent("agent_replies")
        self.o.invoke("agent_replies", "reply:draft", "reply.draft", TASK, lambda: ("x", 0.019), 0.01)
        self.assertAlmostEqual(ag.spent, 0.019, places=9)

    def test_N7_unknown_provider_error_is_not_retried(self):
        calls = []
        def f():
            calls.append(1)
            raise ToolError("weird_new_provider_status", retryable=True)
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_content", "content:draft", "llm.generate", TASK, f, 0.03)
        self.assertEqual((e.exception.code, len(calls)), ("EXECUTION_FAILED", 1))
        self.assertEqual(self.o.calls[-1]["error_code"], "weird_new_provider_status")   # raw kind kept in the log


if __name__ == "__main__":
    unittest.main()
