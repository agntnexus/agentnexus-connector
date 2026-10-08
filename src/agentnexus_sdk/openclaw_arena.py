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
no signing key, profile or provider session can reach it or the runtime.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import queue
import secrets
import shutil
import signal
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


def cli_environment(
    base: dict[str, str], work: Path, config: Path, include_root: Path
) -> dict[str, str]:
    """Return the exact environment one runtime run gets: OS essentials and a throwaway world.

    Nothing of the service's environment but the essentials is passed on - no key, no token, no
    other tool's home - and everything the runtime writes goes below `work`, which is removed.
    Telemetry and update checks are switched off, its log is silent, and the profile's own
    configuration is reachable only through the overlay's include, read-only.
    """
    environment = {key: value for key, value in base.items() if key.upper() in KEPT}
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
        OPENCLAW_STATE_DIR=str(work / "ambient"),
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
        "env": {"ARENA_BRIDGE_ADDRESS": address, "ARENA_BRIDGE_KEY": key},
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
    """A job object that ends every process in it when its last handle closes (Windows)."""

    def __init__(self) -> None:
        """Create the job with kill-on-close, so a killed worker leaves no runtime behind."""
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

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self._kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        information = Extended()
        information.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(information), ctypes.sizeof(information)
        )

    def add(self, process: subprocess.Popen[Any]) -> None:
        """Put a started process, and so everything it starts, in the job."""
        self._kernel.AssignProcessToJobObject(self.handle, int(process._handle))  # type: ignore[attr-defined]

    def end(self) -> None:
        """End every process in the job now."""
        self._kernel.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        """Release the job; whatever is still in it ends."""
        self._kernel.CloseHandle(self.handle)


class Run:
    """One started runtime process and what is needed to end its whole tree."""

    def __init__(self, argv: list[str], environment: dict[str, str], work: Path) -> None:
        """Start the runtime; on POSIX behind the guard, on Windows inside a job object."""
        self.output = open(work / "out.json", "wb")  # noqa: SIM115 - closed in `end`
        self.job: WindowsJob | None = None
        self.tree: set[int] = set()
        if sys.platform == "win32":
            self.job = WindowsJob()
            self.process = subprocess.Popen(  # noqa: S603 - the runtime and this worker's argv
                argv,
                env=environment,
                cwd=work / "cwd",
                stdin=subprocess.DEVNULL,
                stdout=self.output,
                stderr=subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            self.job.add(self.process)
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
        version = subprocess.run(  # noqa: S603 - the driver's reviewed runtime command
            [*command, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=VERSION_SECONDS,
            check=False,
            env=cli_environment(dict(os.environ), boot, boot, boot),
        )
        if version.returncode != 0:
            raise RuntimeError("The runtime does not start.")
        # Asked once, before the worker is ready: a start of the runtime costs seconds that a
        # decision's budget does not have.
        others = server_names(command, config, boot)
        send({"ready": True})
        while True:
            line = arena.line_document(incoming.get())
            if line == {"stop": True}:
                return 0
            decision = arena.checked_decision(line)
            run_decision(command, config, scratch, others, decision, incoming, send)


def run_decision(
    command: list[str],
    config: Path | None,
    scratch: Path,
    others: list[str],
    decision: dict[str, Any],
    incoming: queue.Queue[str],
    send: Callable[[dict[str, Any]], None],
) -> None:
    """Make one decision with one run of the runtime; every ending is one fixed message."""
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
        threading.Thread(target=relay, args=(listener, state, incoming, send), daemon=True).start()
        seconds = max(10, int(decision["seconds"]) - MARGIN_SECONDS)
        include_root = config.parent if config is not None else work
        run = Run(
            exec_arguments(command, prompt, seconds, overlay, work),
            cli_environment(dict(os.environ), work, overlay, include_root),
            work,
        )
        end = time.monotonic() + decision["seconds"] + MARGIN_SECONDS
        while not state.accepted.is_set() and run.running() and time.monotonic() < end:
            time.sleep(0.05)
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


def server_names(command: list[str], config: Path | None, work: Path) -> list[str]:
    """Return the names of the profile's own MCP servers, so the overlay can switch them off.

    Asked of the runtime itself and read for their names only: the answer may carry values that
    the profile keeps private, and none of it is kept or logged.
    """
    if config is None:
        return []
    try:
        answer = subprocess.run(  # noqa: S603 - the driver's reviewed runtime command
            [*command, "config", "get", "mcp.servers", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=VERSION_SECONDS,
            check=False,
            env=cli_environment(dict(os.environ), work, config, config.parent),
        )
        document = json.loads(answer.stdout) if answer.returncode == 0 else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return (
        [name for name in document if isinstance(name, str)] if isinstance(document, dict) else []
    )


def relay(
    listener: Any,
    state: Decision,
    incoming: queue.Queue[str],
    send: Callable[[dict[str, Any]], None],
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
                connection.send_bytes(json.dumps(answer(request, state, incoming, send)).encode())
        with contextlib.suppress(Exception):
            connection.close()


def answer(
    request: Any,
    state: Decision,
    incoming: queue.Queue[str],
    send: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Validate one bridge call, ask the supervisor and shape the reply the bridge hands on."""
    with state.lock:
        if state.accepted.is_set():
            return {"text": "The move was already accepted. Stop.", "error": True}
        try:
            if not isinstance(request, dict) or set(request) != {"op", "arguments"}:
                raise ValueError("Operation outside the bounded Arena contract.")
            bounded = arena.bounded_request(request["op"], request["arguments"])
        except ValueError:
            return {"text": "That request is outside the Arena contract.", "error": True}
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
