"""The owner portal: pending reply proposals (approve / reject), recent inquiries, knowledge facts to approve.

    Portal(store, secret, ...).handle(method, path, headers, body) -> (status, headers, body)

Without it nothing reaches a customer: every reply waits for the owner's decision (C1), and the owner reads
escalations here (a notice carries a reference, never the message text).

Identity: a Supabase Auth access token (service/auth.py verifies it) kept in a __Host- cookie (Secure, HttpOnly,
SameSite=Strict, Path=/), and its refresh token in a second one. Every read and write runs in the owner's own database
session, so the policies decide; the database also refuses a decision not attributed to the session user
(DECIDER_MUST_BE_SESSION_USER). Sign-in is server side (service/supabase_auth.py), since these pages run no script:
  GET  /portal                     the page, or the sign-in page (email -> a 6-digit code)
  POST /portal/login/code          email=<address>                  -> the code page (same answer for any address)
  POST /portal/login/verify        email=<address> code=<6 digits>  -> both cookies
  GET  /portal/mfa                 operator second factor: enrol a TOTP app once, then a code per session (aal2)
  POST /portal/mfa/enroll          csrf=<token>                     -> the secret to type into the app
  POST /portal/mfa/verify          factor=<id> code=<6 digits> csrf=<token> -> an aal2 session
  POST /portal/session             access_token=<Supabase token>   -> cookie (a client that signed in elsewhere)
An access token within five minutes of expiry, or expired, is replaced with the refresh token on the next request
(Supabase rotates it); a refresh that fails signs the user out.
  POST /portal/staging-login       code=<HERMES_STAGING_LOGIN_CODE> -> a session for HERMES_STAGING_OWNER_ID;
                                   refused unless HERMES_GRAPH=simulate and both are set
  POST /portal/decide              approval=<id> decision=approved|rejected csrf=<token>
  POST /portal/facts/approve       fact=<id> csrf=<token>
  POST /portal/logout
  GET  /portal/ops                 operator console: outbox rows waiting for a human, retention, unrouted events
  POST /portal/ops/resolve         outbox=<id> resolution=confirmed_sent|resend|abandon reason=<text> csrf=<token>
The console shows and does only what app.is_operator() allows: an active operator row AND an aal2 session (a
completed second factor). The database decides both; the console asks it and says no otherwise.
Every POST: a form body under 4 KiB, a same-origin Origin header when one is sent, and (except sign-in) the
session's CSRF token. No script runs on these pages (Content-Security-Policy: script-src 'none'); every value
is written through service.render, whose templates are checked at render time (claim A24).
"""
from __future__ import annotations
import hmac, logging, time
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from service.auth import AuthError, csrf_token, issue_staging_token, verify_session_token
from service.supabase_auth import AuthFailed
from service.render import render

log = logging.getLogger("hermes.portal")
COOKIE = "__Host-hermes_owner"
REFRESH_COOKIE = "__Host-hermes_refresh"
REFRESH_MAX_AGE = 14 * 86400
RENEW_BEFORE_SECONDS = 300
MAX_FORM_BYTES = 4096
SECURITY_HEADERS = [
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'"),
    ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"), ("Cache-Control", "no-store"),
]
CATEGORY_AR = {"pricing": "الأسعار", "quality": "الجودة", "legal": "قانوني", "safety": "السلامة", "refund": "استرداد"}


class StorePort(Protocol):
    def overview(self, claims: dict) -> dict: ...
    def decide(self, claims: dict, approval_id: str, decision: str) -> bool: ...
    def approve_fact(self, claims: dict, fact_id: str) -> bool: ...
    def ops_overview(self, claims: dict) -> dict | None: ...                 # None: not an operator in this session
    def resolve(self, claims: dict, outbox_id: str, resolution: str, reason: str) -> None: ...


HEAD = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>هيرمس · بوابة المالك</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;margin:0;background:#f6f7f9;color:#1b1f24;line-height:1.6}
main{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:1.3rem;margin:8px 0 4px}h2{font-size:1.05rem;margin:24px 0 8px;border-bottom:1px solid #d6dae0;padding-bottom:4px}
.card{background:#fff;border:1px solid #d6dae0;border-radius:8px;padding:12px;margin:8px 0}
.meta{color:#5b6470;font-size:.85rem}.body{white-space:pre-wrap;margin:6px 0}
form{display:inline}button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #9aa3ad;background:#fff;cursor:pointer;margin-inline-end:6px}
button.ok{background:#1f6f3f;color:#fff;border-color:#1f6f3f}button.no{color:#8a1f1f;border-color:#c9a3a3}
.note{background:#eef4ff;border:1px solid #c9d8f5;border-radius:8px;padding:8px 12px;margin:8px 0}
input[type=password],input[type=text]{font:inherit;padding:6px;border:1px solid #9aa3ad;border-radius:6px;width:100%;max-width:320px;box-sizing:border-box}
@media (prefers-color-scheme:dark){body{background:#15181c;color:#e6e8eb}.card{background:#1d2126;border-color:#343a42}
.meta{color:#9aa3ad}button{background:#1d2126;color:#e6e8eb}.note{background:#1b2533;border-color:#2c3e57}}
</style></head><body><main>"""
TAIL = "</main></body></html>"
TOP = """<h1>بوابة المالك</h1><p class="meta">{{names}}</p>
<form method="post" action="/portal/logout"><input type="hidden" name="csrf" value="{{csrf}}"><button>خروج</button></form>"""
FLASH = """<p class="note">{{message}}</p>"""
APPROVALS_H = """<h2>ردود تنتظر موافقتك ({{count}})</h2>"""
APPROVAL = """<div class="card"><div class="meta">إلى {{to}} · تنتهي صلاحيته {{expires}}</div><div class="body">{{body}}</div>
<form method="post" action="/portal/decide"><input type="hidden" name="approval" value="{{id}}">
<input type="hidden" name="decision" value="approved"><input type="hidden" name="csrf" value="{{csrf}}"><button class="ok">موافقة وإرسال</button></form>
<form method="post" action="/portal/decide"><input type="hidden" name="approval" value="{{id}}">
<input type="hidden" name="decision" value="rejected"><input type="hidden" name="csrf" value="{{csrf}}"><button class="no">رفض</button></form></div>"""
NONE = """<p class="meta">{{text}}</p>"""
INQUIRIES_H = """<h2>رسائل الأيام السبعة الأخيرة ({{count}})</h2>"""
INQUIRY = """<div class="card"><div class="meta">{{when}} · {{category}}{{flag}}</div><div class="body">{{body}}</div></div>"""
COMPETITORS_H = """<h2>منافسوك ({{count}})</h2><p class="meta">فحص أسبوعي لما ينشره موقع المنافس من قائمة وأسعار وساعات وتقييم.</p>"""
COMPETITOR = """<div class="card"><div class="meta">{{label}} · {{when}} · {{state}}</div><div class="body">{{summary}}</div></div>"""
SNAPSHOT_STATE = {"ok": "تمت المتابعة", "unverifiable": "لا متابعة آلية", "blocked": "الموقع يمنع الفحص", None: "لم يُفحص بعد"}
FACTS_H = """<h2>معلومات منشأتك ({{count}})</h2><p class="meta">لا يُرد على عميل إلا بمعلومة اعتمدتها أنت.</p>"""
FACT = """<div class="card"><div class="meta">{{topic}} · {{state}}</div><div class="body">{{fact}}</div></div>"""
FACT_PENDING = """<div class="card"><div class="meta">{{topic}} · تنتظر اعتمادك</div><div class="body">{{fact}}</div>
<form method="post" action="/portal/facts/approve"><input type="hidden" name="fact" value="{{id}}">
<input type="hidden" name="csrf" value="{{csrf}}"><button class="ok">اعتماد</button></form></div>"""
SIGN_IN = """<h1>بوابة المالك</h1><p>سجّل الدخول لترى الردود التي تنتظر موافقتك.</p><p class="meta">{{how}}</p>"""
EMAIL_FORM = """<div class="card"><form method="post" action="/portal/login/code">
<input type="text" name="email" inputmode="email" autocomplete="email" aria-label="البريد الإلكتروني" placeholder="البريد الإلكتروني" required>
<button class="ok">أرسل رمز الدخول</button></form></div>"""
CODE_FORM = """<h1>رمز الدخول</h1><p>إن كان {{email}} مسجلًا لدينا فقد أرسلنا إليه رمزًا من 6 أرقام. صلاحيته دقائق.</p>
<div class="card"><form method="post" action="/portal/login/verify"><input type="hidden" name="email" value="{{email}}">
<input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" aria-label="الرمز" placeholder="الرمز" required>
<button class="ok">دخول</button></form></div><p class="meta"><a href="/portal">بريد آخر</a></p>"""
MFA_CODE = """<h1>التحقق الثنائي</h1><p>أدخل الرمز الذي يعرضه تطبيق المصادقة الآن.</p>
<div class="card"><form method="post" action="/portal/mfa/verify"><input type="hidden" name="factor" value="{{factor}}">
<input type="hidden" name="csrf" value="{{csrf}}"><input type="text" name="code" inputmode="numeric" autocomplete="one-time-code"
aria-label="الرمز" placeholder="الرمز" required><button class="ok">تحقق</button></form></div>"""
MFA_ENROLL = """<h1>التحقق الثنائي</h1><p>لوحة المشغّل تتطلب تطبيق مصادقة (TOTP). يُضاف مرة واحدة.</p>
<div class="card"><form method="post" action="/portal/mfa/enroll"><input type="hidden" name="csrf" value="{{csrf}}">
<button class="ok">إضافة تطبيق مصادقة</button></form></div>"""
MFA_SECRET = """<h1>أضف المفتاح إلى تطبيق المصادقة</h1><p>اختر «إدخال مفتاح يدويًا» في التطبيق واكتب:</p>
<div class="card"><div class="body" dir="ltr">{{secret}}</div><p class="meta">لن يُعرض هذا المفتاح مرة أخرى.</p></div>"""
STAGING_LOGIN = """<div class="card"><p class="meta">بيئة التجربة (staging): دخول برمز التجربة، لا يعمل في الإنتاج.</p>
<form method="post" action="/portal/staging-login"><input type="password" name="code" aria-label="رمز التجربة" autocomplete="off">
<button class="ok">دخول</button></form></div>"""

OPS_TOP = """<h1>لوحة المشغّل</h1><p class="meta">جلسة مشغّل بتحقق ثنائي · <a href="/portal">بوابة المالك</a></p>
<form method="post" action="/portal/logout"><input type="hidden" name="csrf" value="{{csrf}}"><button>خروج</button></form>"""
OPS_HEALTH = """<h2>الحالة</h2><div class="card"><div>آخر محو ناجح: {{retention}}</div><div>نصوص تجاوزت 31 يومًا: {{overdue}}</div>
<div>أحداث موقّعة لا تتبع أي قناة: {{unrouted}}</div>
<div>المنافسون النشطون: {{competitors}} · ببيانات منظمة {{structured}} · بلا بيانات منظمة {{unstructured}} · يمنعون الفحص {{blocked}}</div></div>"""
OPS_SOURCES_H = """<h2>المصادر الخارجية</h2><p class="meta">لكل مصدر: ما نجح وما فشل وما ينتظر إنسانًا (رسائل واتساب
وردودها خلال 24 ساعة، ومواقع المنافسين خلال 7 أيام). أرقام فقط.</p>"""
OPS_SOURCE = """<div class="card"><div>{{name}} · {{verdict}}</div><div class="meta">نجح {{ok}} · فشل {{failed}} · ينتظر إنسانًا
{{waiting}} · آخر نجاح {{last}}{{cause}}</div></div>"""
SOURCE_AR = {"whatsapp_inbound": "رسائل واتساب الواردة", "reply.send": "الردود عبر Graph",
             "notify.owner": "تنبيهات المالك", "competitor_sites": "مواقع المنافسين"}
OPS_ROWS_H = """<h2>صفوف الصندوق الصادر التي تحتاج إنسانًا ({{count}})</h2>
<p class="meta">«تحتاج قرارًا»: الإرسال غامض، لا يُعاد آليًا أبدًا. «ينتظر العامل»: انتهى حجز الإرسال بلا نتيجة، وسيعلّمه العامل
عند استعادة المهمة. قبل «أُرسل فعلًا» تحقق من المزوّد؛ «أعد الإرسال» يُرسل مرة أخرى وقد يصل مرتين إن كان الأول قد وصل.</p>"""
OPS_ROW = """<div class="card"><div class="meta">#{{id}} · {{customer}} · {{topic}} · محاولات {{attempts}} · {{state}}</div>
<div class="body">{{error}}</div></div>"""
OPS_ROW_DECIDE = """<div class="card"><div class="meta">#{{id}} · {{customer}} · {{topic}} · محاولات {{attempts}} · تحتاج قرارًا</div>
<div class="body">{{error}}</div>
<form method="post" action="/portal/ops/resolve"><input type="hidden" name="outbox" value="{{id}}">
<input type="hidden" name="csrf" value="{{csrf}}"><input type="text" name="reason" aria-label="السبب" placeholder="السبب (إلزامي)" required minlength="5">
<button name="resolution" value="confirmed_sent" class="ok">أُرسل فعلًا</button><button name="resolution" value="resend">أعد الإرسال</button>
<button name="resolution" value="abandon" class="no">تخلَّ عنه</button></form></div>"""
STAGING_OPERATOR = """<div class="card"><p class="meta">مشغّل بيئة التجربة (staging): الرمز نفسه، وجلسة بتحقق ثنائي مُفترض.</p>
<form method="post" action="/portal/staging-login"><input type="hidden" name="as" value="operator">
<input type="password" name="code" aria-label="رمز التجربة" autocomplete="off"><button>دخول كمشغّل</button></form></div>"""

AUTH_FAILED = {"invalid": "الرمز غير صحيح أو انتهت صلاحيته.", "rate_limited": "محاولات كثيرة. انتظر دقيقة ثم أعد المحاولة.",
               "unavailable": "خدمة الدخول لا تستجيب الآن. أعد المحاولة بعد قليل."}
MESSAGES = {"approved": "تمت الموافقة. يُرسل الرد خلال دقيقة.", "rejected": "رُفض المقترح ولن يُرسل.",
            "fact": "اعتُمدت المعلومة.", "resolved": "سُجّل القرار. «أعد الإرسال» يُرسل خلال دقيقة.",
            "reason": "لم يُنفّذ: السبب إلزامي (خمسة أحرف على الأقل).", "mfa": "لم يُقبل الرمز. أعد المحاولة.", "gone": "لم يُنفّذ: المقترح لم يعد معلقًا أو ليس لك.", "error": "تعذّر التنفيذ. أعد المحاولة."}


def _r(template: str, **fields) -> str:
    html, refused = render(template, fields)
    if refused:                                            # no url placeholders here; a refusal means a bug
        raise ValueError(f"refused fields {refused}")
    return html


def _mask(phone) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return "•••" + digits[-3:] if digits else "—"


def _when(ts) -> str:
    return ts.strftime("%Y-%m-%d %H:%M") + " UTC" if hasattr(ts, "strftime") else "—"


class Portal:
    def __init__(self, store: StorePort, secret: str, *, staging_login_code: str = "", staging_owner_id: str = "",
                 staging_operator_id: str = "", simulate: bool = False, now=time.time, auth=None):
        self.store, self.secret, self.now, self.auth = store, secret, now, auth
        self.staging = bool(simulate and staging_login_code and staging_owner_id and secret)
        self.staging_code, self.staging_owner = staging_login_code, staging_owner_id
        self.staging_operator = staging_operator_id if self.staging else ""

    # ------------------------------------------------------------ plumbing
    def _page(self, status: int, inner: str, extra: list | None = None):
        return status, [("Content-Type", "text/html; charset=utf-8"), *SECURITY_HEADERS, *(extra or [])], \
            (HEAD + inner + TAIL).encode("utf-8")

    def _redirect(self, where: str, extra: list | None = None):
        return 303, [("Location", where), *SECURITY_HEADERS, *(extra or [])], b""

    @staticmethod
    def _cookie_value(headers: dict, name: str) -> str:
        raw = headers.get("cookie", "")
        return next((p.split("=", 1)[1].strip() for p in raw.split(";") if p.strip().startswith(name + "=")), "")

    def _session(self, headers: dict):
        """(token, claims, cookies to set). An expiring or expired access token is renewed with the refresh token."""
        token, refresh = self._cookie_value(headers, COOKIE), self._cookie_value(headers, REFRESH_COOKIE)
        claims = None
        if token:
            try:
                claims = verify_session_token(token, self.secret, self.now())
            except AuthError as exc:
                log.info("portal session refused: %s", exc)
        if claims is not None and claims["exp"] - self.now() > RENEW_BEFORE_SECONDS:
            return token, claims, []
        if self.auth is None or not refresh:
            return (token, claims, []) if claims is not None else (None, None, [])
        try:
            new = self.auth.refresh(refresh)
            new_claims = verify_session_token(new.access_token, self.secret, self.now())
        except (AuthFailed, AuthError) as exc:
            log.info("portal session renewal refused: %s", exc)
            if claims is not None:                         # still valid for a few minutes: keep it, try again next time
                return token, claims, []
            return None, None, self._clear()
        return new.access_token, new_claims, self._cookies(new.access_token, new_claims, new.refresh_token)

    def _cookie(self, token: str, claims: dict):
        age = max(0, min(3600, int(claims["exp"] - self.now())))
        return ("Set-Cookie", f"{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={age}")

    def _cookies(self, token: str, claims: dict, refresh: str | None = None) -> list:
        out = [self._cookie(token, claims)]
        if refresh and len(refresh) <= 512 and refresh.isprintable() and ";" not in refresh and " " not in refresh:
            out.append(("Set-Cookie", f"{REFRESH_COOKIE}={refresh}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={REFRESH_MAX_AGE}"))
        return out

    @staticmethod
    def _clear():
        return [("Set-Cookie", f"{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"),
                ("Set-Cookie", f"{REFRESH_COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0")]

    @staticmethod
    def _with(result, cookies: list):
        """Add the renewed cookies unless the response sets the same cookie itself (sign-out, a new aal2 session)."""
        status, headers, body = result
        own = {v.split("=", 1)[0] for k, v in headers if k == "Set-Cookie"}
        return status, [*headers, *(c for c in cookies if c[1].split("=", 1)[0] not in own)], body

    @staticmethod
    def _same_origin(headers: dict) -> bool:
        origin = headers.get("origin")
        if not origin:                                     # older clients send none: SameSite + CSRF token still hold
            return True
        host = headers.get("host", "")
        o = urlsplit(origin)
        return o.scheme in ("https", "http") and o.netloc == host

    # ------------------------------------------------------------ routes
    def handle(self, method: str, path: str, headers: dict, body: bytes):
        headers = {k.lower(): v for k, v in headers.items()}
        route = urlsplit(path).path.rstrip("/") or "/"
        done = parse_qs(urlsplit(path).query).get("done", [""])[0]
        if method == "GET" and route in ("/portal", "/portal/ops", "/portal/mfa"):
            token, claims, renew = self._session(headers)
            page = {"/portal": self._home, "/portal/ops": self._ops, "/portal/mfa": self._mfa}[route]
            return self._with(page(token, claims, done), renew)
        if method != "POST" or not route.startswith("/portal/"):
            return self._page(404, _r(NONE, text="غير موجود"))
        if len(body) > MAX_FORM_BYTES or not self._same_origin(headers):
            return self._page(403, _r(NONE, text="طلب مرفوض"))
        try:
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace"), max_num_fields=8).items()}
        except ValueError:                                 # more fields than any portal form has
            return self._page(400, _r(NONE, text="طلب غير صالح"))
        if route == "/portal/session":
            return self._sign_in(form.get("access_token", ""))
        if route == "/portal/staging-login":
            return self._staging_login(form.get("code", ""), form.get("as") == "operator")
        if route == "/portal/login/code" and self.auth is not None:
            return self._login_code(form.get("email", "").strip())
        if route == "/portal/login/verify" and self.auth is not None:
            return self._login_verify(form.get("email", "").strip(), form.get("code", "").strip())
        token, claims, renew = self._session(headers)
        if claims is None:
            return self._redirect("/portal", self._clear())
        old = self._cookie_value(headers, COOKIE)          # a form made before a renewal carries the old token's CSRF
        if not any(t and hmac.compare_digest(form.get("csrf", ""), csrf_token(t, self.secret))
                   for t in ((token, old) if renew else (token,))):
            return self._page(403, _r(NONE, text="انتهت صلاحية الصفحة. افتح البوابة من جديد."))
        return self._with(self._act(route, form, token, claims), renew)

    def _act(self, route: str, form: dict, token: str, claims: dict):
        if route == "/portal/logout":
            if self.auth is not None:
                self.auth.sign_out(token)
            return self._redirect("/portal", self._clear())
        if route == "/portal/mfa/enroll" and self.auth is not None:
            return self._mfa_enroll(token, claims)
        if route == "/portal/mfa/verify" and self.auth is not None:
            return self._mfa_verify(token, form.get("factor", ""), form.get("code", "").strip())
        try:
            if route == "/portal/decide" and form.get("decision") in ("approved", "rejected"):
                ok = self.store.decide(claims, form.get("approval", ""), form["decision"])
                return self._redirect("/portal?done=" + (form["decision"] if ok else "gone"))
            if route == "/portal/facts/approve":
                ok = self.store.approve_fact(claims, form.get("fact", ""))
                return self._redirect("/portal?done=" + ("fact" if ok else "gone"))
            if route == "/portal/ops/resolve" and form.get("resolution") in ("confirmed_sent", "resend", "abandon"):
                reason = form.get("reason", "").strip()
                if len(reason) < 5:
                    return self._redirect("/portal/ops?done=reason")
                self.store.resolve(claims, form.get("outbox", ""), form["resolution"], reason[:180])
                return self._redirect("/portal/ops?done=resolved")
        except Exception as exc:                           # noqa: BLE001 - a guard refused, or a bad id: say so, no detail
            log.warning("portal action refused: %s", type(exc).__name__)
            return self._redirect(("/portal/ops" if route.startswith("/portal/ops/") else "/portal") + "?done=error")
        return self._page(400, _r(NONE, text="طلب غير صالح"))

    def _sign_in(self, access_token: str):
        try:
            claims = verify_session_token(access_token, self.secret, self.now())
        except AuthError as exc:
            log.info("portal sign-in refused: %s", exc)
            return self._page(401, _r(NONE, text="تعذّر تسجيل الدخول."))
        return self._redirect("/portal", [self._cookie(access_token, claims)])

    def _login_code(self, email: str):
        try:
            self.auth.send_code(email)
        except AuthFailed as exc:
            if exc.code != "invalid":                      # an unknown address is not told apart from a known one
                return self._page(503 if exc.code == "unavailable" else 429,
                                  _r(SIGN_IN, how=AUTH_FAILED[exc.code]) + EMAIL_FORM)
            if not email or "@" not in email:
                return self._page(400, _r(SIGN_IN, how="اكتب بريدًا إلكترونيًا صحيحًا.") + EMAIL_FORM)
        return self._page(200, _r(CODE_FORM, email=email[:254]))

    def _login_verify(self, email: str, code: str):
        try:
            session = self.auth.verify_code(email, code)
            claims = verify_session_token(session.access_token, self.secret, self.now())
        except (AuthFailed, AuthError) as exc:
            log.info("portal sign-in refused: %s", exc)
            message = AUTH_FAILED.get(getattr(exc, "code", ""), AUTH_FAILED["invalid"])
            return self._page(401, _r(FLASH, message=message) + _r(CODE_FORM, email=email[:254]))
        return self._redirect("/portal", self._cookies(session.access_token, claims, session.refresh_token))

    def _mfa(self, token, claims, done: str):
        if claims is None:
            return self._redirect("/portal")
        if self.auth is None:
            return self._page(404, _r(NONE, text="غير موجود"))
        if claims.get("aal") == "aal2":
            return self._redirect("/portal/ops")
        csrf = csrf_token(token, self.secret)
        flash = _r(FLASH, message=MESSAGES["mfa"]) if done == "mfa" else ""
        try:
            factor = self.auth.mfa_factor(token)
        except AuthFailed as exc:
            return self._page(503, _r(NONE, text=AUTH_FAILED[exc.code if exc.code in AUTH_FAILED else "unavailable"]))
        if factor is None:
            return self._page(200, flash + _r(MFA_ENROLL, csrf=csrf))
        return self._page(200, flash + _r(MFA_CODE, factor=factor["id"], csrf=csrf))

    def _mfa_enroll(self, token, claims):
        try:
            if self.auth.mfa_factor(token) is not None:    # one factor per operator; a second enrolment is refused
                return self._redirect("/portal/mfa")
            factor = self.auth.mfa_enroll(token)
        except AuthFailed as exc:
            return self._page(503, _r(NONE, text=AUTH_FAILED[exc.code if exc.code in AUTH_FAILED else "unavailable"]))
        return self._page(200, _r(MFA_SECRET, secret=factor["secret"]) + _r(MFA_CODE, factor=factor["id"],
                                                                         csrf=csrf_token(token, self.secret)))

    def _mfa_verify(self, token, factor: str, code: str):
        try:
            session = self.auth.mfa_verify(token, factor, code)
            claims = verify_session_token(session.access_token, self.secret, self.now())
        except (AuthFailed, AuthError) as exc:
            log.info("portal second factor refused: %s", exc)
            return self._redirect("/portal/mfa?done=mfa")
        return self._redirect("/portal/ops", self._cookies(session.access_token, claims, session.refresh_token))

    def _staging_login(self, code: str, as_operator: bool = False):
        if not self.staging or not hmac.compare_digest(code.encode(), self.staging_code.encode()) \
           or (as_operator and not self.staging_operator):
            log.info("portal staging login refused")
            return self._page(403, _r(NONE, text="غير مسموح"))
        who, aal, where = (self.staging_operator, "aal2", "/portal/ops") if as_operator else (self.staging_owner, "aal1", "/portal")
        token = issue_staging_token(who, self.secret, now=self.now(), aal=aal)
        log.info("portal staging login as %s", "operator" if as_operator else "owner")
        return self._redirect(where, [self._cookie(token, verify_session_token(token, self.secret, self.now()))])

    def _ops(self, token, claims, done: str):
        if claims is None:
            return self._redirect("/portal")
        try:
            data = self.store.ops_overview(claims)
        except Exception as exc:                           # noqa: BLE001
            log.error("ops overview failed: %s", type(exc).__name__)
            return self._page(503, _r(NONE, text="تعذّر تحميل البيانات الآن. أعد المحاولة بعد قليل."))
        if data is None:                                   # the database says: not an operator in this session
            if self.auth is not None and claims.get("aal") != "aal2":
                return self._redirect("/portal/mfa")
            return self._page(403, _r(NONE, text="هذه الصفحة للمشغّلين بجلسة تحقق ثنائي فقط."))
        csrf = csrf_token(token, self.secret)
        out = [_r(OPS_TOP, csrf=csrf)]
        if done in MESSAGES:
            out.append(_r(FLASH, message=MESSAGES[done]))
        comp = data.get("competitors") or {"active": 0, "structured": 0, "unstructured": 0, "blocked": 0}
        out.append(_r(OPS_HEALTH, retention=_when(data["retention_last_run"]), overdue=data["overdue_bodies"],
                      competitors=comp["active"], structured=comp["structured"], unstructured=comp["unstructured"],
                      blocked=comp["blocked"],
                      unrouted=data["unrouted"]))
        if data.get("sources"):
            out.append(OPS_SOURCES_H)
            for src in data["sources"]:
                verdict = ("يحتاج نظرًا" if src["waiting"] or (src["failed"] and src["source"] != "competitor_sites")
                           else "لا نشاط" if not src["ok"] and not src["failed"] else "سليم")
                out.append(_r(OPS_SOURCE, name=SOURCE_AR.get(src["source"], src["source"]), verdict=verdict, ok=src["ok"],
                              failed=src["failed"], waiting=src["waiting"], last=_when(src["last"]),
                              cause=f" · السبب الأكثر: {src['cause']}" if src.get("cause") else ""))
        out.append(_r(OPS_ROWS_H, count=len(data["rows"])))
        for r in data["rows"]:
            fields = dict(id=r["id"], customer=r["customer"] or "المنصة", topic=r["topic"], attempts=r["attempts"],
                          error=r["last_error"] or "")
            if r["needs_human_check"]:
                out.append(_r(OPS_ROW_DECIDE, csrf=csrf, **fields))
            else:
                out.append(_r(OPS_ROW, state="ينتظر العامل", **fields))
        if not data["rows"]:
            out.append(_r(NONE, text="لا شيء ينتظر قرارًا."))
        return self._page(200, "".join(out))

    def _home(self, token, claims, done: str):
        if claims is None:
            how = ("الدخول برمز يصل إلى بريدك." if self.auth is not None else
                   "بيئة تجربة." if self.staging else "الدخول غير مهيأ بعد (HERMES_SUPABASE_URL).")
            return self._page(200, _r(SIGN_IN, how=how) + (EMAIL_FORM if self.auth is not None else "")
                              + (STAGING_LOGIN if self.staging else "")
                              + (STAGING_OPERATOR if self.staging_operator else ""))
        try:
            data = self.store.overview(claims)
        except Exception as exc:                           # noqa: BLE001 - the database is away: say so, keep the session
            log.error("portal overview failed: %s", type(exc).__name__)
            return self._page(503, _r(NONE, text="تعذّر تحميل البيانات الآن. أعد المحاولة بعد قليل."))
        csrf = csrf_token(token, self.secret)
        out = [_r(TOP, names=" · ".join(c["name"] for c in data["customers"]) or "لا منشأة مرتبطة بحسابك", csrf=csrf)]
        if done in MESSAGES:
            out.append(_r(FLASH, message=MESSAGES[done]))
        out.append(_r(APPROVALS_H, count=len(data["approvals"])))
        for a in data["approvals"]:
            p = a["payload"] if isinstance(a["payload"], dict) else {}
            out.append(_r(APPROVAL, id=a["id"], to=_mask(p.get("to")), expires=_when(a["expires_at"]),
                          body=p.get("body", ""), csrf=csrf))
        if not data["approvals"]:
            out.append(_r(NONE, text="لا شيء ينتظر موافقتك."))
        out.append(_r(INQUIRIES_H, count=len(data["inquiries"])))
        for q in data["inquiries"]:
            out.append(_r(INQUIRY, when=_when(q["received_at"]), category=CATEGORY_AR.get(q["category"] or "", "بلا تصنيف"),
                          flag=" · سؤال لك" if q["owner_inquiry"] else "",
                          body=q["body"] if q["body"] is not None else "(حُذف النص بعد 30 يومًا)"))
        comps = data.get("competitors", [])
        if comps:
            out.append(_r(COMPETITORS_H, count=len(comps)))
            for c in comps:
                out.append(_r(COMPETITOR, label=c["label"], when=_when(c["fetched_at"]), state=SNAPSHOT_STATE.get(c["status"], "—"),
                              summary=c["summary"] or "يُفحص خلال الأيام القادمة."))
        out.append(_r(FACTS_H, count=len(data["facts"])))
        for f in data["facts"]:
            if f["approved"]:
                out.append(_r(FACT, topic=f["topic"], state="معتمدة", fact=f["fact"]))
            else:
                out.append(_r(FACT_PENDING, topic=f["topic"], fact=f["fact"], id=f["id"], csrf=csrf))
        return self._page(200, "".join(out))
