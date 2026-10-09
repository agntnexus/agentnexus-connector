"""#223: no intent is claimed and no model work starts unless the turn budget is guaranteed.

The decision bound is only as good as the things it stands on: a reserve that covers the poll, the
move and the read-back, a decision bound that leaves that reserve under the provider's deadline, a
cleanup that is short beside it, an adapter that keeps the very numbers the parent checked, and a
kill that really ends the runtime's whole tree. The gate asks for all of them before a seat is
claimed, in the foreground run and in the service the same way, and refuses when any one is missing.

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

from agentnexus_sdk import arena_runner, connector, hermes_arena

PROVIDER_TURN = 60
RESERVE = 15
POLL = 4
PROVIDER_PHASE = 10
CLEANUP = 3


def good_report() -> dict[str, Any]:
    """Return the budget the adapter reports, as the contract numbers say it must."""
    return {
        "version": 1,
        "turn": {"connect-four": PROVIDER_TURN, "chess": PROVIDER_TURN},
        "decision": {
            "connect-four": PROVIDER_TURN - RESERVE,
            "chess": PROVIDER_TURN - RESERVE,
        },
        "reserve": RESERVE,
        "poll": POLL,
        "cleanup": CLEANUP,
    }


# ---------------------------------------------------------------------------------------------
# What the budget stands on
# ---------------------------------------------------------------------------------------------


def test_the_shipped_budget_is_guaranteed() -> None:
    """With the admitted numbers nothing is wrong."""
    assert arena_runner.budget_problems() == []


def test_the_adapter_reports_exactly_the_budget_the_contract_names() -> None:
    """The report is the adapter's own constants, and the parent can compare it."""
    assert hermes_arena.budget_report() == good_report()
    assert arena_runner.budget_problems(good_report()) == []


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
    monkeypatch.setattr(hermes_arena, name, value)
    assert problem in arena_runner.budget_problems()


def test_an_adapter_that_keeps_another_budget_than_the_parent_checked_is_named() -> None:
    """The file the runtime will run must say what the parent checked."""
    report = good_report()
    report["decision"] = {"connect-four": 44, "chess": 44}
    assert "adapter_budget_differs" in arena_runner.budget_problems(report)
    assert "adapter_budget_differs" in arena_runner.budget_problems({})


# ---------------------------------------------------------------------------------------------
# The kill that the budget depends on, proved before it is trusted
# ---------------------------------------------------------------------------------------------


def test_the_preflight_proves_the_tree_kill_on_this_machine() -> None:
    """A process, a grandchild, one kill: no process of the tree remains."""
    assert hermes_arena.prove_tree() is True


def test_a_kill_that_leaves_a_grandchild_alive_is_not_a_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the tree kill removed the proof fails, so a gate that trusts it refuses."""
    monkeypatch.setattr(hermes_arena, "end_tree", lambda process: process.kill())
    started = time.monotonic()
    assert hermes_arena.prove_tree() is False
    assert time.monotonic() - started < 30


# ---------------------------------------------------------------------------------------------
# The preflight as a whole
# ---------------------------------------------------------------------------------------------


def runtime(tmp_path: Path) -> arena_runner.HermesRun:
    """Return a verified-looking runtime; the probe is replaced in each test."""
    return arena_runner.HermesRun(tmp_path, Path(sys.executable), tmp_path)


def answer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    report: dict[str, Any] | None = None,
    tree: bool = True,
    code: int = 0,
    timeout: bool = False,
) -> None:
    """Make the adapter's preflight answer with this report, tree proof and status."""
    document = {
        "bounded": True,
        "tools": sorted(hermes_arena.TOOLS),
        "turn_budget": good_report() if report is None else report,
        "tree": tree,
    }
    result = None if timeout else (code, json.dumps(document) + "\n")
    monkeypatch.setattr(hermes_arena, "run_in_tree", lambda *args, **kwargs: result)


def test_a_preflight_that_guarantees_the_budget_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control: the same probe with the right answer is accepted."""
    answer(monkeypatch)
    assert runtime(tmp_path).preflight() is True


@pytest.mark.parametrize("failure", ["report", "tree", "status", "timeout", "garbage", "parent"])
def test_a_preflight_that_cannot_guarantee_the_budget_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    """Any missing foundation fails the preflight closed, and none raises."""
    if failure == "report":
        other = good_report()
        other["decision"] = {"connect-four": 50, "chess": 50}
        answer(monkeypatch, report=other)
    elif failure == "tree":
        answer(monkeypatch, tree=False)
    elif failure == "status":
        answer(monkeypatch, code=3)
    elif failure == "timeout":
        answer(monkeypatch, timeout=True)
    elif failure == "garbage":
        monkeypatch.setattr(
            hermes_arena, "run_in_tree", lambda *args, **kwargs: (0, "not json at all\n")
        )
    else:
        answer(monkeypatch)
        monkeypatch.setattr(hermes_arena, "prove_tree", lambda: False)
    assert runtime(tmp_path).preflight() is False


def test_a_preflight_bounds_the_cold_start_of_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loading and configuring the runtime must finish within the stated cold-start limit."""
    seen: dict[str, Any] = {}

    def probe(*args: Any, **kwargs: Any) -> tuple[int, str]:
        seen.update(kwargs)
        return 0, json.dumps({"bounded": True, "turn_budget": good_report(), "tree": True}) + "\n"

    monkeypatch.setattr(hermes_arena, "run_in_tree", probe)
    assert runtime(tmp_path).preflight() is True
    assert seen["seconds"] == 60
    assert arena_runner.PREFLIGHT_SECONDS == 60
    assert arena_runner.PREFLIGHT_SECONDS < hermes_arena.READY_SECONDS


# ---------------------------------------------------------------------------------------------
# No claim
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
        hermes_arena, "start_in_tree", lambda *args, **kwargs: calls.append("spawn") or 1 / 0
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
    monkeypatch.setattr(hermes_arena, name, value)
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
        monkeypatch.setattr(hermes_arena, "DECISION_SECONDS", {"chess": 59, "connect-four": 45})
        with pytest.raises(mutant.RunnerRefused, match="after the claim"):
            mutant.ArenaRunner._launch(runner, owned)
        assert calls == [f"/{owned.intent_id}/claim"]
    finally:
        sys.modules.pop(mutant.__name__, None)


@pytest.mark.parametrize(
    ("original", "replacement", "break_it"),
    [
        (
            'return not budget_problems(report.get("turn_budget")) and hermes_arena.prove_tree()',
            "return hermes_arena.prove_tree()",
            "budget",
        ),
        (
            'if report.get("tree") is not True:',
            "if False:",
            "tree-report",
        ),
        (
            'return not budget_problems(report.get("turn_budget")) and hermes_arena.prove_tree()',
            'return not budget_problems(report.get("turn_budget"))',
            "tree-proof",
        ),
    ],
    ids=["budget-not-compared", "tree-report-not-required", "tree-not-proved-by-the-parent"],
)
def test_a_preflight_without_one_of_its_checks_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    original: str,
    replacement: str,
    break_it: str,
) -> None:
    """Mutation: with one check removed, the preflight accepts what the real one refuses."""
    mutant = load_mutant(tmp_path, arena_runner, original, replacement)
    try:
        if break_it == "budget":
            other = good_report()
            other["decision"] = {"connect-four": 50, "chess": 50}
            answer(monkeypatch, report=other)
        elif break_it == "tree-report":
            answer(monkeypatch, tree=False)
        else:
            answer(monkeypatch)
            monkeypatch.setattr(hermes_arena, "prove_tree", lambda: False)
        assert mutant.HermesRun(tmp_path, Path(sys.executable), tmp_path).preflight() is True
        assert runtime(tmp_path).preflight() is False
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


def test_both_paths_refuse_before_any_runner_exists_when_the_preflight_cannot_guarantee(
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
    inspected: list[str] = []

    def inspect(paths: Any) -> Any:
        inspected.append(paths.profile)
        raise arena_runner.RunnerRefused("The Arena turn budget is not guaranteed.")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a runner was built although the preflight refused")

    monkeypatch.setattr(arena_runner.HermesRun, "inspect", staticmethod(inspect))
    monkeypatch.setattr(arena_runner, "ArenaRunner", forbidden)
    messages = []
    for namespace in namespaces:
        with pytest.raises(arena_runner.RunnerRefused) as caught:
            arena_runner.command(namespace, Path("/synthetic-install"))
        messages.append(str(caught.value))
    assert inspected == ["agent2", "agent2"]
    assert messages[0] == messages[1]
