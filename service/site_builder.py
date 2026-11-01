"""A customer's one-page site, built from the owner's approved facts only (spec 2.1, 20.2, 28.39).

    files = site_files(business, facts, base_url="https://alreef.example", today=date.today(),
                       app_url="https://<hermes-app>", site_key="...")      # {path: bytes}
    content_hash(files)                                                      # the identity of what was built

Deterministic: the same approved facts give the same bytes and the same hash, so a deployment of an unchanged site
is recognisable (Cloudflare Pages deduplicates by content; app.deploy_payload carries the hash).

What it publishes and why:
  * Arabic, right to left, system fonts, no script, no external resource: a first page well under 500 KB on a weak
    connection (20.2); dark mode and visible focus for keyboard users (WCAG 2.2 AA intent).
  * Only the topics an owner approved, each under a fixed heading (TOPICS); an unapproved or unknown topic is never
    shown, so an internal note cannot leak onto the site.
  * schema.org JSON-LD for local search (service/site_seo.py), sitemap.xml, robots.txt, and a Cloudflare Pages
    _headers file with a strict Content-Security-Policy whose form-action is this service alone.
  * WhatsApp: a wa.me link and its QR code (service/qr.py, generated here, no QR service) from the approved 'phone'.
  * The contact form posts to this service (service/site_form.py); no reply goes to the visitor automatically.
Every owner value enters through service/render.py; the generated blocks (JSON-LD, QR, form) are inserted at one
fixed marker each.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import urlsplit

from service.qr import qr_svg, wa_me
from service.render import render
from service.site_form import contact_form
from service.site_seo import PHONE, inject, jsonld_block, robots_txt, sitemap_xml

MAX_BYTES = 500_000                     # spec 20.2: the first page on a weak connection
TOPICS = (("about", "من نحن"), ("hours", "ساعات العمل"), ("location", "موقعنا"), ("delivery", "التوصيل"),
          ("payment", "طرق الدفع"), ("phone", "للتواصل"))
SECTOR_AR = {"restaurant": "مطعم", "retail": "متجر", "home_services": "خدمات منزلية", "education": "تعليم", "other": "منشأة"}
MARKERS = ("<!--hermes:sections-->", "<!--hermes:contact-->")

PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{name}} · {{city}}</title>
<meta name="description" content="{{description}}">
<link rel="canonical" href="{{url:canonical}}">
<!--hermes:structured-data-->
<style>
:root{--bg:#fbfaf7;--fg:#1b1f24;--muted:#57606a;--card:#fff;--line:#d8dee4;--accent:#0b6e4f}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e8eaed;--muted:#a3abb5;--card:#1b1f24;--line:#30363d;--accent:#3fb98a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;line-height:1.75}
main{max-width:720px;margin:0 auto;padding:20px 16px 40px}h1{font-size:1.6rem;margin:8px 0 0}.sub{color:var(--muted);margin:0 0 16px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
h2{font-size:1.1rem;margin:0 0 6px}p{margin:0;white-space:pre-line}a{color:var(--accent)}
a:focus,button:focus,input:focus,textarea:focus{outline:3px solid var(--accent);outline-offset:2px}
.wa{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-weight:600}
.qr svg{width:148px;height:148px;margin-top:10px;background:#fff;border-radius:6px}
form label{display:block;margin:8px 0}input,textarea{width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button{font:inherit;padding:8px 18px;border-radius:6px;border:0;background:var(--accent);color:#fff;margin-top:8px}
footer{color:var(--muted);font-size:.85rem;margin-top:24px;text-align:center}
</style></head>
<body><main>
<header><h1>{{name}}</h1><p class="sub">{{sector}} · {{city}}</p></header>
<!--hermes:sections-->
<!--hermes:contact-->
<footer>آخر تحديث {{updated}}</footer>
</main></body></html>
"""
SECTION = """<section><h2>{{title}}</h2><p>{{text}}</p></section>
"""
PHONE_SECTION = """<section><h2>{{title}}</h2><p><a href="{{url:tel}}" dir="ltr">{{text}}</a></p></section>
"""
CONTACT_OPEN = """<section id="contact"><h2>تواصل معنا</h2>
"""
WHATSAPP = """<p><a class="wa" href="{{url:link}}">راسلنا على واتساب</a></p>
<div class="qr" role="img" aria-label="{{label}}">"""
CONTACT_CLOSE = "</section>\n"


def _put(page: str, marker: str, block: str) -> str:
    if page.count(marker) != 1:
        raise ValueError(f"the template needs exactly one {marker}")
    return page.replace(marker, block)


def _r(template: str, **fields) -> str:
    out, refused = render(template, fields)
    if refused:
        raise ValueError(f"refused: {refused}")
    return out


def headers_file(app_url: str | None) -> str:
    """Cloudflare Pages _headers: no script, no frame, styles inline, images from the page itself, forms to us only."""
    action = app_url.rstrip("/") if app_url else "'none'"
    csp = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; "
           f"form-action {action}")
    return ("/*\n"
            f"  Content-Security-Policy: {csp}\n"
            "  X-Content-Type-Options: nosniff\n"
            "  Referrer-Policy: strict-origin-when-cross-origin\n"
            "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n")


def site_files(business: dict, facts: dict[str, str], *, base_url: str, today: date,
               app_url: str | None = None, site_key: str | None = None) -> dict[str, bytes]:
    """business: {name, sector, city}; facts: topic -> the owner's approved text (the caller reads approved ones only)."""
    u = urlsplit(base_url)
    if u.scheme != "https" or not u.hostname or u.path not in ("", "/"):
        raise ValueError("base_url is https://<host>")
    base = f"https://{u.netloc}"
    name, city, sector = str(business.get("name") or "").strip(), str(business.get("city") or "").strip(), business.get("sector")
    if not name or not city or sector not in SECTOR_AR:
        raise ValueError("a name, a city and a known sector")
    shown = [(topic, title, str(facts[topic]).strip()) for topic, title in TOPICS if str(facts.get(topic) or "").strip()]
    phone = str(facts.get("phone") or "").strip()
    profile = {"approved": True, "name": name, "sector": sector, "url": base + "/", "city": city}
    if PHONE.match(phone):
        profile["telephone"] = phone
    description = " · ".join(t for topic, _, t in shown if topic in ("about", "hours"))[:160] or f"{SECTOR_AR[sector]} في {city}"
    page = _r(PAGE, name=name, city=city, sector=SECTOR_AR[sector], description=description, canonical=base + "/",
              updated=today.isoformat())
    page = inject(page, jsonld_block(profile))
    def section(topic, title, text):
        if topic == "phone" and PHONE.match(text):      # a number reads left to right, and dials on a phone
            return _r(PHONE_SECTION, title=title, text=text, tel="tel:" + re.sub(r"\s", "", text))
        return _r(SECTION, title=title, text=text)
    page = _put(page, MARKERS[0], "".join(section(*s) for s in shown))
    contact = ""
    try:
        link = wa_me(phone) if phone else None
    except ValueError:
        link = None                                     # a number WhatsApp cannot dial is shown as text, not linked
    if link or (app_url and site_key):
        contact = CONTACT_OPEN
        if link:
            # inline in HTML an <svg> needs no namespace; it is dropped so the page carries no http:// at all
            contact += _r(WHATSAPP, link=link, label=f"رمز QR لمراسلة {name} على واتساب") + qr_svg(link, module_px=4).replace(' xmlns="http://www.w3.org/2000/svg"', "", 1) + "</div>\n"
        if app_url and site_key:
            contact += contact_form(app_url, site_key) + "\n"
        contact += CONTACT_CLOSE
    page = _put(page, MARKERS[1], contact)
    files = {"index.html": page.encode("utf-8"),
             "sitemap.xml": sitemap_xml(base, [("/", today)]).encode("utf-8"),
             "robots.txt": robots_txt(base).encode("utf-8"),
             "_headers": headers_file(app_url).encode("utf-8")}
    check(files, base, app_url)
    return files


def check(files: dict[str, bytes], base: str, app_url: str | None) -> None:
    """What every built site must satisfy before anyone sees it: size, no plain http, links only where expected."""
    total = sum(len(v) for v in files.values())
    if total > MAX_BYTES:
        raise ValueError(f"the site is {total} bytes, above {MAX_BYTES}")
    page = files["index.html"].decode("utf-8")
    if 'lang="ar" dir="rtl"' not in page or "<script src" in page or "http://" in page:
        raise ValueError("an Arabic right-to-left page with no external script and no plain http")
    allowed = {urlsplit(base).hostname, "wa.me"} | ({urlsplit(app_url).hostname} if app_url else set())
    for url in re.findall(r'(?:href|src|action)="([^"]+)"', page):
        host = urlsplit(url).hostname
        if host and host not in allowed:
            raise ValueError(f"a link to {host} is not expected on the site")


def content_hash(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode() + b"\0" + hashlib.sha256(files[path]).digest())
    return h.hexdigest()
