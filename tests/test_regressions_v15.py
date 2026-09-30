"""Regression tests for defects reported against package 1.1 (still present through 1.4).

R1 concurrent calls overshoot the agent cap (0.37 + 2 x 0.03 = 0.43 > 0.40)
R2 a duplicate request running concurrently executes twice
R3 text and image generation in one task share an idempotency key (image returns cached text)
R4 stage-2 triage downgrades a legal complaint to high / 60 min (policy: critical / 15 min)
R5 half-open circuit lets more than one concurrent probe through   (same check-then-act class)
R6 per_task_usd is declared in contracts but never enforced          (found while fixing R1)
"""
import sys, threading, time, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from enforce import Orchestrator, Denied, ToolError
from complaints import Matcher
from triage import combined_route

TASK = {"task_id": "t_1", "customer_id": "cust_0421", "week_id": "2026-W40", "topic_hash": "a1", "message_id": "m1"}


def run_concurrently(fns):
    out = [None] * len(fns)

    def wrap(i, f):
        try:
            out[i] = f()
        except Exception as e:  # noqa: BLE001
            out[i] = e
    ts = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in ts]
    [t.join(5) for t in ts]
    return out


class Rendezvous:
    """Holds each execution until the other thread also arrives (or 0.5 s pass)."""
    def __init__(self, n=2):
        self.b = threading.Barrier(n, timeout=0.5)
        self.count = 0
        self.lock = threading.Lock()

    def execute(self, result, cost):
        def f():
            with self.lock:
                self.count += 1
            try:
                self.b.wait()
            except threading.BrokenBarrierError:
                pass
            return result, cost
        return f


class TestRegressions(unittest.TestCase):
    def setUp(self):
        self.o = Orchestrator()

    def test_R1_concurrent_calls_never_exceed_agent_cap(self):
        ag = self.o.state("cust_0421").agent("agent_content")
        ag.spent = 0.37
        rv = Rendezvous()
        calls = [lambda h=h: self.o.invoke("agent_content", "content:draft", "llm.generate", dict(TASK, topic_hash=h),
                                          rv.execute("x", 0.03), 0.03) for h in ("h1", "h2")]
        out = run_concurrently(calls)
        self.assertLessEqual(round(ag.spent, 6), 0.40, f"spent {ag.spent:.2f}")
        self.assertEqual(sum(isinstance(r, dict) and r["status"] == "OK" for r in out), 1)
        self.assertTrue(any(isinstance(r, Denied) and r.code == "BUDGET_EXCEEDED_AGENT" for r in out))

    def test_R2_concurrent_duplicate_executes_once(self):
        rv = Rendezvous()
        calls = [lambda: self.o.invoke("agent_content", "content:draft", "llm.generate", dict(TASK), rv.execute("x", 0.01), 0.01)] * 2
        out = run_concurrently(calls)
        self.assertEqual(rv.count, 1, "duplicate executed twice")
        self.assertIn({r["status"] for r in out if isinstance(r, dict)}, ({"OK", "IN_PROGRESS"}, {"OK"}))

    def test_R3_text_and_image_in_one_task_do_not_share_a_key(self):
        ran = []
        t = self.o.invoke("agent_content", "content:draft", "llm.generate", dict(TASK), lambda: (ran.append("llm") or "TEXT", 0.003), 0.01)
        i = self.o.invoke("agent_content", "content:image_generate", "flux.generate", dict(TASK), lambda: (ran.append("flux") or "IMAGE", 0.025), 0.03)
        self.assertEqual((t["result"], i["result"]), ("TEXT", "IMAGE"))
        self.assertEqual(ran, ["llm", "flux"])

    def test_R4_stage2_uses_policy_severity_and_sla(self):
        m = Matcher()
        legal = lambda text: (True, "legal", 0.9)
        d = combined_route(m, "بيوصلكم خطاب من الجهات المختصة", "patron", legal)
        self.assertEqual((d["severity"], d["sla_minutes"]), ("critical", 15))
        self.assertIn("founder_slack", d["routes"])                    # patron legal is copied to the founder
        d = combined_route(m, "بيوصلكم خطاب من الجهات المختصة", "client", legal)
        self.assertEqual((d["severity"], d["sla_minutes"]), ("critical", 15))
        self.assertIn("founder_sms", d["routes"])

    def test_R4b_unknown_category_is_conservative(self):
        d = combined_route(Matcher(), "كلام غامض", "client", lambda t: (True, "something_else", 0.9))
        pol = Matcher().p["triage_unknown_category"]
        self.assertEqual((d["severity"], d["sla_minutes"]), (pol["severity"], pol["sla_minutes"]))
        self.assertTrue(d.get("needs_category"))

    def test_R5_half_open_admits_a_single_probe(self):
        ag = self.o.state("cust_0421").agent("agent_replies")
        ag.circuit, ag.opened_at = "open", self.o.clock() - 10_000
        rv = Rendezvous()
        calls = [lambda m=m: self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(TASK, message_id=m), rv.execute("x", 0.01), 0.01)
                 for m in ("p1", "p2")]
        out = run_concurrently(calls)
        self.assertEqual(rv.count, 1, "more than one probe in half_open")
        self.assertTrue(any(isinstance(r, Denied) and r.code == "CIRCUIT_OPEN" for r in out))

    def test_R6_per_task_cap_is_enforced(self):
        for i in range(2):
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(TASK, message_id=f"k{i}"), lambda: ("x", 0.02), 0.02)
        with self.assertRaises(Denied) as e:
            self.o.invoke("agent_replies", "reply:draft", "reply.draft", dict(TASK, message_id="k2"), lambda: ("x", 0.02), 0.02)
        self.assertEqual(e.exception.code, "BUDGET_EXCEEDED_TASK")


if __name__ == "__main__":
    unittest.main()
