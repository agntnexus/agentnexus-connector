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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from agentnexus_sdk import transport
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

#: How many leftover paths an error message lists before it summarises the rest.
MAX_REPORTED_LEFTOVERS: Final = 10

#: Written into a profile directory that a failed import could not fully clean up.
#:
#: Its presence is what stops the next run from walking over a half-undone state. It records paths
#: and runtime names only: the backups it points at may hold runtime configuration, so it names
#: them rather than quoting them.
RESIDUE_MARKER: Final = "import-incomplete.json"

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

    def __init__(
        self, message: str, *, recovery: str | None = None, residue: Sequence[str] = ()
    ) -> None:
        """Carry the recovery step beside the failure, the way the connector's errors do.

        `residue` is what a failed operation could **not** undo, named so the operator can find it:
        a runtime entry that is still registered, a directory kept because the backups in it are
        the only way back. It is empty for the ordinary refusals, which change nothing.

        Every string in it is a path or a runtime name. No key material, no configuration content
        and no password ever goes in here, because this is printed and may be pasted into a bug
        report.
        """
        super().__init__(message)
        self.recovery = recovery
        self.residue = list(residue)


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
    # This archive format carries four addresses and no statement of which network they are on,
    # so a profile that has been deliberately moved to another plane cannot travel in it: the
    # destination would rebuild the record with the migrated address and no declaration, and read
    # it back as the private network. Refused rather than exported and silently relabelled. A
    # later archive version that carries the declaration is what lifts this.
    declared = transport.read_transport(record)
    if declared.mode != transport.MODE_TAILNET:
        message = (
            f"The {paths.profile!r} profile has been migrated to the {declared.mode} agent API "
            "and cannot be exported by this connector version."
        )
        raise MigrationError(
            message,
            recovery=(
                "Roll it back first with `agentnexus-connector profile endpoint rollback "
                f"--profile {paths.profile}`, export it, and migrate it again on the destination. "
                "Nothing was written."
            ),
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
            # Empty for a single-base profile, which is every profile written before the
            # read host existed. An import of such an archive stays single-base.
            "agent_read_url": str(record.endpoints.get("agent_read_url", "")),
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
    """Write the sealed archive completely, or leave nothing behind that looks like one.

    Exclusive creation matters for the same reason it does for a key file: this one carries a key
    too, and silently replacing somebody's export is destroying a credential. It is also what makes
    the cleanup below safe — `O_EXCL` succeeding proves this call created that file, so removing it
    on failure cannot remove somebody else's.

    **`os.write` is not required to write everything it is given.** It returns how many bytes it
    took, and a single call was being trusted to take all of them. A short write would have left a
    truncated file with no error anywhere: the export would look successful, and would fail to
    authenticate on the destination computer, long after the source had been cleaned up. So this
    loops until the payload is gone, and treats a write that takes nothing as a failure rather than
    spinning on it.
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

    written = 0
    try:
        while written < len(payload):
            taken = os.write(descriptor, payload[written:])
            if taken <= 0:
                message = (
                    f"Writing {destination} stopped after {written} of {len(payload)} bytes with "
                    "no progress."
                )
                raise MigrationError(message, recovery="Nothing usable was left behind.")
            written += taken
    except (OSError, MigrationError) as error:
        os.close(descriptor)
        # Only this attempt's file, and only because `O_EXCL` proved this call created it.
        destination.unlink(missing_ok=True)
        if isinstance(error, MigrationError):
            raise
        message = f"{destination} could not be written after {written} bytes: {error}"
        raise MigrationError(
            message,
            recovery=(
                "The partly written file was removed, so nothing that looks like a valid export "
                "was left behind. Free space or choose another location, then export again."
            ),
        ) from error
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
class UnwindReport:
    """What a failed import managed to undo, and what it did not.

    The distinction this exists to keep is between "rolled back" and "asked the adapter to roll
    back". `adapter.rollback()` returning is not evidence: `OpenClawAdapter.rollback` calls
    `_remove_entry`, which *returns* the reason a removal was refused rather than raising, so a
    swallowed refusal used to look exactly like success. Every entry is therefore read back.
    """

    #: Runtimes whose entry was confirmed gone afterwards.
    restored: list[str] = field(default_factory=list)
    #: Runtimes whose entry is still registered, or whose state could not be established. Each
    #: string names the runtime and why, with no configuration content in it.
    failed: list[str] = field(default_factory=list)
    #: Things this import created outside its own directory and deliberately did not delete.
    left_in_place: list[str] = field(default_factory=list)
    #: The profile directory, when it is still there afterwards.
    preserved: Path | None = None
    #: Why it is still there: kept on purpose, or a removal that did not work.
    preserved_reason: str = ""
    #: Files still under the profile directory after a removal that was attempted and failed.
    #: Only what a re-read found — a file that really was deleted is not reported as remaining.
    leftover_files: list[Path] = field(default_factory=list)
    #: Whether the residue marker reached the disk. A directory that cannot be removed may equally
    #: be one that cannot be written to, and the operator still has to be told what is there.
    marker_written: bool = False

    @property
    def rollback_complete(self) -> bool:
        """Report whether every runtime change was undone and confirmed."""
        return not self.failed

    @property
    def complete(self) -> bool:
        """Report whether the runtime *and* the filesystem were both put back.

        Both halves, because the second one used to be assumed. `shutil.rmtree(...,
        ignore_errors=True)` discards the reason it failed, and a file held open by a runtime or an
        antivirus scanner is an ordinary Windows outcome — so "removed" was being reported for a
        directory that was still there.
        """
        return self.rollback_complete and self.preserved is None

    def as_residue(self) -> list[str]:
        """Render what is still on this machine, for an error message. Paths and names only."""
        lines = list(self.failed)
        lines.extend(self.left_in_place)
        if self.preserved is not None:
            lines.append(f"{self.preserved_reason}: {self.preserved}")
            if not self.leftover_files:
                lines.append(f"runtime configuration backups: {self.preserved / 'backups'}")
        for leftover in self.leftover_files[:MAX_REPORTED_LEFTOVERS]:
            lines.append(f"still on disk: {leftover}")
        remaining = len(self.leftover_files) - MAX_REPORTED_LEFTOVERS
        if remaining > 0:
            lines.append(f"and {remaining} more under {self.preserved}")
        if self.preserved is not None and not self.marker_written:
            lines.append(
                f"{RESIDUE_MARKER} could NOT be written there, so this message is the only record"
            )
        return lines


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
    agent_read_url: str | None = None,
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
    from agentnexus_sdk.profiles import ProfileError, profile_lock, validate_profile_name

    try:
        validate_profile_name(profile)
    except ProfileError as error:
        raise MigrationError(str(error), recovery=error.recovery) from error

    address = agent_api_url or contents.endpoints.get("agent_api_url", "")
    # An archive written before the split records none, and an import of one stays a single-base
    # profile. Unlike the write address there is no "and none was supplied" refusal: one address
    # for both directions is a supported deployment, not a missing value.
    read_address = agent_read_url or contents.endpoints.get("agent_read_url", "") or None
    if not address:
        message = "The archive records no Agent API address and none was supplied."
        raise MigrationError(
            message, recovery="Pass --agent-api-url with the address this agent should talk to."
        )

    try:
        with profile_lock(install_root, profile):
            require_free_destination(
                install_root=install_root,
                profile=profile,
                environment=environment,
                runtimes=[str(name) for name in (contents.source.get("runtimes") or [])],
            )
            return _import_locked(
                install_root=install_root,
                profile=profile,
                contents=contents,
                environment=environment,
                address=address,
                read_address=read_address,
            )
    except ProfileError as error:
        raise MigrationError(str(error), recovery=error.recovery) from error


def _another_name(profile: str) -> str:
    """Suggest a local name that is unlikely to be the one that just collided."""
    return f"{profile}2" if not profile[-1:].isdigit() else f"{profile}-new"


def require_free_destination(
    *,
    install_root: Path,
    profile: str,
    environment: Any,
    runtimes: Sequence[str],
) -> None:
    """Refuse before anything is written if this name is taken anywhere that matters.

    **A missing AgentNexus profile directory proves nothing on its own.** The connector's directory
    is not where a runtime keeps its own profile: Hermes owns `<HERMES_HOME>/profiles/<name>` with
    its own `config.yaml`, `.env` and `SOUL.md`, and OpenClaw owns whatever `OPENCLAW_CONFIG_PATH`
    and `OPENCLAW_STATE_DIR` point at. An import that only looked at its own directory would
    register into somebody's existing runtime profile and, when the soul was installed, overwrite a
    document they wrote.

    So this asks each runtime the archive came from, before the import creates anything:

    * is there already an MCP entry under this profile's server name;
    * does the runtime already have a profile of this name;
    * is there already an instruction document where this one would be written.

    A runtime that cannot be asked is treated as occupied. That is deliberately conservative and
    the cost of being wrong is small — the operator picks another local name — while the cost the
    other way is writing a key into somebody else's agent.

    There is **no force option** and none is being added. Overwriting an existing agent's runtime
    configuration is not a thing to make one flag away.
    """
    from agentnexus_sdk.connector import Paths
    from agentnexus_sdk.runtimes import ADAPTERS

    # `Paths.for_profile` runs `profile_directory`, which already refuses a reparse point on the
    # install root, the profiles root and this profile's own directory, and refuses a name that
    # collides with an existing one only by case. What it cannot know about is the *runtime's*
    # side, which is what the rest of this function asks about.
    paths = Paths.for_profile(install_root, profile)
    suggestion = _another_name(profile)

    if paths.root.exists():
        residue = paths.root / RESIDUE_MARKER
        if residue.is_file():
            _refuse_residue(paths.root, residue)
        message = f"A {profile!r} profile already exists in {install_root}."
        raise MigrationError(
            message,
            recovery=(
                f"Refusing to overwrite it. Import under a different name with "
                f"--profile {suggestion}, or remove that profile deliberately first."
            ),
        )

    context = paths.runtime_context()
    for name in sorted(set(runtimes) & set(ADAPTERS)):
        adapter = ADAPTERS[name](which=environment.which, runner=environment.run, context=context)
        if not adapter.detect().installed:
            continue
        _require_free_runtime_target(adapter, name=name, profile=profile, suggestion=suggestion)

    # OpenClaw's real target paths, as its adapter drives them: `OPENCLAW_CONFIG_PATH` and
    # `OPENCLAW_STATE_DIR`. `RuntimeContext.prepare()` creates both during `configure`, so an
    # import that did not look would create a registration inside a directory somebody else owns.
    for candidate in (context.openclaw_config, context.openclaw_state):
        if candidate is None:
            continue
        _require_real_target(candidate, suggestion=suggestion)
        if candidate.exists():
            message = f"{candidate} already exists."
            raise MigrationError(
                message,
                recovery=(
                    "That is where this profile's OpenClaw configuration and state would live, "
                    "and an import must not write into somebody's existing one. Use "
                    f"--profile {suggestion}."
                ),
            )


def _require_real_target(path: Path, *, suggestion: str) -> None:
    """Refuse a destination reached through a link, so "it does not exist" means what it says.

    A junction costs nothing to create on Windows and `is_symlink()` reports it as an ordinary
    directory. Without this, an absent path could be absent only because the link it sits under
    points somewhere else entirely — and the import would then write a key through it.
    """
    from agentnexus_sdk.soul import SoulError, require_real_location

    try:
        require_real_location(path)
    except SoulError as error:
        raise MigrationError(
            str(error),
            recovery=(
                "Nothing was written. An import will not follow a link to decide a destination "
                f"is free. Remove the link, or use --profile {suggestion}."
            ),
        ) from error


def _require_free_runtime_target(adapter: Any, *, name: str, profile: str, suggestion: str) -> None:
    """Ask one runtime whether this profile's name is already its own. Refuse if it is."""
    from agentnexus_sdk.runtimes import RuntimeIntegrationError

    try:
        entry = adapter.existing_entry()
    except RuntimeIntegrationError as error:
        message = f"{adapter.display_name} could not be asked whether {profile!r} is free: {error}"
        raise MigrationError(
            message,
            recovery=(
                "Nothing was written. A runtime this connector cannot read is treated as "
                f"occupied rather than assumed empty. Fix that, or use --profile {suggestion}."
            ),
        ) from error
    if entry is not None:
        message = f"{adapter.display_name} already has an MCP entry for the {profile!r} profile."
        raise MigrationError(
            message,
            recovery=(
                "Refusing to replace it: it belongs to an agent that is already set up here. "
                f"Use --profile {suggestion}."
            ),
        )

    existing_profiles = getattr(adapter, "existing_profiles", None)
    if existing_profiles is not None:
        try:
            names = existing_profiles()
        except RuntimeIntegrationError as error:
            message = f"{adapter.display_name} could not list its profiles: {error}"
            raise MigrationError(
                message,
                recovery=(
                    "Nothing was written. Without that list this cannot tell whether "
                    f"{profile!r} is free, and will not guess. Use --profile {suggestion}."
                ),
            ) from error
        target = getattr(adapter, "_context", None)
        wanted = getattr(target, "hermes_profile", None) or profile
        if wanted in names:
            message = f"{adapter.display_name} already has a {wanted!r} profile of its own."
            raise MigrationError(
                message,
                recovery=(
                    "An import must not register into an existing runtime profile or overwrite "
                    f"its instruction document. Use --profile {suggestion}."
                ),
            )

    try:
        location = adapter.soul_location()
    except (RuntimeIntegrationError, NotImplementedError):
        # No published location is not a collision: it means nothing would be written there.
        return
    _require_real_target(location.path, suggestion=suggestion)
    if location.path.exists():
        message = f"{location.path} already exists."
        raise MigrationError(
            message,
            recovery=(
                "That is the instruction document this import would write. Refusing to replace "
                f"somebody's own. Use --profile {suggestion}."
            ),
        )


def _refuse_residue(root: Path, marker: Path) -> None:
    """Refuse to run over the leftovers of an import that could not fully undo itself."""
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        outstanding = [str(item) for item in record.get("outstanding", [])]
    except (OSError, ValueError):  # pragma: no cover - a damaged marker is still a refusal
        outstanding = []
    message = f"{root} holds the leftovers of an import that could not be fully undone."
    raise MigrationError(
        message,
        recovery="Nothing was changed. " + recovery_steps(root, profile=root.name),
        residue=outstanding or [f"see {marker}"],
    )


def _import_locked(
    *,
    install_root: Path,
    profile: str,
    contents: ArchiveContents,
    environment: Any,
    address: str,
    read_address: str | None,
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
    #: Adapters this call configured, with the handle to undo each.
    configured: list[tuple[Any, Any]] = []
    #: Adapters whose `configure` raised. Their state is *unknown*, not unchanged: Hermes creates
    #: its profile and may replace an entry before it fails, and OpenClaw removes the old entry
    #: before adding the new one with no rollback of its own. They are verified like the rest.
    attempted: list[Any] = []

    try:
        # The key first. `write_private_key_file` creates it exclusively at mode 600 where the
        # platform enforces that, and refuses rather than overwriting anything.
        paths.key_directory.mkdir(parents=True, exist_ok=True)
        try:
            signer = signer_from_bytes(contents.members[MEMBER_KEY])
            write_private_key_file(signer, paths.private_key)
        except KeyHandlingError as error:
            message = f"The private key could not be written: {error}"
            raise MigrationError(message, recovery="Nothing was left behind.") from error

        endpoints = Endpoints(
            onboarding_base_url=contents.endpoints.get("onboarding_base_url", ""),
            agent_api_url=address,
            public_api_url=contents.endpoints.get("public_api_url", "") or None,
            observer_url=contents.endpoints.get("observer_url", "") or None,
            # An archive written before the split records none, and an import of one stays a
            # single-base profile. Unlike the write address there is no "and none was supplied"
            # refusal: a deployment with one address for both directions is a supported state,
            # not a missing value.
            agent_read_url=read_address,
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
            attempted.append(adapter)
            try:
                outcome = adapter.configure(spec, backup_directory=paths.backups)
            except RuntimeIntegrationError as error:
                message = f"{adapter.display_name} refused the registration: {error}"
                raise MigrationError(message) from error
            attempted.pop()
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
                "agent_read_url": endpoints.agent_read_url or "",
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
    except MigrationError as error:
        report = _unwind(paths=paths, configured=configured, attempted=attempted)
        raise _failed_import(error, report=report, paths=paths) from error
    except Exception as error:
        report = _unwind(paths=paths, configured=configured, attempted=attempted)
        wrapped = MigrationError(f"The import failed: {error}")
        raise _failed_import(wrapped, report=report, paths=paths) from error

    result.notes.append(SECOND_COPY_WARNING)
    result.notes.append("nothing was started; start the agent yourself once the source is stopped")
    return result


def _unwind(
    *, paths: Any, configured: Sequence[tuple[Any, Any]], attempted: Sequence[Any]
) -> UnwindReport:
    """Undo what this import created, verify it, and report what would not come back.

    Two things changed here after review. The rollback is **read back** rather than assumed, and a
    directory whose backups are the only route to recovery is **kept** rather than deleted.

    Reading back matters because the adapters do not fail the same way. `HermesAdapter.rollback`
    copies a backup over the configuration and raises nothing; `OpenClawAdapter.rollback` calls
    `_remove_entry`, which *returns* the reason OpenClaw refused rather than raising — its own
    configuration guard can reject the write. A caught exception was therefore never going to be a
    reliable signal, and the entry itself is.

    Keeping the directory matters because deleting it destroys the runtime configuration backups
    taken on the way in *and* the key file that a still-registered entry points at. A registered
    MCP server whose key file has been deleted is a worse state than one this command admits it
    could not clean up.
    """
    from agentnexus_sdk.runtimes import RuntimeIntegrationError

    report = UnwindReport()
    for adapter, outcome in reversed(list(configured)):
        if not outcome.changed:
            report.restored.append(adapter.display_name)
            continue
        _rollback_and_verify(
            adapter, outcome.backup, report=report, runtime_error=RuntimeIntegrationError
        )

    # `configure` can fail after changing something. Hermes restores its own configuration backup
    # and OpenClaw does not, so neither may be assumed clean: the entry is read back either way.
    for adapter in reversed(list(attempted)):
        _rollback_and_verify(adapter, None, report=report, runtime_error=RuntimeIntegrationError)
        created = getattr(adapter, "_context", None)
        runtime_profile = getattr(created, "hermes_profile", None)
        if runtime_profile:
            report.left_in_place.append(
                f"{adapter.display_name} profile {runtime_profile!r} may have been created by this "
                "attempt and was not deleted"
            )

    if report.rollback_complete:
        # Every runtime change is undone, so the directory is no longer holding anything the
        # operator needs and may go. Whether it actually went is then established by looking.
        _remove_directory(paths.root, report)
        if report.complete:
            return report
    else:
        report.preserved = paths.root
        report.preserved_reason = "kept for recovery, not deleted"

    report.marker_written = _write_residue_marker(paths.root, report)
    return report


def _remove_directory(root: Path, report: UnwindReport) -> None:
    """Remove the directory this import created, then check that it is gone.

    `shutil.rmtree(..., ignore_errors=True)` was being used and its result discarded, which is how
    a directory that is still there came to be reported as removed. A file held open by a runtime
    or a scanner is an everyday Windows outcome, not an exotic one.

    The errors are still ignored *during* the walk — stopping at the first one would leave more
    behind than continuing does — but what remains afterwards is read off the filesystem, so the
    report describes the disk rather than the intention. Files that really were deleted are not
    listed as remaining.

    Scoped to this directory and nothing else. There is no retry that relaxes permissions and no
    handler that takes ownership of what it cannot delete: a cleanup with more authority than the
    thing it is cleaning up after is a worse failure mode than a leftover directory.
    """
    import shutil

    shutil.rmtree(root, ignore_errors=True)
    if not root.exists():
        return

    report.preserved = root
    report.preserved_reason = "could not be removed"
    try:
        report.leftover_files = sorted(path for path in root.rglob("*") if path.is_file())
    except OSError:  # pragma: no cover - a directory that cannot even be walked is still residue
        report.leftover_files = []


def _rollback_and_verify(
    adapter: Any, backup: Any, *, report: UnwindReport, runtime_error: type[Exception]
) -> None:
    """Ask one adapter to undo its change, then confirm from the runtime that it did."""
    failure: str | None = None
    try:
        adapter.rollback(backup)
    except runtime_error as error:
        failure = str(error)
    except Exception as error:  # pragma: no cover - defensive; an adapter must not end the unwind
        failure = str(error)

    try:
        entry = adapter.existing_entry()
    except Exception as error:
        # Any failure here means "cannot confirm", which is reported as a failure rather than
        # assumed clean. An unwind that stopped on an adapter's exception would also leave the
        # remaining adapters untouched.
        report.failed.append(
            f"{adapter.display_name}: could not confirm the entry was removed ({error})"
        )
        return
    if entry is not None:
        detail = f" ({failure})" if failure else ""
        report.failed.append(
            f"{adapter.display_name}: the MCP entry is still registered and must be removed by "
            f"hand{detail}"
        )
        return
    if failure is not None:
        report.restored.append(f"{adapter.display_name} (after reporting: {failure})")
        return
    report.restored.append(adapter.display_name)


def _write_residue_marker(root: Path, report: UnwindReport) -> bool:
    """Record what was left behind, and report whether that record reached the disk.

    Whether it did is not a detail. A directory that could not be removed may equally be one that
    cannot be written to, and the marker was the only place the leftovers were being written down.
    So this returns the answer and the caller says so in the error, because the operator has to be
    told what is there whether or not a file could be left naming it.

    Paths and runtime names only. The backups this points at can hold runtime configuration, so it
    names their directory rather than quoting anything out of it, and no key material, archive
    content or password is written here.
    """
    document = {
        "written_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outstanding": report.as_residue(),
        "backups": str(root / "backups"),
    }
    try:
        (root / RESIDUE_MARKER).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError:
        return False
    return True


def recovery_steps(root: Path, *, profile: str) -> str:
    """Describe how to get back to a state where this profile can be imported again.

    One description, used by both the failure and the later refusal, because they disagreed. The
    error used to say "delete `import-incomplete.json` and import again" while
    `require_free_destination` refuses on the profile directory existing at all — so following the
    advice produced a second refusal with a different message and no progress.

    What actually frees the name is the directory being gone, deliberately, after whatever is in
    it has been dealt with. That is an operator's decision about a directory holding a private key
    and configuration backups, so this says what to check and leaves the removal to them. It does
    not print a recursive delete command for a real profile path.
    """
    return (
        "To import this profile again under the same name:\n"
        "  1. remove any runtime entry listed above, with that runtime's own command;\n"
        f"  2. take what you need out of {root / 'backups'} — those are the runtime "
        "configuration files as they were before this import;\n"
        f"  3. then remove {root} yourself. Deleting {RESIDUE_MARKER} alone does not free the "
        "name: an import refuses while that directory exists at all.\n"
        f"Or import under a different local name with --profile <name>, which is immediate but "
        "leaves everything above exactly where it is."
    )


def _failed_import(error: MigrationError, *, report: UnwindReport, paths: Any) -> MigrationError:
    """Build the error the caller sees, keeping the two failures apart.

    The original failure and the state of the unwind are different facts, and the first version
    merged them: it said "nothing of this import was left behind" whatever the unwind had managed.
    """
    if report.complete:
        return MigrationError(
            str(error),
            recovery=(
                "Everything this import created was removed, and both the runtime entries and the "
                "profile directory were checked afterwards. Fix the cause and run it again."
            ),
        )
    if report.rollback_complete:
        headline = f"{error} The runtime changes were undone, but the cleanup did not complete."
    else:
        headline = f"{error} The rollback did not complete."
    return MigrationError(
        headline,
        recovery=(
            "What is still on this computer is listed below. "
            f"{paths.root} is still there: the runtime configuration backups in it are the way "
            "back, and the key file is what any still-registered entry points at, so nothing was "
            "forced.\n" + recovery_steps(paths.root, profile=str(paths.profile))
        ),
        residue=report.as_residue(),
    )


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
