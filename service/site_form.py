"""The contact form on a customer's site (spec 28.24): a visitor's message reaches the owner like a WhatsApp inquiry.

    POST /forms/<site key>     message=<text> name=<text> phone=<digits> website=<must stay empty>

A site we build is static: it cannot sign what it sends, so the form is protected otherwise. The site key in the URL
must belong to an active site_form channel (an unknown key stores nothing and gets 404); the body is small and form-
encoded; a hidden field that people never fill ("website") catches most bots, which get the same thank-you page and
store nothing; at most FORM_LIMIT messages per site and address in FORM_WINDOW seconds. The message is stored as an
event of the site_form channel; signature_valid there means "an active site key", the only proof a static form has.
The visitor gets no automatic reply: WhatsApp allows a business to start a conversation only with an approved
template, so the owner reads the message (name and number included) in the portal and answers the visitor.
"""
from __future__ import annotations
import re, threading, time, uuid
from urllib.parse import parse_qs

from service.render import render

FORM_MAX_BYTES = 8192
FORM_LIMIT, FORM_WINDOW = 5, 600
KEY = re.compile(r"^[A-Za-z0-9_-]{24,64}$")
PHONE = re.compile(r"^\+?[0-9][0-9 ]{5,19}$")
HEADERS = [("Content-Type", "text/html; charset=utf-8"), ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"),
           ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"), ("Cache-Control", "no-store")]
PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{title}}</title><style>body{font-family:system-ui,Tahoma,sans-serif;margin:0;padding:24px;line-height:1.7}</style></head>
<body><p>{{text}}</p></body></html>"""
FORM = """<form method="post" action="{{url:action}}" accept-charset="utf-8">
<label>الاسم <input type="text" name="name" maxlength="80" autocomplete="name"></label>
<label>رقم واتساب للتواصل <input type="tel" name="phone" maxlength="20" autocomplete="tel" dir="ltr"></label>
<label>رسالتك <textarea name="message" maxlength="2000" required rows="4"></textarea></label>
<input type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-9999px">
<button>إرسال</button></form>"""


def _page(status: int, title: str, text: str):
    html, _ = render(PAGE, {"title": title, "text": text})
    return status, HEADERS, html.encode("utf-8")


def contact_form(app_url: str, site_key: str) -> str:
    """The form for a customer's site template; its action is this service's /forms/<site key>."""
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", app_url.rstrip("/")) or not KEY.match(site_key):
        raise ValueError("an https app URL and a site key")
    html, refused = render(FORM, {"action": f"{app_url.rstrip('/')}/forms/{site_key}"})
    if refused:
        raise ValueError("the form action did not pass safe_url")
    return html


class Limiter:
    """In memory, per process: enough against a burst from one address; a distributed flood needs the edge."""

    def __init__(self, limit=FORM_LIMIT, window=FORM_WINDOW, now=time.monotonic):
        self.limit, self.window, self.now, self.hits, self.lock = limit, window, now, {}, threading.Lock()

    def allow(self, key: tuple) -> bool:
        with self.lock:
            t = self.now()
            recent = [h for h in self.hits.get(key, []) if t - h < self.window]
            if len(recent) >= self.limit:
                self.hits[key] = recent
                return False
            self.hits[key] = recent + [t]
            if len(self.hits) > 10000:                  # bounded memory: forget the oldest addresses
                for k in list(self.hits)[:5000]:
                    del self.hits[k]
            return True


class FormHandler:
    def __init__(self, ingest, limiter: Limiter | None = None):
        self.ingest, self.limiter = ingest, limiter or Limiter()

    def handle(self, site_key: str, headers: dict, body: bytes):
        h = {k.lower(): v for k, v in headers.items()}
        if not KEY.match(site_key or ""):
            return _page(404, "غير موجود", "هذا النموذج غير موجود.")
        if len(body) > FORM_MAX_BYTES:
            return _page(413, "رسالة طويلة", "الرسالة أطول من المسموح.")
        if not (h.get("content-type") or "").startswith("application/x-www-form-urlencoded"):
            return _page(415, "طلب غير صالح", "أرسل الرسالة من النموذج.")
        if not self.ingest.channel_active("site_form", site_key):
            return _page(404, "غير موجود", "هذا النموذج غير موجود.")
        address = (h.get("x-forwarded-for") or "").split(",")[-1].strip() or "-"   # the entry our proxy appended, not
                                                                                  # the visitor's own (spoofable) first
        if not self.limiter.allow((site_key, address)):
            return _page(429, "مهلًا", "وصلتنا رسائل كثيرة منك الآن. أعد المحاولة بعد قليل.")
        try:
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8"), max_num_fields=6).items()}
        except (ValueError, UnicodeDecodeError):
            return _page(400, "طلب غير صالح", "تعذّر قراءة الرسالة.")
        thanks = _page(200, "شكرًا", "وصلت رسالتك إلى صاحب المنشأة، وسيتواصل معك.")
        if form.get("website"):                         # the trap field: a bot; same answer, nothing stored
            return thanks
        message = " ".join((form.get("message") or "").split())[:2000]
        name = " ".join((form.get("name") or "").split())[:80]
        phone = (form.get("phone") or "").strip()
        if not message:
            return _page(400, "رسالة فارغة", "اكتب رسالتك ثم أرسلها.")
        if phone and not PHONE.match(phone):
            return _page(400, "رقم غير صالح", "اكتب رقم واتساب بالأرقام، أو اتركه فارغًا.")
        self.ingest.insert_event("site_form", f"form:{uuid.uuid4().hex}", True,
                                 {"form": {"message": message, "name": name, "phone": phone}}, site_key)
        return thanks
