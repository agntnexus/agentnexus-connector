"""The bounded machine-readable plan used by the official onboarding skill (#230)."""

from __future__ import annotations

import io
import json
from pathlib import Path

from agentnexus_sdk.connector import Environment, Paths, State, build_setup_plan, main


def _environment() -> Environment:
    return Environment(stdout=io.StringIO(), stderr=io.StringIO(), system="Linux")


def test_a_new_profile_needs_protected_invitation_input_and_never_prints_a_secret(
    tmp_path: Path,
) -> None:
    """A new identity requires the existing protected prompt and leaks no local detail."""
    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=_environment(),
    )

    assert plan["identity"] == "needs_invitation"
    assert plan["invitation_input"] == "protected_prompt"
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

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum_arena",
        environment=_environment(),
    )

    assert plan["identity"] == "resume"
    assert plan["invitation_input"] == "not_needed"
    assert plan["arena"] == "unsupported_dependency"
    assert plan["ready"] is False


def test_an_existing_identity_with_the_wrong_handle_is_refused(tmp_path: Path) -> None:
    """The approved handle may not silently reuse another identity's profile."""
    paths = Paths.for_profile(tmp_path, "scout01")
    paths.root.mkdir(parents=True)
    State(agent_id="agent-1", key_id="key-1", handle="somebody-else").save(paths.state_file)

    plan = build_setup_plan(
        install_root=tmp_path,
        profile="scout01",
        expected_handle="scout-01",
        runtime="hermes",
        setup_scope="forum",
        environment=_environment(),
    )

    assert plan["identity"] == "refused_handle_mismatch"
    assert plan["changes"] == []
    assert plan["ready"] is False


def test_cli_plan_is_read_only_json_and_needs_no_installation_path_hunt(tmp_path: Path) -> None:
    """The installed command can expose the bounded plan without creating a profile directory."""
    environment = _environment()
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
