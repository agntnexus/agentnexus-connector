"""Signers and private-key handling.

## The rule this module exists to enforce

AgentNexus never accepts, stores, or distributes private-key material. The private key belongs to
the agent's owner and stays on the owner's host. Nothing in this module sends a key anywhere, and
nothing here creates a key as a side effect of an import, a client construction, or a request.

Key material is kept out of every observable surface. `repr` is overridden, no exception message
interpolates a key, and the only function that can produce private-key bytes is named for what it
does and must be called deliberately.

## Why a protocol rather than a concrete class

`Signer` is a two-method protocol so that an in-memory key, an explicitly selected key file, and
a future hardware or remote signer are interchangeable without redesigning the HTTP client. The
client never sees key bytes; it sees `sign(message) -> bytes`.
"""

from __future__ import annotations

import base64
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

#: Raw Ed25519 key sizes, from RFC 8032.
PRIVATE_KEY_BYTES: Final = 32
PUBLIC_KEY_BYTES: Final = 32

#: File mode for a newly written private key on systems with POSIX permissions.
PRIVATE_KEY_FILE_MODE: Final = 0o600

WINDOWS_PERMISSION_WARNING: Final = (
    "This platform does not enforce POSIX file permissions. Windows inherits the directory ACL, "
    "so verify separately that only your account can read the key file, for example with "
    "'icacls <path>'."
)


class KeyHandlingError(Exception):
    """A private key could not be generated, written, or loaded.

    Deliberately not named `KeyError`: that builtin means something entirely different, and
    shadowing it in a `except` clause would be a trap.
    """


@runtime_checkable
class Signer(Protocol):
    """Anything that can produce an Ed25519 signature for the canonical string.

    An implementation must never expose the private key through this interface. The client only
    ever calls `sign`, and only ever reads `public_key_base64` for display or registration.
    """

    def sign(self, message: bytes) -> bytes:
        """Return the raw 64-byte Ed25519 signature over `message`."""
        ...

    @property
    def public_key_base64(self) -> str:
        """Return the base64 public key in the exact registration format."""
        ...


class Ed25519Signer:
    """A signer holding an Ed25519 private key in process memory.

    The key is never written anywhere by this class. Persisting it is a separate, explicit act
    through `write_private_key_file`.
    """

    __slots__ = ("_private_key",)

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        """Wrap an Ed25519 private key. The key is never copied out of this object.

        The runtime type check guards callers who are not type-checked; handing this class an
        RSA key would otherwise fail later, inside `sign`, with a far less obvious message.
        """
        if not isinstance(private_key, Ed25519PrivateKey):
            message = "Ed25519Signer requires an Ed25519 private key."  # type: ignore[unreachable]
            raise KeyHandlingError(message)
        self._private_key = private_key

    def sign(self, message: bytes) -> bytes:
        """Return the raw Ed25519 signature over the canonical string bytes."""
        return self._private_key.sign(message)

    @property
    def public_key_base64(self) -> str:
        """Return the base64 public key exactly as the registration path expects it."""
        return encode_public_key(self._private_key.public_key())

    @property
    def public_key_fingerprint(self) -> str:
        """Return the SHA-256 fingerprint of the raw public key, as the server reports it."""
        import hashlib

        raw = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        return hashlib.sha256(raw).hexdigest()

    def __repr__(self) -> str:
        """Return a representation that identifies the key without revealing it.

        A default dataclass or object repr in a traceback, a log line, or an interactive session
        is one of the most common ways private keys escape.
        """
        return f"Ed25519Signer(public_key_base64={self.public_key_base64!r})"

    def __str__(self) -> str:
        """Return the same redacted representation."""
        return repr(self)


@dataclass(frozen=True, slots=True)
class GeneratedKeyPair:
    """The result of an explicit key generation.

    Only the signer can produce a signature, and only `export_private_key_bytes` can produce the
    private bytes. The public key is here because the operator has to hand it over for
    registration.
    """

    signer: Ed25519Signer
    public_key_base64: str
    public_key_fingerprint: str


def generate_key_pair() -> GeneratedKeyPair:
    """Generate a new Ed25519 key pair in memory.

    Nothing is written to disk. The caller decides whether the key is used for one process or
    persisted through `write_private_key_file`.
    """
    signer = Ed25519Signer(Ed25519PrivateKey.generate())
    return GeneratedKeyPair(
        signer=signer,
        public_key_base64=signer.public_key_base64,
        public_key_fingerprint=signer.public_key_fingerprint,
    )


def encode_public_key(public_key: Ed25519PublicKey) -> str:
    """Return the base64 raw public key in the registration format."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def export_private_key_bytes(signer: Ed25519Signer) -> bytes:
    """Return the raw 32-byte private key.

    This exists for the one legitimate case — writing a key the operator asked to persist — and
    is deliberately verbose to call. Nothing in the client, the CLI's default output, or any
    error path uses it.
    """
    # Reaching into the private attribute is intentional and confined to this function, which is
    # the single documented export point.
    key: Ed25519PrivateKey = signer._private_key
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def write_private_key_file(signer: Ed25519Signer, destination: Path) -> Path:
    """Write the private key to a file the operator explicitly selected.

    The file is created **exclusively**: if the path already exists the call fails rather than
    overwriting, because silently replacing a key file destroys the only copy of a credential.

    On platforms with POSIX permissions the file is created with mode 600 and the mode is
    verified after writing. Windows does not enforce those bits; the caller is expected to
    surface `WINDOWS_PERMISSION_WARNING` so the operator checks the ACL separately.
    """
    destination = Path(destination)
    if destination.exists():
        message = (
            f"{destination} already exists. Refusing to overwrite a private key: "
            "choose a new path, or remove the old file deliberately."
        )
        raise KeyHandlingError(message)
    destination.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, PRIVATE_KEY_FILE_MODE)
    try:
        os.write(descriptor, base64.b64encode(export_private_key_bytes(signer)) + b"\n")
    finally:
        os.close(descriptor)

    if _supports_posix_permissions():
        os.chmod(destination, PRIVATE_KEY_FILE_MODE)
        mode = stat.S_IMODE(destination.stat().st_mode)
        if mode != PRIVATE_KEY_FILE_MODE:  # pragma: no cover - depends on the filesystem
            message = (
                f"{destination} was created with mode {mode:o} instead of "
                f"{PRIVATE_KEY_FILE_MODE:o}. Refusing to leave a private key readable by others."
            )
            destination.unlink(missing_ok=True)
            raise KeyHandlingError(message)
    return destination


def load_private_key_file(source: Path) -> Ed25519Signer:
    """Load a signer from an explicitly selected private-key file.

    The path is always explicit. There is no search path, no default location, and no environment
    variable that this function consults on its own: a client that discovers keys is a client
    that can be pointed at the wrong one.
    """
    source = Path(source)
    try:
        content = source.read_bytes()
    except OSError as error:
        message = f"Cannot read the private-key file at {source}: {error.strerror}."
        raise KeyHandlingError(message) from None

    raw = _decode_private_key(content, source=source)
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError:
        message = f"The file at {source} does not contain a valid Ed25519 private key."
        raise KeyHandlingError(message) from None
    return Ed25519Signer(private_key)


def public_key_file_warning() -> str | None:
    """Return the platform warning for private-key files, or None when permissions are enforced."""
    return None if _supports_posix_permissions() else WINDOWS_PERMISSION_WARNING


def _decode_private_key(content: bytes, *, source: Path) -> bytes:
    """Accept either raw 32 bytes or base64 text, and nothing else."""
    if len(content) == PRIVATE_KEY_BYTES:
        return content
    text = content.strip()
    try:
        decoded = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        message = (
            f"The file at {source} is neither 32 raw bytes nor base64 Ed25519 private-key material."
        )
        raise KeyHandlingError(message) from None
    if len(decoded) != PRIVATE_KEY_BYTES:
        message = (
            f"The file at {source} decodes to {len(decoded)} bytes; an Ed25519 private key is "
            f"{PRIVATE_KEY_BYTES} bytes."
        )
        raise KeyHandlingError(message)
    return decoded


def _supports_posix_permissions() -> bool:
    """Return whether the platform enforces POSIX file modes."""
    return sys.platform != "win32"
