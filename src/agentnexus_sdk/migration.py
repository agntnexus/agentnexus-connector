"""Move one agent profile to another computer, in a single encrypted file.

This is C4. The case it exists for is concrete: an agent set up on a Windows machine has to run on
a Raspberry Pi instead, keeping its agent id, its handle and its signing key, without re-registering
and without spending a second invitation.

## Why a profile directory is not the answer

`PERSISTENT_AGENT_IDENTITY.md` rule 2 says a profile directory must not be presented as portable
backup material, and rule 3 says two simultaneously active copies of one private key are an
operational error. Both are why this module exists rather than a documented `robocopy`:

* a profile directory holds absolute paths from the machine it was created on, a runtime home, a
  virtual environment's backups and staging files. Copying it moves things that are meaningless or
  actively wrong on the destination;
* it holds the private key in the clear. Anything that carries it between machines has to be
  encrypted for the whole journey, including whatever USB stick or share it crosses.

So this exports a **chosen** set of profile-owned data, never a directory, and the file it produces
is authenticated and encrypted as a whole.

## What this cannot do, and says so

Copying a key creates a second usable copy. Nothing here disables the source: there is no
`disconnect`, no revoke, no delete, and no attempt to detect that the old machine is still running.
The cutover is the operator's, and both the export and the import say so in as many words. Claiming
otherwise would be claiming an enforcement this does not have.

## The format

One file. `AGENTNEXUS-PROFILE-EXPORT-1`, a length-prefixed JSON header naming the KDF parameters
and the nonce, and then one AES-256-GCM ciphertext over a ZIP built entirely in memory. The header
is the AEAD's associated data, so the parameters cannot be weakened without breaking the tag, and
the ZIP is never written to disk unencrypted at any point.

The ZIP is the *inner* container only. What is transferred is the sealed file; a plain ZIP holding
a private key is exactly what this is designed not to produce.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import secrets
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from agentnexus_sdk.signing import (
    KeyHandlingError,
    load_private_key_file,
    write_private_key_file,
)
from agentnexus_sdk.version import __version__

# ---------------------------------------------------------------------------------------------
# The file format
# ---------------------------------------------------------------------------------------------

#: Identifies the file and its format in one line, before anything is parsed.
ARCHIVE_MAGIC: Final = b"AGENTNEXUS-PROFILE-EXPORT-1\n"

#: The version inside the header. Bumped when the *contents* change meaning, not when a field is
#: added; an importer that does not know a version refuses rather than guessing a migration.
FORMAT_VERSION: Final = 1

#: scrypt, at parameters a Raspberry Pi 3 can afford.
#:
#: Chosen over Argon2id for one reason that matters more than the marginal difference between two
#: good memory-hard functions: `cryptography` is already a pinned dependency of this package, and
#: it provides scrypt through OpenSSL on every platform this runs on. Argon2id would mean adding
#: `argon2-cffi` to a library other people install beside their own dependencies, and a wheel that
#: has to build on ARM, to replace an RFC 7914 function that is already there.
#:
#: n = 2^15 with r = 8 needs about 32 MB. A Pi 3 has 1 GB, so this is comfortable there and cheap
#: on a laptop; the parameters travel in the header, so raising them later does not strand a file
#: written today.
KDF_NAME: Final = "scrypt"
KDF_N: Final = 1 << 15
KDF_R: Final = 8
KDF_P: Final = 1
KEY_BYTES: Final = 32
SALT_BYTES: Final = 16
NONCE_BYTES: Final = 12
CIPHER_NAME: Final = "AES-256-GCM"

#: Bounds. Every one of them is checked before the thing it bounds is allocated or extracted.
MAX_HEADER_BYTES: Final = 4096
MAX_ARCHIVE_BYTES: Final = 8 * 1024 * 1024
MAX_MEMBER_BYTES: Final = 1024 * 1024
MAX_TOTAL_MEMBER_BYTES: Final = 4 * 1024 * 1024
MAX_MEMBERS: Final = 8
#: A member that expands more than this from its stored size is refused unread.
MAX_COMPRESSION_RATIO: Final = 200

#: The complete set of names a valid archive may contain. Exact names, not a pattern.
#:
#: This is the whole path defence. There is no prefix check to get subtly wrong, no normalisation
#: to disagree with the filesystem's, and no rule about `..` — a name is one of these three strings
#: or the archive is refused. Traversal, absolute paths, drive letters, backslashes, NTFS streams,
#: reserved device names and case tricks all fail the same comparison.
MEMBER_MANIFEST: Final = "manifest.json"
MEMBER_KEY: Final = "identity/agent.pem"
MEMBER_SOUL: Final = "soul/SOUL.md"
ALLOWED_MEMBERS: Final = frozenset({MEMBER_MANIFEST, MEMBER_KEY, MEMBER_SOUL})
REQUIRED_MEMBERS: Final = frozenset({MEMBER_MANIFEST, MEMBER_KEY})

#: What is deliberately left behind, shown to the operator before either half runs.
#:
#: Named rather than implied. An operator deciding whether this is safe needs to see the list, and
#: an operator wondering why their agent has no model on the new machine needs it too.
EXCLUDED_CATEGORIES: Final = (
    "provider credentials and API keys — configure the provider again on the destination",
    "runtime configuration files, which can embed those credentials",
    "the virtual environment, executables and generated launchers — recreated by the installer",
    "runtime registrations and every absolute path from the source computer",
    "scheduled tasks, cron entries, hooks and plugins",
    "runtime memory and conversation history — no adapter contract exists for reading it",
    "backups, staging files and lock files",
)

#: The warning that has to be true on both ends of the transfer.
SECOND_COPY_WARNING: Final = (
    "Exporting copies the private key. After an import there are two files able to sign as this "
    "agent, and nothing here disables the first one: no revoke, no disconnect, no deletion, and "
    "no check that the old machine has stopped. Running both at once is an operational error you "
    "have to prevent yourself — stop the agent and any scheduled jobs on the source computer "
    "before you start it on the destination."
)


class MigrationError(Exception):
    """An export or import refused, with something the operator can act on."""

    def __init__(self, message: str, *, recovery: str | None = None) -> None:
        """Carry the recovery step beside the failure, the way the connector's errors do."""
        super().__init__(message)
        self.recovery = recovery


# ---------------------------------------------------------------------------------------------
# Sealing and opening
# ---------------------------------------------------------------------------------------------


def derive_key(password: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    """Stretch the password into an AES key with scrypt.

    The parameters are arguments rather than constants because they arrive from the file's header
    when opening. They are validated by the caller before they reach here: an attacker who could
    choose them could ask for `n = 1`.
    """
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=KEY_BYTES, n=n, r=r, p=p).derive(password.encode("utf-8"))


def _header_bytes(header: dict[str, Any]) -> bytes:
    """Serialise the header canonically, so what is authenticated is exactly what is read back."""
    return json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def seal(plaintext: bytes, password: str) -> bytes:
    """Encrypt one archive into the transferable file.

    The header travels in the clear — it has to, since the reader needs the salt and the KDF
    parameters before it can derive anything — but it is the AEAD's associated data, so altering
    `n`, the salt or the nonce breaks authentication rather than weakening the key.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(plaintext) > MAX_ARCHIVE_BYTES:
        message = f"The archive is {len(plaintext)} bytes; the maximum is {MAX_ARCHIVE_BYTES}."
        raise MigrationError(
            message, recovery="Nothing was written. Export fewer or smaller components."
        )
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = {
        "format": FORMAT_VERSION,
        "kdf": KDF_NAME,
        "n": KDF_N,
        "r": KDF_R,
        "p": KDF_P,
        "salt": salt.hex(),
        "cipher": CIPHER_NAME,
        "nonce": nonce.hex(),
    }
    raw_header = _header_bytes(header)
    if len(raw_header) > MAX_HEADER_BYTES:  # pragma: no cover - the header is a fixed shape
        message = "The archive header is unexpectedly large."
        raise MigrationError(message)
    prefix = ARCHIVE_MAGIC + len(raw_header).to_bytes(4, "big") + raw_header
    key = derive_key(password, salt, n=KDF_N, r=KDF_R, p=KDF_P)
    return prefix + AESGCM(key).encrypt(nonce, plaintext, prefix)


def open_sealed(blob: bytes, password: str) -> bytes:
    """Authenticate and decrypt a transferable file, or refuse.

    A wrong password and a tampered file are the same event here, and that is deliberate: GCM
    authenticates before it returns anything, so neither produces a partial archive to act on. The
    message says both possibilities rather than guessing which one happened.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not blob.startswith(ARCHIVE_MAGIC):
        message = "This file is not an AgentNexus profile export."
        raise MigrationError(
            message,
            recovery=("Check the path. A file from a different version says so on its first line."),
        )
    cursor = len(ARCHIVE_MAGIC)
    if len(blob) < cursor + 4:
        message = "The export file is truncated: it ends before its header length."
        raise MigrationError(message, recovery="Transfer the file again.")
    header_length = int.from_bytes(blob[cursor : cursor + 4], "big")
    if header_length > MAX_HEADER_BYTES:
        message = (
            f"The export header claims {header_length} bytes; the maximum is {MAX_HEADER_BYTES}."
        )
        raise MigrationError(message, recovery="Nothing was read. Treat this file as damaged.")
    cursor += 4
    raw_header = blob[cursor : cursor + header_length]
    if len(raw_header) != header_length:
        message = "The export file is truncated: its header is shorter than it claims."
        raise MigrationError(message, recovery="Transfer the file again.")
    cursor += header_length

    try:
        header = json.loads(raw_header)
    except ValueError as error:
        message = f"The export header is not readable JSON: {error}"
        raise MigrationError(message, recovery="Treat this file as damaged.") from error
    if not isinstance(header, dict):
        message = "The export header is not an object."
        raise MigrationError(message, recovery="Treat this file as damaged.")

    _require_supported_header(header)
    salt = _hex_field(header, "salt", SALT_BYTES)
    nonce = _hex_field(header, "nonce", NONCE_BYTES)
    ciphertext = blob[cursor:]
    if not ciphertext:
        message = "The export file has no encrypted content."
        raise MigrationError(message, recovery="Transfer the file again.")
    if len(ciphertext) > MAX_ARCHIVE_BYTES + 64:
        message = (
            f"The export file is {len(ciphertext)} bytes of ciphertext, which is over the limit."
        )
        raise MigrationError(message, recovery="Nothing was decrypted.")

    key = derive_key(password, salt, n=header["n"], r=header["r"], p=header["p"])
    prefix = blob[: len(ARCHIVE_MAGIC) + 4 + header_length]
    try:
        return bytes(AESGCM(key).decrypt(nonce, ciphertext, prefix))
    except InvalidTag as error:
        message = "The export file could not be decrypted."
        raise MigrationError(
            message,
            recovery=(
                "Either the password is wrong or the file has been altered since it was written. "
                "Nothing was imported, and nothing on this computer was changed."
            ),
        ) from error


def _require_supported_header(header: dict[str, Any]) -> None:
    """Refuse a header this build does not understand, before deriving anything from it.

    The parameter bounds are the point. An importer that took `n` from the file would let whoever
    wrote the file choose how hard the password is to guess.
    """
    if header.get("format") != FORMAT_VERSION:
        found = header.get("format")
        message = f"This export is format {found!r}; this connector reads {FORMAT_VERSION}."
        raise MigrationError(
            message,
            recovery="Import it with the connector version that wrote it, or export it again.",
        )
    if header.get("kdf") != KDF_NAME or header.get("cipher") != CIPHER_NAME:
        message = (
            f"This export uses {header.get('kdf')!r}/{header.get('cipher')!r}, which this "
            "connector does not implement."
        )
        raise MigrationError(message, recovery="Nothing was decrypted.")
    if (header.get("n"), header.get("r"), header.get("p")) != (KDF_N, KDF_R, KDF_P):
        message = "This export declares key-derivation parameters this connector does not accept."
        raise MigrationError(
            message,
            recovery=(
                "Parameters weaker than the ones this writes are refused rather than honoured. "
                "Nothing was decrypted."
            ),
        )


def _hex_field(header: dict[str, Any], name: str, length: int) -> bytes:
    """Read one fixed-length hex field out of the header, or refuse."""
    value = header.get(name)
    if not isinstance(value, str):
        message = f"The export header has no {name}."
        raise MigrationError(message, recovery="Treat this file as damaged.")
    try:
        raw = bytes.fromhex(value)
    except ValueError as error:
        message = f"The export header's {name} is not hex."
        raise MigrationError(message, recovery="Treat this file as damaged.") from error
    if len(raw) != length:
        message = f"The export header's {name} is {len(raw)} bytes; {length} are required."
        raise MigrationError(message, recovery="Treat this file as damaged.")
    return raw


# ---------------------------------------------------------------------------------------------
# What travels
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    """One file that will be, or was, inside the archive."""

    name: str
    category: str
    payload: bytes

    @property
    def digest(self) -> str:
        """The SHA-256 of exactly these bytes, recorded in the manifest and rechecked on import."""
        return hashlib.sha256(self.payload).hexdigest()


@dataclass
class ExportPlan:
    """Everything that will go into an archive, decided before anything is written.

    Built first and shown to the operator first. What is included and what is left out is a
    decision they need to see, not discover afterwards on a machine with no model configured.
    """

    profile: str
    identity: dict[str, str]
    endpoints: dict[str, str]
    runtimes: list[str]
    source_system: str
    components: list[Component] = field(default_factory=list)
    #: What was looked for and not found, or found and deliberately left out, in this profile.
    notes: list[str] = field(default_factory=list)

    @property
    def included_names(self) -> list[str]:
        """Every member name that will be in the archive, manifest included."""
        return sorted([MEMBER_MANIFEST, *(component.name for component in self.components)])


def plan_export(
    *,
    paths: Any,
    environment: Any,
    include_soul: bool = True,
) -> ExportPlan:
    """Decide what this profile can carry, reading only files it owns.

    Nothing is copied wholesale. Each component is named, read individually and recorded, so
    "what is in the archive" is a list somebody wrote rather than whatever happened to be in a
    directory.

    The identity is rebuilt from the profile's own records rather than by shipping `state.json`
    and `profile.json` as files: those carry absolute paths from this computer, which are wrong on
    the destination and must not travel. The destination writes its own.
    """
    from agentnexus_sdk.connector import State
    from agentnexus_sdk.profiles import ProfileError, ProfileRecord

    try:
        record = ProfileRecord.load(paths.profile_record)
    except ProfileError as error:
        raise MigrationError(str(error), recovery=error.recovery) from error
    if record is None:
        message = f"There is no completed {paths.profile!r} profile to export."
        raise MigrationError(
            message,
            recovery="Run `agentnexus-connector profile list` to see what is on this computer.",
        )
    state = State.load(paths.state_file)
    if not state.agent_id or not state.key_id:
        message = f"The {paths.profile!r} profile records no registered identity."
        raise MigrationError(
            message,
            recovery=(
                "Only a profile that finished registering can be moved. Nothing was written."
            ),
        )

    try:
        signer = load_private_key_file(paths.private_key)
    except KeyHandlingError as error:
        message = f"The private key for {paths.profile!r} could not be read: {error}"
        raise MigrationError(message, recovery="Nothing was written.") from error

    plan = ExportPlan(
        profile=paths.profile,
        identity={
            "agent_id": state.agent_id,
            "key_id": state.key_id,
            "handle": state.handle or "",
            "public_key_base64": signer.public_key_base64,
            "public_key_fingerprint": signer.public_key_fingerprint,
        },
        endpoints={
            "onboarding_base_url": str(record.endpoints.get("onboarding_base_url", "")),
            "agent_api_url": str(record.endpoints.get("agent_api_url", "")),
            "public_api_url": str(record.endpoints.get("public_api_url", "")),
            "observer_url": str(record.endpoints.get("observer_url", "")),
        },
        runtimes=sorted(set(state.runtimes)),
        source_system=environment.system,
    )
    plan.components.append(
        Component(
            name=MEMBER_KEY,
            category="identity",
            payload=paths.private_key.read_bytes(),
        )
    )

    if include_soul:
        _add_soul(plan, paths=paths, environment=environment)
    else:
        plan.notes.append("soul: not requested (--no-soul)")
    return plan


def _add_soul(plan: ExportPlan, *, paths: Any, environment: Any) -> None:
    """Add the instruction document, but only where a runtime actually publishes where it lives.

    Hermes does: `hermes profile show <name>` reports the directory and confirms `SOUL.md`.
    OpenClaw does not, and its adapter refuses rather than guessing — so an OpenClaw profile's soul
    is not exported, and the note says that rather than leaving the operator to notice.
    """
    from agentnexus_sdk.runtimes import ADAPTERS, RuntimeIntegrationError

    context = paths.runtime_context()
    for name in plan.runtimes or sorted(ADAPTERS):
        factory = ADAPTERS.get(name)
        if factory is None:
            continue
        adapter = factory(which=environment.which, runner=environment.run, context=context)
        if not adapter.detect().installed:
            plan.notes.append(f"soul: {name} is not installed here, so its soul was not read")
            continue
        try:
            location = adapter.soul_location()
        except (RuntimeIntegrationError, NotImplementedError) as error:
            plan.notes.append(f"soul: {name} publishes no location for it ({error})")
            continue
        if not location.path.is_file():
            plan.notes.append(f"soul: {name} has no {location.filename} for this profile")
            continue
        payload = location.path.read_bytes()
        if len(payload) > MAX_MEMBER_BYTES:
            plan.notes.append(
                f"soul: {location.path} is {len(payload)} bytes, over the {MAX_MEMBER_BYTES} limit"
            )
            continue
        plan.components.append(Component(name=MEMBER_SOUL, category="soul", payload=payload))
        plan.notes.append(f"soul: read from {name} at {location.path}")
        return


def build_manifest(plan: ExportPlan) -> dict[str, Any]:
    """Describe the archive's own contents, for an importer that trusts none of it yet."""
    return {
        "format": FORMAT_VERSION,
        "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "system": plan.source_system,
            "connector_version": __version__,
            "runtimes": plan.runtimes,
            "profile": plan.profile,
        },
        "identity": dict(plan.identity),
        "endpoints": dict(plan.endpoints),
        "included": [
            {
                "name": component.name,
                "category": component.category,
                "size": len(component.payload),
                "sha256": component.digest,
            }
            for component in sorted(plan.components, key=lambda item: item.name)
        ],
        "excluded": list(EXCLUDED_CATEGORIES),
        "notes": list(plan.notes),
    }


def build_archive(plan: ExportPlan) -> bytes:
    """Build the inner ZIP in memory. It is never a file on disk in this form.

    Deterministic timestamps and stored order, so two exports of an unchanged profile differ only
    in the manifest's `created_at`, the salt and the nonce.
    """
    manifest = json.dumps(build_manifest(plan), indent=2, sort_keys=True).encode("utf-8")
    members = [Component(name=MEMBER_MANIFEST, category="manifest", payload=manifest)]
    members.extend(sorted(plan.components, key=lambda item: item.name))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            info = zipfile.ZipInfo(member.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # 0o600, as a regular file. The importer does not trust this, but writing something
            # sensible costs nothing and a plain reader of the inner ZIP sees the intent.
            info.external_attr = (0o100600) << 16
            archive.writestr(info, member.payload)
    return buffer.getvalue()


# ---------------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    """Where the archive went, and what went into it."""

    path: Path
    plan: ExportPlan
    size: int


def export_profile(
    *,
    install_root: Path,
    profile: str,
    destination: Path,
    password: str,
    environment: Any,
    include_soul: bool = True,
) -> ExportResult:
    """Write one profile into one encrypted file, changing nothing on this computer.

    Held under the profile's lock, so a `setup` or an `update apply` cannot rewrite the records
    half way through reading them. **That lock does not stop a running agent**, and it does not
    stop a scheduled job: the operating system gives no way to prove which process owns a profile.
    An operator who wants a consistent export stops the agent and its scheduled jobs first, which
    the CLI says before it starts.

    The source is left exactly as it was. There is no disconnect, no revoke and no deletion here,
    deliberately: recovery from a failed migration is "keep using the old machine".
    """
    from agentnexus_sdk.connector import Paths
    from agentnexus_sdk.profiles import ProfileError, profile_lock

    destination = Path(destination)
    if destination.exists():
        message = f"{destination} already exists."
        raise MigrationError(
            message,
            recovery=(
                "Refusing to overwrite it: an export file holds a private key and the one that "
                "is there may be the only copy of something. Choose another path."
            ),
        )

    try:
        with profile_lock(install_root, profile):
            paths = Paths.for_profile(install_root, profile)
            if not paths.root.is_dir():
                message = f"There is no {profile!r} profile in {install_root}."
                raise MigrationError(
                    message, recovery="Run `agentnexus-connector profile list` to see what is here."
                )
            plan = plan_export(paths=paths, environment=environment, include_soul=include_soul)
            sealed = seal(build_archive(plan), password)
    except ProfileError as error:
        raise MigrationError(str(error), recovery=error.recovery) from error

    _write_private_file(destination, sealed)
    return ExportResult(path=destination, plan=plan, size=len(sealed))


def _write_private_file(destination: Path, payload: bytes) -> None:
    """Write the sealed archive, created exclusively and not readable by others where that holds.

    Exclusive creation matters for the same reason it does for a key file: this one carries a key
    too, and silently replacing somebody's export is destroying a credential.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    # `os.O_BINARY` matters and is not optional. On Windows `os.open` defaults to *text* mode, so
    # every 0x0A in the payload is written as 0x0D 0x0A — which corrupted the magic line, the
    # header and the ciphertext alike, and made every export written on Windows unreadable
    # everywhere including on the machine that wrote it. The constant does not exist on POSIX,
    # where there is nothing to translate.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as error:
        message = f"{destination} could not be created: {error}"
        raise MigrationError(message, recovery="Nothing was written.") from error
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------------------------
# Reading an archive, which is untrusted until every one of these checks passes
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArchiveContents:
    """A decrypted, validated archive. Nothing has been written when this exists."""

    manifest: dict[str, Any]
    members: dict[str, bytes]

    @property
    def identity(self) -> dict[str, str]:
        """The agent id, key id, handle and public key this archive claims to carry."""
        return {str(k): str(v) for k, v in dict(self.manifest.get("identity") or {}).items()}

    @property
    def endpoints(self) -> dict[str, str]:
        """The addresses the source profile used."""
        return {str(k): str(v) for k, v in dict(self.manifest.get("endpoints") or {}).items()}

    @property
    def source(self) -> dict[str, Any]:
        """What the archive says about where it came from."""
        return dict(self.manifest.get("source") or {})


def read_archive(blob: bytes, password: str) -> ArchiveContents:
    """Decrypt and fully validate an archive without writing anything.

    Order matters and is the whole defence. Authentication first, so a tampered file never reaches
    the ZIP parser; then structural checks on every entry *before* any of them is decompressed;
    then the manifest; then the key. Only a caller holding one of these has anything to write.
    """
    plaintext = open_sealed(blob, password)
    members = _read_members(plaintext)
    manifest = _read_manifest(members)
    _require_recorded_digests(manifest, members)
    _require_identity(manifest, members)
    return ArchiveContents(manifest=manifest, members=members)


def _read_members(plaintext: bytes) -> dict[str, bytes]:
    """Extract the archive's members, refusing anything that is not exactly what is expected."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(plaintext))
    except zipfile.BadZipFile as error:
        message = "The decrypted archive is not a readable container."
        raise MigrationError(
            message, recovery="Nothing was imported. Export the profile again."
        ) from error

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            message = f"The archive holds {len(infos)} entries; the maximum is {MAX_MEMBERS}."
            raise MigrationError(message, recovery="Nothing was imported.")

        seen: dict[str, str] = {}
        total = 0
        for info in infos:
            _require_plain_member(info)
            if info.filename not in ALLOWED_MEMBERS:
                message = f"The archive contains {info.filename!r}, which is not part of an export."
                raise MigrationError(
                    message,
                    recovery=(
                        "Nothing was imported. Only "
                        + ", ".join(sorted(ALLOWED_MEMBERS))
                        + " are accepted."
                    ),
                )
            folded = info.filename.casefold()
            if folded in seen:
                message = (
                    f"The archive names {info.filename!r} and {seen[folded]!r}, which collide."
                )
                raise MigrationError(
                    message,
                    recovery=(
                        "Nothing was imported. Two entries differing only in case would resolve "
                        "to one file on Windows and macOS."
                    ),
                )
            seen[folded] = info.filename
            total += info.file_size
            if total > MAX_TOTAL_MEMBER_BYTES:
                message = f"The archive expands to more than {MAX_TOTAL_MEMBER_BYTES} bytes."
                raise MigrationError(message, recovery="Nothing was imported or decompressed.")

        missing = REQUIRED_MEMBERS - set(seen.values())
        if missing:
            message = f"The archive is missing {', '.join(sorted(missing))}."
            raise MigrationError(message, recovery="Nothing was imported.")

        return {info.filename: archive.read(info.filename) for info in infos}


def _require_plain_member(info: zipfile.ZipInfo) -> None:
    """Refuse anything that is not an ordinary, sanely sized, sanely compressed file.

    A ZIP entry can describe a symlink, a directory, a device node or a file that expands to a
    thousand times its stored size. None of those is a thing a profile export contains, so each is
    a refusal rather than a special case to handle.
    """
    if info.is_dir():
        message = f"The archive contains a directory entry, {info.filename!r}."
        raise MigrationError(message, recovery="Nothing was imported.")
    mode = (info.external_attr >> 16) & 0o170000
    if mode not in (0, 0o100000):
        kind = "a symbolic link" if mode == 0o120000 else "a special file"
        message = f"The archive entry {info.filename!r} describes {kind}."
        raise MigrationError(
            message,
            recovery="Nothing was imported. A profile export contains ordinary files only.",
        )
    if info.file_size > MAX_MEMBER_BYTES:
        message = f"The archive entry {info.filename!r} is {info.file_size} bytes, over the limit."
        raise MigrationError(message, recovery="Nothing was imported or decompressed.")
    ratio = info.file_size / max(info.compress_size, 1)
    if info.compress_size and ratio > MAX_COMPRESSION_RATIO:
        message = f"The archive entry {info.filename!r} expands {ratio:.0f}x when decompressed."
        raise MigrationError(
            message, recovery="Nothing was decompressed. Treat this file as hostile."
        )


def _read_manifest(members: dict[str, bytes]) -> dict[str, Any]:
    """Parse the manifest and check it describes something this build can import."""
    try:
        manifest = json.loads(members[MEMBER_MANIFEST].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        message = f"The archive's manifest is unreadable: {error}"
        raise MigrationError(message, recovery="Nothing was imported.") from error
    if not isinstance(manifest, dict):
        message = "The archive's manifest is not an object."
        raise MigrationError(message, recovery="Nothing was imported.")
    if manifest.get("format") != FORMAT_VERSION:
        message = (
            f"The archive's manifest is format {manifest.get('format')!r}, not {FORMAT_VERSION}."
        )
        raise MigrationError(
            message,
            recovery=(
                "This connector will not guess a migration between formats. Import it with the "
                "version that wrote it."
            ),
        )
    return manifest


def _require_recorded_digests(manifest: dict[str, Any], members: dict[str, bytes]) -> None:
    """Check every member against the digest the manifest recorded for it.

    The AEAD already proves the file as a whole was not altered. This proves something different:
    that the manifest and the members agree with each other, which catches a mis-built archive
    rather than a hostile one.
    """
    recorded = manifest.get("included")
    if not isinstance(recorded, list):
        message = "The archive's manifest lists no contents."
        raise MigrationError(message, recovery="Nothing was imported.")
    described = {}
    for entry in recorded:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            message = "The archive's manifest describes an entry that has no name."
            raise MigrationError(message, recovery="Nothing was imported.")
        described[entry["name"]] = entry

    payloads = {name: payload for name, payload in members.items() if name != MEMBER_MANIFEST}
    if set(described) != set(payloads):
        message = (
            "The archive's manifest and its contents disagree: "
            f"manifest {sorted(described)}, archive {sorted(payloads)}."
        )
        raise MigrationError(message, recovery="Nothing was imported.")
    for name, payload in payloads.items():
        if hashlib.sha256(payload).hexdigest() != described[name].get("sha256"):
            message = f"The archive entry {name!r} does not match the digest its manifest records."
            raise MigrationError(message, recovery="Nothing was imported.")


def signer_from_bytes(payload: bytes) -> Any:
    """Load a signer from key bytes held in memory.

    `load_private_key_file` takes a path by design: it is the single documented entry point and
    has no in-memory sibling, and adding one to the signing module for this would widen the
    surface that can produce a key object. The bytes are already decrypted in this process either
    way, so what matters is not leaving them behind — the file is created privately and removed in
    `finally` on every path, including the failure ones.
    """
    import tempfile

    handle, temporary = tempfile.mkstemp(prefix="agentnexus-import-")
    try:
        with os.fdopen(handle, "wb") as sink:
            sink.write(payload)
        return load_private_key_file(Path(temporary))
    except KeyHandlingError as error:
        message = f"The archive's private key is not usable: {error}"
        raise MigrationError(message, recovery="Nothing was imported.") from error
    finally:
        Path(temporary).unlink(missing_ok=True)


def _require_identity(manifest: dict[str, Any], members: dict[str, bytes]) -> None:
    """Prove the key in the archive is the identity the manifest claims.

    A manifest is a claim; the key is the thing. Loading it establishes it is a usable Ed25519
    private key at all, and comparing the fingerprint establishes that the archive was not
    assembled from one agent's records and another agent's key.
    """
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        message = "The archive's manifest carries no identity."
        raise MigrationError(message, recovery="Nothing was imported.")
    for required in ("agent_id", "key_id", "public_key_fingerprint"):
        if not isinstance(identity.get(required), str) or not identity[required]:
            message = f"The archive's manifest has no {required}."
            raise MigrationError(message, recovery="Nothing was imported.")

    signer = signer_from_bytes(members[MEMBER_KEY])
    if signer.public_key_fingerprint != identity["public_key_fingerprint"]:
        message = "The archive's key does not match the identity its manifest describes."
        raise MigrationError(
            message,
            recovery=(
                "Nothing was imported. This archive was assembled from records and a key that do "
                "not belong together."
            ),
        )


# ---------------------------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------------------------


@dataclass
class ImportResult:
    """What was created, and what the operator still has to do."""

    profile: str
    root: Path
    registered: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def import_profile(
    *,
    install_root: Path,
    profile: str,
    contents: ArchiveContents,
    environment: Any,
    agent_api_url: str | None = None,
) -> ImportResult:
    """Create one new local profile from a validated archive.

    Every path here is the destination's own. The archive carries no absolute path at all — the
    source's were dropped at export — so there is nothing to rewrite and nothing to accidentally
    keep: the key goes to this computer's profile directory, and the runtime registration is built
    from this computer's installed connector.

    Held under the destination profile's lock. It takes no other lock: nothing here writes to the
    shared `connector/<version>/` tree, it only reads which executable is there, so the
    installation lock is not needed and taking it would only widen what an import blocks. The
    documented order elsewhere — installation before profile — is therefore not engaged, and no
    path in this module takes a profile lock and then any other.

    **Nothing is started.** The agent is registered with its runtime and left stopped, because the
    operator has to stop the source before this one should run.
    """
    from agentnexus_sdk.connector import Paths
    from agentnexus_sdk.profiles import ProfileError, profile_lock, validate_profile_name

    try:
        validate_profile_name(profile)
    except ProfileError as error:
        raise MigrationError(str(error), recovery=error.recovery) from error

    address = agent_api_url or contents.endpoints.get("agent_api_url", "")
    if not address:
        message = "The archive records no Agent API address and none was supplied."
        raise MigrationError(
            message, recovery="Pass --agent-api-url with the address this agent should talk to."
        )

    try:
        with profile_lock(install_root, profile):
            if Paths.for_profile(install_root, profile).root.exists():
                message = f"A {profile!r} profile already exists in {install_root}."
                raise MigrationError(
                    message,
                    recovery=(
                        "Refusing to overwrite it. Import under a different name with --profile, "
                        "or remove that profile deliberately first."
                    ),
                )
            return _import_locked(
                install_root=install_root,
                profile=profile,
                contents=contents,
                environment=environment,
                address=address,
            )
    except ProfileError as error:
        raise MigrationError(str(error), recovery=error.recovery) from error


def _import_locked(
    *,
    install_root: Path,
    profile: str,
    contents: ArchiveContents,
    environment: Any,
    address: str,
) -> ImportResult:
    """Perform the import. The caller holds the destination profile's lock.

    One linear transaction with one unwind path. `unwind` closes over the exact list of runtime
    registrations this call made and the directory it created, which is why they are built here
    rather than passed in: a rollback that has to be told what to undo is a rollback that will one
    day be told incompletely.
    """
    from agentnexus_sdk.connector import Endpoints, Identity, Paths, State, build_server_spec
    from agentnexus_sdk.profiles import ProfileRecord, ensure_profile_directory
    from agentnexus_sdk.runtimes import ADAPTERS, RuntimeIntegrationError
    from agentnexus_sdk.soul import SoulError, install_soul

    identity_document = contents.identity
    paths = Paths(
        root=ensure_profile_directory(install_root, profile),
        profile=profile,
        install_root=Path(install_root),
    )
    result = ImportResult(profile=profile, root=paths.root)
    configured: list[tuple[Any, Any]] = []

    def unwind() -> None:
        """Undo exactly what this import created, and nothing that was here before it."""
        import shutil

        for adapter, outcome in reversed(configured):
            if outcome.changed:
                try:
                    adapter.rollback(outcome.backup)
                except RuntimeIntegrationError:  # pragma: no cover - defensive
                    result.notes.append(f"{adapter.display_name} could not be rolled back")
        shutil.rmtree(paths.root, ignore_errors=True)

    try:
        # The key first. `write_private_key_file` creates it exclusively at mode 600 where the
        # platform enforces that, and refuses rather than overwriting anything.
        paths.key_directory.mkdir(parents=True, exist_ok=True)
        try:
            write_private_key_file(
                signer_from_bytes(contents.members[MEMBER_KEY]), paths.private_key
            )
        except KeyHandlingError as error:
            message = f"The private key could not be written: {error}"
            raise MigrationError(message, recovery="Nothing was left behind.") from error

        endpoints = Endpoints(
            onboarding_base_url=contents.endpoints.get("onboarding_base_url", ""),
            agent_api_url=address,
            public_api_url=contents.endpoints.get("public_api_url", "") or None,
            observer_url=contents.endpoints.get("observer_url", "") or None,
        )
        identity = Identity(
            agent_id=identity_document["agent_id"],
            key_id=identity_document["key_id"],
            handle=identity_document.get("handle", ""),
        )
        # Resolved from *this* computer's environment, so the registered command names the
        # destination's connector. Nothing from the source machine reaches this.
        spec = build_server_spec(
            identity=identity,
            private_key_path=str(paths.private_key),
            endpoints=endpoints,
            environment=environment,
            profile=profile,
        )

        context = paths.runtime_context()
        wanted = sorted(set(contents.source.get("runtimes") or []) & set(ADAPTERS))
        for name in wanted:
            adapter = ADAPTERS[name](
                which=environment.which, runner=environment.run, context=context
            )
            if not adapter.detect().installed:
                result.notes.append(
                    f"{name} is not installed here, so nothing was registered for it"
                )
                continue
            try:
                outcome = adapter.configure(spec, backup_directory=paths.backups)
            except RuntimeIntegrationError as error:
                message = f"{adapter.display_name} refused the registration: {error}"
                raise MigrationError(
                    message, recovery="Nothing of this import was left behind."
                ) from error
            configured.append((adapter, outcome))
            result.registered.append(adapter.display_name)

        if not configured:
            result.notes.append(
                "no supported runtime is installed here; the profile exists but nothing starts it"
            )

        record = ProfileRecord(
            name=profile,
            endpoints={
                "onboarding_base_url": endpoints.onboarding_base_url,
                "agent_api_url": endpoints.agent_api_url,
                "public_api_url": endpoints.public_api_url or "",
                "observer_url": endpoints.observer_url or "",
            },
            runtime={
                "isolation": paths.isolation,
                "server_name": context.server_name,
                "hermes_profile": context.hermes_profile or "",
                "openclaw_config_path": (
                    str(context.openclaw_config) if context.openclaw_config else ""
                ),
                "openclaw_state_dir": (
                    str(context.openclaw_state) if context.openclaw_state else ""
                ),
                "command": spec.command,
            },
            migrated_from=str(contents.source.get("profile") or "") or None,
        )
        record.save(paths.profile_record)

        state = State(
            stage=_imported_stage(),
            agent_id=identity.agent_id,
            key_id=identity.key_id,
            handle=identity.handle,
            private_key_path=str(paths.private_key),
            runtimes=sorted(adapter.name for adapter, _ in configured),
        )
        state.save(paths.state_file)

        soul = contents.members.get(MEMBER_SOUL)
        if soul is not None:
            _install_imported_soul(
                soul,
                paths=paths,
                adapters=[adapter for adapter, _ in configured],
                result=result,
                install=install_soul,
                soul_error=SoulError,
                runtime_error=RuntimeIntegrationError,
            )
    except MigrationError:
        unwind()
        raise
    except Exception as error:
        unwind()
        message = f"The import failed and was rolled back: {error}"
        raise MigrationError(message, recovery="Nothing of this import was left behind.") from error

    result.notes.append(SECOND_COPY_WARNING)
    result.notes.append("nothing was started; start the agent yourself once the source is stopped")
    return result


def _imported_stage() -> Any:
    """Return the stage an imported profile is at: registered, and its runtimes configured."""
    from agentnexus_sdk.connector import Stage

    return Stage.RUNTIMES_CONFIGURED


def _install_imported_soul(
    payload: bytes,
    *,
    paths: Any,
    adapters: list[Any],
    result: ImportResult,
    install: Any,
    soul_error: type[Exception],
    runtime_error: type[Exception],
) -> None:
    """Write the imported instruction document where this computer's runtime keeps it.

    A soul that cannot be placed is a note, not a failure. The identity is the thing being moved;
    refusing a whole migration because a runtime will not say where its instruction document lives
    would strand it over the least important component.
    """
    for adapter in adapters:
        try:
            location = adapter.soul_location()
        except (runtime_error, NotImplementedError) as error:
            result.notes.append(f"soul: {adapter.name} publishes no location for it ({error})")
            continue
        try:
            install(
                payload.decode("utf-8"),
                path=location.path,
                profile=paths.profile,
                backups=paths.backups,
            )
        except (soul_error, UnicodeDecodeError) as error:
            result.notes.append(f"soul: not written ({error})")
            return
        result.notes.append(f"soul: written to {location.path}")
        return
    result.notes.append("soul: carried in the archive but no runtime here could place it")
