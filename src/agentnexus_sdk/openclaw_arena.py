"""The OpenClaw decision worker, its process guard and the throwaway configuration it builds.

agntnexus/agentnexus#228. One Arena decision is one `openclaw agent exec` run, started by this
worker under the Connector's own interpreter and never in the profile: OpenClaw gets a throwaway
state directory, a throwaway home and an overlay configuration that includes the profile's own
configuration read-only and then closes it to exactly three tools. Which provider, model and
authentication the profile has is OpenClaw's business; this program names none and parses none.

The runtime is started with the three Arena operations as the one MCP server of that configuration:
`openclaw_bridge.py`, which relays every call to this worker over an authenticated local channel.
This worker validates it, asks the supervisor for the one bound match and answers. Once the
supervisor reports a move accepted, the whole OpenClaw process tree is ended - the runtime would
otherwise ask its model once more - and the throwaway directory is removed.

Standalone: it loads the sibling `arena_match.py` by path and imports nothing of the Connector, so
no signing key or provider session can reach it. OpenClaw receives only its isolated profile state
root through its own environment; `agent exec --state-dir` keeps each session disposable.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import queue
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any


def _arena_match() -> Any:
    """Load the sibling `arena_match.py` by path: this runs isolated, from a copy in a test."""
    path = Path(__file__).resolve().with_name("arena_match.py")
    spec = importlib.util.spec_from_file_location("arena_match", path)
    if spec is None or spec.loader is None:
        raise ImportError("The shared Arena match module is missing.")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("arena_match", module)
    spec.loader.exec_module(module)
    return module


arena = _arena_match()

COMMAND_ENV = "AGENTNEXUS_OPENCLAW_COMMAND"
CONFIG_ENV = "AGENTNEXUS_OPENCLAW_CONFIG"
SCRATCH_ENV = "AGENTNEXUS_ARENA_SCRATCH"
PROFILE_ROOT_ENV = "AGENTNEXUS_OPENCLAW_PROFILE_ROOT"
PROFILE_CONFIG_ENV = "AGENTNEXUS_OPENCLAW_PROFILE_CONFIG"
PROFILE_STATE_ENV = "AGENTNEXUS_OPENCLAW_PROFILE_STATE"
#: What the supervisor pinned about the profile's configuration file for this match: metadata only.
PIN_ENV = "AGENTNEXUS_OPENCLAW_PIN"
#: The server name of the bridge in the throwaway configuration; the runtime prefixes its tools.
SERVER = "arena"
PREFIXED = tuple(f"{SERVER}__{name}" for name in sorted(arena.TOOLS))
#: How long the runtime's own process gets to leave before the whole tree is killed.
TERM_SECONDS = 1.5
#: The most the supervisor-facing worker waits for the runtime to start before the decision.
VERSION_SECONDS = 60
#: What the runtime's own deadline is shortened by, so it ends before the supervisor's cutoff.
MARGIN_SECONDS = 5
ENVELOPE_BYTES = 65536
#: The signal that cannot be ignored (POSIX); Windows ends a tree through its job object instead.
FORCE = getattr(signal, "SIGKILL", signal.SIGTERM)

KEPT = {"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}


SID = re.compile(r"S-1-\d+(?:-\d+)+")
#: The accounts that can take any file on Windows anyway: the system and the Administrators group.
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
ACE_ALLOWED, ACE_DENIED = 0, 1


def acl_is_private(owner: str, entries: list[tuple[int, str]] | None, user: str) -> bool:
    """Decide from an owner and an access list whether only `user` (and the system) can reach it.

    The owner must be the user. Every grant must be to the user, to the system or to the
    Administrators group; a grant to anyone else - Everyone, Users, Authenticated Users, another
    account - is refusal. Denials are ignored, they only narrow. A missing list (which grants
    everyone everything), an entry of a kind that is not a plain grant or denial, and anything that
    is not a well-formed security identifier cannot be judged, so they refuse.
    """
    if entries is None or not SID.fullmatch(user) or owner != user:
        return False
    allowed = {user, SYSTEM_SID, ADMINISTRATORS_SID}
    for kind, sid in entries:
        if kind == ACE_DENIED:
            continue
        if kind != ACE_ALLOWED or not SID.fullmatch(sid) or sid not in allowed:
            return False
    return True


def windows_security(path: Path) -> tuple[str, list[tuple[int, str]] | None] | None:
    """Read a directory's owner and access list (Windows), or `None` when it cannot be read.

    Only the security descriptor is asked for; nothing inside the directory is opened or listed.
    """
    import ctypes
    from ctypes import wintypes

    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi.GetAclInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    )
    advapi.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)

    def text_of(sid: int | None) -> str | None:
        pointer = ctypes.c_void_p()
        if not sid or not advapi.ConvertSidToStringSidW(sid, ctypes.byref(pointer)):
            return None
        try:
            return ctypes.wstring_at(pointer.value) if pointer.value else None
        finally:
            kernel.LocalFree(pointer)

    class AclSize(ctypes.Structure):
        _fields_ = (("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD))

    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x1 | 0x4,  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status != 0:
        return None
    try:
        owner_text = text_of(owner.value)
        if owner_text is None:
            return None
        if not dacl.value:
            return owner_text, None
        size = AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            return None
        entries: list[tuple[int, str]] = []
        for index in range(size.count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                return None
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind in (ACE_ALLOWED, ACE_DENIED):
                sid = text_of(ace.value + 8)
                if sid is None:
                    return None
                entries.append((kind, sid))
            else:
                entries.append((kind, ""))
        return owner_text, entries
    finally:
        kernel.LocalFree(descriptor)


def windows_current_sid() -> str:
    """Return the security identifier of the user this process runs as (Windows), or `""`."""
    import ctypes
    from ctypes import wintypes

    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return ""
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    advapi.OpenProcessToken.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi.GetTokenInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    token = ctypes.c_void_p()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
        return ""
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))  # TokenUser
        if not needed.value:
            return ""
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            return ""
        sid = ctypes.c_void_p.from_buffer(buffer).value
        pointer = ctypes.c_void_p()
        if not sid or not advapi.ConvertSidToStringSidW(sid, ctypes.byref(pointer)):
            return ""
        try:
            return ctypes.wstring_at(pointer.value) if pointer.value else ""
        finally:
            kernel.LocalFree(pointer)
    finally:
        kernel.CloseHandle(token)


def windows_private(path: Path) -> bool:
    """Return whether only the current user (and the system accounts) can reach this directory."""
    try:
        security = windows_security(path)
        user = windows_current_sid()
    except (OSError, ValueError):
        return False
    return security is not None and acl_is_private(security[0], security[1], user)


def profile_context_valid(profile_root: Path, profile_config: Path, profile_state: Path) -> bool:
    """Accept only the non-linked OpenClaw config and state this Connector profile owns.

    Nothing is opened: only paths and their metadata are looked at. The configuration and the state
    must lie below the profile's own directory, with no link or junction on the way down. On a
    system with owners and modes the profile directory must be the current user's and closed to
    everyone else; on Windows the owner and the access list of the profile and of its state must
    say the same (the user, the system and Administrators only). The runtime's own auth store lives
    in that state and nobody else may reach it. Anything that cannot be proven is refused, and a
    refusal comes before any seat is claimed.
    """
    try:
        root = Path(profile_root).absolute()
        config = Path(profile_config).absolute()
        state = Path(profile_state).absolute()
        if config == root or state == root:
            return False
        if not root.is_dir() or not config.is_file() or not state.is_dir():
            return False
        for path in (config, state):
            if not path.is_relative_to(root):
                return False
            current = root
            for part in (Path(".") if path == root else path.relative_to(root)).parts:
                current /= part
                is_junction = getattr(current, "is_junction", lambda: False)
                if current.is_symlink() or is_junction():
                    return False
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            return False
        resolved_root = root.resolve(strict=True)
        if not config.resolve(strict=True).is_relative_to(resolved_root):
            return False
        if not state.resolve(strict=True).is_relative_to(resolved_root):
            return False
        if hasattr(os, "geteuid"):
            info = root.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return False
        elif sys.platform == "win32" and not (windows_private(root) and windows_private(state)):
            # No owner and mode here: the owner and the access list of the profile and of its
            # state must say it is the user's alone, or it is not proven and not accepted.
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def config_fingerprint(
    profile_root: Path, profile_config: Path, profile_state: Path
) -> dict[str, str] | None:
    """Describe the profile's configuration file by what can be said without opening it, or `None`.

    The runtime alone reads its original configuration and its authentication store; the Connector
    keeps only a fingerprint of the file's state: its canonical path, its identity (device and file
    number), its size, its modification and status times, that it is a plain file and not a link,
    and the owner and privacy boundary that `profile_context_valid` already checks. The file is
    never opened, read, parsed, hashed or copied, and nothing derived from its content is in the
    fingerprint. If any part cannot be proven, there is no fingerprint and the match is not played.
    """
    try:
        if not profile_context_valid(profile_root, profile_config, profile_state):
            return None
        path = Path(profile_config).absolute()
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_ino == 0:
            return None
        return {
            "path": str(path.resolve(strict=True)),
            "device": str(info.st_dev),
            "inode": str(info.st_ino),
            "size": str(info.st_size),
            "modified_ns": str(info.st_mtime_ns),
            "changed_ns": str(info.st_ctime_ns),
            "kind": "file",  # metadata
        }
    except (OSError, RuntimeError, ValueError):
        return None


def pin_text(fingerprint: dict[str, str]) -> str:
    """Return the fingerprint as the one stable text that is carried to the worker."""
    return json.dumps(fingerprint, sort_keys=True)


def pinned_state_holds(
    pin: str, profile_root: Path, profile_config: Path, profile_state: Path
) -> bool:
    """Return whether the configuration file is, provably, still the one that was pinned."""
    current = config_fingerprint(profile_root, profile_config, profile_state)
    return current is not None and pin_text(current) == pin


class ProfileChangedError(RuntimeError):
    """The profile's configuration file is no longer the one the match started with."""


ProfileChanged = ProfileChangedError


def agent_directories_valid(
    entries: Any, default_id: Any, profile_root: Path, profile_state: Path
) -> bool:
    """Accept only bounded, non-linked agent-store paths owned by this profile."""
    try:
        if entries is None:
            entries = []
        if isinstance(entries, dict):
            agents = [
                {**entry, "id": identity} if isinstance(entry, dict) else entry
                for identity, entry in entries.items()
            ]
        elif isinstance(entries, list):
            agents = entries
        else:
            return False
        root = Path(profile_root).resolve(strict=True)
        state = Path(profile_state).resolve(strict=True)
        if len(agents) > 32:
            return False
        paths: list[str] = []
        default = default_id if isinstance(default_id, str) else "main"
        if not 0 < len(default) <= 64:
            return False
        paths.append(str(state / "agents" / default / "agent"))
        for agent in agents:
            if not isinstance(agent, dict):
                return False
            identity = agent.get("id")
            if not isinstance(identity, str) or not 0 < len(identity) <= 64:
                return False
            value = agent.get("agentDir")
            if value is None:
                value = state / "agents" / identity / "agent"
            if not isinstance(value, str | Path) or not 0 < len(str(value)) <= 2048:
                return False
            paths.append(str(value))
        for value in paths:
            path = Path(value)
            if not path.is_absolute():
                return False
            current = Path(path.anchor)
            for part in path.parts[1:]:
                current /= part
                is_junction = getattr(current, "is_junction", lambda: False)
                if current.is_symlink() or is_junction():
                    return False
            if not path.resolve(strict=False).is_relative_to(root):
                return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return True


def config_value(
    command: list[str], path: str, environment: dict[str, str], work: Path, default: Any = None
) -> Any:
    """Read one bounded config field through OpenClaw, using a default only when it is unset."""
    status, output = bounded_run(
        [*command, "config", "get", path, "--json"], environment, work, VERSION_SECONDS
    )
    try:
        value = json.loads(output)
    except ValueError as unreadable:
        raise RuntimeError("OpenClaw returned an unreadable config field.") from unreadable
    if status == 0:
        return value
    error = value.get("error") if isinstance(value, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if (
        status == 1
        and isinstance(error, dict)
        and error.get("type") == "cli_error"
        and isinstance(message, str)
        and message.startswith(f"Config path is valid but unset: {path}.")
    ):
        return default
    raise RuntimeError("OpenClaw refused a config field required for state isolation.")


def cli_environment(
    base: dict[str, str],
    work: Path,
    config: Path,
    include_root: Path,
    *,
    profile_root: Path | None = None,
    profile_config: Path | None = None,
    profile_state: Path | None = None,
) -> dict[str, str]:
    """Return the exact environment one runtime run gets: OS essentials and a throwaway world.

    Nothing of the service's environment but the essentials is passed on - no key, no token, no
    other tool's home. OpenClaw receives its profile state root through its supported boundary so it
    can resolve its own auth; `agent exec --state-dir` directs sessions and run data below `work`.
    Telemetry and update checks are switched off, its log is silent, and the profile's own
    configuration is reachable only through the overlay's include, read-only.
    """
    environment = {key: value for key, value in base.items() if key.upper() in KEPT}
    state_dir = work / "ambient"
    context_values = (profile_root, profile_config, profile_state)
    if any(value is not None for value in context_values):
        if any(value is None for value in context_values):
            raise ValueError("Incomplete OpenClaw profile context.")
        if profile_root is None or profile_config is None or profile_state is None:
            raise ValueError("Incomplete OpenClaw profile context.")
        if not profile_context_valid(profile_root, profile_config, profile_state):
            raise ValueError("OpenClaw profile state is outside its owned context.")
        state_dir = profile_state
    home = work / "home"
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        APPDATA=str(work / "appdata"),
        LOCALAPPDATA=str(work / "localappdata"),
        XDG_CONFIG_HOME=str(work / "xdg-config"),
        XDG_STATE_HOME=str(work / "xdg-state"),
        XDG_CACHE_HOME=str(work / "xdg-cache"),
        XDG_DATA_HOME=str(work / "xdg-data"),
        TMP=str(work / "tmp"),
        TEMP=str(work / "tmp"),
        TMPDIR=str(work / "tmp"),
        NODE_COMPILE_CACHE=str(work / "cc"),
        OPENCLAW_STATE_DIR=str(state_dir),
        OPENCLAW_CONFIG_PATH=str(config),
        OPENCLAW_INCLUDE_ROOTS=str(include_root),
        OPENCLAW_CONFIG_READONLY="1",
        OPENCLAW_NO_AUTO_UPDATE="1",
        OPENCLAW_LOG_LEVEL="silent",
        DO_NOT_TRACK="1",
        NO_COLOR="1",
        PYTHONUTF8="1",
    )
    return environment


def make_world(work: Path) -> None:
    """Create the throwaway directories a run is pointed at."""
    for name in (
        "home", "appdata", "localappdata", "xdg-config", "xdg-state", "xdg-cache", "xdg-data",
        "tmp", "cc", "ambient", "run", "cwd",
    ):  # fmt: skip
        (work / name).mkdir(parents=True, exist_ok=True)


def overlay_document(
    profile_config: Path | None,
    *,
    python: str,
    bridge: Path,
    address: str,
    key: str,
    disabled: list[str],
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the throwaway configuration: the profile's, closed to exactly the three operations.

    The profile's configuration is only included. Sibling keys are merged over it: the tool
    allow-list replaces whatever the profile allows, tool search is off so the three are not hidden
    behind it, the profile's other MCP servers are disabled, and the bridge is the one server. A
    `provider` replaces the profile's model route; the preflight's canary uses it to point the
    runtime at a model that is the Connector's own.
    """
    servers: dict[str, Any] = {name: {"enabled": False} for name in disabled if name != SERVER}
    servers[SERVER] = {
        "command": python,
        "args": [str(bridge)],
        "env": {
            "ARENA_BRIDGE_ADDRESS": address,
            "ARENA_BRIDGE_KEY": key,
        },
        "enabled": True,
        "connectionTimeoutMs": 30000,
        "requestTimeoutMs": 60000,
        "toolFilter": {"include": sorted(arena.TOOLS)},
    }
    document: dict[str, Any] = {
        "tools": {"allow": list(PREFIXED), "toolSearch": False},
        "mcp": {"servers": servers},
        "update": {"checkOnStart": False},
        "telemetry": {"enabled": False},
    }
    if profile_config is not None:
        document = {"$include": str(profile_config), **document}
    if provider is not None:
        document["models"] = {"mode": "replace", "providers": {"canary": provider}}
        document["agents"] = {"defaults": {"model": {"primary": "canary/canary"}}}
    return document


def write_overlay(work: Path, document: dict[str, Any]) -> Path:
    """Write the overlay privately into the throwaway directory and return its path."""
    path = work / "arena.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return path


def exec_arguments(
    command: list[str], prompt: Path, seconds: int, overlay: Path, work: Path
) -> list[str]:
    """Return the argv of one run: the message in a file, throwaway state and working directory."""
    return [
        *command,
        "--no-color",
        "agent",
        "exec",
        "--message-file",
        str(prompt),
        "--json",
        "--timeout",
        str(seconds),
        "--config",
        str(overlay),
        "--state-dir",
        str(work / "run"),
        "--cwd",
        str(work / "cwd"),
    ]


# ---------------------------------------------------------------------------------------------
# Process trees: the runtime keeps children in sessions of their own, so a group is not enough
# ---------------------------------------------------------------------------------------------


def descendants(root: int) -> set[int]:
    """Return the pids below `root` (POSIX), read from the process table now."""
    if sys.platform == "win32":
        return set()
    try:
        table = subprocess.run(  # noqa: S603 - a fixed system command
            [shutil.which("ps") or "/bin/ps", "-A", "-o", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    children: dict[int, list[int]] = {}
    for line in table.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: set[int] = set()
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def alive(pid: int) -> bool:
    """Return whether a process with this pid exists (POSIX)."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def kill_tree(root: int, known: set[int] | None = None) -> None:
    """End `root` and everything below it, politely first and then for certain (POSIX).

    The tree is read before the first signal, because a child that outlives its parent is no longer
    found through it. SIGTERM goes to the root alone, which tears its own tree down; whoever is
    still there after a short wait, from the earlier reading or the current one, is killed.
    """
    members = {root} | (known or set()) | descendants(root)
    with contextlib.suppress(OSError):
        os.kill(root, signal.SIGTERM)
    end = time.monotonic() + TERM_SECONDS
    while time.monotonic() < end and alive(root):
        time.sleep(0.05)
    members |= descendants(root)
    for pid in members:
        with contextlib.suppress(OSError):
            os.kill(pid, FORCE)


def guard_main(command: list[str]) -> int:
    """Run `command` as the runtime and end its whole tree when this guard's own parent is gone.

    Started by the worker, so that a worker killed outright still leaves no runtime behind: the
    guard notices the change of parent within a quarter of a second, and a TERM from the worker is
    the ordinary way to end a decision.
    """
    parent = os.getppid()
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL)  # noqa: S603 - the worker's argv
    known: set[int] = set()

    def teardown(*_: object) -> None:
        kill_tree(child.pid, known)
        os._exit(143)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), teardown)
    last = 0.0
    while child.poll() is None:
        if os.getppid() != parent:
            teardown()
        if time.monotonic() - last > 1.0:
            known |= descendants(child.pid)
            last = time.monotonic()
        time.sleep(0.25)
    for pid in known:
        if alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, FORCE)
    return child.returncode


class WindowsJob:
    """A job object that ends every process in it when it is ended or its last handle closes.

    Every answer of Windows is checked; a call that fails raises `OSError` and the job is not used.
    """

    def __init__(self, kernel: Any = None, ntdll: Any = None) -> None:
        """Create the job with kill-on-close, so a killed worker leaves no runtime behind."""
        if kernel is None:
            kernel, ntdll = self._libraries()
        self._kernel, self._ntdll = kernel, ntdll
        self.handle: int | None = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("The process job could not be created.")
        if not self._limit_to_kill_on_close():
            self.close()
            raise OSError("The process job could not be limited.")

    @staticmethod
    def _libraries() -> tuple[Any, Any]:
        """Load the Windows libraries with their argument and result types declared."""
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = ctypes.c_long
        return kernel, ntdll

    def _limit_to_kill_on_close(self) -> bool:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = (
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            )

        class Counters(ctypes.Structure):
            _fields_ = tuple((name, ctypes.c_uint64) for name in ("a", "b", "c", "d", "e", "f"))

        class Extended(ctypes.Structure):
            _fields_ = (
                ("Basic", Basic),
                ("Io", Counters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            )

        information = Extended()
        information.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        return bool(
            self._kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(information), ctypes.sizeof(information)
            )
        )

    def adopt(self, process: subprocess.Popen[Any]) -> None:
        """Put a process that was started suspended in the job, and only then let it run."""
        handle = int(process._handle)  # type: ignore[attr-defined]
        if not self._kernel.AssignProcessToJobObject(self.handle, handle):
            raise OSError("The process could not be put in its job.")
        if self._ntdll.NtResumeProcess(handle) != 0:
            raise OSError("The process of its job could not be resumed.")

    def end(self) -> None:
        """End every process in the job now."""
        if self.handle:
            self._kernel.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        """Release the job; whatever is still in it ends."""
        handle, self.handle = self.handle, None
        if handle:
            self._kernel.CloseHandle(handle)


def start_in_job(argv: list[str], job: WindowsJob, **options: Any) -> subprocess.Popen[Any]:
    """Start a process suspended, put it in the job, and resume it; fail with nothing running.

    The process cannot start anything before it is in the job. If joining or resuming fails, the
    process is killed, the job is released and the start raises.
    """
    flags = options.pop("creationflags", 0) | 0x4  # CREATE_SUSPENDED
    process = subprocess.Popen(argv, creationflags=flags, **options)  # noqa: S603 - the caller's argv
    try:
        job.adopt(process)
    except BaseException:
        with contextlib.suppress(OSError):
            process.kill()
        job.end()
        job.close()
        raise
    return process


class Run:
    """One started runtime process and what is needed to end its whole tree."""

    def __init__(self, argv: list[str], environment: dict[str, str], work: Path) -> None:
        """Start the runtime; on POSIX behind the guard, on Windows inside a job object."""
        self.output = open(work / "out.json", "wb")  # noqa: SIM115 - closed in `end`
        self.job: WindowsJob | None = None
        self.tree: set[int] = set()
        if sys.platform == "win32":
            try:
                self.job = WindowsJob()
                self.process = start_in_job(
                    argv,
                    self.job,
                    env=environment,
                    cwd=work / "cwd",
                    stdin=subprocess.DEVNULL,
                    stdout=self.output,
                    stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                )
            except BaseException:
                if self.job is not None:
                    self.job.close()
                self.output.close()
                raise
        else:
            self.process = subprocess.Popen(  # noqa: S603 - the runtime behind this guard
                [sys.executable, "-I", str(Path(__file__).resolve()), "--guard", "--", *argv],
                env=environment,
                cwd=work / "cwd",
                stdin=subprocess.DEVNULL,
                stdout=self.output,
                stderr=subprocess.DEVNULL,
            )

    def running(self) -> bool:
        """Return whether the runtime (or its guard) is still running."""
        return self.process.poll() is None

    def end(self) -> int | None:
        """End the whole tree and reap it; return the exit status the runtime gave, if any."""
        if self.job is not None:
            self.job.end()
        elif self.process.poll() is None:
            self.tree = descendants(self.process.pid)
            with contextlib.suppress(OSError):
                self.process.terminate()
            try:
                self.process.wait(timeout=TERM_SECONDS * 2)
            except subprocess.TimeoutExpired:
                kill_tree(self.process.pid, self.tree)
        for pid in self.tree:
            if alive(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, FORCE)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=5)
        if self.job is not None:
            self.job.close()
        self.output.close()
        return self.process.returncode


def bounded_run(
    argv: list[str], environment: dict[str, str], work: Path, seconds: int
) -> tuple[int, str]:
    """Run one OpenClaw command with a tree bound and bounded captured output."""
    run = Run(argv, environment, work)
    deadline = time.monotonic() + seconds
    while run.running() and time.monotonic() < deadline:
        time.sleep(0.05)
    timed_out = run.running()
    status = run.end()
    output = work / "out.json"
    try:
        if output.stat().st_size > 65536:
            return 125, ""
        stdout = output.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return 125, ""
    return (124 if timed_out else status if status is not None else 125), stdout


def channel_address(work: Path) -> tuple[str, Path | None]:
    """Return the local channel's address, and a short private directory if it needed one.

    A named pipe on Windows; a Unix socket in the throwaway directory elsewhere. A socket path has a
    hard limit of about a hundred bytes, which a long temporary directory can exceed: the socket
    then lives in a short private directory of its own, which the decision removes.
    """
    if sys.platform == "win32":
        return rf"\\.\pipe\agentnexus-arena-{secrets.token_hex(8)}", None
    address = str(work / "r.sock")
    if len(os.fsencode(address)) <= 90:
        return address, None
    base = "/tmp"  # noqa: S108 - a short path is the whole point; the directory is private
    short = Path(tempfile.mkdtemp(prefix="ax", dir=base if Path(base).is_dir() else None))
    return str(short / "r.sock"), short


def remove_tree(path: Path) -> bool:
    """Remove the throwaway directory; a process still exiting may hold a file a moment."""
    for _ in range(25):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------------------------
# The decision worker
# ---------------------------------------------------------------------------------------------


class Decision:
    """What the relay and the main loop of one decision share."""

    def __init__(self) -> None:
        """Start with no move accepted."""
        self.accepted = threading.Event()
        self.over = threading.Event()
        self.changed = threading.Event()
        self.lock = threading.Lock()


def worker_main(
    input_stream: Any = None,
    output: Any = None,
    exit_hard: Callable[[int], object] = os._exit,
) -> int:
    """Serve decisions for the match process until it stops us, or until it is gone."""
    input_stream = sys.stdin if input_stream is None else input_stream
    output = sys.stdout if output is None else output
    incoming: queue.Queue[str] = queue.Queue()
    command: list[str] = json.loads(os.environ[COMMAND_ENV])
    config = Path(os.environ[CONFIG_ENV]) if os.environ.get(CONFIG_ENV) else None
    scratch = Path(os.environ[SCRATCH_ENV])
    profile_root = Path(os.environ[PROFILE_ROOT_ENV])
    profile_config = Path(os.environ[PROFILE_CONFIG_ENV])
    profile_state = Path(os.environ[PROFILE_STATE_ENV])
    pin = os.environ.get(PIN_ENV, "")
    if (
        not pin
        or config != profile_config
        or not profile_context_valid(profile_root, profile_config, profile_state)
    ):
        raise RuntimeError("OpenClaw profile context is no longer owned.")

    def unchanged() -> bool:
        return pinned_state_holds(pin, profile_root, profile_config, profile_state)

    def check() -> None:
        """Raise unless the configuration file is, provably, as the match started with it."""
        if not unchanged():
            raise ProfileChanged("The profile's configuration changed during the match.")

    def pump() -> None:
        # The match process closing this pipe means it is gone. The guard ends the runtime's tree.
        with contextlib.suppress(OSError, ValueError):
            for line in iter(lambda: input_stream.readline(65537), ""):
                incoming.put(line)
        exit_hard(3)

    def send(document: dict[str, Any]) -> None:
        output.write(json.dumps(document) + "\n")
        output.flush()

    threading.Thread(target=pump, daemon=True).start()
    with contextlib.redirect_stdout(sys.stderr):
        boot = scratch / "boot"
        make_world(boot)
        check()
        version, _ = bounded_run(
            [*command, "--version"],
            cli_environment(
                dict(os.environ),
                boot,
                boot,
                boot,
                profile_root=profile_root,
                profile_config=profile_config,
                profile_state=profile_state,
            ),
            boot,
            VERSION_SECONDS,
        )
        if version != 0:
            raise RuntimeError("The runtime does not start.")
        # Asked once, before the worker is ready: a start of the runtime costs seconds that a
        # decision's budget does not have.
        check()
        others = server_names(
            command,
            config,
            boot,
            profile_root=profile_root,
            profile_config=profile_config,
            profile_state=profile_state,
        )
        send({"ready": True})
        while True:
            line = arena.line_document(incoming.get())
            if line == {"stop": True}:
                return 0
            decision = arena.checked_decision(line)
            run_decision(
                command,
                config,
                scratch,
                others,
                decision,
                incoming,
                send,
                profile_root,
                profile_config,
                profile_state,
                check,
                unchanged,
            )


def run_decision(
    command: list[str],
    config: Path | None,
    scratch: Path,
    others: list[str],
    decision: dict[str, Any],
    incoming: queue.Queue[str],
    send: Callable[[dict[str, Any]], None],
    profile_root: Path,
    profile_config: Path,
    profile_state: Path,
    check: Callable[[], None],
    unchanged: Callable[[], bool],
) -> None:
    """Make one decision with one run of the runtime; every ending is one fixed message.

    The configuration file is checked immediately before the runtime starts, again before each
    request is relayed upstream and once more when the runtime has finished. A change at any of
    them ends the runtime path, discards the decision and sends no move.
    """
    work = Path(tempfile.mkdtemp(prefix="d", dir=scratch))
    state = Decision()
    run: Run | None = None
    listener: Any = None
    short: Path | None = None
    outcome = "exception"
    try:
        make_world(work)
        key = secrets.token_bytes(32)
        address, short = channel_address(work)
        listener = Listener(address, authkey=key)
        prompt = work / "prompt.txt"
        prompt.write_text(
            arena.decision_prompt(
                decision["game"], decision["role"], decision["seat"], decision["state"]
            ),
            encoding="utf-8",
        )
        overlay = write_overlay(
            work,
            overlay_document(
                config,
                python=sys.executable,
                bridge=Path(__file__).resolve().with_name("openclaw_bridge.py"),
                address=address,
                key=key.hex(),
                disabled=others,
            ),
        )
        threading.Thread(
            target=relay, args=(listener, state, incoming, send, unchanged), daemon=True
        ).start()
        seconds = max(10, int(decision["seconds"]) - MARGIN_SECONDS)
        include_root = config.parent if config is not None else work
        check()
        run = Run(
            exec_arguments(command, prompt, seconds, overlay, work),
            cli_environment(
                dict(os.environ),
                work,
                overlay,
                include_root,
                profile_root=profile_root,
                profile_config=profile_config,
                profile_state=profile_state,
            ),
            work,
        )
        end = time.monotonic() + decision["seconds"] + MARGIN_SECONDS
        while (
            not state.accepted.is_set()
            and not state.changed.is_set()
            and run.running()
            and time.monotonic() < end
        ):
            time.sleep(0.05)
        if state.changed.is_set():
            raise ProfileChanged("The profile's configuration changed during the decision.")
        check()
        if state.accepted.is_set():
            outcome = "completed"
            send({"decision": "completed"})
        else:
            outcome = "failed"
    except Exception:
        outcome = "exception"
    finally:
        state.over.set()
        status = run.end() if run is not None else None
        if listener is not None:
            with contextlib.suppress(Exception):
                listener.close()
        if outcome == "completed":
            pass
        elif outcome == "failed":
            send({"decision": "returned", "outcome": "ok" if status == 0 else "failed"})
        else:
            send({"decision": "exception"})
        if short is not None:
            shutil.rmtree(short, ignore_errors=True)
        send({"decision": "closed" if remove_tree(work) else "close_failed"})


def server_names(
    command: list[str],
    config: Path | None,
    work: Path,
    *,
    profile_root: Path,
    profile_config: Path,
    profile_state: Path,
) -> list[str]:
    """Return the names of the profile's own MCP servers, so the overlay can switch them off.

    Asked of the runtime itself and read for their names only: the answer may carry values that
    the profile keeps private, and none of it is kept or logged.
    """
    if config is None:
        return []
    environment = cli_environment(
        dict(os.environ),
        work,
        config,
        config.parent,
        profile_root=profile_root,
        profile_config=profile_config,
        profile_state=profile_state,
    )
    entries = config_value(command, "agents.entries", environment, work, [])
    default_id = config_value(
        command, "agents.defaults.systemAgent.agentId", environment, work, "main"
    )
    if not agent_directories_valid(entries, default_id, profile_root, profile_state):
        raise RuntimeError("OpenClaw agent directories are outside the profile.")
    try:
        status, stdout = bounded_run(
            [*command, "config", "get", "mcp.servers", "--json"],
            environment,
            work,
            VERSION_SECONDS,
        )
        document = json.loads(stdout) if status == 0 else {}
    except (OSError, ValueError):
        return []
    return (
        [name for name in document if isinstance(name, str)] if isinstance(document, dict) else []
    )


def relay(
    listener: Any,
    state: Decision,
    incoming: queue.Queue[str],
    send: Callable[[dict[str, Any]], None],
    verify: Callable[[], bool] | None = None,
) -> None:
    """Accept the bridge and answer its calls, one at a time, for the one bound match."""
    while not state.over.is_set():
        try:
            connection = listener.accept()
        except Exception:
            if state.over.is_set():
                return
            continue
        with contextlib.suppress(Exception):
            while not state.over.is_set():
                if not connection.poll(0.25):
                    continue
                request = json.loads(connection.recv_bytes(65536))
                connection.send_bytes(
                    json.dumps(answer(request, state, incoming, send, verify)).encode()
                )
        with contextlib.suppress(Exception):
            connection.close()


def answer(
    request: Any,
    state: Decision,
    incoming: queue.Queue[str],
    send: Callable[[dict[str, Any]], None],
    verify: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Validate one bridge call, ask the supervisor and shape the reply the bridge hands on.

    `verify` says whether the profile's configuration file is still the one the match started with.
    If it says no, or cannot say, nothing is relayed upstream: the decision is over and sends no
    move.
    """
    with state.lock:
        if state.accepted.is_set():
            return {"text": "The move was already accepted. Stop.", "error": True}
        try:
            if not isinstance(request, dict) or set(request) != {"op", "arguments"}:
                raise ValueError("Operation outside the bounded Arena contract.")
            bounded = arena.bounded_request(request["op"], request["arguments"])
        except ValueError:
            return {"text": "That request is outside the Arena contract.", "error": True}
        try:
            allowed = verify is None or verify()  # verify
        except Exception:
            allowed = False  # unverifiable
        if not allowed:
            state.changed.set()
            return {"text": "The profile changed. Stop.", "error": True}
        send(bounded)
        reply = arena.line_document(incoming.get())
        if reply == {"complete": True}:
            state.accepted.set()
            return {"text": "Move accepted. The decision is over; stop."}
        if set(reply) != {"result"}:
            return {"text": "Invalid supervised game response.", "error": True}
        return {"text": json.dumps(reply["result"])}


def main() -> int:
    """Serve decisions as the worker, or run the guard around the runtime."""
    if sys.argv[1:2] == ["--guard"] and sys.argv[2:3] == ["--"] and len(sys.argv) > 3:
        return guard_main(sys.argv[3:])
    if len(sys.argv) != 1:
        return 2
    return worker_main()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Do not expose runtime errors, credentials, config or model output in service logs.
        with contextlib.suppress(OSError):
            arena.diagnostic(sys.stdout, "runtime_exception")
        raise SystemExit(3) from None
