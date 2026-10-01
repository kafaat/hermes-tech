"""Daily competitor check: a deterministic workflow, not an agent (no model: the owner's summary is computed).

    python -m service.jobs competitor_check      (Railway cron, daily; role hermes_jobs)

For every active competitor whose last snapshot is older than 7 days (weekly, about 8 checks a month for two
competitors, under the 10 the package allows and the database enforces, 0017):
  crawler.fetch(url)          the one network path: robots.txt, pinned address, no login pages (service/crawler.py)
  structured.extract(body)    schema.org JSON-LD facts, never the layout (service/structured.py)
  one snapshot, one of three states the owner can tell apart:
    ok            facts found; the summary is the computed difference from the last facts (or the first inventory)
    unverifiable  fetched but no structured data (or the fetch failed): the summary says automatic tracking does
                  not work for this competitor, and whether the page's visible text changed since the last check
    blocked       the site refuses automated checks (robots.txt) or shows a login page: we do not go further
Each competitor is its own transaction; one failure never stops the rest. Only the facts and a hash of the page's
visible text are stored, never the page (spec 4.4).
"""
from __future__ import annotations
import hashlib, logging, sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from safe_fetch import FetchRefused  # noqa: E402
from service.structured import content_hash, diff, extract, summarize  # noqa: E402

log = logging.getLogger("hermes.competitor")
DUE_AFTER_DAYS = 7
MAX_PER_RUN = 200
BLOCKED_CODES = {"ROBOTS_DISALLOW": "الموقع يطلب من الأدوات الآلية عدم فحص هذه الصفحة (robots.txt)؛ لا نتجاوز ذلك.",
                 "LOGIN_PAGE": "الصفحة تطلب تسجيل دخول؛ لا نفحص صفحات الحسابات."}
TEMPORARY_CODES = {"ROBOTS_UNREADABLE": "تعذّرت قراءة ملف robots.txt للموقع (الموقع لا يستجيب أو يتعطل)؛ لا نفحص دون قراءته. يُعاد الفحص في موعده التالي."}
NO_FACTS = "المتابعة الآلية لا تعمل لهذا المنافس: صفحته لا تنشر بيانات منظمة (قائمة وأسعار). إن احتجت متابعته فراقبه يدويًا."


class Store(Protocol):
    def due(self, limit: int) -> list[dict]: ...           # id, customer_id, url, label, last_facts, last_page_hash
    def insert(self, snap: dict) -> bool: ...              # False: refused (the monthly cap or an inactive competitor)


class Fetcher(Protocol):
    def fetch(self, url: str): ...                         # -> page with .status and .body; raises FetchRefused


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def text_hash(body: bytes) -> str:
    """Hash of the visible text only: markup, scripts and whitespace churn do not count as a change."""
    p = _Text()
    try:
        p.feed(body.decode("utf-8", "replace"))
        p.close()
    except Exception:                                      # noqa: BLE001 - malformed markup: hash what was read
        pass
    return hashlib.sha256(" ".join(" ".join(p.parts).split()).encode()).hexdigest()


def _first(facts: dict) -> str:
    items, rating = facts.get("items", []), facts.get("rating")
    parts = [f"أول لقطة: {len(items)} من المنتجات والخدمات" + ("، بأسعارها" if any(i["price"] for i in items) else "")]
    if facts.get("business", {}).get("hours"):
        parts.append("ساعات العمل: " + "، ".join(facts["business"]["hours"]))
    if rating:
        parts.append(f"التقييم {rating['value']}" + (f" ({rating['count']} تقييمًا)" if rating.get("count") else ""))
    return "\n".join(parts)


def check(fetcher: Fetcher, comp: dict) -> dict:
    """One snapshot for one competitor. Never raises for anything the site does."""
    snap = {"customer_id": comp["customer_id"], "competitor_id": comp["id"], "content_hash": None,
            "structured_facts": None, "page_hash": None}
    try:
        page = fetcher.fetch(comp["url"])
    except FetchRefused as exc:
        code = getattr(exc, "code", None) or "REFUSED"
        detail = str(exc)[len(code):].strip()
        cause = f"{code}:{detail}" if detail and code == "ROBOTS_UNREADABLE" else code   # the cause, never a host or URL
        if code in BLOCKED_CODES:
            return {**snap, "status": "blocked", "reason": cause, "diff_summary": BLOCKED_CODES[code]}
        if code in TEMPORARY_CODES:
            return {**snap, "status": "unverifiable", "reason": cause, "diff_summary": TEMPORARY_CODES[code]}
        return {**snap, "status": "unverifiable", "reason": cause,
                "diff_summary": f"تعذّر جلب الصفحة ({code}). يُعاد الفحص في موعده التالي."}
    except Exception as exc:                               # noqa: BLE001 - network, TLS, timeouts
        return {**snap, "status": "unverifiable", "reason": type(exc).__name__,
                "diff_summary": f"تعذّر الوصول إلى الموقع ({type(exc).__name__}). يُعاد الفحص في موعده التالي."}
    if page.status != 200:
        return {**snap, "status": "unverifiable", "reason": f"HTTP_{page.status}",
                "diff_summary": f"أعاد الموقع الحالة {page.status}. يُعاد الفحص في موعده التالي."}
    ph = text_hash(page.body)
    facts = extract(page.body)
    if not facts:
        if comp.get("last_page_hash") and comp["last_page_hash"] != ph:
            summary = "تغيّر نص الصفحة منذ الفحص السابق، ولا يمكن معرفة ما تغيّر آليًا. " + NO_FACTS
        else:
            summary = NO_FACTS
        return {**snap, "status": "unverifiable", "reason": "NO_STRUCTURED_DATA", "page_hash": ph, "diff_summary": summary}
    last = comp.get("last_facts")
    if not last:
        summary = _first(facts)
    else:
        changes = diff(last, facts)
        summary = summarize(changes) if changes else "لا تغيير في القائمة والأسعار والساعات والتقييم منذ الفحص السابق."
    return {**snap, "status": "ok", "reason": "CHANGED" if last and diff(last, facts) else ("FIRST" if not last else "UNCHANGED"),
            "content_hash": content_hash(facts), "structured_facts": facts, "page_hash": ph,
            "diff_summary": summary}


def run(store: Store, fetcher: Fetcher, limit: int = MAX_PER_RUN, report=None) -> dict:
    """Counts per state. report(competitor_id, state, reason) gets one line per competitor: an id and a code, no
    page content, so a failed run says why in the job's own log."""
    counts = {"ok": 0, "unverifiable": 0, "blocked": 0, "refused": 0, "error": 0}
    for comp in store.due(limit):
        try:
            snap = check(fetcher, comp)
            state = snap["status"] if store.insert(snap) else "refused"
            counts[state] += 1
            reason = snap.get("reason", "")
        except Exception as exc:                           # noqa: BLE001 - our side failed; the next competitor still runs
            counts["error"] += 1
            state, reason = "error", type(exc).__name__
            log.error("competitor check failed: %s", reason)
        if report:
            report(comp["id"], state, reason)
    return counts
