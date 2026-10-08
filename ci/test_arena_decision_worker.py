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
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from arena_fakes import (
    FLAGS,
    MOVES,
    expect_guard,
    load_mutant,
)
from arena_process_harness import (
    RUNTIMES,
    assert_no_residue,
    assert_profile_untouched,
    hermes_homes,
    hermes_stand_in,
    lax_match_process,
    moves_of,
    mutated_programs,
    profile_changes,
    run_process,
    snapshot,
)

from agentnexus_sdk import (
    arena_driver,
    arena_driver_hermes,
    arena_match,
    arena_runner,
    hermes_arena,
)


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_a_blocked_model_transport_is_ended_at_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    runtime: str,
) -> None:
    """A model that never answers is killed at the cutoff: no move, one diagnostic, no residue."""
    run = run_process(monkeypatch, capsys, tmp_path, role, {"mode": "block"}, runtime=runtime)
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
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_a_slow_model_cannot_reach_game_move_after_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    runtime: str,
) -> None:
    """A model that finally answers with a valid move after the cutoff gets nothing forwarded."""
    behavior = {"mode": "slow", "seconds": 4.0, "arguments": MOVES[role]}
    run = run_process(monkeypatch, capsys, tmp_path, role, behavior, runtime=runtime)
    assert run.forwarded == []
    assert run.events.count("decision_budget_expired") == 1
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_a_fast_model_still_plays_exactly_one_move(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    runtime: str,
) -> None:
    """The ordinary decision through real processes: one move, a clean finish, no expiry."""
    behavior = {"mode": "fast", "arguments": MOVES[role]}
    run = run_process(monkeypatch, capsys, tmp_path, role, behavior, bound=20.0, runtime=runtime)
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


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_a_hanging_closing_request_cannot_hold_the_run_between_two_own_turns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    runtime: str,
) -> None:
    """An accepted move ends its decision: Hermes' closing request is never waited for.

    Two own turns are separated by an instant reply of the provider's computer. After each move the
    stand-in Hermes would issue the closing request and block on it for ever; both moves must still
    be made, one for each turn, and the run must end as a finished game.
    """
    behavior = {"mode": "fast", "arguments": MOVES[role], "tail": "hang"}
    run = run_process(
        monkeypatch, capsys, tmp_path, role, behavior, bound=10.0, turns=2, runtime=runtime
    )
    assert moves_of(run, role) == [MOVES[role], MOVES[role]], "one move for each of the two turns"
    assert run.events.count("model_call_started") == 2
    assert run.events.count("model_call_returned") == 2
    assert "decision_budget_expired" not in run.events and "late_move_refused" not in run.events
    assert run.runner.terminal and run.runner.playing
    assert run.seconds < 8, "the run waited for a closing request instead of the next turn"
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_a_hanging_agent_close_after_an_accepted_move_costs_the_match_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    runtime: str,
) -> None:
    """A cleanup that never ends is cut off, reported, and replaced; the match plays on."""
    behavior = {"mode": "fast", "arguments": MOVES[role], "close": "hang"}
    run = run_process(
        monkeypatch, capsys, tmp_path, role, behavior, bound=10.0, turns=2, runtime=runtime
    )
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
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_an_agent_close_that_raises_is_reported_and_the_match_plays_on(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    runtime: str,
) -> None:
    """A cleanup failure is one fixed diagnostic: no retry of the move, no text of the failure."""
    behavior = {"mode": "fast", "arguments": MOVES[role], "close": "raise"}
    run = run_process(
        monkeypatch, capsys, tmp_path, role, behavior, bound=10.0, turns=2, runtime=runtime
    )
    assert moves_of(run, role) == [MOVES[role], MOVES[role]]
    assert run.events.count("decision_cleanup_failed") == 2
    assert run.runner.terminal and run.runner.playing
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_a_move_accepted_near_the_cutoff_is_not_lost_to_a_cleanup_that_runs_past_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    runtime: str,
) -> None:
    """The cutoff is for the model; once the move is accepted a slow cleanup cannot end the run.

    The move is accepted at 2.2 s of a 2.5 s turn and the cleanup never ends. It is cut off at its
    own bound, after the turn's cutoff, and the parent's window must not have been waiting for that.
    """
    behavior = {"mode": "slow", "seconds": 2.2, "arguments": MOVES[role], "close": "hang"}
    run = run_process(
        monkeypatch,
        capsys,
        tmp_path,
        role,
        behavior,
        bound=2.5,
        cleanup=1.0,
        turns=1,
        runtime=runtime,
    )
    assert moves_of(run, role) == [MOVES[role]]
    assert "decision_budget_expired" not in run.events
    assert run.events.count("decision_cleanup_expired") == 1
    assert run.runner.terminal and run.runner.playing
    assert_no_residue(run)


@pytest.mark.parametrize("role", ["white", "first"])
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_two_fast_turns_leave_the_profile_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    role: str,
    runtime: str,
) -> None:
    """Two ordinary turns: the disposable profile has the same paths, types and hashes after."""
    behavior = {"mode": "fast", "arguments": MOVES[role]}
    run = run_process(
        monkeypatch, capsys, tmp_path, role, behavior, bound=10.0, turns=2, runtime=runtime
    )
    assert moves_of(run, role) == [MOVES[role], MOVES[role]]
    assert run.events.count("model_call_returned") == 2
    assert_no_residue(run)
    assert "synthetic-private" not in run.raw and "synthetic-disposable" not in run.raw


def preflight_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Require that enabling a profile imports Hermes without filling the profile."""
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    scratch_parent = work / "scratch-parent"
    scratch_parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_parent))
    record = work / "hermes-homes.stand-in-record"
    source, home = hermes_stand_in(work, {"mode": "fast", "record": str(record)})
    before = snapshot(home)
    handle = module.HermesRun(source, Path(sys.executable), home)
    assert module.HermesArenaDriver().preflight(handle) == arena_match.TOOLS
    assert profile_changes(before, snapshot(home)) == []
    homes = hermes_homes(record)
    assert homes, "the stand-in never filled a Hermes home, so nothing was proven"
    assert all(Path(item) != home and not Path(item).exists() for item in homes)
    assert list(scratch_parent.iterdir()) == []


def test_the_preflight_leaves_the_profile_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Enabling a profile imports Hermes too: that must not fill the profile either."""
    preflight_oracle(arena_driver_hermes, monkeypatch, tmp_path)


def homes_refusal_oracle(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Require the adapter to refuse a Hermes home that is the profile, or a missing one."""
    for name in FLAGS:
        monkeypatch.setenv(name, "1")
    monkeypatch.setattr(sys, "argv", ["hermes_arena.py", str(tmp_path), "--preflight"])
    monkeypatch.setattr(sys, "path", list(sys.path))
    for home, profile in (
        (str(tmp_path), None),
        (str(tmp_path), str(tmp_path)),
        (None, str(tmp_path / "profile")),
        ("", str(tmp_path / "profile")),
    ):
        for name, value in (("HERMES_HOME", home), ("AGENTNEXUS_ARENA_PROFILE", profile)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        try:
            code: Any = module.main()
        except Exception:
            code = "an unrefused start"
        assert code == 2, f"Hermes was started with home {home!r} and profile {profile!r}"


def test_the_adapter_refuses_to_run_hermes_in_the_profile_it_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hermes' home must be its own: the same directory, or a missing one, is refused."""
    homes_refusal_oracle(hermes_arena, monkeypatch, tmp_path)


def test_the_runtime_process_never_sees_the_signing_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The key's path and identity stay in the parent: the worker's own environment holds neither.

    The fake runtime records the environment it actually started with, so this is read off the
    real process and not off what the supervisor meant to pass.
    """
    monkeypatch.setenv("AGENTNEXUS_PRIVATE_KEY_FILE", str(tmp_path / "keys" / "agent.pem"))
    monkeypatch.setenv("AGENTNEXUS_AGENT_ID", str(uuid.uuid4()))
    monkeypatch.setenv("AGENTNEXUS_KEY_ID", "synthetic-key-id")
    record = tmp_path / "worker-environment.stand-in-record"
    behavior = {"mode": "fast", "arguments": MOVES["white"], "environment": str(record)}
    run = run_process(monkeypatch, capsys, tmp_path, "white", behavior, bound=10.0, runtime="fake")
    assert_no_residue(run)
    assert run.forwarded, "the fake runtime never played, so nothing was proven"
    seen = json.loads(record.read_text(encoding="utf-8").splitlines()[0])
    assert [name for name in seen if name.upper().startswith("AGENTNEXUS_")] == []
    assert "agent.pem" not in json.dumps(seen) and "synthetic-key-id" not in json.dumps(seen)


HELPER = """\
import os
import subprocess
import sys
import time

# A helper that inherits the worker's stdout pipe and outlives it, as a Hermes helper process could.
helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"])
open(sys.argv[1], "w").write(str(helper.pid))
time.sleep(120)
"""


def test_ending_a_worker_does_not_wait_for_a_helper_that_holds_its_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reader of a pipe that never closes must not make ending the worker take for ever."""
    script = tmp_path / "worker_with_helper.py"
    script.write_text(HELPER, encoding="utf-8")
    pid_file = tmp_path / "helper.pid"
    real = subprocess.Popen

    def popen(command: list[str], **kwargs: Any) -> Any:
        return real([sys.executable, str(script), str(pid_file)], **kwargs)

    monkeypatch.setattr(arena_match.subprocess, "Popen", popen)
    worker = arena_match.Worker(["unused"])
    try:
        deadline = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        begun = time.monotonic()
        worker.close()
        elapsed = time.monotonic() - begun
        assert elapsed < 12, f"ending the worker took {elapsed:.1f} s behind a helper's pipe"
    finally:
        if pid_file.exists():
            with contextlib.suppress(OSError):
                os.kill(int(pid_file.read_text()), getattr(signal, "SIGKILL", signal.SIGTERM))


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


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_the_parent_ends_a_match_process_that_does_not_end_its_own_decision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    runtime: str,
) -> None:
    """The second line: the parent's window kills the match process, and its worker dies with it."""
    lax_match, lax_worker = lax_match_process(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run = run_process(
        monkeypatch,
        capsys,
        run_dir,
        "white",
        {"mode": "block"},
        arena=lax_match,
        worker=lax_worker if runtime == "hermes" else None,
        runtime=runtime,
    )
    assert run.forwarded == []
    assert run.events.count("decision_budget_expired") == 1
    assert_no_residue(run)


def test_a_parent_that_does_not_kill_leaves_a_match_process_that_does_not_end_itself(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Without the parent's kill the second line is gone: the process-level proof notices."""
    lax_match, lax_worker = lax_match_process(tmp_path)
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
                arena=lax_match,
                worker=lax_worker,
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
    broken_match, broken_worker = mutated_programs(
        tmp_path / "broken",
        match=(("message = worker.get(remaining)", "message = worker.get(3600)"),),
        worker=(("exit_hard(3)", "pass"),),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run = run_process(
        monkeypatch,
        capsys,
        run_dir,
        "white",
        {"mode": "block"},
        arena=broken_match,
        worker=broken_worker,
    )
    assert run.forwarded == []
    with pytest.raises(AssertionError, match="still alive"):
        assert_no_residue(run)


# ---------------------------------------------------------------------------------------------
# Mutation proofs of the profile: weaken one condition, require the proof to notice
# ---------------------------------------------------------------------------------------------


def test_a_scratch_home_that_outlives_the_run_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Weakened so that the supervisor never removes the throwaway home, the proof sees it."""
    broken = load_mutant(
        tmp_path / "mutant", arena_runner, "remove_scratch(self.scratch)  # scratch", "pass"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    try:
        behavior = {"mode": "fast", "arguments": MOVES["white"]}
        run = run_process(
            monkeypatch, capsys, run_dir, "white", behavior, bound=10.0, turns=2, module=broken
        )
        with pytest.raises(AssertionError, match="left behind"):
            assert_profile_untouched(run)
        run.tethers.close()
    finally:
        sys.modules.pop(broken.__name__, None)


def test_a_supervisor_that_gives_hermes_the_profile_is_refused_by_the_adapter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The second line of defence: Hermes is never started in the profile, whatever calls it."""
    broken = load_mutant(
        tmp_path / "mutant",
        arena_driver_hermes,
        "HERMES_HOME=str(scratch),",
        "HERMES_HOME=str(home),",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    try:
        behavior = {"mode": "fast", "arguments": MOVES["white"]}
        run = run_process(
            monkeypatch, capsys, run_dir, "white", behavior, bound=10.0, driver_module=broken
        )
        assert run.forwarded == [] and run.homes == [], "the adapter went on into Hermes"
        assert "model_call_started" not in run.events
        assert profile_changes(run.profile_before, run.profile_after) == []
        run.tethers.close()
    finally:
        sys.modules.pop(broken.__name__, None)


def test_a_supervisor_and_an_adapter_that_both_give_hermes_the_profile_are_noticed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """With both lines of defence removed the snapshot sees what Hermes writes into the profile."""
    broken = load_mutant(
        tmp_path / "mutant",
        arena_driver_hermes,
        "HERMES_HOME=str(scratch),",
        "HERMES_HOME=str(home),",
    )
    lax_match, lax_worker = mutated_programs(
        tmp_path / "lax", worker=(("if not homes_are_apart():", "if False:"),)
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    try:
        behavior = {"mode": "fast", "arguments": MOVES["white"]}
        run = run_process(
            monkeypatch,
            capsys,
            run_dir,
            "white",
            behavior,
            bound=10.0,
            driver_module=broken,
            arena=lax_match,
            worker=lax_worker,
        )
        with pytest.raises(AssertionError, match="changed the Hermes profile"):
            assert_profile_untouched(run)
        run.tethers.close()
    finally:
        sys.modules.pop(broken.__name__, None)


def test_a_preflight_that_gives_hermes_the_profile_is_noticed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The enabling check is held to the same proof as the run."""
    broken = load_mutant(
        tmp_path / "mutant",
        arena_driver_hermes,
        "env=hermes_environment(handle.home, Path(scratch)),",
        "env=hermes_environment(handle.home, handle.home),",
    )
    try:
        # The worker's own refusal of a shared home is the second line: the check fails closed.
        with pytest.raises(arena_driver.DriverRefusedError):
            preflight_oracle(broken, monkeypatch, tmp_path)
    finally:
        sys.modules.pop(broken.__name__, None)


def test_a_adapter_that_does_not_refuse_a_shared_home_is_noticed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refusal is load-bearing: without it the start goes on into Hermes."""
    (tmp_path / "mutant").mkdir()
    shutil.copy(arena_match.__file__, tmp_path / "mutant" / "arena_match.py")
    mutant = load_mutant(
        tmp_path / "mutant", hermes_arena, "if not homes_are_apart():", "if False:"
    )
    try:
        expect_guard(
            lambda module: homes_refusal_oracle(module, monkeypatch, tmp_path), hermes_arena, mutant
        )
    finally:
        sys.modules.pop(mutant.__name__, None)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (
            'path = Path(os.environ[PROFILE_ENV]) / "config.yaml"',
            'path = Path(os.environ["HERMES_HOME"]) / "config.yaml"',
        ),
        (
            "profile = Path(os.environ[PROFILE_ENV])",
            'profile = Path(os.environ["HERMES_HOME"])',
        ),
    ],
    ids=["config-read-from-hermes-home", "credentials-read-from-hermes-home"],
)
def test_an_adapter_that_reads_the_profile_from_the_wrong_place_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    """The profile is read for its model and its credentials, and from its own path alone."""
    broken_match, broken_worker = mutated_programs(
        tmp_path / "broken", worker=((original, replacement),)
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    behavior = {"mode": "fast", "arguments": MOVES["white"]}
    run = run_process(
        monkeypatch,
        capsys,
        run_dir,
        "white",
        behavior,
        bound=10.0,
        arena=broken_match,
        worker=broken_worker,
    )
    with pytest.raises(AssertionError):
        assert_profile_untouched(run)
    run.tethers.close()
