"""Staging process: the webhook endpoint and the worker loop (P1), wired to Postgres.

    python -m service.app      env: DATABASE_URL, HERMES_WEBHOOK_SECRETS (comma-separated), HERMES_VERIFY_TOKEN,
                                    HERMES_GRAPH=simulate, PORT (default 8080), HERMES_APPROVAL_POLL_SECONDS (60)

  GET  /webhook   Meta subscription handshake (webhook.verify_subscription)
  POST /webhook   webhook.Handler: size limit, HMAC on the raw bytes, then insert as hermes_ingest; 200 only after commit
  GET  /healthz   liveness, the deployed commit and the handler counters (numbers only)
The worker runs in a thread as hermes_worker. It accepts connections; it opens none (the Graph API is simulated:
HERMES_GRAPH must be "simulate" until a real client exists, and anything else refuses to start).
"""
from __future__ import annotations
import json, logging, os, sys, threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from service import redact
from service.pg import Database, Ingest
from service.webhook import MAX_BODY_BYTES, Handler, verify_subscription
from service.worker import Worker, simulated_adapters, worker_name

log = logging.getLogger("hermes.app")
COMMIT = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")          # lets a caller wait for THIS build, not the previous one


def make_http_handler(webhook: Handler, verify_token: str):
    class H(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: str, ctype: str = "text/plain; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlsplit(self.path)
            if u.path == "/healthz":
                return self._reply(200, json.dumps({"status": "ok", "commit": COMMIT, "webhook": asdict(webhook.counters)}),
                                   "application/json")
            if u.path == "/webhook":
                return self._reply(*verify_subscription(dict(parse_qsl(u.query)), verify_token))
            return self._reply(404, "not found")

        def do_POST(self):
            if urlsplit(self.path).path != "/webhook":
                return self._reply(404, "not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._reply(400, "bad length")
            if length > MAX_BODY_BYTES:
                return self._reply(413, "too large")          # never read an oversize body
            raw = self.rfile.read(length)
            try:
                status, body = webhook.handle(dict(self.headers.items()), raw)
            except Exception as exc:                          # noqa: BLE001 - not committed: the provider retries
                log.error("ingest failed: %s", type(exc).__name__)
                status, body = 500, "retry"
            return self._reply(status, body)

        def log_message(self, fmt, *args):                    # request lines go through the redacting logger
            log.info("http %s", self.command)
    return H


def main():
    handler = logging.StreamHandler(sys.stdout)              # added before install(): the filter goes on the handler,
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))  # so every logger is redacted
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    redact.install()
    if os.environ.get("HERMES_GRAPH") != "simulate":
        sys.exit("HERMES_GRAPH must be 'simulate': no real Graph API client is built (P3)")
    url = os.environ["DATABASE_URL"]
    secrets = [s.strip().encode() for s in os.environ["HERMES_WEBHOOK_SECRETS"].split(",") if s.strip()]
    if not secrets:
        sys.exit("HERMES_WEBHOOK_SECRETS is empty")
    webhook = Handler(secrets, Ingest(Database(url, "hermes_ingest")))
    worker = Worker(Database(url, "hermes_worker"), worker_name(), simulated_adapters(),
                    approval_poll_seconds=int(os.environ.get("HERMES_APPROVAL_POLL_SECONDS", "60")))
    threading.Thread(target=worker.loop, name="worker", daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on %s", port)
    ThreadingHTTPServer(("0.0.0.0", port), make_http_handler(webhook, os.environ.get("HERMES_VERIFY_TOKEN", ""))).serve_forever()


if __name__ == "__main__":
    main()
