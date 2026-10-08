"""#228: OpenClaw uses its own profile-bound authentication store; the Connector never touches it.

The owner confirmed that OpenClaw may open its own store through its normal, supported boundary and
that the Connector may pass it only the bounded profile context it needs to do so. Held here:

* the profile's state directory is owned, private and below the profile with no link on the way,
  and anything else is refused before an intent is claimed, on every way the runner starts;
* the Connector's code neither reads nor copies the store, and a copy or a read is noticed;
* a run's session state is a throwaway path of its own and the store is not copied there;
* the store, as a snapshot, is the same before and after a run.

No real profile, credential or account: a stand-in runtime and disposable files.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from arena_fakes import expect_guard, load_mutant
from arena_openclaw_support import stand_in_handle
from fake_chat_model import FakeChatModel

from agentnexus_sdk import (
    arena_driver,
    arena_driver_openclaw,
    arena_match,
    arena_runner,
    openclaw_arena,
    runtimes,
)

SOURCE = Path(arena_match.__file__).parent
FILES = ("openclaw_arena.py", "arena_driver_openclaw.py", "openclaw_bridge.py")
POSIX = hasattr(os, "geteuid")


def owned_profile(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a profile the way the Connector lays it out, private to its owner."""
    profile = tmp_path / "profile"
    runtime = profile / "runtime" / "openclaw"
    (runtime / "state").mkdir(parents=True)
    config = runtime / "openclaw.json"
    config.write_text("{}", encoding="utf-8")
    if POSIX:
        profile.chmod(0o700)
    return profile, config, runtime / "state"


@pytest.mark.skipif(not POSIX, reason="owners and modes are a POSIX notion")
def test_a_profile_open_to_other_users_is_refused(tmp_path: Path) -> None:
    """The store lives below the profile: if others can enter the profile, they can reach it."""
    profile, config, state = owned_profile(tmp_path)
    assert openclaw_arena.profile_context_valid(profile, config, state)
    for mode in (0o750, 0o705, 0o755):
        profile.chmod(mode)
        assert not openclaw_arena.profile_context_valid(profile, config, state), oct(mode)


def test_a_state_reached_through_a_link_is_refused(tmp_path: Path) -> None:
    """A link below the profile is an escape, however it is made; nothing is opened to find out."""
    profile, config, state = owned_profile(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    state.rmdir()
    try:
        state.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"links are unavailable on this host: {type(error).__name__}")
    assert not openclaw_arena.profile_context_valid(profile, config, state)


def test_a_traversal_out_of_the_profile_is_refused(tmp_path: Path) -> None:
    """A state named through `..` leaves the profile and is refused."""
    profile, config, _ = owned_profile(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert not openclaw_arena.profile_context_valid(profile, config, profile / ".." / "elsewhere")


def runner_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str) -> str:
    """Start the runner's command the way `action` does and return the refusal's text."""
    profile, config, _ = owned_profile(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    detection = runtimes.RuntimeDetection(
        installed=True, version="2026.9.9", executable=sys.executable
    )
    monkeypatch.setattr(runtimes.OpenClawAdapter, "detect", lambda self: detection)
    context = SimpleNamespace(
        server_name="agentnexus", openclaw_config=config, openclaw_state=outside
    )
    paths = SimpleNamespace(
        isolation="isolated",
        root=profile,
        runtime_context=lambda: context,
        state_file=tmp_path / "missing-state.json",
    )
    constructed: list[str] = []
    monkeypatch.setattr(
        arena_runner.ArenaRunner, "__init__", lambda self, *a, **k: constructed.append("runner")
    )
    with pytest.raises(arena_runner.RunnerRefused) as raised:
        arena_runner.inspected_runtime(paths, "openclaw")
    assert constructed == [], f"a runner was built before the {action} proof"
    return str(raised.value)


@pytest.mark.parametrize("action", ["preflight", "enable", "foreground run", "service run"])
def test_an_escaped_state_is_refused_before_a_runner_exists_on_every_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, action: str
) -> None:
    """Preflight, enable, the foreground run and the service share one gate and one refusal."""
    text = runner_refuses(monkeypatch, tmp_path, action)
    assert text == "Automatic Arena play requires an isolated named OpenClaw profile."


def test_foreground_and_service_launch_the_same_worker_with_the_same_context(
    tmp_path: Path,
) -> None:
    """There is one launch: the service and the foreground differ in who started them, not in it."""
    profile, config, state = owned_profile(tmp_path)
    handle = arena_driver_openclaw.OpenClawRun(("openclaw",), "2026.9.9", config, state, profile)
    driver = arena_driver_openclaw.OpenClawArenaDriver()
    first = driver.launch(handle, tmp_path / "one")
    second = driver.launch(handle, tmp_path / "two")
    for launch in (first, second):
        assert launch.environment[openclaw_arena.PROFILE_STATE_ENV] == str(state)
        assert launch.environment[openclaw_arena.PROFILE_ROOT_ENV] == str(profile)
    assert first.command == second.command


def test_the_runtime_is_given_the_store_in_place_and_the_session_apart(tmp_path: Path) -> None:
    """The ambient state is the profile's own; the run's state is a throwaway path elsewhere."""
    profile, config, state = owned_profile(tmp_path)
    work = tmp_path / "work"
    openclaw_arena.make_world(work)
    environment = openclaw_arena.cli_environment(
        {}, work, work / "arena.json", config.parent,
        profile_root=profile, profile_config=config, profile_state=state,
    )  # fmt: skip
    assert environment["OPENCLAW_STATE_DIR"] == str(state)
    argv = openclaw_arena.exec_arguments([], work / "prompt.txt", 20, work / "arena.json", work)
    session = Path(argv[argv.index("--state-dir") + 1])
    assert session == work / "run" and not session.is_relative_to(state)
    assert not state.is_relative_to(work)


def test_the_store_as_a_snapshot_is_the_same_after_a_run(tmp_path: Path) -> None:
    """Running the stand-in runtime through the driver leaves the store's bytes as they were."""
    model = FakeChatModel("text")
    try:
        handle = stand_in_handle(tmp_path, model)
        store = Path(str(handle.state)) / "state" / "openclaw.sqlite"
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_bytes(b"synthetic store, never rewritten by the Connector")
        before = hashlib.sha256(store.read_bytes()).hexdigest()
        driver = arena_driver_openclaw.OpenClawArenaDriver()
        arena_driver.check_preflight(driver, handle)
        assert hashlib.sha256(store.read_bytes()).hexdigest() == before
    finally:
        model.close()


# ---------------------------------------------------------------------------------------------
# The Connector neither reads nor copies the store
# ---------------------------------------------------------------------------------------------

READS = {"read_text", "read_bytes", "open", "copy", "copy2", "copyfile", "copytree", "move"}


def touches_the_store(source: str) -> list[str]:
    """Return the file-reading and copying calls in a source that mention the profile state."""
    tree = ast.parse(source)
    found = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        text = ast.unparse(function)
        if "profile_state" not in text and "handle.state" not in text:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or "profile_state" not in ast.unparse(node):
                continue
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            else:
                continue
            if name in READS:
                found.add(f"{function.name}:{name}")
    return sorted(found)


@pytest.mark.parametrize("name", FILES)
def test_the_connector_code_does_not_read_or_copy_the_store(name: str) -> None:
    """The store's path is passed on and its metadata looked at; its content is never opened."""
    assert touches_the_store((SOURCE / name).read_text(encoding="utf-8")) == []


def test_code_that_reads_or_copies_the_store_is_noticed() -> None:
    """The static proof fails for code that opens, reads or copies the profile's state."""
    for line in (
        "data = (profile_state / 'x').read_bytes()",
        "shutil.copytree(profile_state, work)",
        "handle = open(profile_state / 'x')",
    ):
        source = f"def f(profile_state, work):\n    {line}\n"
        assert touches_the_store(source), line


def test_a_worker_that_copies_the_store_into_the_throwaway_is_noticed(tmp_path: Path) -> None:
    """Break the worker so it copies the profile state into the run's world: the proof refuses."""
    original = "        state_dir = profile_state\n"
    source = (SOURCE / "openclaw_arena.py").read_text(encoding="utf-8")
    assert source.count(original) == 1, "the guarded line moved; update this proof"

    def oracle(module: ModuleType) -> None:
        text = Path(str(module.__file__)).read_text(encoding="utf-8")
        assert touches_the_store(text) == [], "the worker copies or reads the store"

    (tmp_path / "m").mkdir()
    shutil.copy(arena_match.__file__, tmp_path / "m" / "arena_match.py")
    mutant = load_mutant(
        tmp_path / "m",
        openclaw_arena,
        original,
        original + "        __import__('shutil').copytree(profile_state, work / 'copy')\n",
    )
    expect_guard(oracle, openclaw_arena, mutant)


def test_the_environment_given_to_the_runtime_carries_no_credential_of_the_service() -> None:
    """Only OS essentials and the runtime's own context pass; nothing of the service's secrets."""
    base: dict[str, Any] = {
        "PATH": "/bin",
        "AGENTNEXUS_PRIVATE_KEY_FILE": "/keys/agent.pem",
        "SYNTHETIC_PROVIDER_KEY": "synthetic",
        "OPENCLAW_STATE_DIR": "/elsewhere",
    }
    work = Path("/w")
    environment = openclaw_arena.cli_environment(base, work, work / "arena.json", work)
    assert "AGENTNEXUS_PRIVATE_KEY_FILE" not in environment
    assert "SYNTHETIC_PROVIDER_KEY" not in environment
    assert environment["OPENCLAW_STATE_DIR"] != "/elsewhere"
    assert json.dumps(environment).count("synthetic") == 0
