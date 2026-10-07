"""#223: a model decision ends inside the provider's turn, and nothing it sends can arrive late.

The Chess and Connect Four providers give a seat 60 seconds per turn (`D-134` TL-4, `D-170` CH-4).
Hermes' `run_budget_seconds` only advises the model and caps an implicit stale timeout at 60 seconds
or more; it never interrupts a blocked model call. So the bound is enforced twice, from outside the
model: the child refuses a move at its own cutoff, and the parent, which holds the signing key and
its own clock, forwards no move at or after the cutoff and kills a child still running at it.

The contract numbers are written out here instead of imported, so that changing the module's
constants cannot also change what these tests demand.

Three layers, each against the same behaviour:

* the child (`hermes_arena.main`) with a fake clock and a scripted provider;
* the parent (`ArenaRunner._serve`) with scripted child streams, a fake clock and a real timer;
* a real parent and a real child process, with a stand-in for the Hermes API surface whose model
  blocks, is slow or is fast, and a bound small enough to wait for.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest

from agentnexus_sdk import arena_runner, games, hermes_arena

PROVIDER_TURN = 60.0
RESERVE = 15.0
LIMIT = PROVIDER_TURN - RESERVE
POLL = 4.0
#: One move for each role: Chess is UCI, Connect Four a column.
MOVES: dict[str, dict[str, Any]] = {
    "white": {"move": "e2e4"},
    "black": {"move": "e7e5"},
    "first": {"column": 3},
    "second": {"column": 4},
}
ROLES = ["white", "black", "first", "second"]
GAMES = {"white": "chess-1-solo", "black": "chess-1-solo", "first": "connect-four-1-solo"}
GAMES["second"] = GAMES["first"]
PRIVATE = "synthetic-private-observation"
FLAGS = ("HERMES_SAFE_MODE", "HERMES_IGNORE_RULES", "HERMES_IGNORE_USER_CONFIG")


# ---------------------------------------------------------------------------------------------
# The child, in this process, with a fake clock
# ---------------------------------------------------------------------------------------------


class Clock:
    """A clock that moves only when a test, a fake model or a sleep moves it."""

    def __init__(self) -> None:
        """Start well away from zero, so a first read never looks like it just happened."""
        self.now = 1000.0

    def monotonic(self) -> float:
        """Return the fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Sleeping spends fake time and no real time."""
        self.now += max(0.0, seconds)


class FakeParent:
    """The parent's end of the child's private stdio, answering every request from a script."""

    def __init__(self, answer: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        """Queue the one start request the real parent sends, then answer as requests come."""
        self.answer = answer
        start = {"match_id": str(uuid.uuid4()), "seat": "first", "seconds": 3600}
        self.replies: deque[str] = deque([json.dumps(start) + "\n"])
        self.messages: list[dict[str, Any]] = []
        self.raw = ""
        self._partial = ""

    def write(self, text: str) -> int:
        """Take what the child writes; a request is answered at once, a diagnostic is recorded."""
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
        """Give the child its next reply, or end of input."""
        return self.replies.popleft() if self.replies else ""


class Match:
    """A scripted provider seat: whose turn it is, what a move does and what the model sees."""

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


@dataclass
class Decision:
    """What a fake model can see and do during one decision."""

    clock: Clock
    handler: Callable[[str, Any], Any]
    kwargs: dict[str, Any]
    index: int
    started: float
    role: str

    def call(self, operation: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call one of the three tools, as Hermes' patched dispatch would."""
        return self.handler(operation, MOVES[self.role] if arguments is None else arguments)


@dataclass
class Played:
    """What one run of the child did."""

    code: int | None
    error: BaseException | None
    constructed: list[dict[str, Any]]
    match: Match
    parent: FakeParent
    clock: Clock
    closed: int = 0
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    @property
    def events(self) -> list[str]:
        """The diagnostic names, in order."""
        return [message["diagnostic"] for message in self.diagnostics]

    @property
    def budgets(self) -> list[float]:
        """The `run_budget_seconds` each decision's agent was given."""
        return [kwargs["run_budget_seconds"] for kwargs in self.constructed]


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


def play(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    behavior: Callable[[Decision], Any] | list[Callable[[Decision], Any]],
    *,
    module: ModuleType = hermes_arena,
    **options: Any,
) -> Played:
    """Run the child against a scripted seat, with fake time and a fake model."""
    clock = Clock()
    match = Match(role, clock, **options)
    parent = FakeParent(match.answer)
    constructed: list[dict[str, Any]] = []
    handlers: list[Callable[[str, Any], Any]] = []
    script = behavior if isinstance(behavior, list) else None
    played = Played(None, None, constructed, match, parent, clock)

    class SyntheticAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.index = len(constructed)
            self.started = clock.now
            self.kwargs = kwargs
            constructed.append(kwargs)
            self.tools = [{"function": {"name": name}} for name in sorted(hermes_arena.TOOLS)]

        def run_conversation(self, prompt: str) -> Any:
            print("synthetic-private-model-output")
            step = script[min(self.index, len(script) - 1)] if script else behavior
            assert callable(step)
            return step(Decision(clock, handlers[0], self.kwargs, self.index, self.started, role))

        def close(self) -> None:
            played.closed += 1

    model = {"provider": "openrouter", "default": "synthetic-model"}
    monkeypatch.setattr(
        module, "configure", lambda handler: (handlers.append(handler), (SyntheticAgent, model))[1]
    )
    original_import = importlib.import_module

    def fake_import(name: str) -> object:
        if name == "dotenv":
            return SimpleNamespace(dotenv_values=lambda *args, **kwargs: {})
        if name == "hermes_cli.runtime_provider":
            return SimpleNamespace(resolve_runtime_provider=lambda **kwargs: model)
        return original_import(name)

    monkeypatch.setattr(module.importlib, "import_module", fake_import)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    )
    for name in FLAGS:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("HERMES_HOME", ".")
    monkeypatch.setattr(sys, "argv", ["hermes_arena.py", "."])
    monkeypatch.setattr(sys, "stdin", parent)
    monkeypatch.setattr(sys, "stdout", parent)
    try:
        played.code = module.main()
    except Exception as error:
        played.error = error
    played.diagnostics = [m for m in parent.messages if "diagnostic" in m]
    return played


# ---------------------------------------------------------------------------------------------
# The contract: numbers, per turn, per game
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("game", ["chess", "connect-four"])
def test_the_local_bound_leaves_the_documented_reserve_under_each_provider_deadline(
    game: str,
) -> None:
    """The decision bound is a named, versioned value; the provider's 60 seconds are not touched."""
    assert hermes_arena.TURN_BUDGET_VERSION == 1
    assert hermes_arena.PROVIDER_TURN_SECONDS[game] == PROVIDER_TURN
    assert hermes_arena.DECISION_SECONDS[game] <= LIMIT
    assert PROVIDER_TURN - hermes_arena.DECISION_SECONDS[game] >= RESERVE
    assert set(hermes_arena.DECISION_SECONDS) == set(hermes_arena.DECISIONS)


def test_the_reserve_covers_a_stale_observation_and_one_bounded_provider_phase() -> None:
    """15 seconds hold one poll interval, one provider phase's timeout and a second of slack."""
    assert hermes_arena.STATE_POLL_SECONDS == POLL
    assert hermes_arena.TURN_RESERVE_SECONDS >= RESERVE
    assert hermes_arena.TURN_RESERVE_SECONDS >= POLL + games.PROVIDER_TIMEOUT_SECONDS + 1


def test_the_state_reads_the_child_makes_are_spaced_by_the_documented_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll interval the reserve is computed from is the one the child really sleeps."""
    starts: list[float] = []

    def records(decision: Decision) -> Any:
        starts.append(decision.started)
        return move_after(1)(decision)

    played = play(monkeypatch, "first", records, turns=2, waits=3)
    assert played.error is None and played.code == 0
    # Three reads show the opponent to move; the last two and the one that shows our turn follow
    # their predecessors by one interval each.
    assert starts[1] - starts[0] >= 3 * POLL


def test_the_sdk_bounds_each_phase_of_a_provider_message() -> None:
    """`game_move` and the readback share the SDK's finite provider timeout, one per phase."""
    player = games.GamePlayer(
        agent_id=str(uuid.uuid4()),
        key_id=str(uuid.uuid4()),
        sessions=Path("."),
        api=SimpleNamespace(),  # type: ignore[arg-type]
        origins={},
    )
    try:
        assert games.PROVIDER_TIMEOUT_SECONDS == 10.0
        assert player._http.timeout == httpx.Timeout(10.0)
    finally:
        player.close()


# ---------------------------------------------------------------------------------------------
# The child: a per-turn budget, never the run's
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_a_fresh_decision_is_bounded_by_the_turn_and_below_the_provider_deadline(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The agent is given at most 45 of the 60 seconds, not the 120 the run once allowed."""
    played = play(monkeypatch, role, move_after(1))
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 1
    assert 0 < played.budgets[0] <= LIMIT
    assert played.budgets[0] <= PROVIDER_TURN - RESERVE


@pytest.mark.parametrize("role", ["white", "first"])
def test_the_run_lifetime_does_not_set_the_turn_budget(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """With 100 seconds of the run left, one turn still has 45 and not 100."""
    played = play(monkeypatch, role, move_after(1), join_seconds=3500)
    assert played.error is None and played.code == 0
    assert played.budgets[0] == pytest.approx(LIMIT)


@pytest.mark.parametrize("role", ["white", "first"])
def test_the_run_deadline_still_ends_a_decision(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    """With 30 seconds of the run left the decision gets 30, and a move at 31 is not sent."""
    played = play(monkeypatch, role, move_after(1), join_seconds=3570)
    assert played.budgets[0] == pytest.approx(30)
    late = play(monkeypatch, role, move_after(31), join_seconds=3570)
    assert late.match.forwarded == []
    assert late.code == 3
    assert late.events.count("decision_budget_expired") == 1


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize(
    ("offset", "sent"), [(-0.001, 1), (0.0, 0), (0.001, 0), (5.0, 0), (60.0, 0)]
)
def test_a_move_just_below_the_cutoff_is_sent_and_one_at_or_after_it_is_not(
    monkeypatch: pytest.MonkeyPatch, role: str, offset: float, sent: int
) -> None:
    """Below the cutoff exactly one move; at it and after it none, a fixed diagnostic and a stop."""
    played = play(monkeypatch, role, move_after(LIMIT + offset))
    assert played.error is None
    assert len(played.match.forwarded) == sent
    if sent:
        assert played.code == 0
        assert "late_move_refused" not in played.events
        assert "decision_budget_expired" not in played.events
        # The reserve is what is left for the move's own round trip.
        assert played.clock.now - 1000.0 <= LIMIT < PROVIDER_TURN
    else:
        assert played.code == 3
        assert played.events.count("late_move_refused") == 1
        assert played.events.count("decision_budget_expired") == 1
        # Nothing else is sent: no readback, no other move, no result.
        assert [m["operation"] for m in played.match.requests] == ["game_join"]


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_late_model_that_keeps_calling_tools_forwards_nothing(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """After the cutoff every tool call is refused locally, and the refusal is reported once."""

    def keeps_trying(decision: Decision) -> Any:
        decision.clock.now = decision.started + LIMIT + 3
        for _ in range(4):
            decision.call("game_move")
            decision.call("game_state", {})
        return {"failed": False}

    played = play(monkeypatch, role, keeps_trying)
    assert played.match.forwarded == []
    assert [m["operation"] for m in played.match.requests] == ["game_join"]
    assert played.events.count("late_move_refused") == 1
    assert played.events.count("decision_budget_expired") == 1
    assert played.code == 3


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_decision_that_returns_late_without_a_move_ends_the_run(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A late return starts no new decision and invents no move: the run fails closed."""
    played = play(monkeypatch, role, no_move(LIMIT + 1))
    assert played.code == 3
    assert len(played.constructed) == 1
    assert played.match.forwarded == []
    assert played.events.count("decision_budget_expired") == 1
    assert [m["operation"] for m in played.match.requests] == ["game_join"]


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_decision_without_a_move_inside_the_budget_shares_the_turn(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A second decision in the same turn gets what the first left, not a new 45 seconds."""
    played = play(monkeypatch, role, [no_move(30), move_after(1)])
    assert played.error is None and played.code == 0
    assert len(played.constructed) == 2
    assert played.budgets[0] == pytest.approx(LIMIT)
    assert played.budgets[1] == pytest.approx(LIMIT - 30)
    assert len(played.match.forwarded) == 1


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_decision_that_ends_without_a_move_never_invents_one(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """No first legal move, no random move, no old move, no resignation, no claim."""
    played = play(monkeypatch, role, no_move(), turns=1)
    assert played.code == 3
    assert played.match.forwarded == []
    assert all(m["operation"] in {"game_join", "game_state"} for m in played.match.requests)
    assert "decision_without_move" in played.events


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("waits", [0, 25])
def test_the_next_own_turn_has_a_fresh_budget_and_waiting_spends_none(
    monkeypatch: pytest.MonkeyPatch, role: str, waits: int
) -> None:
    """After a slow move the next turn starts at 45 again, quick opponent or slow."""
    played = play(monkeypatch, role, move_after(40), turns=2, waits=waits)
    assert played.error is None and played.code == 0
    assert len(played.constructed) == 2, "the model ran while the opponent was to move"
    assert played.budgets[0] == pytest.approx(LIMIT)
    assert played.budgets[1] == pytest.approx(LIMIT)
    assert len(played.match.forwarded) == 2


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_turn_that_passes_without_our_move_starts_the_next_one_fresh(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Whatever ended a turn, waiting for the opponent ends the budget it was spending."""
    played = play(monkeypatch, role, [no_move(30), move_after(1)], flip_on_read=1)
    assert played.error is None and played.code == 0
    assert len(played.constructed) == 2
    assert played.budgets[0] == pytest.approx(LIMIT)
    assert played.budgets[1] == pytest.approx(LIMIT)


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_turn_spent_by_the_readback_starts_no_further_decision(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A decision that ended inside the budget, then a slow readback: nothing is decided."""
    played = play(monkeypatch, role, no_move(44), read_seconds=2)
    assert played.code == 3
    assert len(played.constructed) == 1, "a decision was started on a spent turn"
    assert played.events.count("decision_budget_expired") == 1
    assert played.match.forwarded == []


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_second_move_in_one_decision_is_not_sent(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """One decision, one move: a repeated tool call is answered locally."""
    played = play(monkeypatch, role, move_after(1, times=3), turns=1)
    assert len(played.match.forwarded) == 1
    assert played.error is None


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_refused_move_can_still_be_corrected_inside_the_budget(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The provider refuses the first move; a corrected one inside the budget is the one move."""

    def corrects(decision: Decision) -> Any:
        first = decision.call("game_move")
        assert first == {"error": "provider.move_not_legal"}
        decision.clock.now = decision.started + 10
        decision.call("game_move")
        return {"failed": False}

    played = play(monkeypatch, role, corrects, refuse_first=True)
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2 and played.match.accepted == 1


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_model_exception_is_not_retried(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    """The exception ends the run: one agent, one fixed diagnostic, no readback, no move."""

    def raises(decision: Decision) -> Any:
        raise RuntimeError("synthetic-private-exception")

    played = play(monkeypatch, role, raises)
    assert isinstance(played.error, RuntimeError)
    assert len(played.constructed) == 1 and played.closed == 1
    assert played.events == ["model_call_started", "model_call_exception"]
    assert played.match.forwarded == []
    assert "synthetic-private" not in json.dumps(played.diagnostics)


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("offset", [-1.0, 5.0])
def test_diagnostics_stay_closed_and_carry_no_private_data(
    monkeypatch: pytest.MonkeyPatch, role: str, offset: float
) -> None:
    """Fixed names, bounded integer durations, nothing else: no prompt, output or observation."""
    played = play(monkeypatch, role, move_after(LIMIT + offset))
    assert played.diagnostics
    for message in played.diagnostics:
        assert set(message) == {"diagnostic", "duration_ms"}
        assert message["diagnostic"] in hermes_arena.DIAGNOSTICS
        assert type(message["duration_ms"]) is int
        assert 0 <= message["duration_ms"] <= 3600000
    assert "synthetic-private" not in played.parent.raw
    assert PRIVATE not in json.dumps(played.diagnostics)
    assert {"decision_budget_expired", "late_move_refused"} <= hermes_arena.DIAGNOSTICS


def test_a_decision_that_expires_stays_inside_the_diagnostic_bound() -> None:
    """At most three diagnostics per decision and one as the run ends cover the expiring one too."""
    bound = hermes_arena.diagnostic_bound(hermes_arena.DECISIONS["chess"])
    assert bound == 3 * 243 + 1
    # started, returned, late_move_refused and expired: four, in the one decision that ends the run
    # in place of the final `run_bound_reached`.
    assert bound == 3 * (243 - 1) + 4


# ---------------------------------------------------------------------------------------------
# The parent: its own clock, its own gate before a move is forwarded
# ---------------------------------------------------------------------------------------------


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
    runner._report = lambda status: None  # type: ignore[method-assign]
    return runner, owned


class Pipe:
    """A child's stdout: lines in order, and a callable item runs before the next line."""

    def __init__(self, items: list[str | Callable[[], None]]) -> None:
        """Hold the script."""
        self.items = deque(items)

    def readline(self, limit: int = -1) -> str:
        """Return the next line, or end of output."""
        while self.items:
            item = self.items.popleft()
            if callable(item):
                item()
                continue
            return item + "\n"
        return ""


class OpenPipe:
    """A child's stdout that stays open until the child is killed, as a pipe does."""

    def __init__(self) -> None:
        """Start empty and open."""
        self.lines: queue.Queue[str] = queue.Queue()

    def feed(self, line: str) -> None:
        """Let the child say something."""
        self.lines.put(line + "\n")

    def readline(self, limit: int = -1) -> str:
        """Block until the child speaks or dies."""
        return self.lines.get()

    def close(self) -> None:
        """End of output, as when the process is gone."""
        self.lines.put("")


class FakeChild:
    """A child process as `_serve` sees it, which records being killed."""

    def __init__(self, stdout: Pipe | OpenPipe) -> None:
        """Attach the script."""
        self.stdout = stdout
        self.stdin = io.StringIO()
        self.killed = 0

    def kill(self) -> None:
        """Record the kill and, like a dead process, end the output."""
        self.killed += 1
        if isinstance(self.stdout, OpenPipe):
            self.stdout.close()


def started(elapsed_ms: int = 0) -> str:
    """Return the child's diagnostic that opens a decision, with the turn time already used."""
    return json.dumps({"diagnostic": "model_call_started", "duration_ms": elapsed_ms})


RETURNED = json.dumps({"diagnostic": "model_call_returned", "duration_ms": 5})
JOIN = json.dumps({"operation": "game_join"})
MOVE = json.dumps({"operation": "game_move", "column": 3})
FINISHED = json.dumps({"finished": True})


def serve(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    items: list[str | Callable[[], None]],
    *,
    clock: Clock | None = None,
    module: ModuleType = arena_runner,
) -> tuple[Any, FakeChild, list[dict[str, Any]], list[str]]:
    """Serve one scripted child and return the runner, child, forwarded commands and log events."""
    runner, owned = supervisor(module)
    forwarded: list[dict[str, Any]] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        forwarded.append(command)
        return {"status": "active", "game_version": "connect-four-1-solo"}

    monkeypatch.setattr(module.bridge, "_run_game_command", game)
    if clock is not None:
        monkeypatch.setattr(
            module, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
        )
    child = FakeChild(Pipe(items))
    module.ArenaRunner._serve(runner, child, owned)
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    return runner, child, forwarded, events


def test_a_move_inside_an_open_decision_is_forwarded_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ordinary decision: opened by the child, one move forwarded, closed on return."""
    runner, child, forwarded, events = serve(
        monkeypatch, capsys, [JOIN, started(), MOVE, RETURNED, FINISHED], clock=Clock()
    )
    assert [command["operation"] for command in forwarded] == ["game_join", "game_move"]
    assert child.killed == 0
    assert "decision_budget_expired" not in events and "late_move_refused" not in events
    assert runner.finished.is_set()


@pytest.mark.parametrize(
    ("elapsed_ms", "delay", "forwarded_moves"),
    [
        (0, LIMIT - 0.001, 1),
        (0, LIMIT, 0),
        (0, LIMIT + 0.001, 0),
        (0, LIMIT + 30, 0),
        (30000, 15.0 - 0.001, 1),
        (30000, 15.0, 0),
        (44000, 1.0 - 0.001, 1),
        (44000, 1.0, 0),
    ],
)
def test_the_parent_forwards_no_move_at_or_after_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    elapsed_ms: int,
    delay: float,
    forwarded_moves: int,
) -> None:
    """The cutoff is the turn's 45 seconds on the parent's clock, including time already used."""
    clock = Clock()
    base = clock.now

    def at() -> None:
        clock.now = base + delay

    runner, child, forwarded, events = serve(
        monkeypatch,
        capsys,
        [JOIN, started(elapsed_ms), at, MOVE, RETURNED, FINISHED],
        clock=clock,
    )
    assert [c["operation"] for c in forwarded].count("game_move") == forwarded_moves
    if forwarded_moves:
        assert child.killed == 0 and "decision_budget_expired" not in events
        return
    assert child.killed == 1
    assert events.count("late_move_refused") == 1
    assert events.count("decision_budget_expired") == 1
    assert runner.finished.is_set() and not runner.terminal
    assert [c["operation"] for c in forwarded] == ["game_join"]


@pytest.mark.parametrize(
    "items",
    [
        [JOIN, MOVE, FINISHED],
        [JOIN, started(), RETURNED, MOVE, FINISHED],
    ],
)
def test_a_move_outside_a_decision_is_refused_and_not_forwarded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    items: list[str | Callable[[], None]],
) -> None:
    """A move needs an open decision: before one opens or after one returns, it is a violation."""
    runner, _, forwarded, events = serve(monkeypatch, capsys, items, clock=Clock())
    assert [command["operation"] for command in forwarded] == ["game_join"]
    assert events[-1] == "protocol_refused"
    assert runner.finished.is_set()


def test_a_decision_cannot_be_reopened_to_extend_its_time(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second opening while one is open is refused, so the cutoff cannot be pushed back."""
    _, _, forwarded, events = serve(
        monkeypatch, capsys, [JOIN, started(), started(), MOVE, FINISHED], clock=Clock()
    )
    assert [command["operation"] for command in forwarded] == ["game_join"]
    assert events[-1] == "protocol_refused"


def test_an_uncertain_move_is_forwarded_once_and_is_not_a_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A provider that does not answer ends the call with an error result, not a retry."""
    runner, owned = supervisor()
    calls: list[str] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        calls.append(command["operation"])
        if command["operation"] == "game_move":
            raise games.GameRefusedError(
                "games.provider_unavailable", "synthetic-private-provider-output", retryable=True
            )
        return {"status": "active", "game_version": "connect-four-1-solo"}

    monkeypatch.setattr(arena_runner.bridge, "_run_game_command", game)
    monkeypatch.setattr(arena_runner, "time", SimpleNamespace(monotonic=Clock().monotonic))
    child = FakeChild(Pipe([JOIN, started(), MOVE, RETURNED, FINISHED]))
    arena_runner.ArenaRunner._serve(runner, child, owned)
    raw = capsys.readouterr().out
    assert calls == ["game_join", "game_move"], "the runner repeated or re-sent the move"
    answers = [json.loads(line) for line in child.stdin.getvalue().splitlines()]
    assert answers[-1] == {"result": {"error": "games.provider_unavailable"}}
    assert not runner.terminal and "synthetic-private" not in raw


def test_the_parent_ends_a_blocked_child_at_the_cutoff_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A child that says it began and then goes silent is killed at the cutoff, once."""
    monkeypatch.setattr(
        hermes_arena, "DECISION_SECONDS", {"chess": 0.6, "connect-four": 0.6}, raising=False
    )
    runner, owned = supervisor()
    monkeypatch.setattr(
        arena_runner.bridge, "_run_game_command", lambda *a, **k: {"status": "active"}
    )
    pipe = OpenPipe()
    child = FakeChild(pipe)
    pipe.feed(JOIN)
    pipe.feed(started(200))
    worker = threading.Thread(
        target=arena_runner.ArenaRunner._serve, args=(runner, child, owned), daemon=True
    )
    begun = time.monotonic()
    worker.start()
    assert runner.finished.wait(timeout=10), "the blocked child was never ended"
    elapsed = time.monotonic() - begun
    worker.join(timeout=5)
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert child.killed == 1
    assert events.count("decision_budget_expired") == 1
    assert 0.3 <= elapsed < 5, "the 200 ms already used count against the 0.6 s turn"
    assert not worker.is_alive()
    assert not [t for t in threading.enumerate() if isinstance(t, threading.Timer)]


# ---------------------------------------------------------------------------------------------
# A real parent and a real child process
# ---------------------------------------------------------------------------------------------

LAUNCHER = """\
import importlib.util
import sys

arena, source, bound = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("hermes_arena", arena)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.DECISION_SECONDS = {"chess": float(bound), "connect-four": float(bound)}
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
import socket
import time
from pathlib import Path

import model_tools

BEHAVIOR = json.loads(Path(__file__).with_name("behavior.json").read_text())
get_tool_definitions = model_tools.get_tool_definitions
handle_function_call = model_tools.handle_function_call


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
            left, right = socket.socketpair()
            left.recv(1)  # a transport that never answers
        if mode == "slow":
            time.sleep(BEHAVIOR["seconds"])
        if mode in ("fast", "slow"):
            model_tools.handle_function_call("game_move", BEHAVIOR["arguments"])
        return {"failed": False}

    def close(self):
        pass
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
    module: ModuleType = arena_runner,
    arena: Path | None = None,
    wait: float = 30.0,
) -> Process:
    """Start the real adapter as a real child of a real supervisor and let it play one turn."""
    source, home = hermes_stand_in(tmp_path, behavior)
    arena_file = arena or Path(hermes_arena.__file__)
    launcher = tmp_path / "launcher.py"
    launcher.write_text(LAUNCHER, encoding="utf-8")
    root = tmp_path / "run"
    root.mkdir()
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    monkeypatch.setattr(
        hermes_arena, "DECISION_SECONDS", {"chess": bound, "connect-four": bound}, raising=False
    )
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
        if forwarded:
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
    return Process(runner, child, forwarded, commands, log, raw, reports, seconds, leftovers)


def assert_no_residue(run: Process) -> None:
    """Require the child dead and reaped, its pipes shut and no thread of ours lingering."""
    assert run.child.poll() is not None, "the child process is still running"
    assert run.child.stdin.closed and run.child.stdout.closed
    assert run.runner.worker is None and run.runner.child is None
    assert not [t for t in threading.enumerate() if isinstance(t, threading.Timer)]
    assert run.leftovers == [], "the run left files behind"


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


def load_mutant(tmp_path: Path, module: ModuleType, original: str, replacement: str) -> ModuleType:
    """Return a copy of the module's source with exactly one condition weakened."""
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert source.count(original) == 1, f"the guarded line is not unique: {original!r}"
    name = module.__name__.rsplit(".", 1)[-1] + "_mutant"
    path = tmp_path / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.replace(original, replacement), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
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


def reserve_oracle(module: ModuleType) -> None:
    """Require the named bound to leave the reserve."""
    assert module.DECISION_SECONDS["chess"] <= LIMIT, "the reserve is gone"
    assert module.DECISION_SECONDS["connect-four"] <= LIMIT, "the reserve is gone"


def lifetime_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that the run's lifetime does not set the turn's budget."""
    played = play(monkeypatch, "white", move_after(1), module=module, join_seconds=3500)
    assert played.budgets[0] == pytest.approx(LIMIT), "the run's lifetime set the turn's budget"


def late_move_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a move after the cutoff is not forwarded."""
    played = play(monkeypatch, "white", move_after(LIMIT + 5), module=module)
    assert played.match.forwarded == [], "a late move was forwarded"


def fresh_turn_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require a whole budget for the next turn."""
    played = play(monkeypatch, "first", move_after(40), module=module, turns=2)
    assert played.budgets[1] == pytest.approx(LIMIT), "the second turn inherited the old budget"


def one_move_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """One decision sends one move."""
    played = play(monkeypatch, "first", move_after(1, times=3), module=module, turns=1)
    assert len(played.match.forwarded) == 1, "a second move in one decision was sent"


def shared_turn_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a second decision of one turn gets what the first left."""
    played = play(monkeypatch, "white", [no_move(30), move_after(1)], module=module)
    assert played.budgets[1] == pytest.approx(LIMIT - 30), "a second decision got a whole turn"


def passed_turn_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a turn which passed without our move does not carry its budget on."""
    played = play(monkeypatch, "white", [no_move(30), move_after(1)], module=module, flip_on_read=1)
    assert played.budgets[1] == pytest.approx(LIMIT), "waiting did not end the turn's budget"


def no_restart_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """No decision starts once the turn's budget is spent."""
    played = play(monkeypatch, "white", no_move(44), module=module, read_seconds=2)
    assert len(played.constructed) == 1, "a decision was started after the budget was spent"


@pytest.mark.parametrize(
    ("name", "original", "replacement", "oracle"),
    [
        ("reserve", "TURN_RESERVE_SECONDS = 15", "TURN_RESERVE_SECONDS = 0", reserve_oracle),
        (
            "run-lifetime",
            "turn_started + DECISION_SECONDS[game]",
            "deadline",
            lifetime_oracle,
        ),
        ("late-move", "or time.monotonic() >= cutoff:", "or False:", late_move_oracle),
        ("fresh-turn", "turn_started = None  # accepted", "pass  # accepted", fresh_turn_oracle),
        ("one-move", "and moved:", "and False:", one_move_oracle),
        ("shared-turn", "if turn_started is None:", "if True:", shared_turn_oracle),
        ("passed-turn", "turn_started = None  # waiting", "pass  # waiting", passed_turn_oracle),
        ("no-restart", "if now >= cutoff_at:", "if False:", no_restart_oracle),
    ],
)
def test_child_guards_detect_a_weakened_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    original: str,
    replacement: str,
    oracle: Callable[..., None],
) -> None:
    """Each child condition is load-bearing: weakened, its own oracle fails."""
    mutant = load_mutant(tmp_path, hermes_arena, original, replacement)
    if oracle is reserve_oracle:
        expect_guard(oracle, hermes_arena, mutant)
        return
    expect_guard(lambda module: oracle(module, monkeypatch), hermes_arena, mutant)


def parent_gate_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that the parent forwards no move after the cutoff."""
    clock = Clock()
    base = clock.now

    def at() -> None:
        clock.now = base + LIMIT + 1

    _, _, forwarded, _ = serve(
        monkeypatch,
        capsys,
        [JOIN, started(), at, MOVE, RETURNED, FINISHED],
        clock=clock,
        module=module,
    )
    assert [c["operation"] for c in forwarded] == ["game_join"], "a late move was forwarded"


def parent_window_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that a move outside any decision is a violation, not a late move."""
    _, _, forwarded, events = serve(
        monkeypatch, capsys, [JOIN, MOVE, FINISHED], clock=Clock(), module=module
    )
    assert [c["operation"] for c in forwarded] == ["game_join"], "a move outside a decision passed"
    assert events[-1] == "protocol_refused", (
        "a move outside a decision was not told from a late one"
    )


def parent_elapsed_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that the turn time the child already used counts against the cutoff."""
    clock = Clock()
    base = clock.now

    def at() -> None:
        clock.now = base + 15.0

    _, _, forwarded, _ = serve(
        monkeypatch, capsys, [JOIN, started(30000), at, MOVE, FINISHED], clock=clock, module=module
    )
    assert [c["operation"] for c in forwarded] == ["game_join"], "the used turn time was forgotten"


@pytest.mark.parametrize(
    ("original", "replacement", "oracle"),
    [
        ("if not window.allows_move():", "if False:", parent_gate_oracle),
        ("if not window.is_open:", "if False:", parent_window_oracle),
        ('seconds - request["duration_ms"] / 1000', "seconds", parent_elapsed_oracle),
    ],
)
def test_parent_guards_detect_a_weakened_source(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    original: str,
    replacement: str,
    oracle: Callable[..., None],
) -> None:
    """The parent's gate, its window requirement and its elapsed accounting are load-bearing."""
    mutant = load_mutant(tmp_path, arena_runner, original, replacement)
    expect_guard(lambda module: oracle(module, monkeypatch, capsys), arena_runner, mutant)


def test_a_parent_that_does_not_kill_leaves_the_blocked_child_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Without the kill the blocked child outlives the cutoff: the process-level proof notices."""
    broken = load_mutant(
        tmp_path / "mutant", arena_runner, "child.kill()  # decision cutoff", "pass"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    try:
        with pytest.raises(AssertionError, match="never ended"):
            run_process(
                monkeypatch, capsys, run_dir, "white", {"mode": "block"}, module=broken, wait=6.0
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
