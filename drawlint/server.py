"""drawlint HTTP endpoint (stdlib). Local by default; no billing, no keys.

POST /v1/lint            body: one SVG (Content-Type image/svg+xml), one PDF (application/pdf,
                         one sheet per page), or JSON {"sheets": [{"name": "...", "svg": "..."} or
                         {"name": "...", "pdf_base64": "..."}], "numerals": ["10"], "profile": "auto"}
                         query ?profile=auto|letter|a4 for the SVG and PDF forms
GET  /healthz            {"ok": true, "version": ...}
GET  /                   free one-sheet checker page (static/index.html)

Each successful lint appends one usage event per sheet to --usage-log (JSON lines:
time, event "sheet-linted", sheets, request id). No drawing content, file names or
client addresses are kept. Billing adapters read
this event name later; nothing here charges anyone.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import engine, intake

MAX_BODY = engine.MAX_BYTES
MAX_SHEETS = 100
PAGE = Path(__file__).with_name("static") / "index.html"


class LintHandler(BaseHTTPRequestHandler):
    server_version = f"drawlint/{engine.VERSION}"
    usage_log = None
    _lock = threading.Lock()

    def log_message(self, fmt, *args):  # no client addresses in logs
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg, "disclaimer": engine.DISCLAIMER})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, {"ok": True, "version": engine.VERSION})
        if path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        self._err(404, "not found")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/v1/lint":
            return self._err(404, "not found")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._err(411, "Content-Length required")
        if n <= 0:
            return self._err(400, "empty body")
        if n > MAX_BODY:
            return self._err(413, f"body larger than {MAX_BODY} bytes")
        body = self.rfile.read(n)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            if ctype == "application/json":
                try:
                    req = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return self._err(400, "body is not valid JSON")
                if not isinstance(req, dict):
                    return self._err(400, "JSON body must be an object")
                sheets = req.get("sheets")
                if not isinstance(sheets, list) or not sheets:
                    return self._err(400, "'sheets' must be a non-empty list")
                if len(sheets) > MAX_SHEETS:
                    return self._err(413, f"at most {MAX_SHEETS} sheets per request")
                profile = req.get("profile", "auto")
                numerals = req.get("numerals")
                if numerals is not None and (not isinstance(numerals, list)
                                             or not all(isinstance(x, (str, int)) for x in numerals)):
                    return self._err(400, "'numerals' must be a list of strings")
                items = []
                for i, s in enumerate(sheets):
                    if isinstance(s, dict) and isinstance(s.get("svg"), str):
                        items.append((str(s.get("name") or f"sheet-{i + 1}.svg"), s["svg"]))
                    elif isinstance(s, dict) and isinstance(s.get("pdf_base64"), str):
                        try:
                            raw = base64.b64decode(s["pdf_base64"], validate=True)
                        except (binascii.Error, ValueError):
                            return self._err(400, f"sheet {i}: 'pdf_base64' is not valid base64")
                        items.append((str(s.get("name") or f"sheet-{i + 1}.pdf"), raw))
                    else:
                        return self._err(400, f"sheet {i}: needs a string 'svg' or 'pdf_base64'")
            elif ctype in ("image/svg+xml", "application/xml", "text/xml", "application/pdf"):
                profile = parse_qs(url.query).get("profile", ["auto"])[0]
                numerals = None
                items = [("sheet.pdf" if ctype == "application/pdf" else "sheet.svg", body)]
            else:
                return self._err(415, "send image/svg+xml, application/pdf or application/json")
            if profile not in ("auto", "letter", "a4"):
                return self._err(400, "profile must be auto, letter or a4")
            reports = [r for name, data in items for r in intake.lint_file(data, name=name, profile=profile)]
            if len(reports) > MAX_SHEETS:
                return self._err(413, f"at most {MAX_SHEETS} sheets per request")
        except engine.TooManySheets as e:
            return self._err(413, str(e))
        except engine.InputError as e:
            return self._err(422, str(e))
        rep = engine.report(reports, engine.lint_set(reports, [str(x) for x in numerals] if numerals else None))
        rid = uuid.uuid4().hex
        rep["request_id"] = rid
        if self.usage_log:
            line = json.dumps({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "event": "sheet-linted", "sheets": len(reports), "request_id": rid})
            with self._lock, open(self.usage_log, "a") as f:
                f.write(line + "\n")
        self._send(200, rep)


def make_server(host="127.0.0.1", port=8787, usage_log=None):
    handler = type("Handler", (LintHandler,), {"usage_log": usage_log})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="drawlint-server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--usage-log", help="append usage events (JSON lines) here")
    a = ap.parse_args(argv)
    srv = make_server(a.host, a.port, a.usage_log)
    print(f"drawlint {engine.VERSION} listening on http://{a.host}:{srv.server_address[1]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
