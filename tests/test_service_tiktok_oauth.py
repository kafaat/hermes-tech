"""Linking TikTok and keeping it alive (spec 28.30): the state is signed for this business and this same user; the code
and refresh exchanges are TikTok's form requests; a token is renewed before it expires, under the task's lease, and a
failed renewal is recorded and fails the send before anything is sent."""
import contextlib, os, sys, unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.dispatcher import CURRENT_BIND  # noqa: E402
from service.portal import Portal  # noqa: E402
from service.tiktok_oauth import (OAuthError, SimulatedTikTokOAuth, TikTokOAuth, TikTokTokens, Tokens,  # noqa: E402
                                  sign_state, verify_state)
from service.token_box import TokenBox, aad  # noqa: E402

SECRET = "s" * 40
CUST, SUB = "00000000-0000-0000-0000-00000000000a", "11111111-1111-1111-1111-111111111111"
REDIRECT = "https://hermes.example/portal/connect/tiktok/callback"
GOOD = {"access_token": "act.1", "refresh_token": "rft.1", "expires_in": 86400, "refresh_expires_in": 31536000,
        "open_id": "_000abcDEF", "scope": "user.info.basic,video.publish", "token_type": "Bearer"}


class Api:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def request(self, method, url, body=None, headers=None, form=None):
        self.calls.append((method, url, body, headers, form))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


class TestOAuth(unittest.TestCase):
    def test_consent_exchange_and_refresh_are_tiktoks_requests(self):
        api = Api((200, GOOD), (200, {**GOOD, "access_token": "act.2", "refresh_token": "rft.2"}))
        o = TikTokOAuth("ck", "cs", REDIRECT, api)
        q = parse_qs(urlsplit(o.authorize_url("st.ate")).query)
        self.assertEqual({k: v[0] for k, v in q.items()}, {"client_key": "ck", "scope": "user.info.basic,video.publish",
                                                           "response_type": "code", "redirect_uri": REDIRECT, "state": "st.ate"})
        t = o.exchange("code*!123456")
        self.assertEqual((t.open_id, t.access, t.refresh, t.access_expires_in), ("_000abcDEF", "act.1", "rft.1", 86400))
        o.refresh("rft.1")
        (m1, u1, b1, _, f1), (m2, u2, b2, _, f2) = api.calls
        self.assertEqual((m1, u1, b1), ("POST", "https://open.tiktokapis.com/v2/oauth/token/", None))
        self.assertEqual(f1, {"client_key": "ck", "client_secret": "cs", "code": "code*!123456", "grant_type": "authorization_code",
                              "redirect_uri": REDIRECT})
        self.assertEqual(f2, {"client_key": "ck", "client_secret": "cs", "grant_type": "refresh_token", "refresh_token": "rft.1"})

    def test_refusals_and_bad_answers(self):
        for answer in ((400, {"error": "invalid_grant", "error_description": "x"}), (200, {}), (200, {**GOOD, "open_id": "../x"}),
                       (200, {**GOOD, "expires_in": "soon"}), (500, {})):
            with self.subTest(answer), self.assertRaises(OAuthError):
                TikTokOAuth("ck", "cs", REDIRECT, Api(answer)).exchange("code12345678")
        for bad in (("", "cs", REDIRECT), ("ck", "cs", "http://hermes.example/portal/connect/tiktok/callback"),
                    ("ck", "cs", "https://hermes.example/elsewhere")):
            with self.subTest(bad), self.assertRaises(ValueError):
                TikTokOAuth(*bad, Api())

    def test_the_state_is_for_this_business_and_this_user_only(self):
        st = sign_state(SECRET, CUST, SUB, now=1000)
        self.assertEqual(verify_state(SECRET, st, SUB, now=1500), CUST)
        self.assertEqual(verify_state(SECRET, st, "22222222-2222-2222-2222-222222222222", now=1500), "")   # another user
        self.assertEqual(verify_state(SECRET, st, SUB, now=1000 + 601), "")                                # expired
        self.assertEqual(verify_state("x" * 40, st, SUB, now=1500), "")                                     # another key
        body, sig = st.split(".")
        self.assertEqual(verify_state(SECRET, body[:-2] + "AA." + sig, SUB, now=1500), "")                 # tampered
        self.assertEqual(verify_state(SECRET, "garbage", SUB, now=1500), "")
        bound = sign_state(SECRET, CUST, SUB, now=1000, nonce="abc")
        self.assertEqual(verify_state(SECRET, bound, SUB, now=1500, nonce="abc"), CUST)
        self.assertEqual(verify_state(SECRET, bound, SUB, now=1500, nonce="abd"), "")


class Db:
    """app.provider_tokens for one account, as TikTokTokens uses it; `fresh`/`renewable` stand for the expiry checks."""

    def __init__(self, box, fresh, renewable=True):
        self.row = {"access": box.seal("act.old", aad("tiktok", "_000abcDEF", "access")),
                    "refresh": box.seal("rft.old", aad("tiktok", "_000abcDEF", "refresh")), "failures": 0, "error": None}
        self.fresh, self.renewable, self.binds = fresh, renewable, []

    @contextlib.contextmanager
    def tx(self, bind=None, **k):
        self.binds.append(bind)
        yield self

    def execute(self, sql, args=()):
        self.last = sql
        if sql.startswith("update app.provider_tokens set failures"):
            self.row["failures"] += 1
            self.row["error"] = args[0]
        elif sql.startswith("update app.provider_tokens set access_ct"):
            self.row.update(access=args[0], refresh=args[1], failures=0, error=None)

    def fetchone(self):
        return (self.row["access"], self.row["refresh"], self.fresh, self.renewable) if self.last.startswith("select") else None


class TestRenewal(unittest.TestCase):
    def setUp(self):
        self.box = TokenBox([os.urandom(32)])
        self.mark = CURRENT_BIND.set(("task", "token"))

    def tearDown(self):
        CURRENT_BIND.reset(self.mark)

    def test_a_fresh_token_is_used_as_is_under_the_tasks_lease(self):
        db = Db(self.box, fresh=True)
        self.assertEqual(TikTokTokens(db, self.box, None)("_000abcDEF"), "act.old")
        self.assertEqual(db.binds, [("task", "token")])

    def test_an_expiring_token_is_renewed_and_both_new_tokens_are_sealed(self):
        db = Db(self.box, fresh=False)
        oauth = TikTokOAuth("ck", "cs", REDIRECT, Api((200, {**GOOD, "access_token": "act.new", "refresh_token": "rft.new"})))
        self.assertEqual(TikTokTokens(db, self.box, oauth)("_000abcDEF"), "act.new")
        self.assertEqual(oauth.api.calls[0][4]["refresh_token"], "rft.old")
        self.assertEqual(self.box.open(db.row["refresh"], aad("tiktok", "_000abcDEF", "refresh")), "rft.new")   # rotated
        self.assertEqual(self.box.open(db.row["access"], aad("tiktok", "_000abcDEF", "access")), "act.new")

    def test_a_failed_renewal_is_recorded_and_gives_no_token(self):
        for oauth, renewable in ((TikTokOAuth("ck", "cs", REDIRECT, Api((400, {"error": "invalid_grant"}))), True),
                                 (TikTokOAuth("ck", "cs", REDIRECT, Api(TimeoutError("slow"))), True),
                                 (TikTokOAuth("ck", "cs", REDIRECT, Api((200, {**GOOD, "open_id": "_000otherXY"}))), True),
                                 (None, False)):
            with self.subTest(oauth=oauth, renewable=renewable):
                db = Db(self.box, fresh=False, renewable=renewable)
                self.assertIsNone(TikTokTokens(db, self.box, oauth)("_000abcDEF"))
                self.assertEqual(db.row["failures"], 1)
                self.assertTrue(db.row["error"])
        db = Db(self.box, fresh=True)
        CURRENT_BIND.set(None)
        self.assertIsNone(TikTokTokens(db, self.box, None)("_000abcDEF"))           # no lease: no token, nothing read
        self.assertEqual(db.binds, [])


class TestRenewalDurability(unittest.TestCase):
    """The review of 2026-10-02: new tokens count only once committed; a failed commit is a clean failure, recorded."""

    def test_a_failed_commit_gives_no_token_and_is_recorded(self):
        box = TokenBox([os.urandom(32)])

        class Flaky(Db):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.calls = 0

            @contextlib.contextmanager
            def tx(self, bind=None, **k):
                self.calls += 1
                yield self
                if self.calls == 1:
                    raise OSError("connection lost at commit")
        db = Flaky(box, fresh=False)
        oauth = TikTokOAuth("ck", "cs", REDIRECT, Api((200, {**GOOD, "access_token": "act.new", "refresh_token": "rft.new"})))
        mark = CURRENT_BIND.set(("task", "token"))
        try:
            self.assertIsNone(TikTokTokens(db, box, oauth)("_000abcDEF"))
        finally:
            CURRENT_BIND.reset(mark)
        self.assertEqual((db.calls, db.row["failures"]), (2, 1))
        self.assertTrue(db.row["error"].startswith("store:"))


class Store:
    def __init__(self, fail=None):
        self.fail, self.linked = fail, []

    def link_tiktok(self, claims, customer, open_id, name, access_ct, refresh_ct, a_in, r_in):
        if self.fail:
            raise RuntimeError(self.fail)
        self.linked.append((claims["sub"], customer, open_id, name, access_ct, refresh_ct, a_in, r_in))
        return True


class TestPortalLink(unittest.TestCase):
    def finish(self, oauth, store, state=None, sub=SUB, nonce="n0nce"):
        box = TokenBox([os.urandom(32)])
        portal = Portal(store, SECRET, tiktok=oauth, box=box, now=lambda: 1000.0)
        claims = {"sub": sub, "exp": 5000}
        state = state or sign_state(SECRET, CUST, SUB, 1000, nonce="n0nce")
        return portal._tiktok_finish({"code": "SIMcode0123456789", "state": state}, claims, nonce), box

    def test_the_owner_links_and_only_sealed_tokens_reach_the_database(self):
        store = Store()
        (status, headers, _), box = self.finish(SimulatedTikTokOAuth(), store)
        self.assertEqual((status, dict(headers)["Location"]), (303, "/portal?done=tiktok_linked"))
        self.assertIn("__Host-hermes_tiktok=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0",
                      [v for k, v in headers if k == "Set-Cookie"])               # the state completes once
        (sub, customer, open_id, name, a_ct, r_ct, a_in, r_in), = store.linked
        self.assertEqual((sub, customer, a_in), (SUB, CUST, 86400))
        self.assertTrue(open_id.startswith("_sim"))
        self.assertEqual(box.open(a_ct, aad("tiktok", open_id, "access")), "sim-access-" + open_id[4:])
        self.assertNotIn(b"sim-", a_ct + r_ct)

    def test_refusals(self):
        class NoPublish(SimulatedTikTokOAuth):
            def exchange(self, code):
                return Tokens("_000abcDEF", "a", "r", 86400, 86400, "user.info.basic")
        cases = [((SimulatedTikTokOAuth(), Store(), sign_state(SECRET, CUST, SUB, 1000), "22222222-2222-2222-2222-222222222222"), "tiktok_failed"),
                 ((NoPublish(), Store(), None, SUB), "tiktok_scope"),
                 ((SimulatedTikTokOAuth(), Store(fail="LINK_ACCOUNT_TAKEN"), None, SUB), "tiktok_taken"),
                 ((SimulatedTikTokOAuth(), Store(fail="LINK_OWNER_ONLY"), None, SUB), "tiktok_failed"),
                 ((SimulatedTikTokOAuth(), Store(fail="LINK_CHANNEL_SUSPENDED"), None, SUB), "tiktok_suspended")]
        for (oauth, store, state, sub), done in cases:
            with self.subTest(done):
                (status, headers, _), _ = self.finish(oauth, store, state, sub)
                self.assertEqual(dict(headers)["Location"], f"/portal?done={done}")
                self.assertEqual(store.linked, [])
        for nonce in ("", "another-browser"):                     # no cookie, or a state started in another browser
            with self.subTest(nonce=nonce):
                store = Store()
                (status, headers, _), _ = self.finish(SimulatedTikTokOAuth(), store, nonce=nonce)
                self.assertEqual(dict(headers)["Location"], "/portal?done=tiktok_failed")
                self.assertEqual(store.linked, [])

    def test_the_callback_page_posts_back_from_this_site(self):
        portal = Portal(Store(), SECRET, tiktok=SimulatedTikTokOAuth(), box=TokenBox([os.urandom(32)]))
        status, _, page = portal.handle("GET", "/portal/connect/tiktok/callback?code=SIMcode0123456789&state=a.b", {}, b"")
        self.assertEqual(status, 200)
        self.assertIn('action="/portal/connect/tiktok/finish"', page.decode())
        status, headers, _ = portal.handle("GET", "/portal/connect/tiktok/callback?error=access_denied&state=a.b", {}, b"")
        self.assertEqual(dict(headers)["Location"], "/portal?done=tiktok_denied")


if __name__ == "__main__":
    unittest.main()
