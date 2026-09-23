"""Connector release metadata: what a bootstrap is allowed to install, and how it knows.

A one-line bootstrap command is a supply-chain decision before it is a convenience. The loader an
applicant pipes into their shell is small and inspectable, but it is fetched fresh every time, so
it cannot be pinned by digest. What it *can* carry is a public key. Everything else follows from
that: the loader verifies a signed manifest with the key it was shipped with, and installs only
bytes the manifest pins by size and SHA-256.

**Why ECDSA P-256 here and Ed25519 everywhere else.** The agent protocol signs with Ed25519 and
that does not change. This is a different problem with a different constraint: the Windows
bootstrap has to verify a signature *before* it is allowed to install anything, using only what is
already on the machine. Windows PowerShell 5.1 runs on .NET Framework, which has no Ed25519 at
all — verifying one would mean shipping crypto code and trusting it before any verification had
happened, which is the wrong way round. It does have ECDSA P-256, natively, via `ECDsa.Create()`
and `ImportParameters`. So release signing uses the primitive the verifier already trusts, and the
two algorithms stay in their own layers: Ed25519 authenticates agents to the server, P-256
authenticates releases to installers.

Nothing in this module holds or needs a private key. Signing lives in
`scripts/build_release.py`, which an operator runs with a key that is never in this
repository; this module only describes and verifies.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

#: The manifest shape this code understands. A loader that meets a newer schema stops rather than
#: guessing: an unknown field could be the one that says "this artifact was revoked".
SCHEMA_VERSION: Final = 1

#: The signature is 64 raw bytes, `r || s`, each a 32-byte big-endian integer. Not DER: .NET's
#: `VerifyData` wants exactly this layout, and asking a bootstrap to parse DER before it has
#: verified anything is more attack surface for no benefit.
SIGNATURE_BYTES: Final = 64

#: An artifact may not exceed this. The bound is enforced before a download starts, so a manifest
#: that pointed at an enormous file could not exhaust an applicant's disk before the digest check
#: had a chance to reject it.
MAX_ARTIFACT_BYTES: Final = 64 * 1024 * 1024

#: Platform tokens an artifact may declare. `any` is honest rather than aspirational: the connector
#: is a pure-Python wheel today, so one artifact genuinely serves every platform. The list exists
#: so a future compiled artifact can be added without changing the format.
PLATFORMS: Final = frozenset({"any", "windows-x64", "linux-x64", "linux-arm64", "macos-arm64"})

_VERSION_PATTERN: Final = re.compile(r"^\d+\.\d+\.\d+(?:[.-][0-9A-Za-z.]+)?$")
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
#: A filename, not a path. A manifest that could name `../../authorized_keys` would be a manifest
#: that could write outside the install directory.
_FILENAME_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class ReleaseError(Exception):
    """Raised when release metadata cannot be trusted. Always fatal: there is no partial install."""


@dataclass(frozen=True, slots=True)
class Artifact:
    """One installable file, pinned by the two properties a downloader can check itself."""

    platform: str
    filename: str
    url: str
    size: int
    sha256: str

    def matches(self, payload: bytes) -> bool:
        """Return whether these bytes are exactly the artifact this entry pins."""
        return len(payload) == self.size and hashlib.sha256(payload).hexdigest() == self.sha256


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """A verified statement about one connector version."""

    connector_version: str
    released_at: str
    artifacts: tuple[Artifact, ...]

    def artifact_for(self, platform: str) -> Artifact:
        """Return the artifact for one platform, preferring an exact match over the portable one."""
        for candidate in self.artifacts:
            if candidate.platform == platform:
                return candidate
        for candidate in self.artifacts:
            if candidate.platform == "any":
                return candidate
        message = f"The manifest publishes no artifact for {platform!r}."
        raise ReleaseError(message)


def canonical_bytes(document: dict[str, Any]) -> bytes:
    """Return the exact bytes a signature covers.

    Sorted keys, no insignificant whitespace, UTF-8. Signing a canonical form rather than the file
    as written means a re-serialised manifest still verifies, and — more importantly — that the
    signer and the verifier cannot disagree about which bytes were meant.
    """
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def parse_manifest(document: dict[str, Any], *, origin: str) -> ReleaseManifest:
    """Validate a manifest's shape and refuse anything that could redirect an install.

    Shape validation is not signature validation and does not replace it. It runs anyway, because
    a correctly signed manifest with a URL pointing somewhere else is exactly the mistake a signing
    key protects against least: the key says "we published this", not "this is safe".
    """
    _require(isinstance(document, dict), "The manifest is not a JSON object.")
    _require(
        document.get("schema_version") == SCHEMA_VERSION,
        f"Unsupported manifest schema {document.get('schema_version')!r}; "
        f"this connector understands {SCHEMA_VERSION}.",
    )

    version = document.get("connector_version")
    _require(
        isinstance(version, str) and bool(_VERSION_PATTERN.match(version)),
        f"The manifest names an invalid connector version: {version!r}.",
    )
    released_at = document.get("released_at")
    _require(isinstance(released_at, str) and released_at.endswith("Z"), "released_at must be UTC.")

    raw_artifacts = document.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        message = "The manifest publishes no artifacts."
        raise ReleaseError(message)

    normalised_origin = origin.rstrip("/")
    artifacts: list[Artifact] = []
    seen: set[str] = set()
    for entry in raw_artifacts:
        _require(isinstance(entry, dict), "An artifact entry is not an object.")
        platform = entry.get("platform")
        _require(platform in PLATFORMS, f"Unknown artifact platform: {platform!r}.")
        _require(platform not in seen, f"The manifest lists {platform!r} twice.")
        seen.add(str(platform))

        filename = entry.get("filename")
        _require(
            isinstance(filename, str) and bool(_FILENAME_PATTERN.match(filename)),
            f"An artifact filename is not a plain file name: {filename!r}.",
        )

        url = entry.get("url")
        _require(isinstance(url, str), "An artifact url is missing.")
        # The signature proves who wrote the manifest, not where it may send the installer. An
        # artifact must live on the same origin the loader was configured with, so a stolen or
        # mis-issued signature still cannot point an install at another host.
        _require(
            isinstance(url, str) and url.startswith(f"{normalised_origin}/"),
            f"An artifact url leaves the configured origin {normalised_origin!r}: {url!r}.",
        )
        _require(".." not in str(url), f"An artifact url contains a traversal segment: {url!r}.")

        size = entry.get("size")
        _require(
            isinstance(size, int) and not isinstance(size, bool) and 0 < size <= MAX_ARTIFACT_BYTES,
            f"An artifact size is not within 1..{MAX_ARTIFACT_BYTES}: {size!r}.",
        )

        digest = entry.get("sha256")
        _require(
            isinstance(digest, str) and bool(_SHA256_PATTERN.match(digest)),
            f"An artifact sha256 is not a lowercase hex digest: {digest!r}.",
        )

        artifacts.append(
            Artifact(
                platform=str(platform),
                filename=str(filename),
                url=str(url),
                size=int(size),
                sha256=str(digest),
            )
        )

    return ReleaseManifest(
        connector_version=str(version),
        released_at=str(released_at),
        artifacts=tuple(artifacts),
    )


def load_public_key(x_hex: str, y_hex: str) -> ec.EllipticCurvePublicKey:
    """Build the release public key from the two coordinates a loader embeds.

    Coordinates rather than PEM on purpose: this is the one form both a Python verifier and a
    Windows PowerShell verifier can read without a parser, so the same key material is literally
    the same two strings in both loaders.
    """
    for label, value in (("x", x_hex), ("y", y_hex)):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value or ""):
            message = f"The release public key coordinate {label} is not 32 hex bytes."
            raise ReleaseError(message)
    numbers = ec.EllipticCurvePublicNumbers(int(x_hex, 16), int(y_hex, 16), ec.SECP256R1())
    try:
        return numbers.public_key()
    except ValueError as error:  # pragma: no cover - refused by the coordinate check above
        message = f"The release public key is not a valid P-256 point: {error}"
        raise ReleaseError(message) from error


def verify_signature(
    payload: bytes, signature: bytes, public_key: ec.EllipticCurvePublicKey
) -> None:
    """Refuse anything but a valid P-256/SHA-256 signature over exactly these bytes."""
    if len(signature) != SIGNATURE_BYTES:
        message = (
            f"The release signature is {len(signature)} bytes; expected {SIGNATURE_BYTES} "
            "raw r||s bytes."
        )
        raise ReleaseError(message)
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    try:
        public_key.verify(utils.encode_dss_signature(r, s), payload, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as error:
        message = "The release manifest signature does not verify against the embedded key."
        raise ReleaseError(message) from error


def verify_manifest(
    raw_manifest: bytes,
    signature: bytes,
    *,
    x_hex: str,
    y_hex: str,
    origin: str,
) -> ReleaseManifest:
    """Verify a downloaded manifest end to end, then return what it permits installing.

    Order matters and is deliberate: the signature is checked against the bytes as downloaded,
    before the document is interpreted at all, so a malformed document can never reach the parser
    on the strength of being well-formed JSON.
    """
    verify_signature(raw_manifest, signature, load_public_key(x_hex, y_hex))
    try:
        document = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = f"The release manifest is not valid UTF-8 JSON: {error}"
        raise ReleaseError(message) from error
    # Re-canonicalise and compare: a signature over canonical bytes must not be usable to bless a
    # differently-ordered or padded document that a lenient parser would read differently.
    if canonical_bytes(document) != raw_manifest:
        message = "The release manifest is not in canonical form; the signature covers other bytes."
        raise ReleaseError(message)
    return parse_manifest(document, origin=origin)
