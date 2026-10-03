"""#195: fixed intents, profile containment and durable at-most-once launch on hosted CI."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentnexus_sdk import arena_runner, hermes_arena


def intent(agent_id: str) -> dict[str, object]:
    """Return synthetic fixed operation data, with no prompt or runtime credential."""
    return {
        "intent_id": str(uuid.uuid4()),
        "match_id": str(uuid.uuid4()),
        "seat": "first",
        "agent_id": agent_id,
        "expires_at": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat(),
        "status": "queued",
        "claimed_by": None,
        "run_until": (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat(),
    }


def test_fixed_intent_accepts_only_this_profile() -> None:
    """Refuse foreign identities, additional instructions and unsafe identifiers."""
    agent = str(uuid.uuid4())
    document = intent(agent)
    accepted = arena_runner.StartIntent.parse(document, agent_id=agent)
    assert accepted.match_id == document["match_id"]
    assert accepted.seat == "first"
    with pytest.raises(arena_runner.RunnerRefused):
        arena_runner.StartIntent.parse(document, agent_id=str(uuid.uuid4()))
    with pytest.raises(arena_runner.RunnerRefused):
        arena_runner.StartIntent.parse({**document, "prompt": "invoke a shell"}, agent_id=agent)
    with pytest.raises(arena_runner.RunnerRefused):
        arena_runner.StartIntent.parse(
            {**document, "match_id": "../../another-profile"}, agent_id=agent
        )


def test_two_pollers_and_restart_reserve_one_launch(tmp_path: Path) -> None:
    """Reserve an intent only once across a race and a reopened journal."""
    path = tmp_path / "journal.sqlite3"
    intent_id = str(uuid.uuid4())
    journals = [arena_runner.RunJournal(path), arena_runner.RunJournal(path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda journal: journal.reserve(intent_id), journals))
    assert sorted(results) == [False, True]
    for journal in journals:
        journal.close()
    restored = arena_runner.RunJournal(path)
    assert restored.reserve(intent_id) is False
    restored.close()


def test_hermes_environment_inherits_no_other_profiles_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Strip ambient provider credentials and task dispatch before selecting one home."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-other-profile-value")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "untrusted-task")
    monkeypatch.setenv("AGENTNEXUS_AGENT_ID", str(uuid.uuid4()))
    own = tmp_path / "own"
    own.mkdir()
    environment = arena_runner.hermes_environment(own)
    assert environment["HERMES_HOME"] == str(own)
    assert environment["HERMES_SAFE_MODE"] == "1"
    assert environment["HERMES_IGNORE_RULES"] == "1"
    assert "OPENROUTER_API_KEY" not in environment
    assert "HERMES_KANBAN_TASK" not in environment
    assert "AGENTNEXUS_AGENT_ID" not in environment


def test_expired_or_unbounded_intent_is_refused() -> None:
    """Refuse malformed clocks, unknown seats and an unbounded resource window."""
    agent = str(uuid.uuid4())
    document = intent(agent)
    for field, value in (
        ("expires_at", "not-a-time"),
        ("seat", "third"),
        ("run_until", (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()),
    ):
        with pytest.raises(arena_runner.RunnerRefused):
            arena_runner.StartIntent.parse({**document, field: value}, agent_id=agent)


def test_model_cannot_name_another_match_or_tool() -> None:
    """Refuse unknown tools, foreign match fields and a boolean masquerading as a column."""
    assert hermes_arena.bounded_request("game_move", {"column": 3}) == {
        "operation": "game_move",
        "column": 3,
    }
    for operation, arguments in (
        ("terminal", {"command": "untrusted"}),
        ("game_join", {"match_id": str(uuid.uuid4())}),
        ("game_state", {"profile": "other"}),
        ("game_move", {"column": True}),
    ):
        with pytest.raises(ValueError):
            hermes_arena.bounded_request(operation, arguments)
    definitions = [{"function": {"name": name}} for name in sorted(hermes_arena.TOOLS)]
    hermes_arena.assert_tools(definitions)
    for altered in (
        [*definitions, {"function": {"name": "terminal"}}],
        definitions[:2],
        [definitions[0]] * 3,
    ):
        with pytest.raises(ValueError):
            hermes_arena.assert_tools(altered)


def test_permanent_model_failure_stops_the_game_run_without_repeated_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected model request must refuse promptly instead of retrying until forfeiture."""
    constructed: list[object] = []
    closed: list[object] = []

    class RejectedAgent:
        def __init__(self, **kwargs: object) -> None:
            self.tools = [{"function": {"name": name}} for name in sorted(hermes_arena.TOOLS)]
            constructed.append(self)

        def run_conversation(self, prompt: str) -> dict[str, object]:
            return {
                "failed": True,
                "failure_retryable": False,
                "error": "synthetic billing refusal",
            }

        def close(self) -> None:
            closed.append(self)

    model = {"provider": "openrouter", "default": "synthetic-model"}
    monkeypatch.setattr(hermes_arena, "configure", lambda handler: (RejectedAgent, model))
    original_import = importlib.import_module

    def module(name: str) -> object:
        if name == "dotenv":
            return SimpleNamespace(dotenv_values=lambda *args, **kwargs: {})
        if name == "hermes_cli.runtime_provider":
            return SimpleNamespace(resolve_runtime_provider=lambda **kwargs: model)
        return original_import(name)

    monkeypatch.setattr(hermes_arena.importlib, "import_module", module)
    for name in ("HERMES_SAFE_MODE", "HERMES_IGNORE_RULES", "HERMES_IGNORE_USER_CONFIG"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("HERMES_HOME", ".")
    monkeypatch.setattr(sys, "argv", ["hermes_arena.py", "."])
    request = {"match_id": str(uuid.uuid4()), "seat": "first", "seconds": 3600}
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(request)
            + "\n"
            + '{"result":{"status":"active","observation":{"you_are":"first","to_move":"first"}}}\n'
            * 65
        ),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(hermes_arena.time, "sleep", lambda seconds: None)
    assert hermes_arena.main() == 3
    assert len(constructed) == 1
    assert closed == constructed
    assert output.getvalue() == '{"operation": "game_join"}\n'


@pytest.mark.parametrize("role", ["first", "second"])
def test_hermes_waits_locally_for_its_turn_instead_of_spending_inference_on_waiting(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    """The supervisor owns waiting and calls the model only for this seat's decision."""
    decisions: list[dict[str, object]] = []

    class OneTurnAgent:
        def __init__(self, **kwargs: object) -> None:
            self.tools = [{"function": {"name": name}} for name in sorted(hermes_arena.TOOLS)]

        def run_conversation(self, prompt: str) -> dict[str, object]:
            state = json.loads(prompt.split("Current game data: ", 1)[1])
            assert state["observation"]["to_move"] == role, "Model ran during the other seat's turn"
            decisions.append(state)
            return {"failed": False}

        def close(self) -> None:
            pass

    model = {"provider": "openrouter", "default": "synthetic-model"}
    monkeypatch.setattr(hermes_arena, "configure", lambda handler: (OneTurnAgent, model))
    original_import = importlib.import_module

    def module(name: str) -> object:
        if name == "dotenv":
            return SimpleNamespace(dotenv_values=lambda *args, **kwargs: {})
        if name == "hermes_cli.runtime_provider":
            return SimpleNamespace(resolve_runtime_provider=lambda **kwargs: model)
        return original_import(name)

    monkeypatch.setattr(hermes_arena.importlib, "import_module", module)
    for name in ("HERMES_SAFE_MODE", "HERMES_IGNORE_RULES", "HERMES_IGNORE_USER_CONFIG"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("HERMES_HOME", ".")
    monkeypatch.setattr(sys, "argv", ["hermes_arena.py", "."])
    request = {"match_id": str(uuid.uuid4()), "seat": "first", "seconds": 3600}
    observations = [
        {
            "status": "active",
            "observation": {"you_are": role, "to_move": "second" if role == "first" else "first"},
        },
        {"status": "active", "observation": {"you_are": role, "to_move": role}},
        {"status": "ended"},
    ]
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(request)
            + "\n"
            + "".join(json.dumps({"result": state}) + "\n" for state in observations)
        ),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(hermes_arena.time, "sleep", lambda seconds: None)
    assert hermes_arena.main() == 0
    assert len(decisions) == 1
    assert output.getvalue().splitlines() == [
        '{"operation": "game_join"}',
        '{"operation": "game_state"}',
        '{"operation": "game_state"}',
        '{"finished": true}',
    ]


def test_supervisor_services_the_whole_game_with_only_its_bound_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Joining, playing and terminal observation all use the parent-supplied seat."""
    agent = str(uuid.uuid4())
    owned = arena_runner.StartIntent.parse(intent(agent), agent_id=agent)
    runner = object.__new__(arena_runner.ArenaRunner)
    runner.config = SimpleNamespace(agent_id=agent)
    runner.client = object()
    runner.finished = threading.Event()
    runner.playing = runner.terminal = False
    calls: list[dict[str, object]] = []
    reports: list[str] = []
    monkeypatch.setattr(runner, "_report", reports.append)

    def game(command: dict[str, object], **kwargs: object) -> dict[str, str]:
        """Record the fixed authority and return a three-step synthetic provider lifecycle."""
        calls.append(command)
        return {"status": "ended" if command["operation"] == "game_state" else "active"}

    monkeypatch.setattr(arena_runner.bridge, "_run_game_command", game)
    child = SimpleNamespace(
        stdout=io.StringIO(
            '{"operation":"game_join"}\n{"operation":"game_move","column":3}\n'
            '{"operation":"game_state"}\n{"finished":true}\n'
        ),
        stdin=io.StringIO(),
    )
    runner._serve(child, owned)
    assert [call["operation"] for call in calls] == ["game_join", "game_move", "game_state"]
    assert all(call["match_id"] == owned.match_id and call["seat"] == owned.seat for call in calls)
    assert reports == ["playing"]
    assert runner.terminal and runner.finished.is_set()
    calls.clear()
    malicious = SimpleNamespace(
        stdout=io.StringIO('{"operation":"game_join","match_id":"foreign"}\n'), stdin=io.StringIO()
    )
    runner._serve(malicious, owned)
    assert calls == []


def test_restarted_runner_refuses_uncertain_claim_and_never_launches_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat after restart must not keep an uncertain old child falsely starting."""
    agent = str(uuid.uuid4())
    runner = object.__new__(arena_runner.ArenaRunner)
    runner.config = SimpleNamespace(agent_id=agent)
    runner.journal = arena_runner.RunJournal(tmp_path / "journal.sqlite3")
    runner.active = None
    document = {**intent(agent), "status": "starting", "claimed_by": runner.journal.runner_id}
    writes: list[str] = []

    def post(path: str, payload: object) -> object:
        """Replay an old claim while recording which bounded operations the runner sends."""
        writes.append(path)
        return {"intents": [document]} if path == "/poll" else document

    monkeypatch.setattr(runner, "_post", post)
    monkeypatch.setattr(runner, "_launch", lambda _: pytest.fail("restart launched a second child"))
    runner.tick()
    assert writes == ["/poll", f"/{document['intent_id']}/status"]
    assert runner.active is None
    runner.journal.close()


def test_terminal_runner_waits_for_the_signed_result_without_refusing_or_relaunching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed local game must retain its claim while outcome delivery is delayed."""
    agent = str(uuid.uuid4())
    runner = object.__new__(arena_runner.ArenaRunner)
    runner.config = SimpleNamespace(agent_id=agent)
    runner.journal = arena_runner.RunJournal(tmp_path / "journal.sqlite3")
    document = {**intent(agent), "status": "playing", "claimed_by": runner.journal.runner_id}
    runner.active = arena_runner.StartIntent.parse(document, agent_id=agent)
    runner.deadline = float("inf")
    runner.finished = threading.Event()
    runner.finished.set()
    runner.playing = runner.terminal = True
    runner.child = SimpleNamespace(poll=lambda: 0)
    reports: list[str] = []
    stopped: list[bool] = []

    def post(path: str, payload: dict[str, object]) -> object:
        if path == "/poll":
            return {"intents": [document]}
        reports.append(str(payload["status"]))
        if payload["status"] == "completed":
            raise arena_runner.RunnerRefused("Synthetic signed result has not arrived.")
        pytest.fail("A terminal game was incorrectly changed to refused.")

    def stop() -> None:
        stopped.append(True)
        runner.active = None

    monkeypatch.setattr(runner, "_post", post)
    monkeypatch.setattr(runner, "stop_child", stop)
    monkeypatch.setattr(runner, "_launch", lambda _: pytest.fail("A terminal game was relaunched."))
    runner.tick()
    assert reports == ["completed"]
    assert runner.active is not None and stopped == []
    document["status"] = "completed"
    runner.tick()
    assert stopped == [True] and runner.active is None
    runner.journal.close()


def test_shutdown_closes_both_pipes_after_a_dead_child_even_when_flush_refuses() -> None:
    """Windows can refuse flushing a closed pipe; the stopped child must still be released."""
    runner = object.__new__(arena_runner.ArenaRunner)

    class DeadPipe(io.StringIO):
        def close(self) -> None:
            super().close()
            raise OSError(22, "Synthetic dead Windows pipe")

    output = io.StringIO()
    runner.child = SimpleNamespace(
        poll=lambda: 0, wait=lambda **kwargs: None, stdin=DeadPipe(), stdout=output
    )
    runner.worker = None
    runner.active = object()
    runner.stop_child()
    assert output.closed
    assert runner.child is None and runner.active is None
    runner.stop_child()


def test_shutdown_refuses_to_release_a_worker_that_has_not_stopped() -> None:
    """A live operation retains its profile/claim even after the model child exits."""
    runner = object.__new__(arena_runner.ArenaRunner)
    source, output = io.StringIO(), io.StringIO()
    runner.child = SimpleNamespace(
        poll=lambda: 0, wait=lambda **kwargs: None, stdin=source, stdout=output
    )
    runner.worker = SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: True)
    active = runner.active = object()
    with pytest.raises(arena_runner.RunnerRefused, match="has not stopped"):
        runner.stop_child()
    assert not source.closed and not output.closed
    assert runner.active is active and runner.child is not None


def test_a_linked_key_or_journal_cannot_borrow_another_profile(tmp_path: Path) -> None:
    """Reject nested symlinks before reading any key or creating a launch journal."""
    from agentnexus_sdk.connector import Paths

    own = Paths.for_profile(tmp_path / "install", "agent2")
    own.key_directory.mkdir(parents=True)
    other = tmp_path / "other-key"
    other.write_text("synthetic unrelated profile key", encoding="utf-8")
    own.private_key.symlink_to(other)
    with pytest.raises(arena_runner.RunnerRefused):
        arena_runner.profile_storage(own)


@pytest.mark.parametrize("enable", [True, False])
def test_service_lifecycle_refuses_a_unit_linked_to_another_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enable: bool
) -> None:
    """Neither enabling nor stopping a profile may act through another profile's unit."""
    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    other = units / "agentnexus-arena-other.service"
    other.write_text("synthetic unrelated unit", encoding="utf-8")
    unit = units / "agentnexus-arena-agent2.service"
    unit.symlink_to(other)
    calls: list[object] = []
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(arena_runner.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    paths = SimpleNamespace(
        profile="agent2", install_root=SimpleNamespace(resolve=lambda: "/synthetic-install")
    )
    with pytest.raises(arena_runner.RunnerRefused, match="unit"):
        arena_runner._service(paths, enable=enable)
    assert other.read_text(encoding="utf-8") == "synthetic unrelated unit"
    assert unit.is_symlink()
    assert calls == []


def test_journal_never_shares_runner_identity_between_profiles(tmp_path: Path) -> None:
    """Two independent profile journals hold distinct identities and reservations."""
    first = arena_runner.RunJournal(tmp_path / "first" / "journal.sqlite3")
    second = arena_runner.RunJournal(tmp_path / "second" / "journal.sqlite3")
    assert first.runner_id != second.runner_id
    runner_id = first.runner_id
    first.close()
    reopened = arena_runner.RunJournal(tmp_path / "first" / "journal.sqlite3")
    assert reopened.runner_id == runner_id
    reopened.close()
    second.close()


@pytest.mark.parametrize("mutation", ["profile", "reservation"])
def test_authority_oracles_kill_deliberately_weakened_guards(tmp_path: Path, mutation: str) -> None:
    """Run the same authority assertion against restored source and a deliberately weakened copy."""
    source = Path(arena_runner.__file__).read_text(encoding="utf-8")
    if mutation == "profile":
        original = "identity != _uuid(agent_id)"
        replacement = "False"
    else:
        original = "return cursor.rowcount == 1"
        replacement = "return True"
    assert source.count(original) == 1
    path = tmp_path / "mutant.py"
    path.write_text(source.replace(original, replacement), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("arena_mutant", path)
    assert spec is not None and spec.loader is not None
    mutant = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mutant
    try:
        spec.loader.exec_module(mutant)
        if mutation == "profile":
            document = intent(str(uuid.uuid4()))
            with pytest.raises(AssertionError):
                accepted = mutant.StartIntent.parse(document, agent_id=str(uuid.uuid4()))
                assert accepted.agent_id != document["agent_id"]
        else:
            journal = mutant.RunJournal(tmp_path / "mutant.sqlite3")
            identifier = str(uuid.uuid4())
            assert journal.reserve(identifier)
            with pytest.raises(AssertionError):
                assert not journal.reserve(identifier)
            journal.close()
    finally:
        del sys.modules[spec.name]


@pytest.mark.parametrize("outcome", ["no-move", "failed", "invalid", "exception"])
def test_model_phase_diagnostics_never_copy_runtime_output(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Distinguish a pending call, returned failure and no move without copying private output."""
    closed: list[bool] = []
    calls: list[str] = []

    class SyntheticAgent:
        def __init__(self, **kwargs: object) -> None:
            self.tools = [{"function": {"name": name}} for name in sorted(hermes_arena.TOOLS)]

        def run_conversation(self, prompt: str) -> object:
            calls.append(prompt)
            print("synthetic-private-runtime-output")
            if outcome == "exception":
                raise RuntimeError("synthetic-private-exception")
            if outcome == "failed":
                return {"failed": True, "error": "synthetic-private-provider-output"}
            if outcome == "invalid":
                return "synthetic-private-model-output"
            return {"failed": False, "answer": "synthetic-private-model-output"}

        def close(self) -> None:
            closed.append(True)

    model = {"provider": "openrouter", "default": "synthetic-model"}
    monkeypatch.setattr(hermes_arena, "configure", lambda handler: (SyntheticAgent, model))
    original_import = importlib.import_module

    def module(name: str) -> object:
        if name == "dotenv":
            return SimpleNamespace(dotenv_values=lambda *args, **kwargs: {})
        if name == "hermes_cli.runtime_provider":
            return SimpleNamespace(resolve_runtime_provider=lambda **kwargs: model)
        return original_import(name)

    monkeypatch.setattr(hermes_arena.importlib, "import_module", module)
    for name in ("HERMES_SAFE_MODE", "HERMES_IGNORE_RULES", "HERMES_IGNORE_USER_CONFIG"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("HERMES_HOME", ".")
    monkeypatch.setattr(sys, "argv", ["hermes_arena.py", "."])
    request = {"match_id": str(uuid.uuid4()), "seat": "first", "seconds": 3600}
    states = [
        {"status": "active", "observation": {"you_are": "first", "to_move": "first"}},
        {"status": "ended"},
    ]
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(request)
            + "\n"
            + "".join(json.dumps({"result": state}) + "\n" for state in states)
        ),
    )
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    if outcome == "exception":
        with pytest.raises(RuntimeError):
            hermes_arena.main()
    else:
        assert hermes_arena.main() == (0 if outcome == "no-move" else 3)
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    diagnostics = [message for message in messages if "diagnostic" in message]
    expected = {
        "no-move": ["model_call_started", "model_call_returned", "decision_without_move"],
        "failed": ["model_call_started", "model_call_returned", "model_call_failed"],
        "invalid": ["model_call_started", "model_call_returned", "model_return_invalid"],
        "exception": ["model_call_started", "model_call_exception"],
    }
    assert [message["diagnostic"] for message in diagnostics] == expected[outcome]
    assert all(set(message) == {"diagnostic", "duration_ms"} for message in diagnostics)
    assert all(
        type(message["duration_ms"]) is int and message["duration_ms"] >= 0
        for message in diagnostics
    )
    assert "synthetic-private" not in output.getvalue()
    assert len(calls) == 1 and closed == [True]


def diagnostic_supervisor() -> tuple[arena_runner.ArenaRunner, arena_runner.StartIntent]:
    """Construct a synthetic supervisor without opening a profile, key, process or connection."""
    agent = str(uuid.uuid4())
    owned = arena_runner.StartIntent.parse(intent(agent), agent_id=agent)
    runner = object.__new__(arena_runner.ArenaRunner)
    runner.config = SimpleNamespace(agent_id=agent)
    runner.client = object()
    runner.finished = threading.Event()
    runner.playing = runner.terminal = False
    runner._report = lambda status: None
    return runner, owned


def test_parent_correlates_only_closed_diagnostics_and_game_phases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Log parent-owned identifiers and timings, never model, provider or request content."""
    runner, owned = diagnostic_supervisor()
    calls: list[dict[str, object]] = []

    def game(command: dict[str, object], **kwargs: object) -> dict[str, object]:
        calls.append(command)
        return {"status": "active", "private": "synthetic-private-provider-output"}

    monkeypatch.setattr(arena_runner.bridge, "_run_game_command", game)
    child = SimpleNamespace(
        stdout=io.StringIO(
            '{"diagnostic":"model_call_started","duration_ms":0}\n'
            '{"operation":"game_join"}\n'
            '{"operation":"game_move","column":3}\n'
            '{"diagnostic":"model_call_returned","duration_ms":123}\n'
            '{"finished":true}\n'
        ),
        stdin=io.StringIO(),
    )
    runner._serve(child, owned)
    raw = capsys.readouterr().out
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["event"] for record in records] == [
        "model_call_started",
        "game_join_started",
        "game_join_returned",
        "game_move_started",
        "game_move_returned",
        "model_call_returned",
    ]
    assert all(
        set(record) == {"kind", "event", "match_id", "intent_id", "seat", "duration_ms"}
        and record["kind"] == "arena_runtime"
        and record["match_id"] == owned.match_id
        and record["intent_id"] == owned.intent_id
        and record["seat"] == owned.seat
        and type(record["duration_ms"]) is int
        and record["duration_ms"] >= 0
        for record in records
    )
    assert records[-1]["duration_ms"] == 123
    assert "synthetic-private" not in raw
    assert len(calls) == 2 and runner.finished.is_set()


@pytest.mark.parametrize(
    "document",
    [
        {"diagnostic": "model_call_started", "duration_ms": 0, "prompt": "synthetic-private"},
        {"diagnostic": "synthetic-private", "duration_ms": 0},
        {"diagnostic": ["model_call_started"], "duration_ms": 0},
        {"diagnostic": "model_call_started", "duration_ms": True},
        {"diagnostic": "model_call_started", "duration_ms": -1},
        {"diagnostic": "model_call_started", "duration_ms": 3600001},
        {"diagnostic": "model_call_started", "duration_ms": "synthetic-private"},
        {"diagnostic": "model_call_started"},
    ],
)
def test_invalid_diagnostic_refuses_before_another_game_operation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    document: dict[str, object],
) -> None:
    """Unknown events, extra fields and invalid durations fail closed without being copied."""
    runner, owned = diagnostic_supervisor()
    calls: list[object] = []
    monkeypatch.setattr(arena_runner.bridge, "_run_game_command", lambda *a, **k: calls.append(a))
    child = SimpleNamespace(
        stdout=io.StringIO(json.dumps(document) + '\n{"operation":"game_join"}\n'),
        stdin=io.StringIO(),
    )
    runner._serve(child, owned)
    raw = capsys.readouterr().out
    records = [json.loads(line) for line in raw.splitlines()]
    assert calls == [] and runner.finished.is_set()
    assert [record["event"] for record in records] == ["protocol_refused"]
    assert "synthetic-private" not in raw and "prompt" not in raw


def test_diagnostic_flood_is_bounded_before_another_game_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A child cannot flood the service journal with otherwise valid phase messages."""
    runner, owned = diagnostic_supervisor()
    calls: list[object] = []
    monkeypatch.setattr(arena_runner.bridge, "_run_game_command", lambda *a, **k: calls.append(a))
    child = SimpleNamespace(
        stdout=io.StringIO(
            '{"diagnostic":"model_call_started","duration_ms":0}\n' * 257
            + '{"operation":"game_join"}\n'
        ),
        stdin=io.StringIO(),
    )
    runner._serve(child, owned)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert calls == [] and runner.finished.is_set()
    assert len(records) == 257 and records[-1]["event"] == "protocol_refused"


@pytest.mark.parametrize("failure", ["refused", "io"])
def test_parent_failure_codes_never_log_exception_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    """Classify tool refusal and I/O failure with fixed events, not exception or provider text."""
    runner, owned = diagnostic_supervisor()

    def game(*args: object, **kwargs: object) -> object:
        if failure == "refused":
            raise arena_runner.games.GameRefusedError(
                "provider.move_not_legal", "synthetic-private-provider-output"
            )
        raise OSError("synthetic-private-io-output")

    monkeypatch.setattr(arena_runner.bridge, "_run_game_command", game)
    child = SimpleNamespace(
        stdout=io.StringIO('{"operation":"game_move","column":3}\n'), stdin=io.StringIO()
    )
    runner._serve(child, owned)
    raw = capsys.readouterr().out
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["event"] for record in records] == [
        "game_move_started",
        "game_move_refused" if failure == "refused" else "io_failed",
    ]
    assert "synthetic-private" not in raw


def test_new_phase_channel_keeps_raw_child_stderr_discarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Structured diagnostics must not enable unfiltered Hermes or provider stderr."""
    runner, owned = diagnostic_supervisor()
    runner_id = str(uuid.uuid4())
    runner.journal = SimpleNamespace(runner_id=runner_id, reserve=lambda identifier: True)
    runner.paths = SimpleNamespace(root=tmp_path)
    runner.runtime = arena_runner.HermesRun(tmp_path, tmp_path, tmp_path)
    document = {**intent(owned.agent_id), "status": "starting", "claimed_by": runner_id}
    runner._post = lambda suffix, payload: document
    spawned: list[dict[str, object]] = []

    def spawn(*args: object, **kwargs: object) -> object:
        spawned.append(kwargs)
        return SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO())

    monkeypatch.setattr(arena_runner.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        arena_runner.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None)
    )
    runner._launch(owned)
    assert len(spawned) == 1
    assert spawned[0]["stderr"] == arena_runner.subprocess.DEVNULL
