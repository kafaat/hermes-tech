"""Competitor page fetcher (claim A23): the ONLY server-side HTTP path, and it goes through tools/safe_fetch.

For every hop (the first URL and each redirect):
  plan = safe_fetch.plan(url, resolver)      # resolves ONCE; every address must be public
  connector(plan, request) -> raw bytes      # connects to plan["connect_to"]; TLS SNI and certificate check
                                             # use the hostname; the connector never resolves a name
No HTTP library is used, so HTTP(S)_PROXY / NO_PROXY and similar environment settings cannot redirect
the connection through something that resolves the name again. robots.txt is fetched through the same path;
login pages are refused (§8.4). tests/test_service_boundaries.py fails if another module opens sockets.
"""
from __future__ import annotations
import re, socket, ssl, sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import safe_fetch  # noqa: E402
from safe_fetch import FetchRefused  # noqa: E402

USER_AGENT = "HermesCompetitorCheck/1.8 (+owner-requested competitor summary)"
LOGIN_HINT = re.compile(rb"<input[^>]+type=[\"']?password|/(?:login|signin|sign-in|auth|account)(?:[/?#]|$)", re.I)


def system_resolver(host: str) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})


def tls_connector(plan: dict, request: bytes) -> bytes:
    """Connect to the pinned address; verify the certificate for the hostname; read at most max_bytes + 1."""
    ctx = ssl.create_default_context()
    with socket.create_connection((plan["connect_to"], plan["port"]), timeout=plan["timeout_seconds"]) as raw:
        with ctx.wrap_socket(raw, server_hostname=plan["host"]) as s:
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
            status, headers, body = _parse(self.connector(plan, _request(current, plan["host"])), plan["max_bytes"])
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
        except OSError:                               # cannot read robots.txt: do not fetch, and say why (temporary)
            raise FetchRefused("ROBOTS_UNREADABLE") from None
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
