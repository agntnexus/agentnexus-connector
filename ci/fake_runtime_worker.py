"""A second Arena runtime for the conformance suite (agntnexus/agentnexus#228).

It is not Hermes and not any real runtime: a small, standalone decision worker that speaks the Arena
worker protocol, with a behaviour a test chooses. It holds no model, no provider and no credential;
it exists to show that the Arena state machine, its deadlines, its cleanup and its isolation do not
depend on which runtime sits behind a driver.

Environment (set by the fake driver, never by the Connector): FAKE_RUNTIME_BEHAVIOR (a JSON file),
FAKE_RUNTIME_HOME (its throwaway home) and FAKE_RUNTIME_PROFILE (the profile it may only read).

Behaviours: `mode` is `block` (a model that never answers), `slow` (answers after `seconds`) or
`fast`; `tail: hang` makes it wait for a closing request after a tool result, as some runtimes do;
`close` is `ok`, `hang` or `raise` for its cleanup; `tools` overrides what its preflight reports.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

BEHAVIOR = json.loads(Path(os.environ["FAKE_RUNTIME_BEHAVIOR"]).read_text(encoding="utf-8"))
HOME = Path(os.environ["FAKE_RUNTIME_HOME"])
PROFILE = Path(os.environ["FAKE_RUNTIME_PROFILE"])
TOOLS = ["game_join", "game_move", "game_state"]


def send(document: dict[str, Any]) -> None:
    """Write one protocol line."""
    sys.stdout.write(json.dumps(document) + "\n")
    sys.stdout.flush()


def forever() -> None:
    """Block as a model transport or a cleanup that never answers; only a kill ends it."""
    left, _right = socket.socketpair()
    left.recv(1)


def record(path: str | None, text: str) -> None:
    """Append a line to a file the test reads, outside everything the run may touch."""
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def scaffold() -> None:
    """Fill its own home the way a real runtime was measured to, and read its credentials."""
    for name in ("cache", "logs", "sessions", "state"):
        (HOME / name).mkdir(parents=True, exist_ok=True)
    for name in ("state/db", "logs/agent.log"):
        with open(HOME / name, "ab") as handle:
            handle.write(b"fake-runtime\n")
    record(BEHAVIOR.get("record"), str(HOME))
    # The credentials are the profile's own and are only read, from the file named .env.
    (PROFILE / ".env").read_text(encoding="utf-8")
    record(BEHAVIOR.get("env_reads"), str(PROFILE / ".env"))


def tether() -> None:
    """Hold a socket open while this process lives, so the test can tell when it is gone."""
    port = BEHAVIOR.get("tether")
    if not port:
        return
    connection = socket.create_connection(("127.0.0.1", port))

    def watch() -> None:
        with contextlib.suppress(OSError):
            connection.recv(1)
        os._exit(1)

    threading.Thread(target=watch, daemon=True).start()


incoming: queue.Queue[str] = queue.Queue()


def pump() -> None:
    """End with the match process: a closed pipe means it is gone and nothing may outlive it."""
    for line in sys.stdin:
        incoming.put(line)
    os._exit(3)


def request(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Put one tool request to the match process and wait for its answer."""
    send({"operation": operation, **arguments})
    reply: dict[str, Any] = json.loads(incoming.get())
    return reply


def decide() -> bool:
    """Make one decision; return whether the match process told it the decision is complete."""
    mode = BEHAVIOR["mode"]
    if mode == "block":
        forever()
    if mode == "slow":
        time.sleep(BEHAVIOR["seconds"])
    if mode in ("fast", "slow"):
        if request("game_move", BEHAVIOR["arguments"]) == {"complete": True}:
            return True
        if BEHAVIOR.get("tail") == "hang":
            forever()
    send({"decision": "returned", "outcome": "ok"})
    return False


def main() -> int:
    """Preflight, or serve decisions until stopped."""
    if sys.argv[1:] == ["--preflight"]:
        send({"bounded": True, "tools": BEHAVIOR.get("tools", TOOLS)})
        return 0
    scaffold()
    tether()
    threading.Thread(target=pump, daemon=True).start()
    send({"ready": True})
    while True:
        command = json.loads(incoming.get())
        if command == {"stop": True}:
            return 0
        if decide():
            send({"decision": "completed"})
        how = BEHAVIOR.get("close", "ok")
        if how == "hang":
            forever()
        send({"decision": "close_failed" if how == "raise" else "closed"})


if __name__ == "__main__":
    raise SystemExit(main())
