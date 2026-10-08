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

import contextlib
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from arena_boundary import names_a_secrets_file, names_in
from arena_fakes import MOVES, expect_guard, load_mutant
from arena_process_harness import (
    SUBSCRIPTION_TOKEN,
    assert_no_residue,
    hermes_stand_in,
    moves_of,
    mutated_programs,
    profile_changes,
    run_process,
    subscription_auth,
)
from fake_subscription_server import FakeSubscription

from agentnexus_sdk import arena_driver, arena_driver_hermes, arena_match, hermes_arena

THREE = sorted(arena_match.TOOLS)
DIGEST = hashlib.sha256(SUBSCRIPTION_TOKEN.encode()).hexdigest()
SOURCE = Path(arena_match.__file__).parent


SHAPES = ("command", "acp_command", "app_server", "acp_url")


@contextlib.contextmanager
def backend(mode: str = "move") -> Iterator[FakeSubscription]:
    """Provide a fake subscription backend, stopped afterwards."""
    server = FakeSubscription(mode)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def transport() -> Iterator[FakeSubscription]:
    """Provide a fake subscription backend to a test, stopped afterwards."""
    with backend() as server:
        yield server


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


@pytest.mark.parametrize("shape", SHAPES)
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


def records_of(path: Path) -> list[dict[str, Any]]:
    """Return the JSON lines a stand-in wrote, or none when it never got as far as writing."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def text_of(path: Path) -> str:
    """Return a stand-in record as text, empty when it was never written."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def check_grant_flow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    server: FakeSubscription,
    **programs: Any,
) -> None:
    """No copy, log line, environment variable, diagnostic or file of the throwaway home."""
    environment = tmp_path / "worker-environment.stand-in-record"
    listing = tmp_path / "throwaway-home.stand-in-record"
    agents = tmp_path / "agents.stand-in-record"
    behavior = subscription(
        server,
        "white",
        environment=str(environment),
        home_listing=str(listing),
        agents=str(agents),
        token=SUBSCRIPTION_TOKEN,
    )
    run = play(monkeypatch, capsys, tmp_path, behavior, **programs)
    assert moves_of(run, "white") == [MOVES["white"]]
    assert SUBSCRIPTION_TOKEN not in run.raw, "the grant reached the service log"
    assert SUBSCRIPTION_TOKEN not in text_of(environment), "the grant reached an environment"
    held = records_of(listing)
    assert held and all(not item["holds_token"] for item in held), "the grant was written down"
    assert all("auth.json" not in item["files"] for item in held), "the auth store was copied"
    # The profile's own store is byte for byte as it was, and the lock Hermes keeps already existed.
    assert (run.profile / "auth.json").read_text(encoding="utf-8") == subscription_auth()
    assert_no_residue(run)


def test_the_grant_reaches_the_transport_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """The grant is the transport's bearer and is nowhere else."""
    check_grant_flow(monkeypatch, capsys, tmp_path, transport)


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


def check_api_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    server: FakeSubscription,
    **programs: Any,
) -> None:
    """Require a key provider to resolve in the throwaway home, the profile never a home."""
    del server
    behavior = {"mode": "fast", "arguments": MOVES["white"]}
    run = play(monkeypatch, capsys, tmp_path, behavior, **programs)
    assert moves_of(run, "white") == [MOVES["white"]]
    assert profile_changes(run.profile_before, run.profile_after) == [], "the window was opened"
    assert_no_residue(run)


def test_an_api_key_profile_is_still_resolved_by_hermes_without_touching_the_profile(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """The earlier route stays: Hermes reads the profile's secrets itself and writes nothing."""
    check_api_key(monkeypatch, capsys, tmp_path, transport)


# ---------------------------------------------------------------------------------------------
# A match stays on what it started with; credentials follow the runtime
# ---------------------------------------------------------------------------------------------

OTHER = "model:\n  provider: openai-codex\n  default: synthetic-other-model\n"


def check_pinned(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    server: FakeSubscription,
    **programs: Any,
) -> None:
    """Change the model during the match: the replacement worker still plays the old one."""
    agents = tmp_path / "agents.stand-in-record"
    behavior = subscription(
        server,
        "white",
        close="hang",
        agents=str(agents),
        owner_changes={"profile": str(tmp_path / "home"), "config": OTHER},
    )
    run = play(monkeypatch, capsys, tmp_path, behavior, turns=2, **programs)
    models = [record["model"] for record in records_of(agents)]
    assert models == ["synthetic-subscription-model"] * 2, models
    assert moves_of(run, "white") == [MOVES["white"]] * 2
    assert run.events.count("decision_cleanup_expired") == 2


def test_a_match_is_pinned_to_the_model_it_started_with_even_when_its_worker_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """A change of the profile applies from the next match, never inside this one."""
    check_pinned(monkeypatch, capsys, tmp_path, transport)


def check_rotation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    server: FakeSubscription,
    **programs: Any,
) -> None:
    """Rotate the grant between two decisions: the second decision presents the new one."""
    rotated = "synthetic-subscription-rotated-token-0002"
    behavior = subscription(
        server,
        "white",
        owner_changes={
            "profile": str(tmp_path / "home"),
            "config": "model:\n  provider: openai-codex\n  default: synthetic-subscription-model\n"
            f"  base_url: {server.url}\n  context_length: 272000\n",
            "grant": subscription_auth(rotated),
        },
    )
    play(monkeypatch, capsys, tmp_path, behavior, turns=2, **programs)
    digests = [request["token_sha256"] for request in server.requests]
    assert digests == [DIGEST, hashlib.sha256(rotated.encode()).hexdigest()]


def test_the_credential_is_resolved_again_for_each_decision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """Hermes is asked again before each decision, so an expiring grant is refreshed in time."""
    check_rotation(monkeypatch, capsys, tmp_path, transport)


def check_agent_build(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    server: FakeSubscription,
    **programs: Any,
) -> None:
    """Hermes is built in the throwaway home again, and with nothing of the profile's store."""
    agents = tmp_path / "agents.stand-in-record"
    run = play(
        monkeypatch,
        capsys,
        tmp_path,
        subscription(server, "white", agents=str(agents)),
        turns=2,
        **programs,
    )
    records = records_of(agents)
    assert len(records) == 2, "the decisions were not both made"
    for record in records:
        assert record["home"] == record["resolved_home"] == run.homes[0], "the window stayed open"
        assert "credential_pool" not in record["extra"], "the profile's pool reached the agent"


def test_the_profile_window_is_closed_and_no_pool_is_handed_to_the_agent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """The window is shut again before the agent exists, and the pool never leaves the resolver."""
    check_agent_build(monkeypatch, capsys, tmp_path, transport)


def check_ready(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    server: FakeSubscription,
    **programs: Any,
) -> None:
    """Refuse the start where it is cheap: no decision is made and no request is sent."""
    behavior = subscription(
        server, "white", grant=False, agents=str(tmp_path / "agents.stand-in-record")
    )
    run = play(monkeypatch, capsys, tmp_path, behavior, wait=15.0, **programs)
    assert run.forwarded == [] and server.requests == []
    assert not (tmp_path / "agents.stand-in-record").exists(), "an agent was built without a grant"
    # The worker ends before it says ready: no decision was ever handed to it.
    assert run.events == ["run_started", "runtime_exception", "child_nonzero_exit", "run_stopped"]


def test_a_profile_whose_grant_is_missing_never_gets_a_worker_that_says_ready(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """A worker that cannot authenticate does not join, so no claimed seat is left to refuse."""
    check_ready(monkeypatch, capsys, tmp_path, transport)


def preflight_tools(handle: Any, worker: Path | None = None) -> frozenset[str] | None:
    """Run the worker's own preflight, the real one or a mutated copy, as the driver would."""
    scratch = handle.home.parent / "preflight-scratch"
    (scratch / "home").mkdir(parents=True)
    command = handle.command("--preflight")
    if worker is not None:
        command[2] = str(worker)
    probe = subprocess.run(  # noqa: S603 - the interpreter and a test-owned program
        command,
        env=arena_driver_hermes.hermes_environment(handle.home, scratch),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return arena_driver.parse_preflight(probe.stdout) if probe.returncode == 0 else None


def check_preflight(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    server: FakeSubscription,
    **programs: Any,
) -> None:
    """Pass a subscription profile, and refuse what could not play or would run its own program."""
    del monkeypatch, capsys
    worker = programs.get("worker")
    passing = handle_of(tmp_path / "passing", subscription(server, "white"))
    assert preflight_tools(passing, worker) == arena_match.TOOLS, "a subscription was refused"
    ungranted = handle_of(tmp_path / "ungranted", subscription(server, "white", grant=False))
    assert preflight_tools(ungranted, worker) is None, "a profile without a grant passed"
    for shape in SHAPES:
        shaped = handle_of(tmp_path / shape, subscription(server, "white", resolve_shape=shape))
        assert preflight_tools(shaped, worker) is None, f"a {shape} transport passed"
    assert server.requests == [], "the preflight made an inference request"


def test_the_worker_preflight_accepts_what_can_play_and_refuses_what_cannot_or_would_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    transport: FakeSubscription,
) -> None:
    """One proof for the subscription, the missing grant and the four shapes, on the real worker."""
    check_preflight(monkeypatch, capsys, tmp_path, transport)


# ---------------------------------------------------------------------------------------------
# The Hermes driver names no model and no provider either
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["hermes_arena.py", "arena_driver_hermes.py"])
def test_the_hermes_driver_contains_no_model_or_provider_name(name: str) -> None:
    """A runtime may know its own provider; the Connector that drives it does not."""
    assert names_in(SOURCE / name) == [], f"{name} names a model or a provider"


def test_the_worker_reads_no_credential_file_itself() -> None:
    """Hermes reads its profile's secrets through its own scope: the worker opens no such file."""
    text = (SOURCE / "hermes_arena.py").read_text(encoding="utf-8")
    assert not names_a_secrets_file(text), "the worker names a secrets file"


def environment_oracle(module: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Require the throwaway as HOME and USERPROFILE and no other tool's login variable."""
    monkeypatch.setenv("HOME", str(tmp_path / "real"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "real"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "real" / ".codex"))
    scratch = tmp_path / "scratch"
    environment = module.hermes_environment(tmp_path / "profile", scratch)
    assert environment.get("HOME") == environment.get("USERPROFILE") == str(scratch / "home")
    assert "CODEX_HOME" not in environment
    assert hermes_arena.PROFILE_ENV in environment


def test_the_environment_given_to_hermes_has_no_real_home_and_no_other_logins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes may adopt another tool's login from the user's home: it is not shown that home."""
    environment_oracle(arena_driver_hermes, monkeypatch, tmp_path)


# ---------------------------------------------------------------------------------------------
# Mutation proofs: break each guard on purpose and require the proof to refuse
# ---------------------------------------------------------------------------------------------

#: Name -> (line of the worker, what it is replaced by, the proof that must then fail).
WORKER_MUTATIONS: dict[
    str, tuple[str | tuple[str, ...], str | tuple[str, ...], Callable[..., None]]
] = {
    "window-leaves-the-process-home-open": (
        '            os.environ["HERMES_HOME"] = saved\n',
        "            pass\n",
        check_agent_build,
    ),
    "window-leaves-the-context-override-open": (
        "            constants.reset_hermes_home_override(override)\n",
        "            pass\n",
        check_agent_build,
    ),
    "the-profile-is-always-opened-as-a-home": (
        "        credentials = ask()\n    except refused:\n",
        "        ask()\n        credentials = None\n    except refused:\n",
        check_api_key,
    ),
    "the-resolvers-pool-is-handed-on": (
        (
            '    credentials.pop("credential_pool", None)\n',
            '                requested_provider=credentials.get("requested_provider"),\n',
        ),
        (
            "    pass\n",
            '                requested_provider=credentials.get("requested_provider"),\n'
            '                credential_pool=credentials.get("credential_pool"),\n',
        ),
        check_agent_build,
    ),
    "the-model-is-not-pinned": (
        "    if pin.is_file():\n",
        "    if False:\n",
        check_pinned,
    ),
    "the-credential-is-resolved-once": (
        "            credentials = resolve_credentials(model)\n",
        "            credentials = resolve_credentials.__dict__.setdefault(\n"
        '                "first", resolve_credentials(model)\n'
        "            )\n",
        check_rotation,
    ),
    "ready-is-said-before-the-credential-resolves": (
        '        resolve_credentials(model)\n        send({"ready": True})\n',
        '        send({"ready": True})\n',
        check_ready,
    ),
    "the-preflight-does-not-resolve-the-credential": (
        "        resolve_credentials(model)\n    sys.stdout.write(",
        "        pass\n    sys.stdout.write(",
        check_preflight,
    ),
    "the-shape-of-the-transport-is-not-checked": (
        "    refuse_external_transport(credentials)\n",
        "    pass\n",
        check_preflight,
    ),
    "a-provider-allowlist-comes-back": (
        '    safe["model"] = pinned_model()\n',
        '    safe["model"] = pinned_model()\n'
        '    if safe["model"].get("provider", "x") not in {"x"}:\n'
        '        raise ValueError("This provider has not passed Arena review.")\n',
        check_preflight,
    ),
    "the-grant-is-put-in-an-environment-variable": (
        '    credentials.pop("credential_pool", None)\n    return credentials\n',
        '    credentials.pop("credential_pool", None)\n'
        '    os.environ["LEAKED_GRANT"] = str(credentials.get("api_key"))\n'
        "    return credentials\n",
        check_grant_flow,
    ),
    "the-grant-is-written-into-the-throwaway-home": (
        '    credentials.pop("credential_pool", None)\n    return credentials\n',
        '    credentials.pop("credential_pool", None)\n'
        '    (Path(os.environ["HERMES_HOME"]) / "copy.txt").write_text(\n'
        '        str(credentials.get("api_key")), encoding="utf-8"\n'
        "    )\n"
        "    return credentials\n",
        check_grant_flow,
    ),
}


@pytest.mark.parametrize("name", sorted(WORKER_MUTATIONS))
def test_a_broken_worker_guard_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    name: str,
) -> None:
    """The proof holds for the real worker, and fails for the worker with this one guard broken."""
    original, replacement, check = WORKER_MUTATIONS[name]
    originals = (original,) if isinstance(original, str) else original
    replacements = (replacement,) if isinstance(replacement, str) else replacement
    (tmp_path / "control").mkdir()
    with backend() as server:
        check(monkeypatch, capsys, tmp_path / "control", server)
    broken_match, broken_worker = mutated_programs(
        tmp_path / "broken", worker=tuple(zip(originals, replacements, strict=True))
    )
    (tmp_path / "mutant").mkdir()
    with backend() as server, pytest.raises(AssertionError):
        check(
            monkeypatch,
            capsys,
            tmp_path / "mutant",
            server,
            arena=broken_match,
            worker=broken_worker,
        )


#: Name -> (line of the driver, what it is replaced by).
DRIVER_MUTATIONS: dict[str, tuple[str, str]] = {
    "home-is-passed-through": (
        '        HOME=str(scratch / "home"),\n',
        '        HOME=os.environ.get("HOME", ""),\n',
    ),
    "userprofile-is-passed-through": (
        '        USERPROFILE=str(scratch / "home"),\n',
        '        USERPROFILE=os.environ.get("USERPROFILE", ""),\n',
    ),
    "another-tools-home-variable-is-passed-on": (
        '        "WINDIR",\n',
        '        "WINDIR",\n        "CODEX_HOME",\n',
    ),
}


@pytest.mark.parametrize("name", sorted(DRIVER_MUTATIONS))
def test_a_broken_environment_guard_is_noticed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    """The environment proof fails for a driver that passes the user's home or another login on."""
    original, replacement = DRIVER_MUTATIONS[name]
    mutant = load_mutant(tmp_path / "mutant", arena_driver_hermes, original, replacement)
    expect_guard(
        lambda module: environment_oracle(module, monkeypatch, tmp_path),
        arena_driver_hermes,
        mutant,
    )


def test_a_worker_that_reads_a_secrets_file_or_names_a_provider_is_noticed(tmp_path: Path) -> None:
    """The two static proofs fail for a worker that does what they forbid."""
    text = (SOURCE / "hermes_arena.py").read_text(encoding="utf-8")
    assert not names_a_secrets_file(text) and names_in(SOURCE / "hermes_arena.py") == []
    for addition in ("import dotenv\n", 'SECRETS = ".env"\n'):
        assert names_a_secrets_file(text + "\n" + addition)
    mutated = tmp_path / "hermes_arena.py"
    mutated.write_text(text + '\nif provider == "openai":\n    pass\n', encoding="utf-8")
    assert names_in(mutated) == ["openai"]
