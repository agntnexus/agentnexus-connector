"""#228: the OpenClaw driver's proofs, and the guards of its worker, bridge and configuration.

The preflight runs the stand-in OpenClaw through the driver's own code, including the canary run
against a model of the Connector's own. Every guard of the worker, the bridge and the overlay is
then broken on purpose and the proof beside it must refuse.
"""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from arena_fakes import expect_guard, load_mutant
from arena_openclaw_support import stand_in_handle, survivors
from arena_process_harness import profile_changes, snapshot
from fake_chat_model import FakeChatModel

from agentnexus_sdk import (
    arena_driver,
    arena_driver_openclaw,
    arena_match,
    openclaw_arena,
    openclaw_bridge,
    runtimes,
)

SOURCE = Path(arena_match.__file__).parent


@pytest.fixture
def model() -> Any:
    """Provide a loopback chat model that answers the first request with a move."""
    server = FakeChatModel("move")
    yield server
    server.close()


# ---------------------------------------------------------------------------------------------
# The preflight
# ---------------------------------------------------------------------------------------------


def test_the_preflight_proves_three_tools_and_leaves_the_profile_alone(
    tmp_path: Path, model: FakeChatModel
) -> None:
    """The canary shows exactly the three operations; nothing is written into the profile."""
    handle = stand_in_handle(tmp_path, model)
    before = snapshot(tmp_path / "home")
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    arena_driver.check_preflight(driver, handle)
    assert driver.preflight(handle) == arena_match.TOOLS
    assert profile_changes(before, snapshot(tmp_path / "home")) == []
    assert model.requests == [], "the preflight used the profile's own route"


@pytest.mark.skipif(
    not os.environ.get("AGENTNEXUS_OPENCLAW_COMMAND"),
    reason="set AGENTNEXUS_OPENCLAW_COMMAND to an isolated reviewed CLI argv",
)
def test_the_reviewed_openclaw_install_proves_its_isolated_three_tool_path(
    tmp_path: Path, model: FakeChatModel
) -> None:
    """Exercise the installed CLI only with a disposable profile and loopback canary model."""
    command = json.loads(os.environ["AGENTNEXUS_OPENCLAW_COMMAND"])
    assert isinstance(command, list) and command and all(isinstance(part, str) for part in command)
    version = subprocess.run(  # noqa: S603 - the reviewed runtime command of the test environment
        [*command, "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert version.returncode == 0 and "OpenClaw 2026.9.9" in version.stdout
    config = tmp_path / "profile" / "openclaw.json"
    config.parent.mkdir()
    config.parent.chmod(0o700)
    config.write_text(json.dumps(model.profile_config()), encoding="utf-8")
    state = tmp_path / "profile" / "state"
    state.mkdir()
    auth_store = state / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
    auth_store.parent.mkdir(parents=True)
    sqlite3.connect(auth_store).close()
    auth_before = snapshot(auth_store.parent)
    config_before = config.read_bytes()
    handle = arena_driver_openclaw.OpenClawRun(
        tuple(command), "2026.9.9", config, state, config.parent
    )
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    tools = driver.preflight(handle)
    arena_driver.prove_tools(driver, tools)
    assert tools == arena_match.TOOLS
    assert config.read_bytes() == config_before
    assert profile_changes(auth_before, snapshot(auth_store.parent)) == []
    assert model.requests == [], "preflight used the profile route instead of its loopback canary"


def test_a_runtime_that_offers_a_fourth_tool_is_refused(
    tmp_path: Path, model: FakeChatModel
) -> None:
    """What the model sees is what is held to the contract, not what was configured."""
    handle = stand_in_handle(tmp_path, model, fault="extra_tool_in_request")
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    assert driver.preflight(handle) != arena_match.TOOLS
    with pytest.raises(arena_driver.DriverRefusedError) as raised:
        arena_driver.check_preflight(driver, handle)
    assert raised.value.code == "contract_violated"


def test_a_runtime_that_fails_after_reporting_its_tools_is_refused(
    tmp_path: Path, model: FakeChatModel
) -> None:
    """A request containing three tools is not proof when the decision process fails."""
    handle = stand_in_handle(tmp_path, model, fault="cleanup_fail")
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    with pytest.raises(arena_driver.DriverRefusedError) as raised:
        arena_driver.check_preflight(driver, handle)
    assert raised.value.code == "preflight_refused"


def test_the_preflight_exit_status_guard_is_live(tmp_path: Path, model: FakeChatModel) -> None:
    """Removing the invocation-success check makes the failed-runtime oracle fail."""

    def refuses_failed_runtime(module: ModuleType) -> None:
        handle = stand_in_handle(
            tmp_path / module.__name__.replace(".", "_"), model, fault="cleanup_fail"
        )
        try:
            arena_driver.check_preflight(module.OpenClawArenaDriver(), handle)
        except arena_driver.DriverRefusedError as refused:
            assert refused.code == "preflight_refused"
        else:
            raise AssertionError("a runtime that failed after reporting its tools passed")

    mutant = load_mutant(
        tmp_path / "mutants",
        arena_driver_openclaw,
        "                if canary_run.returncode != 0:\n                    raise refusal\n",
        "                if False:\n                    raise refusal\n",
    )
    expect_guard(refuses_failed_runtime, arena_driver_openclaw, mutant)


def test_a_profile_with_no_usable_route_is_refused_before_any_claim(
    tmp_path: Path, model: FakeChatModel
) -> None:
    """No provider, no authentication: the runtime says so and the driver refuses with its code."""
    handle = stand_in_handle(tmp_path, model, profile={})
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    with pytest.raises(arena_driver.DriverRefusedError) as raised:
        arena_driver.check_preflight(driver, handle)
    assert raised.value.code == "preflight_refused"
    assert str(raised.value) == "OpenClaw refused the exact three-tool Arena preflight."


def test_a_timed_out_preflight_reaps_the_runtime_process_tree(
    tmp_path: Path, model: FakeChatModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preflight timeout must not leave the runtime or its MCP child running."""
    monkeypatch.setattr(arena_driver_openclaw, "STEP_SECONDS", 20)
    monkeypatch.setattr(arena_driver_openclaw, "CANARY_SECONDS", 2)
    handle = stand_in_handle(tmp_path, model, fault="hang,leak_child,ignore_sigterm")
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    try:
        with pytest.raises(arena_driver.DriverRefusedError):
            arena_driver.check_preflight(driver, handle)
        leaked = survivors(tmp_path, wait=2.0)
        assert leaked == [], f"preflight left runtime processes alive: {leaked}"
    finally:
        for pid in survivors(tmp_path, wait=0.1):
            if os.name == "nt":
                subprocess.run(  # noqa: S603 - the platform command to end a test-owned tree
                    ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607 - a system command
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            else:
                os.kill(pid, signal.SIGKILL)


def inspect_with(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    version: str | None,
    isolation: str = "isolated",
    state_override: Path | None = None,
) -> Any:
    """Run `inspect` against a runtime detection the test chooses."""
    executable = shutil.which("python") or sys.executable
    detection = runtimes.RuntimeDetection(
        installed=version is not None, version=version, executable=executable
    )
    monkeypatch.setattr(runtimes.OpenClawAdapter, "detect", lambda self: detection)
    profile = tmp_path / "profile"
    runtime = profile / "runtime" / "openclaw"
    runtime.mkdir(parents=True)
    profile.chmod(0o700)
    config = runtime / "openclaw.json"
    config.write_text("{}", encoding="utf-8")
    state = runtime / "state"
    state.mkdir()
    context = SimpleNamespace(
        server_name="agentnexus", openclaw_config=config, openclaw_state=state_override or state
    )
    paths = SimpleNamespace(root=profile, isolation=isolation, runtime_context=lambda: context)
    return arena_driver_openclaw.OpenClawArenaDriver().inspect(paths)


def test_only_a_reviewed_release_is_inspected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reviewed release passes; another release, or none, is refused as unreviewed."""
    assert inspect_with(monkeypatch, tmp_path / "valid", "2026.9.9").version == "2026.9.9"
    for version in ("2026.9.8", "2026.10.1", None):
        with pytest.raises(arena_driver.DriverRefusedError) as raised:
            inspect_with(monkeypatch, tmp_path / f"version-{version}", version)
        assert raised.value.code == "unreviewed"


def test_a_shared_profile_is_not_isolated_and_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default shared context cannot enable automatic play."""
    with pytest.raises(arena_driver.DriverRefusedError) as raised:
        inspect_with(monkeypatch, tmp_path, "2026.9.9", isolation="shared")
    assert raised.value.code == "not_isolated"


def test_an_openclaw_state_outside_its_profile_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A state directory that is not below the profile's own is refused at inspection."""
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(arena_driver.DriverRefusedError) as raised:
        inspect_with(monkeypatch, tmp_path, "2026.9.9", state_override=outside)
    assert raised.value.code == "not_isolated"


# ---------------------------------------------------------------------------------------------
# Generation and the declared model
# ---------------------------------------------------------------------------------------------


def test_the_generation_follows_the_configuration_and_ignores_the_content(
    tmp_path: Path, model: FakeChatModel
) -> None:
    """Metadata only: a rewrite of the configuration is a new generation, a read is not."""
    handle = stand_in_handle(tmp_path, model)
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    first = driver.generation(handle)
    assert first is not None and len(first) == 32
    assert driver.generation(handle) == first
    (tmp_path / "home" / "state" / "canary.txt").write_text("elsewhere", encoding="utf-8")
    assert driver.generation(handle) == first, "the state directory is not watched"
    handle.config.write_text(json.dumps({"agents": {}}), encoding="utf-8")  # type: ignore[union-attr]
    assert driver.generation(handle) != first


def test_the_declared_model_is_the_runtimes_own_line_if_the_forums_check_accepts_it(
    tmp_path: Path, model: FakeChatModel
) -> None:
    """Optional: the profile's route as the runtime reports it, or nothing."""
    handle = stand_in_handle(tmp_path, model)
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    assert driver.declared_model(handle) == "fakeprov/fake-model"
    bare = stand_in_handle(tmp_path / "bare", model, profile={})
    assert driver.declared_model(bare) is None


def test_the_declared_model_uses_the_shared_runtime_annotation_semantics(
    tmp_path: Path, model: FakeChatModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strip only the existing trailing runtime annotation before the same RMD-1 check."""
    handle = stand_in_handle(tmp_path, model)
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    monkeypatch.setattr(
        driver,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[3], 0, stdout="opaque/model (runtime-tag)\n", stderr=""
        ),
    )
    assert driver.declared_model(handle) == "opaque/model"


# ---------------------------------------------------------------------------------------------
# The worker's relay, the bridge and the overlay
# ---------------------------------------------------------------------------------------------


class Relay:
    """The supervisor's side of one worker relay: what is sent, and what is answered."""

    def __init__(self, module: ModuleType, replies: list[dict[str, Any]]) -> None:
        """Queue the supervisor's replies."""
        self.module = module
        self.sent: list[dict[str, Any]] = []
        self.incoming: queue.Queue[str] = queue.Queue()
        for reply in replies:
            self.incoming.put(json.dumps(reply))
        self.state = module.Decision()

    def ask(self, request: Any) -> dict[str, Any]:
        """Hand one bridge request to the worker's answer."""
        answer: dict[str, Any] = self.module.answer(
            request, self.state, self.incoming, self.sent.append
        )
        return answer


def relay_oracle(module: ModuleType) -> None:
    """Require the worker to forward only the three bound operations, and one accepted move."""
    relay = Relay(
        module, [{"result": {"status": "active"}}, {"complete": True}, *[{"result": {}}] * 12]
    )
    for request in (
        {"op": "exec", "arguments": {}},
        {"op": "game_state", "arguments": {"url": "http://elsewhere"}},
        {"op": "game_move", "arguments": {"move": "../../etc"}},
        {"op": "game_move", "arguments": {"move": "e2e4"}, "extra": 1},
        "not a request",
    ):
        answer = relay.ask(request)
        assert answer.get("error") is True, request
    assert relay.sent == [], "something outside the contract was forwarded"
    assert json.loads(relay.ask({"op": "game_state", "arguments": {}})["text"]) == {
        "status": "active"
    }
    assert "accepted" in relay.ask({"op": "game_move", "arguments": {"move": "e2e4"}})["text"]
    assert relay.state.accepted.is_set()
    sent = len(relay.sent)
    assert relay.ask({"op": "game_move", "arguments": {"move": "d2d4"}}).get("error") is True
    assert len(relay.sent) == sent, "a second move was forwarded after the accepted one"


def test_the_worker_forwards_only_the_bound_operations_and_one_accepted_move() -> None:
    """Fourth tool, extra arguments, a URL, a free path and a second move all stop here."""
    relay_oracle(openclaw_arena)


def bridge_oracle(module: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    """Require the MCP bridge to list three tools and to refuse a fourth."""
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "exec"}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "game_state"}},
    ]
    module.serve(io.StringIO("".join(json.dumps(line) + "\n" for line in lines)))
    answers = {item["id"]: item for item in map(json.loads, capsys.readouterr().out.splitlines())}
    assert sorted(tool["name"] for tool in answers[2]["result"]["tools"]) == sorted(
        arena_match.TOOLS
    )
    assert "error" in answers[3], "a fourth tool was served"
    assert answers[4]["result"]["isError"] is True, "a call with no relay did not fail closed"


def test_the_bridge_lists_three_tools_and_refuses_a_fourth(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No relay is open here, so even a real tool fails closed."""
    bridge_oracle(openclaw_bridge, capsys)


def environment_oracle(module: ModuleType, tmp_path: Path) -> None:
    """Require nothing of the service's environment but OS essentials to reach the runtime."""
    base = {
        "PATH": "/bin",
        "SYSTEMROOT": "C:/Windows",
        "SYNTHETIC_PROVIDER_KEY": "synthetic",
        "AGENTNEXUS_PRIVATE_KEY_FILE": "/keys/agent.pem",
        "HOME": "/home/real",
        "CODEX_HOME": "/home/real/.x",
    }
    work = tmp_path / "work"
    environment = module.cli_environment(base, work, work / "arena.json", tmp_path)
    assert not set(environment) & {
        "SYNTHETIC_PROVIDER_KEY",
        "AGENTNEXUS_PRIVATE_KEY_FILE",
        "CODEX_HOME",
    }, sorted(environment)
    assert environment["OPENCLAW_CONFIG_READONLY"] == "1"
    assert environment["OPENCLAW_NO_AUTO_UPDATE"] == "1"
    for name in ("HOME", "USERPROFILE", "OPENCLAW_STATE_DIR", "TMPDIR"):
        assert environment[name].startswith(str(work)), name


def test_the_runtime_gets_only_os_essentials_and_a_throwaway_world(tmp_path: Path) -> None:
    """No key, no token and no other tool's home reach the runtime."""
    environment_oracle(openclaw_arena, tmp_path)


def test_profile_auth_state_and_per_decision_state_are_separate(tmp_path: Path) -> None:
    """The runtime sees its owned auth root; the agent exec still gets disposable session state."""
    profile = tmp_path / "profile"
    state = profile / "runtime" / "openclaw" / "state"
    state.mkdir(parents=True)
    profile.chmod(0o700)
    profile_config = profile / "runtime" / "openclaw" / "openclaw.json"
    profile_config.write_text("{}", encoding="utf-8")
    work = tmp_path / "work"
    openclaw_arena.make_world(work)
    environment = openclaw_arena.cli_environment(
        {},
        work,
        work / "arena.json",
        profile / "runtime" / "openclaw",
        profile_root=profile,
        profile_config=profile_config,
        profile_state=state,
    )
    assert environment["OPENCLAW_STATE_DIR"] == str(state)
    argv = openclaw_arena.exec_arguments([], work / "prompt.txt", 20, work / "arena.json", work)
    session_state = Path(argv[argv.index("--state-dir") + 1])
    assert session_state == work / "run"
    assert session_state != state


def test_profile_auth_state_must_be_inside_the_owned_runtime_home(tmp_path: Path) -> None:
    """A symlink or path outside the Connector-owned profile is refused without opening files."""
    profile = tmp_path / "profile"
    runtime = profile / "runtime" / "openclaw"
    config = runtime / "openclaw.json"
    state = runtime / "state"
    state.mkdir(parents=True)
    profile.chmod(0o700)
    config.write_text("{}", encoding="utf-8")
    assert openclaw_arena.profile_context_valid(profile, config, state)

    outside = tmp_path / "outside"
    outside.mkdir()
    assert not openclaw_arena.profile_context_valid(profile, config, outside)

    linked_profile = tmp_path / "linked-profile"
    linked_runtime = linked_profile / "runtime" / "openclaw"
    linked_runtime.mkdir(parents=True)
    linked_profile.chmod(0o700)
    (linked_runtime / "openclaw.json").write_text("{}", encoding="utf-8")
    try:
        (linked_runtime / "state").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable on this host: {type(error).__name__}")
    assert not openclaw_arena.profile_context_valid(
        linked_profile, linked_runtime / "openclaw.json", linked_runtime / "state"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX profile ownership/mode checks are unavailable")
def test_profile_auth_state_requires_private_profile_ownership(tmp_path: Path) -> None:
    """The profile root's owner and private mode govern access to the runtime-owned store."""
    profile = tmp_path / "profile"
    runtime = profile / "runtime" / "openclaw"
    runtime.mkdir(parents=True)
    profile.chmod(0o700)
    config = runtime / "openclaw.json"
    config.write_text("{}", encoding="utf-8")
    state = runtime / "state"
    state.mkdir()
    assert openclaw_arena.profile_context_valid(profile, config, state)
    profile.chmod(0o755)
    try:
        assert not openclaw_arena.profile_context_valid(profile, config, state)
    finally:
        profile.chmod(0o700)


def test_openclaw_agent_directories_must_stay_in_the_owned_profile(tmp_path: Path) -> None:
    """The agent directories the runtime reports must all lie below the profile."""
    profile = tmp_path / "profile"
    state = profile / "runtime" / "openclaw" / "state"
    state.mkdir(parents=True)
    profile.chmod(0o700)
    owned = state / "agents" / "main" / "agent"
    escaped = tmp_path / "shared-openclaw-agent"
    assert openclaw_arena.agent_directories_valid(
        [{"id": "main", "agentDir": str(owned)}], "main", profile, state
    )
    assert not openclaw_arena.agent_directories_valid(
        [{"id": "main", "agentDir": str(escaped)}], "main", profile, state
    )
    link_target = profile / "alternate-agent"
    link_target.mkdir()
    linked_path = state / "agents" / "linked" / "agent"
    try:
        linked_path.parent.symlink_to(link_target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable on this host: {type(error).__name__}")
    assert not openclaw_arena.agent_directories_valid(
        [{"id": "linked", "agentDir": str(linked_path)}], "main", profile, state
    )


def test_openclaw_agent_directory_path_guard_is_live(tmp_path: Path) -> None:
    """Break the containment check: the oracle for an escaped agent directory must fail."""
    profile = tmp_path / "profile"
    state = profile / "runtime" / "openclaw" / "state"
    state.mkdir(parents=True)
    profile.chmod(0o700)
    entries = [{"id": "main", "agentDir": str(tmp_path / "outside-agent")}]

    def rejects_escape(module: ModuleType) -> None:
        assert not module.agent_directories_valid(entries, "main", profile, state)

    mutant_path = tmp_path / "mutants"
    mutant_path.mkdir()
    shutil.copyfile(
        Path(openclaw_arena.__file__).with_name("arena_match.py"),
        mutant_path / "arena_match.py",
    )
    mutant = load_mutant(
        mutant_path,
        openclaw_arena,
        "if not path.resolve(strict=False).is_relative_to(root):",
        "if False:",
    )
    expect_guard(rejects_escape, openclaw_arena, mutant)


def test_an_escaped_runtime_auth_path_is_refused_before_proof(
    tmp_path: Path, model: FakeChatModel
) -> None:
    """A reported external agent store fails preflight before a model call."""
    handle = stand_in_handle(tmp_path, model, fault="agent_path_escape")
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    with pytest.raises(arena_driver.DriverRefusedError) as raised:
        arena_driver.check_preflight(driver, handle)
    assert raised.value.code == "preflight_refused"
    assert model.requests == []


def overlay_oracle(module: ModuleType) -> None:
    """Require the overlay to close the profile to three tools and one server."""
    document = module.overlay_document(
        Path("profile.json"),
        python="python",
        bridge=Path("bridge.py"),
        address="addr",
        key="00",
        disabled=["theirs", "arena"],
    )
    assert document["$include"] == "profile.json"
    assert document["tools"] == {"allow": list(module.PREFIXED), "toolSearch": False}
    servers = document["mcp"]["servers"]
    assert servers.get("theirs") == {"enabled": False}, "the profile's server stays on"
    assert servers["arena"]["enabled"] is True
    assert servers["arena"]["toolFilter"] == {"include": sorted(arena_match.TOOLS)}
    assert set(servers["arena"]["env"]) == {
        "ARENA_BRIDGE_ADDRESS",
        "ARENA_BRIDGE_KEY",
    }
    assert document["update"] == {"checkOnStart": False}


def test_the_overlay_closes_the_profile_to_three_tools_and_one_server() -> None:
    """The allow-list replaces the profile's, tool search is off, the profile's servers are off."""
    overlay_oracle(openclaw_arena)


# ---------------------------------------------------------------------------------------------
# Static boundaries, and mutation proofs
# ---------------------------------------------------------------------------------------------


def test_the_generation_reads_no_file_that_may_hold_a_credential() -> None:
    """Stat only: a configuration or secrets file is never opened to make a token."""
    import ast

    tree = ast.parse((SOURCE / "arena_driver_openclaw.py").read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "generation"
    )
    reads = {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read_text", "read_bytes", "open", "load", "loads"}
    }
    assert reads == set()


def mutant_of(tmp_path: Path, module: ModuleType, original: str, replacement: str) -> ModuleType:
    """Return a copy of a module with one line broken, next to the match program it loads."""
    (tmp_path / "mutant").mkdir(parents=True, exist_ok=True)
    shutil.copy(arena_match.__file__, tmp_path / "mutant" / "arena_match.py")
    return load_mutant(tmp_path / "mutant", module, original, replacement)


WORKER_MUTATIONS: dict[str, tuple[str, str]] = {
    "a-second-move-is-forwarded": (
        "        if state.accepted.is_set():\n"
        '            return {"text": "The move was already accepted. Stop.", "error": True}\n',
        "",
    ),
    "the-request-is-not-bounded": (
        'bounded = arena.bounded_request(request["op"], request["arguments"])',
        'bounded = {"operation": request["op"], **request["arguments"]}',
    ),
}


@pytest.mark.parametrize("name", sorted(WORKER_MUTATIONS))
def test_a_broken_relay_guard_is_noticed(name: str, tmp_path: Path) -> None:
    """The relay proof holds for the real worker and fails for the worker with this guard broken."""
    original, replacement = WORKER_MUTATIONS[name]
    mutant = mutant_of(tmp_path, openclaw_arena, original, replacement)
    expect_guard(relay_oracle, openclaw_arena, mutant)


def test_a_worker_that_passes_the_whole_environment_on_is_noticed(tmp_path: Path) -> None:
    """The environment proof fails for a worker that does not drop the service's variables."""
    mutant = mutant_of(
        tmp_path,
        openclaw_arena,
        "environment = {key: value for key, value in base.items() if key.upper() in KEPT}",
        "environment = dict(base)",
    )
    expect_guard(lambda module: environment_oracle(module, tmp_path), openclaw_arena, mutant)


OVERLAY_MUTATIONS: dict[str, tuple[str, str]] = {
    "tool-search-is-left-on": (
        '"tools": {"allow": list(PREFIXED), "toolSearch": False},',
        '"tools": {"allow": list(PREFIXED)},',
    ),
    "the-allow-list-is-the-profiles": (
        '"tools": {"allow": list(PREFIXED), "toolSearch": False},',
        '"tools": {"toolSearch": False},',
    ),
    "the-profiles-servers-stay-on": (
        'servers: dict[str, Any] = {name: {"enabled": False} '
        "for name in disabled if name != SERVER}",
        "servers: dict[str, Any] = {}",
    ),
}


@pytest.mark.parametrize("name", sorted(OVERLAY_MUTATIONS))
def test_a_broken_overlay_guard_is_noticed(name: str, tmp_path: Path) -> None:
    """The overlay proof fails when the allow-list, tool search or the server switch is broken."""
    original, replacement = OVERLAY_MUTATIONS[name]
    mutant = mutant_of(tmp_path, openclaw_arena, original, replacement)
    expect_guard(overlay_oracle, openclaw_arena, mutant)


def test_a_bridge_that_serves_a_fourth_tool_is_noticed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bridge proof fails for a bridge whose tool names include another one."""
    mutant = load_mutant(
        tmp_path / "mutant",
        openclaw_bridge,
        'NAMES = frozenset(tool["name"] for tool in TOOLS)',
        'NAMES = frozenset(tool["name"] for tool in TOOLS) | {"exec"}',
    )
    expect_guard(lambda module: bridge_oracle(module, capsys), openclaw_bridge, mutant)
