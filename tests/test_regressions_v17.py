"""Reference-enforcement findings of the independent review (measured on 1.1, still true in 1.6).

M1 budget state was not partitioned by month
M2 a tiny caller estimate (0.001) with an actual cost of 0.50 returned OK and changed nothing
M3 the reservation trusted the caller's estimate instead of the computed bound
"""
import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from enforce import Orchestrator, Denied

TASK = {"task_id": "t_1", "customer_id": "cust_0421", "week_id": "W", "topic_hash": "h", "message_id": "m"}


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


class TestRegressionsV17(unittest.TestCase):
    def test_M1_new_month_starts_from_zero(self):
        clock = Clock(1790000000.0)                                  # 2026-09
        o = Orchestrator(clock=clock)
        o.state("cust_0421").agent("agent_content").spent = 0.39
        with self.assertRaises(Denied):
            o.invoke("agent_content", "content:draft", "llm.generate", TASK, lambda: ("x", 0.03), 0.03)
        clock.t += 40 * 86400                                        # 2026-10
        r = o.invoke("agent_content", "content:draft", "llm.generate", dict(TASK, topic_hash="h2"), lambda: ("x", 0.03), 0.03)
        self.assertEqual(r["status"], "OK")
        self.assertAlmostEqual(o.state("cust_0421").agent("agent_content").spent, 0.03, places=9)

    def test_M2_actual_cost_above_per_call_pauses_the_agent_at_once(self):
        o = Orchestrator()
        o.invoke("agent_content", "content:draft", "llm.generate", TASK, lambda: ("x", 0.50), 0.001)
        self.assertEqual(o.paused.get("agent_content"), "PER_CALL_BREACH")
        with self.assertRaises(Denied) as e:
            o.invoke("agent_content", "content:draft", "llm.generate", dict(TASK, topic_hash="h2"), lambda: ("x", 0.01), 0.01)
        self.assertEqual(e.exception.code, "AGENT_PAUSED")

    def test_M3_computed_bound_overrides_a_low_estimate(self):
        o = Orchestrator()
        bound = o.call_upper_bound("agent_content", 8000, "llm.generate")
        o.state("cust_0421").agent("agent_content").spent = 0.40 - bound / 2
        with self.assertRaises(Denied) as e:                          # the estimate 0.001 would have fitted
            o.invoke("agent_content", "content:draft", "llm.generate", TASK, lambda: ("x", 0.001), 0.001, input_tokens=8000)
        self.assertEqual(e.exception.code, "BUDGET_EXCEEDED_AGENT")


if __name__ == "__main__":
    unittest.main()
