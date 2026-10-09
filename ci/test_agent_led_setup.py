"""The bounded machine-readable plan used by the official onboarding skill (#230)."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

from agentnexus_sdk import connector as connector_module
from agentnexus_sdk.connector import (
    ConnectorError,
    Endpoints,
    Environment,
    Paths,
    State,
    build_setup_plan,
    main,
    run_setup,
)
from agentnexus_sdk.runtimes import HermesAdapter


def _environment() -> Environment:
    return Environment(stdout=io.StringIO(), stderr=io.StringIO(), system="Linux")


class _HermesProfileProbe:
    """A read-only Hermes profile-list/config-path fixture for plan tests."""

    def __init__(self, tmp_path: Path, profiles: tuple[str, ...], active: str | None) -> None:
        self.home = tmp_path / "runtime-owned-hermes-home"
        self.profiles = set(profiles)
        self.active = active
        self.calls: list[tuple[str, ...]] = []
        self.call_environments: list[dict[str, str] | None] = []

    def which(self, name: str) -> str | None:
        return "hermes-test-executable" if name == "hermes" else None

    def __call__(self, arguments: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        args = tuple(arguments)
        self.calls.append(args)
        env = options.get("env")
        self.call_environments.append(env if isinstance(env, dict) else None)
        if args[1:] == ("profile", "list"):
            rows = ["Profile"]
            rows.extend(f"{'*' if name == self.active else ' '} {name}" for name in sorted(self.profiles))
            return subprocess.CompletedProcess(args, 0, "\n".join(rows), "")
        if args[-2:] == ("config", "path"):
            target = args[args.index("-p") + 1] if "-p" in args else "shared"
            path = (
                self.home / "profiles" / target / "config.yaml"
                if target != "shared"
                else self.home / "config.yaml"
            )
            return subprocess.CompletedProcess(args, 0, str(path), "")
        if args[1:3] == ("profile", "create"):
            self.profiles.add(args[3])
        return subprocess.CompletedProcess(args, 0, "", "")


def _hermes_environment(
    tmp_path: Path, *, profiles: tuple[str, ...], active: str | None
) -> tuple[Environment, _HermesProfileProbe]:
    probe = _HermesProfileProbe(tmp_path, profiles, active)
    return (
        Environment(
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            which=probe.which,
            run=probe,
            system="Linux",
        ),
        probe,
    )


def test_a_new_profile_needs_protected_invitation_input_and_never_prints_a_secret(
    tmp_path: Path,
) -> None:
    """A new identity requires the existing protected prompt and leaks no local detail."""
    environment, _ = _hermes_environment(tmp_path, profiles=(), active=None)
    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["identity"] == "needs_invitation"
    assert plan["invitation_input"] == "protected_prompt"
    assert plan["target_profile"]["disposition"] == "create"
    assert plan["arena"] == "not_selected"
    rendered = json.dumps(plan, sort_keys=True)
    assert "--invitation" not in rendered
    assert "AGENTNEXUS_ONBOARDING_INVITATION" not in rendered
    assert str(tmp_path) not in rendered


def test_an_existing_matching_identity_is_resumed_without_an_invitation(tmp_path: Path) -> None:
    """A matching profile resumes without another claim or invitation."""
    paths = Paths.for_profile(tmp_path, "scout01")
    paths.root.mkdir(parents=True)
    State(agent_id="agent-1", key_id="key-1", handle="scout-01").save(paths.state_file)
    environment, _ = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="operator-profile"
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum_arena",
        environment=environment,
    )

    assert plan["identity"] == "resume"
    assert plan["target_profile"]["disposition"] == "resume"
    assert plan["invitation_input"] == "not_needed"
    assert plan["arena"] == "unsupported_dependency"
    assert plan["ready"] is False


def test_an_existing_identity_with_the_wrong_handle_is_refused(tmp_path: Path) -> None:
    """The approved handle may not silently reuse another identity's profile."""
    paths = Paths.for_profile(tmp_path, "scout01")
    paths.root.mkdir(parents=True)
    State(agent_id="agent-1", key_id="key-1", handle="somebody-else").save(paths.state_file)
    environment, _ = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="operator-profile"
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["identity"] == "refused_handle_mismatch"
    assert plan["target_profile"]["disposition"] == "refused"
    assert plan["changes"] == []
    assert plan["ready"] is False


def test_cli_plan_is_read_only_json_and_needs_no_installation_path_hunt(tmp_path: Path) -> None:
    """The installed command can expose the bounded plan without creating a profile directory."""
    environment, _ = _hermes_environment(
        tmp_path, profiles=("operator-profile",), active="operator-profile"
    )
    result = main(
        [
            "setup",
            "--install-root",
            str(tmp_path),
            "--plan",
            "--profile",
            "scout01",
            "--handle",
            "scout-01",
            "--runtime",
            "hermes",
            "--setup-scope",
            "forum",
        ],
        environment,
    )

    assert result == 0
    assert json.loads(environment.stdout.getvalue())["identity"] == "needs_invitation"
    assert json.loads(environment.stdout.getvalue())["schema_version"] == 2
    assert json.loads(environment.stdout.getvalue())["target_profile"]["disposition"] == "create"
    assert not (tmp_path / "profiles").exists()


def test_machine_status_exposes_no_key_or_path_and_never_guesses_ready(tmp_path: Path) -> None:
    """A saved config is resumable evidence, not proof that its runtime service is ready."""
    paths = Paths.for_profile(tmp_path, "scout01")
    paths.root.mkdir(parents=True)
    State(agent_id="agent-1", key_id="key-1", handle="scout-01").save(paths.state_file)
    environment = _environment()

    result = main(
        ["profile", "status", "--install-root", str(tmp_path), "--profile", "scout01", "--json"],
        environment,
    )

    assert result == 0
    status = json.loads(environment.stdout.getvalue())
    assert status["identity_registered"] is True
    assert status["ready"] is False
    assert "key_id" not in status
    assert "path" not in json.dumps(status)


def test_setup_plan_creates_the_explicit_target_not_the_invoking_hermes_profile(
    tmp_path: Path, monkeypatch
) -> None:
    """A Hermes caller profile is context, never an implicit target or a copy source."""
    monkeypatch.setenv("HERMES_PROFILE", "operator-profile")
    environment, probe = _hermes_environment(
        tmp_path, profiles=("operator-profile",), active="operator-profile"
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["identity"] == "needs_invitation"
    assert plan["invitation_input"] == "protected_prompt"
    assert plan["target_profile"] == {
        "name": "scout01",
        "disposition": "create",
        "reason": "target_profile_missing",
        "runtimes": {"hermes": {"disposition": "create", "reason": "target_profile_missing"}},
    }
    assert not any("profile" in call and "create" in call for call in probe.calls)
    assert not (Paths.for_profile(tmp_path, "scout01").root).exists()
    rendered = json.dumps(plan, sort_keys=True)
    assert "operator-profile" not in rendered
    assert str(tmp_path) not in rendered


def test_setup_plan_adopts_an_inactive_unbound_target_without_copying_runtime_data(
    tmp_path: Path, monkeypatch
) -> None:
    """Adoption targets the form name and preserves any model/provider-owned values in place."""
    environment, probe = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="operator-profile"
    )
    target_config = probe.home / "profiles" / "scout01" / "config.yaml"
    target_config.parent.mkdir(parents=True)
    target_config.write_text(
        "model: target-model-canary\n"
        "provider: target-provider-canary\n"
        "instructions: target-instructions-canary\n"
        "memories: target-memories-canary\n",
        encoding="utf-8",
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["target_profile"]["disposition"] == "adopt"
    assert plan["target_profile"]["runtimes"] == {
        "hermes": {
            "disposition": "adopt",
            "reason": "existing_unbound_inactive_target",
        }
    }
    rendered = json.dumps(plan, sort_keys=True)
    assert "operator-profile" not in rendered
    for marker in (
        "target-model-canary",
        "target-provider-canary",
        "target-instructions-canary",
        "target-memories-canary",
        str(probe.home),
    ):
        assert marker not in rendered
    assert not any(call[1:3] == ("profile", "create") for call in probe.calls)


def test_setup_plan_refuses_unreadable_target_config_without_echoing_its_path_or_data(
    tmp_path: Path,
) -> None:
    environment, probe = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="operator-profile"
    )
    target_config = probe.home / "profiles" / "scout01" / "config.yaml"
    target_config.parent.mkdir(parents=True)
    target_config.write_text(
        "mcp_servers:\n  broken: [provider-secret-canary\n",
        encoding="utf-8",
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    rendered = json.dumps(plan)
    assert plan["target_profile"]["disposition"] == "refused"
    assert str(tmp_path) not in rendered
    assert "provider-secret-canary" not in rendered


def test_setup_plan_redacts_an_unverifiable_connector_profile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = str(tmp_path / "connector-private-path-canary")

    def refuse_profile_path(cls, install_root: Path, profile: str) -> Paths:
        del cls, install_root, profile
        raise ConnectorError(f"Unsafe profile location: {private_path}")

    monkeypatch.setattr(Paths, "for_profile", classmethod(refuse_profile_path))
    environment, _ = _hermes_environment(tmp_path, profiles=(), active=None)

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    rendered = json.dumps(plan)
    assert plan["target_profile"]["disposition"] == "refused"
    assert private_path not in rendered


def test_setup_plan_refuses_an_active_target_before_invitation_input(tmp_path: Path) -> None:
    """An active target is refused even when the invoking profile would be safe to use."""
    environment, _ = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="scout01"
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["target_profile"]["disposition"] == "refused"
    assert plan["target_profile"]["reason"] == "target_profile_active"
    assert plan["invitation_input"] == "not_requested"
    assert plan["changes"] == []


def test_cli_plan_returns_nonzero_for_a_refused_target_without_requesting_an_invitation(
    tmp_path: Path,
) -> None:
    environment, _ = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="scout01"
    )
    result = main(
        [
            "setup",
            "--install-root",
            str(tmp_path),
            "--plan",
            "--profile",
            "scout01",
            "--handle",
            "scout-01",
            "--runtime",
            "hermes",
            "--setup-scope",
            "forum",
        ],
        environment,
    )

    plan = json.loads(environment.stdout.getvalue())
    assert result != 0
    assert plan["target_profile"]["disposition"] == "refused"
    assert plan["invitation_input"] == "not_requested"
    assert not (tmp_path / "profiles").exists()


def test_setup_plan_refuses_an_existing_target_when_activity_cannot_be_verified(
    tmp_path: Path,
) -> None:
    environment, _ = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active=None
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["target_profile"]["disposition"] == "refused"
    assert plan["target_profile"]["reason"] == "activity_unverified"
    assert plan["invitation_input"] == "not_requested"


def test_setup_refuses_an_active_target_before_reading_the_invitation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment, _ = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="scout01"
    )
    paths = Paths.for_profile(tmp_path, "scout01")
    adapter = HermesAdapter(
        which=environment.which,
        runner=environment.run,
        context=paths.runtime_context(),
    )
    monkeypatch.setattr(connector_module, "preflight", lambda *_: [])
    monkeypatch.setattr(
        connector_module,
        "read_invitation",
        lambda *_: pytest.fail("an active target must be refused before invitation input"),
    )

    with pytest.raises(ConnectorError, match="target profile"):
        run_setup(
            paths=paths,
            endpoints=Endpoints(
                onboarding_base_url="https://onboarding.example",
                agent_api_url="https://agent.example",
            ),
            environment=environment,
            runtime="hermes",
            expected_handle="scout-01",
            adapters=[adapter],
            soul_mode="skip",
        )

    assert not paths.root.exists()


def test_setup_plan_refuses_a_target_bound_to_another_agent_without_echoing_its_id(
    tmp_path: Path,
) -> None:
    environment, probe = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="operator-profile"
    )
    target_config = probe.home / "profiles" / "scout01" / "config.yaml"
    target_config.parent.mkdir(parents=True)
    target_config.write_text(
        "mcp_servers:\n  agentnexus-scout01:\n    env:\n      AGENTNEXUS_AGENT_ID: other-agent-id-canary\n",
        encoding="utf-8",
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["target_profile"]["disposition"] == "refused"
    assert plan["target_profile"]["reason"] == "bound_to_another_agent"
    assert plan["invitation_input"] == "not_requested"
    assert "other-agent-id-canary" not in json.dumps(plan)
    assert not any(call[1:3] == ("profile", "create") for call in probe.calls)


def test_creating_a_hermes_target_uses_its_official_fresh_profile_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_PROFILE", "operator-profile")
    monkeypatch.setenv("OPENAI_API_KEY", "invoking-provider-secret-canary")
    environment, probe = _hermes_environment(
        tmp_path, profiles=("operator-profile",), active="operator-profile"
    )
    paths = Paths.for_profile(tmp_path, "scout01")
    adapter = HermesAdapter(
        which=environment.which,
        runner=environment.run,
        context=paths.runtime_context(),
    )

    adapter._ensure_profile("hermes-test-executable")

    create_call = (
        "hermes-test-executable",
        "profile",
        "create",
        "scout01",
        "--no-alias",
        "--description",
        "AgentNexus agent profile",
    )
    assert create_call in probe.calls
    create_environment = probe.call_environments[probe.calls.index(create_call)]
    assert create_environment is not None
    assert create_environment.get("HERMES_HOME") == str(probe.home)
    assert "HERMES_PROFILE" not in create_environment
    assert "AGENTNEXUS_PROFILE" not in create_environment
    assert "OPENAI_API_KEY" not in create_environment
    assert "--from" not in create_call
    assert not any("operator-profile" in argument for call in probe.calls for argument in call)
    assert not paths.root.exists()


def test_setup_plan_resumes_only_a_matching_interrupted_agent_profile(
    tmp_path: Path, monkeypatch
) -> None:
    paths = Paths.for_profile(tmp_path, "scout01")
    paths.root.mkdir(parents=True)
    State(agent_id="agent-1", key_id="key-1", handle="scout-01").save(paths.state_file)
    environment, probe = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="operator-profile"
    )
    target_config = probe.home / "profiles" / "scout01" / "config.yaml"
    target_config.parent.mkdir(parents=True)
    target_config.write_text(
        "mcp_servers:\n  agentnexus-scout01:\n    env:\n      AGENTNEXUS_AGENT_ID: agent-1\n",
        encoding="utf-8",
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["identity"] == "resume"
    assert plan["target_profile"]["disposition"] == "resume"
    assert plan["invitation_input"] == "not_needed"
    assert "agent-1" not in json.dumps(plan)


def test_an_active_matching_interrupted_hermes_profile_is_refused(
    tmp_path: Path,
) -> None:
    paths = Paths.for_profile(tmp_path, "scout01")
    paths.root.mkdir(parents=True)
    State(agent_id="agent-1", key_id="key-1", handle="scout-01").save(paths.state_file)
    environment, probe = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="scout01"
    )
    target_config = probe.home / "profiles" / "scout01" / "config.yaml"
    target_config.parent.mkdir(parents=True)
    target_config.write_text(
        "mcp_servers:\n  agentnexus-scout01:\n    env:\n      AGENTNEXUS_AGENT_ID: agent-1\n",
        encoding="utf-8",
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["identity"] == "resume"
    assert plan["target_profile"]["disposition"] == "refused"
    assert plan["target_profile"]["reason"] == "target_profile_active"
    assert plan["invitation_input"] == "not_requested"
    assert plan["changes"] == []


def test_matching_connector_state_resumes_an_active_unregistered_target(
    tmp_path: Path,
) -> None:
    paths = Paths.for_profile(tmp_path, "scout01")
    paths.root.mkdir(parents=True)
    State(agent_id="agent-1", key_id="key-1", handle="scout-01").save(paths.state_file)
    environment, _ = _hermes_environment(
        tmp_path, profiles=("operator-profile", "scout01"), active="scout01"
    )

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=environment,
    )

    assert plan["identity"] == "resume"
    assert plan["target_profile"]["disposition"] == "resume"
    assert plan["invitation_input"] == "not_needed"
