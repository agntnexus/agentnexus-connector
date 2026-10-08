"""In-process stand-ins for the decision worker, its parent and a provider seat (#223).

`hermes_arena.main` plays a game through a decision worker: a process that holds Hermes. Here the
worker is a function running in a helper thread that speaks the worker's protocol, so the match
process's own logic (the turn clock, the cutoff, the one-move gate, the cleanup bound, replacing a
worker) runs unchanged under a fake clock. The real worker is exercised as a real process, against
a stand-in for the Hermes API surface, in `test_arena_decision_worker.py`.
"""

from __future__ import annotations

import importlib.util
import json
import queue
import sys
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from agentnexus_sdk import arena_runner, hermes_arena

PRIVATE = "synthetic-private-observation"
FLAGS = ("HERMES_SAFE_MODE", "HERMES_IGNORE_RULES", "HERMES_IGNORE_USER_CONFIG")
#: One move for each role: Chess is UCI, Connect Four a column.
MOVES: dict[str, dict[str, Any]] = {
    "white": {"move": "e2e4"},
    "black": {"move": "e7e5"},
    "first": {"column": 3},
    "second": {"column": 4},
}
HUNG = object()
KILL = object()


class Clock:
    """A clock that moves only when a test, a fake model or a sleep moves it."""

    def __init__(self, *, still: bool = False) -> None:
        """Start well away from zero; a `still` clock does not move when something sleeps."""
        self.now = 1000.0
        self.still = still

    def monotonic(self) -> float:
        """Return the fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Sleeping spends fake time and no real time."""
        if not self.still:
            self.now += max(0.0, seconds)


class FakeParent:
    """The parent's end of the match process's private stdio, answering from a script."""

    def __init__(self, answer: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        """Queue the one start request the real parent sends, then answer as requests come."""
        self.answer = answer
        start = {"match_id": str(uuid.uuid4()), "seat": "first", "seconds": 3600}
        self.replies: deque[str] = deque([json.dumps(start) + "\n"])
        self.messages: list[dict[str, Any]] = []
        self.raw = ""
        self._partial = ""

    def write(self, text: str) -> int:
        """Take what the match process writes; a request is answered, a diagnostic recorded."""
        self.raw += text
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            message = json.loads(line)
            self.messages.append(message)
            if "diagnostic" not in message and message != {"finished": True}:
                self.replies.append(json.dumps({"result": self.answer(message)}) + "\n")
        return len(text)

    def flush(self) -> None:
        """Nothing is buffered."""

    def readline(self, limit: int = -1) -> str:
        """Give the match process its next reply, or end of input."""
        return self.replies.popleft() if self.replies else ""


class Match:
    """A scripted provider seat: whose turn it is, what a move does and what the seat is shown."""

    def __init__(
        self,
        role: str,
        clock: Clock,
        *,
        turns: int = 1,
        waits: int = 0,
        join_seconds: float = 0.0,
        refuse_first: bool = False,
        flip_on_read: int | None = None,
        read_seconds: float = 0.0,
    ) -> None:
        """Play `turns` of this seat's turns; the opponent takes `waits` reads to answer.

        With `flip_on_read` the turn passes to the opponent and back on that read without any move
        of ours, as it does when the provider moves a turn on by its own rules.
        """
        self.role, self.other = role, hermes_arena.ROLES[role]
        self.clock, self.turns, self.waits = clock, turns, waits
        self.join_seconds, self.refuse_first = join_seconds, refuse_first
        self.flip_on_read, self.reads, self.read_seconds = flip_on_read, 0, read_seconds
        self.accepted = 0
        self.refused = False
        self.waiting = 0
        self.forwarded: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def view(self) -> dict[str, Any]:
        """Return what the seat is shown now."""
        if self.accepted >= self.turns:
            return {"status": "ended"}
        to_move = self.other if self.waiting > 0 else self.role
        return {
            "status": "active",
            "observation": {"you_are": self.role, "to_move": to_move, "private": PRIVATE},
        }

    def answer(self, message: dict[str, Any]) -> dict[str, Any]:
        """Answer one request as the provider would for this seat."""
        self.requests.append(message)
        operation = message["operation"]
        if operation == "game_join":
            self.clock.now += self.join_seconds
            return self.view()
        if operation == "game_move":
            self.forwarded.append(message)
            if self.refuse_first and not self.refused:
                self.refused = True
                return {"error": "provider.move_not_legal"}
            self.accepted += 1
            self.waiting = self.waits
            return self.view()
        self.reads += 1
        self.clock.now += self.read_seconds
        if self.reads == self.flip_on_read:
            self.waiting = 2
        view = self.view()
        self.waiting = max(0, self.waiting - 1)
        return view


class Staying:
    """A provider seat that stays on this seat's turn for `turns` reads and then maybe ends."""

    def __init__(self, role: str, turns: int, *, end: bool) -> None:
        """Show the seat's own turn `turns` times, then an ended game if `end`."""
        self.role, self.turns, self.end, self.reads = role, turns, end, 0
        self.requests: list[dict[str, Any]] = []
        self.forwarded: list[dict[str, Any]] = []

    def answer(self, message: dict[str, Any]) -> dict[str, Any]:
        """Answer one request."""
        self.requests.append(message)
        if message["operation"] == "game_move":
            self.forwarded.append(message)
        turn = {"status": "active", "observation": {"you_are": self.role, "to_move": self.role}}
        if message["operation"] != "game_state":
            return turn
        self.reads += 1
        return {"status": "ended"} if self.end and self.reads >= self.turns else turn


class Scripted:
    """A provider seat that answers from a fixed list of states, in order."""

    def __init__(self, states: list[dict[str, Any]]) -> None:
        """Answer the n-th request with the n-th state."""
        self.states = deque(states)
        self.requests: list[dict[str, Any]] = []
        self.forwarded: list[dict[str, Any]] = []

    def answer(self, message: dict[str, Any]) -> dict[str, Any]:
        """Answer one request with the next state."""
        self.requests.append(message)
        if message["operation"] == "game_move":
            self.forwarded.append(message)
        return self.states.popleft()


class Killed(BaseException):
    """The stand-in worker was killed while it waited."""


class Complete(BaseException):
    """The stand-in's own `DecisionComplete`: the match told the decision it is over."""


class Decision:
    """What a fake model can see and do during one decision."""

    def __init__(self, worker: FakeWorker, command: dict[str, Any], index: int) -> None:
        """Remember the decide command; `started` is the fake time it arrived."""
        self.worker, self.command, self.index = worker, command, index
        self.clock = worker.clock
        self.started = worker.clock.now
        self.role = command["role"]

    def call(self, operation: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call one of the three tools, as Hermes' dispatch would, and wait for the answer."""
        move = MOVES[self.role] if arguments is None else arguments
        self.worker.out.put({"operation": operation, **move})
        reply = self.worker.inbox.get()
        if reply is KILL:
            raise Killed
        if reply == {"complete": True}:
            raise Complete
        return reply["result"]

    def block(self) -> None:
        """Never return, as a transport or cleanup that never answers; only a kill ends it."""
        self.worker.out.put(HUNG)
        while self.worker.inbox.get() is not KILL:
            pass
        raise Killed


class FakeWorker:
    """A decision worker that is a function in a helper thread, speaking the worker's protocol."""

    def __init__(self, workers: Workers) -> None:
        """Start, and say `ready` as a configured worker does."""
        self.workers, self.clock = workers, workers.clock
        self.inbox: queue.Queue[Any] = queue.Queue()
        self.out: queue.Queue[Any] = queue.Queue()
        self.dead = self.ended = False
        self.served = 0
        self.out.put({"ready": True} if workers.ready else HUNG)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            while True:
                item = self.inbox.get()
                if item is KILL or item == {"stop": True}:
                    return
                self.workers.commands.append(item)
                self.served += 1
                index = self.workers.decisions
                self.workers.decisions += 1
                self._decide(item["decide"], index)
        except Killed:
            return
        finally:
            self.out.put(None)

    def _decide(self, command: dict[str, Any], index: int) -> None:
        decision = Decision(self, command, index)
        try:
            result = self.workers.behavior_for(index)(decision)
        except Complete:
            self.out.put({"decision": "completed"})
        except Exception:
            self.out.put({"decision": "exception"})
        else:
            if not isinstance(result, dict):
                outcome = "invalid"
            elif result.get("failed") or result.get("error"):
                outcome = "failed"
            else:
                outcome = "ok"
            self.out.put({"decision": "returned", "outcome": outcome})
        how = self.workers.close_for(index)
        if how == "hang":
            decision.block()
        if isinstance(how, tuple):  # ("after", seconds): the cleanup takes this long
            self.clock.now += how[1]
        if how == "chatty":  # a request after the decision is over: it must not be served
            self.out.put({"operation": "game_move", **MOVES[decision.role]})
        failed = how == "raise"
        self.out.put({"decision": "close_failed" if failed else "closed"})

    # -- the interface the match process uses ------------------------------------------------

    def send(self, document: dict[str, Any]) -> bool:
        """Write one line to the worker; false when it is already gone."""
        if self.dead:
            return False
        self.inbox.put(document)
        return True

    def get(self, timeout: float) -> dict[str, Any] | None:
        """Return the next message, None once ended; a hung worker lets the fake time pass."""
        if self.ended:
            return None
        item = self.out.get(timeout=10)
        if item is HUNG:
            self.clock.now += max(0.0, timeout)
            raise queue.Empty
        if item is None:
            self.ended = True
            return None
        return item  # type: ignore[no-any-return]

    def alive(self) -> bool:
        """Return whether the worker still runs."""
        return not self.dead and not self.ended

    def kill(self) -> None:
        """End the worker now."""
        self.dead = True
        self.inbox.put(KILL)

    def close(self) -> None:
        """End the worker if it still runs and wait for its thread."""
        self.kill()
        self.thread.join(timeout=5)


class Workers:
    """The factory `spawn_worker` is replaced with, and a record of what it made."""

    def __init__(
        self,
        clock: Clock,
        behavior: Callable[[Decision], Any] | list[Callable[[Decision], Any]],
        close: Any = "ok",
        *,
        ready: bool = True,
    ) -> None:
        """Take a behaviour for each decision (the last repeats) and a cleanup ending for each."""
        self.clock, self.behavior, self.close_mode, self.ready = clock, behavior, close, ready
        self.spawned: list[FakeWorker] = []
        self.commands: list[dict[str, Any]] = []
        self.decisions = 0

    def behavior_for(self, index: int) -> Callable[[Decision], Any]:
        """Return the behaviour of the n-th decision of the run."""
        if isinstance(self.behavior, list):
            return self.behavior[min(index, len(self.behavior) - 1)]
        return self.behavior

    def close_for(self, index: int) -> Any:
        """Return how the n-th decision's cleanup ends (see `FakeWorker._decide`)."""
        if isinstance(self.close_mode, list):
            return self.close_mode[min(index, len(self.close_mode) - 1)]
        return self.close_mode

    def __call__(self, source: str) -> FakeWorker:
        """Start a worker, as `spawn_worker` does."""
        worker = FakeWorker(self)
        self.spawned.append(worker)
        return worker


def move_after(seconds: float, *, times: int = 1) -> Callable[[Decision], Any]:
    """Return a model that thinks for `seconds` and then makes its move `times` times."""

    def behave(decision: Decision) -> Any:
        decision.clock.now = decision.started + seconds
        for _ in range(times):
            decision.call("game_move")
        return {"failed": False}

    return behave


def no_move(seconds: float = 0.0) -> Callable[[Decision], Any]:
    """Return a model that thinks for `seconds` and then ends without any tool call."""

    def behave(decision: Decision) -> Any:
        decision.clock.now = decision.started + seconds
        return {"failed": False}

    return behave


@dataclass
class Played:
    """What one run of the match process did."""

    code: int | None
    error: BaseException | None
    match: Any
    parent: FakeParent
    clock: Clock
    workers: Workers
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    @property
    def events(self) -> list[str]:
        """The diagnostic names, in order."""
        return [message["diagnostic"] for message in self.diagnostics]

    @property
    def budgets(self) -> list[float]:
        """The seconds each decision was given, as the match process told the worker."""
        return [command["decide"]["seconds"] for command in self.workers.commands]

    @property
    def operations(self) -> list[str]:
        """The operations the parent was asked to do, in order."""
        return [message["operation"] for message in self.match.requests]


def play(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    behavior: Callable[[Decision], Any] | list[Callable[[Decision], Any]],
    *,
    module: ModuleType = hermes_arena,
    close: Any = "ok",
    provider: Any = None,
    still: bool = False,
    ready: bool = True,
    **options: Any,
) -> Played:
    """Run the match process against a scripted seat, a fake clock and fake workers."""
    clock = Clock(still=still)
    seat = provider or Match(role, clock, **options)
    parent = FakeParent(seat.answer)
    workers = Workers(clock, behavior, close, ready=ready)
    played = Played(None, None, seat, parent, clock, workers)
    monkeypatch.setattr(module, "spawn_worker", workers)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    for name in FLAGS:
        monkeypatch.setenv(name, "1")
    monkeypatch.setattr(sys, "argv", ["hermes_arena.py", "."])
    monkeypatch.setattr(sys, "stdin", parent)
    monkeypatch.setattr(sys, "stdout", parent)
    try:
        played.code = module.main()
    except Exception as error:
        played.error = error
    played.diagnostics = [m for m in parent.messages if "diagnostic" in m]
    return played


def intent(agent_id: str) -> dict[str, object]:
    """Return synthetic fixed operation data, with no prompt or runtime credential."""
    soon = arena_runner.dt.datetime.now(arena_runner.dt.UTC) + arena_runner.dt.timedelta(minutes=5)
    return {
        "intent_id": str(uuid.uuid4()),
        "match_id": str(uuid.uuid4()),
        "seat": "first",
        "agent_id": agent_id,
        "expires_at": soon.isoformat(),
        "status": "queued",
        "claimed_by": None,
        "run_until": soon.isoformat(),
    }


def supervisor(module: ModuleType = arena_runner) -> tuple[Any, Any]:
    """Construct a synthetic supervisor without opening a profile, key, process or connection."""
    agent = str(uuid.uuid4())
    owned = module.StartIntent.parse(intent(agent), agent_id=agent)
    runner = object.__new__(module.ArenaRunner)
    runner.config = SimpleNamespace(agent_id=agent)
    runner.client = object()
    runner.finished = threading.Event()
    runner.playing = runner.terminal = False
    runner._report = lambda status: None
    return runner, owned


def load_mutant(tmp_path: Path, module: ModuleType, original: str, replacement: str) -> ModuleType:
    """Return a copy of the module's source with exactly one condition weakened."""
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    if source.count(original) != 1:
        raise AssertionError(f"the guarded line is not unique: {original!r}")
    name = module.__name__.rsplit(".", 1)[-1] + "_mutant"
    path = tmp_path / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.replace(original, replacement), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("the mutant cannot be loaded")
    mutant = importlib.util.module_from_spec(spec)
    sys.modules[name] = mutant
    spec.loader.exec_module(mutant)
    return mutant


def expect_guard(
    oracle: Callable[[ModuleType], None], module: ModuleType, mutant: ModuleType
) -> None:
    """Require the oracle to hold for the restored source and fail for the weakened copy."""
    oracle(module)
    try:
        with pytest.raises(AssertionError):
            oracle(mutant)
    finally:
        sys.modules.pop(mutant.__name__, None)
