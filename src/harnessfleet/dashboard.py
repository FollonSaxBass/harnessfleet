"""Loopback-only dashboard, independent of execution supervisors."""

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit
from .contracts import FleetError
from .graph import Graph
from .worker import redact


def server(manager, port=8421):
    token = secrets.token_urlsafe(24)
    graph = Graph(manager)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def valid_host(self):
            return self.headers.get("Host") in (
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            )

        def send(self, status, data, content_type="application/json"):
            payload = data.encode() if isinstance(data, str) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self.valid_host():
                self.send(403, {"error": "host"})
                return
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/api/graph":
                    self.send(
                        200,
                        graph.page(
                            q.get("parent", [None])[0],
                            int(q.get("offset", [0])[0]),
                            int(q.get("limit", [50])[0]),
                            q.get("query", [""])[0],
                        ),
                    )
                elif url.path == "/api/output":
                    result = graph.output(q.get("id", [""])[0])
                    result["text"] = redact(result["text"])
                    self.send(200, result)
                elif url.path == "/api/runs":
                    self.send(200, manager.list(int(q.get("offset", [0])[0])))
                elif url.path == "/api/events":
                    self.send(
                        200,
                        {
                            "schema_version": 1,
                            "events": manager.store.events(q.get("id", [""])[0], int(q.get("after", [0])[0])),
                        },
                    )
                elif url.path == "/api/session":
                    self.send(200, {"schema_version": 1, "csrf": token})
                elif url.path in ("/", "/app.js", "/style.css"):
                    name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[url.path]
                    mime = {
                        "/": "text/html; charset=utf-8",
                        "/app.js": "text/javascript; charset=utf-8",
                        "/style.css": "text/css",
                    }[url.path]
                    self.send(200, files("harnessfleet").joinpath("assets", name).read_text(), mime)
                else:
                    self.send(404, {"error": "not_found"})
            except (FleetError, ValueError) as e:
                self.send(
                    400,
                    {
                        "error": {
                            "code": e.code if isinstance(e, FleetError) else "request",
                            "message": str(e) if isinstance(e, FleetError) else "Invalid request",
                        }
                    },
                )

        def do_POST(self):
            origin = self.headers.get("Origin")
            valid_origin = origin in (
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            )
            if (
                not self.valid_host()
                or not valid_origin
                or not secrets.compare_digest(self.headers.get("X-HarnessFleet-Token", ""), token)
            ):
                self.send(403, {"error": "origin"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 4096:
                    raise ValueError()
                data = json.loads(self.rfile.read(length))
                if self.path == "/api/stop":
                    self.send(200, manager.stop(data["id"]))
                elif self.path == "/api/cancel":
                    self.send(200, manager.cancel(data["id"]))
                else:
                    self.send(404, {"error": "not_found"})
            except (FleetError, ValueError, KeyError) as e:
                self.send(
                    400,
                    {
                        "error": {
                            "code": getattr(e, "code", "request"),
                            "message": str(e) if isinstance(e, FleetError) else "Invalid request",
                        }
                    },
                )

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
