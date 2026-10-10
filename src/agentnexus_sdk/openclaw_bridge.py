"""The three-tool MCP bridge an Arena decision hands its runtime (agntnexus/agentnexus#228).

OpenClaw starts this program itself, as the one MCP server of a throwaway configuration, so it lives
in the runtime's process tree and speaks the MCP stdio transport to it. It knows exactly three
operations and nothing else: no signing key, no profile, no match, no URL. Every call is relayed,
unchanged but for being length-checked, to the Arena decision worker over an authenticated local
channel whose address and one-time key arrive in this process's configured environment; the worker
validates it again, answers for the one match it serves, and alone talks to the supervisor.

Standalone and stdlib only: it runs under whatever interpreter the throwaway configuration names.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from multiprocessing.connection import Client
from typing import Any

PROTOCOL = "2025-06-18"
ADDRESS_ENV = "ARENA_BRIDGE_ADDRESS"
KEY_ENV = "ARENA_BRIDGE_KEY"
#: The longest the worker may take to answer one call: its own bound is far below this.
ANSWER_SECONDS = 120.0
NO_CHANNEL = "The Arena relay is not available."

EMPTY: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "game_join",
        "description": "Join only the assigned match and seat.",
        "inputSchema": EMPTY,
    },
    {
        "name": "game_state",
        "description": "Read only the assigned game; waits briefly between reads.",
        "inputSchema": EMPTY,
    },
    {
        "name": "game_move",
        "description": (
            "Make one legal move in only the assigned game: a column in Connect Four, "
            "a move in UCI and optionally a listed draw claim in Chess."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "column": {"type": "integer", "minimum": 0, "maximum": 6},
                "move": {"type": "string", "pattern": "^[a-h][1-8][a-h][1-8][qrbn]?$"},
                "claim": {"enum": ["fifty_moves", "threefold_repetition"]},
            },
            "additionalProperties": False,
        },
    },
]
NAMES = frozenset(tool["name"] for tool in TOOLS)


class Relay:
    """The authenticated channel to the decision worker, opened on the first call."""

    def __init__(self) -> None:
        """Read the one-time channel from the environment; nothing is opened yet."""
        self.address = os.environ.get(ADDRESS_ENV)
        self.key = os.environ.get(KEY_ENV)
        self.connection: Any = None

    def ask(self, name: str, arguments: Any) -> tuple[str, bool]:
        """Relay one call; return its text and whether it is an error."""
        if not self.address or not self.key:
            return NO_CHANNEL, True
        try:
            if self.connection is None:
                self.connection = Client(self.address, authkey=bytes.fromhex(self.key))
            self.connection.send_bytes(json.dumps({"op": name, "arguments": arguments}).encode())
            if not self.connection.poll(ANSWER_SECONDS):
                self.close()
                return NO_CHANNEL, True
            answer = json.loads(self.connection.recv_bytes(65536))
        except (OSError, ValueError, EOFError):
            self.close()
            return NO_CHANNEL, True
        text = answer.get("text") if isinstance(answer, dict) else None
        if not isinstance(text, str):
            return NO_CHANNEL, True
        return text[:60000], bool(answer.get("error"))

    def close(self) -> None:
        """Drop the channel; the next call opens it again."""
        if self.connection is not None:
            with contextlib.suppress(OSError):
                self.connection.close()
            self.connection = None


def reply(identifier: Any, result: Any) -> None:
    """Write one JSON-RPC result line."""
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}) + "\n")
    sys.stdout.flush()


def fail(identifier: Any, code: int, message: str) -> None:
    """Write one JSON-RPC error line."""
    document = {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}
    sys.stdout.write(json.dumps(document) + "\n")
    sys.stdout.flush()


def serve(lines: Any) -> int:
    """Answer MCP requests until the runtime closes the pipe."""
    relay = Relay()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        method, identifier = message.get("method"), message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            reply(
                identifier,
                {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agentnexus-arena", "version": "1"},
                },
            )
        elif method == "tools/list":
            reply(identifier, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            if name not in NAMES:
                fail(identifier, -32602, "Unknown tool.")
                continue
            text, error = relay.ask(str(name), params.get("arguments") or {})
            reply(identifier, {"content": [{"type": "text", "text": text}], "isError": error})
        elif method == "ping":
            reply(identifier, {})
        elif identifier is not None:
            fail(identifier, -32601, "Method not found.")
    relay.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve(sys.stdin))
