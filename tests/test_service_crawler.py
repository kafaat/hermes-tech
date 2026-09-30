import os, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from service.crawler import Crawler, FetchRefused

PUBLIC = "93.184.216.34"


def resp(status=200, body=b"<html>menu</html>", headers=None):
    h = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    return f"HTTP/1.1 {status} X\r\n{h}\r\n".encode() + body


class Net:
    """Fake DNS + server. Records every resolution and every connection."""

    def __init__(self, dns, pages):
        self.dns, self.pages, self.resolutions, self.connections = dns, pages, [], []

    def resolve(self, host):
        self.resolutions.append(host)
        v = self.dns[host]
        return v.pop(0) if isinstance(v, list) and v and isinstance(v[0], list) else v

    def connect(self, plan, request):
        self.connections.append((plan["connect_to"], plan["host"]))
        path = request.split(b" ")[1].decode()
        return self.pages.get((plan["host"], path), resp(404, b""))


class TestSystemResolver(unittest.TestCase):
    def test_a_name_that_does_not_exist_is_unresolved_not_an_unreadable_robots(self):
        import socket
        from unittest import mock
        from service import crawler
        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, "Name or service not known")):
            self.assertEqual(crawler.system_resolver("no-such.example"), [])
            with self.assertRaises(FetchRefused) as e:
                Crawler().fetch("https://no-such.example/")
            self.assertEqual(e.exception.code, "UNRESOLVED")
        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror(socket.EAI_AGAIN, "Temporary failure")):
            with self.assertRaises(socket.gaierror):
                crawler.system_resolver("shop.example")


class TestCrawler(unittest.TestCase):
    def test_connects_to_the_pinned_address_with_the_hostname_for_tls(self):
        n = Net({"shop.example": [PUBLIC]}, {("shop.example", "/menu"): resp()})
        p = Crawler(n.resolve, n.connect).fetch("https://shop.example/menu")
        self.assertEqual((p.status, p.connected_to), (200, PUBLIC))
        self.assertTrue(all(c == (PUBLIC, "shop.example") for c in n.connections))

    def test_dns_rebinding_between_check_and_connect_is_impossible(self):
        # first answer public, any later answer private: each hop resolves once and the connector never resolves
        n = Net({"shop.example": [[PUBLIC], ["10.0.0.5"], ["10.0.0.5"]]}, {("shop.example", "/robots.txt"): resp(404, b""),
                                                                          ("shop.example", "/"): resp()})
        with self.assertRaises(FetchRefused) as e:
            Crawler(n.resolve, n.connect).fetch("https://shop.example/")
        self.assertEqual(e.exception.code, "NON_PUBLIC_ADDRESS")      # the second hop's resolution is checked again
        self.assertNotIn("10.0.0.5", [c[0] for c in n.connections])

    def test_redirect_to_metadata_private_or_nat64_is_refused(self):
        for target, dns in (("https://meta.example/", {"meta.example": ["169.254.169.254"]}),
                            ("https://nat64.example/", {"nat64.example": ["64:ff9b::a9fe:a9fe"]}),
                            ("https://[fd00::1]/", {}),
                            ("http://shop.example/", {})):
            n = Net({"shop.example": [PUBLIC], **dns}, {("shop.example", "/robots.txt"): resp(404, b""),
                                                         ("shop.example", "/a"): resp(302, b"", {"Location": target})})
            with self.assertRaises(FetchRefused, msg=target):
                Crawler(n.resolve, n.connect).fetch("https://shop.example/a")
            self.assertTrue(all(c[0] == PUBLIC for c in n.connections), target)

    def test_redirects_are_bounded(self):
        pages = {("shop.example", "/robots.txt"): resp(404, b"")}
        for i in range(6):
            pages[("shop.example", f"/r{i}")] = resp(302, b"", {"Location": f"/r{i + 1}"})
        n = Net({"shop.example": [PUBLIC]}, pages)
        with self.assertRaises(FetchRefused) as e:
            Crawler(n.resolve, n.connect).fetch("https://shop.example/r0")
        self.assertEqual(e.exception.code, "TOO_MANY_REDIRECTS")

    def test_environment_proxies_cannot_change_the_connection(self):
        os.environ["HTTPS_PROXY"] = "http://10.0.0.1:3128"
        try:
            n = Net({"shop.example": [PUBLIC]}, {("shop.example", "/"): resp()})
            Crawler(n.resolve, n.connect).fetch("https://shop.example/")
            self.assertEqual({c[0] for c in n.connections}, {PUBLIC})
        finally:
            del os.environ["HTTPS_PROXY"]

    def test_an_address_that_does_not_answer_falls_back_to_the_next_validated_one(self):
        n = Net({"shop.example": ["2a00:1450:4001:82b::200e", PUBLIC]}, {("shop.example", "/robots.txt"): resp(404, b""),
                                                                         ("shop.example", "/menu"): resp()})
        tried = []

        def connect(plan, request):
            tried.append(plan["connect_to"])
            if plan["connect_to"] == PUBLIC:
                raise ConnectionRefusedError("down")
            return n.connect(plan, request)
        page = Crawler(n.resolve, connect).fetch("https://shop.example/menu")
        self.assertEqual(page.connected_to, "2a00:1450:4001:82b::200e")
        self.assertEqual(tried[:2], [PUBLIC, "2a00:1450:4001:82b::200e"])       # IPv4 first, then the IPv6 fallback
        def all_down(plan, request):
            raise ConnectionRefusedError("down")
        with self.assertRaises(FetchRefused) as cm:                             # no address answers: said, not hidden
            Crawler(Net({"shop.example": [PUBLIC]}, {}).resolve, all_down).fetch("https://shop.example/menu")
        self.assertEqual(cm.exception.code, "ROBOTS_UNREADABLE")

    def test_an_unreadable_robots_txt_names_its_cause(self):
        n = Net({"shop.example": [PUBLIC]}, {})

        def down(plan, request):
            raise TimeoutError("no answer")
        with self.assertRaises(FetchRefused) as cm:
            Crawler(n.resolve, down).fetch("https://shop.example/")
        self.assertEqual((cm.exception.code, str(cm.exception)), ("ROBOTS_UNREADABLE", "ROBOTS_UNREADABLE TimeoutError"))

    def test_an_unreadable_robots_txt_is_a_temporary_refusal_not_a_prohibition(self):
        for robots in (resp(503, b""), resp(500, b"")):
            n = Net({"shop.example": [PUBLIC]}, {("shop.example", "/robots.txt"): robots, ("shop.example", "/"): resp(200, b"ok")})
            with self.assertRaises(FetchRefused) as cm:
                Crawler(n.resolve, n.connect).fetch("https://shop.example/")
            self.assertEqual(cm.exception.args[0], "ROBOTS_UNREADABLE")
        n = Net({"shop.example": [PUBLIC]}, {("shop.example", "/robots.txt"): resp(403, b""), ("shop.example", "/"): resp(200, b"ok")})
        with self.assertRaises(FetchRefused) as cm:
            Crawler(n.resolve, n.connect).fetch("https://shop.example/")
        self.assertEqual(cm.exception.args[0], "ROBOTS_DISALLOW")

    def test_robots_disallow_login_pages_and_size(self):
        n = Net({"shop.example": [PUBLIC]}, {("shop.example", "/robots.txt"): resp(200, b"User-agent: *\nDisallow: /private"),
                                             ("shop.example", "/login"): resp(), ("shop.example", "/big"): resp(200, b"x" * 2_000_001),
                                             ("shop.example", "/form"): resp(200, b'<form><input type="password"></form>')})
        c = Crawler(n.resolve, n.connect)
        for path, code in (("/private/x", "ROBOTS_DISALLOW"), ("/login", "LOGIN_PAGE"), ("/form", "LOGIN_PAGE"), ("/big", "TOO_LARGE")):
            with self.assertRaises(FetchRefused, msg=path) as e:
                c.fetch("https://shop.example" + path)
            self.assertEqual(e.exception.code, code, path)


if __name__ == "__main__":
    unittest.main()
