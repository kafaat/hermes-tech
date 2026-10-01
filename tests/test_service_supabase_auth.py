"""Owner and operator sign-in through Supabase Auth, server side: a 6-digit code by email, the session decided by the
server (never by the browser), renewed before it expires, and a second factor (TOTP) for the operator console. Supabase
is faked here at its REST surface; the network path is crawler.ApiClient (tests/test_service_crawler.py)."""
import sys, unittest
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from service.auth import csrf_token, issue_staging_token  # noqa: E402
from service.portal import COOKIE, REFRESH_COOKIE, Portal  # noqa: E402
from service.supabase_auth import AuthFailed, SupabaseAuth, project_host  # noqa: E402
from test_service_portal import NOW, OWNER, SECRET, FakeStore  # noqa: E402

URL, KEY, OPERATOR = "https://abcd1234.supabase.co", "anon-key", "00000000-0000-0000-0000-0000000e2e0b"
FACTOR = "11111111-2222-3333-4444-555555555555"


class FakeGoTrue:
    """Supabase Auth's REST answers; records each call without its secrets."""

    def __init__(self, status=200, raises=None):
        self.calls, self.status, self.raises, self.factors, self.rotations = [], status, raises, [], 0

    def tok(self, sub=OWNER, aal="aal1", ttl=3600):
        return issue_staging_token(sub, SECRET, ttl, now=NOW, aal=aal)

    def request(self, method, url, body=None, headers=None):
        assert url.startswith(URL + "/auth/v1/"), url
        assert headers["apikey"] == KEY
        path = url[len(URL):]
        self.calls.append((method, path, body))
        if self.raises:
            raise self.raises
        if self.status != 200:
            return self.status, {"msg": "secret detail from supabase"}
        if path == "/auth/v1/otp":
            return 200, {}
        if path == "/auth/v1/verify":
            return (200, {"access_token": self.tok(), "refresh_token": "r1"}) if body["token"] == "123456" else (400, {})
        if path.startswith("/auth/v1/token"):
            if body["refresh_token"] != f"r{self.rotations + 1}":
                return 400, {}
            self.rotations += 1
            return 200, {"access_token": self.tok(), "refresh_token": f"r{self.rotations + 1}"}
        if path == "/auth/v1/user":
            return 200, {"factors": self.factors}
        if path == "/auth/v1/factors":
            self.factors.append({"id": FACTOR, "factor_type": "totp", "status": "unverified"})
            return 200, {"id": FACTOR, "totp": {"secret": "JBSWY3DP<b>EHPK3PXP", "qr_code": "data:..."}}
        if path.endswith("/challenge"):
            return 200, {"id": "ch1"}
        if path.endswith("/verify"):
            return (200, {"access_token": self.tok(OPERATOR, "aal2"), "refresh_token": "r-aal2"}) if body["code"] == "654321" \
                else (422, {})
        if path.startswith("/auth/v1/logout"):
            return 204, {}
        return 404, {}


def setup(api=None, store=None):
    api = api or FakeGoTrue()
    return api, Portal(store or FakeStore(), SECRET, now=lambda: NOW, auth=SupabaseAuth(URL, KEY, api))


def post(p, path, form, cookies=""):
    return p.handle("POST", path, {"Host": "h", "Cookie": cookies, "Origin": "https://h"}, urlencode(form).encode())


def set_cookies(headers):
    return {v.split("=", 1)[0]: v for k, v in headers if k == "Set-Cookie"}


class TestSupabaseAuth(unittest.TestCase):
    def test_the_project_url_must_be_a_bare_https_host(self):
        self.assertEqual(project_host("https://AbCd.supabase.co/"), "abcd.supabase.co")
        for bad in ("http://x.supabase.co", "https://x.supabase.co/rest", "https://x.supabase.co:8443", "https://", "x"):
            with self.assertRaises(ValueError, msg=bad):
                project_host(bad)

    def test_codes_go_to_existing_accounts_only_and_bad_input_never_leaves(self):
        api = FakeGoTrue()
        auth = SupabaseAuth(URL, KEY, api)
        auth.send_code("Owner@Example.com")
        self.assertEqual(api.calls, [("POST", "/auth/v1/otp", {"email": "owner@example.com", "create_user": False})])
        for email, code in (("not-an-email", "123456"), ("a@b.co", "12345"), ("a@b.co", "12345a")):
            with self.assertRaises(AuthFailed):
                auth.verify_code(email, code)
        self.assertEqual(len(api.calls), 1)

    def test_supabase_refusals_become_short_codes_without_its_text(self):
        for api, code in ((FakeGoTrue(429), "rate_limited"), (FakeGoTrue(503), "unavailable"), (FakeGoTrue(400), "invalid"),
                          (FakeGoTrue(raises=TimeoutError("x")), "unavailable")):
            with self.assertRaises(AuthFailed) as e:
                SupabaseAuth(URL, KEY, api).send_code("a@b.co")
            self.assertEqual(e.exception.code, code)
            self.assertNotIn("secret", str(e.exception))


class TestPortalSignIn(unittest.TestCase):
    def test_email_then_code_sets_a_session_and_a_refresh_cookie(self):
        api, p = setup()
        status, _, body = p.handle("GET", "/portal", {"Host": "h"}, b"")
        self.assertIn('action="/portal/login/code"', body.decode())
        status, _, body = post(p, "/portal/login/code", {"email": "o@example.com"})
        self.assertEqual(status, 200)
        self.assertIn('value="o@example.com"', body.decode())
        status, headers, _ = post(p, "/portal/login/verify", {"email": "o@example.com", "code": "123456"})
        c = set_cookies(headers)
        self.assertEqual(status, 303)
        for name in (COOKIE, REFRESH_COOKIE):
            self.assertIn("Secure; HttpOnly; SameSite=Strict", c[name])
            self.assertIn("Path=/;", c[name])
        self.assertTrue(c[REFRESH_COOKIE].startswith(f"{REFRESH_COOKIE}=r1;"))

    def test_a_wrong_code_signs_nobody_in_and_an_unknown_address_looks_the_same(self):
        api, p = setup()
        status, headers, body = post(p, "/portal/login/verify", {"email": "o@example.com", "code": "000000"})
        self.assertEqual((status, set_cookies(headers)), (401, {}))
        self.assertIn("الرمز غير صحيح", body.decode())
        a = post(p, "/portal/login/code", {"email": "known@example.com"})[2].decode().replace("known", "X")
        b = post(p, "/portal/login/code", {"email": "other@example.com"})[2].decode().replace("other", "X")
        self.assertEqual(a, b)

    def test_rate_limited_and_unavailable_are_said_plainly(self):
        self.assertEqual(post(setup(FakeGoTrue(429))[1], "/portal/login/code", {"email": "o@example.com"})[0], 429)
        self.assertEqual(post(setup(FakeGoTrue(503))[1], "/portal/login/code", {"email": "o@example.com"})[0], 503)
        status, _, body = post(setup(FakeGoTrue(422))[1], "/portal/login/code", {"email": "nobody@example.com"})
        self.assertEqual(status, 200)                                      # "no such user" is not told to the browser
        self.assertIn('action="/portal/login/verify"', body.decode())


class TestSessionRenewal(unittest.TestCase):
    def jar(self, api, ttl, refresh="r1"):
        return f"{COOKIE}={api.tok(ttl=ttl)}; {REFRESH_COOKIE}={refresh}"

    def test_a_session_close_to_expiry_is_renewed_and_the_refresh_token_rotates(self):
        api, p = setup()
        status, headers, _ = p.handle("GET", "/portal", {"Host": "h", "Cookie": self.jar(api, 60)}, b"")
        self.assertEqual(status, 200)
        self.assertTrue(set_cookies(headers)[REFRESH_COOKIE].startswith(f"{REFRESH_COOKIE}=r2;"))
        api.calls.clear()
        p.handle("GET", "/portal", {"Host": "h", "Cookie": self.jar(api, 3600)}, b"")
        self.assertEqual(api.calls, [])                                    # far from expiry: no call to Supabase

    def test_an_expired_session_is_renewed_and_a_failed_renewal_signs_out(self):
        api, p = setup()
        status, headers, body = p.handle("GET", "/portal", {"Host": "h", "Cookie": self.jar(api, -600)}, b"")
        self.assertEqual(status, 200)
        self.assertIn(COOKIE, set_cookies(headers))
        self.assertIn("بوابة المالك", body.decode())
        status, headers, body = p.handle("GET", "/portal", {"Host": "h", "Cookie": self.jar(api, -600, "stolen")}, b"")
        self.assertIn('action="/portal/login/code"', body.decode())
        self.assertTrue(all(v.endswith("Max-Age=0") for v in set_cookies(headers).values()))

    def test_a_form_made_before_the_renewal_still_counts_and_logout_clears_both(self):
        api, p = setup()
        old = api.tok(ttl=60)
        jar = f"{COOKIE}={old}; {REFRESH_COOKIE}=r1"
        status, headers, _ = post(p, "/portal/decide", {"approval": "a1", "decision": "approved", "csrf": csrf_token(old, SECRET)}, jar)
        self.assertEqual((status, dict(headers)["Location"]), (303, "/portal?done=approved"))
        self.assertIn(REFRESH_COOKIE, set_cookies(headers))
        fresh = api.tok()
        status, headers, _ = post(p, "/portal/logout", {"csrf": csrf_token(fresh, SECRET)}, f"{COOKIE}={fresh}; {REFRESH_COOKIE}=r2")
        self.assertTrue(all(v.endswith("Max-Age=0") for v in set_cookies(headers).values()))
        self.assertEqual(len(set_cookies(headers)), 2)
        self.assertEqual(api.calls[-1][1], "/auth/v1/logout?scope=local")


class TestOperatorSecondFactor(unittest.TestCase):
    def test_enrol_once_then_a_code_gives_an_aal2_session_for_the_console(self):
        store = FakeStore()
        store.operator = False                                             # aal1: the database says not an operator yet
        api, p = setup(store=store)
        aal1 = api.tok(OPERATOR)
        jar = f"{COOKIE}={aal1}"
        status, headers, _ = p.handle("GET", "/portal/ops", {"Host": "h", "Cookie": jar}, b"")
        self.assertEqual((status, dict(headers)["Location"]), (303, "/portal/mfa"))
        body = p.handle("GET", "/portal/mfa", {"Host": "h", "Cookie": jar}, b"")[2].decode()
        self.assertIn('action="/portal/mfa/enroll"', body)
        status, _, body = post(p, "/portal/mfa/enroll", {"csrf": csrf_token(aal1, SECRET)}, jar)
        self.assertIn("JBSWY3DP&lt;b&gt;EHPK3PXP", body.decode())          # the secret, escaped, shown once
        self.assertEqual(post(p, "/portal/mfa/enroll", {"csrf": csrf_token(aal1, SECRET)}, jar)[0], 303)   # no second factor
        self.assertEqual(sum(1 for c in api.calls if c[1] == "/auth/v1/factors"), 1)
        status, headers, _ = post(p, "/portal/mfa/verify", {"factor": FACTOR, "code": "111111", "csrf": csrf_token(aal1, SECRET)}, jar)
        self.assertEqual(dict(headers)["Location"], "/portal/mfa?done=mfa")
        status, headers, _ = post(p, "/portal/mfa/verify", {"factor": FACTOR, "code": "654321", "csrf": csrf_token(aal1, SECRET)}, jar)
        self.assertEqual(dict(headers)["Location"], "/portal/ops")
        self.assertTrue(set_cookies(headers)[REFRESH_COOKIE].startswith(f"{REFRESH_COOKIE}=r-aal2;"))

    def test_without_supabase_configured_nothing_new_is_reachable(self):
        p = Portal(FakeStore(), SECRET, now=lambda: NOW)
        self.assertNotIn("/portal/login/code", p.handle("GET", "/portal", {"Host": "h"}, b"")[2].decode())
        self.assertEqual(post(p, "/portal/login/code", {"email": "o@example.com"})[0], 303)     # no session: back to /portal
        self.assertEqual(p.handle("GET", "/portal/mfa", {"Host": "h"}, b"")[0], 303)


class TestConfiguration(unittest.TestCase):
    def test_sign_in_is_off_unset_on_with_all_three_and_refuses_half(self):
        from service.app import supabase_from_env
        self.assertIsNone(supabase_from_env({}))
        auth = supabase_from_env({"HERMES_SUPABASE_URL": URL, "HERMES_SUPABASE_ANON_KEY": KEY, "HERMES_JWT_SECRET": SECRET})
        self.assertEqual(auth.api.hosts, frozenset({"abcd1234.supabase.co"}))     # that project host and nothing else
        for env in ({"HERMES_SUPABASE_URL": URL}, {"HERMES_SUPABASE_URL": URL, "HERMES_SUPABASE_ANON_KEY": KEY}):
            with self.assertRaises(SystemExit):
                supabase_from_env(env)


if __name__ == "__main__":
    unittest.main()
