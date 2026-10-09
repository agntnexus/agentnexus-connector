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

* the match process (`arena_match.main`) with a fake clock, a scripted provider and fake workers;
* the supervisor (`ArenaRunner._serve`) with scripted streams, a fake clock and a real timer.

`test_arena_decision_worker.py` runs the real worker as a real process.
"""

from __future__ import annotations

import io
import json
import queue
import sys
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
    Scripted,
    expect_guard,
    load_mutant,
    move_after,
    no_move,
    play,
    supervisor,
)

from agentnexus_sdk import arena_match, arena_runner, games

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
    assert arena_match.TURN_BUDGET_VERSION == 1
    assert arena_match.PROVIDER_TURN_SECONDS[game] == PROVIDER_TURN
    assert arena_match.DECISION_SECONDS[game] <= LIMIT
    assert PROVIDER_TURN - arena_match.DECISION_SECONDS[game] >= RESERVE
    assert set(arena_match.DECISION_SECONDS) == set(arena_match.DECISIONS)


def test_the_reserve_covers_a_stale_observation_and_one_bounded_provider_phase() -> None:
    """15 seconds hold one poll interval, one provider phase's timeout and a second of slack."""
    assert arena_match.STATE_POLL_SECONDS == POLL
    assert arena_match.TURN_RESERVE_SECONDS >= RESERVE
    assert arena_match.TURN_RESERVE_SECONDS >= POLL + games.PROVIDER_TIMEOUT_SECONDS + 1


def test_the_cleanup_bound_is_short_beside_the_reserve() -> None:
    """A cleanup that never ends costs the next turn at most this: a few seconds."""
    assert 0 < arena_match.CLEANUP_SECONDS <= 5
    assert arena_match.CLEANUP_SECONDS < arena_match.TURN_RESERVE_SECONDS - POLL


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
    chess = arena_match.decision_prompt("chess", "white", "first", state)
    four = arena_match.decision_prompt("connect-four", "second", "second", state)
    assert arena_match.system_prompt("chess") == arena_match.CHESS_PROMPT
    assert arena_match.system_prompt("connect-four") == arena_match.PROMPT
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
        assert message["diagnostic"] in arena_match.DIAGNOSTICS
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
    assert new <= arena_match.DIAGNOSTICS


def test_a_decision_stays_inside_the_diagnostic_bound() -> None:
    """Four per decision (started, cleanup, returned, a verdict) and one as the run ends."""
    assert arena_match.diagnostic_bound(arena_match.DECISIONS["chess"]) == 4 * 243 + 1
    assert arena_match.diagnostic_bound(arena_match.DECISIONS["connect-four"]) == 4 * 64 + 1


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
    assert played.clock.now - 1000.0 <= 2 * (1 + arena_match.CLEANUP_SECONDS) + 1


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
        (arena_match.CLEANUP_SECONDS - 0.001, False),
        (float(arena_match.CLEANUP_SECONDS), True),
        (arena_match.CLEANUP_SECONDS + 0.001, True),
        (60.0, True),
    ],
)
def test_a_cleanup_just_inside_its_bound_is_accepted_and_one_at_or_after_it_is_not(
    monkeypatch: pytest.MonkeyPatch, role: str, after: float, expired: bool
) -> None:
    """Below the bound the worker is reused; at it and after it, it is cut off. Never a failure.

    The opponent answers after the move, so that the cleanup falls into its turn and not into ours:
    what a cleanup costs the next own turn is the subject of the carry tests.
    """
    played = play(monkeypatch, role, move_after(1), turns=2, waits=1, close=("after", after))
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert ("decision_cleanup_expired" in played.events) is expired
    assert len(played.workers.spawned) == (3 if expired else 1)


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
    assert played.events == ["runtime_exception"]
    assert all(worker.dead for worker in played.workers.spawned)


# ---------------------------------------------------------------------------------------------
# What an accepted move changes: no turn cutoff after it, a staged move through a read, the order
# ---------------------------------------------------------------------------------------------


def test_the_settle_and_ready_bounds_are_named_and_sized() -> None:
    """After an accepted move the match gets time to settle; a worker gets time to start."""
    settle = arena_match.SETTLE_SECONDS
    assert settle >= arena_match.CLEANUP_SECONDS + 15, "cleanup, a kill, a reap and a respawn"
    assert arena_match.READY_SECONDS >= 120, "Hermes may need a long time to start on a slow device"


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_staged_move_delivered_by_a_read_ends_the_decision(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The provider applied the move but its answer was lost; the read that resends it lands it.

    The SDK keeps an unresolved move staged and sends it again on the next state read. That read is
    then the acceptance: the decision is over, and the model must not go on to a closing request or
    to a second move.
    """
    reached: list[int] = []

    def move_then_read(decision: Decision) -> Any:
        lost = decision.call("game_move")
        assert lost == {"error": "games.provider_unavailable", "uncertain": True}
        decision.call("game_state", {})
        reached.append(decision.index)  # only reached if the read's result came back
        decision.block()

    played = play(monkeypatch, role, move_then_read, turns=2, waits=2, lose_answer=True)
    for worker in played.workers.spawned:
        worker.kill()
    assert reached == [], "the decision went on after the staged move landed"
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert "decision_cleanup_expired" not in played.events


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_move_accepted_after_the_cutoff_by_a_slow_round_trip_still_ends_the_decision(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Admitted just before the cutoff, accepted just after: the cutoff no longer applies to it."""
    played = play(monkeypatch, role, move_after(LIMIT - 0.2), turns=2, move_seconds=0.5)
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert "decision_cleanup_expired" not in played.events
    assert "decision_budget_expired" not in played.events
    assert len(played.workers.spawned) == 1, "a healthy worker was cut off for the turn's cutoff"


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize(
    ("after", "expired"),
    [
        (0.999, False),
        (1.0, False),
        (1.5, False),
        (arena_match.CLEANUP_SECONDS - 0.001, False),
        (float(arena_match.CLEANUP_SECONDS), True),
        (arena_match.CLEANUP_SECONDS + 0.001, True),
        (60.0, True),
    ],
)
def test_after_an_accepted_move_the_cleanup_bound_is_its_own_and_not_the_turns(
    monkeypatch: pytest.MonkeyPatch, role: str, after: float, expired: bool
) -> None:
    """A move accepted at 44 s leaves the cleanup its full bound, not the turn's last second.

    The opponent answers after the move, so that the cleanup falls into its turn and not into ours.
    """
    played = play(monkeypatch, role, move_after(44), turns=2, waits=1, close=("after", after))
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2, "the accepted move stands whatever the cleanup does"
    assert ("decision_cleanup_expired" in played.events) is expired
    assert "decision_budget_expired" not in played.events


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("after", [0.999, 1.0, 1.5])
def test_without_a_move_the_cleanup_is_bound_by_what_is_left_of_the_turn(
    monkeypatch: pytest.MonkeyPatch, role: str, after: float
) -> None:
    """While no move is accepted the parent's window is the turn's: cleanup stays inside it."""
    played = play(monkeypatch, role, no_move(44), close=("after", after))
    first = played.events[: played.events.index("model_call_returned")]
    assert ("decision_cleanup_expired" in first) is (after >= 1.0)


@pytest.mark.parametrize("role", ["white", "first"])
def test_the_worker_is_killed_before_the_expiry_is_reported(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The parent ends the match process the moment it reads the report: the worker goes first."""

    def blocks(decision: Decision) -> Any:
        decision.block()

    played = play(monkeypatch, role, blocks)
    assert played.at_event["decision_budget_expired"]["dead"] == [True]
    late = play(monkeypatch, role, move_after(LIMIT + 5))
    assert late.at_event["late_move_refused"]["dead"] == [True]
    assert late.at_event["decision_budget_expired"]["dead"] == [True]


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_decision_is_reported_returned_before_its_worker_is_reaped_and_replaced(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Ending and replacing a stuck worker is not part of the decision: it follows the report."""
    played = play(monkeypatch, role, move_after(1), turns=2, close="hang")
    seen = played.at_event["model_call_returned"]
    assert seen["dead"] == [True], "the stuck worker was not killed before the report"
    assert seen["spawned"] == 1, "the replacement was started before the decision was reported"
    assert len(played.workers.spawned) == 3


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_garbled_cleanup_is_a_failed_cleanup_and_the_match_goes_on(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A worker line that is no protocol line, after an accepted move, loses the match nothing."""
    played = play(monkeypatch, role, move_after(1), turns=2, close="garbled")
    for worker in played.workers.spawned:
        worker.kill()
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert played.events.count("decision_cleanup_failed") == 2


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_garbled_decision_is_a_fixed_failure_of_the_run(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Before a move is accepted a worker that garbles its protocol ends the run, fail closed."""

    def garbles(decision: Decision) -> Any:
        decision.garble()

    played = play(monkeypatch, role, garbles)
    for worker in played.workers.spawned:
        worker.kill()
    assert played.error is None and played.code == 3
    assert played.events == ["model_call_started", "model_call_exception"]
    assert played.match.forwarded == []


# ---------------------------------------------------------------------------------------------
# The next turn's clock: the computer may answer while the cleanup is still running
# ---------------------------------------------------------------------------------------------

#: What happens between an accepted move and the next state read, at its bounds: the cleanup is cut
#: off at 3 s, the replacement worker takes about 4 s to be ready (measured), and a state read takes
#: up to 4 s. The provider's own clock for the next turn runs through all of it.
CLEANUP_BOUND = 3.0
RESTART = 4.0
AFTER_MOVE = CLEANUP_BOUND + RESTART + POLL


def solo(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    behavior: Any,
    **options: Any,
) -> Any:
    """Play two own turns against a computer that answers at once: our seat is to move again."""
    return play(
        monkeypatch,
        role,
        behavior,
        turns=2,
        waits=0,
        close=["hang", "ok"],
        spawn_seconds=RESTART,
        read_seconds=POLL,
        **options,
    )


def immediate_reply_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that cleanup, restart and poll time are charged to the turn the reply began."""
    played = solo(monkeypatch, "white", [move_after(5), move_after(1)], module=module)
    assert played.error is None and played.code == 0
    assert played.budgets[1] == pytest.approx(LIMIT - AFTER_MOVE), "the new turn got a fresh budget"


@pytest.mark.parametrize("role", ["white", "first"])
def test_an_immediate_computer_reply_charges_cleanup_restart_and_poll_to_the_new_turn(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """Accepted at t=0, cleanup 3 s, restart 4 s, poll 4 s, own turn seen at t=11: 34 s are left."""
    played = solo(monkeypatch, role, [move_after(5), move_after(1)])
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert played.budgets[0] == pytest.approx(LIMIT)
    assert played.budgets[1] == pytest.approx(LIMIT - AFTER_MOVE), "the new turn got a fresh budget"
    started = [m for m in played.diagnostics if m["diagnostic"] == "model_call_started"]
    assert [m["duration_ms"] for m in started] == [0, round(AFTER_MOVE * 1000)], (
        "the parent was not told how much of the turn the post-move work had used"
    )


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize(
    ("after", "forwarded"),
    [
        (LIMIT - AFTER_MOVE - 0.001, 2),
        (LIMIT - AFTER_MOVE, 1),
        (LIMIT - AFTER_MOVE + 1.0, 1),
        (LIMIT - AFTER_MOVE + 30.0, 1),
    ],
)
def test_a_move_after_the_providers_true_cutoff_is_not_forwarded_after_an_immediate_reply(
    monkeypatch: pytest.MonkeyPatch, role: str, after: float, forwarded: int
) -> None:
    """A model that moves 46 s after the acceptance is late whatever budget it believes it has."""
    played = solo(monkeypatch, role, [move_after(5), move_after(after)])
    assert len(played.match.forwarded) == forwarded
    if forwarded == 2:
        assert played.error is None and played.code == 0
        assert "late_move_refused" not in played.events
        return
    assert played.error is None and played.code == 3
    assert played.events.count("late_move_refused") == 1
    assert played.events.count("decision_budget_expired") == 1
    assert played.events[-1] == "decision_budget_expired"


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("waits", [1, 2, 3])
def test_an_opponent_turn_seen_first_leaves_the_next_own_turn_a_fresh_budget(
    monkeypatch: pytest.MonkeyPatch, role: str, waits: int
) -> None:
    """Replacement time that fell into the opponent's turn is not charged to the later own turn."""
    played = play(
        monkeypatch,
        role,
        [move_after(5), move_after(1)],
        turns=2,
        waits=waits,
        close=["hang", "ok"],
        spawn_seconds=RESTART,
        read_seconds=POLL,
    )
    assert played.error is None and played.code == 0
    assert len(played.match.forwarded) == 2
    assert played.budgets == [pytest.approx(LIMIT), pytest.approx(LIMIT)]
    started = [m for m in played.diagnostics if m["diagnostic"] == "model_call_started"]
    assert [m["duration_ms"] for m in started] == [0, 0]


def opponent_first_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that an opponent's turn seen first clears what the accepted move carried."""
    played = play(
        monkeypatch,
        "white",
        [move_after(5), move_after(1)],
        module=module,
        turns=2,
        waits=2,
        close=["hang", "ok"],
        spawn_seconds=RESTART,
        read_seconds=POLL,
    )
    assert played.budgets[1] == pytest.approx(LIMIT), "the carry reached a later own turn"


@pytest.mark.parametrize("role", ["white", "first"])
def test_an_ended_game_after_the_move_starts_no_decision(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """After the last move the readback says ended: no budget, no worker decision, finished."""
    played = play(
        monkeypatch,
        role,
        move_after(5),
        turns=1,
        close="hang",
        spawn_seconds=RESTART,
        read_seconds=POLL,
    )
    assert played.error is None and played.code == 0
    assert len(played.workers.commands) == 1
    assert played.events.count("model_call_started") == 1
    assert played.parent.messages[-1] == {"finished": True}


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("status", ["ended", "aborted"])
def test_a_game_the_provider_ended_or_aborted_after_the_move_starts_no_decision(
    monkeypatch: pytest.MonkeyPatch, role: str, status: str
) -> None:
    """Whatever ends the game, the readback after the move is the end: no budget, no decision."""
    own = {
        "status": "active",
        "observation": {"you_are": role, "to_move": role, "private": PRIVATE},
    }
    seat = Scripted([own, own, {"status": status}])
    played = play(
        monkeypatch,
        role,
        move_after(5),
        provider=seat,
        close="hang",
        spawn_seconds=RESTART,
        read_seconds=POLL,
    )
    assert played.error is None and played.code == 0
    assert len(played.workers.commands) == 1 and len(seat.forwarded) == 1
    assert played.parent.messages[-1] == {"finished": True}


def ended_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that an ended game, read after the move, is the end and not another turn."""
    played = play(
        monkeypatch,
        "white",
        move_after(5),
        module=module,
        turns=1,
        close="hang",
        spawn_seconds=RESTART,
        read_seconds=POLL,
    )
    assert played.error is None and played.code == 0, "an ended game was not the end"
    assert len(played.workers.commands) == 1


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_refused_readback_after_the_move_starts_no_decision_and_no_budget(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """A readback that is refused proves no own turn: the run fails closed, as it always did."""
    own = {
        "status": "active",
        "observation": {"you_are": role, "to_move": role, "private": PRIVATE},
    }
    seat = Scripted([own, own, {"error": "games.provider_unavailable"}])
    played = play(
        monkeypatch,
        role,
        move_after(5),
        provider=seat,
        close="hang",
        spawn_seconds=RESTART,
        read_seconds=POLL,
    )
    assert played.error is None and played.code == 3
    assert played.events[-1] == "game_state_refused"
    assert len(played.workers.commands) == 1
    assert len(seat.forwarded) == 1


def refused_readback_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a refused readback is never taken for the start of a turn."""
    own = {
        "status": "active",
        "observation": {"you_are": "white", "to_move": "white", "private": PRIVATE},
    }
    seat = Scripted([own, own, {"error": "games.provider_unavailable"}] + [own] * 8)
    played = play(
        monkeypatch,
        "white",
        move_after(5),
        module=module,
        provider=seat,
        close="hang",
        spawn_seconds=RESTART,
        read_seconds=POLL,
    )
    assert played.error is None and played.code == 3, "a refused readback went on"
    assert len(played.workers.commands) == 1, "a decision was started on a refused readback"


@pytest.mark.parametrize("role", ["white", "first"])
def test_cleanup_and_restart_that_already_spent_the_turn_start_no_decision(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """An immediate reply and a restart of 44 s leave nothing: no decision, one fixed report."""
    played = play(
        monkeypatch,
        role,
        move_after(5),
        turns=2,
        waits=0,
        close="hang",
        spawn_seconds=44.0,
        read_seconds=POLL,
    )
    assert played.error is None and played.code == 3
    assert len(played.workers.commands) == 1
    assert len(played.match.forwarded) == 1
    assert played.events.count("model_call_started") == 1
    assert played.events[-1] == "decision_budget_expired"


HOSTILE_PAYLOAD = {
    "accepted_at": 0,
    "turn_started": 0,
    "elapsed": 0,
    "elapsed_ms": 0,
    "deadline": 10**9,
    "duration_ms": 0,
    "seconds": 3600,
    "now": 0,
}


def payload_clock_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, role: str = "white"
) -> None:
    """Require that no provider field, model time or run time moves the instant the carry starts.

    The provider payload carries every field a hostile one could: none of them is read. The move
    takes 2 s to answer and the carry starts when the answer arrived, so the same 11 s are charged.
    """
    played = solo(
        monkeypatch,
        role,
        [move_after(5), move_after(1)],
        module=module,
        extra=HOSTILE_PAYLOAD,
        move_seconds=2.0,
    )
    assert played.error is None and played.code == 0, "a provider field was taken for a clock"
    assert played.budgets[1] == pytest.approx(LIMIT - AFTER_MOVE)


@pytest.mark.parametrize("role", ["white", "first"])
def test_the_acceptance_instant_comes_from_the_match_process_clock_alone(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """No provider field, no model time and no run time can move the carry's starting instant."""
    payload_clock_oracle(arena_match, monkeypatch, role)


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_staged_move_landed_by_a_read_starts_the_carry_at_that_reads_answer(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    """The acceptance is the answer of the read that delivered the move; the poll after counts."""

    def move_then_read(decision: Decision) -> Any:
        decision.call("game_move")
        decision.call("game_state", {})
        decision.block()

    played = play(
        monkeypatch,
        role,
        [move_then_read, move_after(1)],
        turns=2,
        waits=0,
        lose_answer=True,
        read_seconds=POLL,
    )
    for worker in played.workers.spawned:
        worker.kill()
    assert played.error is None and played.code == 0
    assert played.budgets[1] == pytest.approx(LIMIT - POLL)


def early_clear_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that the carry survives until the first readback has been looked at."""
    immediate_reply_oracle(module, monkeypatch)


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
    refuse_first_state: bool = False,
) -> tuple[Any, FakeChild, list[dict[str, Any]], list[str]]:
    """Serve one scripted child and return the runner, child, forwarded commands and log events."""
    runner, owned = supervisor(module)
    forwarded: list[dict[str, Any]] = []
    refused: list[int] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        forwarded.append(command)
        if uncertain and command["operation"] == "game_move":
            raise games.GameRefusedError("games.provider_unavailable", "lost", retryable=True)
        if refuse_first_state and command["operation"] == "game_state" and not refused:
            refused.append(1)
            raise games.GameRefusedError("provider.move_not_legal", "refused")
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
        (11000, 34.0 - 0.001, 1),
        (11000, 34.0, 0),
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
    assert answers[-1] == {"result": {"error": "games.provider_unavailable", "uncertain": True}}
    assert not runner.terminal and "synthetic-private" not in raw


def test_the_parent_ends_a_blocked_child_at_the_cutoff_exactly_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A child that says it began and then goes silent is killed at the cutoff, once."""
    monkeypatch.setattr(
        arena_match, "DECISION_SECONDS", {"chess": 0.6, "connect-four": 0.6}, raising=False
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
        arena_match, "DECISION_SECONDS", {"chess": 0.4, "connect-four": 0.4}, raising=False
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


def serve_live(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    script: list[tuple[float, str]],
    *,
    cutoff: float,
    settle: float,
    slow_move: float = 0.0,
    wait: float = 4.0,
    module: ModuleType = arena_runner,
) -> tuple[FakeChild, list[str], list[str], float]:
    """Serve a scripted child in real time, with real timers, and say what happened.

    Each line is fed after its delay. Returns the child, the operations forwarded, the log events
    and the seconds from the last line to the end of the serve.
    """
    monkeypatch.setattr(
        arena_match, "DECISION_SECONDS", {"chess": cutoff, "connect-four": cutoff}, raising=False
    )
    monkeypatch.setattr(arena_match, "SETTLE_SECONDS", settle, raising=False)
    runner, owned = supervisor(module)
    forwarded: list[str] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        forwarded.append(command["operation"])
        if command["operation"] == "game_move":
            time.sleep(slow_move)
        return {"status": "active", "game_version": "connect-four-1-solo"}

    monkeypatch.setattr(module.bridge, "_run_game_command", game)
    pipe = OpenPipe()
    child = FakeChild(pipe)
    thread = threading.Thread(
        target=module.ArenaRunner._serve, args=(runner, child, owned), daemon=True
    )
    thread.start()
    for delay, line in script:
        time.sleep(delay)
        pipe.feed(line)
    fed = time.monotonic()
    finished = runner.finished.wait(timeout=wait)
    elapsed = time.monotonic() - fed
    if not finished:
        child.kill()  # the test's own cleanup of a serve the code under test failed to end
    thread.join(timeout=5)
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    return child, forwarded, events, elapsed


def accepted_move_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that an accepted move ends the turn's cutoff for the parent's window."""
    child, forwarded, events, _ = serve_live(
        monkeypatch,
        capsys,
        [(0, JOIN), (0, started()), (0.1, MOVE), (1.0, RETURNED), (0, FINISHED)],
        cutoff=0.5,
        settle=5.0,
        module=module,
    )
    assert forwarded == ["game_join", "game_move"]
    assert child.killed == 0, "the match process was cut off for the turn's cutoff after its move"
    assert "decision_budget_expired" not in events


def test_an_accepted_move_ends_the_turns_cutoff_for_the_parents_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After the provider accepted the move the 45 seconds are over: the match may settle."""
    accepted_move_oracle(arena_runner, monkeypatch, capsys)


def in_flight_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that a move admitted before the cutoff is not cut off while it is on its way."""
    child, forwarded, events, _ = serve_live(
        monkeypatch,
        capsys,
        [(0, JOIN), (0, started()), (0.1, MOVE), (1.5, RETURNED), (0, FINISHED)],
        cutoff=0.4,
        settle=5.0,
        slow_move=0.8,
        module=module,
    )
    assert forwarded == ["game_join", "game_move"]
    assert child.killed == 0, "the match process was killed while its accepted move was in flight"
    assert "decision_budget_expired" not in events


def test_a_move_in_flight_at_the_cutoff_is_not_cut_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Admitted before the cutoff and still on its way when it comes: the timer waits."""
    in_flight_oracle(arena_runner, monkeypatch, capsys)


def settle_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that a match process which never reports back is still ended at the settle bound."""
    child, _, events, elapsed = serve_live(
        monkeypatch,
        capsys,
        [(0, JOIN), (0, started()), (0.1, MOVE)],
        cutoff=5.0,
        settle=0.4,
        wait=3.0,
        module=module,
    )
    assert child.killed == 1
    assert events.count("decision_budget_expired") == 1
    assert elapsed < 2.5, "the settle bound was not what ended it"


def test_a_match_process_that_does_not_report_back_after_an_accepted_move_is_cut_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The window is not abandoned after the move: a wedged match process is still ended."""
    settle_oracle(arena_runner, monkeypatch, capsys)


def staged_acceptance_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that the read which resends a staged move counts as the move's acceptance."""
    _, _, forwarded, events = serve(
        monkeypatch,
        capsys,
        [JOIN, started(), MOVE, STATE, MOVE, FINISHED],
        clock=Clock(),
        module=module,
        uncertain=True,
    )
    assert [c["operation"] for c in forwarded] == ["game_join", "game_move", "game_state"]
    assert events[-1] == "protocol_refused"


def test_a_staged_move_delivered_by_a_read_is_the_acceptance_for_the_parent_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The read that resends a staged move lands it: a second move of the decision is refused."""
    staged_acceptance_oracle(arena_runner, monkeypatch, capsys)


def provider_settled_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that a staged move the provider settled for good no longer holds the reads back."""
    clock = Clock()
    base = clock.now

    def at() -> None:
        clock.now = base + LIMIT + 10

    _, child, forwarded, events = serve(
        monkeypatch,
        capsys,
        [JOIN, started(), MOVE, RETURNED, STATE, at, STATE, FINISHED],
        clock=clock,
        module=module,
        uncertain=True,
        refuse_first_state=True,
    )
    assert [c["operation"] for c in forwarded] == [
        "game_join",
        "game_move",
        "game_state",
        "game_state",
    ]
    assert "late_move_refused" not in events
    assert child.killed == 0


def test_a_staged_move_settled_by_the_provider_stops_holding_back_the_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The provider refused the staged move for good: nothing is staged, a later read is a read."""
    provider_settled_oracle(arena_runner, monkeypatch, capsys)


def uncertain_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require that the match process is told which move result is not known to have landed."""
    _, child, _, _ = serve(
        monkeypatch,
        capsys,
        [JOIN, started(), MOVE, RETURNED, FINISHED],
        clock=Clock(),
        module=module,
        uncertain=True,
    )
    answers = [json.loads(line) for line in child.stdin.getvalue().splitlines()]
    assert answers[-1] == {"result": {"error": "games.provider_unavailable", "uncertain": True}}


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
    assert played.events.count("model_call_started") == 1, "a decision was reported as begun"


def cleanup_bound_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a cleanup that never ends costs the match its bound and not more."""
    played = play(monkeypatch, "white", move_after(1), module=module, turns=2, close="hang")
    spent = played.clock.now - 1000.0
    assert spent <= 2 * (1 + module.CLEANUP_SECONDS) + 1, "a hanging cleanup was waited for"


def remnant_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a worker whose cleanup was cut off is killed and not left behind."""
    played = play(monkeypatch, "white", move_after(1), module=module, turns=2, close="hang")
    try:
        assert all(worker.closed for worker in played.workers.spawned), "a worker was left behind"
    finally:
        for worker in played.workers.spawned:
            worker.kill()


def replaced_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that a hanging cleanup replaces the worker and does not end the match."""
    played = play(monkeypatch, "white", move_after(1), module=module, turns=2, close="hang")
    for worker in played.workers.spawned:
        worker.kill()
    assert len(played.match.forwarded) == 2, "the match was lost to a cleanup"


def moved_bound_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that after an accepted move the turn's cutoff no longer bounds the cleanup."""
    close = ("after", 1.5)
    played = play(monkeypatch, "white", move_after(44), module=module, turns=2, close=close)
    assert "decision_cleanup_expired" not in played.events, "the turn's cutoff bound the cleanup"


def turn_bound_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require that without a move the cleanup stays inside what is left of the turn."""
    played = play(monkeypatch, "white", no_move(44), module=module, close=("after", 1.5))
    first = played.events[: played.events.index("model_call_returned")]
    assert "decision_cleanup_expired" in first, "the cleanup ran past the turn's cutoff"


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
            "ending = cleanup(worker, bound_at)",
            "ending = cleanup(worker, time.monotonic() + 3600)",
            cleanup_bound_oracle,
        ),
        ("worker.close()  # cleanup remnant", "pass  # cleanup remnant", remnant_oracle),
        ("accepted_at = time.monotonic()  # accepted", "pass  # accepted", immediate_reply_oracle),
        (
            "turn_started = now if accepted_at is None else accepted_at  # carry",
            "turn_started = now  # carry",
            immediate_reply_oracle,
        ),
        ("accepted_at = None  # opponent", "pass  # opponent", opponent_first_oracle),
        (
            'diagnostic(output, "model_call_returned", started)',
            'diagnostic(output, "model_call_returned", started)\n            accepted_at = None',
            early_clear_oracle,
        ),
        (
            "ending = cleanup(worker, bound_at)",
            "ending = cleanup(worker, bound_at)\n            accepted_at = time.monotonic()",
            immediate_reply_oracle,
        ),
        (
            "worker = spawn_worker(command)  # replaced",
            "worker = spawn_worker(command); accepted_at = time.monotonic()  # replaced",
            immediate_reply_oracle,
        ),
        (
            'state = read_state()\n        diagnostic(output, "run_bound_reached")',
            "state = read_state()\n            accepted_at = time.monotonic()\n"
            '        diagnostic(output, "run_bound_reached")',
            immediate_reply_oracle,
        ),
        (
            "accepted_at = time.monotonic()  # accepted",
            'accepted_at = result.get("accepted_at", time.monotonic())  # accepted',
            payload_clock_oracle,
        ),
        ('if state.get("status") in {"ended", "aborted"}:', "if False:", ended_oracle),
        (
            'diagnostic(output, "game_state_refused")\n                return 3\n'
            '            if state["status"] != "active"',
            'diagnostic(output, "game_state_refused")\n                accepted_at = None\n'
            '                state = {"status": "active", '
            '"observation": {"you_are": role, "to_move": role}}\n'
            '                continue\n            if state["status"] != "active"',
            refused_readback_oracle,
        ),
        ('if outcome != "moved":', "if True:", moved_bound_oracle),
        ('if outcome != "moved":', "if False:", turn_bound_oracle),
        ("worker = spawn_worker(command)  # replaced", "return 3  # replaced", replaced_oracle),
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
        "carry-discarded",
        "carry-reset-to-now",
        "carry-kept-across-an-opponent-turn",
        "carry-cleared-too-early",
        "cleanup-time-not-counted",
        "restart-time-not-counted",
        "poll-time-not-counted",
        "carry-from-a-provider-field",
        "ended-game-starts-a-turn",
        "refused-readback-starts-a-fresh-turn",
        "moved-cleanup-bound-is-the-turns",
        "unmoved-cleanup-bound-is-not-the-turns",
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
    mutant = load_mutant(tmp_path, arena_match, original, replacement)
    if oracle is reserve_oracle:
        expect_guard(oracle, arena_match, mutant)
        return
    expect_guard(lambda module: oracle(module, monkeypatch), arena_match, mutant)


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
    _, _, forwarded, events = serve(
        monkeypatch,
        capsys,
        [JOIN, started(), MOVE, MOVE, RETURNED, FINISHED],
        clock=Clock(),
        module=module,
    )
    assert [c["operation"] for c in forwarded] == ["game_join", "game_move"], "a second move left"
    assert events[-1] == "protocol_refused", "a second move was not told from a late one"


def log_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require the service log to be written by one thread at a time."""
    log_exclusion_oracle(module, monkeypatch)


@pytest.mark.parametrize(
    ("original", "replacement", "oracle"),
    [
        ("if not window.begin_move():", "if False:", parent_gate_oracle),
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
        (
            "if accepted:\n            self.accept()",
            "if False:\n            self.accept()",
            accepted_move_oracle,
        ),
        ("if generation is not None and self._in_flight:", "if False:", in_flight_oracle),
        (
            "arena_match.SETTLE_SECONDS, self.expire, kwargs",
            "3600, self.expire, kwargs",
            settle_oracle,
        ),
        ('if operation == "game_state" and uncertain:', "if False:", staged_acceptance_oracle),
        ('elif error.code.startswith("provider."):', "elif False:", provider_settled_oracle),
        ('response["result"]["uncertain"] = True', "pass", uncertain_oracle),
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


# ---------------------------------------------------------------------------------------------
# No late move after the intent was cancelled or the run stopped (agntnexus/agentnexus#223)
# ---------------------------------------------------------------------------------------------


def stopped_run(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    stop_after_the_line_is_read: bool,
) -> list[str]:
    """Stop the runner while the match process's move is still unread, and list what was served."""
    runner, owned = supervisor(module)
    served: list[str] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        served.append(command["operation"])
        return {"status": "active", "game_version": "connect-four-1-solo"}

    monkeypatch.setattr(module.bridge, "_run_game_command", game)
    stop = lambda: setattr(runner, "stopping", True)  # noqa: E731 - what `stop_child` does first
    if stop_after_the_line_is_read:
        real = module.arena_match.bounded_request

        def bounded(operation: str, arguments: Any) -> Any:
            if operation == "game_move":
                stop()
            return real(operation, arguments)

        monkeypatch.setattr(module.arena_match, "bounded_request", bounded)
        items: list[str | Callable[[], None]] = [JOIN, started(), MOVE, RETURNED, FINISHED]
    else:
        items = [JOIN, started(), stop, MOVE, RETURNED, FINISHED]
    module.ArenaRunner._serve(runner, FakeChild(Pipe(items)), owned)
    capsys.readouterr()
    return served


@pytest.mark.parametrize("after_the_line_is_read", [False, True])
def test_a_move_still_in_the_pipe_when_the_run_is_stopped_is_never_forwarded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    after_the_line_is_read: bool,
) -> None:
    """A cancelled intent or a replaced run has no late move, whatever the child had written."""
    served = stopped_run(
        arena_runner, monkeypatch, capsys, stop_after_the_line_is_read=after_the_line_is_read
    )
    assert served == ["game_join"]


@pytest.mark.parametrize("after_the_line_is_read", [False, True])
def test_a_parent_that_serves_after_a_stop_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    after_the_line_is_read: bool,
) -> None:
    """Mutation: with the check removed, the stopped run forwards its move."""
    mutant = load_mutant(
        tmp_path, arena_runner, "if self.stopping:  # stopping before the forward", "if False:"
    )
    try:
        served = stopped_run(
            mutant, monkeypatch, capsys, stop_after_the_line_is_read=after_the_line_is_read
        )
        assert served == ["game_join", "game_move"]
    finally:
        sys.modules.pop(mutant.__name__, None)


def test_stopping_a_run_says_so_before_it_ends_the_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag is up before the child dies, so nothing it left behind is served."""
    runner = object.__new__(arena_runner.ArenaRunner)
    runner.child = SimpleNamespace(
        poll=lambda: 0, kill=lambda: None, wait=lambda **kwargs: None, stdin=None, stdout=None
    )
    runner.worker = None
    runner.active = None
    seen: list[bool] = []
    monkeypatch.setattr(
        arena_runner.arena_match, "end_tree", lambda process: seen.append(runner.stopping)
    )
    runner.stop_child()
    assert seen == [True]


def test_a_stop_cannot_begin_between_the_check_and_the_forward(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exact interleaving: a stop that starts inside the forward waits for it, then forwards none.

    The stop begins while the move is being forwarded, after the check that let it through. It
    cannot take effect until the forward is over, and the next move, in the next decision, is not
    forwarded at all. Ending the process alone would not order the two.
    """
    runner, owned = supervisor()
    runner.child = SimpleNamespace(
        poll=lambda: 0, kill=lambda: None, wait=lambda **kwargs: None, stdin=None, stdout=None
    )
    runner.worker = None
    runner.active = None
    monkeypatch.setattr(arena_runner.arena_match, "end_tree", lambda process: True)
    served: list[str] = []
    inside: list[bool] = []
    stopper: list[threading.Thread] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        served.append(command["operation"])
        if command["operation"] == "game_move":
            stopper.append(threading.Thread(target=runner.stop_child))
            stopper[0].start()
            time.sleep(0.3)
            inside.append(runner.stopping)
        return {"status": "active", "game_version": "connect-four-1-solo"}

    monkeypatch.setattr(arena_runner.bridge, "_run_game_command", game)

    def wait_for_the_stop() -> None:
        stopper[0].join(timeout=10)

    items: list[str | Callable[[], None]] = [
        JOIN,
        started(),
        MOVE,
        RETURNED,
        started(),
        wait_for_the_stop,
        MOVE,
        FINISHED,
    ]
    arena_runner.ArenaRunner._serve(runner, FakeChild(Pipe(items)), owned)
    capsys.readouterr()
    assert inside == [False], "a stop took effect while a move was being forwarded"
    assert runner.stopping is True
    assert served == ["game_join", "game_move"], served


def test_a_parent_whose_stop_does_not_wait_for_the_forward_is_noticed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Mutation: with the gate removed, the stop takes effect inside the forward."""
    mutant = load_mutant(
        tmp_path,
        arena_runner,
        "with self._gate:  # forward gate",
        "with contextlib.nullcontext():  # forward gate",
    )
    try:
        runner, owned = supervisor(mutant)
        runner.child = SimpleNamespace(
            poll=lambda: 0, kill=lambda: None, wait=lambda **kwargs: None, stdin=None, stdout=None
        )
        runner.worker = None
        runner.active = None
        monkeypatch.setattr(mutant.arena_match, "end_tree", lambda process: True)
        inside: list[bool] = []
        stopper: list[threading.Thread] = []

        def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
            if command["operation"] == "game_move":
                stopper.append(threading.Thread(target=runner.stop_child))
                stopper[0].start()
                time.sleep(0.3)
                inside.append(runner.stopping)
            return {"status": "active", "game_version": "connect-four-1-solo"}

        monkeypatch.setattr(mutant.bridge, "_run_game_command", game)
        items: list[str | Callable[[], None]] = [JOIN, started(), MOVE, RETURNED, FINISHED]
        mutant.ArenaRunner._serve(runner, FakeChild(Pipe(items)), owned)
        capsys.readouterr()
        stopper[0].join(timeout=10)
        assert inside == [True], "the weakened parent still ordered the stop after the forward"
    finally:
        sys.modules.pop(mutant.__name__, None)
