from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from time import sleep

hold_lock = Lock()
hold_started = False


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        global hold_started
        if self.path == "/hold":
            with hold_lock:
                first_request = not hold_started
                hold_started = True
            if first_request:
                sleep(20)
            self._respond(b"hold released\n")
            return
        if self.path == "/health":
            self._respond(b"healthy\n")
            return
        self._respond(b"crash window runtime\n")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
