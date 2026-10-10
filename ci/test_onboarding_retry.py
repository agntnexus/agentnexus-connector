"""An applicant can retry a rejected invitation without unlocking an uncertain redemption.

agntnexus/agentnexus#200. Exercise the actual CLI, onboarding HTTP client, persisted state and
throwaway profile key. Runtime discovery and signed post-setup reads are stand-ins; no real
invitation, installation, runtime, account or external server is used.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import httpx2 as httpx
import pytest

from agentnexus_sdk import connector
from agentnexus_sdk.onboarding import OnboardingClient
from agentnexus_sdk.runtimes import ConfigurationOutcome, ModelStatus, TargetProfileInspection

BAD_INVITATION = "mistyped-invitation-fixture"
GOOD_INVITATION = "correct-invitation-fixture"
CHALLENGE = {
    "invitation_id": "00000000-0000-4000-8000-000000000001",
    "challenge": "synthetic-challenge-fixture",
    "protocol_version": "onboarding-v1",
    "profile_digest": "0" * 64,
    "expires_at": "2099-01-01T00:00:00+00:00",
}
IDENTITY = {"agent_id": "fixture-agent", "key_id": "fixture-key", "handle": "sampleagent"}


class StandInRuntime:
    """A runtime setup can configure without installing Hermes or OpenClaw."""

    name = "hermes"
    display_name = "Hermes"

    def inspect_target_profile(self) -> TargetProfileInspection:
        """Report a missing, creatable, inactive target in this profile only."""
        return TargetProfileInspection(
            available=True,
            exists=False,
            active=False,
            safe=True,
            can_create=True,
        )

    def existing_entry(self) -> None:
        """Report no AgentNexus entry on this target."""
        return None

    def configure(
        self,
        spec: object,
        *,
        backup_directory: Path,
        target_profile_disposition: str | None = None,
    ) -> ConfigurationOutcome:
        """Accept registration without writing another profile."""
        del spec, backup_directory, target_profile_disposition
        return ConfigurationOutcome(changed=True, backup=None, detail="stand-in configured")

    def verify_isolation(self) -> list[str]:
        """Report isolation without inspecting a real runtime."""
        return []

    def verify(self) -> list[str]:
        """Report the stand-in registration as present."""
        return ["stand-in verified"]

    def model_status(self) -> ModelStatus:
        """Report a usable model so setup does not open a provider wizard."""
        return ModelStatus(configured=True, known=True, detail="stand-in model")

    def start_hint(self) -> list[str]:
        """Give a local start hint that names no other profile."""
        return ["start the stand-in runtime"]

    def provider_setup_invocation(self) -> None:
        """Publish no provider wizard."""
        return None


class Setup:
    """Run identical CLI arguments against synthetic onboarding responses."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep profile, input, output and HTTP traffic within this test."""
        self.root = root
        self.paths = connector.Paths.for_profile(root, "sampleagent")
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.prompts: list[str] = []
        self.requests: list[httpx.Request] = []
        self.invitation = BAD_INVITATION
        self.challenge_failure: httpx.Response | Exception | None = None
        self.redemption_failure: httpx.Response | Exception | None = None
        self.stages: list[connector.Stage] = []
        monkeypatch.setattr(
            connector, "select_adapters", lambda *_args, **_kwargs: [StandInRuntime()]
        )
        monkeypatch.setattr(connector, "smoke_test", lambda **_kwargs: None)
        monkeypatch.setattr(
            connector,
            "OnboardingClient",
            lambda **kwargs: OnboardingClient(
                **kwargs, transport=httpx.MockTransport(self.respond)
            ),
        )

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Reject a wrong invitation and accept a corrected one."""
        self.requests.append(request)
        self.stages.append(connector.State.load(self.paths.state_file).stage)
        body = json.loads(request.content)
        if request.url.path.endswith("/challenges"):
            if self.challenge_failure is not None:
                if isinstance(self.challenge_failure, Exception):
                    raise self.challenge_failure
                return self.challenge_failure
            if body["invitation_capability"] == BAD_INVITATION:
                return httpx.Response(
                    400,
                    json={"code": "onboarding.challenge_invalid", "detail": "No such invitation."},
                )
            return httpx.Response(201, json=CHALLENGE)
        assert request.url.path.endswith("/redemptions")
        if self.redemption_failure is not None:
            if isinstance(self.redemption_failure, Exception):
                raise self.redemption_failure
            return self.redemption_failure
        return httpx.Response(201, json=IDENTITY)

    def prompt(self, text: str) -> str:
        """Return synthetic invitation input without printing it."""
        self.prompts.append(text)
        return self.invitation

    def run(self) -> int:
        """Use the same arguments the bootstrap passes on every invocation."""
        return connector.main(
            [
                "setup",
                "--install-root",
                str(self.root),
                "--profile",
                "sampleagent",
                "--handle",
                "sampleagent",
                "--runtime",
                "hermes",
                "--origin",
                "https://observer.example.org",
                "--agent-api-url",
                "https://agent.example.org",
                "--agent-read-url",
                "https://read.example.org",
                "--soul",
                "skip",
            ],
            environment=connector.Environment(
                stdout=self.stdout,
                stderr=self.stderr,
                prompt=self.prompt,
                which=lambda _name: str(self.root / "fixture-executable"),
                tcp_probe=lambda *_args: True,
            ),
        )


def test_original_command_resumes_after_wrong_invitation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrected invitation finishes setup on the next run using the original key."""
    setup = Setup(tmp_path / "installation", monkeypatch)
    assert setup.run() == connector.EXIT_REDEMPTION
    key = setup.paths.private_key.read_bytes()
    assert len(setup.requests) == 1
    assert setup.requests[0].url.path.endswith("/challenges")

    setup.invitation = GOOD_INVITATION
    assert setup.run() == 0
    assert setup.paths.private_key.read_bytes() == key
    state = connector.State.load(setup.paths.state_file)
    assert state.stage == connector.Stage.COMPLETE
    assert state.handle == IDENTITY["handle"]
    assert len(setup.prompts) == 2
    assert len(setup.requests) == 3
    assert setup.stages == [
        connector.Stage.KEY_CREATED,
        connector.Stage.KEY_CREATED,
        connector.Stage.REDEMPTION_ATTEMPTED,
    ]
    for invitation in (BAD_INVITATION, GOOD_INVITATION):
        assert invitation not in setup.paths.state_file.read_text(encoding="utf-8")
        assert invitation not in setup.stdout.getvalue() + setup.stderr.getvalue()


def test_server_echo_does_not_disclose_invitation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even request-specific error prose cannot put an invitation into CLI output."""
    setup = Setup(tmp_path / "installation", monkeypatch)
    setup.challenge_failure = httpx.Response(
        400,
        json={"code": "onboarding.challenge_invalid", "detail": BAD_INVITATION},
    )
    assert setup.run() == connector.EXIT_REDEMPTION
    assert BAD_INVITATION not in setup.stdout.getvalue() + setup.stderr.getvalue()
    assert BAD_INVITATION not in setup.paths.state_file.read_text(encoding="utf-8")


def problem(status: int, code: str) -> httpx.Response:
    """Return the API's structured problem shape, without request-specific detail."""
    return httpx.Response(
        status,
        headers={"content-type": "application/problem+json; charset=utf-8"},
        json={"status": status, "code": code, "detail": "Synthetic rejection."},
    )


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("Synthetic lost challenge answer."),
        httpx.ConnectError("Synthetic connection failure."),
        problem(409, "onboarding.invalid_state"),
        problem(429, "rate_limit.exceeded"),
        problem(503, "service.database_unavailable"),
        httpx.Response(200, json=[]),
    ],
    ids=["timeout", "connection", "inactive", "rate-limit", "database", "malformed-success"],
)
def test_challenge_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: httpx.Response | Exception
) -> None:
    """No challenge failure can strand a key when redemption was never sent."""
    setup = Setup(tmp_path / "installation", monkeypatch)
    setup.invitation = GOOD_INVITATION
    setup.challenge_failure = failure
    assert setup.run() == connector.EXIT_REDEMPTION
    assert connector.State.load(setup.paths.state_file).stage == connector.Stage.KEY_CREATED
    key = setup.paths.private_key.read_bytes()
    setup.challenge_failure = None
    assert setup.run() == 0
    assert setup.paths.private_key.read_bytes() == key


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "onboarding.redemption_invalid"),
        (401, "onboarding.authenticity_failed"),
        (409, "onboarding.identity_conflict"),
    ],
)
def test_confirmed_redemption_rejection_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, code: str
) -> None:
    """Only explicit transactional rejections permit the same command to ask again."""
    setup = Setup(tmp_path / "installation", monkeypatch)
    setup.invitation = GOOD_INVITATION
    setup.redemption_failure = problem(status, code)
    assert setup.run() == connector.EXIT_REDEMPTION
    assert setup.stages[-1] == connector.Stage.REDEMPTION_ATTEMPTED
    assert connector.State.load(setup.paths.state_file).stage == connector.Stage.KEY_CREATED
    assert "same original setup command" in setup.stderr.getvalue()
    key = setup.paths.private_key.read_bytes()
    setup.redemption_failure = None
    assert setup.run() == 0
    assert setup.paths.private_key.read_bytes() == key


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("Synthetic lost redemption answer."),
        httpx.ConnectError("Synthetic connection failure."),
        problem(409, "onboarding.invalid_state"),
        problem(400, "onboarding.future_failure"),
        problem(503, "onboarding.redemption_invalid"),
        problem(500, "service.internal_error"),
        httpx.Response(400, text="onboarding.redemption_invalid: Synthetic prose."),
        httpx.Response(400, json={"status": 400, "code": "onboarding.redemption_invalid"}),
        httpx.Response(
            400,
            headers={"content-type": "application/problem+json"},
            json={"status": 409, "code": "onboarding.redemption_invalid"},
        ),
        httpx.Response(
            400,
            headers={"content-type": "application/problem+json"},
            json={"code": "onboarding.redemption_invalid"},
        ),
        httpx.Response(302, headers={"location": "https://other.example.org"}),
        httpx.Response(201, json=[]),
        httpx.Response(201, json={}),
        httpx.Response(201, json={**IDENTITY, "key_id": None}),
    ],
    ids=[
        "timeout",
        "connection",
        "already-used",
        "unknown-code",
        "wrong-status",
        "server-error",
        "code-in-prose",
        "wrong-content-type",
        "mismatched-status",
        "missing-status",
        "redirect",
        "non-object-success",
        "missing-identity",
        "invalid-identity",
    ],
)
def test_uncertain_redemption_stays_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: httpx.Response | Exception
) -> None:
    """An uncertain answer never unlocks the profile or consumes another invitation."""
    setup = Setup(tmp_path / "installation", monkeypatch)
    setup.invitation = GOOD_INVITATION
    setup.redemption_failure = failure
    assert setup.run() == connector.EXIT_REDEMPTION
    assert connector.State.load(setup.paths.state_file).stage == (
        connector.Stage.REDEMPTION_ATTEMPTED
    )
    state_bytes = setup.paths.state_file.read_bytes()
    key = setup.paths.private_key.read_bytes()
    setup.redemption_failure = None
    setup.invitation = BAD_INVITATION
    assert setup.run() == connector.EXIT_NEEDS_REPLACEMENT
    assert len(setup.prompts) == 1
    assert len(setup.requests) == 2
    assert setup.paths.state_file.read_bytes() == state_bytes
    assert setup.paths.private_key.read_bytes() == key


def test_pending_marker_must_be_durable_before_redemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed pending-state write prevents the identity-creating HTTP call."""
    setup = Setup(tmp_path / "installation", monkeypatch)
    setup.invitation = GOOD_INVITATION
    original = connector.State.save

    def refuse_pending_save(state: connector.State, path: Path) -> None:
        if state.stage == connector.Stage.REDEMPTION_ATTEMPTED:
            raise OSError("Synthetic state-write failure.")
        original(state, path)

    with monkeypatch.context() as patch:
        patch.setattr(connector.State, "save", refuse_pending_save)
        with pytest.raises(OSError, match="Synthetic state-write failure"):
            setup.run()
    assert len(setup.requests) == 1
    assert connector.State.load(setup.paths.state_file).stage == connector.Stage.KEY_CREATED
    key = setup.paths.private_key.read_bytes()
    assert setup.run() == 0
    assert setup.paths.private_key.read_bytes() == key


def test_existing_ambiguous_state_is_not_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old pending states contain no evidence that the failure was only a challenge."""
    setup = Setup(tmp_path / "installation", monkeypatch)
    setup.paths.root.mkdir(parents=True)
    connector.State(stage=connector.Stage.REDEMPTION_ATTEMPTED).save(setup.paths.state_file)
    before = setup.paths.state_file.read_bytes()
    assert setup.run() == connector.EXIT_NEEDS_REPLACEMENT
    assert setup.prompts == []
    assert setup.requests == []
    assert setup.paths.state_file.read_bytes() == before


def test_completed_profile_does_not_redeem_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed identity and every neighbouring profile survive another invocation."""
    setup = Setup(tmp_path / "installation", monkeypatch)
    other = connector.Paths.for_profile(setup.root, "otheragent")
    other.root.mkdir(parents=True)
    sentinel = other.root / "runtime-config.json"
    sentinel.write_text('{"owner":"otheragent"}', encoding="utf-8")
    before = sentinel.read_bytes()
    setup.invitation = GOOD_INVITATION
    assert setup.run() == 0
    key = setup.paths.private_key.read_bytes()
    assert setup.run() == 0
    assert len(setup.requests) == 2
    assert len(setup.prompts) == 1
    assert setup.paths.private_key.read_bytes() == key
    assert sentinel.read_bytes() == before
