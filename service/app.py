"""Staging process: the webhook endpoint and the worker loop (P1), wired to Postgres.

    python -m service.app      env: DATABASE_URL, HERMES_WEBHOOK_SECRETS (comma-separated), HERMES_VERIFY_TOKEN,
                                    HERMES_GRAPH=simulate|live (live: HERMES_GRAPH_TOKEN), PORT (default 8080),
                                    HERMES_APPROVAL_POLL_SECONDS (60)

  GET  /webhook   Meta subscription handshake (webhook.verify_subscription)
  POST /webhook   webhook.Handler: size limit, HMAC on the raw bytes, then insert as hermes_ingest; 200 only after commit
  GET  /portal, POST /portal/...   the owner portal (service/portal.py): pending replies, inquiries, facts. Sign-in
                  through Supabase Auth when HERMES_SUPABASE_URL and HERMES_SUPABASE_ANON_KEY are set (the access
                  tokens are verified with HERMES_JWT_SECRET, the project's legacy JWT secret, or with the project's
                  published signing keys, ES256 / RS256, spec 28.26; HERMES_JWT_SECRET also keys the CSRF tokens)
  POST /forms/<site key>          the contact form of a customer's site (service/site_form.py, spec 28.24)
  POST /email/inbound             Postmark's inbound webhook (service/email_inbound.py, spec 28.25), basic auth with
                  HERMES_EMAIL_INBOUND_SECRETS ("user:password", comma-separated); unset: 404
  GET  /healthz   database reachable (as hermes_ingest), the deployed commit and the handler counters (numbers only)
  GET  /deps      what must stay true between deploys, for an external uptime monitor: app.health_signals() as
                  hermes_monitor (numbers, never rows); 503 when a signal fails. The numbers and the failing names
                  only with X-Monitor-Token = HERMES_MONITOR_TOKEN; otherwise the code and "ok" / "degraded".
                  Railway checks /healthz at deploy time only and never reports a skipped or hung cron run, so this
                  is read from outside.
The worker runs in a thread as hermes_worker. Outbound connections go through crawler.ApiClient only (spec 28.12):
the portal's calls to the Supabase project host, and with HERMES_GRAPH=live the Graph API. HERMES_GRAPH must be
"simulate" or "live" (spec 28.14), and anything else refuses to start.
"""
from __future__ import annotations
import json, logging, os, signal, sys, threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from service import redact
from service.health import SIGNALS, assess, authorized
from service.pg import Database, Ingest, PortalDb
from service.portal import Portal
from service.supabase_auth import from_env as supabase_from_env
from service.email_inbound import EMAIL_MAX_BYTES, EmailHandler
from service.site_form import FORM_MAX_BYTES, FormHandler
from service.webhook import MAX_BODY_BYTES, Handler, verify_subscription
from service.worker import Worker, adapters_for, worker_name

log = logging.getLogger("hermes.app")
COMMIT = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
SHUTDOWN_WAIT_SECONDS = 25        # for the task in hand after SIGTERM: above a send's timeout, below Railway's drain (30 s)          # lets a caller wait for THIS build, not the previous one


def make_http_handler(webhook: Handler, verify_token: str, monitor: Database | None = None, monitor_token: str = "",
                      portal: Portal | None = None, forms: FormHandler | None = None, email: EmailHandler | None = None):
    class H(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: str, ctype: str = "text/plain; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _portal(self, body: bytes = b""):
            status, headers, data = portal.handle(self.command, self.path, dict(self.headers.items()), body)
            self.send_response(status)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlsplit(self.path)
            if portal is not None and (u.path == "/portal" or u.path.startswith("/portal/")):
                return self._portal()
            if u.path == "/healthz":                   # Railway calls it at deploy time only: a build that cannot reach
                try:                                   # the database never goes live
                    with webhook.ingest.db.tx() as cur:
                        cur.execute("select 1")
                except Exception as exc:               # noqa: BLE001
                    return self._reply(503, json.dumps({"status": "db_unreachable", "error": type(exc).__name__}),
                                       "application/json")
                counters = {"webhook": asdict(webhook.counters)}
                if email is not None:
                    counters["email"] = asdict(email.counters)
                return self._reply(200, json.dumps({"status": "ok", "commit": COMMIT, **counters}), "application/json")
            if u.path == "/deps" and monitor is not None:
                full = authorized(self.headers.get("X-Monitor-Token"), monitor_token)
                try:
                    with monitor.tx() as cur:
                        cur.execute("select " + ", ".join(SIGNALS) + " from app.health_signals()")
                        signals = dict(zip(SIGNALS, cur.fetchone()))
                except Exception as exc:               # noqa: BLE001
                    if not full:
                        return self._reply(503, "db_unreachable")
                    return self._reply(503, json.dumps({"status": "db_unreachable", "error": type(exc).__name__}),
                                       "application/json")
                status, body = assess(signals)
                if not full:
                    return self._reply(status, body["status"])
                return self._reply(status, json.dumps(body), "application/json")
            if u.path == "/webhook":
                return self._reply(*verify_subscription(dict(parse_qsl(u.query)), verify_token))
            return self._reply(404, "not found")

        def do_POST(self):
            path = urlsplit(self.path).path
            if portal is not None and path.startswith("/portal/"):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._reply(400, "bad length")
                if length > 4096:
                    return self._reply(413, "too large")
                return self._portal(self.rfile.read(length))
            if forms is not None and path.startswith("/forms/"):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._reply(400, "bad length")
                if length > FORM_MAX_BYTES:
                    return self._reply(413, "too large")      # never read an oversize body
                status, headers, data = forms.handle(path[len("/forms/"):], dict(self.headers.items()), self.rfile.read(length))
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if email is not None and path == "/email/inbound":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return self._reply(400, "bad length")
                if length > EMAIL_MAX_BYTES:
                    return self._reply(403, "too large")      # never read; 403 stops the provider's retries
                try:
                    status, body = email.handle(dict(self.headers.items()), self.rfile.read(length))
                except Exception as exc:                      # noqa: BLE001 - not committed: the provider retries
                    log.error("email ingest failed: %s", type(exc).__name__)
                    status, body = 500, "retry"
                return self._reply(status, body)
            if path != "/webhook":
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
    try:
        adapters = adapters_for(os.environ)
    except RuntimeError as exc:
        sys.exit(str(exc))
    url = os.environ["DATABASE_URL"]
    secrets = [s.strip().encode() for s in os.environ["HERMES_WEBHOOK_SECRETS"].split(",") if s.strip()]
    if not secrets:
        sys.exit("HERMES_WEBHOOK_SECRETS is empty")
    ingest = Ingest(Database(url, "hermes_ingest"))
    webhook = Handler(secrets, ingest)
    worker = Worker(Database(url, "hermes_worker"), worker_name(), adapters,
                    approval_poll_seconds=int(os.environ.get("HERMES_APPROVAL_POLL_SECONDS", "60")))
    stop = threading.Event()
    worker_thread = threading.Thread(target=worker.loop, kwargs={"stop": stop}, name="worker", daemon=True)
    worker_thread.start()
    port = int(os.environ.get("PORT", "8080"))
    email_secrets = [s.strip() for s in os.environ.get("HERMES_EMAIL_INBOUND_SECRETS", "").split(",") if s.strip()]
    email = EmailHandler(email_secrets, ingest) if email_secrets else None
    secret = os.environ.get("HERMES_JWT_SECRET", "")          # Supabase project JWT secret; unset: the portal signs nobody in
    auth = supabase_from_env(os.environ)
    keys = None
    if auth is not None:                                       # signing keys: fetched only when such a token arrives
        from service.crawler import ApiClient
        from service.jwks import from_url
        from service.supabase_auth import project_host
        keys = from_url(os.environ["HERMES_SUPABASE_URL"], os.environ["HERMES_SUPABASE_ANON_KEY"],
                        ApiClient({project_host(os.environ["HERMES_SUPABASE_URL"])}))
    portal = Portal(PortalDb(Database(url, "authenticated")), secret, auth=auth, keys=keys,
                    staging_login_code=os.environ.get("HERMES_STAGING_LOGIN_CODE", ""),
                    staging_owner_id=os.environ.get("HERMES_STAGING_OWNER_ID", ""),
                    staging_operator_id=os.environ.get("HERMES_STAGING_OPERATOR_ID", ""),
                    simulate=os.environ.get("HERMES_GRAPH") == "simulate")
    server = ThreadingHTTPServer(("0.0.0.0", port), make_http_handler(
        webhook, os.environ.get("HERMES_VERIFY_TOKEN", ""), Database(url, "hermes_monitor"),
        os.environ.get("HERMES_MONITOR_TOKEN", ""), portal, FormHandler(ingest), email))

    def on_term(signum, frame):                    # Railway sends SIGTERM, then SIGKILL after drainingSeconds: stop taking
        log.info("SIGTERM: draining")               # requests, let the current task finish (its lease covers a kill anyway)
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()   # shutdown() blocks; never call it on this thread
    signal.signal(signal.SIGTERM, on_term)
    log.info("listening on %s", port)
    server.serve_forever()
    server.server_close()
    worker_thread.join(SHUTDOWN_WAIT_SECONDS)
    log.info("stopped%s", " (worker still busy)" if worker_thread.is_alive() else "")



if __name__ == "__main__":
    main()
