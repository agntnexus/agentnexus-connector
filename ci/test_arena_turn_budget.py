"""#223: a model decision ends inside the provider's turn, and an accepted move ends the decision.

The Chess and Connect Four providers give a seat 60 seconds per turn (`D-134` TL-4, `D-170` CH-4).
Hermes' `run_budget_seconds` only advises the model and caps an implicit stale timeout at 60 seconds
or more; it never interrupts a blocked model call, and after a tool call Hermes asks the model once
more for closing prose. So the adapter is two processes: the match process plays the game and keeps
each turn's clock, and the decision worker holds Hermes and can be killed. The bound is kept three
times, from outside the model: the match process refuses a request at the cutoff and kills a worker
that is still running at it; the parent, which holds the signing key and its own clock, forwards no
move at or after the cutoff and kills a match process that fails to; and a move the provider
accepts ends its decision at once, so no closing request and no cleanup can hold the match.

The contract numbers are written out here instead of imported, so that changing the module's
constants cannot also change what these tests demand.

Two layers live in this file, each against the same behaviour:

* the match process (`hermes_arena.main`) with a fake clock, a scripted provider and fake workers;
* the supervisor (`ArenaRunner._serve`) with scripted streams, a fake clock and a real timer.

`test_arena_decision_worker.py` runs the real worker as a real process.
"""

from __future__ import annotations

import io
import json
import queue
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest
from arena_fakes import (
    PRIVATE,
    Clock,
    Decision,
    expect_guard,
    load_mutant,
    move_after,
    no_move,
    play,
    supervisor,
)

from agentnexus_sdk import arena_runner, games, hermes_arena

PROVIDER_TURN = 60.0
RESERVE = 15.0
LIMIT = PROVIDER_TURN - RESERVE
POLL = 4.0
ROLES = ["white", "black", "first", "second"]


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


def test_the_cleanup_bound_is_short_beside_the_reserve() -> None:
    """A cleanup that never ends costs the next turn at most this: a few seconds."""
    assert 0 < hermes_arena.CLEANUP_SECONDS <= 5
    assert hermes_arena.CLEANUP_SECONDS < hermes_arena.TURN_RESERVE_SECONDS - POLL


def test_the_state_reads_the_match_makes_are_spaced_by_the_documented_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll interval the reserve is computed from is the one the match really sleeps."""
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


def test_the_decision_prompt_names_its_own_game_and_carries_the_state_once() -> None:
    """A decision is told its game's fixed instruction; the state is data inside the message."""
    state = {"observation": {"you_are": "white", "to_move": "white"}}
    chess = hermes_arena.decision_prompt("chess", "white", "first", state)
    four = hermes_arena.decision_prompt("connect-four", "second", "second", state)
    assert hermes_arena.system_prompt("chess") == hermes_arena.CHESS_PROMPT
    assert hermes_arena.system_prompt("connect-four") == hermes_arena.PROMPT
    assert "chess match" in chess and "Connect Four" not in chess
    assert "Connect Four" in four and "chess match" not in four
    for prompt in (chess, four):
        assert prompt.count("Current game data: ") == 1
        assert json.dumps(state) in prompt


# ---------------------------------------------------------------------------------------------
# The match process: a per-turn budget, never the run's, and an accepted move ends its decision
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_a_fresh_decision_is_bounded_by_the_turn_and_below_the_provider_deadline(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The decision is given at most 45 of the 60 seconds, not the 120 the run once allowed."""
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
        assert played.events == ["model_call_started", "model_call_returned"]
        # The reserve is what is left for the move's own round trip.
        assert played.clock.now - 1000.0 <= LIMIT < PROVIDER_TURN
    else:
        assert played.code == 3
        assert played.events.count("late_move_refused") == 1
        assert played.events.count("decision_budget_expired") == 1
        # Nothing else is sent: no readback, no other move, no result.
        assert played.operations == ["game_join"]
        assert all(worker.dead for worker in played.workers.spawned), "a worker was left running"


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_late_model_that_keeps_calling_tools_forwards_nothing(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The first request at the cutoff ends the decision; the worker is killed, not argued with."""

    def keeps_trying(decision: Decision) -> Any:
        decision.clock.now = decision.started + LIMIT + 3
        for _ in range(4):
            decision.call("game_move")
            decision.call("game_state", {})
        return {"failed": False}

    played = play(monkeypatch, role, keeps_trying)
    assert played.match.forwarded == []
    assert played.operations == ["game_join"]
    assert played.events.count("late_move_refused") == 1
    assert played.events.count("decision_budget_expired") == 1
    assert played.code == 3
    assert all(worker.dead for worker in played.workers.spawned)


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_decision_that_returns_late_without_a_move_ends_the_run(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A late return starts no new decision and invents no move: the run fails closed."""
    played = play(monkeypatch, role, no_move(LIMIT + 1))
    assert played.code == 3
    assert len(played.workers.commands) == 1
    assert played.match.forwarded == []
    assert played.events.count("decision_budget_expired") == 1
    assert played.operations == ["game_join"], "a readback followed a decision that was too late"


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_decision_without_a_move_inside_the_budget_shares_the_turn(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A second decision in the same turn gets what the first left, not a new 45 seconds."""
    played = play(monkeypatch, role, [no_move(30), move_after(1)])
    assert played.error is None and played.code == 0
    assert len(played.workers.commands) == 2
    assert played.budgets[0] == pytest.approx(LIMIT)
    assert played.budgets[1] == pytest.approx(LIMIT - 30)
    assert len(played.match.forwarded) == 1
    assert len(played.workers.spawned) == 1, "an ordinary decision must not start another worker"


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_decision_that_ends_without_a_move_never_invents_one(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """No first legal move, no random move, no old move, no resignation, no claim."""
    played = play(monkeypatch, role, no_move(), turns=1)
    assert played.code == 3
    assert played.match.forwarded == []
    assert set(played.operations) <= {"game_join", "game_state"}
    assert "decision_without_move" in played.events


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("waits", [0, 25])
def test_the_next_own_turn_has_a_fresh_budget_and_waiting_spends_none(
    monkeypatch: pytest.MonkeyPatch, role: str, waits: int
) -> None:
    """After a slow move the next turn starts at 45 again, quick opponent or slow."""
    played = play(monkeypatch, role, move_after(40), turns=2, waits=waits)
    assert played.error is None and played.code == 0
    assert len(played.workers.commands) == 2, "the model ran while the opponent was to move"
    assert played.budgets[0] == pytest.approx(LIMIT)
    assert played.budgets[1] == pytest.approx(LIMIT)
    assert len(played.match.forwarded) == 2
    assert len(played.workers.spawned) == 1, "the same worker plays both turns"


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_turn_that_passes_without_our_move_starts_the_next_one_fresh(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Whatever ended a turn, waiting for the opponent ends the budget it was spending."""
    played = play(monkeypatch, role, [no_move(30), move_after(1)], flip_on_read=1)
    assert played.error is None and played.code == 0
    assert len(played.workers.commands) == 2
    assert played.budgets[0] == pytest.approx(LIMIT)
    assert played.budgets[1] == pytest.approx(LIMIT)


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_turn_spent_by_the_readback_starts_no_further_decision(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A decision that ended inside the budget, then a slow readback: nothing is decided."""
    played = play(monkeypatch, role, no_move(44), read_seconds=2)
    assert played.code == 3
    assert len(played.workers.commands) == 1, "a decision was started on a spent turn"
    assert played.events.count("decision_budget_expired") == 1
    assert played.match.forwarded == []


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_state_read_that_sleeps_past_the_cutoff_is_not_sent(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The poll spacing is spent before the cutoff is checked, not after it."""

    def reads(decision: Decision) -> Any:
        decision.clock.now = decision.started + LIMIT - 2
        decision.call("game_state", {})
        decision.call("game_state", {})
        return {"failed": False}

    played = play(monkeypatch, role, reads)
    assert played.operations == ["game_join", "game_state"], "a read left after the cutoff"
    assert played.code == 3


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
    """The exception ends the run: one decision, one fixed diagnostic, no readback, no move."""

    def raises(decision: Decision) -> Any:
        raise RuntimeError("synthetic-private-exception")

    played = play(monkeypatch, role, raises)
    assert played.code == 3
    assert len(played.workers.commands) == 1
    assert played.events == ["model_call_started", "model_call_exception"]
    assert played.operations == ["game_join"]
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
    new = {
        "decision_budget_expired",
        "late_move_refused",
        "decision_cleanup_expired",
        "decision_cleanup_failed",
    }
    assert new <= hermes_arena.DIAGNOSTICS


def test_a_decision_stays_inside_the_diagnostic_bound() -> None:
    """Four per decision (started, cleanup, returned, a verdict) and one as the run ends."""
    assert hermes_arena.diagnostic_bound(hermes_arena.DECISIONS["chess"]) == 4 * 243 + 1
    assert hermes_arena.diagnostic_bound(hermes_arena.DECISIONS["connect-four"]) == 4 * 64 + 1


# ---------------------------------------------------------------------------------------------
# An accepted move ends the decision; nothing after it can hold the match
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["white", "first"])
def test_an_accepted_move_ends_the_decision_before_any_closing_request(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Hermes' closing request is never made: the model never gets the tool result back.

    Two own turns, an instant reply between them. If the move's result returned to the model it
    would go on to the closing request, which here never answers; the signal ends the decision
    first, so both moves are made and the closing request is never reached.
    """
    reached: list[int] = []

    def move_then_close(decision: Decision) -> Any:
        decision.call("game_move")
        reached.append(decision.index)  # only reached if the result came back
        decision.block()

    played = play(monkeypatch, role, move_then_close, turns=2)
    assert played.error is None and played.code == 0
    assert reached == [], "the decision went on to a closing request after its move was accepted"
    assert len(played.match.forwarded) == 2
    assert played.events == ["model_call_started", "model_call_returned"] * 2
    assert "decision_budget_expired" not in played.events
    assert len(played.workers.spawned) == 1 and played.workers.spawned[0].served == 2


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_second_move_in_one_decision_is_never_reached(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """One decision, one move: the signal unwinds the model before a repeated tool call."""
    calls: list[int] = []

    def three_moves(decision: Decision) -> Any:
        for number in range(3):
            calls.append(number)
            decision.call("game_move")
        return {"failed": False}

    played = play(monkeypatch, role, three_moves, turns=1)
    assert len(played.match.forwarded) == 1
    assert calls == [0]
    assert played.error is None


@pytest.mark.parametrize("role", ["white", "first"])
def test_the_state_machine_after_an_accepted_move_is_cleanup_then_returned_then_readback(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Observation, decision, accepted move, decision over, cleanup, readback, wait, new turn."""
    played = play(monkeypatch, role, move_after(2), turns=2, waits=2)
    assert played.error is None and played.code == 0
    assert played.operations == [
        "game_join",
        "game_move",  # decision 1: accepted, the decision is over
        "game_state",  # readback: the opponent is to move
        "game_state",  # local wait
        "game_state",  # the opponent has answered
        "game_move",  # decision 2, a new turn with a new budget
        "game_state",  # readback: the game has ended
    ]
    assert played.events == ["model_call_started", "model_call_returned"] * 2


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_hanging_cleanup_is_cut_off_at_its_bound_and_the_worker_replaced(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The match loses nothing: cleanup is ended after its bound, reported, and replaced."""
    played = play(monkeypatch, role, move_after(1), turns=2, close="hang")
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert (
        played.events
        == [
            "model_call_started",
            "decision_cleanup_expired",
            "model_call_returned",
        ]
        * 2
    ), "the window may close only after the cleanup is over"
    first, second, third = played.workers.spawned
    assert first.dead and second.dead and third.dead
    assert (first.served, second.served, third.served) == (1, 1, 0)
    # Each hang cost the match its cleanup bound and not a second more.
    assert played.clock.now - 1000.0 <= 2 * (1 + hermes_arena.CLEANUP_SECONDS) + 1


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_cleanup_that_raises_is_reported_and_the_move_is_never_repeated(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A cleanup failure is one fixed diagnostic: the accepted move stands and nothing is resent."""
    played = play(monkeypatch, role, move_after(1), turns=2, close="raise")
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert (
        played.events
        == [
            "model_call_started",
            "decision_cleanup_failed",
            "model_call_returned",
        ]
        * 2
    )
    assert played.workers.spawned[0].dead


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize(
    ("after", "expired"),
    [
        (hermes_arena.CLEANUP_SECONDS - 0.001, False),
        (float(hermes_arena.CLEANUP_SECONDS), True),
        (hermes_arena.CLEANUP_SECONDS + 0.001, True),
        (60.0, True),
    ],
)
def test_a_cleanup_just_inside_its_bound_is_accepted_and_one_at_or_after_it_is_not(
    monkeypatch: pytest.MonkeyPatch, role: str, after: float, expired: bool
) -> None:
    """Below the bound the worker is reused; at it and after it, it is cut off. Never a failure."""
    played = play(monkeypatch, role, move_after(1), turns=2, close=("after", after))
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert ("decision_cleanup_expired" in played.events) is expired
    assert len(played.workers.spawned) == (3 if expired else 1)


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize(
    ("after", "expired"), [(0.999, False), (1.0, True), (1.5, True), (30.0, True)]
)
def test_a_cleanup_is_bound_by_the_turn_when_the_decision_ends_near_its_cutoff(
    monkeypatch: pytest.MonkeyPatch, role: str, after: float, expired: bool
) -> None:
    """A move at 44 s leaves 1 s of the turn: the cleanup gets that, not the full bound."""
    played = play(monkeypatch, role, move_after(44), turns=2, close=("after", after))
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2, "the accepted move stands whatever the cleanup does"
    assert ("decision_cleanup_expired" in played.events) is expired
    assert "decision_budget_expired" not in played.events


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_request_a_finished_decision_leaves_behind_is_not_served(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """After the decision is over a worker's request is dropped: no second move goes out."""
    played = play(monkeypatch, role, move_after(1), turns=1, close="chatty")
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 1


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_worker_that_is_not_ready_ends_the_run_before_the_seat_is_joined(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Hermes must be configured first, as it always had to: nothing is joined, nothing is sent."""
    played = play(monkeypatch, role, move_after(1), ready=False)
    assert played.code == 3
    assert played.operations == []
    assert all(worker.dead for worker in played.workers.spawned)


# ---------------------------------------------------------------------------------------------
# The parent: its own clock, its own gate before a move is forwarded
# ---------------------------------------------------------------------------------------------


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
STATE = json.dumps({"operation": "game_state"})
FINISHED = json.dumps({"finished": True})


def serve(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    items: list[str | Callable[[], None]],
    *,
    clock: Clock | None = None,
    module: ModuleType = arena_runner,
    uncertain: bool = False,
) -> tuple[Any, FakeChild, list[dict[str, Any]], list[str]]:
    """Serve one scripted child and return the runner, child, forwarded commands and log events."""
    runner, owned = supervisor(module)
    forwarded: list[dict[str, Any]] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        forwarded.append(command)
        if uncertain and command["operation"] == "game_move":
            raise games.GameRefusedError("games.provider_unavailable", "lost", retryable=True)
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


def test_the_child_is_killed_even_if_the_log_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kill comes first: a log that raises or blocks must not leave a blocked child alive."""
    runner, owned = supervisor()
    child = FakeChild(Pipe([]))

    def broken(*args: object, **kwargs: object) -> None:
        raise ValueError("synthetic closed log")

    monkeypatch.setattr(arena_runner, "diagnostic", broken)
    with pytest.raises(ValueError, match="closed log"):
        runner._cut_off(child, owned, 5)
    assert child.killed == 1


class LeakyChild(FakeChild):
    """A child whose pipe still holds a request when it is killed, as a real pipe can."""

    def kill(self) -> None:
        """Leave one more request in the pipe, then die."""
        assert isinstance(self.stdout, OpenPipe)
        self.stdout.feed(STATE)
        super().kill()


def served_after_cut_off(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Cut a blocked child off with a state read left in its pipe; return what was forwarded."""
    monkeypatch.setattr(
        hermes_arena, "DECISION_SECONDS", {"chess": 0.4, "connect-four": 0.4}, raising=False
    )
    runner, owned = supervisor(module)
    calls: list[str] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        calls.append(command["operation"])
        return {"status": "active", "game_version": "connect-four-1-solo"}

    monkeypatch.setattr(module.bridge, "_run_game_command", game)
    pipe = OpenPipe()
    child = LeakyChild(pipe)
    pipe.feed(JOIN)
    pipe.feed(started())
    worker = threading.Thread(
        target=module.ArenaRunner._serve, args=(runner, child, owned), daemon=True
    )
    worker.start()
    assert runner.finished.wait(timeout=10), "the blocked child was never ended"
    worker.join(timeout=5)
    return calls


def test_requests_still_in_the_pipe_when_a_child_is_cut_off_are_not_served(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After the expiry nothing more is forwarded, whatever the dead child left in its pipe."""
    assert served_after_cut_off(arena_runner, monkeypatch) == ["game_join"]
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert events.count("decision_budget_expired") == 1


@pytest.mark.parametrize(
    ("delay", "operations"),
    [
        (LIMIT - 0.001, ["game_join", "game_move", "game_state"]),
        (LIMIT, ["game_join", "game_move"]),
        (LIMIT + 10, ["game_join", "game_move"]),
    ],
)
def test_a_state_read_after_an_uncertain_move_is_held_to_the_cutoff_like_a_move(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    delay: float,
    operations: list[str],
) -> None:
    """The SDK resolves an unresolved move by sending it again when the state is read."""
    clock = Clock()
    base = clock.now

    def at() -> None:
        clock.now = base + delay

    _, child, forwarded, events = serve(
        monkeypatch,
        capsys,
        [JOIN, started(), MOVE, RETURNED, at, STATE, FINISHED],
        clock=clock,
        uncertain=True,
    )
    assert [c["operation"] for c in forwarded] == operations
    if len(operations) == 2:
        assert child.killed == 1
        assert events.count("late_move_refused") == 1
        assert events.count("decision_budget_expired") == 1


def test_a_state_read_after_a_successful_move_is_never_held_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing is staged after an accepted move, so the readback is a plain read at any time."""
    clock = Clock()
    base = clock.now

    def at() -> None:
        clock.now = base + LIMIT + 10

    _, child, forwarded, _ = serve(
        monkeypatch, capsys, [JOIN, started(), MOVE, RETURNED, at, STATE, FINISHED], clock=clock
    )
    assert [c["operation"] for c in forwarded] == ["game_join", "game_move", "game_state"]
    assert child.killed == 0


def test_a_second_move_in_one_decision_is_refused_by_the_parent_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parent counts accepted moves itself and does not rely on the child's own guard."""
    _, _, forwarded, events = serve(
        monkeypatch, capsys, [JOIN, started(), MOVE, MOVE, RETURNED, FINISHED], clock=Clock()
    )
    assert [c["operation"] for c in forwarded] == ["game_join", "game_move"]
    assert events[-1] == "protocol_refused"


def log_exclusion_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that two threads never write the service log at the same time."""
    gate = threading.Lock()
    inside = peak = 0

    def slow_print(*args: object, **kwargs: object) -> None:
        nonlocal inside, peak
        with gate:
            inside += 1
            peak = max(peak, inside)
        time.sleep(0.05)
        with gate:
            inside -= 1

    monkeypatch.setattr(module, "print", slow_print, raising=False)
    _, owned = supervisor(module)
    threads = [
        threading.Thread(target=module.diagnostic, args=(owned, "decision_budget_expired", 1))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert peak == 1, "two threads wrote the service log at once"


def test_the_decision_timer_and_the_worker_never_interleave_a_log_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timer thread logs beside the worker thread; one record is written at a time."""
    log_exclusion_oracle(arena_runner, monkeypatch)


# ---------------------------------------------------------------------------------------------
# Mutation proofs: weaken one condition, require the guard to notice, restore
# ---------------------------------------------------------------------------------------------


def reserve_oracle(module: ModuleType) -> None:
    """Require the named bound to leave the reserve."""
    assert module.DECISION_SECONDS["chess"] <= LIMIT, "the reserve is gone"
    assert module.DECISION_SECONDS["connect-four"] <= LIMIT, "the reserve is gone"


def lifetime_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that the run's lifetime does not set the turn's budget."""
    played = play(monkeypatch, "white", move_after(1), module=module, join_seconds=3500)
    assert played.budgets[0] == pytest.approx(LIMIT), "the run's lifetime set the turn's budget"


def late_message_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a decision which only returns after the cutoff is not read back."""
    played = play(monkeypatch, "white", no_move(LIMIT + 1), module=module)
    assert played.operations == ["game_join"], "a late return was treated as a decision"


def late_request_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a read that sleeps past the cutoff is not sent."""

    def reads(decision: Decision) -> Any:
        decision.clock.now = decision.started + LIMIT - 2
        decision.call("game_state", {})
        decision.call("game_state", {})
        return {"failed": False}

    played = play(monkeypatch, "white", reads, module=module)
    assert played.operations == ["game_join", "game_state"], "a read left after the cutoff"


def fresh_turn_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that the next turn's budget is a whole one."""
    played = play(monkeypatch, "first", move_after(40), module=module, turns=2)
    assert played.budgets[1] == pytest.approx(LIMIT), "the second turn inherited the old budget"


def complete_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that an accepted move ends the decision before any closing request."""
    reached: list[int] = []

    def move_then_close(decision: Decision) -> Any:
        decision.call("game_move")
        reached.append(decision.index)  # only reached if the move's result came back
        decision.block()

    played = play(monkeypatch, "white", move_then_close, module=module, turns=2)
    for worker in played.workers.spawned:
        worker.kill()
    assert reached == [], "the model was given the move's result and went on to a closing request"
    assert len(played.match.forwarded) == 2
    assert "decision_cleanup_expired" not in played.events


def shared_turn_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a second decision of one turn gets what the first left."""
    played = play(monkeypatch, "white", [no_move(30), move_after(1)], module=module)
    assert played.budgets[1] == pytest.approx(LIMIT - 30), "a second decision got a whole turn"


def passed_turn_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a turn which passed without our move does not carry its budget on."""
    played = play(monkeypatch, "white", [no_move(30), move_after(1)], module=module, flip_on_read=1)
    assert played.budgets[1] == pytest.approx(LIMIT), "waiting did not end the turn's budget"


def no_restart_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that no decision starts once the turn's budget is spent."""
    played = play(monkeypatch, "white", no_move(44), module=module, read_seconds=2)
    assert len(played.workers.commands) == 1, "a decision was started after the budget was spent"


def cleanup_bound_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a cleanup that never ends costs the match its bound and not more."""
    played = play(monkeypatch, "white", move_after(1), module=module, turns=2, close="hang")
    spent = played.clock.now - 1000.0
    assert spent <= 2 * (1 + module.CLEANUP_SECONDS) + 1, "a hanging cleanup was waited for"


def remnant_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a worker whose cleanup was cut off is killed and not left behind."""
    played = play(monkeypatch, "white", move_after(1), module=module, turns=2, close="hang")
    try:
        assert all(worker.dead for worker in played.workers.spawned), "a worker was left behind"
    finally:
        for worker in played.workers.spawned:
            worker.kill()


def replaced_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a hanging cleanup replaces the worker and does not end the match."""
    played = play(monkeypatch, "white", move_after(1), module=module, turns=2, close="hang")
    for worker in played.workers.spawned:
        worker.kill()
    assert len(played.match.forwarded) == 2, "the match was lost to a cleanup"


def ordering_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that the decision is reported returned only after its cleanup is over."""
    played = play(monkeypatch, "white", move_after(1), module=module, turns=1, close="hang")
    for worker in played.workers.spawned:
        worker.kill()
    assert played.events == [
        "model_call_started",
        "decision_cleanup_expired",
        "model_call_returned",
    ], "the window was closed before the cleanup was over"


@pytest.mark.parametrize(
    ("original", "replacement", "oracle"),
    [
        ("TURN_RESERVE_SECONDS = 15", "TURN_RESERVE_SECONDS = 0", reserve_oracle),
        ("turn_started + DECISION_SECONDS[game]", "deadline", lifetime_oracle),
        (
            "if time.monotonic() >= cutoff_at:  # late message",
            "if False:  # late message",
            late_message_oracle,
        ),
        (
            "if time.monotonic() >= cutoff_at:  # late request",
            "if False:  # late request",
            late_request_oracle,
        ),
        ("turn_started = None  # accepted", "pass  # accepted", fresh_turn_oracle),
        (
            'worker.send({"complete": True})  # accepted',
            'worker.send({"result": result})  # accepted',
            complete_oracle,
        ),
        ("if turn_started is None:", "if True:", shared_turn_oracle),
        ("turn_started = None  # waiting", "pass  # waiting", passed_turn_oracle),
        ("if now >= cutoff_at:", "if False:", no_restart_oracle),
        (
            "min(time.monotonic() + CLEANUP_SECONDS, cutoff_at)",
            "time.monotonic() + 3600",
            cleanup_bound_oracle,
        ),
        ("worker.close()  # cleanup remnant", "pass  # cleanup remnant", remnant_oracle),
        ("worker = spawn_worker(source)  # replaced", "return 3  # replaced", replaced_oracle),
        (
            "ending = cleanup(worker,",
            'diagnostic(output, "model_call_returned", started)\n'
            "            ending = cleanup(worker,",
            ordering_oracle,
        ),
    ],
    ids=[
        "reserve",
        "run-lifetime",
        "late-message",
        "late-request",
        "fresh-turn",
        "no-completion-signal",
        "shared-turn",
        "passed-turn",
        "no-restart",
        "unbounded-cleanup",
        "cleanup-remnant",
        "whole-match-lost",
        "window-closed-before-cleanup",
    ],
)
def test_match_process_guards_detect_a_weakened_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    original: str,
    replacement: str,
    oracle: Callable[..., None],
) -> None:
    """Each condition of the match process is load-bearing: weakened, its own oracle fails."""
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


def buffered_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that nothing a cut-off child left in its pipe is served."""
    assert served_after_cut_off(module, monkeypatch) == ["game_join"], "a dead child was served"


def staged_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that a staged move is not sent again by a state read after the cutoff."""
    clock = Clock()
    base = clock.now

    def at() -> None:
        clock.now = base + LIMIT + 10

    _, _, forwarded, _ = serve(
        monkeypatch,
        capsys,
        [JOIN, started(), MOVE, RETURNED, at, STATE, FINISHED],
        clock=clock,
        module=module,
        uncertain=True,
    )
    assert [c["operation"] for c in forwarded] == ["game_join", "game_move"], "a staged move left"


def counted_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that the parent forwards one accepted move per decision."""
    _, _, forwarded, _ = serve(
        monkeypatch,
        capsys,
        [JOIN, started(), MOVE, MOVE, RETURNED, FINISHED],
        clock=Clock(),
        module=module,
    )
    assert [c["operation"] for c in forwarded] == ["game_join", "game_move"], "a second move left"


def log_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require the service log to be written by one thread at a time."""
    log_exclusion_oracle(module, monkeypatch)


@pytest.mark.parametrize(
    ("original", "replacement", "oracle"),
    [
        ("if not window.allows_move():", "if False:", parent_gate_oracle),
        ("if not window.is_open:", "if False:", parent_window_oracle),
        ('seconds - request["duration_ms"] / 1000', "seconds", parent_elapsed_oracle),
        (
            "with contextlib.suppress(OSError), _LOG:",
            "with contextlib.suppress(OSError):",
            log_oracle,
        ),
        ("if window.expired:", "if False:", buffered_oracle),
        ("not window.before_cutoff()", "False", staged_oracle),
        ("if window.moved:", "if False:", counted_oracle),
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
    """The parent's gate, its window, its elapsed accounting and its log lock are load-bearing."""
    mutant = load_mutant(tmp_path, arena_runner, original, replacement)
    expect_guard(lambda module: oracle(module, monkeypatch, capsys), arena_runner, mutant)
