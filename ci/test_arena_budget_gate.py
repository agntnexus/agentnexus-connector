"""#223: no intent is claimed and no model work starts unless the turn budget is guaranteed.

The decision bound is only as good as the things it stands on: a reserve that covers the poll, the
move and the read-back, a decision bound that leaves that reserve under the provider's deadline, a
cleanup that is short beside it, and a kill that really ends the runtime's whole tree. The gate asks
for all of them before a seat is claimed, in the foreground run and in the service the same way,
whatever the runtime, and refuses when any one is missing.

The contract numbers are written out here instead of imported, so that changing the module's
constants cannot also change what these tests demand.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arena_fakes import load_mutant, supervisor

from agentnexus_sdk import arena_driver_hermes, arena_match, arena_runner, connector

PROVIDER_PHASE = 10


def test_the_shipped_budget_is_guaranteed() -> None:
    """With the admitted numbers nothing is wrong."""
    assert arena_runner.budget_problems() == []


@pytest.mark.parametrize(
    ("name", "value", "problem"),
    [
        ("DECISION_SECONDS", {"chess": 46, "connect-four": 45}, "decision_exceeds_turn"),
        ("DECISION_SECONDS", {"chess": 45, "connect-four": 0}, "decision_exceeds_turn"),
        ("TURN_RESERVE_SECONDS", 14, "reserve_too_small"),
        ("STATE_POLL_SECONDS", 6, "reserve_too_small"),
        ("CLEANUP_SECONDS", 12, "cleanup_too_long"),
        ("CLEANUP_SECONDS", 0, "cleanup_too_long"),
        ("PROVIDER_TURN_SECONDS", {"chess": 60, "connect-four": 60, "x": 60}, "games_disagree"),
        ("TURN_BUDGET_VERSION", 2, "version"),
    ],
)
def test_a_budget_that_cannot_be_guaranteed_is_named(
    monkeypatch: pytest.MonkeyPatch, name: str, value: Any, problem: str
) -> None:
    """Each foundation, weakened alone, is found and named."""
    monkeypatch.setattr(arena_match, name, value)
    assert problem in arena_runner.budget_problems()


def test_the_process_tree_kill_is_proved_on_this_machine() -> None:
    """A process, a grandchild, one kill: no process of the tree remains."""
    assert arena_match.prove_tree() is True


def test_a_kill_that_leaves_a_grandchild_alive_is_not_a_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the tree kill removed the proof fails, so a gate that trusts it refuses."""
    monkeypatch.setattr(arena_match, "end_tree", lambda process: process.kill())
    started = time.monotonic()
    assert arena_match.prove_tree() is False
    assert time.monotonic() - started < 30


# ---------------------------------------------------------------------------------------------
# The Hermes driver's preflight: bounded cold start, closed answer
# ---------------------------------------------------------------------------------------------


def hermes_handle(tmp_path: Path) -> arena_driver_hermes.HermesRun:
    """Return a verified-looking installation; the probe is replaced in each test."""
    return arena_driver_hermes.HermesRun(tmp_path, Path(sys.executable), tmp_path, None)


def probe_answers(monkeypatch: pytest.MonkeyPatch, result: Any) -> dict[str, Any]:
    """Make the worker's preflight answer with this result, and record how it was asked."""
    seen: dict[str, Any] = {}

    def run_in_tree(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return result

    monkeypatch.setattr(arena_match, "run_in_tree", run_in_tree)
    return seen


def test_a_preflight_bounds_the_cold_start_of_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loading and configuring the runtime must finish within the stated cold-start limit."""
    document = json.dumps({"bounded": True, "tools": sorted(arena_match.TOOLS)}) + "\n"
    seen = probe_answers(monkeypatch, (0, document))
    driver = arena_driver_hermes.HermesArenaDriver()
    assert driver.preflight(hermes_handle(tmp_path)) == arena_match.TOOLS
    assert seen["seconds"] == 60
    assert arena_driver_hermes.PREFLIGHT_SECONDS == 60
    assert arena_driver_hermes.PREFLIGHT_SECONDS < arena_match.READY_SECONDS


@pytest.mark.parametrize(
    "result", [None, (3, "{}\n"), (0, "not json at all\n")], ids=["timeout", "status", "garbage"]
)
def test_a_preflight_that_cannot_finish_or_answer_refuses_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: Any
) -> None:
    """A probe that times out, fails or babbles is a refusal of the driver, nothing else."""
    probe_answers(monkeypatch, result)
    with pytest.raises(arena_driver_hermes.DriverRefused):
        arena_driver_hermes.HermesArenaDriver().preflight(hermes_handle(tmp_path))


# ---------------------------------------------------------------------------------------------
# No claim, in every entry
# ---------------------------------------------------------------------------------------------


def launching(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, list[str]]:
    """Return a supervisor whose network and process start are recorded instead of used."""
    runner, owned = supervisor()
    calls: list[str] = []

    def post(suffix: str, payload: Any) -> Any:
        calls.append(f"post{suffix}")
        raise arena_runner.RunnerRefused("stop after the claim")

    runner._post = post
    runner.journal = SimpleNamespace(runner_id="r", reserve=lambda identifier: True)
    monkeypatch.setattr(
        arena_match, "start_in_tree", lambda *args, **kwargs: calls.append("spawn") or 1 / 0
    )
    return runner, owned, calls


def test_a_guaranteed_budget_reaches_the_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: with the admitted numbers the launch goes as far as the claim."""
    runner, owned, calls = launching(monkeypatch)
    with pytest.raises(arena_runner.RunnerRefused, match="after the claim"):
        arena_runner.ArenaRunner._launch(runner, owned)
    assert calls == [f"post/{owned.intent_id}/claim"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DECISION_SECONDS", {"chess": 59, "connect-four": 45}),
        ("TURN_RESERVE_SECONDS", 5),
        ("CLEANUP_SECONDS", 20),
    ],
)
def test_no_intent_is_claimed_and_nothing_started_without_a_guaranteed_budget(
    monkeypatch: pytest.MonkeyPatch, name: str, value: Any
) -> None:
    """The refusal comes before the claim: the seat stays queued and no process is started."""
    runner, owned, calls = launching(monkeypatch)
    monkeypatch.setattr(arena_match, name, value)
    with pytest.raises(arena_runner.RunnerRefused, match="budget"):
        arena_runner.ArenaRunner._launch(runner, owned)
    assert calls == []


def test_a_launch_that_skips_the_budget_check_is_noticed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutation: with the check removed the same weakened budget reaches the claim."""
    mutant = load_mutant(tmp_path, arena_runner, "if budget_problems():", "if False:")
    try:
        runner, owned = supervisor(mutant)
        calls: list[str] = []

        def post(suffix: str, payload: Any) -> Any:
            calls.append(suffix)
            raise mutant.RunnerRefused("stop after the claim")

        runner._post = post
        runner.journal = SimpleNamespace(runner_id="r", reserve=lambda identifier: True)
        monkeypatch.setattr(arena_match, "DECISION_SECONDS", {"chess": 59, "connect-four": 45})
        with pytest.raises(mutant.RunnerRefused, match="after the claim"):
            mutant.ArenaRunner._launch(runner, owned)
        assert calls == [f"/{owned.intent_id}/claim"]
    finally:
        sys.modules.pop(mutant.__name__, None)


def refusing_inspection(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make a runtime lookup fail the test if it is reached, and record that it was."""
    reached: list[str] = []

    def driver_for(name: str) -> Any:
        reached.append(name)
        raise AssertionError("a driver was asked although the budget is not guaranteed")

    monkeypatch.setattr(arena_runner.arena_driver, "driver_for", driver_for)
    return reached


@pytest.mark.parametrize("failure", ["budget", "tree"])
def test_the_inspection_refuses_before_any_driver_is_asked(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Enable, preflight, foreground and service all pass here: nothing is asked of a runtime."""
    reached = refusing_inspection(monkeypatch)
    if failure == "budget":
        monkeypatch.setattr(arena_match, "DECISION_SECONDS", {"chess": 59, "connect-four": 45})
    else:
        monkeypatch.setattr(arena_match, "prove_tree", lambda: False)
    paths = SimpleNamespace(state_file=Path("unused"))
    with pytest.raises(arena_runner.RunnerRefused, match="turn budget"):
        arena_runner.inspected_runtime(paths, None)
    assert reached == []


@pytest.mark.parametrize("failure", ["budget", "tree"])
def test_an_inspection_without_the_budget_gate_is_noticed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    """Mutation: with the gate removed, the inspection goes on past a failing budget."""
    mutant = load_mutant(
        tmp_path,
        arena_runner,
        "    require_budget()\n    from agentnexus_sdk.connector import State\n",
        "    from agentnexus_sdk.connector import State\n",
    )
    try:
        if failure == "budget":
            monkeypatch.setattr(arena_match, "DECISION_SECONDS", {"chess": 59, "connect-four": 45})
        else:
            monkeypatch.setattr(arena_match, "prove_tree", lambda: False)
        paths = SimpleNamespace(state_file=tmp_path / "no-such-state")
        with pytest.raises(Exception) as caught:
            mutant.inspected_runtime(paths, None)
        assert "turn budget" not in str(caught.value)
    finally:
        sys.modules.pop(mutant.__name__, None)


# ---------------------------------------------------------------------------------------------
# Foreground and service are the same path
# ---------------------------------------------------------------------------------------------


def service_arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Return the command line the generated service unit runs."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(arena_runner.subprocess, "run", lambda *args, **kwargs: None)
    paths = SimpleNamespace(
        profile="agent2", install_root=SimpleNamespace(resolve=lambda: "/synthetic-install")
    )
    arena_runner._service(paths, enable=True)
    unit = tmp_path / ".config" / "systemd" / "user" / "agentnexus-arena-agent2.service"
    line = next(
        text
        for text in unit.read_text(encoding="utf-8").splitlines()
        if text.startswith("ExecStart=")
    )
    return shlex.split(line.removeprefix("ExecStart="))


def test_the_service_and_the_foreground_run_parse_to_the_same_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit runs `arena run` for the profile; typing it by hand is the same namespace."""
    argv = service_arguments(tmp_path, monkeypatch)
    assert argv[1:3] == ["-m", "agentnexus_sdk.connector"]
    parser = connector._build_parser()
    service = parser.parse_args(argv[3:])
    foreground = parser.parse_args(
        ["arena", "run", "--profile", "agent2", "--install-root", "/synthetic-install"]
    )
    assert vars(service) == vars(foreground)
    assert (service.command, service.arena_action) == ("arena", "run")


def test_both_paths_refuse_before_any_runner_exists_when_the_budget_is_not_guaranteed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The foreground run and the service run reach the same refusal, with no runner built."""
    argv = service_arguments(tmp_path, monkeypatch)
    parser = connector._build_parser()
    namespaces = [
        parser.parse_args(argv[3:]),
        parser.parse_args(
            ["arena", "run", "--profile", "agent2", "--install-root", "/synthetic-install"]
        ),
    ]
    monkeypatch.setattr(arena_match, "DECISION_SECONDS", {"chess": 59, "connect-four": 45})

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a runner was built although the budget is not guaranteed")

    monkeypatch.setattr(arena_runner, "ArenaRunner", forbidden)
    messages = []
    for namespace in namespaces:
        with pytest.raises(arena_runner.RunnerRefused) as caught:
            arena_runner.command(namespace, Path("/synthetic-install"))
        messages.append(str(caught.value))
    assert messages[0] == messages[1]
    assert "turn budget" in messages[0]
