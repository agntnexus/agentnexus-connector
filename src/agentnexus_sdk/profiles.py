"""Named agent profiles: one AgentNexus identity each, and nothing shared between them.

One operating-system user may be approved for several agents. Before this module there was one
state root, one key, and one runtime entry, so a second invitation redeemed into the first
identity's directory or was refused outright. A profile is the unit that fixes that: a validated
name, a contained directory, and its own key, state, endpoint record, backups, and runtime
context.

**The name is the whole attack surface here.** It arrives from a command line and becomes a
directory name, a lock file name, and an MCP entry name, so it is validated before anything is
downloaded or written. One canonical grammar covers every consumer at once — lower-case letters
and digits, starting with a letter, 1 to 32 characters — plus a reserved-device refusal the pattern
cannot express, and a containment check that the resolved directory sits directly under this
installation's own profiles root with no reparse point on the way there. A name that fails any of
those is refused rather than sanitised: quietly rewriting somebody's profile name is how two agents
end up sharing one key.

**What is deliberately *not* here.** No invitation, no key material, and no capability value ever
reaches a profile record. `profile.json` carries the name, the addresses setup was pointed at, and
the runtime layout; `state.json` carries what the server already published. Both are readable by
anyone who can already read the key file, so neither is a place to keep a secret.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TextIO

#: The profile a plain installation gets, and the one the legacy single-profile layout becomes.
DEFAULT_PROFILE_NAME: Final = "default"

#: Everything profile-shaped lives under this one directory inside the AgentNexus install root.
PROFILES_DIRECTORY_NAME: Final = "profiles"

#: Lock files live beside the profiles rather than inside them, so a lock can be taken for a
#: profile that does not exist yet — which is exactly the moment two concurrent setups race.
LOCKS_DIRECTORY_NAME: Final = "locks"

#: Where the pre-profile layout is kept after migration. Never deleted by this connector.
LEGACY_RETIREMENT_DIRECTORY_NAME: Final = "legacy-pre-profiles"

#: Where a removed profile's key is kept when the caller did not ask for it to be destroyed. It
#: is deliberately *outside* the profiles directory: leaving it in place would make the profile
#: name unusable afterwards, because setup refuses to start where a key already exists.
RETIRED_DIRECTORY_NAME: Final = "retired-keys"

#: Prefix of a half-built profile directory. A crash leaves one behind; the next run clears it.
INCOMING_PREFIX: Final = ".incoming-"

#: The profile record's own schema. A newer one stops rather than guessing what a field meant.
PROFILE_SCHEMA_VERSION: Final = 1

MAX_PROFILE_NAME_LENGTH: Final = 32

#: One canonical grammar: lower-case letters and digits, starting with a letter, 1 to 32
#: characters. `agent2` is the shape; `second-agent`, `agent_2`, `Agent2` and `2agent` are not.
#:
#: **This is deliberately narrower than any single consumer requires, because it has to satisfy
#: all of them at once and keep satisfying them.** What each one actually accepts was measured
#: rather than assumed:
#:
#: * Real Hermes v0.20.6 reports its own rule in its refusal message — `[a-z0-9][a-z0-9_-]{0,63}`
#:   — and lower-cases the input before applying it, so `Agent2` silently becomes `agent2`. Its
#:   `profile create --help` says only "lowercase, alphanumeric", which is *stricter* than what
#:   the build accepts. Sitting inside the documented promise rather than the observed behaviour
#:   is what keeps a future Hermes from turning a working profile name into a failed setup.
#: * Windows resolves a device name as a path whatever the extension, which is not theoretical
#:   here: `hermes profile create nul` answered "Profile 'nul' already exists". Hence the
#:   reserved set below, which the pattern alone cannot express.
#: * A leading letter, and no leading or trailing hyphen, keeps the name from being read as an
#:   option by PowerShell or a POSIX shell, or as a number by anything.
#:
#: A name outside this is refused with the one correction that fixes it. It is never rewritten:
#: Hermes' own silent lower-casing is exactly the behaviour that would let two AgentNexus
#: profiles collapse into one runtime context, and this connector must not add a second one.
PROFILE_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9]{0,31}$")

#: Names Windows resolves to a device rather than a directory, whatever the extension. Creating
#: `.../profiles/nul` succeeds in appearance and writes to the null device.
RESERVED_PROFILE_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
    # Not devices. `all` is a sentinel this connector's own commands use for "every profile", and
    # `migration` names the lock that serialises layout migration, which no profile may share.
    | {"all", "migration"}
)

#: The lock that serialises migration and layout changes across every profile. It is a reserved
#: name, so it can use the same per-profile lock file scheme without ever colliding with one.
MIGRATION_LOCK_NAME: Final = "migration"


class ProfileError(Exception):
    """A profile-layout failure carrying an actionable recovery step."""

    def __init__(self, message: str, *, recovery: str | None = None) -> None:
        """Build a failure that knows how it should be recovered from."""
        super().__init__(message)
        self.recovery = recovery


# ---------------------------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------------------------

#: The name every message and every document uses when it needs to show one. It satisfies the
#: canonical grammar, and a test asserts that, so the example can never drift from the rule.
EXAMPLE_NAME: Final = "agent2"

#: The rule in one sentence. Every refusal ends with it, so an applicant reads the whole rule
#: once rather than discovering it one rejection at a time.
NAME_RULE: Final = (
    "Use lower-case letters and digits only, starting with a letter, 1 to "
    f"{MAX_PROFILE_NAME_LENGTH} characters — no hyphens, underscores, or dots"
)


def _suggest(value: str) -> str | None:
    """Return the nearest valid name, when stripping the disallowed characters leaves one."""
    reduced = "".join(
        character for character in value.lower() if character.isascii() and character.isalnum()
    )
    reduced = reduced.lstrip("0123456789")[:MAX_PROFILE_NAME_LENGTH]
    return reduced if PROFILE_NAME_PATTERN.match(reduced) else None


def _try_instead(candidate: str) -> str:
    """Name the corrected form when there is one, and the example when there is not."""
    suggestion = candidate if PROFILE_NAME_PATTERN.match(candidate) else _suggest(candidate)
    return f"Try `{suggestion or EXAMPLE_NAME}`."


def _use_only(value: str) -> str:
    """Build the single correction message every grammar refusal carries."""
    return f"{NAME_RULE}. {_try_instead(value)}"


def validate_profile_name(value: str) -> str:
    """Return `value` unchanged if it is a safe profile name, or refuse with the reason.

    Refusal rather than repair, on purpose. A sanitiser that turned `../other` into `other` would
    silently hand one applicant another applicant's key, and one that lower-cased `Agent1` would
    make two commands mean the same profile on Windows and different profiles on Linux.
    """
    if not isinstance(value, str) or value == "":
        message = "A profile name is required."
        raise ProfileError(message, recovery=f"Choose a short name such as `{EXAMPLE_NAME}`.")

    if len(value) > MAX_PROFILE_NAME_LENGTH:
        message = (
            f"The profile name is {len(value)} characters; the maximum is "
            f"{MAX_PROFILE_NAME_LENGTH}."
        )
        raise ProfileError(message, recovery=f"Shorten it, for example to `{EXAMPLE_NAME}`.")

    # Checked before the pattern so each refusal names the actual problem rather than "not
    # allowed", which is what an applicant needs in order to pick a different name.
    if any(character in value for character in ("/", "\\", ":")):
        message = f"The profile name {value!r} contains a path separator."
        raise ProfileError(
            message,
            recovery=f"A profile name is a single name, never a path. Try `{EXAMPLE_NAME}`.",
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        message = "The profile name contains a control character."
        raise ProfileError(message, recovery=_use_only(value))
    if value in {".", ".."} or value.startswith("."):
        message = f"The profile name {value!r} is a directory traversal segment."
        raise ProfileError(message, recovery=_use_only(value))
    if value != value.lower():
        message = f"The profile name {value!r} contains upper-case letters."
        raise ProfileError(
            message,
            recovery=(
                "Profile names are lower-case. Hermes silently lower-cases a profile name, so "
                "two spellings would become one runtime context while staying two directories "
                f"on Linux. {_try_instead(value.lower())}"
            ),
        )
    if PROFILE_NAME_PATTERN.match(value) is None:
        message = f"The profile name {value!r} is not a valid profile name."
        raise ProfileError(message, recovery=_use_only(value))
    if value in RESERVED_PROFILE_NAMES:
        message = f"The profile name {value!r} is reserved."
        raise ProfileError(
            message,
            recovery=(
                "Windows resolves that name to a device rather than a directory — a real Hermes "
                f"answered \"Profile 'nul' already exists\" to it. {_try_instead(EXAMPLE_NAME)}"
            ),
        )
    return value


# ---------------------------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------------------------


def _is_reparse_point(path: Path) -> bool:
    """Whether `path` is a symlink, a junction, or any other reparse point.

    `Path.is_symlink()` alone is not enough on Windows: a directory junction is a reparse point
    that it reports as a normal directory, and a junction is the cheapest way to point this
    connector's profiles root at somebody else's files.
    """
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = int(getattr(info, "st_file_attributes", 0))
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _refuse_reparse_point(path: Path, *, what: str) -> None:
    if _is_reparse_point(path):
        message = f"{path} is a link rather than a real directory."
        raise ProfileError(
            message,
            recovery=(
                f"AgentNexus refuses to follow a link out of the directory it owns. Inspect {what} "
                "and remove the link, or install into a fresh location."
            ),
        )


def profiles_root(install_root: Path) -> Path:
    """Return the one directory every profile lives directly beneath."""
    return Path(install_root) / PROFILES_DIRECTORY_NAME


def profile_directory(install_root: Path, name: str) -> Path:
    """Resolve one profile's directory, refusing anything that escapes the profiles root.

    Both halves matter. The name check stops `..` and a separator from ever becoming a path; the
    containment check stops a *planted link* from carrying a perfectly valid name somewhere else.
    """
    validated = validate_profile_name(name)
    root = profiles_root(install_root)
    _refuse_reparse_point(Path(install_root), what="the AgentNexus install root")
    _refuse_reparse_point(root, what="the profiles directory")

    candidate = root / validated
    _refuse_reparse_point(candidate, what=f"the {validated!r} profile directory")

    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved.parent != resolved_root:
        message = f"The {validated!r} profile directory resolves outside {resolved_root}."
        raise ProfileError(
            message,
            recovery="Remove whatever redirects that path, or install into a fresh location.",
        )
    _refuse_case_collision(root, validated)
    return candidate


def _refuse_case_collision(root: Path, name: str) -> None:
    """Refuse a name that differs from an existing profile only by case.

    The name rules already exclude upper case, so this can only fire against a directory some
    other tool created. It still has to fire: on Windows that directory *is* the profile the
    caller would get, and its key is not the key the caller asked for.
    """
    if not root.is_dir():
        return
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name != name and entry.name.casefold() == name.casefold():
            message = f"A profile directory named {entry.name!r} already exists."
            raise ProfileError(
                message,
                recovery=(
                    f"It differs from {name!r} only by case, which is one directory on Windows and "
                    "two on Linux. Rename it, or choose a different profile name."
                ),
            )


def ensure_profile_directory(install_root: Path, name: str) -> Path:
    """Create a profile's directory if it does not exist, and return the checked path."""
    directory = profile_directory(install_root, name)
    directory.mkdir(parents=True, exist_ok=True)
    _harden(Path(install_root))
    _harden(profiles_root(install_root))
    _harden(directory)
    # Re-checked after creation: the checks above ran against a path that did not exist yet.
    return profile_directory(install_root, name)


def _harden(directory: Path) -> None:
    """Make a directory the user's own where the platform enforces that."""
    if os.name == "posix":
        with contextlib.suppress(OSError):
            directory.chmod(0o700)


# ---------------------------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------------------------


@contextlib.contextmanager
def profile_lock(install_root: Path, name: str) -> Iterator[Path]:
    """Hold an exclusive lock on one profile for the duration of the block.

    An operating-system lock rather than a lock *file* whose existence means "busy": the kernel
    releases this one when the process dies, so a run killed halfway through never leaves a stale
    lock that an applicant has to be told to delete by hand.

    The scope is one profile, so setting up `agent2` does not wait behind `agent1`, and two runs of
    the same profile cannot interleave a key creation with a redemption.
    """
    validated = name if name == MIGRATION_LOCK_NAME else validate_profile_name(name)
    directory = Path(install_root) / LOCKS_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    _harden(directory)
    path = directory / f"{validated}.lock"

    handle = path.open("a+b")
    try:
        _acquire(handle, validated)
        yield path
    finally:
        with contextlib.suppress(OSError, ValueError):
            _release(handle)
        handle.close()


def _acquire(handle: Any, name: str) -> None:
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        message = f"Another AgentNexus setup is already running for the {name!r} profile."
        raise ProfileError(
            message,
            recovery="Wait for it to finish, or close the other window, then run this again.",
        ) from error


def _release(handle: Any) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------------------------
# The profile record
# ---------------------------------------------------------------------------------------------


@dataclass
class ProfileRecord:
    """The non-secret description of one profile: its name, addresses, and runtime layout."""

    name: str
    created_at: str = ""
    endpoints: dict[str, str] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    migrated_from: str | None = None

    def to_document(self) -> dict[str, Any]:
        """Serialise the record. Every field is a name, an address, or a local path."""
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "name": self.name,
            "created_at": self.created_at,
            "endpoints": dict(self.endpoints),
            "runtime": dict(self.runtime),
            "migrated_from": self.migrated_from,
        }

    @classmethod
    def load(cls, path: Path) -> ProfileRecord | None:
        """Read a profile record, refusing a schema this build does not understand."""
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            message = f"The profile record at {path} could not be read: {error}"
            raise ProfileError(
                message, recovery=f"Inspect {path}, or delete it to let setup rewrite it."
            ) from error
        if document.get("schema_version") != PROFILE_SCHEMA_VERSION:
            message = f"The profile record at {path} was written by a different connector version."
            raise ProfileError(
                message, recovery="Install the matching connector version, or delete that file."
            )
        return cls(
            name=str(document.get("name", "")),
            created_at=str(document.get("created_at", "")),
            endpoints=dict(document.get("endpoints") or {}),
            runtime=dict(document.get("runtime") or {}),
            migrated_from=document.get("migrated_from"),
        )

    def save(self, path: Path) -> None:
        """Persist the record atomically, so an interrupted write cannot truncate it."""
        if not self.created_at:
            self.created_at = _now()
        write_json_atomically(path, self.to_document())


def write_json_atomically(path: Path, document: dict[str, Any]) -> None:
    """Write a JSON document through a temporary file and one rename.

    A half-written `state.json` is the difference between "resume this identity" and "there is no
    identity here", so the file is never opened for truncation in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    """What `profile list` reports. Public identifiers and local paths only."""

    name: str
    directory: Path
    stage: str | None
    handle: str | None
    agent_id: str | None
    key_present: bool
    runtimes: tuple[str, ...]
    isolation: str

    @property
    def connected(self) -> bool:
        """Whether this profile has an AgentNexus identity behind it."""
        return self.stage in {"redeemed", "runtimes_configured", "complete"}


def isolation_for(name: str) -> str:
    """Which runtime context a profile gets, decided by its name and nothing else.

    `default` keeps the runtime's own home, because that is where every installation made before
    this slice already registered its entry and breaking those is not an upgrade. Every *named*
    profile is isolated, because the moment there are two identities on one machine, a shared
    runtime context would expose both signing keys to one agent.
    """
    return "shared" if name == DEFAULT_PROFILE_NAME else "isolated"


def list_profiles(install_root: Path) -> list[ProfileSummary]:
    """Summarise every profile in this installation, in name order."""
    root = profiles_root(install_root)
    if not root.is_dir():
        return []
    summaries: list[ProfileSummary] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.name.startswith(INCOMING_PREFIX):
            continue
        if PROFILE_NAME_PATTERN.match(entry.name) is None:
            continue
        summaries.append(summarise_profile(install_root, entry.name))
    return summaries


def summarise_profile(install_root: Path, name: str) -> ProfileSummary:
    """Read one profile's public summary without touching its key."""
    directory = profile_directory(install_root, name)
    document = _read_state_document(directory / "state.json")
    return ProfileSummary(
        name=name,
        directory=directory,
        stage=_optional_string(document.get("stage")),
        handle=_optional_string(document.get("handle")),
        agent_id=_optional_string(document.get("agent_id")),
        key_present=(directory / "keys" / "agent.pem").is_file(),
        runtimes=tuple(str(item) for item in (document.get("runtimes") or [])),
        isolation=isolation_for(name),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _read_state_document(path: Path) -> dict[str, Any]:
    """Read a state file for reporting only, tolerating anything unreadable.

    Deliberately lenient where `State.load` is strict: `profile list` exists partly to help
    somebody understand a broken installation, so it must not be the command that refuses to run
    because one profile out of three has a damaged file.
    """
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


# ---------------------------------------------------------------------------------------------
# Migration from the single-profile layout
# ---------------------------------------------------------------------------------------------

#: What the pre-profile layout put directly in the install root.
LEGACY_ENTRIES: Final = ("state.json", "keys", "backups")


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """What migration did, so setup can report it and a test can assert it."""

    performed: bool
    profile: str
    retired_to: Path | None
    detail: str


def legacy_layout_present(install_root: Path) -> bool:
    """Whether this install root still holds a pre-profile identity."""
    root = Path(install_root)
    return (root / "state.json").is_file() or (root / "keys" / "agent.pem").is_file()


def migrate_legacy_profile(install_root: Path, *, stdout: TextIO | None = None) -> MigrationResult:
    """Move a pre-profile installation into the `default` profile, without ever risking its key.

    The ordering is the design. Everything is **copied** into a hidden staging directory first and
    the key's digest is compared before anything else happens; the staging directory becomes the
    profile through a single rename, which either happened or did not; only then is the old layout
    moved aside, and *moved*, never deleted, because a private key with no other copy is not
    something an installer gets to remove.

    Every step is re-entrant. A run interrupted before the rename leaves a staging directory that
    the next run clears, with the original untouched. A run interrupted after it finds the profile
    already in place and finishes the retirement step. Running it on an already-migrated or
    never-legacy installation does nothing at all.
    """
    root = Path(install_root)
    _refuse_reparse_point(root, what="the AgentNexus install root")
    profiles = profiles_root(root)
    _clear_stale_staging(profiles, root)

    destination = profiles / DEFAULT_PROFILE_NAME
    if destination.is_dir() and (destination / "state.json").is_file():
        retired = _retire_legacy_entries(root, stdout=stdout)
        return MigrationResult(
            performed=False,
            profile=DEFAULT_PROFILE_NAME,
            retired_to=retired,
            detail="the default profile already exists; nothing was migrated",
        )

    if not legacy_layout_present(root):
        return MigrationResult(
            performed=False,
            profile=DEFAULT_PROFILE_NAME,
            retired_to=None,
            detail="no pre-profile installation was found",
        )

    if destination.exists():
        message = f"{destination} exists but holds no state file."
        raise ProfileError(
            message,
            recovery=(
                "An earlier migration was interrupted in an unexpected way. Move that directory "
                "aside so the original identity in the install root can be migrated cleanly."
            ),
        )

    if stdout is not None:
        stdout.write("  Migrating this machine's existing identity into the `default` profile\n")

    profiles.mkdir(parents=True, exist_ok=True)
    _harden(profiles)
    staging = profiles / f"{INCOMING_PREFIX}{DEFAULT_PROFILE_NAME}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    _harden(staging)

    try:
        for entry in LEGACY_ENTRIES:
            source = root / entry
            if not source.exists():
                continue
            _refuse_reparse_point(source, what=f"the existing {entry}")
            if source.is_dir():
                shutil.copytree(source, staging / entry)
            else:
                shutil.copy2(source, staging / entry)

        _verify_copied_key(root / "keys" / "agent.pem", staging / "keys" / "agent.pem")
        # The path recorded is the one the profile will have *after* the rename, never the
        # staging path: the recorded value becomes a runtime entry's key location, and a runtime
        # pointed at a directory that exists for one more instruction is not a working agent.
        _repoint_state(
            staging / "state.json",
            staged_key=staging / "keys" / "agent.pem",
            final_key=destination / "keys" / "agent.pem",
        )
        ProfileRecord(
            name=DEFAULT_PROFILE_NAME,
            created_at=_now(),
            runtime={"isolation": isolation_for(DEFAULT_PROFILE_NAME)},
            migrated_from=str(root),
        ).save(staging / "profile.json")
    except BaseException:
        # The original is still exactly where it was: nothing outside the staging directory has
        # been touched yet, so removing the partial copy is the complete cleanup.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # The one irreversible step, and it is a single rename: it either produced the profile or left
    # the staging directory for the next run to clear.
    os.rename(staging, destination)
    retired = _retire_legacy_entries(root, stdout=stdout)
    return MigrationResult(
        performed=True,
        profile=DEFAULT_PROFILE_NAME,
        retired_to=retired,
        detail=f"migrated the existing identity into {destination}",
    )


def _clear_stale_staging(profiles: Path, install_root: Path) -> None:
    """Remove a staging directory an interrupted migration left behind.

    Safe only while the original is still in place, which is checked rather than assumed: the
    staging directory holds a *copy* of a private key, and deleting the only copy of a key because
    a directory looked half-built is precisely the failure this module exists to prevent.
    """
    if not profiles.is_dir():
        return
    for entry in list(profiles.iterdir()):
        if not entry.name.startswith(INCOMING_PREFIX) or not entry.is_dir():
            continue
        if _is_reparse_point(entry):
            continue
        if (entry / "keys" / "agent.pem").is_file() and not legacy_layout_present(install_root):
            message = f"{entry} holds a key and the original it was copied from is gone."
            raise ProfileError(
                message,
                recovery=(
                    "An interrupted migration left the only copy of a private key there. Inspect "
                    "it and move it to the profile it belongs to rather than re-running setup."
                ),
            )
        shutil.rmtree(entry, ignore_errors=True)


def _verify_copied_key(source: Path, copy: Path) -> None:
    """Prove the migrated key is byte-identical before the migration is allowed to continue."""
    if not source.is_file():
        return
    if not copy.is_file():
        message = f"The private key was not copied to {copy}."
        raise ProfileError(message)
    if _digest(source) != _digest(copy):
        message = "The migrated private key does not match the original byte for byte."
        raise ProfileError(
            message,
            recovery=(
                "Nothing was moved and the original key is untouched. Re-run setup; if this "
                "repeats, the install root's filesystem is the thing to look at."
            ),
        )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repoint_state(state_file: Path, *, staged_key: Path, final_key: Path) -> None:
    """Point the migrated state at the key inside its profile.

    The recorded path is what becomes the runtime entry's `AGENTNEXUS_PRIVATE_KEY_FILE`, so
    leaving it on the retired location would produce a working profile whose runtime reads a key
    from a directory this connector no longer maintains.
    """
    if not state_file.is_file():
        return
    try:
        document = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        message = f"The existing state file could not be read: {error}"
        raise ProfileError(
            message,
            recovery="Nothing was moved. Inspect that file, then run setup again.",
        ) from error
    if not isinstance(document, dict):
        message = "The existing state file is not a JSON object."
        raise ProfileError(message)
    if staged_key.is_file():
        document["private_key_path"] = str(final_key)
    write_json_atomically(state_file, document)


def _retire_legacy_entries(install_root: Path, *, stdout: TextIO | None = None) -> Path | None:
    """Move the pre-profile files aside, keeping every byte of them.

    Moved rather than removed: this directory is the migration's backup, and it is the applicant's
    to delete once their agent works. `profile doctor` names it so it does not become a mystery.
    """
    root = Path(install_root)
    present = [entry for entry in LEGACY_ENTRIES if (root / entry).exists()]
    if not present:
        return None

    retirement = root / LEGACY_RETIREMENT_DIRECTORY_NAME
    retirement.mkdir(parents=True, exist_ok=True)
    _harden(retirement)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    for entry in present:
        source = root / entry
        target = retirement / entry
        if target.exists():
            target = retirement / f"{entry}-{stamp}"
        os.replace(source, target)

    (retirement / "README.txt").write_text(
        "These files are the AgentNexus installation as it was before named profiles existed.\n"
        "They were moved here, not copied: the live identity now lives in\n"
        f"{profiles_root(root) / DEFAULT_PROFILE_NAME}.\n\n"
        "This directory is kept as the migration's backup and contains a private key. Nothing in\n"
        "AgentNexus reads it any more. Delete it yourself once the `default` profile works, and\n"
        "treat it as key material until you do.\n",
        encoding="utf-8",
    )
    if stdout is not None:
        stdout.write(f"  Previous layout kept as a backup in {retirement}\n")
    return retirement


# ---------------------------------------------------------------------------------------------
# The installation manifest
# ---------------------------------------------------------------------------------------------

#: Its own version line, separate from `PROFILE_SCHEMA_VERSION`. A profile written before this
#: file existed must keep working, so `profile.json` is untouched and this is additive.
INSTALLATION_SCHEMA_VERSION: Final = 1

#: What removal reads to decide what AgentNexus may take back.
INSTALLATION_FILE_NAME: Final = "installation.json"


@dataclass
class InstallationManifest:
    """What AgentNexus created for one profile, so removal can take back exactly that.

    Removal needs answers a directory listing cannot give. Did this connector create the runtime
    profile, or did it adopt one the applicant already had? Is the `SOUL.md` sitting in that
    profile the one AgentNexus wrote, or has the applicant edited it since? Guessing either wrong
    destroys somebody's work, and neither is recoverable from names on disk.

    **It is not the source of truth for identity.** `state.json` already holds the agent and key
    identifiers, and it is written after each irreversible step of setup. This file records
    *provenance* — what was created versus adopted, and what a file looked like when AgentNexus
    last wrote it. That split is deliberate: a lost or corrupt manifest must never make an
    identity unrecoverable, and it does not, because nothing here is the only copy of anything.

    **A profile without one is normal, not broken.** Every profile installed before this file
    existed has none, and removal handles that by keeping whatever it cannot prove it owns. The
    manifest can make removal *braver*; its absence only ever makes it more conservative.

    Nothing secret goes in it: identifiers the server already published, local paths, a filename,
    and digests of a document the applicant can read. No key material, no invitation, no provider
    credential.
    """

    profile: str
    agent_handle: str = ""
    agent_id: str = ""
    key_id: str = ""
    runtime: str = ""
    #: Relative to the profile root when it lives inside it, which is the only case setup writes.
    private_key_path: str = ""
    mcp_server_name: str = ""
    #: True only when this connector ran the runtime's own "create profile" command. False when it
    #: adopted a profile that already existed — in which case `--purge-runtime-profile` is
    #: destroying something AgentNexus never made, and says so.
    created_runtime_profile: bool = False
    #: Where AgentNexus wrote a soul, and what it wrote. Empty when it never wrote one.
    soul_path: str = ""
    soul_digest: str = ""
    connector_version: str = ""
    installed_at: str = ""
    updated_at: str = ""

    def to_document(self) -> dict[str, Any]:
        """Serialise the manifest. Every field is an identifier, a path, or a digest."""
        return {
            "schema_version": INSTALLATION_SCHEMA_VERSION,
            "profile": self.profile,
            "agent_handle": self.agent_handle,
            "agent_id": self.agent_id,
            "key_id": self.key_id,
            "runtime": self.runtime,
            "private_key_path": self.private_key_path,
            "mcp_server_name": self.mcp_server_name,
            "created_runtime_profile": self.created_runtime_profile,
            "soul_path": self.soul_path,
            "soul_digest": self.soul_digest,
            "connector_version": self.connector_version,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def load(cls, path: Path) -> InstallationManifest | None:
        """Read a manifest, or return ``None`` when there is nothing usable to read.

        Unreadable is treated as absent rather than fatal, and that is the important decision. A
        corrupt manifest must not be able to block the removal of an agent somebody is trying to
        retire — removal without it is conservative, not impossible, so failing here would turn a
        damaged file into a locked door.
        """
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        if document.get("schema_version") != INSTALLATION_SCHEMA_VERSION:
            return None
        return cls(
            profile=str(document.get("profile", "")),
            agent_handle=str(document.get("agent_handle", "")),
            agent_id=str(document.get("agent_id", "")),
            key_id=str(document.get("key_id", "")),
            runtime=str(document.get("runtime", "")),
            private_key_path=str(document.get("private_key_path", "")),
            mcp_server_name=str(document.get("mcp_server_name", "")),
            created_runtime_profile=bool(document.get("created_runtime_profile", False)),
            soul_path=str(document.get("soul_path", "")),
            soul_digest=str(document.get("soul_digest", "")),
            connector_version=str(document.get("connector_version", "")),
            installed_at=str(document.get("installed_at", "")),
            updated_at=str(document.get("updated_at", "")),
        )

    def save(self, path: Path) -> None:
        """Write the manifest beside the profile's other records, readable by its owner only."""
        moment = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        if not self.installed_at:
            self.installed_at = moment
        self.updated_at = moment
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _harden(path)


def soul_digest_of(path: Path) -> str:
    """Return the digest of a soul file as it is on disk right now, or ``""`` when there is none.

    Over the file's bytes, deliberately: comparing rendered text would call a document changed
    because its line endings differ, and comparing anything looser would call an edited document
    unchanged. The question this answers is "is this still the exact file AgentNexus wrote", and
    only the bytes can answer it.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------------------------------------
# The retirement record
# ---------------------------------------------------------------------------------------------

#: Its own version, separate from the installation manifest's: this file outlives the profile
#: directory the manifest lives in, and the two are read at different moments by different code.
RETIREMENT_SCHEMA_VERSION: Final = 1

#: Written beside a quarantined key, inside its own timestamped directory.
RETIREMENT_FILE_NAME: Final = "retirement.json"


@dataclass
class RetirementRecord:
    """What a removed profile left behind, so a later run knows what it is looking at.

    Removal deletes the profile directory, and with it `state.json`, `profile.json` and
    `installation.json` — every record of which identity that key belonged to. Without this file a
    second run would have nothing to go on but the *name* of a directory, and would be guessing
    that `retired-keys/lexilux-20260902T101500Z` is the key of the agent called `lexilux`. A
    similar name is not evidence, and a wrong guess here either destroys the wrong key or hands an
    operator the wrong identifiers to revoke.

    So the identifiers are written down at the moment they are still known, next to the key they
    describe. A later run reads this rather than inferring anything.

    Nothing secret is in it. The agent and key identifiers are values the server published, the
    fingerprint is a digest of a *public* key, the path is a local path, and the agent API address
    is routing information the applicant's own setup command already carried in the clear. There
    is no private key material, no invitation, and no provider credential.
    """

    profile: str
    agent_handle: str = ""
    agent_id: str = ""
    key_id: str = ""
    #: SHA-256 of the raw public key, exactly as the server reports it.
    key_fingerprint: str = ""
    #: Absolute path of the quarantined private key. The file it names is key material; this
    #: record is not.
    private_key_path: str = ""
    #: Where the signed agent API lives, kept so a later run can ask that server whether this key
    #: still authenticates. Routing information, not a credential.
    agent_api_url: str = ""
    removed_at: str = ""
    #: `quarantined` while the key is still here, `destroyed` once it has been deleted.
    state: str = "quarantined"
    #: What is known about the server side: `not_verified` until a signed probe has been refused
    #: for an identity or key status reason, `revoked` once one has.
    server_retirement: str = "not_verified"
    verified_at: str = ""
    #: The problem code the server answered with when the probe proved it. Never a message.
    verified_code: str = ""

    def to_document(self) -> dict[str, Any]:
        """Serialise the record. Every field is an identifier, a digest, a path, or a state."""
        return {
            "schema_version": RETIREMENT_SCHEMA_VERSION,
            "profile": self.profile,
            "agent_handle": self.agent_handle,
            "agent_id": self.agent_id,
            "key_id": self.key_id,
            "key_fingerprint": self.key_fingerprint,
            "private_key_path": self.private_key_path,
            "agent_api_url": self.agent_api_url,
            "removed_at": self.removed_at,
            "state": self.state,
            "server_retirement": self.server_retirement,
            "verified_at": self.verified_at,
            "verified_code": self.verified_code,
        }

    @classmethod
    def load(cls, path: Path) -> RetirementRecord | None:
        """Read one retirement record, or return ``None`` when there is nothing usable."""
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        if document.get("schema_version") != RETIREMENT_SCHEMA_VERSION:
            return None
        return cls(
            profile=str(document.get("profile", "")),
            agent_handle=str(document.get("agent_handle", "")),
            agent_id=str(document.get("agent_id", "")),
            key_id=str(document.get("key_id", "")),
            key_fingerprint=str(document.get("key_fingerprint", "")),
            private_key_path=str(document.get("private_key_path", "")),
            agent_api_url=str(document.get("agent_api_url", "")),
            removed_at=str(document.get("removed_at", "")),
            state=str(document.get("state", "quarantined")),
            server_retirement=str(document.get("server_retirement", "not_verified")),
            verified_at=str(document.get("verified_at", "")),
            verified_code=str(document.get("verified_code", "")),
        )

    def save(self, path: Path) -> None:
        """Write the record beside the key it describes, readable by its owner only."""
        if not self.removed_at:
            self.removed_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _harden(path)


def find_retirements(install_root: Path, profile: str) -> list[RetirementRecord]:
    """Return every recorded retirement for one profile, oldest first.

    Selected by what each record *says* its profile is, never by what its directory is called.
    That distinction is the whole point of the file: `retired-keys/lexilux-2026...` looking like it
    belongs to `lexilux` is a coincidence of naming, and removal must not act on a coincidence.
    """
    retired = Path(install_root) / RETIRED_DIRECTORY_NAME
    if not retired.is_dir():
        return []
    found: list[tuple[str, RetirementRecord]] = []
    for entry in sorted(retired.iterdir()):
        if not entry.is_dir():
            continue
        record = RetirementRecord.load(entry / RETIREMENT_FILE_NAME)
        if record is None or record.profile != profile:
            continue
        # The record names its key; the directory it was found in says where that key is now. A
        # quarantine directory that somebody moved or renamed still describes itself correctly,
        # and resolving the recorded *file name* beside the record is not a guess — it is the
        # only file the record ever referred to.
        if record.private_key_path:
            beside = entry / Path(record.private_key_path).name
            if beside.is_file():
                record.private_key_path = str(beside)
        found.append((record.removed_at, record))
    return [record for _stamp, record in sorted(found, key=lambda pair: pair[0])]
