"""A fake chat-completions model on loopback for the OpenClaw Arena tests (#228).

It stands where a runtime's provider would stand. It answers each request by a plan, one entry per
request index and the last one repeating: `move` answers with one `game_move` tool call, `state`
with a `game_state` call, `text` with closing prose, `hang` accepts and never answers. It records
what a transport may see - the tool names offered, the message roles, the scheme of the bearer - and
never a body or a key.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

END = b"\n\n"


class FakeChatModel:
    """A loopback server with a plan and a record of the requests it received."""

    def __init__(self, plan: str = "move", arguments: dict[str, Any] | None = None) -> None:
        """Listen on a free loopback port; `arguments` is what the model's move says."""
        self.plan = plan.split(",")
        self.arguments = arguments or {"move": "e2e4"}
        self.requests: list[dict[str, Any]] = []
        #: Called with the request's index, in the server thread, before the model answers it.
        self.before_request: Callable[[int], None] | None = None
        self.stopped = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                """Say nothing."""

            def do_POST(self) -> None:
                """Record what a transport may see, then answer by plan."""
                length = int(self.headers.get("content-length", "0"))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                names = []
                for tool in body.get("tools") or []:
                    function = tool.get("function") or tool
                    names.append(str(function.get("name")))
                index = len(owner.requests)
                owner.requests.append(
                    {
                        "path": self.path,
                        "model": body.get("model"),
                        "tools": sorted(names),
                        "roles": [m.get("role") for m in body.get("messages", [])],
                        "scheme": (self.headers.get("authorization") or "").partition(" ")[0],
                    }
                )
                if owner.before_request is not None:
                    owner.before_request(index)
                mode = owner.plan[min(index, len(owner.plan) - 1)]
                if mode == "hang":
                    owner.stopped.wait(3600)
                    return
                wanted = {"move": "game_move", "state": "game_state"}.get(mode)
                resolved = next((n for n in names if wanted and n.endswith(wanted)), None)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("connection", "close")
                self.end_headers()
                if resolved:
                    arguments = json.dumps(owner.arguments) if wanted == "game_move" else "{}"
                    self._chunk(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_{index}",
                                    "type": "function",
                                    "function": {"name": resolved, "arguments": ""},
                                }
                            ],
                        }
                    )
                    self._chunk(
                        {"tool_calls": [{"index": 0, "function": {"arguments": arguments}}]}
                    )
                    self._chunk({}, "tool_calls")
                else:
                    self._chunk({"role": "assistant", "content": "done"})
                    self._chunk({}, "stop")
                self.wfile.write(b"data: [DONE]" + END)
                self.wfile.flush()

            def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> None:
                payload = {
                    "id": "x",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "fake-model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                if finish:
                    payload["usage"] = {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                self.wfile.write(b"data: " + json.dumps(payload).encode() + END)
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def provider(self) -> dict[str, Any]:
        """Return the provider entry of a profile configuration that points at this model."""
        return {
            "baseUrl": self.url,
            "apiKey": "synthetic-not-a-secret",
            "api": "openai-completions",
            "models": [
                {
                    "id": "fake-model",
                    "name": "Fake Model",
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 32000,
                    "maxTokens": 2048,
                }
            ],
        }

    def profile_config(self) -> dict[str, Any]:
        """Return a whole profile configuration: this model as the profile's route."""
        return {
            "agents": {"defaults": {"model": {"primary": "fakeprov/fake-model"}}},
            "models": {"mode": "replace", "providers": {"fakeprov": self.provider()}},
        }

    def close(self) -> None:
        """Stop listening and let a hanging request go."""
        self.stopped.set()
        self.server.shutdown()
        self.server.server_close()
        time.sleep(0)
