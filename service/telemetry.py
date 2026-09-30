"""What leaves for the monitoring backend (claim C12.5): an allow-list, not a block-list.

sanitize_span(span_dict) keeps only the attributes named in ops/otel_genai_mapping.yaml, drops every span
event (GenAI content events carry prompts and completions), keeps an exception's TYPE only, and drops any
kept string value that still looks like personal data or is longer than MAX_VALUE. Content capture flags in
the mapping must stay false; tests/test_service_telemetry.py fails otherwise. The exporter wrapper applies
this to every batch before the real exporter sees it.
"""
from __future__ import annotations
import re, yaml
from pathlib import Path

MAPPING = yaml.safe_load((Path(__file__).resolve().parent.parent / "ops/otel_genai_mapping.yaml").read_text(encoding="utf-8"))
ALLOWED = set(MAPPING["attributes"]) | {"hermes.span.kind"}
MAX_VALUE = 120
_PII = re.compile(r"[\w.+-]+@[\w-]+\.\w|(?:\+|00)?\d[\d\s-]{7,}\d|[\u0600-\u06FF]{2,}\s+[\u0600-\u06FF]{2,}")


def _clean(v):
    if isinstance(v, str):
        return None if len(v) > MAX_VALUE or _PII.search(v) else v
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return None                                        # lists, dicts: never exported


def sanitize_span(span: dict) -> dict:
    attrs = {}
    for k, v in (span.get("attributes") or {}).items():
        if k in ALLOWED:
            c = _clean(v)
            if c is not None:
                attrs[k] = c
    out = {k: span[k] for k in ("name", "trace_id", "span_id", "parent_id", "start", "end", "status") if k in span}
    if isinstance(out.get("name"), str) and (_clean(out["name"]) is None):
        out["name"] = "span"
    if span.get("exception_type"):
        attrs["error.type"] = str(span["exception_type"])[:60]
    out["attributes"], out["events"] = attrs, []
    return out


class SanitizingExporter:
    """Wraps any exporter with an export(list_of_span_dicts) method."""

    def __init__(self, inner):
        self.inner = inner

    def export(self, spans):
        return self.inner.export([sanitize_span(s) for s in spans])
