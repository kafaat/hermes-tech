import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.telemetry import sanitize_span, SanitizingExporter, MAPPING
from service.health import SIGNALS, assess, authorized


class Sink:
    def __init__(self):
        self.batches = []

    def export(self, spans):
        self.batches.append(spans)
        return "ok"


class TestTelemetry(unittest.TestCase):
    def test_content_capture_is_off_in_the_mapping(self):
        self.assertTrue(MAPPING["content_capture"])
        self.assertFalse(any(MAPPING["content_capture"].values()))

    def test_only_mapped_attributes_leave_and_content_events_are_dropped(self):
        span = {"name": "chat gpt-5.4-mini", "attributes": {
            "gen_ai.request.model": "gpt-5.4-mini", "gen_ai.usage.input_tokens": 812, "hermes.agent_id": "agent_replies",
            "gen_ai.input.messages": [{"role": "user", "content": "أريد حجز طاولة باسم أحمد"}],
            "gen_ai.output.messages": "تم", "gen_ai.prompt": "x", "hermes.body": "نص", "http.request.body": "x"},
            "events": [{"name": "gen_ai.content.prompt", "attributes": {"gen_ai.prompt": "كامل النص"}}]}
        out = sanitize_span(span)
        self.assertEqual(set(out["attributes"]), {"gen_ai.request.model", "gen_ai.usage.input_tokens", "hermes.agent_id"})
        self.assertEqual(out["events"], [])

    def test_mapped_attribute_that_carries_personal_data_is_dropped(self):
        out = sanitize_span({"name": "execute_tool send", "attributes": {"gen_ai.tool.name": "reply to +967771234567",
                                                                         "hermes.customer_ref": "cust_ab12", "error.type": "x" * 500}})
        self.assertEqual(out["attributes"], {"hermes.customer_ref": "cust_ab12"})

    def test_exporter_wrapper_sanitizes_every_batch(self):
        sink = Sink()
        SanitizingExporter(sink).export([{"name": "chat", "attributes": {"gen_ai.input.messages": "x"}, "exception_type": "TimeoutError",
                                          "exception_message": "phone 967771234567"}])
        (s,), = sink.batches
        self.assertEqual(s["attributes"], {"error.type": "TimeoutError"})
        self.assertNotIn("exception_message", s)


class TestDepsAssessment(unittest.TestCase):
    """GET /deps: what an external uptime monitor sees (numbers from app.health_signals(), SQL case 47)."""
    OK = {"retention_age_seconds": 3600, "overdue_bodies": 0, "outbox_attention": 0, "webhook_backlog": 0, "webhook_unrouted": 3}

    def test_healthy_signals_are_200_and_carry_numbers_only(self):
        status, body = assess(self.OK)
        self.assertEqual((status, body["status"], body["failing"]), (200, "ok", []))
        self.assertEqual(set(body), {"status", "failing", *SIGNALS})
        self.assertTrue(all(isinstance(body[k], int) for k in SIGNALS))

    def test_a_missed_or_never_run_purge_fails_after_26_hours(self):
        self.assertEqual(assess({**self.OK, "retention_age_seconds": 26 * 3600})[0], 200)
        for age in (26 * 3600 + 1, None):
            status, body = assess({**self.OK, "retention_age_seconds": age})
            self.assertEqual((status, body["failing"]), (503, ["retention_stale"]))

    def test_each_backlog_fails_by_name_and_unrouted_events_never_do(self):
        for key in ("overdue_bodies", "outbox_attention", "webhook_backlog"):
            status, body = assess({**self.OK, key: 1})
            self.assertEqual((status, body["status"], body["failing"]), (503, "degraded", [key]))
        self.assertEqual(assess({**self.OK, "webhook_unrouted": 500})[0], 200)

    def test_the_numbers_need_the_monitor_token(self):
        self.assertTrue(authorized("s3cret-token", "s3cret-token"))
        for header in (None, "", "s3cret", "s3cret-token ", "S3CRET-TOKEN"):
            self.assertFalse(authorized(header, "s3cret-token"))
        self.assertFalse(authorized("", ""), "no configured token: nobody gets the numbers")
        self.assertFalse(authorized(None, ""))


if __name__ == "__main__":
    unittest.main()
