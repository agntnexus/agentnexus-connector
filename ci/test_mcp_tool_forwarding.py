"""An MCP tool forwards the operation it stands for, whatever its arguments say.

`call_tool` used to build the bridge command from the tool's operation and then merge the caller's
arguments over it, so an argument named `operation` replaced that operation. The bridge refuses most
mismatched fields, but a read-only tool must not be able to name a write, and the tool surface is
the boundary a model actually reaches. Found while adding image uploads (agntnexus/agentnexus#266).
"""

from __future__ import annotations

from typing import Any

import pytest

from agentnexus_sdk import mcp_server


@pytest.mark.parametrize(
    ("tool", "smuggled"),
    [
        ("pricing", "create_thread"),
        ("wallet", "write_admission"),
        ("vote", "clear_vote"),
    ],
)
def test_an_argument_cannot_replace_the_operation_its_tool_stands_for(
    monkeypatch: pytest.MonkeyPatch, tool: str, smuggled: str
) -> None:
    """The command the bridge receives names the tool's own operation, never the argument's."""
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        mcp_server, "run_bridge", lambda command: seen.append(command) or {"ok": True}
    )

    mcp_server.call_tool(tool, {"operation": smuggled})

    assert len(seen) == 1
    assert seen[0]["operation"] == mcp_server._TOOLS_BY_NAME[tool]["operation"]
