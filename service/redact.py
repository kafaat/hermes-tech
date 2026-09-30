"""Personal-data redaction for every log record (claim C12.4).

install() attaches RedactingFilter to every handler of the root logger (and to handlers added later through
install(logger) for named loggers). Two layers, because rules cannot find names:
  1. structural: any field whose NAME says it carries message content (body, text, message, content,
     prompt, completion, payload, messages, reply, caption, note) is replaced by "[REDACTED len=N]",
     in `extra` fields and in dict/list arguments, at any depth;
  2. pattern: phone numbers (Yemeni and E.164), e-mails, URLs, long digit runs (accounts, IDs), bearer
     tokens, JWTs and Meta/OpenAI-style keys are replaced in the rendered message, after Arabic-Indic
     digits are normalised (٧٧١… is a phone number too).
The rule for developers is the first layer: log ids and codes, never text. The second layer catches mistakes.
"""
from __future__ import annotations
import logging, re

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
CONTENT_FIELDS = {"body", "text", "message_text", "content", "prompt", "completion", "payload", "messages", "reply",
                  "caption", "note", "fact", "raw", "input", "output"}
PATTERNS = [
    ("<JWT>", re.compile(r"eyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}")),
    ("<TOKEN>", re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{12,}|\b(?:sk|pk|rk)-[\w-]{16,}|\bEAA[\w]{20,}")),
    ("<EMAIL>", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("<URL>", re.compile(r"https?://\S+|www\.\S+")),
    ("<PHONE>", re.compile(r"(?:\+|00)\d[\d\s-]{7,16}\d|(?:\b967[\s-]?)?\b7[01378]\d(?:[\s-]?\d){6}\b")),
    ("<NUMBER>", re.compile(r"(?<![\w-])\d{6,}(?![\w-])")),
]
_RESERVED = set(vars(logging.LogRecord("x", 0, "x", 0, "", (), None))) | {"message", "asctime"}


def redact_text(s: str) -> str:
    t = s.translate(AR_DIGITS)
    for token, rx in PATTERNS:
        t = rx.sub(token, t)
    return t


def redact_value(v, key: str | None = None, depth: int = 0):
    if key is not None and key.lower() in CONTENT_FIELDS and v is not None:
        return f"[REDACTED len={len(v) if hasattr(v, '__len__') else '?'}]"
    if depth > 6:
        return "[REDACTED depth]"
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, dict):
        return {k: redact_value(x, str(k), depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return type(v)(redact_value(x, None, depth + 1) for x in v)
    return v


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):                      # structural pass first: field names still known
            record.args = redact_value(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_value(a) for a in record.args)
        try:
            msg = record.getMessage()
        except Exception:                            # a broken format string must not leak its arguments raw
            msg = str(record.msg)
        record.msg, record.args = redact_text(msg), ()
        for k in list(vars(record)):
            if k not in _RESERVED:
                setattr(record, k, redact_value(getattr(record, k), k))
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact_text(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install(logger: logging.Logger | None = None) -> RedactingFilter:
    f = RedactingFilter()
    target = logger or logging.getLogger()
    for h in target.handlers:
        if not any(isinstance(x, RedactingFilter) for x in h.filters):
            h.addFilter(f)
    if not any(isinstance(x, RedactingFilter) for x in target.filters):
        target.addFilter(f)
    return f
