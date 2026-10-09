"""Shared helpers of the OpenClaw Arena tests (agntnexus/agentnexus#228)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from arena_process_harness import openclaw_stand_in
from fake_chat_model import FakeChatModel

from agentnexus_sdk import arena_driver_openclaw, openclaw_arena


def make_private(path: Path) -> None:
    """Make a profile directory the user's alone: mode 0700, or an access list of the user only."""
    path.chmod(0o700)
    if sys.platform == "win32":
        sid = openclaw_arena.windows_current_sid()
        subprocess.run(  # noqa: S603 - fixed system tool on a test directory
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],  # noqa: S607
            check=True,
            capture_output=True,
        )


def pid_alive(pid: int) -> bool:
    """Return whether a process with this pid still runs."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return code.value == 259  # STILL_ACTIVE


def behavior_of(
    server: FakeChatModel, tmp_path: Path, fault: str = "", **env: str
) -> dict[str, Any]:
    """Return the stand-in's behaviour: the profile's route, and its own knobs."""
    return {
        "profile": server.profile_config(),
        "env": {
            "FAKE_OPENCLAW_RECORD": str(tmp_path / "openclaw.stand-in-record"),
            "FAKE_OPENCLAW_FAULT": fault,
            "FAKE_OPENCLAW_OUTSIDE": str(tmp_path / "outside.stand-in-record"),
            **env,
        },
    }


def records_of(tmp_path: Path) -> list[dict[str, Any]]:
    """Return what the stand-in's processes recorded."""
    path = tmp_path / "openclaw.stand-in-record"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def survivors(tmp_path: Path, wait: float = 10.0) -> list[int]:
    """Return the pids of the stand-in's processes that still run after a grace period."""
    pids = sorted({r["pid"] for r in records_of(tmp_path) if isinstance(r.get("pid"), int)})
    pids = [pid for pid in pids if pid != os.getpid()]
    end = time.monotonic() + wait
    while time.monotonic() < end and any(pid_alive(pid) for pid in pids):
        time.sleep(0.2)
    return [pid for pid in pids if pid_alive(pid)]


def stand_in_handle(
    tmp_path: Path,
    server: FakeChatModel,
    fault: str = "",
    profile: dict[str, Any] | None = None,
) -> arena_driver_openclaw.OpenClawRun:
    """Return the handle of a stand-in installation with a disposable profile."""
    behavior = behavior_of(server, tmp_path, fault)
    if profile is not None:
        behavior["profile"] = profile
    command, config, state = openclaw_stand_in(tmp_path, behavior)
    return arena_driver_openclaw.OpenClawRun(command, "2026.9.9", config, state, config.parent)
