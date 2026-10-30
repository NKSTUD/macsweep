"""MacSweep desktop app: a local web UI served on 127.0.0.1 and opened in your browser."""
import json
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import core

TOKEN = secrets.token_hex(16)  # blocks requests coming from other websites
PAGE = (Path(__file__).with_name("ui.html")).read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)


def _results():
    res = core.results()
    cats = [c | {"items": res[c["id"]]} for c in core.categories()]
    return {"cats": cats, "protected": core.protected(), "disk": core.disk(), "phase": core.STATE["phase"]}


ACTIONS = {
    "init": lambda b: {"home": str(core.HOME), "disk": core.disk(), "categories": core.categories(), "status": core.status()},
    "start_scan": lambda b: core.start_scan() and core.status(),
    "status": lambda b: core.status(),
    "cancel": lambda b: core.cancel_scan() or core.status(),
    "results": lambda b: _results(),
    "trash": lambda b: core.move_to_trash(b.get("paths", [])) | {"disk": core.disk()},
    "empty_trash": lambda b: core.empty_trash() | {"disk": core.disk()},
    "reveal": lambda b: {"ok": core.reveal(b.get("path", ""))},
    "protect": lambda b: {"protected": core.protect(b.get("path", ""))},
    "unprotect": lambda b: {"protected": core.unprotect(b.get("path", ""))},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/":
            return self._send(404, {"error": "not found"})
        self._send(200, PAGE, "text/html")

    def do_POST(self):
        if self.path != "/api" or self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid json"})
        fn = ACTIONS.get(body.get("action"))
        if not fn:
            return self._send(400, {"error": "unknown action"})
        self._send(200, fn(body) or core.status())


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port 0 = any free port
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"MacSweep is running at {url}  (Ctrl+C to quit)", flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        core.cancel_scan()


if __name__ == "__main__":
    main()
