"""``fleetnotes serve``: the overlay and its two JSON files over HTTP, nothing else.

Exactly three paths answer: ``/`` (and ``/fleetnotes.html``) with the overlay,
``/notes.json`` and ``/fleet.json`` from the output folder. Anything else is
a 404, so the rest of that folder (and the rest of the disk) is never served.
It listens on 127.0.0.1 by default: only the streaming software on this
computer needs it.
"""

from __future__ import annotations

import os
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from fleetnotes.notes import FLEET_FILENAME, MANUAL_FILENAME

OVERLAY_FILENAME = "fleetnotes.html"


def overlay_path() -> str:
    """The overlay shipped inside the package."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay", OVERLAY_FILENAME)


class Handler(BaseHTTPRequestHandler):
    server_version = "fleetnotes"
    sys_version = ""

    def __init__(self, *args, routes: dict[str, tuple[str, str]], **kwargs):
        self.routes = routes
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # quiet: the console may be on stream
        pass

    def do_GET(self):
        self._answer(body=True)

    def do_HEAD(self):
        self._answer(body=False)

    def _answer(self, body: bool) -> None:
        route = self.routes.get(urlsplit(self.path).path)
        data = None
        if route is not None:
            try:
                with open(route[0], "rb") as f:
                    data = f.read()
            except OSError:
                data = None
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", route[1])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(data)


def make_server(host: str, port: int, output_dir: str, overlay: str | None = None) -> ThreadingHTTPServer:
    """A server for the overlay (``overlay``, default: the packaged one) and ``output_dir``'s JSON files."""
    page = (overlay or overlay_path(), "text/html; charset=utf-8")
    routes = {
        "/": page,
        f"/{OVERLAY_FILENAME}": page,
        f"/{MANUAL_FILENAME}": (os.path.join(output_dir, MANUAL_FILENAME), "application/json"),
        f"/{FLEET_FILENAME}": (os.path.join(output_dir, FLEET_FILENAME), "application/json"),
    }
    server_class = ThreadingHTTPServer
    if ":" in host:
        import socket

        class V6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_class = V6Server
    return server_class((host, port), partial(Handler, routes=routes))
