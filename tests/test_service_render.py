import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.render import render, safe_url, check_template, TemplateError

T = '<h1>{{name}}</h1><p title="{{tagline}}">{{about}}</p><a href="{{url:menu_url}}">القائمة</a>'


class TestRender(unittest.TestCase):
    def test_owner_text_is_escaped_in_text_and_quoted_attributes(self):
        out, refused = render(T, {"name": "<script>alert(1)</script>", "tagline": '" onmouseover="alert(1)',
                                  "about": "<img src=x onerror=alert(1)>", "menu_url": "https://menu.example/ar"})
        self.assertNotIn("<script>", out)
        self.assertNotIn("<img", out)
        self.assertNotIn('" onmouseover', out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn('href="https://menu.example/ar"', out)
        self.assertEqual(refused, [])

    def test_executable_urls_are_refused_in_every_disguise(self):
        for u in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "java\tscript:alert(1)",
                  "java\nscript:alert(1)", "\x01javascript:alert(1)", "&#106;avascript:alert(1)", "&#x6A;avascript:alert(1)",
                  "javascript&colon;alert(1)", "data:text/html;base64,PHNjcmlwdD4=", "vbscript:msgbox(1)",
                  "//evil.example/x", "/\\evil.example", "http://plain.example", "https:evil", "java\u200bscript:alert(1)"):
            self.assertIsNone(safe_url(u), repr(u))
            out, refused = render(T, {"name": "n", "tagline": "t", "about": "a", "menu_url": u})
            self.assertIn('href="#"', out, repr(u))
            self.assertEqual(refused, ["menu_url"])

    def test_allowed_links(self):
        for u in ("https://wa.me/967771234567", "tel:+967771234567", "mailto:owner@example.com", "/menu#today"):
            self.assertEqual(safe_url(u), u)

    def test_arabic_text_is_preserved(self):
        out, _ = render("<p>{{about}}</p>", {"about": "مطعم الشيباني — مندي وحنيذ"})
        self.assertEqual(out, "<p>مطعم الشيباني — مندي وحنيذ</p>")

    def test_dangerous_template_positions_are_refused(self):
        for bad in ("<script>var n = '{{name}}';</script>", "<style>.x{content:'{{name}}'}</style>",
                    '<div onclick="{{name}}">', "<div title={{name}}>", '<a href="{{name}}">', '<p>{{url:menu_url}}</p>',
                    '<div style="{{name}}">', '<img alt="{{url:menu_url}}">', "<{{name}}>"):
            with self.assertRaises(TemplateError, msg=bad):
                check_template(bad)

    def test_missing_field_is_an_error_not_an_empty_string(self):
        with self.assertRaises(TemplateError):
            render("<p>{{about}}</p>", {})


class TestFormAction(unittest.TestCase):
    def test_a_form_action_takes_only_a_validated_url(self):
        tpl = '<form method="post" action="{{url:a}}"></form>'
        self.assertEqual(render(tpl, {"a": "https://app.example/forms/k"})[0], '<form method="post" action="https://app.example/forms/k"></form>')
        html, refused = render(tpl, {"a": "javascript:alert(1)"})
        self.assertEqual((html, refused), ('<form method="post" action="#"></form>', ["a"]))
        with self.assertRaises(TemplateError):
            check_template('<form action="{{a}}"></form>')              # a text placeholder in action stays refused


if __name__ == "__main__":
    unittest.main()
