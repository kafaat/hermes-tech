"""Operator API and the hermes command line: a session token verified as the portal's and aal2 required; every
argument typed and bounded, an unknown one refused; the staging login only where it is configured; no answer
carries an exception text; the command line holds its session in a 0600 file, talks HTTPS only (http on loopback),
and maps every answer to the exit codes of spec 14. The database side runs in db/tests/ops_cli_pg.py."""
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.auth import issue_staging_token, verify_session_token  # noqa: E402
from service.ops_api import COMMANDS, OpsApi, OpsError, clean_args  # noqa: E402
from cli import hermes  # noqa: E402

SECRET, OPERATOR, NOW = "s" * 40, "00000000-0000-0000-0000-00000000c11a", 1_790_000_000


class FakeStore:
    def __init__(self, answer=None, error=None):
        self.calls, self.answer, self.error = [], answer, error

    def run(self, claims, name, args):
        self.calls.append((claims["sub"], name, args))
        if self.error:
            raise self.error
        return self.answer if self.answer is not None else {"ran": name}


def token(aal="aal2", sub=OPERATOR, secret=SECRET, now=NOW):
    return issue_staging_token(sub, secret, now=now, aal=aal)


def call(api, name, args=None, tok=None, raw=None):
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    body = raw if raw is not None else json.dumps({"args": args or {}}).encode()
    status, headers, data = api.handle("POST", "/ops/api/" + name, headers, body)
    return status, json.loads(data)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.api = OpsApi(self.store, SECRET, now=lambda: NOW)

    def test_a_command_runs_in_the_verified_operator_session(self):
        status, doc = call(self.api, "whoami", tok=token())
        self.assertEqual((status, doc), (200, {"ok": True, "data": {"ran": "whoami"}}))
        self.assertEqual(self.store.calls, [(OPERATOR, "whoami", {})])

    def test_no_or_forged_or_expired_session_is_401_and_reaches_no_store(self):
        for tok in (None, "x.y.z", token(secret="t" * 40), token(now=NOW - 7200)):
            self.assertEqual(call(self.api, "whoami", tok=tok)[0], 401)
        self.assertEqual(self.store.calls, [])

    def test_a_session_without_the_second_factor_is_refused(self):
        self.assertEqual(call(self.api, "whoami", tok=token("aal1")), (403, {"ok": False, "error": "aal2_required"}))
        self.assertEqual(self.store.calls, [])

    def test_unknown_command_unknown_argument_and_bad_values_never_reach_the_store(self):
        self.assertEqual(call(self.api, "drop.tables", tok=token())[0], 404)
        cases = [("customers.show", {"customer": "cust_ab12", "sql": "x"}, "unknown_argument"),
                 ("customers.show", {}, "missing"),
                 ("customers.show", {"customer": "'; drop"}, "invalid"),
                 ("customers.add", {"name": "x", "sector": "bank", "city": "c", "zone": "zone_a"}, "invalid"),
                 ("calls", {"limit": 501}, "invalid"),
                 ("calls", {"limit": True}, "invalid"),
                 ("calls", {"outcome": "success; --"}, "invalid"),
                 ("competitors.add", {"customer": "cust_ab12", "url": "http://x.example", "label": "a"}, "invalid"),
                 ("billing.invoice", {"subscription": "00000000-0000-0000-0000-000000000001", "amount": "10",
                                      "currency": "YER"}, "missing"),
                 ("billing.payment", {"invoice": "00000000-0000-0000-0000-000000000001", "channel": "bank",
                                      "receipt": "r", "amount": "-5", "currency": "USD", "received_at": "2026-11-01"},
                  "invalid"),
                 ("billing.subscribe", {"customer": "cust_ab12", "price_usd": "100", "start": "2026-11-01",
                                        "end": "2026-11-01"}, "invalid"),
                 ("incident.update", {"id": "INC-2026-11-01-001", "field": "fixed_at", "value": "2026-11-01T10:00"},
                  "invalid"),
                 ("customers.add", {"name": "x", "sector": "retail", "city": "c", "zone": "zone_a", "reason": "no"},
                  "invalid")]
        for name, args, error in cases:
            status, doc = call(self.api, name, args, token())
            self.assertEqual((status, doc["error"]), (400, error), (name, args))
        self.assertEqual(self.store.calls, [])

    def test_each_channel_kind_is_routed_by_its_own_identity(self):
        ok = [("whatsapp_cloud", "106540352242922"), ("email", "shop@inbound.example.com"),
              ("site_form", "A" * 32), ("site_form", None)]
        bad = [("whatsapp_cloud", "pn-1"), ("email", "Shop@Example"), ("site_form", "short"), ("facebook_page", None)]
        for kind, ext in ok:
            args = {"customer": "cust_ab12", "kind": kind, **({"external_id": ext} if ext else {})}
            self.assertEqual(clean_args("channels.add", args)["kind"], kind)
        for kind, ext in bad:
            args = {"customer": "cust_ab12", "kind": kind, **({"external_id": ext} if ext else {})}
            with self.assertRaises(OpsError):
                clean_args("channels.add", args)

    def test_usage_reconciliation_requires_integer_counts_evidence_and_server_owned_authority(self):
        args = {"call_key": "a" * 64, "tokens_in": 0, "tokens_out": 12, "evidence_sha256": "b" * 64,
                "provider_ref": "receipt-test-1", "reason": "provider receipt checked"}
        self.assertEqual(clean_args("usage.reconcile", args), args)
        self.assertEqual(COMMANDS["usage.reconcile"].role, "tech")
        for field, value in (("tokens_in", True), ("tokens_in", 1.0), ("tokens_out", "12"),
                             ("tokens_out", -1), ("tokens_in", 2**31), ("evidence_sha256", "bad"),
                             ("provider_ref", "private response with whitespace")):
            with self.subTest(field=field, value=value), self.assertRaises(OpsError):
                clean_args("usage.reconcile", {**args, field: value})
        for field in ("customer_id", "usage_source", "price_in", "actor_id", "usage_state"):
            with self.subTest(field=field), self.assertRaises(OpsError) as ctx:
                clean_args("usage.reconcile", {**args, field: "caller cannot choose"})
            self.assertEqual(ctx.exception.code, "unknown_argument")
        args.pop("reason")
        self.assertEqual(call(self.api, "usage.reconcile", args, token())[0], 400)
        self.assertEqual(self.store.calls, [])

    def test_a_reason_is_required_where_no_incident_stands_in_for_it(self):
        for name, args in (("agent.enable", {"agent": "agent_replies"}),
                           ("outbox.resolve", {"id": 7, "resolution": "abandon"})):
            self.assertEqual(call(self.api, name, args, token()), (400, {"ok": False, "error": "reason_required",
                                                                         "field": "reason"}))
        status, _ = call(self.api, "agent.disable", {"agent": "agent_replies"}, token())
        self.assertEqual(status, 200)        # the store decides: reason, or an open incident
        self.assertNotIn("reason", self.store.calls[-1][2])

    def test_a_read_takes_no_reason(self):
        self.assertEqual(call(self.api, "customers.list", {"reason": "because"}, token())[1]["error"], "unknown_argument")

    def test_every_write_names_an_audit_event_except_the_one_the_database_audits(self):
        for name, cmd in COMMANDS.items():
            if cmd.write:
                self.assertTrue(cmd.event or name == "outbox.resolve", name)
                self.assertIn(cmd.role, ("ops", "tech", "founder", "finance"), name)

    def test_store_errors_answer_their_code_and_never_their_text(self):
        api = OpsApi(FakeStore(error=OpsError(409, "ACTIVATION_WITHOUT_MATCHED_PAYMENT")), SECRET, now=lambda: NOW)
        self.assertEqual(call(api, "billing.activate", {"subscription": "00000000-0000-0000-0000-000000000001",
                                                        "reason": "paid in cash"}, token()),
                         (409, {"ok": False, "error": "ACTIVATION_WITHOUT_MATCHED_PAYMENT"}))
        api = OpsApi(FakeStore(error=RuntimeError("password=hunter2 at db.internal")), SECRET, now=lambda: NOW)
        with self.assertLogs("hermes.ops", "ERROR") as logs:
            status, doc = call(api, "whoami", tok=token())
        self.assertEqual((status, doc), (500, {"ok": False, "error": "server"}))
        self.assertNotIn("hunter2", "".join(logs.output))

    def test_body_limits_and_shape(self):
        self.assertEqual(call(self.api, "whoami", tok=token(), raw=b"x" * 20000)[0], 413)
        self.assertEqual(call(self.api, "whoami", tok=token(), raw=b"[1]")[0], 400)
        self.assertEqual(call(self.api, "whoami", tok=token(), raw=b"{bad")[0], 400)
        status, _, _ = self.api.handle("GET", "/ops/api/whoami", {"Authorization": f"Bearer {token()}"}, b"")
        self.assertEqual(status, 404)

    def test_staging_login_only_where_configured_and_only_with_the_code(self):
        self.assertEqual(call(self.api, "session/staging", raw=b'{"code": "c"}')[0], 404)
        api = OpsApi(self.store, SECRET, now=lambda: NOW, staging_login_code="right-code", staging_operator_id=OPERATOR)
        self.assertEqual(call(api, "session/staging", raw=b'{"code": "wrong"}')[0], 403)
        self.assertEqual(call(api, "session/staging", raw=b'{}')[0], 403)
        status, doc = call(api, "session/staging", raw=b'{"code": "right-code"}')
        self.assertEqual(status, 200)
        claims = verify_session_token(doc["access_token"], SECRET, NOW)
        self.assertEqual((claims["sub"], claims["aal"]), (OPERATOR, "aal2"))


class _Serve:
    """service.app's route for /ops/api/, on loopback, around an OpsApi."""

    def __init__(self, api):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                status, headers, data = api.handle("POST", self.path, dict(self.headers.items()), self.rfile.read(n))
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.session = Path(self.dir.name) / "hermes" / "session.json"
        self.env = mock.patch.dict(os.environ, {"HERMES_SESSION_FILE": str(self.session)})
        self.env.start()
        self.store = FakeStore({"rows": [{"public_ref": "cust_ab12", "status": "active"}]})
        self.srv = _Serve(OpsApi(self.store, SECRET, staging_login_code="code-1", staging_operator_id=OPERATOR))

    def tearDown(self):
        self.srv.close()
        self.env.stop()
        self.dir.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = hermes.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def login(self):
        with mock.patch("getpass.getpass", return_value="code-1"):
            return self.cli("login", "--url", self.srv.url, "--staging")

    def test_every_command_of_the_server_is_on_the_command_line(self):
        parser = hermes.build_parser()
        for name, cmd in COMMANDS.items():
            argv = [name] if name in hermes.TOP else [name.split(".")[0], name.split(".", 1)[1].replace("_", "-")]
            for arg, (check, required) in cmd.args.items():
                if required:
                    argv += ["--" + arg.replace("_", "-"), "1"]
            if cmd.reason_required:
                argv += ["--reason", "a reason"]
            if name in ("logs.export", "site.build"):
                argv += ["--to", "x"]
            self.assertEqual(parser.parse_args(argv).command, name)

    def test_staging_login_writes_a_session_only_this_user_reads(self):
        self.assertEqual(self.login()[0], 0)
        self.assertEqual(stat.S_IMODE(self.session.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.session.parent.stat().st_mode), 0o700)
        with mock.patch("getpass.getpass", return_value="wrong"):
            self.assertEqual(self.cli("login", "--url", self.srv.url, "--staging")[0], 3)

    def test_a_command_sends_its_arguments_and_prints_rows(self):
        self.login()
        code, out, _ = self.cli("customers", "list", "--status", "active")
        self.assertEqual(code, 0)
        self.assertEqual(out, "public_ref\tstatus\ncust_ab12\tactive\n")
        self.assertEqual(self.store.calls[-1], (OPERATOR, "customers.list", {"status": "active"}))
        code, out, _ = self.cli("--json", "agent", "disable", "--agent", "agent_replies", "--reason", "incident 4")
        self.assertEqual(json.loads(out), self.store.answer)
        self.assertEqual(self.store.calls[-1][2], {"agent": "agent_replies", "reason": "incident 4"})

    def test_answers_map_to_the_exit_codes_of_spec_14(self):
        self.login()
        for error, want in ((OpsError(400, "invalid", "x"), 2), (OpsError(403, "role"), 3), (OpsError(404, "not_found"), 4),
                            (OpsError(409, "COMPETITOR_LIMIT_REACHED"), 5), (RuntimeError("x"), 10)):
            self.store.error = error
            code, _, err = self.cli("whoami")
            self.assertEqual(code, want, error)
            self.assertIn(error.code if isinstance(error, OpsError) else "server", err)

    def test_no_session_a_shared_session_file_and_plain_http_are_refused(self):
        self.assertEqual(self.cli("whoami")[0], 3)
        self.login()
        os.chmod(self.session, 0o644)
        code, _, err = self.cli("whoami")
        self.assertEqual(code, 3)
        self.assertIn("readable by others", err)
        self.assertEqual(self.cli("login", "--url", "http://hermes.example", "--staging")[0], 2)

    def test_a_redirect_is_not_followed_with_the_session_token(self):
        seen = []

        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(307)
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            hermes.save_session({"url": f"http://127.0.0.1:{srv.server_address[1]}", "access_token": token()})
            self.assertEqual(self.cli("whoami")[0], 10)
            self.assertEqual(len(seen), 1)              # asked once, never re-sent to the Location
        finally:
            srv.shutdown()
            srv.server_close()

    def test_evidence_goes_to_a_new_file_and_never_overwrites_one(self):
        self.login()
        self.store.answer = {"audit": [{"id": 1}], "agent_calls": []}
        target = Path(self.dir.name) / "evidence.json"
        code, out, _ = self.cli("logs", "export", "--customer", "cust_ab12", "--to", str(target), "--reason", "INC-1 review")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(target.read_text()), self.store.answer)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(self.cli("logs", "export", "--customer", "cust_ab12", "--to", str(target), "--reason", "again")[0], 2)

    def test_an_expiring_supabase_session_is_renewed_before_the_call(self):
        hermes.save_session({"url": self.srv.url, "access_token": token(now=0), "refresh_token": "r1"})
        fresh = hermes.SupabaseAuth.__new__(hermes.SupabaseAuth)
        with mock.patch.object(hermes, "supabase", return_value=fresh), \
             mock.patch.object(hermes.SupabaseAuth, "refresh",
                               return_value=hermes.SupabaseAuth._session({"access_token": token(), "refresh_token": "r2"})):
            sess = hermes.load_session()
            self.assertEqual(hermes.token_for(sess, now=lambda: NOW), token())
        self.assertEqual(hermes.load_session()["refresh_token"], "r2")


if __name__ == "__main__":
    unittest.main()
