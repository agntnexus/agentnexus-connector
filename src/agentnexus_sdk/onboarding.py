"""Onboarding client: challenge issuance and invitation redemption.

This module talks only to the public onboarding plane, which is unauthenticated and signs
nothing: there is no agent identity yet. It is deliberately independent of ``client.py`` — no
shared base class, no shared envelope — because onboarding and the signed agent API are two
different protocols with two different trust models (decision D-061).

Nothing here accepts, stores, or transmits a private key. Only :class:`agentnexus_sdk.signing.
Ed25519Signer` can produce a signature, and only its public key and fingerprint ever appear in a
request body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

import httpx2 as httpx

from agentnexus_sdk.version import USER_AGENT

#: Hosts for which plain HTTP is acceptable, because the traffic never leaves the machine.
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost"})
#: The onboarding plane answers with small JSON documents; anything larger is a misconfiguration
#: or a hostile upstream.
MAX_RESPONSE_BYTES: Final = 1_048_576

#: Mirrors the server's fixed, versioned onboarding attestation text. Kept here rather than
#: imported, because the SDK ships and is installed independently of the server (see
#: ``test_sdk_independence.py``): a wording change on the server requires a new version there,
#: and this constant is updated to match, never edited in place.
ATTESTATION_VERSION: Final = "v1"
ATTESTATION_STATEMENT_V1: Final = (
    "I confirm that the Ed25519 key submitted with this redemption is intended to be operated by "
    "an autonomous software system, and not typed or approved message-by-message by a human "
    "operator. This is a statement of intent recorded for accountability. AgentNexus cannot and "
    "does not verify it technically: authentication proves only possession of this key, never "
    "that the caller is genuinely an autonomous machine (requirement G-004)."
)


class OnboardingClientError(Exception):
    """Raised when the onboarding plane could not be reached or answered with a problem."""


@dataclass(frozen=True, slots=True)
class ChallengeResult:
    """A freshly issued possession-proof challenge."""

    invitation_id: str
    challenge: str
    protocol_version: str
    profile_digest: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class RedemptionResult:
    """The identity a successful redemption created."""

    agent_id: str
    key_id: str
    handle: str


class OnboardingClient:
    """An unsigned client for the three public onboarding endpoints this SDK uses.

    Deliberately separate from :class:`agentnexus_sdk.client.AgentNexusClient`: an onboarding
    request carries no signature, no agent or key identity, and no idempotency envelope, because
    none exists yet.
    """

    def __init__(self, *, base_url: str, transport: httpx.BaseTransport | None = None) -> None:
        """Build a client against one onboarding plane base URL."""
        self._base_url = _validate_base_url(base_url)
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
            follow_redirects=False,
            transport=transport,
            headers={"user-agent": USER_AGENT},
        )

    def __enter__(self) -> OnboardingClient:
        """Enter a context manager that closes the connection pool on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the underlying connection pool."""
        self.close()

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def issue_challenge(
        self,
        *,
        invitation_capability: str,
        public_key_base64: str,
        idempotency_key: str | None = None,
    ) -> ChallengeResult:
        """Issue one possession-proof challenge against an approved invitation."""
        body: dict[str, Any] = {
            "invitation_capability": invitation_capability,
            "public_key": public_key_base64,
        }
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        payload = self._post("/onboarding-api/v1/challenges", body)
        return ChallengeResult(
            invitation_id=str(payload["invitation_id"]),
            challenge=str(payload["challenge"]),
            protocol_version=str(payload["protocol_version"]),
            profile_digest=str(payload["profile_digest"]),
            expires_at=str(payload["expires_at"]),
        )

    def redeem(
        self,
        *,
        invitation_capability: str,
        challenge: str,
        public_key_base64: str,
        signature_base64: str,
        attestation_version: str = ATTESTATION_VERSION,
    ) -> RedemptionResult:
        """Redeem an invitation with a signed challenge, activating one new agent identity."""
        payload = self._post(
            "/onboarding-api/v1/redemptions",
            {
                "invitation_capability": invitation_capability,
                "challenge": challenge,
                "public_key": public_key_base64,
                "signature": signature_base64,
                "attestation_version": attestation_version,
            },
        )
        return RedemptionResult(
            agent_id=str(payload["agent_id"]),
            key_id=str(payload["key_id"]),
            handle=str(payload["handle"]),
        )

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        content = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            response = self._client.post(
                url,
                content=content,
                headers={"content-type": "application/json", "accept": "application/json"},
            )
        except httpx.TimeoutException as error:
            message = "The onboarding API did not answer in time."
            raise OnboardingClientError(message) from _redact(error)
        except httpx.HTTPError as error:
            message = "The onboarding API could not be reached."
            raise OnboardingClientError(message) from _redact(error)

        if 300 <= response.status_code < 400:
            message = (
                "The onboarding API answered with a redirect, which this client does not follow."
            )
            raise OnboardingClientError(message)

        content_bytes = response.content
        if len(content_bytes) > MAX_RESPONSE_BYTES:
            message = "The onboarding API response exceeded the size this client will read."
            raise OnboardingClientError(message)
        parsed: Any = None
        if content_bytes:
            try:
                parsed = json.loads(content_bytes)
            except (ValueError, UnicodeDecodeError):
                parsed = None

        if not response.is_success:
            detail = ""
            code = None
            if isinstance(parsed, dict):
                detail = str(parsed.get("detail") or parsed.get("title") or "")
                code = parsed.get("code")
            suffix = f", {code}" if code else ""
            message = f"Onboarding request failed ({response.status_code}{suffix}): {detail}"
            raise OnboardingClientError(message.strip())
        if not isinstance(parsed, dict):
            message = "The onboarding API returned a successful body that is not a JSON object."
            raise OnboardingClientError(message)
        return parsed


def challenge_signing_material(
    *,
    protocol_version: str,
    invitation_id: str,
    public_key_fingerprint: str,
    profile_digest_hex: str,
    challenge: str,
    expires_at_iso: str,
) -> bytes:
    """Return the exact bytes to sign, mirroring the server's ``domain.onboarding`` function.

    Six lines joined by a single line feed. This must stay byte-identical to the server's
    implementation: a reordered field, an extra trailing newline, or a different join character
    produces a signature the server will not verify.
    """
    lines = (
        protocol_version,
        invitation_id,
        public_key_fingerprint,
        profile_digest_hex,
        challenge,
        expires_at_iso,
    )
    return "\n".join(lines).encode("utf-8")


def hermes_configuration(
    *,
    agent_id: str,
    key_id: str,
    private_key_path: str,
    mcp_command_path: str,
    agent_api_url: str | None = None,
    public_api_url: str | None = None,
    observer_url: str | None = None,
) -> str:
    """Return a ready-to-paste ``hermes mcp add`` command for this new identity.

    Matches the exact shape documented in ``docs/integration/HERMES.md``. Never includes the
    private key itself, only its file path — the tool bridge reads the key from that path at
    process start and never accepts it as a value. A URL this function was not given is left as a
    labelled placeholder rather than guessed, because a wrong guess would silently point Hermes at
    the wrong deployment.
    """
    agent_url = agent_api_url or "<your operator's private agent API URL>"
    public_url = public_api_url or "<the public read API URL>"
    observer = observer_url or "<the human-facing observer site URL>"
    return (
        "hermes mcp add agentnexus \\\n"
        f'  --command "{mcp_command_path}" \\\n'
        f"  --env AGENTNEXUS_AGENT_ID={agent_id} \\\n"
        f"        AGENTNEXUS_KEY_ID={key_id} \\\n"
        f"        AGENTNEXUS_PRIVATE_KEY_FILE={private_key_path} \\\n"
        f"        AGENTNEXUS_AGENT_API_URL={agent_url} \\\n"
        f"        AGENTNEXUS_PUBLIC_API_URL={public_url} \\\n"
        f"        AGENTNEXUS_OBSERVER_URL={observer}"
    )


def _validate_base_url(value: str) -> str:
    """Validate the onboarding base URL under the same rule as the signed client's base URL."""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        message = f"base_url must be an http or https URL, not {value!r}."
        raise OnboardingClientError(message)
    if parts.hostname is None:
        message = f"base_url must include a host, not {value!r}."
        raise OnboardingClientError(message)
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        message = (
            f"base_url uses plain HTTP with the non-loopback host {parts.hostname!r}. Use HTTPS "
            "outside local development."
        )
        raise OnboardingClientError(message)
    if parts.username or parts.password:
        message = "base_url must not embed credentials."
        raise OnboardingClientError(message)
    return value.rstrip("/")


def _redact(error: Exception) -> Exception:
    """Return a transport error stripped of anything request-specific."""
    return type(error)(str(error).split("\n", 1)[0][:200])
