"""#228: OpenClaw takes part in the Arena through the same driver contract as any runtime.

A stand-in OpenClaw with the real process topology (launcher, child, runtime in its own session,
the MCP bridge below it) and a loopback chat model stand for the runtime and its provider. No real
profile, credential, account or model is involved.

What is held to the contract: the decision's runtime sees exactly the three operations; one run
makes at most one move; the run is ended as soon as a move is accepted, so no closing request is
made; a run that outlives its bound, or a worker that is killed, leaves no process of the tree; the
empty profile auth-store snapshot is unchanged, and per-decision session state is throwaway.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from arena_boundary import names_a_secrets_file, names_in
from arena_fakes import MOVES
from arena_openclaw_support import behavior_of, records_of, stand_in_handle, survivors
from arena_process_harness import (
    moves_of,
    mutated_programs,
    profile_changes,
    run_process,
)
from fake_chat_model import FakeChatModel

from agentnexus_sdk import (
    arena_driver,
    arena_driver_openclaw,
    arena_match,
    openclaw_arena,
    openclaw_bridge,
)

SOURCE = Path(arena_match.__file__).parent
THREE = sorted(f"arena__{name}" for name in arena_match.TOOLS)


@pytest.fixture
def model() -> Any:
    """Provide a loopback chat model that answers the first request with a move."""
    server = FakeChatModel("move")
    yield server
    server.close()


def play(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    server: FakeChatModel,
    fault: str = "",
    **options: Any,
) -> Any:
    """Run one real match of the stand-in OpenClaw against the loopback model."""
    options.setdefault("bound", 25.0)
    options.setdefault("wait", 60.0)
    return run_process(
        monkeypatch,
        capsys,
        tmp_path,
        "white",
        behavior_of(server, tmp_path, fault),
        runtime="openclaw",
        **options,
    )


def test_one_decision_makes_one_request_with_three_tools_and_one_move(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    model: FakeChatModel,
) -> None:
    """The runtime is offered exactly the three operations, moves once, and is ended at the move."""
    run = play(monkeypatch, capsys, tmp_path, model)
    assert moves_of(run, "white") == [MOVES["white"]]
    assert [request["tools"] for request in model.requests] == [THREE], model.requests
    assert run.runner.terminal and run.runner.playing
    assert run.leftovers == [], run.leftovers
    assert survivors(tmp_path) == [], "a process of the runtime tree is still running"
    assert profile_changes(run.profile_before, run.profile_after) == []
    assert not (tmp_path / "outside.stand-in-record").exists()
    run.tethers.close()


def test_the_runtime_is_ended_at_the_accepted_move_so_it_never_asks_for_closing_prose(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    model: FakeChatModel,
) -> None:
    """The closing request that the runtime always makes after a tool result does not happen."""
    run = play(monkeypatch, capsys, tmp_path, model)
    time.sleep(2.0)
    assert len(model.requests) == 1, "a closing request was made after the accepted move"
    assert moves_of(run, "white") == [MOVES["white"]]
    run.tethers.close()


def test_a_model_that_never_answers_costs_the_move_and_leaves_no_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The cutoff is the supervisor's: the worker is killed, and the runtime tree goes with it."""
    server = FakeChatModel("hang")
    try:
        run = play(monkeypatch, capsys, tmp_path, server, bound=6.0)
        assert run.forwarded == []
        assert run.events.count("decision_budget_expired") == 1
        assert survivors(tmp_path) == [], "the runtime outlived its killed worker"
        assert run.leftovers == [], run.leftovers
        assert profile_changes(run.profile_before, run.profile_after) == []
    finally:
        server.close()
        run.tethers.close()


def test_a_second_move_in_one_decision_is_never_forwarded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A runtime that keeps going after the move is ended; the match sees one move."""
    server = FakeChatModel("move,move")
    try:
        run = play(monkeypatch, capsys, tmp_path, server, fault="second_move")
        assert moves_of(run, "white") == [MOVES["white"]]
        assert survivors(tmp_path) == []
    finally:
        server.close()
        run.tethers.close()


def test_a_runtime_that_ignores_termination_and_leaks_a_child_is_killed_anyway(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    model: FakeChatModel,
) -> None:
    """The kill ladder reaches a runtime in a session of its own that ignores SIGTERM."""
    run = play(monkeypatch, capsys, tmp_path, model, fault="leak_child,ignore_sigterm")
    assert moves_of(run, "white") == [MOVES["white"]]
    assert survivors(tmp_path, wait=20.0) == [], "the ladder left a process behind"
    assert run.leftovers == [], run.leftovers
    run.tethers.close()


def test_the_runtime_gets_a_throwaway_world_and_nothing_of_the_services_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    model: FakeChatModel,
) -> None:
    """No key, token or other home; only the state path and read-only overlay reach OpenClaw."""
    monkeypatch.setenv("SYNTHETIC_PROVIDER_KEY", "synthetic-must-not-reach-the-runtime")
    monkeypatch.setenv("AGENTNEXUS_PRIVATE_KEY_FILE", str(tmp_path / "keys" / "agent.pem"))
    run = play(monkeypatch, capsys, tmp_path, model)
    seen = [r for r in records_of(tmp_path) if "environ" in r]
    assert seen, "the stand-in did not record the environment it started with"
    for record in seen:
        environment = record["environ"]
        assert "SYNTHETIC_PROVIDER_KEY" not in environment
        assert not [key for key in environment if key.startswith("AGENTNEXUS_")], environment
        assert environment["OPENCLAW_CONFIG_READONLY"] == "1"
        assert environment["OPENCLAW_NO_AUTO_UPDATE"] == "1"
        assert environment["OPENCLAW_STATE_DIR"] == str(run.profile / "state")
        for name in ("HOME", "USERPROFILE", "TMPDIR"):
            assert str(run.profile) not in environment.get(name, ""), name
    contexts = [r for r in records_of(tmp_path) if r.get("role") == "runtime"]
    assert contexts, "the stand-in did not record its runtime state boundary"
    for context in contexts:
        assert context["profile_state"] == str(run.profile / "state")
        session_state = Path(context["session_state"])
        assert session_state.is_relative_to((tmp_path / "scratch-parent").resolve())
        assert not session_state.is_relative_to(run.profile.resolve())
    run.tethers.close()


# ---------------------------------------------------------------------------------------------
# Static boundaries
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["arena_driver_openclaw.py", "openclaw_arena.py", "openclaw_bridge.py"]
)
def test_the_openclaw_driver_files_name_no_model_or_provider(name: str) -> None:
    """A runtime may know its own provider; the Connector that drives it does not."""
    assert names_in(SOURCE / name) == [], f"{name} names a model or a provider"


def test_the_openclaw_worker_reads_no_credential_file() -> None:
    """The profile's secrets are the runtime's to read, never the worker's."""
    assert not names_a_secrets_file((SOURCE / "openclaw_arena.py").read_text(encoding="utf-8"))


@pytest.mark.skipif(sys.platform == "win32", reason="on Windows a job object ends the tree")
def test_a_kill_ladder_without_its_last_rung_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    model: FakeChatModel,
) -> None:
    """Break the forceful signal: a runtime that ignores termination then outlives its decision."""
    broken_match, broken_worker = mutated_programs(
        tmp_path / "broken",
        worker=(
            (
                'FORCE = getattr(signal, "SIGKILL", signal.SIGTERM)',
                "FORCE = signal.SIGTERM",
            ),
        ),
        worker_module=openclaw_arena,
        siblings=(openclaw_bridge,),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run = play(
        monkeypatch,
        capsys,
        run_dir,
        model,
        fault="leak_child,ignore_sigterm",
        arena=broken_match,
        worker=broken_worker,
    )
    left = survivors(run_dir, wait=3.0)
    for pid in left:  # the test's own cleanup of what the broken ladder left behind
        os.kill(pid, 9)
    assert left, "a ladder without its last rung left nothing behind, so the proof proves nothing"
    run.tethers.close()


# ---------------------------------------------------------------------------------------------
# One match, one configuration file as it was (agntnexus/agentnexus#228, pinned by metadata)
# ---------------------------------------------------------------------------------------------


def config_disturbed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    fault: str,
    plan: str = "move",
    *,
    no_env: bool = False,
) -> tuple[Any, FakeChatModel]:
    """Play one match in which the stand-in runtime changes the profile's file at this moment."""
    server = FakeChatModel(plan)
    behavior = behavior_of(server, tmp_path, fault)
    behavior["no_profile_env"] = no_env
    run = run_process(
        monkeypatch,
        capsys,
        tmp_path,
        "white",
        behavior,
        runtime="openclaw",
        bound=25.0,
        wait=60.0,
    )
    return run, server


@pytest.mark.windows_security
@pytest.mark.parametrize(
    ("fault", "no_env"),
    [
        ("create_env=request", True),
        ("create_env=tool", True),
        ("remove_env=version", False),
        ("remove_env=tool", False),
        ("replace_env=tool", False),
    ],
)
def test_a_profile_env_created_removed_or_replaced_during_the_match_forwards_no_move(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    fault: str,
    no_env: bool,
) -> None:
    """The optional `.env` is pinned like the configuration, presence and absence included."""
    run, server = config_disturbed(monkeypatch, capsys, tmp_path, fault, no_env=no_env)
    try:
        assert run.forwarded == [], "a move was forwarded after the profile .env changed"
        assert "game_move_started" not in run.events
        assert survivors(tmp_path) == []
        assert not run.runner.terminal
    finally:
        server.close()
        run.tethers.close()


@pytest.mark.windows_security
@pytest.mark.parametrize(
    ("fault", "asked_the_model"),
    [
        ("touch_config=version", False),
        ("touch_config=request", True),
        ("touch_config=tool", True),
        ("replace_config=request", True),
        ("replace_config=tool", True),
    ],
)
def test_a_changed_configuration_ends_the_runtime_path_and_forwards_no_move(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    fault: str,
    asked_the_model: bool,
) -> None:
    """Before a start, during a decision, before the forward, or replaced by a same-size file."""
    run, server = config_disturbed(monkeypatch, capsys, tmp_path, fault)
    try:
        assert run.forwarded == [], "a move was forwarded after the configuration changed"
        assert "game_move_started" not in run.events
        assert bool(server.requests) is asked_the_model, server.requests
        assert survivors(tmp_path) == [], "the runtime path was not ended"
        assert not run.runner.terminal
    finally:
        server.close()
        run.tethers.close()


@pytest.mark.skipif(sys.platform == "win32", reason="a link needs a privilege on Windows")
def test_a_link_put_in_place_of_the_configuration_forwards_no_move(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The file becomes a symbolic link to a copy: the identity and the link state both say no."""
    run, server = config_disturbed(monkeypatch, capsys, tmp_path, "link_config=tool")
    try:
        assert run.forwarded == []
        assert survivors(tmp_path) == []
    finally:
        server.close()
        run.tethers.close()


@pytest.mark.windows_security
def test_a_change_after_the_first_move_stops_the_match_before_its_second(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The first decision runs on the file as it was; the second, after a change, makes no move."""
    server = FakeChatModel("move,move")
    config = tmp_path / "home" / "openclaw.json"

    def change(index: int) -> None:
        # In the model's thread, before it answers the second decision's request: the change is in
        # place before the runtime can ask to move.
        if index == 1:
            with config.open("ab") as handle:
                handle.write(b" ")

    server.before_request = change
    try:
        run = play(monkeypatch, capsys, tmp_path, server, turns=2)
        assert moves_of(run, "white") == [MOVES["white"]], "the second move was forwarded"
        assert len(server.requests) == 2
        assert survivors(tmp_path) == []
        run.tethers.close()
    finally:
        server.close()


def test_the_launch_pins_metadata_and_copies_nothing(tmp_path: Path) -> None:
    """The scratch stays empty, the runtime keeps its original file, and the pin has no content."""
    server = FakeChatModel("move")
    try:
        handle = stand_in_handle(tmp_path, server)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        launch = arena_driver_openclaw.OpenClawArenaDriver().launch(handle, scratch)
        assert list(scratch.rglob("*")) == [], "something was put in the scratch"
        assert launch.environment[openclaw_arena.CONFIG_ENV] == str(handle.config)
        assert launch.environment[openclaw_arena.PROFILE_CONFIG_ENV] == str(handle.config)
        text = handle.config.read_text(encoding="utf-8")
        assert launch.pin and text not in launch.pin
        assert launch.environment[openclaw_arena.PIN_ENV] == launch.pin
        assert all(text not in value for value in launch.environment.values())
        pinned = json.loads(launch.pin)
        assert set(pinned) == {"config", "env"}
        assert set(pinned["config"]) == {
            "path",
            "device",
            "inode",
            "size",
            "modified_ns",
            "changed_ns",
            "kind",
            "owner",
        }
    finally:
        server.close()


def test_a_launch_whose_file_state_cannot_be_pinned_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No provable identity, no match."""
    server = FakeChatModel("move")
    try:
        handle = stand_in_handle(tmp_path, server)
        monkeypatch.setattr(openclaw_arena, "config_fingerprint", lambda *args: None)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        with pytest.raises(arena_driver.DriverRefusedError):
            arena_driver_openclaw.OpenClawArenaDriver().launch(handle, scratch)
    finally:
        server.close()
