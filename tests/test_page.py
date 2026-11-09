"""Spec tests for the GET / checker page. Real server in a thread, real HTTP, real Chrome via Playwright."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from drawlint.server import make_server

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

NS = 'xmlns="http://www.w3.org/2000/svg"'
DISCLAIMER = "Automated formality check only, not legal advice."
HEAD = 'width="8.5in" height="11in" viewBox="0 0 612 792"'
CLEAN = ('<svg %s %s><text x="306" y="100" font-size="14" fill="#000">1/1</text>'
         '<text x="200" y="300" font-size="12" fill="#000">FIG. 1</text></svg>' % (NS, HEAD))
BAD = ('<svg %s width="8.5in" height="11in" viewBox="0 0 612 792"><rect x="10" y="10" width="50" '
       'height="50" fill="#f00"/></svg>' % NS)
XSS = ('<svg %s %s><text x="306" y="100" font-size="14" fill="#000">1/1</text>'
       '<text x="200" y="300" font-size="12" fill="#000">'
       '&lt;img src=x onerror="window.__pwned=1"&gt;&lt;script&gt;window.__pwned=1&lt;/script&gt;</text>'
       '<rect x="10" y="10" width="50" height="50" fill="#f00"/></svg>' % (NS, HEAD))


def api_errors(base, svg):
    req = urllib.request.Request(base + "/v1/lint", svg.encode(), {"Content-Type": "image/svg+xml"})
    return json.load(urllib.request.urlopen(req))["errors"]


class PageBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = make_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.srv.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def write(self, name, text):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w") as f:
            f.write(text)
        return p


class HttpTests(PageBase):
    def test_root_headers_and_disclaimer(self):
        r = urllib.request.urlopen(self.base + "/")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/html"))
        self.assertEqual(r.headers["X-Content-Type-Options"].lower(), "nosniff")
        csp = r.headers["Content-Security-Policy"]
        self.assertTrue(csp)
        self.assertNotIn("http://", csp)
        self.assertNotIn("https://", csp)
        self.assertNotIn("*", csp)
        for d in csp.split(";"):
            parts = d.split()
            if parts and parts[0] in ("script-src", "connect-src", "default-src"):
                for tok in parts[1:]:
                    self.assertTrue(tok.startswith("'") or tok in ("data:", "blob:"), d)
        self.assertIn(DISCLAIMER, r.read().decode())

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(self.base + "/nope")
        c.exception.close()
        self.assertEqual(c.exception.code, 404)


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class BrowserTests(PageBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        super().tearDownClass()

    def open(self, width=1200):
        ctx = self.browser.new_context(viewport={"width": width, "height": 800})
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.requests = []
        page.on("request", lambda r: page.requests.append(r.url))
        page.goto(self.base + "/")
        return page

    def upload(self, page, path):
        page.set_input_files("input[type=file]", path)
        page.click("button:not([disabled])")

    def wait_status(self, page, pred, timeout=15):
        end = time.time() + timeout
        txt = ""
        while time.time() < end:
            txt = page.inner_text("#status")
            if pred(txt):
                return txt
            time.sleep(0.1)
        self.fail("status never matched; last: %r" % txt)

    def test_no_outside_requests(self):
        page = self.open()
        self.upload(page, self.write("c.svg", CLEAN))
        self.wait_status(page, lambda t: "No formality errors found." in t)
        self.assertTrue(page.requests)
        for u in page.requests:
            self.assertTrue(u.startswith(self.base + "/"), u)

    def test_clean_sheet(self):
        page = self.open()
        self.upload(page, self.write("c.svg", CLEAN))
        self.wait_status(page, lambda t: "No formality errors found." in t)
        self.assertEqual(page.locator("#out tbody tr").count(), 0)
        self.assertIn(DISCLAIMER, page.inner_text("body"))

    def test_error_count_matches_api(self):
        n = api_errors(self.base, BAD)
        self.assertGreater(n, 0)
        page = self.open()
        self.upload(page, self.write("b.svg", BAD))
        txt = self.wait_status(page, lambda t: str(n) in t)
        self.assertNotIn("No formality errors found.", txt)
        self.assertGreaterEqual(page.locator("#out tbody tr").count(), n)

    def test_non_svg_failure_state(self):
        page = self.open()
        self.upload(page, self.write("x.svg", "hello"))
        txt = self.wait_status(page, lambda t: "not well-formed XML" in t)
        self.assertNotIn("No formality errors found.", txt)
        self.assertEqual(page.locator("#out tbody tr").count(), 0)

    def test_xss_rendered_literally(self):
        page = self.open()
        self.upload(page, self.write("x.svg", XSS.replace('font-size="12"', 'font-size="5"')))
        self.wait_status(page, lambda t: "No formality errors found." not in t and t.strip() != "")
        time.sleep(0.5)
        self.assertIn("<img src=x", page.inner_text("#out"))
        self.assertEqual(page.locator("#out img").count(), 0)
        self.assertEqual(page.locator("img").count(), 0)
        self.assertEqual(page.evaluate("typeof window.__pwned"), "undefined")

    def test_button_disabled_until_file(self):
        page = self.open()
        self.assertTrue(page.locator("button").first.is_disabled())
        page.set_input_files("input[type=file]", self.write("c.svg", CLEAN))
        self.assertFalse(page.locator("button").first.is_disabled())

    def test_oversize_refused_client_side(self):
        big = '<svg %s %s><!--%s--></svg>' % (NS, HEAD, "a" * (20 * 1024 * 1024 + 10))
        page = self.open()
        before = len(page.requests)
        page.set_input_files("input[type=file]", self.write("big.svg", big))
        btn = page.locator("button").first
        if not btn.is_disabled():
            btn.click()
        txt = self.wait_status(page, lambda t: "20" in t and "MB" in t.upper())
        self.assertNotIn("No formality errors found.", txt)
        self.assertEqual(len(page.requests), before)

    def test_mobile_no_horizontal_scroll(self):
        page = self.open(375)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 375)
        self.upload(page, self.write("b.svg", BAD))
        self.wait_status(page, lambda t: "No formality errors found." not in t and t.strip() != "")
        time.sleep(0.3)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 375)


if __name__ == "__main__":
    unittest.main()
