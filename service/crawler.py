"""Competitor page fetcher (claim A23): the ONLY server-side HTTP path, and it goes through tools/safe_fetch.

For every hop (the first URL and each redirect):
  plan = safe_fetch.plan(url, resolver)      # resolves ONCE; every address must be public
  connector(plan, request) -> raw bytes      # connects to plan["connect_to"]; TLS SNI and certificate check
                                             # use the hostname; the connector never resolves a name
No HTTP library is used, so HTTP(S)_PROXY / NO_PROXY and similar environment settings cannot redirect
the connection through something that resolves the name again. robots.txt is fetched through the same path;
login pages are refused (§8.4). tests/test_service_boundaries.py fails if another module opens sockets.

ApiClient is the same path for the provider APIs the service calls (Supabase Auth, the WhatsApp Graph API): the
caller names the exact hosts it may reach, every address is validated by safe_fetch.plan and pinned, redirects are
returned (never followed), and only the first validated address is tried: a POST that may have reached the provider
is never sent again to another address. No credential ever enters an exception or a log line.
"""
from __future__ import annotations
import json, re, socket, ssl, sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import safe_fetch  # noqa: E402
from safe_fetch import FetchRefused  # noqa: E402

USER_AGENT = "HermesCompetitorCheck/1.8 (+owner-requested competitor summary)"
LOGIN_HINT = re.compile(rb"<input[^>]+type=[\"']?password|/(?:login|signin|sign-in|auth|account)(?:[/?#]|$)", re.I)


NO_SUCH_NAME = {getattr(socket, n) for n in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, n)}


def system_resolver(host: str) -> list[str]:
    """A name that does not exist is no addresses (plan() then says UNRESOLVED); a resolver that fails stays an error."""
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except socket.gaierror as exc:
        if exc.errno in NO_SUCH_NAME:
            return []
        raise


class NotSent(OSError):
    """The request never left this host: the connection or the TLS handshake failed before a byte was written.
    A sender may treat it as a clean failure; anything after the first byte stays ambiguous."""


def tls_connector(plan: dict, request: bytes) -> bytes:
    """Connect to the pinned address; verify the certificate for the hostname; read at most max_bytes + 1."""
    ctx = ssl.create_default_context()
    try:
        raw = socket.create_connection((plan["connect_to"], plan["port"]), timeout=plan["timeout_seconds"])
    except OSError as exc:
        raise NotSent(type(exc).__name__) from None
    with raw:
        try:
            s = ctx.wrap_socket(raw, server_hostname=plan["host"])
        except OSError as exc:                                   # certificate or handshake: nothing was sent
            raise NotSent(type(exc).__name__) from None
        with s:
            s.settimeout(plan["timeout_seconds"])
            s.sendall(request)
            chunks, size = [], 0
            while size <= plan["max_bytes"] + 16384:
                b = s.recv(65536)
                if not b:
                    break
                chunks.append(b)
                size += len(b)
            return b"".join(chunks)


@dataclass
class Page:
    url: str
    status: int
    body: bytes
    content_type: str
    connected_to: str
    hops: int


def _request(url: str, host: str) -> bytes:
    u = urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return (f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: {USER_AGENT}\r\n"
            "Accept: text/html,text/plain;q=0.9\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n").encode("ascii")


def _parse(raw: bytes, max_bytes: int):
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise FetchRefused("MALFORMED_RESPONSE")
    lines = head.split(b"\r\n")
    m = re.match(rb"HTTP/1\.[01] (\d{3})", lines[0])
    if not m:
        raise FetchRefused("MALFORMED_RESPONSE")
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(b":")
        headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")
    if len(body) > max_bytes:
        raise FetchRefused("TOO_LARGE")
    return int(m.group(1)), headers, body


class Crawler:
    def __init__(self, resolver=system_resolver, connector=tls_connector):
        self.resolver, self.connector = resolver, connector

    def _get(self, url: str) -> Page:
        current, hops = url, 0
        while True:
            plan = safe_fetch.plan(current, self.resolver)
            raw, last = None, None
            for addr in plan.get("addresses", [plan["connect_to"]]):   # every address was validated by plan()
                try:
                    raw = self.connector({**plan, "connect_to": addr}, _request(current, plan["host"]))
                    plan = {**plan, "connect_to": addr}
                    break
                except OSError as exc:                     # this address does not answer: try the next validated one
                    last = exc
            if raw is None:
                raise last
            status, headers, body = _parse(raw, plan["max_bytes"])
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                hops += 1
                if hops > safe_fetch.POLICY["max_redirects"]:
                    raise FetchRefused("TOO_MANY_REDIRECTS")
                current = urljoin(current, headers["location"])
                continue
            return Page(current, status, body, headers.get("content-type", ""), plan["connect_to"], hops)

    def allowed_by_robots(self, url: str) -> bool:
        u = urlsplit(url)
        try:
            p = self._get(f"https://{u.netloc}/robots.txt")
        except FetchRefused:
            raise
        except OSError as exc:                        # cannot read robots.txt: do not fetch, and say why (temporary)
            raise FetchRefused("ROBOTS_UNREADABLE", str(exc) if isinstance(exc, NotSent) else type(exc).__name__) from None
        if p.status >= 500:
            raise FetchRefused("ROBOTS_UNREADABLE")   # the site is failing, not forbidding: try again next time
        if p.status >= 400:
            return p.status in (404, 410)             # no robots.txt: allowed; 401/403: not
        rp = RobotFileParser()
        rp.parse(p.body.decode("utf-8", "replace").splitlines())
        return rp.can_fetch(USER_AGENT, url)

    def fetch(self, url: str) -> Page:
        if not self.allowed_by_robots(url):
            raise FetchRefused("ROBOTS_DISALLOW")
        page = self._get(url)
        if LOGIN_HINT.search(page.url.encode()) or LOGIN_HINT.search(page.body[:200000]):
            raise FetchRefused("LOGIN_PAGE")
        return page


API_USER_AGENT = "Hermes/1.8"
_HEADER_OK = re.compile(r"^[\x21-\x7e][\x20-\x7e]*$")


class ApiClient:
    """JSON over HTTPS to named hosts only (spec 28.12): api.request("POST", url, {...}, {"apikey": k}) -> (status, dict)."""

    def __init__(self, hosts, resolver=system_resolver, connector=tls_connector):
        self.hosts = frozenset(h.lower() for h in hosts)
        self.resolver, self.connector = resolver, connector

    def request(self, method: str, url: str, body: dict | None = None, headers: dict | None = None,
                form: dict | None = None) -> tuple[int, dict]:
        """body: JSON; form: application/x-www-form-urlencoded (OAuth token endpoints); never both."""
        if method not in ("GET", "POST", "DELETE"):
            raise ValueError("method")
        host = (urlsplit(url).hostname or "").lower()
        if host not in self.hosts:
            raise FetchRefused("HOST_NOT_ALLOWED")
        plan = safe_fetch.plan(url, self.resolver)               # https, 443, every address public; resolves once
        if body is not None and form is not None:
            raise ValueError("body or form")
        raw = self.connector(plan, _api_request(method, url, plan["host"], body, headers or {}, form))
        status, _, payload = _parse(raw, plan["max_bytes"])
        try:
            data = json.loads(payload.decode("utf-8")) if payload.strip() else {}
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {"items": data}


def _api_request(method: str, url: str, host: str, body: dict | None, headers: dict, form: dict | None = None) -> bytes:
    u = urlsplit(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    lines = [f"{method} {path} HTTP/1.0", f"Host: {host}", f"User-Agent: {API_USER_AGENT}", "Accept: application/json",
             "Accept-Encoding: identity", "Connection: close"]
    if form is not None:
        payload = urlencode({str(k): str(v) for k, v in form.items()}).encode("ascii")
        lines += ["Content-Type: application/x-www-form-urlencoded", f"Content-Length: {len(payload)}"]
    else:
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if body is not None:
        lines += ["Content-Type: application/json", f"Content-Length: {len(payload)}"]
    for k, v in headers.items():
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", k) or not _HEADER_OK.match(str(v)):
            raise ValueError("header")                           # no CR/LF, no control characters: no injected lines
        lines.append(f"{k}: {v}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload
