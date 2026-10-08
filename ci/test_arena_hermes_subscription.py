"""#228: Hermes plays on whatever its own profile is configured with, a subscription included.

The Connector does not know models or providers, so it has no way to refuse a profile for the
provider it uses or to ask for a credential of its own. The Hermes driver lets Hermes' own functions
resolve the profile's provider and credential, in the smallest window that can see them, and holds
everything else to the contract: exactly three operations, the cutoff and cleanup of #223, no
credential copied, logged or passed on, and no profile write that Hermes does not make itself.

A fake subscription transport and a disposable authentication fixture stand for the subscription
backend and its grant. No real credential, account or paid inference is involved.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from arena_boundary import names_in
from arena_fakes import MOVES
from arena_process_harness import (
    SUBSCRIPTION_TOKEN,
    assert_no_residue,
    hermes_stand_in,
    moves_of,
    profile_changes,
    run_process,
    subscription_auth,
)
from fake_subscription_server import FakeSubscription

from agentnexus_sdk import arena_driver, arena_driver_hermes, arena_match, hermes_arena

THREE = sorted(arena_match.TOOLS)
DIGEST = hashlib.sha256(SUBSCRIPTION_TOKEN.encode()).hexdigest()
SOURCE = Path(arena_match.__file__).parent


@pytest.fixture
def transport() -> Any:
    """Provide a fake subscription backend, stopped afterwards."""
    server = FakeSubscription()
    yield server
    server.close()


def subscription(server: FakeSubscription, role: str, **extra: Any) -> dict[str, Any]:
    """Return the behaviour of a Hermes whose profile is a subscription and whose model answers."""
    server.arguments = MOVES[role]
    return {
        "mode": "fast",
        "arguments": MOVES[role],
        "credentials": "subscription",
        "transport": "http",
        "server_url": server.url,
        **extra,
    }


def play(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    behavior: dict[str, Any],
    role: str = "white",
    **options: Any,
) -> Any:
    """Run one real match of a Hermes stand-in against the fake backend."""
    return run_process(monkeypatch, capsys, tmp_path, role, behavior, bound=10.0, **options)


# ---------------------------------------------------------------------------------------------
# The exact preflight proves the subscription path before an intent is claimed
# ---------------------------------------------------------------------------------------------


def handle_of(tmp_path: Path, behavior: dict[str, Any]) -> Any:
    """Return the Hermes handle of a stand-in installation and its disposable profile."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    source, home = hermes_stand_in(tmp_path, behavior)
    return arena_driver_hermes.HermesRun(source, Path(sys.executable), home)


def test_a_subscription_profile_passes_the_exact_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, transport: FakeSubscription
) -> None:
    """The profile is a subscription with no API key anywhere: the three-tool proof still holds."""
    monkeypatch.setattr(arena_driver_hermes.tempfile, "tempdir", str(tmp_path))
    handle = handle_of(tmp_path, subscription(transport, "white"))
    driver = arena_driver_hermes.driver()
    arena_driver.check_preflight(driver, handle)
    assert transport.requests == [], "the preflight made an inference request"


def test_a_profile_whose_grant_is_missing_is_refused_by_the_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, transport: FakeSubscription
) -> None:
    """Before a claim, not after: no seat is left refused by a worker known to be unable to play."""
    monkeypatch.setattr(arena_driver_hermes.tempfile, "tempdir", str(tmp_path))
    handle = handle_of(tmp_path, subscription(transport, "white"))
    arena_driver.check_preflight(arena_driver_hermes.driver(), handle)  # the control passes
    (handle.home / "auth.json").unlink()
    with pytest.raises(arena_driver.DriverRefusedError) as raised:
        arena_driver.check_preflight(arena_driver_hermes.driver(), handle)
    assert raised.value.code == "preflight_refused"
    assert str(raised.value) == "Hermes refused the exact three-tool Arena preflight."


@pytest.mark.parametrize("shape", ["command", "acp_command", "app_server", "acp_url"])
def test_a_transport_that_is_not_a_plain_model_endpoint_is_refused_by_its_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, transport: FakeSubscription, shape: str
) -> None:
    """A resolved command, an app-server runtime or an external-process URL is refused by shape."""
    monkeypatch.setattr(arena_driver_hermes.tempfile, "tempdir", str(tmp_path))
    driver = arena_driver_hermes.driver()
    control = handle_of(tmp_path / "control", subscription(transport, "white"))
    arena_driver.check_preflight(driver, control)  # the same profile without the shape passes
    shaped = handle_of(tmp_path / "shaped", subscription(transport, "white", resolve_shape=shape))
    with pytest.raises(arena_driver.DriverRefusedError):
        arena_driver.check_preflight(driver, shaped)


# ---------------------------------------------------------------------------------------------
# A match on a subscription
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["white", "first"])
def test_a_subscription_decision_makes_exactly_one_request_and_one_legal_move(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
    role: str,
) -> None:
    """One request to the transport with the grant as bearer, three tools, one move forwarded."""
    run = play(monkeypatch, capsys, tmp_path, subscription(transport, role), role)
    assert moves_of(run, role) == [MOVES[role]]
    assert len(transport.requests) == 1, "a closing request was made after the accepted move"
    request = transport.requests[0]
    assert request["path"].endswith("/responses") and request["scheme"] == "Bearer"
    assert request["token_sha256"] == DIGEST, "the grant did not reach the transport"
    assert request["tools"] == THREE
    assert run.runner.terminal and run.runner.playing
    assert_no_residue(run)


@pytest.mark.parametrize("role", ["white", "first"])
def test_two_subscription_turns_make_one_request_each(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
    role: str,
) -> None:
    """No closing iteration follows an accepted move, in either turn."""
    run = play(monkeypatch, capsys, tmp_path, subscription(transport, role), role, turns=2)
    assert moves_of(run, role) == [MOVES[role], MOVES[role]]
    assert len(transport.requests) == 2
    assert_no_residue(run)


def test_a_hanging_subscription_transport_is_ended_at_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """The deadline is the supervisor's: a backend that never answers costs the move only."""
    transport.mode = "hang"
    run = run_process(
        monkeypatch, capsys, tmp_path, "white", subscription(transport, "white"), bound=1.5
    )
    assert run.forwarded == []
    assert run.events.count("decision_budget_expired") == 1
    assert len(transport.requests) == 1
    assert_no_residue(run)


# ---------------------------------------------------------------------------------------------
# The grant stays where Hermes keeps it
# ---------------------------------------------------------------------------------------------


def test_the_grant_reaches_the_transport_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """No copy, log line, environment variable, diagnostic or file of the throwaway home."""
    environment = tmp_path / "worker-environment.stand-in-record"
    listing = tmp_path / "throwaway-home.stand-in-record"
    behavior = subscription(
        transport,
        "white",
        environment=str(environment),
        home_listing=str(listing),
        token=SUBSCRIPTION_TOKEN,
    )
    run = play(monkeypatch, capsys, tmp_path, behavior)
    assert moves_of(run, "white") == [MOVES["white"]]
    assert SUBSCRIPTION_TOKEN not in run.raw, "the grant reached the service log"
    assert SUBSCRIPTION_TOKEN not in environment.read_text(encoding="utf-8")
    held = [json.loads(line) for line in listing.read_text(encoding="utf-8").splitlines()]
    assert held and all(not item["holds_token"] for item in held)
    assert all("auth.json" not in item["files"] for item in held), "the auth store was copied"
    # The profile's own store is byte for byte as it was, and the lock Hermes keeps already existed.
    assert (run.profile / "auth.json").read_text(encoding="utf-8") == subscription_auth()
    assert_no_residue(run)


def test_the_worker_starts_with_neither_the_real_home_nor_the_other_runtimes_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """Hermes may adopt another tool's login that it finds in the home directory: it finds none."""
    environment = tmp_path / "worker-environment.stand-in-record"
    run = play(
        monkeypatch,
        capsys,
        tmp_path,
        subscription(transport, "white", environment=str(environment)),
    )
    seen = json.loads(environment.read_text(encoding="utf-8").splitlines()[0])
    scratch = Path(run.homes[0])
    for name in ("HOME", "USERPROFILE"):
        assert Path(seen[name]).parent == scratch or Path(seen[name]) == scratch, name
    assert "CODEX_HOME" not in seen
    assert_no_residue(run)


def test_without_a_lock_the_only_write_into_the_profile_is_hermes_own_lock(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """The one thing the window lets Hermes write is its credential store's lock, nothing else."""
    run = play(monkeypatch, capsys, tmp_path, subscription(transport, "white", lock=False))
    assert profile_changes(run.profile_before, run.profile_after) == ["added auth.lock"]
    assert_no_residue(run, frozenset({"auth.lock"}))


def test_an_api_key_profile_is_still_resolved_by_hermes_without_touching_the_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The earlier route stays: Hermes reads the profile's .env itself and writes nothing."""
    behavior = {"mode": "fast", "arguments": MOVES["white"]}
    run = play(monkeypatch, capsys, tmp_path, behavior)
    assert moves_of(run, "white") == [MOVES["white"]]
    assert profile_changes(run.profile_before, run.profile_after) == []
    assert_no_residue(run)


# ---------------------------------------------------------------------------------------------
# A match stays on what it started with; credentials follow the runtime
# ---------------------------------------------------------------------------------------------

OTHER = "model:\n  provider: openai-codex\n  default: synthetic-other-model\n"


def test_a_match_is_pinned_to_the_model_it_started_with_even_when_its_worker_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """The owner changes the model during the match; the replacement worker plays the old one."""
    agents = tmp_path / "agents.stand-in-record"
    behavior = subscription(
        transport,
        "white",
        close="hang",
        agents=str(agents),
        owner_changes={"profile": str(tmp_path / "home"), "config": OTHER},
    )
    run = play(monkeypatch, capsys, tmp_path, behavior, turns=2)
    models = [json.loads(line)["model"] for line in agents.read_text(encoding="utf-8").splitlines()]
    assert models == ["synthetic-subscription-model"] * 2, models
    assert moves_of(run, "white") == [MOVES["white"]] * 2
    assert run.events.count("decision_cleanup_expired") == 2


def test_the_credential_is_resolved_again_for_each_decision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """A grant Hermes rotates between two decisions is the one the second decision presents."""
    rotated = "synthetic-subscription-rotated-token-0002"
    behavior = subscription(
        transport,
        "white",
        owner_changes={
            "profile": str(tmp_path / "home"),
            "config": "model:\n  provider: openai-codex\n  default: synthetic-subscription-model\n"
            f"  base_url: {transport.url}\n  context_length: 272000\n",
            "grant": subscription_auth(rotated),
        },
    )
    play(monkeypatch, capsys, tmp_path, behavior, turns=2)
    digests = [request["token_sha256"] for request in transport.requests]
    assert digests == [DIGEST, hashlib.sha256(rotated.encode()).hexdigest()]


# ---------------------------------------------------------------------------------------------
# The Hermes driver names no model and no provider either
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["hermes_arena.py", "arena_driver_hermes.py"])
def test_the_hermes_driver_contains_no_model_or_provider_name(name: str) -> None:
    """A runtime may know its own provider; the Connector that drives it does not."""
    assert names_in(SOURCE / name) == [], f"{name} names a model or a provider"


def test_the_worker_reads_no_credential_file_itself() -> None:
    """Hermes reads its profile's secrets through its own scope: the worker opens no .env."""
    text = (SOURCE / "hermes_arena.py").read_text(encoding="utf-8")
    assert "dotenv" not in text
    assert not re.search(r"\.env(?![A-Za-z_])", text), "the worker names a secrets file"


def test_the_environment_given_to_hermes_has_no_real_home_and_no_other_logins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HOME and USERPROFILE are the throwaway's; no CODEX-style home variable is passed on."""
    monkeypatch.setenv("HOME", str(tmp_path / "real"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "real" / ".codex"))
    scratch = tmp_path / "scratch"
    environment = arena_driver_hermes.hermes_environment(tmp_path / "profile", scratch)
    assert environment["HOME"] == environment["USERPROFILE"] == str(scratch / "home")
    assert "CODEX_HOME" not in environment
    assert hermes_arena.PROFILE_ENV in environment
