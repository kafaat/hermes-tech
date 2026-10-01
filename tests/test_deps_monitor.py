"""The external /deps monitor (ops/monitor/check_deps.py): reads the numbers with the token, fails on the status and
the answer's shape (never on wording), says which signal failed, and never prints the token."""
import io, json, sys, unittest, urllib.error
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ops/monitor"))
import check_deps  # noqa: E402

URL, TOKEN = "https://hermes.example/deps", "monitor-secret-token"
OK = {"status": "ok", "failing": [], "retention_stale": False, "overdue_bodies": 0, "outbox_attention": 0,
      "webhook_backlog": 0, "retention_due_at": "2026-10-02T03:18:02+00:00"}


class Resp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(status, body, seen=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def open_(req, timeout):
        if seen is not None:
            seen.append((req.full_url, req.get_header("X-monitor-token"), timeout))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(raw))
        return Resp(status, raw)
    return open_


class TestCheck(unittest.TestCase):
    def test_ok_passes_and_the_token_goes_in_the_header(self):
        seen = []
        code, line = check_deps.check(URL, TOKEN, opener(200, OK, seen))
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(URL, TOKEN, check_deps.TIMEOUT_SECONDS)])
        self.assertIn("purge due 2026-10-02", line)

    def test_degraded_names_the_failing_signals(self):
        body = {**OK, "status": "degraded", "failing": ["retention_stale", "outbox_attention"], "outbox_attention": 2}
        code, line = check_deps.check(URL, TOKEN, opener(503, body))
        self.assertEqual(code, 1)
        self.assertIn("DEGRADED (retention_stale,outbox_attention)", line)
        self.assertIn("outbox_attention=2", line)

    def test_a_wrong_token_or_another_page_is_a_failure_not_an_ok(self):
        for status, body in ((200, b"ok"), (503, b"degraded"), (200, {"status": "ok"}), (200, b"<html>")):
            with self.subTest(body=body):
                self.assertEqual(check_deps.check(URL, TOKEN, opener(status, body))[0], 1)

    def test_unreachable_and_bad_urls_fail(self):
        def down(req, timeout):
            raise TimeoutError("x")
        self.assertEqual(check_deps.check(URL, TOKEN, down), (1, "unreachable: TimeoutError"))
        for url in ("http://hermes.example/deps", "https://hermes.example/healthz", "https://hermes.example/deps?x=1"):
            self.assertEqual(check_deps.check(url, TOKEN, opener(200, OK))[0], 1, url)

    def test_unconfigured_warns_and_passes_and_the_token_is_never_printed(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(check_deps.main({}), 0)
        self.assertIn("::warning::", out.getvalue())
        for status, body in ((200, OK), (503, {**OK, "status": "degraded", "failing": ["webhook_backlog"]})):
            code, line = check_deps.check(URL, TOKEN, opener(status, body))
            self.assertNotIn(TOKEN, line)

    def test_the_workflow_runs_on_a_schedule_with_read_only_permission_and_secrets(self):
        wf = (ROOT / ".github/workflows/monitor.yml").read_text(encoding="utf-8")
        self.assertIn("schedule:", wf)
        self.assertIn("permissions:\n  contents: read", wf)
        self.assertIn("${{ secrets.HERMES_MONITOR_TOKEN }}", wf)
        self.assertIn("python3 ops/monitor/check_deps.py", wf)


if __name__ == "__main__":
    unittest.main()
