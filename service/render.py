"""Rendering owner-supplied text into site templates (claim A24).

    render(template, {"name": "...", "menu_url": "..."})

Placeholders are the only way owner data enters a page:
  {{field}}       HTML text or a QUOTED attribute value: escaped with html.escape(quote=True)
  {{url:field}}   inside href/src only: must pass safe_url(); otherwise "#" is written and the field is reported
There is no raw/unescaped form. check_template() refuses a template that places a placeholder inside
<script>/<style>, in an event-handler or style attribute, or in an unquoted attribute, because escaping is
not enough in those contexts. Every site template passes check_template() in CI.
"""
from __future__ import annotations
import html, re
from urllib.parse import urlsplit

SAFE_SCHEMES = {"https", "mailto", "tel"}
PLACEHOLDER = re.compile(r"\{\{\s*(url:)?([a-z_][a-z0-9_]*)\s*\}\}")
_CTRL = re.compile(r"[\x00-\x20\x7f-\x9f\u200b-\u200f\u2028\u2029\ufeff]")


class TemplateError(ValueError):
    pass


def safe_url(value: str) -> str | None:
    """The URL if it is https/mailto/tel or a same-site path; None for anything a browser could execute."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return None
    v = value.strip()
    probe = _CTRL.sub("", html.unescape(v)).lower()          # 'java\tscript:', '&#106;avascript:', ' JaVaScRiPt:'
    if probe.startswith("/") and not probe.startswith("//") and "\\" not in probe:
        return v
    scheme = urlsplit(probe).scheme
    if scheme not in SAFE_SCHEMES or ":" not in probe.split("/")[0]:
        return None
    if scheme == "https" and not urlsplit(probe).hostname:
        return None
    if _CTRL.search(v):                                      # a control character in the original: refuse, never repair
        return None
    return v


def check_template(template: str) -> None:
    for m in PLACEHOLDER.finditer(template):
        before = template[:m.start()]
        if before.lower().rfind("<script") > before.lower().rfind("</script") or \
           before.lower().rfind("<style") > before.lower().rfind("</style"):
            raise TemplateError(f"placeholder {m.group(0)} inside <script>/<style>")
        tag_open, tag_close = before.rfind("<"), before.rfind(">")
        if tag_open > tag_close:                             # inside a tag: must be a quoted attribute value
            attr = re.search(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*([\"']?)[^\"'<>]*$", before[tag_open:])
            if not attr:
                raise TemplateError(f"placeholder {m.group(0)} outside an attribute value")
            name, quote = attr.group(1).lower(), attr.group(2)
            if not quote:
                raise TemplateError(f"placeholder {m.group(0)} in an unquoted attribute")
            if name.startswith("on") or name in ("style", "srcdoc", "formaction"):
                raise TemplateError(f"placeholder {m.group(0)} in the {name} attribute")
            if m.group(1) and name not in ("href", "src"):
                raise TemplateError(f"url placeholder {m.group(0)} outside href/src")
            if not m.group(1) and name in ("href", "src", "action"):
                raise TemplateError(f"text placeholder {m.group(0)} in {name}: use {{{{url:...}}}}")
        elif m.group(1):
            raise TemplateError(f"url placeholder {m.group(0)} outside an attribute")


def render(template: str, fields: dict) -> tuple[str, list[str]]:
    """(html, refused_fields). Unknown placeholders are an error, not an empty string."""
    check_template(template)
    refused = []

    def sub(m):
        is_url, name = bool(m.group(1)), m.group(2)
        if name not in fields:
            raise TemplateError(f"no value for {name}")
        value = "" if fields[name] is None else str(fields[name])
        if is_url:
            ok = safe_url(value)
            if ok is None:
                refused.append(name)
                return "#"
            return html.escape(ok, quote=True)
        return html.escape(value, quote=True)

    return PLACEHOLDER.sub(sub, template), refused
