"""Every pre-pilot concern has ONE code path; a second path would bypass the guard (review of 1.7, §4)."""
import ast, re, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
CODE = [p for d in ("service", "tools") for p in (ROOT / d).glob("*.py")]
NETWORK = {"socket", "ssl", "http.client", "urllib.request", "requests", "httpx", "aiohttp", "urllib3", "pycurl"}


def imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


class TestBoundaries(unittest.TestCase):
    def test_only_the_crawler_opens_network_connections(self):
        offenders = {p.name: sorted(imports(p) & NETWORK) for p in CODE if imports(p) & NETWORK and p.name != "crawler.py"}
        self.assertEqual(offenders, {})

    def test_the_crawler_connects_only_to_a_safe_fetch_plan(self):
        src = (ROOT / "service/crawler.py").read_text(encoding="utf-8")
        self.assertIn("safe_fetch.plan(current, self.resolver)", src)
        self.assertEqual(src.count("create_connection("), 1)
        self.assertIn('(plan["connect_to"], plan["port"])', src)
        self.assertIn('server_hostname=plan["host"]', src)

    def test_no_raw_html_insertion_outside_the_renderer(self):
        for p in CODE:
            src = p.read_text(encoding="utf-8")
            if p.name != "render.py":
                self.assertNotRegex(src, r"\|\s*safe\b|Markup\(|autoescape\s*=\s*False", p.name)

    def test_dispatcher_never_loops_over_a_send(self):
        tree = ast.parse((ROOT / "service/dispatcher.py").read_text(encoding="utf-8"))
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_once"):
            self.assertFalse([n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))], "retry loop in run_once")

    def test_logging_setup_installs_redaction(self):
        src = (ROOT / "service/redact.py").read_text(encoding="utf-8")
        self.assertIn("def install(", src)
        for p in (ROOT / "service").glob("*.py"):
            s = p.read_text(encoding="utf-8")
            self.assertNotRegex(s, r"logging\.basicConfig", p.name)   # handlers are created where install() runs


if __name__ == "__main__":
    unittest.main()
