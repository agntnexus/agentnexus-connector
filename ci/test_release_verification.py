"""Check that the release chain still refuses everything it is supposed to refuse.

The Termux fix in `agentnexus_sdk/android.py` runs *after* an artifact is on disk and verified, and
changes nothing about how it got there. That is a claim, and agntnexus/agentnexus#5 requires it to
be measured rather than asserted: manifest signature and artifact digest verification still happen
before an installation, and they still fail closed.

So these cases mutate the published chain — the signature, the manifest, the artifact, the key and
the origin, one at a time — and require a refusal for each. Everything happens in memory; nothing
under `connector-release/` is written to, and no network, credential or device is involved.

The one key this file creates is an ephemeral P-256 key, generated in the test process, used to
sign a synthetic manifest and then discarded. It is not a release key, it signs nothing that is
published, and it never reaches the filesystem. Without it the "fail closed" cases could only be
written the other way round — checking that bad input fails, never that good input passes — and a
verifier that refuses everything would pass that suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from agentnexus_sdk.release import (
    Artifact,
    ReleaseError,
    canonical_bytes,
    parse_manifest,
    verify_manifest,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = REPOSITORY_ROOT / "connector-release"
MANIFEST = RELEASE / "connector" / "connector-release.json"
SIGNATURE = RELEASE / "connector" / "connector-release.json.sig"
RELEASED_LOADER = RELEASE / "connect.sh"

#: The public coordinates stamped into the released loader, which is where a real installation gets
#: the key from. Read from the loader rather than restated here, so this checks the key an operator
#: would actually verify with.
_COORDINATE = re.compile(r"RELEASE_PUBLIC_KEY_(?P<axis>[XY])=\"(?P<value>[0-9a-fA-F]{64})\"")

#: The origin the released loader is configured with. Also read from the loader: an artifact that
#: left that origin is the refusal this chain exists for, and hard-coding it here would let the two
#: drift apart without anything noticing.
_ORIGIN = re.compile(r"ORIGIN=\"\$\{AGENTNEXUS_ORIGIN:-(?P<origin>https://[A-Za-z0-9.:-]+)\}\"")


def loader_text() -> str:
    """Return the released loader, which carries both the key and the origin."""
    return RELEASED_LOADER.read_text(encoding="utf-8", errors="replace")


def stamped_key() -> tuple[str, str]:
    """Return the public P-256 coordinates an installation verifies the manifest with."""
    found = {
        match.group("axis"): match.group("value") for match in _COORDINATE.finditer(loader_text())
    }
    assert set(found) == {"X", "Y"}, "the released loader carries no stamped public key"
    return found["X"], found["Y"]


def origin() -> str:
    """Return the one origin the released loader accepts artifacts from."""
    match = _ORIGIN.search(loader_text())
    assert match is not None, "the released loader no longer declares its origin"
    return match.group("origin")


def manifest_bytes() -> bytes:
    """Return the published manifest exactly as it is served and signed."""
    return MANIFEST.read_bytes()


def signature_bytes() -> bytes:
    """Return the published signature, which is 64 raw bytes written as hex."""
    return bytes.fromhex(SIGNATURE.read_text(encoding="ascii").strip())


def flip(payload: bytes, index: int = 0) -> bytes:
    """Return the same bytes with one bit of one byte changed."""
    mutated = bytearray(payload)
    mutated[index] ^= 0x01
    return bytes(mutated)


def signed(document: dict[str, Any], key: ec.EllipticCurvePrivateKey) -> tuple[bytes, bytes]:
    """Return canonical manifest bytes and a raw r||s signature over exactly those bytes."""
    payload = canonical_bytes(document)
    der = key.sign(payload, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return payload, r.to_bytes(32, "big") + s.to_bytes(32, "big")


def coordinates(key: ec.EllipticCurvePrivateKey) -> tuple[str, str]:
    """Return an ephemeral key's public coordinates in the form a loader stamps."""
    numbers = key.public_key().public_numbers()
    return f"{numbers.x:064x}", f"{numbers.y:064x}"


@pytest.fixture
def ephemeral_key() -> ec.EllipticCurvePrivateKey:
    """Return a P-256 key that exists for one test and is never written anywhere."""
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def synthetic() -> dict[str, Any]:
    """Return a well-formed manifest for a fictional release on the loader's own origin."""
    return {
        "schema_version": 1,
        "connector_version": "9.9.9",
        "released_at": "2026-01-01T00:00:00Z",
        "artifacts": [
            {
                "platform": "any",
                "filename": "agentnexus_sdk-9.9.9-py3-none-any.whl",
                "url": f"{origin()}/connector/9.9.9/agentnexus_sdk-9.9.9-py3-none-any.whl",
                "size": 1024,
                "sha256": "0" * 64,
            }
        ],
    }


def test_ci_executes_this_guard() -> None:
    """A guard nobody runs is not a guard. The workflow has to name this file."""
    assert WORKFLOW.is_file(), "the workflow that must run this guard is missing"
    assert "ci/test_release_verification.py" in WORKFLOW.read_text(encoding="utf-8")


def test_the_published_release_verifies_from_published_data_alone() -> None:
    """The positive control: the real chain, verified the way an installation verifies it."""
    x, y = stamped_key()
    manifest = verify_manifest(
        manifest_bytes(), signature_bytes(), x_hex=x, y_hex=y, origin=origin()
    )

    artifact = manifest.artifact_for("linux-arm64")
    published = RELEASE / "connector" / manifest.connector_version / artifact.filename
    assert published.is_file(), f"{artifact.filename} is named by the manifest but not present"
    assert artifact.matches(published.read_bytes())


def test_a_mutated_manifest_is_refused() -> None:
    """One changed byte in the manifest breaks the signature, and the chain stops."""
    x, y = stamped_key()
    with pytest.raises(ReleaseError, match="signature"):
        verify_manifest(
            flip(manifest_bytes()), signature_bytes(), x_hex=x, y_hex=y, origin=origin()
        )


def test_a_mutated_signature_is_refused() -> None:
    """One changed byte in the signature is not a signature."""
    x, y = stamped_key()
    with pytest.raises(ReleaseError, match="signature"):
        verify_manifest(
            manifest_bytes(), flip(signature_bytes()), x_hex=x, y_hex=y, origin=origin()
        )


def test_a_truncated_signature_is_refused() -> None:
    """A short signature is refused by length before any curve arithmetic is attempted."""
    x, y = stamped_key()
    with pytest.raises(ReleaseError, match="64"):
        verify_manifest(manifest_bytes(), signature_bytes()[:32], x_hex=x, y_hex=y, origin=origin())


def test_another_key_cannot_verify_this_release(
    ephemeral_key: ec.EllipticCurvePrivateKey,
) -> None:
    """A correctly formed key that did not sign this manifest is still refused."""
    x, y = coordinates(ephemeral_key)
    with pytest.raises(ReleaseError, match="signature"):
        verify_manifest(manifest_bytes(), signature_bytes(), x_hex=x, y_hex=y, origin=origin())


def test_a_mutated_artifact_does_not_match_its_pinned_digest() -> None:
    """The second half of the chain: the bytes on disk have to be the bytes the manifest pins."""
    x, y = stamped_key()
    manifest = verify_manifest(
        manifest_bytes(), signature_bytes(), x_hex=x, y_hex=y, origin=origin()
    )
    artifact = manifest.artifact_for("linux-arm64")
    published = RELEASE / "connector" / manifest.connector_version / artifact.filename
    payload = published.read_bytes()

    assert artifact.matches(payload) is True
    assert artifact.matches(flip(payload, index=len(payload) // 2)) is False
    assert artifact.matches(payload + b"\x00") is False


def test_a_signed_manifest_may_not_leave_the_configured_origin(
    ephemeral_key: ec.EllipticCurvePrivateKey, synthetic: dict[str, Any]
) -> None:
    """A valid signature says who wrote a manifest, never where it may send an installer."""
    synthetic["artifacts"][0]["url"] = (
        "https://elsewhere.example/connector/9.9.9/agentnexus_sdk-9.9.9-py3-none-any.whl"
    )
    payload, signature = signed(synthetic, ephemeral_key)
    x, y = coordinates(ephemeral_key)

    with pytest.raises(ReleaseError, match="leaves the configured origin"):
        verify_manifest(payload, signature, x_hex=x, y_hex=y, origin=origin())


def test_a_signed_manifest_may_not_name_a_path(
    ephemeral_key: ec.EllipticCurvePrivateKey, synthetic: dict[str, Any]
) -> None:
    """A filename that could escape the install directory is refused, signature or not."""
    synthetic["artifacts"][0]["filename"] = "../../.ssh/authorized_keys"
    payload, signature = signed(synthetic, ephemeral_key)
    x, y = coordinates(ephemeral_key)

    with pytest.raises(ReleaseError, match="plain file name"):
        verify_manifest(payload, signature, x_hex=x, y_hex=y, origin=origin())


def test_the_signature_is_checked_before_the_document_is_read(
    ephemeral_key: ec.EllipticCurvePrivateKey, synthetic: dict[str, Any]
) -> None:
    """Order matters: an unsigned document must fail as unsigned, not as malformed.

    The manifest here is broken in a way the parser would report, and unsigned as well. If the
    refusal ever names the shape instead of the signature, the parser has been allowed to read
    bytes nobody vouched for — which is the ordering this chain depends on.
    """
    synthetic["artifacts"] = []
    payload = canonical_bytes(synthetic)
    x, y = coordinates(ephemeral_key)

    with pytest.raises(ReleaseError, match="signature"):
        verify_manifest(payload, b"\x00" * 64, x_hex=x, y_hex=y, origin=origin())


def test_a_re_serialised_manifest_is_refused(
    ephemeral_key: ec.EllipticCurvePrivateKey, synthetic: dict[str, Any]
) -> None:
    """A signature over canonical bytes may not bless a differently written document."""
    payload, signature = signed(synthetic, ephemeral_key)
    x, y = coordinates(ephemeral_key)
    padded = json.dumps(json.loads(payload), sort_keys=True, indent=2).encode("utf-8")

    assert padded != payload
    with pytest.raises(ReleaseError, match="signature"):
        verify_manifest(padded, signature, x_hex=x, y_hex=y, origin=origin())


def test_the_published_manifest_still_pins_a_size_and_a_digest() -> None:
    """The properties an installer can check for itself are present for every artifact."""
    document = json.loads(manifest_bytes())
    manifest = parse_manifest(document, origin=origin())

    assert manifest.artifacts
    for artifact in manifest.artifacts:
        assert isinstance(artifact, Artifact)
        assert artifact.size > 0
        assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256)
        assert artifact.url.startswith(f"{origin()}/")
