"""Spec-driven tests for the drawlint HTTP server. Real subprocess, real sockets, no mocks."""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = 'xmlns="http://www.w3.org/2000/svg"'
SECRET = "SECRETDRAWINGTEXT7731"
NAME = "confidential-name-9921.svg"


def sheet(num="1/1", head='width="8.5in" height="11in" viewBox="0 0 612 792"', extra=""):
    return ('<svg %s %s><text x="306" y="100" font-size="14" fill="#000">%s</text>'
            '<text x="200" y="300" font-size="12" fill="#000">%s</text>%s</svg>' % (NS, head, num, SECRET, extra))


ENTITY = '<!DOCTYPE svg [<!ENTITY a "b">]><svg %s width="8.5in" height="11in">&a;</svg>' % NS


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = os.path.join(cls.tmp.name, "usage.jsonl")
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "drawlint.server", "--port", "0", "--usage-log", cls.log],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, CUDA_VISIBLE_DEVICES="", PYTHONUNBUFFERED="1"))
        line = cls.proc.stdout.readline()
        assert "listening on http://127.0.0.1:" in line, line
        cls.port = int(line.strip().rsplit(":", 1)[1].split("/")[0])
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.proc.stdout.close()
        cls.proc.stderr.close()
        cls.tmp.cleanup()

    def log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [l for l in f.read().split("\n") if l.strip()]

    def req(self, method, path, body=None, ctype=None):
        r = urllib.request.Request(self.base + path, data=body, method=method)
        if ctype:
            r.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()

    def post_svg(self, text, path="/v1/lint"):
        return self.req("POST", path, text.encode(), "image/svg+xml")

    def post_json(self, obj, path="/v1/lint", raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        return self.req("POST", path, body, "application/json")


class Basics(ServerCase):
    def test_healthz(self):
        st, body = self.req("GET", "/healthz")
        self.assertEqual(st, 200)
        n = len(self.log_lines())
        self.req("GET", "/healthz")
        self.assertEqual(len(self.log_lines()), n)

    def test_raw_svg(self):
        st, body = self.post_svg(sheet())
        self.assertEqual(st, 200)
        rep = json.loads(body)
        self.assertEqual(rep["disclaimer"], "Automated formality check only, not legal advice.")
        self.assertEqual(len(rep["sheets"]), 1)

    def test_raw_svg_charset_content_type(self):
        st, body = self.req("POST", "/v1/lint", sheet().encode(), "image/svg+xml; charset=utf-8")
        self.assertEqual(st, 200)

    def test_profile_query(self):
        st, body = self.post_svg(sheet(), "/v1/lint?profile=a4")
        self.assertEqual(st, 200)
        codes = [f["code"] for f in json.loads(body)["sheets"][0]["findings"]]
        self.assertIn("page-size", codes)
        st, body = self.post_svg(sheet(), "/v1/lint?profile=letter")
        codes = [f["code"] for f in json.loads(body)["sheets"][0]["findings"]]
        self.assertNotIn("page-size", codes)

    def test_json_multisheet(self):
        st, body = self.post_json({"sheets": [{"name": NAME, "svg": sheet("1/2")},
                                              {"name": "b.svg", "svg": sheet("2/2")}],
                                   "numerals": ["10"], "profile": "auto"})
        self.assertEqual(st, 200)
        rep = json.loads(body)
        self.assertEqual(len(rep["sheets"]), 2)
        codes = [f["code"] for f in rep.get("set_findings", [])]
        self.assertIn("numeral-missing", codes + [f["code"] for s in rep["sheets"] for f in s["findings"]])
        self.assertNotIn("sheet-number-sequence", codes)

    def test_json_sequence_failure(self):
        st, body = self.post_json({"sheets": [{"name": "a", "svg": sheet("1/2")},
                                              {"name": "b", "svg": sheet("1/2")}]})
        self.assertEqual(st, 200)
        rep = json.loads(body)
        allc = [f["code"] for f in rep.get("set_findings", [])] + [f["code"] for s in rep["sheets"] for f in s["findings"]]
        self.assertIn("sheet-number-sequence", allc)
        self.assertFalse(rep["pass"])

    def test_404(self):
        self.assertEqual(self.req("GET", "/nope")[0], 404)
        self.assertEqual(self.post_svg(sheet(), "/v1/other")[0], 404)

    def test_400_bad_json(self):
        self.assertEqual(self.post_json(None, raw=b"{not json")[0], 400)

    def test_400_empty_sheets(self):
        self.assertEqual(self.post_json({"sheets": []})[0], 400)

    def test_400_missing_sheets(self):
        self.assertEqual(self.post_json({"foo": 1})[0], 400)

    def test_415(self):
        self.assertEqual(self.req("POST", "/v1/lint", sheet().encode(), "text/plain")[0], 415)

    def test_415_no_content_type(self):
        st, _ = self.req("POST", "/v1/lint", sheet().encode(), None)
        self.assertIn(st, (415, 400))

    def test_422_entity_raw(self):
        self.assertEqual(self.post_svg(ENTITY)[0], 422)

    def test_422_entity_json(self):
        self.assertEqual(self.post_json({"sheets": [{"name": "a", "svg": ENTITY}]})[0], 422)

    def test_422_not_xml(self):
        self.assertEqual(self.post_svg("this is not xml")[0], 422)

    def test_422_no_size(self):
        self.assertEqual(self.post_svg('<svg %s><rect width="1" height="1"/></svg>' % NS)[0], 422)

    def test_411(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        c.putrequest("POST", "/v1/lint")
        c.putheader("Content-Type", "image/svg+xml")
        c.endheaders()
        resp = c.getresponse()
        self.assertEqual(resp.status, 411)
        c.close()

    def test_413_over_100_sheets(self):
        s = sheet()
        st, _ = self.post_json({"sheets": [{"name": "s%d" % i, "svg": s} for i in range(101)]})
        self.assertEqual(st, 413)

    def test_100_sheets_ok(self):
        s = sheet()
        st, body = self.post_json({"sheets": [{"name": "s%d" % i, "svg": s} for i in range(100)]})
        self.assertEqual(st, 200)

    def test_413_over_20mb(self):
        # declare a 21 MB body; the server must answer 413 from the header alone
        import socket
        n = 21 * 1024 * 1024
        with socket.create_connection(("127.0.0.1", self.port), timeout=30) as so:
            so.sendall(("POST /v1/lint HTTP/1.1\r\nHost: x\r\nContent-Type: image/svg+xml\r\n"
                        "Content-Length: %d\r\n\r\n" % n).encode())
            so.sendall(b" " * 4096)
            data = so.recv(4096)
        self.assertTrue(data.startswith(b"HTTP/1.") and b" 413 " in data.split(b"\r\n")[0], data[:80])


class UsageLog(ServerCase):
    def test_log_semantics(self):
        # failed requests write nothing
        before = len(self.log_lines())
        self.post_svg(ENTITY)
        self.post_json(None, raw=b"{bad")
        self.post_json({"sheets": []})
        self.req("POST", "/v1/lint", b"x", "text/plain")
        self.req("GET", "/nope")
        self.req("GET", "/healthz")
        self.assertEqual(len(self.log_lines()), before)

        # one line per success, with exactly the stated keys
        self.post_svg(sheet())
        self.assertEqual(len(self.log_lines()), before + 1)
        self.post_json({"sheets": [{"name": NAME, "svg": sheet("1/2")}, {"name": "b", "svg": sheet("2/2")}]})
        lines = self.log_lines()
        self.assertEqual(len(lines), before + 2)
        ev1, ev2 = json.loads(lines[-2]), json.loads(lines[-1])
        for ev in (ev1, ev2):
            self.assertEqual(set(ev), {"time", "event", "sheets", "request_id"})
            self.assertEqual(ev["event"], "sheet-linted")
        self.assertEqual(ev1["sheets"], 1)
        self.assertEqual(ev2["sheets"], 2)
        self.assertNotEqual(ev1["request_id"], ev2["request_id"])

        # no drawing content, names or client addresses anywhere in the log
        with open(self.log, encoding="utf-8") as f:
            raw = f.read()
        for needle in (SECRET, NAME, "<svg", "127.0.0.1", "confidential"):
            self.assertNotIn(needle, raw)


if __name__ == "__main__":
    unittest.main()
