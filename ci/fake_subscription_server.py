"""A fake subscription model transport for the Arena tests (agntnexus/agentnexus#228).

It stands where a runtime's subscription backend would stand and speaks a deliberately tiny
protocol of its own: `POST <base>/responses` with a bearer token answers with one tool call. It
records what a transport may see - the path, the scheme and a digest of the bearer, the tool names
the runtime offered - and never a body or a token, so a test can show that the grant reached it
through the runtime and went nowhere else.

Modes: `move` answers with a `game_move` call, `hang` accepts and never answers, `unauthorized`
answers 401.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeSubscription:
    """A loopback server with a mode and a record of the requests it received."""

    def __init__(self, mode: str = "move", arguments: dict[str, Any] | None = None) -> None:
        """Listen on a free loopback port; `arguments` is what the model's move says."""
        self.mode = mode
        self.arguments = arguments or {"move": "e2e4"}
        self.requests: list[dict[str, Any]] = []
        self.stopped = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                """Say nothing."""

            def do_POST(self) -> None:
                """Record what a transport may see, then answer by mode."""
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
                scheme, _, token = self.headers.get("authorization", "").partition(" ")
                owner.requests.append(
                    {
                        "path": self.path,
                        "scheme": scheme,
                        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "tools": sorted(body.get("tools", [])),
                        "model": body.get("model"),
                    }
                )
                if owner.mode == "hang":
                    owner.stopped.wait(3600)
                    return
                if owner.mode == "unauthorized":
                    self.send_response(401)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                answer = json.dumps(
                    {"function_call": {"name": "game_move", "arguments": owner.arguments}}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/backend-api/subscription"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        """Stop listening and let a hanging request go."""
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        time.sleep(0)
