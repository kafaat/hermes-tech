"""Structured facts from a competitor's public page: schema.org JSON-LD first, the page layout never.

    facts = extract(page.body)                 # {"business": {...}, "items": [...], "rating": {...}} or empty
    content_hash(facts)                        # stable across a redesign: only the facts are hashed, not the HTML
    changes = diff(old_facts, new_facts)       # deterministic list; summarize(changes) -> Arabic lines for the owner

Many restaurant and shop sites publish their menu, prices, opening hours and rating as schema.org JSON-LD for search
engines. Reading that instead of the page's markup means a redesign changes nothing here, and the owner's summary is
computed, not generated (agent_competitor: "summary of the mechanically computed difference", no model needed).

Pure parsing of bytes the crawler already fetched through the one network path (service/crawler.py: robots, pinned
address, no login pages). Nothing here fetches, follows @id links, or evaluates anything; every input is bounded:
at most 20 JSON-LD blocks of 256 KiB, 5,000 nodes, depth 12, 300 items, and every string is cut to 200 characters with
control characters removed. A page with no usable JSON-LD yields empty facts, never a guess from the layout.
"""
from __future__ import annotations
import hashlib, json, re
from html.parser import HTMLParser
from decimal import Decimal, InvalidOperation

MAX_BLOCKS, MAX_BLOCK_BYTES, MAX_NODES, MAX_DEPTH, MAX_ITEMS, MAX_STR = 20, 256 * 1024, 5000, 12, 300, 200
BUSINESS_TYPES = {"localbusiness", "restaurant", "foodestablishment", "cafeorcoffeeshop", "bakery", "fastfoodrestaurant",
                  "store", "clothingstore", "grocerystore", "electronicsstore", "beautysalon", "hairsalon",
                  "healthandbeautybusiness", "autorepair", "medicalbusiness", "dentist", "pharmacy", "organization"}
ITEM_TYPES = {"menuitem", "product", "offer", "service"}
DAYS = {"monday": "Mo", "tuesday": "Tu", "wednesday": "We", "thursday": "Th", "friday": "Fr", "saturday": "Sa", "sunday": "Su"}
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩﻿]")


class _Blocks(HTMLParser):
    """Collects the text of <script type="application/ld+json"> blocks, nothing else."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._buf, self._in, self._size = [], [], False, 0

    def handle_starttag(self, tag, attrs):
        if tag == "script" and (dict(attrs).get("type") or "").strip().lower() == "application/ld+json":
            self._in, self._buf, self._size = True, [], 0

    def handle_data(self, data):
        if self._in:
            self._size += len(data)
            if self._size <= MAX_BLOCK_BYTES:
                self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in:
            self._in = False
            if len(self.blocks) < MAX_BLOCKS and self._size <= MAX_BLOCK_BYTES:     # an oversize block is dropped whole
                self.blocks.append("".join(self._buf))


def _s(v) -> str | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return None
    v = " ".join(_CTRL.sub("", v).split())
    return v[:MAX_STR] or None


def _types(node: dict) -> set[str]:
    t = node.get("@type")
    return {str(x).lower().rsplit("/", 1)[-1] for x in (t if isinstance(t, list) else [t]) if x}


def _specific(types: set[str]) -> str:
    """The most specific business type: ["Restaurant", "LocalBusiness"] is a restaurant, not a local business."""
    specific = sorted(types - {"localbusiness", "organization", "foodestablishment", "store"})
    return specific[0] if specific else sorted(types)[0]


def _price(v) -> str | None:
    """A decimal string ("1500", "12.5"), or None. Thousands separators and currency symbols are not guessed at."""
    s = _s(v)
    if s is None:
        return None
    try:
        d = Decimal(s.replace(",", "")) if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?|\d+(\.\d+)?", s) else None
    except InvalidOperation:
        return None
    if d is None or d < 0 or d > Decimal("1e9"):
        return None
    return format(d.normalize(), "f")


def _walk(root, out: list, depth=0, budget=None):
    """Every dict node, breadth by recursion, bounded in count and depth."""
    budget = budget if budget is not None else [MAX_NODES]
    if depth > MAX_DEPTH or budget[0] <= 0:
        return
    if isinstance(root, dict):
        budget[0] -= 1
        out.append(root)
        for k, v in root.items():
            if k != "@context":
                _walk(v, out, depth + 1, budget)
    elif isinstance(root, list):
        for v in root:
            _walk(v, out, depth + 1, budget)


def _offer(node: dict):
    offers = node.get("offers")
    for o in (offers if isinstance(offers, list) else [offers]):
        if isinstance(o, dict):
            p = _price(o.get("price") if o.get("price") is not None else o.get("lowPrice"))
            if p is not None:
                return p, (_s(o.get("priceCurrency")) or "").upper()[:3] or None
    p = _price(node.get("price"))
    return (p, (_s(node.get("priceCurrency")) or "").upper()[:3] or None) if p is not None else (None, None)


def _hours(node: dict) -> list[str]:
    out = []
    oh = node.get("openingHours")
    for h in (oh if isinstance(oh, list) else [oh]):
        s = _s(h)
        if s:
            out.append(s)
    spec = node.get("openingHoursSpecification")
    for h in (spec if isinstance(spec, list) else [spec]):
        if not isinstance(h, dict):
            continue
        days = h.get("dayOfWeek")
        days = [DAYS.get(str(d).lower().rsplit("/", 1)[-1], None) for d in (days if isinstance(days, list) else [days])]
        opens, closes = _s(h.get("opens")), _s(h.get("closes"))
        if any(days) and opens and closes:
            out.append(f"{','.join(d for d in days if d)} {opens[:5]}-{closes[:5]}")
    return sorted(set(out))


def extract(body: bytes | str) -> dict:
    """{"business": {...}, "items": [{"name", "price", "currency"}], "rating": {"value", "count"}}; {} when none."""
    html = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    parser = _Blocks()
    try:
        parser.feed(html)
        parser.close()
    except Exception:                                   # noqa: BLE001 - malformed markup: whatever was collected so far
        pass
    nodes: list[dict] = []
    for raw in parser.blocks:
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        _walk(data, nodes)
    business, items, rating = {}, {}, {}
    for n in nodes:
        t = _types(n)
        if t & BUSINESS_TYPES and not business:
            addr = n.get("address") if isinstance(n.get("address"), dict) else {}
            business = {k: v for k, v in {
                "name": _s(n.get("name")), "type": _specific(t & BUSINESS_TYPES),
                "telephone": _s(n.get("telephone")), "price_range": _s(n.get("priceRange")),
                "locality": _s(addr.get("addressLocality")), "street": _s(addr.get("streetAddress")),
                "hours": _hours(n) or None}.items() if v}
        if (t & {"aggregaterating"} or isinstance(n.get("aggregateRating"), dict)) and not rating:
            r = n if "aggregaterating" in t else n["aggregateRating"]
            value, count = _price(r.get("ratingValue")), _price(r.get("reviewCount") or r.get("ratingCount"))
            if value is not None:
                rating = {"value": value, **({"count": count} if count is not None else {})}
        if t & ITEM_TYPES - {"offer"}:
            name = _s(n.get("name"))
            price, currency = _offer(n)
            if name and len(items) < MAX_ITEMS and name not in items:
                items[name] = {"name": name, "price": price, "currency": currency}
    facts = {}
    if business:
        facts["business"] = business
    if items:
        facts["items"] = sorted(items.values(), key=lambda i: i["name"])
    if rating:
        facts["rating"] = rating
    return facts


def content_hash(facts: dict) -> str | None:
    """None for a page without facts (the snapshot is then 'unverifiable', not 'ok' with a meaningless hash)."""
    if not facts:
        return None
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def diff(old: dict, new: dict) -> list[dict]:
    """Deterministic, ordered changes between two fact sets."""
    changes = []
    o_items = {i["name"]: i for i in (old or {}).get("items", [])}
    n_items = {i["name"]: i for i in (new or {}).get("items", [])}
    for name in sorted(n_items.keys() - o_items.keys()):
        changes.append({"kind": "item_added", "name": name, "price": n_items[name]["price"], "currency": n_items[name]["currency"]})
    for name in sorted(o_items.keys() - n_items.keys()):
        changes.append({"kind": "item_removed", "name": name})
    for name in sorted(o_items.keys() & n_items.keys()):
        a, b = o_items[name], n_items[name]
        if (a["price"], a["currency"]) != (b["price"], b["currency"]) and b["price"] is not None:
            changes.append({"kind": "price_changed", "name": name, "old": a["price"], "new": b["price"],
                            "currency": b["currency"] or a["currency"]})
    oh, nh = (old or {}).get("business", {}).get("hours"), (new or {}).get("business", {}).get("hours")
    if oh != nh and nh is not None:
        changes.append({"kind": "hours_changed", "old": oh, "new": nh})
    ob, nb = (old or {}).get("business", {}).get("price_range"), (new or {}).get("business", {}).get("price_range")
    if ob != nb and nb is not None:
        changes.append({"kind": "price_range_changed", "old": ob, "new": nb})
    orr, nr = (old or {}).get("rating", {}), (new or {}).get("rating", {})
    if nr and (orr.get("value") != nr.get("value")):
        changes.append({"kind": "rating_changed", "old": orr.get("value"), "new": nr.get("value"),
                        "count": nr.get("count")})
    return changes


def summarize(changes: list[dict], limit: int = 10) -> str:
    """Arabic lines for the owner; the text is computed from the changes, never generated."""
    lines = []
    for c in changes[:limit]:
        cur = f" {c['currency']}" if c.get("currency") else ""
        if c["kind"] == "price_changed":
            lines.append(f"تغيّر سعر {c['name']}: {c['old'] or 'بلا سعر'} ← {c['new']}{cur}")
        elif c["kind"] == "item_added":
            lines.append(f"صنف جديد: {c['name']}" + (f" بسعر {c['price']}{cur}" if c.get("price") else ""))
        elif c["kind"] == "item_removed":
            lines.append(f"أُزيل صنف: {c['name']}")
        elif c["kind"] == "hours_changed":
            lines.append("تغيّرت ساعات العمل: " + "، ".join(c["new"]))
        elif c["kind"] == "price_range_changed":
            lines.append(f"تغيّر مستوى الأسعار: {c['old'] or '—'} ← {c['new']}")
        elif c["kind"] == "rating_changed":
            lines.append(f"تغيّر التقييم: {c['old'] or '—'} ← {c['new']}" + (f" ({c['count']} تقييمًا)" if c.get("count") else ""))
    if len(changes) > limit:
        lines.append(f"و{len(changes) - limit} تغييرات أخرى")
    return "\n".join(lines)
