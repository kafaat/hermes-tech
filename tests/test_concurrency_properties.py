"""Property tests around the v1.5 fixes: many threads, crash paths, and stage 1 / stage 2 parity."""
import random, sys, threading, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from enforce import Orchestrator, Denied
from complaints import Matcher, normalize
from triage import combined_route


class TestConcurrencyProperties(unittest.TestCase):
    def test_many_threads_never_overshoot_agent_or_customer_caps(self):
        o = Orchestrator()
        rnd = random.Random(7)
        jobs = []
        for i in range(60):
            aid, action, tool, cost = rnd.choice([("agent_content", "content:draft", "llm.generate", 0.03),
                                                  ("agent_replies", "reply:draft", "reply.draft", 0.02),
                                                  ("agent_quality", "flag:raise", "flag.raise", 0.005)])
            task = {"task_id": f"t_{i}", "customer_id": "cust_0421", "week_id": "W", "topic_hash": f"h{i}",
                    "message_id": f"m{i}", "content_id": f"c{i}", "content_hash": f"x{i}"}
            jobs.append((aid, action, tool, task, cost))
        barrier = threading.Barrier(len(jobs), timeout=5)

        def run(j):
            aid, action, tool, task, cost = j
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            try:
                o.invoke(aid, action, tool, task, lambda: ("ok", cost), cost)
            except Denied:
                pass
        ts = [threading.Thread(target=run, args=(j,)) for j in jobs]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        st = o.state("cust_0421")
        for aid, a in st.agents.items():
            self.assertLessEqual(a.spent, o.contracts[aid]["budget"]["class_cap_usd"] + 1e-9, aid)
            self.assertAlmostEqual(a.reserved, 0.0, places=9)
        self.assertLessEqual(st.ai_spent_total, o.limits["ai_cap_per_active_customer_month_usd"] + 1e-9)

    def test_crash_releases_key_and_reservation(self):
        o = Orchestrator()
        task = {"task_id": "t_1", "customer_id": "cust_0421", "week_id": "W", "topic_hash": "h"}

        def boom():
            raise RuntimeError("worker died")
        with self.assertRaises(RuntimeError):
            o.invoke("agent_content", "content:draft", "llm.generate", task, boom, 0.03)
        ag = o.state("cust_0421").agent("agent_content")
        self.assertAlmostEqual(ag.reserved, 0.0, places=9)
        r = o.invoke("agent_content", "content:draft", "llm.generate", task, lambda: ("ok", 0.01), 0.03)
        self.assertEqual(r["status"], "OK")

    def test_actual_above_reservation_is_flagged(self):
        o = Orchestrator()
        task = {"task_id": "t_1", "customer_id": "cust_0421", "message_id": "m"}
        o.invoke("agent_replies", "reply:draft", "reply.draft", task, lambda: ("ok", 0.019), 0.01)
        self.assertEqual(o.flags[-1]["error_code"], "RESERVATION_EXCEEDED")

    def test_upper_bound_covers_any_call_within_max_output(self):
        o = Orchestrator()
        c = o.contracts["agent_content"]
        price = o.registry["approved_models"][c["model"]["primary"]]
        bound = o.call_upper_bound("agent_content", 5000, "llm.generate")
        for out_tokens in (0, 100, c["model"]["params"]["max_output_tokens"]):
            actual = (5000 * price["input_usd_per_mtok"] + out_tokens * price["output_usd_per_mtok"]) / 1e6
            self.assertLessEqual(actual, bound + 1e-12)

    def test_stage2_decision_equals_stage1_for_every_category_and_channel(self):
        m = Matcher()
        for cat in m.p["categories"]:
            kw = cat["keywords"][0]
            for ch in ("patron", "client"):
                s1 = m.route(kw, ch)
                s2 = combined_route(m, "نص بلا كلمات مفتاحية", ch, lambda t, c=cat["id"]: (True, c, 0.99))
                for k in ("severity", "sla_minutes", "routes", "categories"):
                    self.assertEqual(s1[k], s2[k], f"{cat['id']}/{ch}/{k}")


if __name__ == "__main__":
    unittest.main()
