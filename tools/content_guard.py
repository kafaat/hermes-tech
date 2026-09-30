"""Deterministic content guard for policies/content_rules.json.

Runs before agent_quality and before any draft is shown to an owner. Cheap, testable,
and explainable: every flag names the rule and the matched span. The LLM auditor only
looks for what rules cannot express (unsupported claims versus kb_facts).
"""
from __future__ import annotations
import json, re
from pathlib import Path
from complaints import load_policy, normalize, proclitic_prefix

ROOT = Path(__file__).resolve().parent.parent
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


class ContentGuard:
    def __init__(self, rules=None, norm_rules=None):
        self.cfg = rules or json.loads((ROOT / "policies/content_rules.json").read_text(encoding="utf-8"))
        self.norm = norm_rules or load_policy()["normalization"]
        procl, art = proclitic_prefix(self.norm)
        self.compiled = []
        for r in self.cfg["rules"]:
            pats = []
            for p in r["patterns"]:
                if r["kind"] == "keyword":
                    k = normalize(p, self.norm)
                    if art and k.startswith("ال") and len(k) > 4:
                        k = k[2:]
                    pats.append(re.compile(r"(?:^|\s)" + procl + re.escape(k) + r"(?=\s|$)"))
                else:
                    pats.append(re.compile(p))
            self.compiled.append((r, pats))

    def _prepare(self, text):
        t = text.translate(AR_DIGITS) if self.cfg["convert_arabic_indic_digits"] else text
        links = re.findall(r"https?://[^\s]+", t)
        return t, links

    def check(self, text: str, kind: str):
        """kind: 'post' | 'reply' | 'site'. Returns a list of flags."""
        raw, links = self._prepare(text)
        norm = normalize(raw, self.norm)
        flags = []
        for r, pats in self.compiled:
            sev = r["severity_by_kind"].get(kind)
            if not sev:
                continue
            if r["id"] == "external_link":
                bad = [u for u in links if not any(re.match(rf"https?://(?:[\w-]+\.)*{re.escape(d)}(?:/|$)", u) for d in self.cfg["allow_link_domains"])]
                if bad:
                    flags.append({"rule": r["id"], "severity": sev, "match": bad[0], "reason": r["reason"]})
                continue
            target = raw if r["kind"] == "regex" and r["id"] == "phone_number" else norm
            for p in pats:
                m = p.search(target)
                if m:
                    flags.append({"rule": r["id"], "severity": sev, "match": m.group(0).strip(), "reason": r["reason"]})
                    break
        return flags

    def verdict(self, text, kind):
        f = self.check(text, kind)
        return "block" if any(x["severity"] == "block" for x in f) else "review" if f else "pass"
