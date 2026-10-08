"""#223: the real decision worker, as a real process, against a stand-in for Hermes.

The worker is the only process that holds Hermes, and the only one that can hang on it. These tests
run a real supervisor, a real match process and a real worker process, with a stand-in for the
Hermes API surface whose model blocks, is slow or is fast, whose closing request never answers and
whose cleanup hangs or raises. Every bound is small enough to wait for. After each run no process of
the run may be left: each stand-in Hermes process holds a socket open while it lives, and the test
requires every one of them to be closed (a "tether").

The real Hermes revision is exercised the same way by hand, not here, because it needs the Hermes
checkout; the pull request reports it.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from arena_fakes import (
    MOVES,
    PRIVATE,
    intent,
    load_mutant,
    supervisor,
)

from agentnexus_sdk import arena_runner, hermes_arena

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

arena, source, bound, cleanup = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("hermes_arena", arena)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.DECISION_SECONDS = {"chess": float(bound), "connect-four": float(bound)}
module.CLEANUP_SECONDS = float(cleanup)
sys.argv = [arena, source]
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
    "hermes_cli/runtime_provider.py": """\
def resolve_runtime_provider(**kwargs):
    return {
        "provider": "openrouter",
        "base_url": "http://127.0.0.1:9",
        "api_key": "synthetic-key",
        "api_mode": "chat_completions",
    }
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
def dotenv_values(*args, **kwargs):
    return {}
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
        **rest,
    ):
        self.tools = model_tools.get_tool_definitions(enabled_toolsets=enabled_toolsets)

    def run_conversation(self, prompt):
        print("synthetic-private-model-output")
        mode = BEHAVIOR["mode"]
        if mode == "block":
            forever()
        if mode == "slow":
            time.sleep(BEHAVIOR["seconds"])
        if mode in ("fast", "slow"):
            model_tools.handle_function_call("game_move", BEHAVIOR["arguments"])
            if BEHAVIOR.get("tail") == "hang":
                # Hermes' ordinary closing request, issued once the tool result is back.
                forever()
        return {"failed": False}

    def close(self):
        how = BEHAVIOR.get("close", "ok")
        if how == "hang":
            forever()
        if how == "raise":
            raise RuntimeError("synthetic-private-close-failure")
""",
}


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
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: synthetic-model\n", encoding="utf-8"
    )
    return source, home


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
    wait: float = 30.0,
) -> Process:
    """Start the real adapter as a real child of a real supervisor and let it play one turn."""
    tethers = Tethers()
    source, home = hermes_stand_in(tmp_path, {**behavior, "tether": tethers.port})
    arena_file = arena or Path(hermes_arena.__file__)
    launcher = tmp_path / "launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    monkeypatch.setattr(
        hermes_arena, "DECISION_SECONDS", {"chess": bound, "connect-four": bound}, raising=False
    )
    monkeypatch.setattr(hermes_arena, "CLEANUP_SECONDS", cleanup, raising=False)
    runner, owned = supervisor(module)
    runner_id = str(uuid.uuid4())
    runner.journal = SimpleNamespace(runner_id=runner_id, reserve=lambda identifier: True)
    runner.paths = SimpleNamespace(root=root)
    runner.runtime = SimpleNamespace(
        home=home,
        command=lambda: [
            sys.executable,
            "-I",
            str(launcher),
            str(arena_file),
            str(source),
            str(bound),
            str(cleanup),
        ],
    )
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
        if name not in before and "__pycache__" not in name
    ]
    return Process(
        runner, child, forwarded, commands, log, raw, reports, seconds, leftovers, tethers
    )


def assert_no_residue(run: Process) -> None:
    """Require the child dead and reaped, its pipes shut and no thread of ours lingering."""
    assert run.child.poll() is not None, "the child process is still running"
    assert run.child.stdin.closed and run.child.stdout.closed
    assert run.runner.worker is None and run.runner.child is None
    assert not [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    assert run.leftovers == [], "the run left files behind"
    try:
        run.tethers.gone()
    finally:
        run.tethers.close()


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_blocked_model_transport_is_ended_at_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
) -> None:
    """A model that never answers is killed at the cutoff: no move, one diagnostic, no residue."""
    run = run_process(monkeypatch, capsys, tmp_path, role, {"mode": "block"})
    assert run.forwarded == []
    assert run.events.count("decision_budget_expired") == 1
    assert "game_move_started" not in run.events
    assert run.child.returncode != 0
    # The supervisor's next tick sees a finished child whose game did not end and reports it
    # `refused`: the run is closed, not continued and not completed.
    assert run.reports == ["playing"] and not run.runner.terminal
    waited = run.at("decision_budget_expired") - run.at("model_call_started")
    assert 1.4 <= waited < 6, f"the decision ended after {waited:.2f} s, not at its 1.5 s bound"
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw
    assert all(c["match_id"] and c["seat"] for c in run.commands)


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_slow_model_cannot_reach_game_move_after_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
) -> None:
    """A model that finally answers with a valid move after the cutoff gets nothing forwarded."""
    behavior = {"mode": "slow", "seconds": 4.0, "arguments": MOVES[role]}
    run = run_process(monkeypatch, capsys, tmp_path, role, behavior)
    assert run.forwarded == []
    assert run.events.count("decision_budget_expired") == 1
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_fast_model_still_plays_exactly_one_move(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
) -> None:
    """The ordinary decision through real processes: one move, a clean finish, no expiry."""
    behavior = {"mode": "fast", "arguments": MOVES[role]}
    run = run_process(monkeypatch, capsys, tmp_path, role, behavior, bound=20.0)
    assert [m["operation"] for m in run.forwarded] == ["game_move"]
    assert {
        k: v for k, v in run.forwarded[0].items() if k not in {"match_id", "seat", "operation"}
    } == MOVES[role]
    assert run.events.count("model_call_started") == 1
    assert run.events.count("model_call_returned") == 1
    assert "decision_budget_expired" not in run.events and "late_move_refused" not in run.events
    assert run.runner.terminal and run.runner.playing
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw


def moves_of(run: Process, role: str) -> list[dict[str, Any]]:
    """Return the move payloads the provider received, without the parent's bound match fields."""
    return [
        {k: v for k, v in move.items() if k not in {"match_id", "seat", "operation"}}
        for move in run.forwarded
    ]


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_hanging_closing_request_cannot_hold_the_run_between_two_own_turns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
) -> None:
    """An accepted move ends its decision: Hermes' closing request is never waited for.

    Two own turns are separated by an instant reply of the provider's computer. After each move the
    stand-in Hermes would issue the closing request and block on it for ever; both moves must still
    be made, one for each turn, and the run must end as a finished game.
    """
    behavior = {"mode": "fast", "arguments": MOVES[role], "tail": "hang"}
    run = run_process(monkeypatch, capsys, tmp_path, role, behavior, bound=10.0, turns=2)
    assert moves_of(run, role) == [MOVES[role], MOVES[role]], "one move for each of the two turns"
    assert run.events.count("model_call_started") == 2
    assert run.events.count("model_call_returned") == 2
    assert "decision_budget_expired" not in run.events and "late_move_refused" not in run.events
    assert run.runner.terminal and run.runner.playing
    assert run.seconds < 8, "the run waited for a closing request instead of the next turn"
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_hanging_agent_close_after_an_accepted_move_costs_the_match_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
) -> None:
    """A cleanup that never ends is cut off, reported, and replaced; the match plays on."""
    behavior = {"mode": "fast", "arguments": MOVES[role], "close": "hang"}
    run = run_process(monkeypatch, capsys, tmp_path, role, behavior, bound=10.0, turns=2)
    assert moves_of(run, role) == [MOVES[role], MOVES[role]]
    assert run.events.count("decision_cleanup_expired") == 2
    phases = {"model_call_started", "decision_cleanup_expired", "model_call_returned"}
    # The parent's window stays open until the cleanup is over: returned comes last.
    assert [e for e in run.events if e in phases] == [
        "model_call_started",
        "decision_cleanup_expired",
        "model_call_returned",
    ] * 2
    assert "decision_budget_expired" not in run.events
    assert run.runner.terminal and run.runner.playing
    assert run.seconds < 9
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw


@pytest.mark.parametrize("role", ["white", "first"])
def test_an_agent_close_that_raises_is_reported_and_the_match_plays_on(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
) -> None:
    """A cleanup failure is one fixed diagnostic: no retry of the move, no text of the failure."""
    behavior = {"mode": "fast", "arguments": MOVES[role], "close": "raise"}
    run = run_process(monkeypatch, capsys, tmp_path, role, behavior, bound=10.0, turns=2)
    assert moves_of(run, role) == [MOVES[role], MOVES[role]]
    assert run.events.count("decision_cleanup_failed") == 2
    assert run.runner.terminal and run.runner.playing
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw


# ---------------------------------------------------------------------------------------------
# A diagnostic is visible while the service runs, not when it ends
# ---------------------------------------------------------------------------------------------

EMITTER = """\
import importlib.util
import sys
import time
import uuid

spec = importlib.util.spec_from_file_location("arena_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
when = module.dt.datetime.now(module.dt.UTC)
intent = module.StartIntent(
    str(uuid.uuid4()), str(uuid.uuid4()), "first", str(uuid.uuid4()), when, "queued", None, when
)
module.diagnostic(intent, "run_started", 5)
print("ready", file=sys.stderr, flush=True)
time.sleep(60)
"""


def read_line(stream: Any, wait: float) -> str | None:
    """Return the next line of a pipe if it arrives within `wait` seconds, else None."""
    answer: list[str] = []
    reader = threading.Thread(target=lambda: answer.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(timeout=wait)
    return answer[0] if answer and answer[0] else None


def first_line_from_a_pipe(arena_file: Path, tmp_path: Path) -> str | None:
    """Run the emitter with stdout on a pipe, as under systemd, and read its line while it lives.

    The emitter says `ready` on stderr once its diagnostic call has returned, so the wait that
    follows is for the line to be readable and not for a slow interpreter to start. The event is
    one the previous release already knew, so this holds the flush and nothing new.
    """
    script = tmp_path / "emitter.py"
    script.write_text(EMITTER, encoding="utf-8")
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    process = subprocess.Popen(  # noqa: S603 - this interpreter and a script this test wrote
        [sys.executable, str(script), str(arena_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        for _ in range(20):
            if (line := read_line(process.stderr, 60)) is None or line.strip() == "ready":
                break
        assert line is not None, "the emitter never got to its diagnostic call"
        first = read_line(process.stdout, 2)
        assert process.poll() is None, "the emitter ended before the line could be read early"
        return first
    finally:
        process.kill()
        process.wait(timeout=10)


def test_a_diagnostic_reaches_a_pipe_while_the_service_is_still_running(tmp_path: Path) -> None:
    """Under a pipe, as under systemd, the line is readable at once, not when the run ends."""
    line = first_line_from_a_pipe(Path(arena_runner.__file__), tmp_path)
    assert line is not None, "the diagnostic stayed in the process's buffer"
    record = json.loads(line)
    assert record["event"] == "run_started" and record["duration_ms"] == 5


# ---------------------------------------------------------------------------------------------
# Mutation proofs: weaken one condition, require the guard to notice, restore
# ---------------------------------------------------------------------------------------------


def lax_match_process(tmp_path: Path) -> Path:
    """Return a copy of the adapter whose match process never ends a decision on its own."""
    source = Path(hermes_arena.__file__).read_text(encoding="utf-8")
    original = "message = worker.get(remaining)"
    assert source.count(original) == 1
    path = tmp_path / "lax" / "hermes_arena.py"
    path.parent.mkdir()
    path.write_text(source.replace(original, "message = worker.get(3600)"), encoding="utf-8")
    return path


def test_the_parent_ends_a_match_process_that_does_not_end_its_own_decision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The second line: the parent's window kills the match process, and its worker dies with it."""
    lax = lax_match_process(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run = run_process(monkeypatch, capsys, run_dir, "white", {"mode": "block"}, arena=lax)
    assert run.forwarded == []
    assert run.events.count("decision_budget_expired") == 1
    assert_no_residue(run)


def test_a_parent_that_does_not_kill_leaves_a_match_process_that_does_not_end_itself(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Without the parent's kill the second line is gone: the process-level proof notices."""
    lax = lax_match_process(tmp_path)
    broken = load_mutant(
        tmp_path / "mutant", arena_runner, "child.kill()  # decision cutoff", "pass"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    try:
        with pytest.raises(AssertionError, match="never ended"):
            run_process(
                monkeypatch,
                capsys,
                run_dir,
                "white",
                {"mode": "block"},
                module=broken,
                arena=lax,
                wait=6.0,
            )
    finally:
        sys.modules.pop(broken.__name__, None)


def test_a_diagnostic_that_is_not_flushed_stays_in_the_buffer(tmp_path: Path) -> None:
    """Weakened to `flush=False`, the same pipe read finds nothing until the process ends."""
    source = Path(arena_runner.__file__).read_text(encoding="utf-8")
    assert source.count("flush=True,") == 1
    path = tmp_path / "arena_runner_buffered.py"
    path.write_text(source.replace("flush=True,", "flush=False,"), encoding="utf-8")
    assert first_line_from_a_pipe(path, tmp_path) is None


def test_a_worker_that_outlives_its_match_process_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Without the worker's own exit on a closed pipe, the parent's kill leaves a Hermes behind."""
    source = Path(hermes_arena.__file__).read_text(encoding="utf-8")
    for original, replacement in (
        ("message = worker.get(remaining)", "message = worker.get(3600)"),
        ("exit_hard(3)", "pass"),
    ):
        assert source.count(original) == 1
        source = source.replace(original, replacement)
    broken = tmp_path / "broken" / "hermes_arena.py"
    broken.parent.mkdir()
    broken.write_text(source, encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run = run_process(monkeypatch, capsys, run_dir, "white", {"mode": "block"}, arena=broken)
    assert run.forwarded == []
    with pytest.raises(AssertionError, match="still alive"):
        assert_no_residue(run)
