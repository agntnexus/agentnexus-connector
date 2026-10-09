"""The process harness of the Arena tests (agntnexus/agentnexus#223, #228).

A real supervisor, a real match process and a real decision worker, against a stand-in for the
Hermes API surface or a second, fake runtime, with a disposable profile, a recursive snapshot of it
and a tether for every process that must be gone afterwards. The tests that use it live in
`test_arena_decision_worker.py` and `test_arena_hermes_subscription.py`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from arena_fake_driver import FakeArenaDriver
from arena_fakes import PRIVATE, intent, supervisor

from agentnexus_sdk import (
    arena_driver,
    arena_driver_hermes,
    arena_driver_openclaw,
    arena_match,
    arena_runner,
    hermes_arena,
)

#: The runtimes every process test runs through: the Hermes stand-in and a second, fake runtime.
RUNTIMES = ["hermes", "fake"]
GAMES = {
    "white": "chess-1-solo",
    "black": "chess-1-solo",
    "first": "connect-four-1-solo",
    "second": "connect-four-1-solo",
}


# ---------------------------------------------------------------------------------------------
# A real parent and a real child process
# ---------------------------------------------------------------------------------------------

LAUNCHER = """\
import importlib.util
import sys

arena, bound, cleanup, *worker = sys.argv[1:]
spec = importlib.util.spec_from_file_location("arena_match", arena)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.DECISION_SECONDS = {"chess": float(bound), "connect-four": float(bound)}
module.CLEANUP_SECONDS = float(cleanup)
sys.argv = [arena, *worker]
try:
    raise SystemExit(module.main())
except Exception:
    module.diagnostic(sys.stdout, "runtime_exception")
    raise SystemExit(3) from None
"""

STUBS = {
    "hermes_cli/__init__.py": "",
    "hermes_cli/config.py": """\
DEFAULT_CONFIG = {}


def load_config(*args, **kwargs):
    return {}


load_config_readonly = load_config
""",
    "hermes_cli/auth.py": """\
class AuthError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
""",
    "hermes_constants.py": """\
import os
from pathlib import Path

OVERRIDES = []


def get_hermes_home():
    return Path(OVERRIDES[-1]) if OVERRIDES else Path(os.environ["HERMES_HOME"])


def set_hermes_home_override(path):
    OVERRIDES.append(str(path))
    return len(OVERRIDES)


def reset_hermes_home_override(token):
    del OVERRIDES[token - 1 :]
""",
    "agent/__init__.py": "",
    "agent/secret_scope.py": """\
from pathlib import Path

SCOPES = []


def build_profile_secret_scope(path):
    # Hermes reading its own profile .env: say which file; the credentials never leave Hermes.
    env = Path(path) / ".env"
    with open(Path(__file__).resolve().parents[1] / "dotenv-reads.stand-in-record", "a") as handle:
        handle.write(str(env) + "\\n")
    values = {}
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    return values


def set_secret_scope(scope):
    SCOPES.append(scope)
    return len(SCOPES)


def reset_secret_scope(token):
    del SCOPES[token - 1 :]


def current_scope():
    return SCOPES[-1] if SCOPES else {}
""",
    "hermes_cli/runtime_provider.py": """\
import json
from pathlib import Path

import hermes_constants
from agent import secret_scope
from hermes_cli.auth import AuthError

BEHAVIOR = json.loads(Path(__file__).resolve().parents[1].joinpath("behavior.json").read_text())


def resolve_runtime_provider(requested=None, target_model=None, explicit_base_url=None):
    # Hermes' own resolver: the credential of the configured provider, from where Hermes keeps it.
    shape = BEHAVIOR.get("resolve_shape")
    if BEHAVIOR.get("credentials") == "subscription":
        home = hermes_constants.get_hermes_home()
        # Like Hermes' credential store: its lock is created when the store is first consulted, and
        # a lock that exists is opened and not rewritten (measured on the reviewed Hermes).
        if not (home / "auth.lock").exists():
            (home / "auth.lock").touch()
        store = home / "auth.json"
        if not store.exists():
            raise AuthError("codex_auth_missing")
        grant = json.loads(store.read_text())["providers"][requested]["tokens"]
        resolved = {
            "provider": requested,
            "api_mode": "codex_responses",
            "base_url": explicit_base_url,
            "api_key": grant["access_token"],
            "source": "device_code",
            "requested_provider": requested,
            "credential_pool": object(),
        }
    else:
        key = secret_scope.current_scope().get("OPENROUTER_API_KEY")
        if not key:
            raise AuthError("missing_key")
        # Measured on the reviewed Hermes: resolving a key provider also consults the credential
        # store of whatever home is current, which creates its lock and an empty store there.
        home = hermes_constants.get_hermes_home()
        if not (home / "auth.lock").exists():
            (home / "auth.lock").touch()
        if not (home / "auth.json").exists():
            (home / "auth.json").write_text("{}")
        resolved = {
            "provider": requested or "openrouter",
            "api_mode": "chat_completions",
            "base_url": "http://127.0.0.1:9",
            "api_key": key,
            "source": "env:OPENROUTER_API_KEY",
            "requested_provider": requested,
        }
    if shape == "command":
        resolved["command"] = "synthetic-external-transport"
    elif shape == "acp_command":
        resolved["acp_command"] = "synthetic-external-transport"
    elif shape == "app_server":
        resolved["api_mode"] = "codex_app_server"
    elif shape == "acp_url":
        resolved["base_url"] = "acp://synthetic"
    return resolved
""",
    "tools/__init__.py": "",
    "tools/tool_search.py": """\
class ToolSearchConfig:
    @staticmethod
    def from_raw(raw):
        return raw


load_config = None
""",
    "tools/registry.py": """\
class Registry:
    def __init__(self):
        self.tools = {}

    def register(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs


registry = Registry()
""",
    "toolsets.py": """\
def create_custom_toolset(name, description, tools):
    return None
""",
    "dotenv.py": """\
raise ImportError("the Arena worker must not read credentials itself: Hermes does")
""",
    "model_tools.py": """\
def get_tool_definitions(*args, **kwargs):
    names = ["game_join", "game_move", "game_state"]
    return [{"function": {"name": name}} for name in names]


def handle_function_call(*args, **kwargs):
    raise RuntimeError("unpatched")
""",
    "run_agent.py": """\
import json
import os
import socket
import threading
import time
from pathlib import Path

import model_tools

BEHAVIOR = json.loads(Path(__file__).with_name("behavior.json").read_text())
TETHER = None
if BEHAVIOR.get("tether"):
    # Open for as long as this process lives, so the test can tell when it is gone.
    TETHER = socket.create_connection(("127.0.0.1", BEHAVIOR["tether"]))

    def watch():
        # The test closing its end is the end of this process, whatever it is doing.
        try:
            TETHER.recv(1)
        except OSError:
            pass
        os._exit(1)

    threading.Thread(target=watch, daemon=True).start()
HOME = Path(os.environ["HERMES_HOME"])


def scaffold():
    # What the real Hermes 0.21.3 was measured to do the moment it starts: fill its own home with
    # directories, logs, caches, a state database and a backup of the config it finds there. A file
    # that already exists is left alone, so the canaries of a profile stay as they are.
    for name in (
        "audio_cache", "backups/config", "cache", "cron", "hooks", "image_cache", "logs/curator",
        "memories", "pairing", "sessions", "skills",
    ):
        (HOME / name).mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "state.db", "cache/schema_columns.json"):
        if not (HOME / name).exists():
            (HOME / name).write_bytes(b"stand-in\\n")
    for name in ("logs/agent.log", "logs/errors.log"):
        with open(HOME / name, "ab") as handle:
            handle.write(b"stand-in\\n")
    config = HOME / "config.yaml"
    if config.exists():
        (HOME / "backups/config/config.yaml.good").write_bytes(config.read_bytes())
    record = BEHAVIOR.get("record")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(str(HOME) + "\\n")


scaffold()
if BEHAVIOR.get("helper"):
    # A process the runtime started on its own (a tool server, a transport helper): it holds a
    # tether too, so the test can tell whether it outlived the decision that was cut off.
    import subprocess
    import sys

    HELPER = "\\n".join(
        [
            "import os, socket, sys, threading",
            "s = socket.create_connection(('127.0.0.1', int(sys.argv[1])))",
            "def watch():",
            "    try:",
            "        s.recv(1)",
            "    except OSError:",
            "        pass",
            "    os._exit(1)",
            "threading.Thread(target=watch, daemon=True).start()",
            "open(sys.argv[2], 'w').close()",
            "threading.Event().wait()",
        ]
    )
    # "inherit": it keeps this process's stdout (the pipe to the match process) open, as a helper of
    # a real runtime may. Its readiness is a file, because a pipe it inherits is not for reading.
    up = Path(__file__).with_name("helper-up.stand-in-record")
    up.unlink(missing_ok=True)
    quiet = {"stdin": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if BEHAVIOR["helper"] != "inherit":
        quiet["stdout"] = subprocess.DEVNULL
    subprocess.Popen([sys.executable, "-c", HELPER, str(BEHAVIOR["tether"]), str(up)], **quiet)
    for _ in range(400):
        if up.exists():
            break
        time.sleep(0.05)  # the helper is connected before the decision starts
_environment_record = BEHAVIOR.get("environment")
if _environment_record:
    with open(_environment_record, "a", encoding="utf-8") as _handle:
        _handle.write(json.dumps(dict(os.environ)) + "\\n")
get_tool_definitions = model_tools.get_tool_definitions
handle_function_call = model_tools.handle_function_call


def forever():
    # A model transport that never answers.
    left, right = socket.socketpair()
    left.recv(1)


class AIAgent:
    def __init__(
        self,
        *,
        enabled_toolsets=None,
        max_iterations=3,
        run_budget_seconds=None,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        api_key=None,
        base_url=None,
        model=None,
        **rest,
    ):
        self.tools = model_tools.get_tool_definitions(enabled_toolsets=enabled_toolsets)
        self.api_key, self.base_url, self.model = api_key, base_url, model
        self.rest = rest
        record = BEHAVIOR.get("agents")
        if record:
            import hermes_constants

            with open(record, "a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "model": model,
                            "extra": sorted(rest),
                            "home": os.environ["HERMES_HOME"],
                            "resolved_home": str(hermes_constants.get_hermes_home()),
                        }
                    )
                    + "\\n"
                )
        if _environment_record:
            with open(_environment_record, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(os.environ)) + "\\n")

    def ask(self):
        # One request to the model transport, as Hermes would make it: the key is the bearer.
        import urllib.request

        names = [item["function"]["name"] for item in self.tools]
        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps({"tools": names, "model": self.model}).encode(),
            headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as answer:  # blocks for ever if the server does
            return json.loads(answer.read())

    def run_conversation(self, prompt):
        print("synthetic-private-model-output")
        mode = BEHAVIOR["mode"]
        if mode == "block":
            forever()
        if mode == "slow":
            time.sleep(BEHAVIOR["seconds"])
        if mode in ("fast", "slow"):
            if BEHAVIOR.get("transport") == "http":
                call = self.ask()["function_call"]
                model_tools.handle_function_call(call["name"], call["arguments"])
                # Hermes would ask the model once more now: the closing request.
                self.ask()
            else:
                model_tools.handle_function_call("game_move", BEHAVIOR["arguments"])
            if BEHAVIOR.get("tail") == "hang":
                # Hermes' ordinary closing request, issued once the tool result is back.
                forever()
        return {"failed": False}

    def close(self):
        change = BEHAVIOR.get("owner_changes")
        if change:
            # The owner edits the profile while a match runs: a new model, and a rotated grant.
            profile = Path(change["profile"])
            (profile / "config.yaml").write_text(change["config"], encoding="utf-8")
            if change.get("grant"):
                (profile / "auth.json").write_text(change["grant"], encoding="utf-8")
        seen = BEHAVIOR.get("home_listing")
        if seen:
            held = [str(p.relative_to(HOME)) for p in HOME.rglob("*") if p.is_file()]
            text = " ".join((HOME / name).read_text(errors="ignore") for name in held)
            holds = BEHAVIOR.get("token", "?") in text
            with open(seen, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"files": held, "holds_token": holds}) + "\\n")
        how = BEHAVIOR.get("close", "ok")
        if how == "hang":
            forever()
        if how == "raise":
            raise RuntimeError("synthetic-private-close-failure")
""",
}


#: The fake grant of a subscription profile. It exists only in a disposable profile.
SUBSCRIPTION_TOKEN = "synthetic-subscription-access-token-0001"  # noqa: S105 - a fixture


def subscription_auth(token: str = SUBSCRIPTION_TOKEN) -> str:
    """Return an auth store of the shape a Hermes subscription login writes."""
    return json.dumps(
        {
            "version": 1,
            "providers": {
                "openai-codex": {
                    "tokens": {"access_token": token, "refresh_token": "synthetic-refresh-0001"},
                    "last_refresh": "2026-10-08T00:00:00Z",
                    "auth_mode": "chatgpt",
                }
            },
            "active_provider": "openai-codex",
        }
    )


def hermes_stand_in(root: Path, behavior: dict[str, Any]) -> tuple[Path, Path]:
    """Write a stand-in for the Hermes API surface the adapter touches, and its profile home."""
    source = root / "hermes"
    for name, text in STUBS.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (source / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")
    home = root / "home"
    home.mkdir()
    subscription = behavior.get("credentials") == "subscription"
    (home / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n  default: synthetic-subscription-model\n"
        f"  base_url: {behavior['server_url']}\n  context_length: 272000\n"
        if subscription
        else "model:\n  provider: openrouter\n  default: synthetic-model\n",
        encoding="utf-8",
    )
    if subscription:
        if behavior.get("grant", True):
            (home / "auth.json").write_text(subscription_auth(), encoding="utf-8")
        if behavior.get("lock", True):
            (home / "auth.lock").write_text(" ", encoding="utf-8")
    # A disposable profile with a fake credential and files whose bytes are known: a Hermes that
    # reads or rewrites any of them is seen by the snapshot.
    for name, text in {
        ".env": "OPENROUTER_API_KEY=synthetic-disposable-key\n",
        "SOUL.md": "canary soul, never rewritten\n",
        "memories/MEMORY.md": "canary memory\n",
        "memories/USER.md": "canary user\n",
    }.items():
        canary = home / name
        canary.parent.mkdir(parents=True, exist_ok=True)
        canary.write_text(text, encoding="utf-8")
    return source, home


OPENCLAW_WRAPPER = """import json
import os
import runpy
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
extra = json.loads((here / "openclaw-env.json").read_text(encoding="utf-8"))
if extra.get("FAKE_OPENCLAW_RECORD"):
    # What the worker handed the runtime, before the stand-in's own knobs are added.
    with open(extra["FAKE_OPENCLAW_RECORD"], "a", encoding="utf-8") as handle:
        line = dict(event="environ", pid=os.getpid(), environ=dict(os.environ))
        handle.write(json.dumps(line) + chr(10))
os.environ.update(extra)
target = {target!r}
sys.argv = [target, *sys.argv[1:]]
runpy.run_path(target, run_name="__main__")
"""


def openclaw_stand_in(root: Path, behavior: dict[str, Any]) -> tuple[tuple[str, ...], Path, Path]:
    """Write a stand-in OpenClaw installation and a disposable profile; return how to run it.

    `behavior["profile"]` is the profile configuration (a fake provider that points at a loopback
    model) and `behavior["env"]` the stand-in's own knobs, which its wrapper sets because a decision
    passes the runtime nothing of the service's environment.
    """
    home = root / "home"
    (home / "state").mkdir(parents=True)
    home.chmod(0o700)
    config = home / "openclaw.json"
    config.write_text(json.dumps(behavior["profile"]), encoding="utf-8")
    (home / "state" / "canary.txt").write_text("canary state, never rewritten\n", encoding="utf-8")
    auth_store = home / "state" / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    auth_store.parent.mkdir(parents=True)
    sqlite3.connect(auth_store).close()
    (home / ".env").write_text("SYNTHETIC_KEY=synthetic-disposable-key\n", encoding="utf-8")
    target = str(Path(__file__).with_name("fake_openclaw.py").resolve())
    wrapper = root / "openclaw-wrapper.py"
    wrapper.write_text(OPENCLAW_WRAPPER.format(target=target), encoding="utf-8")
    (root / "openclaw-env.json").write_text(json.dumps(behavior.get("env", {})), encoding="utf-8")
    return (sys.executable, str(wrapper)), config, home / "state"


def fake_runtime(root: Path, behavior: dict[str, Any]) -> tuple[Path, Path]:
    """Write the fake runtime's behaviour file and its disposable profile; return both paths."""
    home = root / "home"
    home.mkdir()
    for name, text in {
        ".env": "SYNTHETIC_KEY=synthetic-disposable-key\n",
        "SOUL.md": "canary soul, never rewritten\n",
        "memories/MEMORY.md": "canary memory\n",
    }.items():
        canary = home / name
        canary.parent.mkdir(parents=True, exist_ok=True)
        canary.write_text(text, encoding="utf-8")
    behavior_file = root / "behavior.json"
    behavior_file.write_text(json.dumps(behavior), encoding="utf-8")
    return home, behavior_file


def snapshot(root: Path) -> dict[str, tuple[str, int, str, int]]:
    """Return path, type, size, SHA-256 and mtime of everything below root, recursively."""
    result: dict[str, tuple[str, int, str, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("symlink", 0, "", 0)
        elif path.is_dir():
            result[relative] = ("dir", 0, "", 0)
        else:
            data = path.read_bytes()
            result[relative] = (
                "file",
                len(data),
                hashlib.sha256(data).hexdigest(),
                path.stat().st_mtime_ns,
            )
    return result


def profile_changes(
    before: dict[str, tuple[str, int, str, int]], after: dict[str, tuple[str, int, str, int]]
) -> list[str]:
    """Say what differs between two snapshots: added, removed and changed paths."""
    return (
        [f"added {name}" for name in sorted(set(after) - set(before))]
        + [f"removed {name}" for name in sorted(set(before) - set(after))]
        + [f"changed {n}" for n in sorted(before.keys() & after.keys()) if before[n] != after[n]]
    )


def hermes_homes(record: Path) -> list[str]:
    """Return the Hermes homes the stand-in filled, as it recorded them."""
    if not record.exists():
        return []
    return [line for line in record.read_text(encoding="utf-8").splitlines() if line]


class StandInDriver:
    """A real driver, with its match process wrapped so that a test can shorten the bounds.

    The command the real driver builds is kept as it is, and three things are substituted: the
    wrapper that sets the bounds, optionally a mutated copy of the match program and optionally a
    mutated copy of the Hermes worker.
    """

    def __init__(
        self,
        real: Any,
        launcher: Path,
        bound: float,
        cleanup: float,
        match_file: Path | None,
        worker_file: Path | None,
    ) -> None:
        """Wrap this driver (the real Hermes one, a mutant of it, or the fake runtime's)."""
        self.real = real
        self.name, self.display_name = self.real.name, self.real.display_name
        self.capabilities = self.real.capabilities
        self.launcher, self.bound, self.cleanup = launcher, bound, cleanup
        self.match_file, self.worker_file = match_file, worker_file

    def launch(self, handle: Any, scratch: Path) -> Any:
        """Return the real launch with the test's wrapper and files put in."""
        launch = self.real.launch(handle, scratch)
        # [interpreter, -I, match, --, interpreter, -I, worker, source, --decision]
        command = list(launch.command)
        assert command[3] == "--"
        match = self.match_file or Path(command[2])
        command[2:3] = [str(self.launcher), str(match), str(self.bound), str(self.cleanup)]
        if self.worker_file is not None:
            command[command.index("--") + 3] = str(self.worker_file)
        return arena_driver.Launch(command, launch.environment)


class Tethers:
    """The test's ends of the sockets every stand-in Hermes process holds open while it lives."""

    def __init__(self) -> None:
        """Listen on loopback; each stand-in process connects once, when it starts."""
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.accepted: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        with contextlib.suppress(OSError):
            while True:
                connection, _ = self.server.accept()
                self.accepted.append(connection)

    def gone(self, wait: float = 15.0) -> int:
        """Require every connected process to be gone, and return how many there were."""
        deadline = time.monotonic() + wait
        for connection in list(self.accepted):
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                data = connection.recv(1)
            except (ConnectionResetError, ConnectionAbortedError):
                continue
            except TimeoutError:
                raise AssertionError("a Hermes process of the run is still alive") from None
            assert data == b"", "a Hermes process of the run sent data after it should be gone"
        return len(self.accepted)

    def close(self) -> None:
        """Stop listening and drop every connection."""
        self.server.close()
        for connection in self.accepted:
            with contextlib.suppress(OSError):
                connection.close()


@dataclass
class Process:
    """What a real parent and a real child did."""

    runner: Any
    child: Any
    forwarded: list[dict[str, Any]]
    commands: list[dict[str, Any]]
    log: list[dict[str, Any]]
    raw: str
    reports: list[str]
    seconds: float
    leftovers: list[str]
    tethers: Tethers
    profile: Path
    profile_before: dict[str, tuple[str, int, str, int]]
    profile_after: dict[str, tuple[str, int, str, int]]
    homes: list[str]
    env_reads: list[str]

    @property
    def events(self) -> list[str]:
        """The parent's service-log events, in order."""
        return [record["event"] for record in self.log]

    def at(self, event: str) -> float:
        """When the parent logged the event, on the test's clock."""
        return next(record["at"] for record in self.log if record["event"] == event)


def run_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    behavior: dict[str, Any],
    *,
    bound: float = 1.5,
    cleanup: float = 0.5,
    turns: int = 1,
    module: ModuleType = arena_runner,
    arena: Path | None = None,
    worker: Path | None = None,
    driver_module: ModuleType = arena_driver_hermes,
    runtime: str = "hermes",
    wait: float = 30.0,
) -> Process:
    """Start the real adapter as a real child of a real supervisor and let it play one turn."""
    tethers = Tethers()
    # Whatever the run creates in a temporary directory is created here, where it can be counted.
    scratch_parent = tmp_path / "scratch-parent"
    scratch_parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_parent))
    homes_record = tmp_path / "hermes-homes.stand-in-record"
    full = {**behavior, "tether": tethers.port, "record": str(homes_record)}
    if runtime == "hermes":
        source, home = hermes_stand_in(tmp_path, full)
        env_record = source / "dotenv-reads.stand-in-record"
    elif runtime == "openclaw":
        env_record = tmp_path / "unused.stand-in-record"
        command, config, state = openclaw_stand_in(tmp_path, full)
        home = config.parent
    else:
        env_record = tmp_path / "fake-env-reads.stand-in-record"
        home, behavior_file = fake_runtime(tmp_path, {**full, "env_reads": str(env_record)})
    launcher = tmp_path / "launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    profile_before = snapshot(home)
    monkeypatch.setattr(
        arena_match, "DECISION_SECONDS", {"chess": bound, "connect-four": bound}, raising=False
    )
    monkeypatch.setattr(arena_match, "CLEANUP_SECONDS", cleanup, raising=False)
    runner, owned = supervisor(module)
    runner_id = str(uuid.uuid4())
    runner.journal = SimpleNamespace(runner_id=runner_id, reserve=lambda identifier: True)
    runner.paths = SimpleNamespace(root=root)
    if runtime == "hermes":
        real: Any = driver_module.HermesArenaDriver()
        runner.handle = driver_module.HermesRun(source, Path(sys.executable), home)
    elif runtime == "openclaw":
        real = arena_driver_openclaw.OpenClawArenaDriver()
        runner.handle = arena_driver_openclaw.OpenClawRun(
            command, "2026.9.9", config, state, config.parent
        )
    else:
        real = FakeArenaDriver(behavior_file, home)
        runner.handle = real.inspect(None)
    runner.driver = StandInDriver(real, launcher, bound, cleanup, arena, worker)
    document = {**intent(owned.agent_id), "status": "starting", "claimed_by": runner_id}
    document.update(intent_id=owned.intent_id, match_id=owned.match_id)
    runner._post = lambda suffix, payload: document  # type: ignore[method-assign]
    reports: list[str] = []
    runner._report = reports.append  # type: ignore[method-assign]
    forwarded: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        commands.append(command)
        if command["operation"] == "game_move":
            forwarded.append(command)
        if len(forwarded) >= turns:
            return {"status": "ended"}
        return {
            "status": "active",
            "game_version": GAMES[role],
            "observation": {"you_are": role, "to_move": role, "private": PRIVATE},
        }

    monkeypatch.setattr(module.bridge, "_run_game_command", game)
    stamps: list[tuple[str, float]] = []
    original = module.diagnostic

    def stamped(intent_: Any, event: str, duration_ms: int = 0) -> None:
        stamps.append((event, time.monotonic()))
        original(intent_, event, duration_ms)

    monkeypatch.setattr(module, "diagnostic", stamped)
    begun = time.monotonic()
    module.ArenaRunner._launch(runner, owned)
    child = runner.child
    finished = runner.finished.wait(timeout=wait)
    seconds = time.monotonic() - begun
    if not finished:
        child.kill()  # the test's own cleanup of a child the code under test failed to end
    runner.stop_child()
    assert finished, "the child run never ended"
    log = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    for record, (event, moment) in zip(log, stamps, strict=True):
        assert record["event"] == event
        record["at"] = moment
    raw = json.dumps(log)
    leftovers = [
        name
        for name in sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
        if name not in before
        and "__pycache__" not in name
        and not name.endswith(".stand-in-record")
        # The profile is judged by its snapshot, which names every difference.
        and Path(name).parts[0] != home.name
    ]
    return Process(
        runner,
        child,
        forwarded,
        commands,
        log,
        raw,
        reports,
        seconds,
        leftovers,
        tethers,
        home,
        profile_before,
        snapshot(home),
        hermes_homes(homes_record),
        hermes_homes(env_record),
    )


def assert_profile_untouched(run: Process, allowed: frozenset[str] = frozenset()) -> None:
    """Require the disposable profile byte for byte as it was, and Hermes' own home gone.

    The stand-in fills the home it is given the way the real Hermes was measured to. If that home
    were the profile, the snapshot would differ; and if it is a scratch directory, none may remain.
    """
    changes = [
        change
        for change in profile_changes(run.profile_before, run.profile_after)
        if change.split(" ", 1)[1] not in allowed
    ]
    assert changes == [], f"the run changed the Hermes profile: {changes}"
    assert run.homes, "the stand-in never filled a Hermes home, so nothing was proven"
    for home in run.homes:
        assert Path(home) != run.profile, "Hermes ran in the profile"
        assert not Path(home).exists(), "a temporary Hermes home was left behind"
    # The credentials were read from the profile, which is only read, and from nowhere else.
    assert run.env_reads, "no credentials were asked for, so their source was not shown"
    assert set(run.env_reads) == {str(run.profile / ".env")}, "credentials came from elsewhere"


def assert_no_residue(run: Process, allowed: frozenset[str] = frozenset()) -> None:
    """Require the child dead and reaped, its pipes shut and no thread of ours lingering."""
    assert_profile_untouched(run, allowed)
    assert run.child.poll() is not None, "the child process is still running"
    assert run.child.stdin.closed and run.child.stdout.closed
    assert run.runner.worker is None and run.runner.child is None
    assert not [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    assert run.leftovers == [], "the run left files behind"
    try:
        run.tethers.gone()
    finally:
        run.tethers.close()


def moves_of(run: Process, role: str) -> list[dict[str, Any]]:
    """Return the move payloads the provider received, without the parent's bound match fields."""
    return [
        {k: v for k, v in move.items() if k not in {"match_id", "seat", "operation"}}
        for move in run.forwarded
    ]


def mutated_programs(
    folder: Path,
    *,
    match: tuple[tuple[str, str], ...] = (),
    worker: tuple[tuple[str, str], ...] = (),
    worker_module: ModuleType = hermes_arena,
    siblings: tuple[ModuleType, ...] = (),
) -> tuple[Path, Path]:
    """Write the match program and a worker side by side with these lines replaced.

    The worker loads its sibling `arena_match.py` by path, so both files live in one folder, and so
    does any other program it starts (`siblings`). Each replaced line must occur exactly once.
    """
    folder.mkdir(parents=True)
    for sibling in siblings:
        (folder / Path(str(sibling.__file__)).name).write_text(
            Path(str(sibling.__file__)).read_text(encoding="utf-8"), encoding="utf-8"
        )
    paths = []
    for module, name, replacements in (
        (arena_match, "arena_match.py", match),
        (worker_module, Path(str(worker_module.__file__)).name, worker),
    ):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for original, replacement in replacements:
            assert source.count(original) == 1, original
            source = source.replace(original, replacement)
        (folder / name).write_text(source, encoding="utf-8")
        paths.append(folder / name)
    return paths[0], paths[1]


def lax_match_process(tmp_path: Path) -> tuple[Path, Path]:
    """Return copies of the programs whose match process never ends a decision on its own."""
    return mutated_programs(
        tmp_path / "lax", match=(("message = worker.get(remaining)", "message = worker.get(3600)"),)
    )
