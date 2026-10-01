"""What a customer's site publishes for search engines (spec 28.17): schema.org JSON-LD, sitemap.xml and robots.txt.

    block = jsonld_block(profile)              # <script type="application/ld+json">…</script>, built from approved facts
    page  = inject(rendered_page, block)       # at the one <!--hermes:structured-data--> marker in the template's head
    sitemap_xml(base_url, [("/", date), ("/menu", date)]) · robots_txt(base_url)

The competitor check found that a real Yemeni restaurant site publishes no structured data (28.11: 0 of 1). Sites we
build publish it, from the owner's approved facts only (every item and the business itself carry approved=True; an
unapproved one is refused, never dropped silently). service/render.py refuses placeholders inside <script>, so this
block is produced by a JSON serializer whose output cannot close the script element ("<", ">" and "&" are escaped as
\\u003c, \\u003e, \\u0026), and inserted at a fixed marker. tests/test_service_site_seo.py reads every block back with
service/structured.extract, the same reader the competitor check uses: what we publish, we read completely.
"""
from __future__ import annotations
import json, re
from datetime import date
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from service.structured import _price, _s

MARKER = "<!--hermes:structured-data-->"
MAX_PAGES = 5                                   # registry package_limits.site_pages_max
MAX_ITEMS = 300
SECTOR_TYPES = {"restaurant": ["Restaurant"], "retail": ["Store"], "home_services": ["HomeAndConstructionBusiness", "LocalBusiness"],
                "education": ["EducationalOrganization", "LocalBusiness"], "other": ["LocalBusiness"]}
DAYS = {"Mo": "Monday", "Tu": "Tuesday", "We": "Wednesday", "Th": "Thursday", "Fr": "Friday", "Sa": "Saturday", "Su": "Sunday"}
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PHONE = re.compile(r"^\+?[0-9][0-9 ]{5,19}$")
PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,100}$")


def _https(url: str) -> str:
    u = urlsplit(url or "")
    if u.scheme != "https" or not u.hostname or u.query or u.fragment or u.username:
        raise ValueError("the site URL must be https://<host>[/path]")
    return url.rstrip("/")


def _approved(entry: dict, what: str) -> dict:
    if entry.get("approved") is not True:
        raise ValueError(f"{what} is not an owner-approved fact")
    return entry


def _text(value, what: str, required: bool = False) -> str | None:
    v = _s(value)
    if required and not v:
        raise ValueError(f"{what} is required")
    return v


def jsonld(profile: dict) -> dict:
    """profile: {approved, name, sector, url, city, street?, telephone?, price_range?,
                 hours: [{days: ["Sa", …], opens: "09:00", closes: "23:00"}],
                 items: [{approved, name, price?, currency?, section?}]}"""
    _approved(profile, "the business profile")
    sector = profile.get("sector")
    if sector not in SECTOR_TYPES:
        raise ValueError("sector")
    node = {"@context": "https://schema.org", "@type": SECTOR_TYPES[sector][0] if len(SECTOR_TYPES[sector]) == 1
            else SECTOR_TYPES[sector], "name": _text(profile.get("name"), "name", True), "url": _https(profile.get("url"))}
    address = {"@type": "PostalAddress", "addressLocality": _text(profile.get("city"), "city", True), "addressCountry": "YE"}
    if _text(profile.get("street"), "street"):
        address["streetAddress"] = _text(profile["street"], "street")
    node["address"] = address
    tel = profile.get("telephone")
    if tel is not None:
        if not PHONE.match(str(tel)):
            raise ValueError("telephone")
        node["telephone"] = str(tel)
    if profile.get("price_range") is not None:
        if not re.fullmatch(r"\${1,4}", str(profile["price_range"])):
            raise ValueError("price_range is $ to $$$$")
        node["priceRange"] = profile["price_range"]
    hours = []
    for h in profile.get("hours") or []:
        days = h.get("days") or []
        if not days or any(d not in DAYS for d in days) or not TIME.match(str(h.get("opens"))) or not TIME.match(str(h.get("closes"))):
            raise ValueError("hours: days Mo..Su, opens and closes HH:MM")
        hours.append({"@type": "OpeningHoursSpecification", "dayOfWeek": [DAYS[d] for d in days],
                      "opens": h["opens"], "closes": h["closes"]})
    if hours:
        node["openingHoursSpecification"] = hours
    items = profile.get("items") or []
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} items")
    sections: dict[str, list] = {}
    for it in items:
        _approved(it, f"item {it.get('name')!r}")
        entry = {"@type": "MenuItem" if sector == "restaurant" else "Product", "name": _text(it.get("name"), "item name", True)}
        if it.get("price") is not None:
            price, cur = _price(it["price"]), str(it.get("currency") or "")
            if price is None or not re.fullmatch(r"[A-Z]{3}", cur):
                raise ValueError(f"item {entry['name']!r}: a decimal price and a 3-letter currency")
            entry["offers"] = {"@type": "Offer", "price": price, "priceCurrency": cur}
        sections.setdefault(_text(it.get("section"), "section") or "", []).append(entry)
    if sections and sector == "restaurant":
        node["hasMenu"] = {"@type": "Menu", "hasMenuSection": [
            {"@type": "MenuSection", **({"name": name} if name else {}), "hasMenuItem": entries}
            for name, entries in sections.items()]}
    elif sections:                              # a shop or a service: Offer{price, itemOffered: Product{name}}
        node["hasOfferCatalog"] = {"@type": "OfferCatalog", "itemListElement": [
            {"@type": "Offer", **{k: v for k, v in e.get("offers", {}).items() if k != "@type"},
             "itemOffered": {"@type": "Product", "name": e["name"]}} for entries in sections.values() for e in entries]}
    return node


def jsonld_block(profile: dict) -> str:
    raw = json.dumps(jsonld(profile), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    safe = raw.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f'<script type="application/ld+json">{safe}</script>'


def inject(page: str, block: str) -> str:
    if page.count(MARKER) != 1 or page.lower().find("</head>") < page.find(MARKER):
        raise ValueError(f"the template needs exactly one {MARKER} inside <head>")
    return page.replace(MARKER, block)


def sitemap_xml(base_url: str, pages: list[tuple[str, date]]) -> str:
    base = _https(base_url)
    if not pages or len(pages) > MAX_PAGES:
        raise ValueError(f"1 to {MAX_PAGES} pages")
    rows = []
    for path, lastmod in pages:
        if not PATH.match(path) or ".." in path:
            raise ValueError(f"path {path!r}")
        rows.append(f"  <url><loc>{escape(base + path)}</loc><lastmod>{lastmod.isoformat()}</lastmod></url>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(rows) + "\n</urlset>\n")


def robots_txt(base_url: str) -> str:
    return f"User-agent: *\nAllow: /\nSitemap: {_https(base_url)}/sitemap.xml\n"
