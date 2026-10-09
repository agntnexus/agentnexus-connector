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

import contextlib
import json
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arena_fakes import expect_guard, load_mutant, supervisor

from agentnexus_sdk import arena_driver_hermes, arena_match, arena_runner, connector

PROVIDER_PHASE = 10


@pytest.mark.windows_security
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


# ---------------------------------------------------------------------------------------------
# Containment that cannot be proven fails closed (agntnexus/agentnexus#223, POSIX process trees)
# ---------------------------------------------------------------------------------------------

POSIX = sys.platform != "win32"
SLEEPER = "import time; time.sleep(30)"
DETACHER = """\
import socket, subprocess, sys, time
port = int(sys.argv[1])
CHILD = (
    "import socket, sys, time\\n"
    "s = socket.create_connection(('127.0.0.1', int(sys.argv[1])))\\n"
    "time.sleep(30)\\n"
)
subprocess.Popen([sys.executable, "-c", CHILD, str(port)], start_new_session=True)
s = socket.create_connection(("127.0.0.1", port))
time.sleep(30)
"""


class Listener:
    """The test's end of the sockets a process tree holds open while it lives."""

    def __init__(self) -> None:
        """Listen on loopback and accept in the background."""
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.accepted: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        with contextlib.suppress(OSError):
            while True:
                self.accepted.append(self.server.accept()[0])

    def wait_for(self, count: int) -> None:
        """Wait until this many processes have connected."""
        deadline = time.monotonic() + 30
        while len(self.accepted) < count and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(self.accepted) == count

    def survivors(self, wait: float = 5.0) -> int:
        """Count the connections that did not close within the wait."""
        alive = 0
        for connection in self.accepted:
            connection.settimeout(wait)
            try:
                if connection.recv(1) != b"":
                    alive += 1
            except TimeoutError:
                alive += 1
            except ConnectionResetError:
                pass
        return alive

    def close(self) -> None:
        """Drop everything."""
        for connection in self.accepted:
            connection.close()
        self.server.close()


GOOD_TABLE = "  1     0\n 10     1\n 11    10\n 12    11\n 20     1\n"
PARTLY_BROKEN = {
    "garbage-line": GOOD_TABLE + "GARBAGE\n",
    "garbage-between-parent-and-grandchild": "  1     0\n 10     1\n GARBAGE\n 12    11\n",
    "too-many-columns": GOOD_TABLE + " 13    12  extra\n",
    "too-few-columns": GOOD_TABLE + " 13\n",
    "negative-pid": GOOD_TABLE + " -13    12\n",
    "signed-pid": GOOD_TABLE + " +13    12\n",
    "non-integer-parent": GOOD_TABLE + " 13    1x\n",
    "float-pid": GOOD_TABLE + " 13.5    12\n",
}


@pytest.mark.skipif(
    sys.platform == "win32", reason="the process table is the POSIX way to find a tree"
)
@pytest.mark.parametrize("failure", ["missing", "timeout", "status", "empty", *PARTLY_BROKEN])
def test_a_process_table_that_cannot_be_read_raises(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """No empty answer stands in for the table, and no line is skipped: it is an error."""

    def run(*args: Any, **kwargs: Any) -> Any:
        if failure == "missing":
            raise FileNotFoundError("ps")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ps", 10)
        text = PARTLY_BROKEN.get(failure, "")
        return SimpleNamespace(returncode=1 if failure == "status" else 0, stdout=text)

    monkeypatch.setattr(arena_match.subprocess, "run", run)
    with pytest.raises(OSError, match="process table"):
        arena_match.process_table()


def test_a_table_that_skips_a_broken_line_is_noticed(tmp_path: Path) -> None:
    """Mutation: if a malformed line is skipped, the same oracle accepts a partly broken table."""
    mutant = load_mutant(
        tmp_path / "mutant",
        arena_match,
        'raise OSError("The process table could not be read.")  # malformed line',
        "continue  # malformed line",
    )
    try:

        def oracle(module: Any) -> None:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(
                    module.subprocess,
                    "run",
                    lambda *a, **k: SimpleNamespace(
                        returncode=0, stdout=PARTLY_BROKEN["garbage-between-parent-and-grandchild"]
                    ),
                )
                try:
                    module.process_table()
                except OSError:
                    return
                raise AssertionError("a table with a broken line was accepted")

        expect_guard(oracle, arena_match, mutant)
    finally:
        sys.modules.pop(mutant.__name__, None)


@pytest.mark.skipif(
    sys.platform == "win32", reason="the process table is the POSIX way to find a tree"
)
def test_the_process_table_maps_every_parent_to_its_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walk below a root finds children and grandchildren, detached or not."""
    table = "  1     0\n 10     1\n 11    10\n 12    11\n 20     1\n"
    monkeypatch.setattr(
        arena_match.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=table),
    )
    assert arena_match.descendants(10) == {11, 12}


def no_table() -> dict[int, list[int]]:
    """Stand in for a process table that cannot be read."""
    raise OSError("process table")


@pytest.mark.skipif(not POSIX, reason="session groups and the process table are POSIX")
def test_a_tree_kill_that_cannot_read_the_table_says_it_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The group is still killed, but the answer is `False`: containment was not proven."""
    process = arena_match.start_in_tree([sys.executable, "-c", SLEEPER])
    monkeypatch.setattr(arena_match, "process_table", no_table)
    assert arena_match.end_tree(process) is False
    process.wait(timeout=10)


@pytest.mark.skipif(not POSIX, reason="session groups and the process table are POSIX")
def test_a_tree_kill_with_a_readable_table_says_it_is_complete() -> None:
    """The control: the same kill with a table is complete."""
    process = arena_match.start_in_tree([sys.executable, "-c", SLEEPER])
    assert arena_match.end_tree(process) is True
    process.wait(timeout=10)


@pytest.mark.skipif(not POSIX, reason="session groups and the process table are POSIX")
def test_a_child_in_a_session_of_its_own_does_not_survive_the_tree_kill(tmp_path: Path) -> None:
    """A detached child is outside the process group; the table walk reaches it."""
    listener = Listener()
    script = tmp_path / "detacher.py"
    script.write_text(DETACHER, encoding="utf-8")
    process = arena_match.start_in_tree([sys.executable, str(script), str(listener.port)])
    try:
        listener.wait_for(2)
        assert arena_match.end_tree(process) is True
        assert listener.survivors() == 0, "a child outside the process group survived"
    finally:
        listener.close()


@pytest.mark.skipif(not POSIX, reason="session groups and the process table are POSIX")
def test_a_tree_kill_that_skips_the_table_leaves_the_detached_child(tmp_path: Path) -> None:
    """Mutation: with the descendants not read, the detached child outlives the kill."""
    mutant = load_mutant(
        tmp_path / "mutant",
        arena_match,
        "members |= descendants(self.pid)  # table",
        "pass  # table",
    )
    listener = Listener()
    script = tmp_path / "detacher.py"
    script.write_text(DETACHER.replace("sleep(30)", "sleep(12)"), encoding="utf-8")
    process = mutant.start_in_tree([sys.executable, str(script), str(listener.port)])
    try:
        listener.wait_for(2)
        mutant.end_tree(process)
        assert listener.survivors(wait=3.0) >= 1, "the weakened kill still reached the child"
    finally:
        listener.close()
        sys.modules.pop(mutant.__name__, None)


@pytest.mark.skipif(not POSIX, reason="session groups and the process table are POSIX")
def test_without_a_process_table_the_capability_proof_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine that cannot list processes cannot prove containment, so it is not trusted."""
    monkeypatch.setattr(arena_match, "process_table", no_table)
    assert arena_match.prove_tree() is False


@pytest.mark.windows_security
def test_the_capability_proof_holds_a_detached_grandchild_too() -> None:
    """The proof's tree contains a process in a session of its own; the real kill ends it."""
    assert arena_match.prove_tree() is True
    assert "start_new_session" in Path(arena_match.__file__).read_text(encoding="utf-8")


@pytest.mark.windows_security
def test_a_stop_that_could_not_prove_containment_blocks_every_later_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an incomplete kill the runner claims nothing more, whatever the budget says."""
    runner, owned, calls = launching(monkeypatch)
    runner.child = SimpleNamespace(
        poll=lambda: 0, kill=lambda: None, wait=lambda **kwargs: None, stdin=None, stdout=None
    )
    runner.worker = None
    runner.active = None
    monkeypatch.setattr(arena_match, "end_tree", lambda process: False)
    runner.stop_child()
    with pytest.raises(arena_runner.RunnerRefused, match="contain"):
        arena_runner.ArenaRunner._launch(runner, owned)
    assert calls == []


@pytest.mark.windows_security
def test_a_cutoff_that_could_not_prove_containment_blocks_every_later_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision cutoff is a kill too: an incomplete one blocks the next claim as well."""
    runner, owned, calls = launching(monkeypatch)
    monkeypatch.setattr(arena_match, "end_tree", lambda process: False)
    monkeypatch.setattr(arena_runner, "diagnostic", lambda *args, **kwargs: None)
    runner._cut_off(SimpleNamespace(), owned, 1)
    with pytest.raises(arena_runner.RunnerRefused, match="contain"):
        arena_runner.ArenaRunner._launch(runner, owned)
    assert calls == []


@pytest.mark.parametrize(
    ("original", "replacement", "kill"),
    [
        (
            "if not arena_match.end_tree(child):  # decision cutoff",
            "if arena_match.end_tree(child) and False:  # decision cutoff",
            "cutoff",
        ),
        (
            "if not arena_match.end_tree(self.child):",
            "if arena_match.end_tree(self.child) and False:",
            "stop",
        ),
        ("if not self.contained:", "if False:", "claim"),
    ],
)
def test_a_parent_that_forgets_an_incomplete_kill_is_noticed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, original: str, replacement: str, kill: str
) -> None:
    """Mutation: if an incomplete kill is not remembered, or not honoured, the claim goes on."""
    mutant = load_mutant(tmp_path, arena_runner, original, replacement)
    try:
        runner, owned = supervisor(mutant)
        calls: list[str] = []

        def post(suffix: str, payload: Any) -> Any:
            calls.append(suffix)
            raise mutant.RunnerRefused("stop after the claim")

        runner._post = post
        runner.journal = SimpleNamespace(runner_id="r", reserve=lambda identifier: True)
        monkeypatch.setattr(arena_match, "end_tree", lambda process: False)
        monkeypatch.setattr(mutant, "diagnostic", lambda *args, **kwargs: None)
        if kill == "cutoff":
            runner._cut_off(SimpleNamespace(), owned, 1)
        elif kill == "stop":
            runner.child = SimpleNamespace(
                poll=lambda: 0,
                kill=lambda: None,
                wait=lambda **kwargs: None,
                stdin=None,
                stdout=None,
            )
            runner.worker = None
            runner.active = None
            runner.stop_child()
        else:
            runner.contained = False
        with pytest.raises(mutant.RunnerRefused, match="after the claim"):
            mutant.ArenaRunner._launch(runner, owned)
        assert calls == [f"/{owned.intent_id}/claim"]
    finally:
        sys.modules.pop(mutant.__name__, None)
