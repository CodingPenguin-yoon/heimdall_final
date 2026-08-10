import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

secret = Path(os.environ["LOG_SECRET"]).read_text().strip()
print(f"stdout-secret={secret}", flush=True)
print(f"stderr-secret={secret}", file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/emit":
            print(f"live-stdout-secret={secret}", flush=True)
            print(f"live-stderr-secret={secret}", file=sys.stderr, flush=True)
        body = b"healthy\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
