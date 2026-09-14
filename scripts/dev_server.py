"""Local dev server for this site.

Static server that mirrors vercel.json's rewrites/redirects, so the five
clean URLs and the legacy redirects can be tested locally exactly as they'll
behave in production.

A plain `python -m http.server` cannot serve /about, /data, /map or
/methodology - those paths are rewrites, not files - so use this instead.

Run: python scripts/dev_server.py [port]   (default 8232)
"""
import sys
from pathlib import Path
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = str(Path(__file__).resolve().parent.parent)
ROUTES = {"/", "/map", "/about", "/data", "/methodology"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def redirect(self, to):
        self.send_response(308)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # --- redirects (mirror of vercel.json) ---
        legacy = {"/about.html": "/about", "/data.html": "/data",
                  "/methodology.html": "/methodology", "/index.html": "/"}
        if path in legacy:
            return self.redirect(legacy[path])
        if path == "/map.html":
            target = "/map" if ("explore" in qs or "cam" in qs) else "/"
            if parsed.query:
                target += "?" + parsed.query
            return self.redirect(target)

        # --- rewrites: the five routes all serve the shell ---
        if path in ROUTES:
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, *a):
        pass


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8232
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
