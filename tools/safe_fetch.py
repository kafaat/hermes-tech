"""SSRF guard for every server-side fetch (competitor pages, owner-supplied links).

The content guard protects what is SHOWN; this protects what is FETCHED:
  - https only, port 443, no credentials in the URL
  - the host is resolved once; EVERY address must be public (blocks 169.254.169.254 metadata,
    loopback, private ranges, CGNAT, IPv4-mapped IPv6); the caller must connect to the returned
    pinned address (no second resolution -> no DNS rebinding)
  - each redirect is re-validated; at most 3
  - size and time limits are part of the returned plan
Resolver is injectable for tests; production passes socket.getaddrinfo.
"""
from __future__ import annotations
import ipaddress, json
from pathlib import Path
from urllib.parse import urlsplit

POLICY = json.loads((Path(__file__).resolve().parent.parent / "policies/fetch_policy.json").read_text(encoding="utf-8"))
BLOCKED = [ipaddress.ip_network(n) for n in POLICY["blocked_networks"]]


class FetchRefused(Exception):
    def __init__(self, code, detail=""):
        super().__init__(f"{code} {detail}".strip())
        self.code = code


NAT64 = [ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48")]


def _public_one(a) -> bool:
    return not any(a in n for n in BLOCKED if a.version == n.version) and a.is_global


def _public(ip: str) -> bool:
    """v1.8: IPv6 forms that carry an IPv4 address are judged by that address; tunnels are refused.
    64:ff9b::a9fe:a9fe is "global" to ipaddress but a NAT64 gateway turns it into 169.254.169.254."""
    a = ipaddress.ip_address(ip.split("%")[0])          # a zone id (fe80::1%eth0) never makes an address public
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped:
            return _public_one(a.ipv4_mapped)
        if any(a in n for n in NAT64):
            return _public_one(ipaddress.IPv4Address(int(a) & 0xFFFFFFFF))
        if a.sixtofour or a.teredo:                     # 2002::/16 and 2001::/32 tunnels: never a competitor page
            return False
    return _public_one(a)


def plan(url: str, resolver) -> dict:
    u = urlsplit(url)
    if u.scheme not in POLICY["schemes"]:
        raise FetchRefused("SCHEME_NOT_ALLOWED", u.scheme)
    if u.username or u.password or "@" in u.netloc:
        raise FetchRefused("CREDENTIALS_IN_URL")
    port = u.port or 443
    if port not in POLICY["ports"]:
        raise FetchRefused("PORT_NOT_ALLOWED", str(port))
    host = (u.hostname or "").rstrip(".")
    if not host:
        raise FetchRefused("NO_HOST")
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        host = host.encode("idna").decode("ascii")
    try:
        literal = ipaddress.ip_address(host)
        addrs = [str(literal)]
    except ValueError:
        addrs = sorted({a for a in resolver(host)})
    if not addrs:
        raise FetchRefused("UNRESOLVED", host)
    bad = [a for a in addrs if not _public(a)]
    if bad:
        raise FetchRefused("NON_PUBLIC_ADDRESS", ",".join(bad))
    return {"url": url, "host": host, "connect_to": addrs[0], "port": port,
            "max_bytes": POLICY["max_bytes"], "timeout_seconds": POLICY["timeout_seconds"]}


def follow(chain: list[str], resolver) -> list[dict]:
    """Validate a redirect chain hop by hop (the fetcher calls this before each hop)."""
    if len(chain) - 1 > POLICY["max_redirects"]:
        raise FetchRefused("TOO_MANY_REDIRECTS")
    return [plan(u, resolver) for u in chain]
