"""A tiny HTTP server for standing in for Sonarr, Plex, Jellyfin or a judge.

Routes map (method, path) to a callable taking the request and returning
(status, body). Every request is recorded so a test can check what was sent.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


@dataclass
class Request:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body or b"null")


@dataclass
class Stub:
    routes: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def route(self, method: str, path: str, reply) -> None:
        """`reply` is a value (sent as JSON with 200) or a callable(req)."""
        self.routes[(method.upper(), path)] = reply

    def seen(self, path: str) -> list[Request]:
        return [r for r in self.requests if r.path == path]

    def start(self) -> "Stub":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                req = Request(self.command, parts.path, parse_qs(parts.query),
                              {k.lower(): v for k, v in self.headers.items()},
                              self.rfile.read(length) if length else b"")
                stub.requests.append(req)
                reply = stub.routes.get((self.command, parts.path))
                if reply is None:
                    status, body = 404, {"error": "no route"}
                elif callable(reply):
                    status, body = reply(req)
                else:
                    status, body = 200, reply
                if isinstance(body, (bytes, bytearray)):
                    data, kind = bytes(body), "application/octet-stream"
                else:
                    data, kind = json.dumps(body).encode(), "application/json"
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

            def log_message(self, *args):  # noqa: D401 - quiet
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
