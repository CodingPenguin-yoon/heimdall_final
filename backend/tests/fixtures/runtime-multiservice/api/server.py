import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def ready() -> bool:
    secret = Path(os.environ["APP_SECRET"]).read_text().strip()
    password = Path(os.environ["DATABASE_PASSWORD_FILE"]).read_text().strip()
    if secret != "runtime-user-secret-canary" or not password:
        return False
    with socket.create_connection(
        (os.environ["DATABASE_HOST"], int(os.environ["DATABASE_PORT"])), timeout=2
    ):
        return True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        healthy = ready()
        status = 200 if healthy else 503
        body = b"api service\n" if healthy else b"not ready\n"
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
