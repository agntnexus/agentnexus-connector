"""#195: fixed intents, profile containment and durable at-most-once launch on hosted CI."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
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
