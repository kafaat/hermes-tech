"""Complaint matcher and router for policies/complaint_keywords.json.

Pipeline: normalize -> neutralize exception spans -> match categories (token-bounded,
Arabic proclitics allowed) -> owner-inquiry detection -> route by channel and severity.
A matched message is never answered by agent_replies.
"""
from __future__ import annotations
import json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
SEVERITY_RANK = {"high": 1, "critical": 2}


def load_policy(path=ROOT / "policies/complaint_keywords.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def normalize(text: str, rules: dict) -> str:
    t = text
    if rules.get("strip_diacritics"):
        t = DIACRITICS.sub("", t)
    if rules.get("strip_tatweel"):
        t = t.replace("\u0640", "")
    if rules.get("unify_alef"):
        t = re.sub("[أإآٱ]", "ا", t)
    if rules.get("ta_marbuta_to_ha"):
        t = t.replace("ة", "ه")
    if rules.get("alef_maqsura_to_ya"):
        t = t.replace("ى", "ي")
    t = re.sub(r"[^\w\s]", " ", t)       # punctuation -> space
    return re.sub(r"\s+", " ", t).strip().lower()


def proclitic_prefix(rules: dict):
    """Regex prefix for Arabic proclitics: [conjunction]? ( لل | [preposition](ال)? | ال )?"""
    allowed = {normalize(x, rules) for x in rules["allowed_proclitics"]}
    conj = "".join(c for c in "وف" if c in allowed)
    prep = "".join(c for c in "بلك" if c in allowed)
    art = "ال" in allowed
    rest = []
    if art and "ل" in prep:
        rest.append("لل")
    if prep:
        rest.append(f"[{prep}]" + ("(?:ال)?" if art else ""))
    if art:
        rest.append("ال")
    prefix = (f"[{conj}]?" if conj else "") + ("(?:" + "|".join(rest) + ")?" if rest else "")
    return prefix, art


class Matcher:
    def __init__(self, policy=None):
        self.p = policy or load_policy()
        self.rules = self.p["normalization"]
        self.procl, self.art = proclitic_prefix(self.rules)
        self.cats = []
        for c in self.p["categories"]:
            pats = [self._pattern(k) for k in c["keywords"] + c["keywords_ye"]]
            self.cats.append((c["id"], c["severity"], c["sla_minutes"], pats))
        self.inquiry = [self._pattern(k) for k in self.p["owner_inquiry_terms"]]
        self.exceptions = [normalize(e["phrase"], self.rules) for e in self.p["exceptions"]]

    def _pattern(self, kw):
        k = normalize(kw, self.rules)
        if self.art and k.startswith("ال") and len(k) > 4:
            k = k[2:]          # the article is handled by the proclitic prefix
        return re.compile(r"(?:^|\s)" + self.procl + re.escape(k) + r"(?=\s|$)")

    def classify(self, text: str):
        t = normalize(text, self.rules)
        for ex in self.exceptions:                       # neutralize only the exception span
            t = t.replace(ex, " " * len(ex))
        hits = [(cid, sev, sla) for cid, sev, sla, pats in self.cats if any(p.search(t) for p in pats)]
        inquiry = any(p.search(t) for p in self.inquiry)
        return hits, inquiry

    def decision_for(self, categories, channel: str, source: str = "keywords"):
        """Single place that turns category ids into severity, SLA and routes (used by stage 1 AND stage 2)."""
        by_id = {c["id"]: c for c in self.p["categories"]}
        known = [by_id[c] for c in categories if c in by_id]
        ch = self.p["channels"][channel]
        if known:
            top = max(known, key=lambda c: (SEVERITY_RANK[c["severity"]], -c["sla_minutes"]))
            severity, sla, needs_category = top["severity"], min(c["sla_minutes"] for c in known), False
        else:
            u = self.p["triage_unknown_category"]
            severity, sla, needs_category = u["severity"], u["sla_minutes"], True
        routes = list(ch["severity_routes"][severity])
        cats = [c["id"] for c in known]
        if channel == "patron" and any(c in ch["copy_to_founder"] for c in cats) and "founder_slack" not in routes:
            routes.append("founder_slack")
        d = {"action": "escalate", "kind": "complaint", "routes": routes, "categories": cats,
             "sla_minutes": sla, "severity": severity, "source": source}
        if needs_category:
            d["needs_category"] = True
        return d

    def route(self, text: str, channel: str):
        """Return a routing decision. channel is 'patron' or 'client'."""
        hits, inquiry = self.classify(text)
        ch = self.p["channels"][channel]
        if not hits:
            if inquiry and channel == "patron":
                return {"action": "escalate", "kind": "owner_inquiry", "routes": ["owner_whatsapp"], "categories": [], "sla_minutes": None}
            return {"action": "reply_allowed", "kind": "none", "routes": [], "categories": [], "sla_minutes": None}
        return self.decision_for([h[0] for h in hits], channel, "keywords")
