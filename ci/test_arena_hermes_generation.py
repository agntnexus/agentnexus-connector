"""#228: Hermes' generation, and a model switch between matches through the real preflight.

The Hermes driver names a generation made of metadata alone - modification time and size of the
profile's model configuration and secrets file, and of the installation's revision markers - so that
a change of what Hermes plays with is seen without reading a file that may hold a credential. These
tests drive the Arena service's claim gate with the real Hermes driver and worker preflight against
the stand-in Hermes: a switch between two supported configurations is proven and claimed, a
switch to one Hermes cannot authenticate is refused before any seat is claimed, and neither
touches the profile.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from arena_fakes import expect_guard, intent, load_mutant
from arena_process_harness import (
    hermes_stand_in,
    profile_changes,
    snapshot,
    subscription_auth,
)
from fake_subscription_server import FakeSubscription

from agentnexus_sdk import arena_driver_hermes, arena_runner

OTHER_MODEL = "synthetic-other-model"


class StandInHermes(arena_driver_hermes.HermesArenaDriver):
    """The Hermes driver, with `inspect` finding the stand-in installation instead of a real one."""

    def __init__(self, source: Path, home: Path) -> None:
        """Remember where the stand-in installation and its profile are, and count inspections."""
        self.source, self.home = source, home
        self.inspected = 0

    def inspect(self, paths: Any) -> arena_driver_hermes.HermesRun:
        """Return a handle for the stand-in; the real inspection needs a real checkout."""
        del paths
        self.inspected += 1
        return arena_driver_hermes.HermesRun(self.source, Path(sys.executable), self.home)


@pytest.fixture
def backend() -> Any:
    """Provide a fake subscription backend, stopped afterwards."""
    server = FakeSubscription()
    yield server
    server.close()


def write_config(home: Path, model: str, url: str) -> None:
    """Write a subscription profile's model section, as an owner's model switch does."""
    (home / "config.yaml").write_text(
        f"model:\n  provider: openai-codex\n  default: {model}\n  base_url: {url}\n"
        "  context_length: 272000\n",
        encoding="utf-8",
    )


def generation_oracle(module: ModuleType, tmp_path: Path, url: str) -> None:
    """Require the metadata-only generation to see a model or secrets change and no refresh."""
    source, home = hermes_stand_in(
        tmp_path, {"mode": "fast", "credentials": "subscription", "server_url": url}
    )
    driver = StandInHermes(source, home)
    handle = driver.inspect(None)

    def generation() -> str | None:
        return module.HermesArenaDriver.generation(driver, handle)  # type: ignore[no-any-return]

    first = generation()
    assert first is not None and re.fullmatch(r"[0-9a-f]{32}", first)
    assert generation() == first, "the generation is not stable"
    (home / "auth.json").write_text(subscription_auth("rotated-by-a-refresh"), encoding="utf-8")
    (home / "auth.lock").write_text("", encoding="utf-8")
    assert generation() == first, "a credential refresh looks like a new runtime"
    write_config(home, OTHER_MODEL, url)
    changed = generation()
    assert changed != first, "a model switch is not seen"
    (home / ".env").write_text("SYNTHETIC_KEY=synthetic-disposable-key-2\n", encoding="utf-8")
    assert generation() not in {first, changed}, "a secrets change is not seen"
    assert OTHER_MODEL not in str(changed), "the token is not opaque"


def test_the_generation_changes_with_the_model_and_the_secrets_but_not_with_the_credential_store(
    tmp_path: Path, backend: FakeSubscription
) -> None:
    """Watch the configuration and the secrets file, not the store Hermes rewrites on refresh."""
    generation_oracle(arena_driver_hermes, tmp_path, backend.url)


def reads_a_file(module: ModuleType) -> list[str]:
    """Return the file-reading calls made inside the driver's `generation`."""
    tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "generation"
    )
    return sorted(
        {
            node.func.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"read_text", "read_bytes", "open", "load", "loads"}
        }
    )


def test_the_generation_reads_no_file_that_may_hold_a_credential() -> None:
    """Stat only: a configuration or secrets file is never opened to make a token."""
    assert reads_a_file(arena_driver_hermes) == []


#: Name -> (line of the Hermes driver, what it is replaced by, the proof that must then fail).
MUTATIONS: dict[str, tuple[str, str]] = {
    "the-secrets-file-is-not-watched": ('            ("secrets", handle.home / ".env"),\n', ""),
    "the-credential-store-is-watched": (
        '            ("project", handle.source / "pyproject.toml"),\n',
        '            ("project", handle.source / "pyproject.toml"),\n'
        '            ("store", handle.home / "auth.json"),\n',
    ),
    "the-model-configuration-is-not-watched": (
        '            ("model", handle.home / "config.yaml"),\n',
        "",
    ),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_a_broken_generation_is_noticed(
    name: str, tmp_path: Path, backend: FakeSubscription
) -> None:
    """The proof holds for the real driver and fails for the driver with this one line broken."""
    original, replacement = MUTATIONS[name]
    mutant = load_mutant(tmp_path / "mutant", arena_driver_hermes, original, replacement)
    expect_guard(
        lambda module: generation_oracle(module, tmp_path / module.__name__, backend.url),
        arena_driver_hermes,
        mutant,
    )


def test_a_generation_that_reads_a_file_is_noticed(tmp_path: Path) -> None:
    """The static proof fails for a driver that reads the configuration to make its token."""
    mutant = load_mutant(
        tmp_path / "mutant",
        arena_driver_hermes,
        'marker = f"{info.st_mtime_ns}:{info.st_size}"',
        'marker = path.read_text(encoding="utf-8")',
    )
    try:
        assert reads_a_file(mutant) == ["read_text"]
    finally:
        sys.modules.pop(mutant.__name__, None)


class Service:
    """The Arena service's claim gate with the real Hermes driver, over a stand-in installation."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
        """Build a runner on the stand-in profile, proven as the command proves before a run."""
        monkeypatch.setattr(arena_driver_hermes.tempfile, "tempdir", str(tmp_path))
        source, self.home = hermes_stand_in(
            tmp_path, {"mode": "fast", "credentials": "subscription", "server_url": url}
        )
        self.driver = StandInHermes(source, self.home)
        self.agent = str(uuid.uuid4())
        self.claims: list[str] = []
        runner = self.runner = object.__new__(arena_runner.ArenaRunner)
        runner.config = SimpleNamespace(agent_id=self.agent)
        runner.journal = arena_runner.RunJournal(tmp_path / "journal.sqlite3")
        runner.paths = SimpleNamespace(root=tmp_path / "service")
        runner.driver, runner.handle = self.driver, self.driver.inspect(None)
        runner.active = None
        runner.begin_proof()
        self.inspected_at_start = self.driver.inspected
        self.documents = [intent(self.agent)]
        monkeypatch.setattr(
            runner,
            "_post",
            lambda path, payload: {"intents": self.documents} if path == "/poll" else {},
        )
        monkeypatch.setattr(runner, "_launch", lambda queued: self.claims.append("claim"))

    def status(self) -> dict[str, Any]:
        """Return the status file as `arena status` shows it."""
        return arena_runner.read_status(self.runner.paths.root / "arena" / "status.json")


def test_a_switch_between_two_supported_configurations_is_proven_and_then_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: FakeSubscription
) -> None:
    """Idle: the unchanged profile is reused, the switched one is proven by the real preflight."""
    service = Service(tmp_path, monkeypatch, backend.url)
    before = snapshot(service.home)
    service.runner.tick()
    assert service.claims == ["claim"] and service.driver.inspected == service.inspected_at_start
    write_config(service.home, OTHER_MODEL, backend.url)
    after_edit = snapshot(service.home)
    service.runner.tick()
    assert service.claims == ["claim", "claim"]
    assert service.driver.inspected == service.inspected_at_start + 1, "no new inspection"
    assert service.status()["preflight"] == "passed" and service.status()["changed"] is False
    # Proving a switch writes nothing into the profile that the switch did not.
    assert profile_changes(after_edit, snapshot(service.home)) == []
    assert profile_changes(before, after_edit) == ["changed config.yaml"]
    assert backend.requests == [], "proving a configuration made an inference request"
    service.runner.journal.close()


def test_a_switch_to_a_configuration_hermes_cannot_authenticate_is_refused_before_any_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: FakeSubscription
) -> None:
    """The owner signs out and switches: nothing is claimed, nothing retried, and it says why."""
    service = Service(tmp_path, monkeypatch, backend.url)
    os.remove(service.home / "auth.json")
    write_config(service.home, OTHER_MODEL, backend.url)
    for _ in range(3):
        service.runner.tick()
        service.runner.maintain()
    assert service.claims == [], "a seat was claimed on a runtime that cannot play"
    assert service.driver.inspected == service.inspected_at_start + 1, "the refusal was retried"
    status = service.status()
    assert status["preflight"] == "refused" and status["refusal"] == "preflight_refused"
    assert "Hermes refused" not in str(status), "a sentence reached the status"
    # Signing in again is a new generation: proven once, then claimed.
    (service.home / "auth.json").write_text(subscription_auth(), encoding="utf-8")
    write_config(service.home, "synthetic-repaired-model", backend.url)
    service.runner.tick()
    assert service.claims == ["claim"]
    assert service.status()["preflight"] == "passed"
    service.runner.journal.close()
