"""A failed signed connection test names the read origin, and a resume can correct that origin.

agntnexus/agentnexus#247. The signed-read edge is served at ``https://read.agntnexus.com:8443``.
An applicant whose profile still holds the portless origin reaches Traefik's self-signed default
certificate; the SDK reports only "The agent API could not be reached", after the one-time
invitation is spent. These tests cover two things:

* the smoke-test failure says *which* origin failed and that a TLS verification failure is a
  likely cause, without printing anything request-specific and without suggesting that
  verification be switched off;
* ``setup`` on an already-registered profile accepts an explicit ``--agent-read-url`` and updates
  the remembered origin without asking for the invitation again.

Synthetic transports and throwaway profiles only; no real host, key, invitation or server.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from test_onboarding_retry import GOOD_INVITATION, Setup, StandInRuntime

from agentnexus_sdk import connector
from agentnexus_sdk.signing import generate_key_pair

WRITE_ORIGIN = "https://agent.example.org"
READ_ORIGIN = "https://read.example.org:8443"
PORTLESS_READ_ORIGIN = "https://read.example.org"

#: A well-formed identity: the envelope refuses anything but lowercase hyphenated UUIDs before a
#: request is ever built, and these tests need the request to reach the transport.
IDENTITY = connector.Identity(
    agent_id="00000000-0000-4000-8000-0000000000a1",
    key_id="00000000-0000-4000-8000-0000000000b2",
    handle="sampleagent",
)

#: Strings a diagnostic must never contain. The first two stand for what a transport error can
#: carry (a header value, a signature); the rest are advice the connector must not give.
SECRET_MARKERS = ("x-agent-signature", "synthetic-secret-signature")
VERIFICATION_ADVICE = ("verify=false", "-k ", "--insecure", "disable verification", "skip verif")


def _endpoints(*, read: str | None) -> connector.Endpoints:
    return connector.Endpoints(
        onboarding_base_url=WRITE_ORIGIN,
        agent_api_url=WRITE_ORIGIN,
        public_api_url=None,
        observer_url=None,
        agent_read_url=read,
    )


def _environment() -> connector.Environment:
    return connector.Environment(
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        prompt=lambda _text: pytest.fail("a smoke test must never prompt"),
        which=lambda _name: None,
        tcp_probe=lambda *_args: True,
    )


def _failing_smoke_test(
    monkeypatch: pytest.MonkeyPatch, *, read: str | None, failure: Exception
) -> connector.ConnectorError:
    """Run the real smoke test against a transport that raises ``failure`` for every request."""
    seen: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise failure

    real_client = connector.AgentNexusClient
    monkeypatch.setattr(
        connector,
        "AgentNexusClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(refuse)),
    )
    with pytest.raises(connector.ConnectorError) as raised:
        connector.smoke_test(
            identity=IDENTITY,
            signer=generate_key_pair().signer,
            endpoints=_endpoints(read=read),
            environment=_environment(),
        )
    assert seen, "the transport must have been reached, or this proves nothing"
    return raised.value


def _text(error: connector.ConnectorError) -> str:
    return f"{error}\n{error.recovery or ''}"


class TestATransportFailureNamesTheOriginThatFailed:
    """Criterion 4: the smoke-test message says where it failed and why that is likely."""

    def test_the_configured_read_origin_is_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The applicant is told which address was tried, not just that one failed."""
        error = _failing_smoke_test(
            monkeypatch, read=READ_ORIGIN, failure=httpx.ConnectError("Synthetic failure.")
        )
        assert READ_ORIGIN in _text(error)

    def test_a_tls_verification_failure_is_named_as_a_likely_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The portless origin fails certificate verification, so the message says so."""
        error = _failing_smoke_test(
            monkeypatch,
            read=PORTLESS_READ_ORIGIN,
            failure=httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed"
            ),
        )
        text = _text(error)
        assert PORTLESS_READ_ORIGIN in text
        assert "TLS" in text
        assert "certificate" in text.lower()

    def test_the_message_points_at_the_documented_correction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The recovery names the flag that corrects the address."""
        error = _failing_smoke_test(
            monkeypatch,
            read=PORTLESS_READ_ORIGIN,
            failure=httpx.ConnectError("Synthetic failure."),
        )
        assert "--agent-read-url" in _text(error)

    def test_without_a_read_origin_the_write_origin_is_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Tailnet profile has one address for both directions; that is the one that failed."""
        error = _failing_smoke_test(
            monkeypatch, read=None, failure=httpx.ConnectError("Synthetic failure.")
        )
        assert WRITE_ORIGIN in _text(error)

    def test_nothing_request_specific_is_printed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A header or signature carried by the transport error never reaches the output."""
        error = _failing_smoke_test(
            monkeypatch,
            read=READ_ORIGIN,
            failure=httpx.ConnectError(
                "Synthetic failure.\nx-agent-signature: synthetic-secret-signature"
            ),
        )
        text = _text(error).lower()
        for marker in SECRET_MARKERS:
            assert marker not in text

    @pytest.mark.parametrize(
        "failure",
        [
            httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate"),
            httpx.ConnectError("Synthetic failure."),
        ],
        ids=["tls", "plain"],
    )
    def test_the_message_never_suggests_switching_verification_off(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        """The fix named is the address; verification is never the thing to relax."""
        text = _text(_failing_smoke_test(monkeypatch, read=READ_ORIGIN, failure=failure)).lower()
        for advice in VERIFICATION_ADVICE:
            assert advice not in text

    def test_the_exit_code_and_resume_guidance_are_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adding detail does not change how setup fails or that it resumes."""
        error = _failing_smoke_test(
            monkeypatch, read=READ_ORIGIN, failure=httpx.ConnectError("Synthetic failure.")
        )
        assert error.exit_code == connector.EXIT_CONNECTIVITY
        assert "resumes from here" in (error.recovery or "")


class TestAnAlreadyRegisteredProfileTakesACorrectedReadOrigin:
    """Criterion 5: the invitation is spent, so the correction must not ask for it again."""

    @staticmethod
    def _run(setup: Setup, read_url: str) -> int:
        return connector.main(
            [
                "setup",
                "--install-root",
                str(setup.root),
                "--profile",
                "sampleagent",
                "--handle",
                "sampleagent",
                "--runtime",
                "hermes",
                "--origin",
                "https://observer.example.org",
                "--agent-api-url",
                WRITE_ORIGIN,
                "--agent-read-url",
                read_url,
                "--soul",
                "skip",
            ],
            environment=connector.Environment(
                stdout=setup.stdout,
                stderr=setup.stderr,
                prompt=setup.prompt,
                which=lambda _name: str(setup.root / "fixture-executable"),
                tcp_probe=lambda *_args: True,
            ),
        )

    def test_an_explicit_read_url_updates_the_remembered_origin_without_the_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Characterisation: the explicit flag already wins; this pins it and the missing prompt."""
        setup = Setup(tmp_path / "installation", monkeypatch)
        setup.invitation = GOOD_INVITATION

        assert self._run(setup, PORTLESS_READ_ORIGIN) == 0
        assert len(setup.prompts) == 1
        requests_after_registration = len(setup.requests)
        key = setup.paths.private_key.read_bytes()
        assert connector.State.load(setup.paths.state_file).stage == connector.Stage.COMPLETE
        assert self._remembered_read_origin(setup) == PORTLESS_READ_ORIGIN

        assert self._run(setup, READ_ORIGIN) == 0

        assert len(setup.prompts) == 1, "the invitation prompt must not be reached on a resume"
        assert len(setup.requests) == requests_after_registration, "nothing is redeemed twice"
        assert setup.paths.private_key.read_bytes() == key
        assert self._remembered_read_origin(setup) == READ_ORIGIN
        assert GOOD_INVITATION not in setup.stdout.getvalue() + setup.stderr.getvalue()

    def test_the_runtime_entry_is_registered_with_the_corrected_origin_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Characterisation: the bridge reads the runtime entry, not profile.json, so it matters.

        An agent whose profile record says `:8443` but whose runtime entry still says the
        portless origin would keep failing, so the resume must hand the runtime the new value.
        """
        setup = Setup(tmp_path / "installation", monkeypatch)
        setup.invitation = GOOD_INVITATION
        registered: list[dict[str, str]] = []

        class RecordingRuntime(StandInRuntime):
            def configure(self, spec: Any, **kwargs: Any) -> Any:
                registered.append(dict(spec.environment))
                return super().configure(spec, **kwargs)

        monkeypatch.setattr(
            connector, "select_adapters", lambda *_args, **_kwargs: [RecordingRuntime()]
        )

        assert self._run(setup, PORTLESS_READ_ORIGIN) == 0
        assert self._run(setup, READ_ORIGIN) == 0

        assert [entry["AGENTNEXUS_AGENT_READ_URL"] for entry in registered] == [
            PORTLESS_READ_ORIGIN,
            READ_ORIGIN,
        ]
        assert len(setup.prompts) == 1

    @staticmethod
    def _remembered_read_origin(setup: Setup) -> str | None:
        record = connector.ProfileRecord.load(setup.paths.profile_record)
        assert record is not None
        return record.endpoints.get("agent_read_url")


class TestThePreflightChecksTheReadHostToo:
    """Criterion 4, preflight: a dead read host is found before the invitation is spent.

    Only the write host was probed before, so a dead read host surfaced as a late
    "could not be reached" from the signed test.
    """

    @staticmethod
    def _check(read: str | None, *, answering: set[tuple[str, int]]) -> list[tuple[str, int]]:
        probed: list[tuple[str, int]] = []

        def probe(host: str, port: int, _timeout: float = 5.0) -> bool:
            probed.append((host, port))
            return (host, port) in answering

        environment = connector.Environment(
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            prompt=lambda _text: pytest.fail("a connectivity check must never prompt"),
            which=lambda _name: None,
            tcp_probe=probe,
        )
        connector.check_connectivity(_endpoints(read=read), environment)
        return probed

    def test_both_hosts_are_probed_on_their_own_ports(self) -> None:
        """The read origin's explicit port is the one opened, not 443."""
        probed = self._check(
            READ_ORIGIN,
            answering={("agent.example.org", 443), ("read.example.org", 8443)},
        )
        assert probed == [("agent.example.org", 443), ("read.example.org", 8443)]

    def test_a_dead_read_host_is_named_with_its_origin_and_port(self) -> None:
        """The failure says it is the read address, and nothing about the write host."""
        with pytest.raises(connector.ConnectorError) as raised:
            self._check(READ_ORIGIN, answering={("agent.example.org", 443)})
        text = _text(raised.value)
        assert raised.value.exit_code == connector.EXIT_CONNECTIVITY
        assert READ_ORIGIN in text
        assert "read.example.org" in text
        assert "8443" in text
        assert "tailscale" not in text.lower()
        for advice in VERIFICATION_ADVICE:
            assert advice not in text.lower()

    def test_a_deployment_without_a_read_origin_is_probed_exactly_as_before(self) -> None:
        """A Tailnet profile has one address for both directions."""
        assert self._check(None, answering={("agent.example.org", 443)}) == [
            ("agent.example.org", 443)
        ]

    def test_the_write_host_failing_is_still_reported_first(self) -> None:
        """The check order and the message for the write host are unchanged."""
        with pytest.raises(connector.ConnectorError, match=r"agent\.example\.org did not answer"):
            self._check(READ_ORIGIN, answering=set())

    def test_the_connector_never_probes_ssh_on_the_read_host(self) -> None:
        """The refusal that protects the write host protects the read host as well."""
        with pytest.raises(connector.ConnectorError, match="Refusing to use TCP/22"):
            self._check("https://read.example.org:22", answering={("agent.example.org", 443)})
