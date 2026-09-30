"""Two-stage complaint detection.

Stage 1 (complaints.Matcher): explainable keyword rules, high precision.
Stage 2 (agent_triage, gpt-5.4-nano): binary classifier for paraphrased complaints
that the lexicon misses. The combination is monotonic: stage 2 can only ADD an
escalation, never remove a stage-1 match, and it never produces a reply. Severity, SLA and
routes come from Matcher.decision_for, the same code stage 1 uses (fix R4, v1.5).
"""
from __future__ import annotations
from complaints import Matcher


def combined_route(matcher: Matcher, text: str, channel: str, classifier, threshold: float = 0.30):
    """classifier(text) -> (is_complaint: bool, category: str | None, confidence: float)."""
    d = matcher.route(text, channel)
    if d["kind"] == "complaint":
        return d
    is_c, cat, conf = classifier(text)
    if is_c and conf >= threshold:
        # same routing code as stage 1: a legal complaint found by stage 2 is critical / 15 min, never softer
        out = matcher.decision_for([cat] if cat else [], channel, "classifier")
        out["confidence"] = conf
        return out
    return dict(d, source="keywords")
