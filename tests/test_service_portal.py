"""Owner portal: session tokens verified strictly, every action bound to the session (CSRF, same origin), no script,
every value escaped. The database path (owner session, policies, the decision reaching the outbox) runs in
db/tests/e2e_pilot.py against Postgres."""
import base64, hashlib, hmac, json, sys, unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.auth import AuthError, csrf_token, issue_staging_token, verify_session_token  # noqa: E402
from service.portal import COOKIE, Portal  # noqa: E402

SECRET, OWNER, NOW = "s" * 40, "00000000-0000-0000-0000-0000000e2e0a", 1_790_000_000


def sign(claims, secret=SECRET, header=None):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    h, b = enc(header or {"alg": "HS256", "typ": "JWT"}), enc(claims)
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{h}.{b}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{h}.{b}.{sig}"


GOOD = {"sub": OWNER, "aud": "authenticated", "role": "authenticated", "aal": "aal1", "iat": NOW, "exp": NOW + 3600}


class TestSessionToken(unittest.TestCase):
    def test_a_supabase_shaped_token_is_accepted(self):
        self.assertEqual(verify_session_token(sign(GOOD), SECRET, NOW)["sub"], OWNER)
        self.assertEqual(verify_session_token(issue_staging_token(OWNER, SECRET, now=NOW), SECRET, NOW)["sub"], OWNER)

    def test_everything_looser_is_refused(self):
        bad = {
            "alg none": sign(GOOD, header={"alg": "none"}),
            "alg RS256": sign(GOOD, header={"alg": "RS256"}),
            "other secret": sign(GOOD, secret="x" * 40),
            "expired": sign({**GOOD, "exp": NOW - 61}),
            "no exp": sign({k: v for k, v in GOOD.items() if k != "exp"}),
            "not yet valid": sign({**GOOD, "nbf": NOW + 120}),
            "audience": sign({**GOOD, "aud": "anon"}),
            "role": sign({**GOOD, "role": "service_role"}),
            "subject": sign({**GOOD, "sub": "not-a-uuid"}),
            "aal": sign({**GOOD, "aal": "aal9"}),
            "malformed": "abc.def",
            "too long": "a" * 9000,
        }
        tampered = sign(GOOD).split(".")
        tampered[1] = base64.urlsafe_b64encode(json.dumps({**GOOD, "sub": "11111111-1111-1111-1111-111111111111"}).encode()).rstrip(b"=").decode()
        bad["tampered body"] = ".".join(tampered)
        for name, token in bad.items():
            with self.subTest(name), self.assertRaises(AuthError):
                verify_session_token(token, SECRET, NOW)
        with self.assertRaises(AuthError):
            verify_session_token(sign(GOOD, secret=""), "", NOW)          # no secret configured: nobody


class FakeStore:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def overview(self, claims):
        self.calls.append(("overview", claims["sub"]))
        return {"customers": [{"id": "c1", "name": "مطعم <b>الريف</b>"}],
                "approvals": [{"id": "a1", "customer_id": "c1", "action": "reply:send", "expires_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
                               "payload": {"to": "967700000001", "body": "<script>alert(1)</script> نفتح 9"}}],
                "inquiries": [{"received_at": datetime(2026, 9, 30, tzinfo=timezone.utc), "category": "pricing", "owner_inquiry": False,
                               "body": "\"><img src=x onerror=alert(1)>"}],
                "facts": [{"id": "f1", "topic": "hours", "fact": "9-11", "approved": False}],
                "competitors": [{"label": "بيت <i>الريف</i>", "fetched_at": datetime(2026, 9, 30, tzinfo=timezone.utc), "status": "ok",
                                 "summary": "تغيّر سعر مندي: 4500 ← 5000 YER"},
                                {"label": "مطعم ب", "fetched_at": None, "status": None, "summary": None}]}

    def decide(self, claims, approval_id, decision):
        if self.fail:
            raise RuntimeError("DECIDER_MUST_BE_SESSION_USER")
        self.calls.append(("decide", claims["sub"], approval_id, decision))
        return True

    def approve_fact(self, claims, fact_id):
        self.calls.append(("fact", claims["sub"], fact_id))
        return True

    operator = True

    def ops_overview(self, claims):
        self.calls.append(("ops", claims["sub"]))
        if not self.operator:
            return None
        return {"retention_last_run": datetime(2026, 9, 30, 18, 40, tzinfo=timezone.utc), "overdue_bodies": 0, "unrouted": 2,
                "competitors": {"active": 5, "structured": 1, "unstructured": 3, "blocked": 1},
                "rows": [{"id": 7, "customer": "مطعم", "topic": "notify.owner", "attempts": 1, "last_error": "<i>AMBIGUOUS</i>",
                          "needs_human_check": True},
                         {"id": 8, "customer": None, "topic": "notify.owner", "attempts": 1, "last_error": None,
                          "needs_human_check": False}]}

    def resolve(self, claims, outbox_id, resolution, reason):
        if self.fail:
            raise RuntimeError("OPERATOR_AAL2_ONLY")
        self.calls.append(("resolve", claims["sub"], outbox_id, resolution, reason))


def portal(store=None, **kw):
    return Portal(store or FakeStore(), SECRET, now=lambda: NOW, **kw)


def cookie(token):
    return {"Cookie": f"other=1; {COOKIE}={token}", "Host": "hermes.example"}


class TestPortal(unittest.TestCase):
    def test_without_a_session_only_the_sign_in_page_and_no_data(self):
        store = FakeStore()
        status, headers, body = portal(store).handle("GET", "/portal", {"Host": "h"}, b"")
        self.assertEqual(status, 200)
        self.assertEqual(store.calls, [])
        self.assertNotIn("staging-login", body.decode())                   # no staging login unless configured

    def test_every_response_forbids_scripts_and_framing(self):
        for method, path in (("GET", "/portal"), ("POST", "/portal/decide"), ("GET", "/portal/x")):
            _, headers, _ = portal().handle(method, path, {"Host": "h"}, b"")
            h = dict(headers)
            self.assertIn("default-src 'none'", h["Content-Security-Policy"])
            self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
            self.assertEqual(h["Cache-Control"], "no-store")

    def test_the_page_escapes_every_value_and_masks_the_customer_number(self):
        token = sign(GOOD)
        status, _, body = portal().handle("GET", "/portal", cookie(token), b"")
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<b>الريف", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("•••001", page)
        self.assertNotIn("967700000001", page)
        self.assertIn(csrf_token(token, SECRET), page)

    def test_a_decision_needs_the_session_csrf_token_and_the_same_origin(self):
        store, token = FakeStore(), sign(GOOD)
        p = portal(store)
        body = f"approval=a1&decision=approved&csrf={csrf_token(token, SECRET)}".encode()
        self.assertEqual(p.handle("POST", "/portal/decide", cookie(token), b"approval=a1&decision=approved")[0], 403)
        other = sign({**GOOD, "exp": NOW + 1800})                           # a token from another session
        self.assertEqual(p.handle("POST", "/portal/decide", cookie(token),
                                  f"approval=a1&decision=approved&csrf={csrf_token(other, SECRET)}".encode())[0], 403)
        self.assertEqual(p.handle("POST", "/portal/decide", {**cookie(token), "Origin": "https://evil.example"}, body)[0], 403)
        self.assertEqual(store.calls, [])
        status, headers, _ = p.handle("POST", "/portal/decide", {**cookie(token), "Origin": "https://hermes.example"}, body)
        self.assertEqual((status, dict(headers)["Location"]), (303, "/portal?done=approved"))
        self.assertEqual(store.calls, [("decide", OWNER, "a1", "approved")])

    def test_only_approve_or_reject_and_a_refusal_is_reported_without_detail(self):
        token = sign(GOOD)
        csrf = csrf_token(token, SECRET)
        self.assertEqual(portal().handle("POST", "/portal/decide", cookie(token),
                                         f"approval=a1&decision=executed&csrf={csrf}".encode())[0], 400)
        status, headers, _ = portal(FakeStore(fail=True)).handle("POST", "/portal/decide", cookie(token),
                                                                 f"approval=a1&decision=approved&csrf={csrf}".encode())
        self.assertEqual(dict(headers)["Location"], "/portal?done=error")

    def test_an_expired_or_forged_session_is_signed_out(self):
        for token in (sign({**GOOD, "exp": NOW - 120}), sign(GOOD, secret="x" * 40)):
            status, headers, _ = portal().handle("POST", "/portal/decide", cookie(token), b"approval=a1&decision=approved&csrf=x")
            self.assertEqual((status, dict(headers)["Location"]), (303, "/portal"))
            self.assertIn("Max-Age=0", dict(headers)["Set-Cookie"])

    def test_sign_in_sets_a_host_only_secure_http_only_strict_cookie(self):
        status, headers, _ = portal().handle("POST", "/portal/session", {"Host": "h"}, f"access_token={sign(GOOD)}".encode())
        c = dict(headers)["Set-Cookie"]
        self.assertEqual(status, 303)
        for part in (f"{COOKIE}=", "Path=/", "Secure", "HttpOnly", "SameSite=Strict", "Max-Age=3600"):
            self.assertIn(part, c)
        self.assertEqual(portal().handle("POST", "/portal/session", {"Host": "h"}, b"access_token=forged")[0], 401)

    def test_the_staging_login_needs_simulation_a_code_and_the_right_code(self):
        form = b"code=letmein-staging-0123456789"
        self.assertEqual(portal().handle("POST", "/portal/staging-login", {"Host": "h"}, form)[0], 403)
        self.assertEqual(portal(staging_login_code="letmein-staging-0123456789", staging_owner_id=OWNER, simulate=False)
                         .handle("POST", "/portal/staging-login", {"Host": "h"}, form)[0], 403)
        p = portal(staging_login_code="letmein-staging-0123456789", staging_owner_id=OWNER, simulate=True)
        self.assertEqual(p.handle("POST", "/portal/staging-login", {"Host": "h"}, b"code=wrong")[0], 403)
        status, headers, _ = p.handle("POST", "/portal/staging-login", {"Host": "h"}, form)
        self.assertEqual(status, 303)
        token = dict(headers)["Set-Cookie"].split(";")[0].split("=", 1)[1]
        self.assertEqual(verify_session_token(token, SECRET, NOW)["sub"], OWNER)
        self.assertIn("staging-login", p.handle("GET", "/portal", {"Host": "h"}, b"")[2].decode())

    def test_oversize_forms_are_refused_before_parsing(self):
        token = sign(GOOD)
        self.assertEqual(portal().handle("POST", "/portal/decide", cookie(token), b"x=" + b"a" * 5000)[0], 403)

    def test_malformed_forms_and_a_database_outage_get_an_answer_not_a_crash(self):
        token = sign(GOOD)
        many = "&".join(f"f{i}=1" for i in range(12)).encode()
        self.assertEqual(portal().handle("POST", "/portal/decide", cookie(token), many)[0], 400)

        class Down(FakeStore):
            def overview(self, claims):
                raise OSError("connection refused")
        status, headers, body = portal(Down()).handle("GET", "/portal", cookie(token), b"")
        self.assertEqual(status, 503)
        self.assertNotIn("connection refused", body.decode())

    def test_the_owner_sees_each_competitor_state_escaped(self):
        page = portal().handle("GET", "/portal", cookie(sign(GOOD)), b"")[2].decode()
        self.assertIn("بيت &lt;i&gt;الريف&lt;/i&gt;", page)
        self.assertIn("تمت المتابعة", page)
        self.assertIn("تغيّر سعر مندي: 4500 ← 5000 YER", page)
        self.assertIn("لم يُفحص بعد", page)

    def test_fact_approval_and_logout(self):
        store, token = FakeStore(), sign(GOOD)
        csrf = csrf_token(token, SECRET)
        status, headers, _ = portal(store).handle("POST", "/portal/facts/approve", cookie(token), f"fact=f1&csrf={csrf}".encode())
        self.assertEqual((status, dict(headers)["Location"], store.calls[-1]), (303, "/portal?done=fact", ("fact", OWNER, "f1")))
        status, headers, _ = portal().handle("POST", "/portal/logout", cookie(token), f"csrf={csrf}".encode())
        self.assertIn("Max-Age=0", dict(headers)["Set-Cookie"])


OPERATOR = "22222222-2222-2222-2222-222222222222"


class TestOperatorConsole(unittest.TestCase):
    def test_the_console_is_shown_only_when_the_database_says_operator(self):
        store = FakeStore()
        store.operator = False
        status, _, body = portal(store).handle("GET", "/portal/ops", cookie(sign(GOOD)), b"")
        self.assertEqual(status, 403)
        self.assertNotIn("#7", body.decode())
        self.assertEqual(portal().handle("GET", "/portal/ops", {"Host": "h"}, b"")[0], 303)      # no session: sign in

    def test_rows_waiting_for_a_human_get_a_decision_form_and_values_are_escaped(self):
        token = sign({**GOOD, "sub": OPERATOR, "aal": "aal2"})
        status, _, body = portal().handle("GET", "/portal/ops", cookie(token), b"")
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("&lt;i&gt;AMBIGUOUS&lt;/i&gt;", page)
        self.assertEqual(page.count('action="/portal/ops/resolve"'), 1)                     # row 8 still waits for the worker
        self.assertIn("ينتظر العامل", page)
        self.assertIn("ببيانات منظمة 1 · بلا بيانات منظمة 3 · يمنعون الفحص 1", page)
        self.assertIn(csrf_token(token, SECRET), page)

    def test_a_resolution_needs_csrf_a_known_value_and_a_reason(self):
        store, token = FakeStore(), sign({**GOOD, "sub": OPERATOR, "aal": "aal2"})
        csrf = csrf_token(token, SECRET)
        p = portal(store)
        self.assertEqual(p.handle("POST", "/portal/ops/resolve", cookie(token), b"outbox=7&resolution=resend&reason=long enough")[0], 403)
        self.assertEqual(p.handle("POST", "/portal/ops/resolve", cookie(token),
                                  f"outbox=7&resolution=delete&reason=long enough&csrf={csrf}".encode())[0], 400)
        status, headers, _ = p.handle("POST", "/portal/ops/resolve", cookie(token), f"outbox=7&resolution=resend&reason=no&csrf={csrf}".encode())
        self.assertEqual(dict(headers)["Location"], "/portal/ops?done=reason")
        self.assertEqual([c for c in store.calls if c[0] == "resolve"], [])
        status, headers, _ = p.handle("POST", "/portal/ops/resolve", cookie(token),
                                      f"outbox=7&resolution=resend&reason=provider says not delivered&csrf={csrf}".encode())
        self.assertEqual((status, dict(headers)["Location"]), (303, "/portal/ops?done=resolved"))
        self.assertEqual(store.calls[-1], ("resolve", OPERATOR, "7", "resend", "provider says not delivered"))
        status, headers, _ = portal(FakeStore(fail=True)).handle("POST", "/portal/ops/resolve", cookie(token),
                                                                 f"outbox=7&resolution=abandon&reason=long enough&csrf={csrf}".encode())
        self.assertEqual(dict(headers)["Location"], "/portal/ops?done=error")

    def test_the_staging_operator_login_gives_aal2_only_to_the_configured_operator(self):
        form = b"code=letmein-staging-0123456789&as=operator"
        no_op = portal(staging_login_code="letmein-staging-0123456789", staging_owner_id=OWNER, simulate=True)
        self.assertEqual(no_op.handle("POST", "/portal/staging-login", {"Host": "h"}, form)[0], 403)
        p = portal(staging_login_code="letmein-staging-0123456789", staging_owner_id=OWNER, staging_operator_id=OPERATOR, simulate=True)
        status, headers, _ = p.handle("POST", "/portal/staging-login", {"Host": "h"}, form)
        claims = verify_session_token(dict(headers)["Set-Cookie"].split(";")[0].split("=", 1)[1], SECRET, NOW)
        self.assertEqual((status, dict(headers)["Location"], claims["sub"], claims["aal"]), (303, "/portal/ops", OPERATOR, "aal2"))
        status, headers, _ = p.handle("POST", "/portal/staging-login", {"Host": "h"}, b"code=letmein-staging-0123456789")
        owner = verify_session_token(dict(headers)["Set-Cookie"].split(";")[0].split("=", 1)[1], SECRET, NOW)
        self.assertEqual((owner["sub"], owner["aal"]), (OWNER, "aal1"))
        off = portal(staging_login_code="letmein-staging-0123456789", staging_owner_id=OWNER, staging_operator_id=OPERATOR, simulate=False)
        self.assertEqual(off.handle("POST", "/portal/staging-login", {"Host": "h"}, form)[0], 403)


if __name__ == "__main__":
    unittest.main()
