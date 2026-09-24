"""Controlled connector updates: check what is available, move chosen profiles onto it.

This is C2, the *foundation*. It does nothing on its own: a person runs `update check`, reads what
it says, and then runs `update apply --profile <name>` for the profiles they chose. There is no
schedule, no service, no scheduled task, no background activation and no privilege escalation
anywhere in this module — that is C3, and it is not implemented.

## What an update actually has to change

An installation is side by side already: the loader installs each release into
``<install root>/connector/<version>/venv`` and never touches an older one. The thing that binds a
profile to a version is not the package — it is the **runtime registration**. `build_server_spec`
records the absolute path of `agentnexus-agent-mcp` inside one version's virtual environment, so a
profile keeps starting the old server until that entry is rewritten. Installing a new wheel and
stopping there changes nothing an agent will notice, which is why this module treats installation
and activation as two separate steps with two separate outcomes.

## The trust chain is the loader's, reused

`release.py` already implements it: a signature over the exact manifest bytes, checked before the
document is parsed at all; a re-canonicalisation check so a differently-ordered document cannot
inherit a signature; artifact URLs pinned to the configured origin; and size plus SHA-256 verified
against the manifest before anything is installed. This module calls that code and adds no second
opinion.

**The connector wheel** is never resolved by name against a package index: pip is handed a verified
file on disk. **Its dependencies are.** `httpx2`, `cryptography` and `pyyaml` are fetched by pip
from whatever index that machine is configured to use, exactly as `connect.sh` and `connect.ps1`
already fetch them. The signature chain covers the connector's own bytes and nothing beyond them,
and saying otherwise would claim a guarantee this does not have.

## What is never destroyed

An update may add an installation. It may not take one away.

The `.installed` marker records that a version was checked; its **absence proves nothing**, because
every release the published loaders installed has no marker and those loaders are not being
changed. So a directory holding a working connector is adopted or refused, never cleared — and a
directory this module cannot account for at all is refused with the reason rather than removed.
Only a directory carrying this module's own staging marker, with no working installation under it,
may be deleted, and cleanup on failure touches only what the failed attempt itself created.

`classify_version` is where that judgement lives, and it distinguishes *unmarked* — somebody's
installation, usually the loader's — from *ours-partial* and from *unrecognised*.

Adoption checks that the package in the directory imports, lists its MCP tools and reports the
version its directory name claims. It does **not** establish that those bytes ever passed a
signature check, because nothing on disk records that, so it is recorded as `adopted` and never
reported as `verified`.

## Locking

Two locks, and the order between them is fixed: **installation first, then profile.**

`installation_lock` covers the shared `connector/<version>/` tree, which no profile lock reaches —
two updates for two different profiles are two legitimate concurrent runs, and without it both
would arrive at the same directory with nothing between them. It is taken *before* the directory's
state is read, because a decision made on an unlocked read is one another process may invalidate
before it is acted on. `install_release` takes only this lock; `activate_version` takes only a
profile lock; nothing takes them the other way round, so they cannot deadlock.

**The shell loaders take no lock at all.** A `connect.sh` or `connect.ps1` run happening at the
same moment as an update is outside what this serialises, and changing that means changing signed
installers, which is a release rather than a correction. What limits the damage is that neither
side deletes: the loaders reuse an existing virtual environment and this module never removes one.

## What is deliberately not decided here

* **Nothing is restarted.** A running agent holds its MCP server open, and swapping the executable
  under a live process is how a half-written state becomes an outage. A profile that has been
  re-registered is reported as `restart required`, never as running.
* **Version-bound cron commands are not rewritten.** A scheduled command that names
  ``connector/0.4.1/venv/...`` keeps naming it. Finding and editing a person's scheduler entries is
  not something this should do quietly; `update apply` says which profiles moved so the owner can
  adjust their own schedules.
* **Downgrades are refused**, with one exception: rolling back the activation this command just
  failed to complete, to the version the profile had before it. There is no general downgrade.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from agentnexus_sdk import version as _version
from agentnexus_sdk.release import Artifact, ReleaseError, ReleaseManifest, verify_manifest

#: The release key's public coordinates, stamped at build time exactly as the loaders' are.
#:
#: A committed checkout carries the placeholders and this module refuses to verify anything, which
#: is the same fail-closed rule `connect.sh` has: an updater that trusted whatever it downloaded
#: would be a worse hole than having no updater. `scripts/build_release.py` substitutes
#: them into the copy it builds the wheel from, so the first release that carries this module is
#: also the first one whose updater can verify a manifest.
RELEASE_PUBLIC_KEY_X: Final = "REPLACE_RELEASE_PUBLIC_KEY_X"
RELEASE_PUBLIC_KEY_Y: Final = "REPLACE_RELEASE_PUBLIC_KEY_Y"

#: Written into a version directory once its package has been proven to work there.
#:
#: Its **absence proves nothing.** Every release installed by `connect.ps1` or `connect.sh` — which
#: is every installation that exists today — has no marker, because the loaders never wrote one and
#: are not being changed for this. A directory without a marker is therefore an ordinary
#: installation far more often than it is a leftover, and treating the two alike is how an update
#: would delete the very connector it is running from. `classify_version` is what tells them apart.
INSTALLED_MARKER: Final = ".installed"

#: Written *before* anything is downloaded, and removed on the way out.
#:
#: This is the only evidence that a version directory belongs to an interrupted attempt of ours.
#: Nothing else can distinguish "we were part way through creating this" from "somebody else's
#: installation is here", so nothing else may authorise a delete.
STAGING_MARKER: Final = ".installing"

#: Where the release manifest and its detached signature live under an origin.
MANIFEST_PATH: Final = "/connector/connector-release.json"
SIGNATURE_PATH: Final = MANIFEST_PATH + ".sig"

#: Bounds on what a fetch may return, mirroring the loaders'.
MAX_MANIFEST_BYTES: Final = 65_536
MAX_SIGNATURE_BYTES: Final = 256

#: What `update apply` reports per profile. Only `updated` changed a runtime registration.
STATUS_UPDATED: Final = "updated"
STATUS_ALREADY_CURRENT: Final = "already-current"
STATUS_FAILED: Final = "failed"
STATUS_SKIPPED: Final = "skipped"


class UpdateError(Exception):
    """Refuse an update, carrying something the operator can act on."""

    def __init__(self, message: str, *, recovery: str | None = None) -> None:
        """Carry the recovery step beside the failure, the way the connector's errors do."""
        super().__init__(message)
        self.recovery = recovery


#: How bytes are fetched. Injected so a test never reaches a network and the caller decides the
#: transport; the connector passes one built on its own HTTP client.
Fetcher = Callable[[str, int], bytes]


def https_fetcher() -> Fetcher:
    """Return the fetcher the CLI uses: HTTPS only, bounded, and streamed.

    Bounded before the body is in memory rather than after. `limit` is the largest response this
    caller will accept, and a stream that goes past it is abandoned mid-download — a manifest
    endpoint that answers with a gigabyte must not become a memory problem before the signature
    check gets a chance to reject it.

    HTTPS is required. The trust chain does not depend on transport security, but there is no
    reason to fetch a release over a channel anyone can rewrite, and refusing here keeps a
    misconfigured origin from looking like a signature failure later.
    """
    import httpx2 as httpx

    from agentnexus_sdk.version import USER_AGENT

    def fetch(url: str, limit: int) -> bytes:
        if not url.startswith("https://"):
            message = f"Refusing to fetch a release over a non-HTTPS address: {url}"
            raise UpdateError(
                message, recovery="Point the connector at an https:// origin and try again."
            )
        chunks: list[bytes] = []
        total = 0
        with (
            httpx.Client(
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                message = f"{url} answered HTTP {response.status_code}."
                raise UpdateError(
                    message,
                    recovery=(
                        "Nothing was installed. A 404 here usually means the origin is not "
                        "serving this release yet."
                    ),
                )
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > limit:
                    message = f"{url} returned more than the {limit} bytes this call accepts."
                    raise UpdateError(
                        message,
                        recovery=(
                            "Nothing was installed. The published bytes and the signed manifest "
                            "disagree about the size, which needs looking at at the origin."
                        ),
                    )
                chunks.append(chunk)
        return b"".join(chunks)

    return fetch


def trusted_release_key() -> tuple[str, str]:
    """Return the stamped public coordinates, or refuse.

    Fail closed. An unstamped build is a development checkout, and there is no safe default for
    "which key do I trust" — guessing one would make every later check theatre.
    """
    if RELEASE_PUBLIC_KEY_X.startswith("REPLACE_") or RELEASE_PUBLIC_KEY_Y.startswith("REPLACE_"):
        message = "This connector build carries no release key, so it cannot verify an update."
        raise UpdateError(
            message,
            recovery=(
                "This is a development or unstamped build. Install a published release, which "
                "carries the key, and run the update from there."
            ),
        )
    return RELEASE_PUBLIC_KEY_X, RELEASE_PUBLIC_KEY_Y


# ---------------------------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------------------------


def connector_root(install_root: Path) -> Path:
    """Return the directory holding one subdirectory per installed version."""
    return Path(install_root) / "connector"


def version_root(install_root: Path, version: str) -> Path:
    """Return one installed version's directory. Side by side with every other."""
    if "/" in version or "\\" in version or version in {"", ".", ".."}:
        message = f"{version!r} is not a usable version directory name."
        raise UpdateError(message)
    return connector_root(install_root) / version


def venv_bin(root: Path, *, system: str) -> Path:
    """Return the virtual environment's executable directory, named per platform."""
    return root / "venv" / ("Scripts" if system == "Windows" else "bin")


def venv_python(root: Path, *, system: str) -> Path:
    """Return the interpreter inside one version's virtual environment."""
    return venv_bin(root, system=system) / ("python.exe" if system == "Windows" else "python")


def mcp_executable(root: Path, *, system: str) -> Path:
    """Return the MCP server a registration points at. This is the version-bound path."""
    suffix = ".exe" if system == "Windows" else ""
    return venv_bin(root, system=system) / f"agentnexus-agent-mcp{suffix}"


#: What `classify_version` can conclude about a version directory.
STATE_ABSENT: Final = "absent"
STATE_COMPLETE: Final = "complete"
STATE_UNMARKED: Final = "unmarked"
STATE_OURS_PARTIAL: Final = "ours-partial"
STATE_UNRECOGNISED: Final = "unrecognised"


@dataclass(frozen=True)
class VersionState:
    """What is at `connector/<version>/`, and what may therefore be done to it."""

    version: str
    root: Path
    state: str
    detail: str
    #: How this directory came to be trusted, when it is. `"verified"` means this updater
    #: downloaded it against a signed manifest and checked its digest. `"adopted"` means it was
    #: already here — almost certainly from a loader — and was proven to run, which is a weaker
    #: claim and is never reported as the stronger one.
    provenance: str | None = None

    @property
    def usable(self) -> bool:
        """Report whether a profile may be pointed at this directory."""
        return self.state in {STATE_COMPLETE, STATE_UNMARKED}

    @property
    def removable(self) -> bool:
        """Report whether this updater may delete this directory.

        True for exactly one case: a directory carrying our own staging marker and no working
        installation. Everything else — including anything we merely failed to recognise — is
        somebody's installation until proven otherwise.
        """
        return self.state == STATE_OURS_PARTIAL


def classify_version(install_root: Path, version: str, *, system: str) -> VersionState:
    """Decide what is at a version directory without assuming it is ours.

    The order matters. A working MCP executable is checked before any marker, because that is what
    an installation *is*; the marker only records who put it there and how well it was checked.
    """
    root = version_root(install_root, version)
    if not root.exists():
        return VersionState(version, root, STATE_ABSENT, "nothing is installed at this version")
    if mcp_executable(root, system=system).is_file():
        if (root / INSTALLED_MARKER).is_file():
            return VersionState(
                version,
                root,
                STATE_COMPLETE,
                "installed and recorded by this connector",
                provenance=_recorded_provenance(root),
            )
        return VersionState(
            version,
            root,
            STATE_UNMARKED,
            "an existing installation with no record of how it was installed, which is what the "
            "published loaders leave behind",
        )
    if (root / STAGING_MARKER).is_file():
        return VersionState(
            version, root, STATE_OURS_PARTIAL, "an interrupted install started by this connector"
        )
    return VersionState(
        version,
        root,
        STATE_UNRECOGNISED,
        "a directory with no usable connector in it and no sign this connector created it",
    )


def _recorded_provenance(root: Path) -> str | None:
    """Read back how a marked installation was established, tolerating an older plain marker."""
    try:
        document = json.loads((root / INSTALLED_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = document.get("provenance") if isinstance(document, dict) else None
    return value if isinstance(value, str) else None


def is_installed(install_root: Path, version: str, *, system: str) -> bool:
    """Report whether a profile may be pointed at this version.

    True for an installation this connector made *and* for one that was already here. The second
    is the whole point: every connector installed before this module existed has no marker, and
    refusing to see those would make the first update on every machine impossible.
    """
    return classify_version(install_root, version, system=system).usable


def installed_versions(install_root: Path, *, system: str) -> list[str]:
    """Return every usable installed version, oldest first by version order.

    Includes installations this connector did not make. A version the loader put there is
    installed by any honest reading of the word.
    """
    root = connector_root(install_root)
    if not root.is_dir():
        return []
    found = [
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and is_installed(install_root, entry.name, system=system)
    ]
    return sorted(found, key=version_key)


def version_key(version: str) -> tuple[int, ...]:
    """Order versions numerically, so 0.4.10 sorts after 0.4.9 rather than before it."""
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(character for character in piece if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


# ---------------------------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileVersions:
    """Describe one profile's version situation, with unknowns left unknown."""

    profile: str
    #: The version whose MCP executable this profile's runtime registration points at, read back
    #: from the runtime's own configuration. `None` means unknown, and `detail` says why.
    registered: str | None
    #: Never inferred, never filled in. Nothing in this process can see inside a live agent's MCP
    #: server, so this stays `None` and is reported as unknown. A registration is what a profile
    #: will start next, not what it is running.
    running: str | None = None
    #: Why `registered` is what it is: which runtime answered, or why none could.
    detail: str = ""


@dataclass(frozen=True)
class UpdateCheck:
    """Answer "is there something newer, and where does this machine stand"."""

    origin: str
    available: str
    artifact: Artifact
    manifest: ReleaseManifest
    installed: tuple[str, ...]
    profiles: tuple[ProfileVersions, ...]

    @property
    def already_installed(self) -> bool:
        """Report whether the available version is already installed and verified here."""
        return self.available in self.installed


def fetch_manifest(origin: str, *, fetch: Fetcher) -> ReleaseManifest:
    """Download and verify the release manifest. Nothing is written and nothing is installed."""
    x_hex, y_hex = trusted_release_key()
    base = origin.rstrip("/")
    try:
        raw = fetch(f"{base}{MANIFEST_PATH}", MAX_MANIFEST_BYTES)
        signature_hex = fetch(f"{base}{SIGNATURE_PATH}", MAX_SIGNATURE_BYTES)
    except UpdateError:
        raise
    except Exception as error:
        message = f"The release manifest could not be fetched from {base}: {error}"
        raise UpdateError(
            message, recovery="Check the connection to the origin and try again."
        ) from error
    try:
        signature = bytes.fromhex(signature_hex.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as error:
        message = "The release signature is not hex text."
        raise UpdateError(
            message, recovery="Refetch it; a truncated download looks like this."
        ) from error
    try:
        return verify_manifest(raw, signature, x_hex=x_hex, y_hex=y_hex, origin=base)
    except ReleaseError as error:
        message = f"The release manifest did not verify: {error}"
        raise UpdateError(
            message,
            recovery=(
                "Nothing was installed. Do not work around this: an unverifiable manifest is "
                "either a corrupted download or a manifest this connector must not trust."
            ),
        ) from error


def version_from_command(command: object, *, install_root: Path) -> str | None:
    """Read the version out of a registered MCP command path, or return None.

    Deliberately conservative: a version is reported only when the registered path really does sit
    under this installation's ``connector/<version>/`` tree. A command somewhere else is somebody
    else's arrangement, and naming a version for it would be a guess.
    """
    if not isinstance(command, str) or not command:
        return None
    try:
        relative = Path(command).resolve().relative_to(connector_root(install_root).resolve())
    except (ValueError, OSError):
        return None
    return relative.parts[0] if relative.parts else None


def inspect_profile(*, install_root: Path, profile: str, environment: Any) -> ProfileVersions:
    """Report which version one profile is registered against, asking the runtimes themselves.

    The runtime's own configuration is the source, because it is what will actually be started.
    The profile record is a fallback for a runtime no longer installed on this machine, and a
    runtime that cannot be read yields `None` with the reason rather than an optimistic guess.
    """
    from agentnexus_sdk.connector import Paths, State
    from agentnexus_sdk.profiles import ProfileError, ProfileRecord
    from agentnexus_sdk.runtimes import ADAPTERS, RuntimeIntegrationError

    paths = Paths.for_profile(install_root, profile)
    try:
        record = ProfileRecord.load(paths.profile_record)
    except ProfileError as error:
        return ProfileVersions(profile=profile, registered=None, detail=str(error))
    if record is None:
        return ProfileVersions(
            profile=profile, registered=None, detail="no profile record; setup never finished here"
        )

    state = State.load(paths.state_file)
    context = paths.runtime_context()
    for name in sorted(set(state.runtimes) or set(ADAPTERS)):
        factory = ADAPTERS.get(name)
        if factory is None:
            continue
        adapter = factory(which=environment.which, runner=environment.run, context=context)
        if not adapter.detect().installed:
            continue
        try:
            entry = adapter.existing_entry()
        except RuntimeIntegrationError as error:
            return ProfileVersions(
                profile=profile,
                registered=None,
                detail=f"{adapter.display_name} could not be read: {error}",
            )
        if entry is None:
            continue
        found = version_from_command(entry.get("command"), install_root=install_root)
        detail = (
            f"registered with {adapter.display_name}"
            if found
            else f"{adapter.display_name} starts a command outside this installation"
        )
        return ProfileVersions(profile=profile, registered=found, detail=detail)

    recorded = version_from_command(record.runtime.get("command"), install_root=install_root)
    if recorded is not None:
        return ProfileVersions(
            profile=profile,
            registered=recorded,
            detail="from the profile record; no configured runtime answered",
        )
    return ProfileVersions(
        profile=profile, registered=None, detail="no runtime on this machine holds an entry"
    )


def check_for_update(
    *,
    install_root: Path,
    origin: str,
    profiles: Sequence[str],
    fetch: Fetcher,
    environment: Any,
) -> UpdateCheck:
    """Report what is available and where this machine stands.

    Installs nothing, downloads no artifact, and touches no profile. This is what a person runs
    before deciding anything, so it has to be safe to run at any moment.
    """
    manifest = fetch_manifest(origin, fetch=fetch)
    return UpdateCheck(
        origin=origin.rstrip("/"),
        available=manifest.connector_version,
        artifact=manifest.artifact_for("any"),
        manifest=manifest,
        installed=tuple(installed_versions(install_root, system=environment.system)),
        profiles=tuple(
            inspect_profile(install_root=install_root, profile=name, environment=environment)
            for name in profiles
        ),
    )


# ---------------------------------------------------------------------------------------------
# Installing, side by side
# ---------------------------------------------------------------------------------------------


@dataclass
class InstallResult:
    """Record where a version ended up, and whether this call is what put it there."""

    version: str
    root: Path
    newly_installed: bool
    notes: list[str] = field(default_factory=list)


def install_release(
    *,
    install_root: Path,
    manifest: ReleaseManifest,
    fetch: Fetcher,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    system: str,
    python_executable: str | None = None,
) -> InstallResult:
    """Install one verified release beside the others, or account for what is already there.

    Serialised on the installation lock for its whole length, and the state of the directory is
    read **after** the lock is taken, never before: a decision made on an unlocked read is a
    decision another process is free to invalidate before it is acted on.

    The rule this function exists to keep is that **an existing installation is never destroyed.**
    A missing `.installed` marker is not evidence of anything — the published loaders never write
    one — so a directory with a working connector in it is adopted or refused, never cleared. Only
    a directory carrying this connector's own staging marker, with no working installation in it,
    may be removed, and only files this attempt wrote are ever cleaned up on failure.

    The order of checks is the loader's: the caller verifies the manifest signature, then this
    checks the artifact's size and digest against that manifest before any of those bytes reach an
    interpreter. The connector wheel is therefore never resolved by name against a package index.
    Its **dependencies are**, exactly as the loaders resolve them: `pip` reads its configured index
    to satisfy `httpx2`, `cryptography` and `pyyaml`. That is unchanged, deliberate, and not
    something this signature chain covers.
    """
    from agentnexus_sdk.profiles import ProfileError, installation_lock

    version = manifest.connector_version
    artifact = manifest.artifact_for("any")

    try:
        with installation_lock(install_root):
            return _install_locked(
                install_root=install_root,
                version=version,
                artifact=artifact,
                fetch=fetch,
                runner=runner,
                system=system,
                python_executable=python_executable,
            )
    except ProfileError as error:
        raise UpdateError(str(error), recovery=error.recovery) from error


def _install_locked(
    *,
    install_root: Path,
    version: str,
    artifact: Artifact,
    fetch: Fetcher,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    system: str,
    python_executable: str | None,
) -> InstallResult:
    """Do the installation. The caller holds the installation lock for the whole call."""
    root = version_root(install_root, version)
    existing = classify_version(install_root, version, system=system)

    if existing.state == STATE_COMPLETE:
        return InstallResult(
            version=version,
            root=root,
            newly_installed=False,
            notes=[f"already installed here ({existing.provenance or 'recorded'})"],
        )

    if existing.state == STATE_UNMARKED:
        # Almost always the loader's work, and possibly the very installation this process is
        # running from. It is not deleted, not overwritten and not reinstalled over: it is checked,
        # and either recorded as usable or refused with the reason.
        return _adopt_existing(root, version=version, runner=runner, system=system)

    if existing.state == STATE_UNRECOGNISED:
        message = f"{root} already exists and does not look like anything this can safely replace."
        raise UpdateError(
            message,
            recovery=(
                "Nothing was downloaded or changed. Look at that directory: if it is a failed "
                "install of your own, move it aside; if it is somebody's installation, leave it."
            ),
        )

    # From here the directory is either absent or provably a leftover of our own, so this attempt
    # owns whatever it creates and may clean up after itself.
    owns_directory = existing.state == STATE_ABSENT
    if existing.state == STATE_OURS_PARTIAL:
        shutil.rmtree(root, ignore_errors=True)
        owns_directory = True

    written: list[Path] = []

    def undo() -> None:
        """Remove only what this attempt created, and nothing that was here before it."""
        if owns_directory:
            shutil.rmtree(root, ignore_errors=True)
            return
        for created in reversed(written):  # pragma: no cover - unreachable while owns is always
            with contextlib.suppress(OSError):
                created.unlink()

    try:
        root.mkdir(parents=True, exist_ok=True)
        staging = root / STAGING_MARKER
        staging.write_text(f"{version}\n", encoding="utf-8")
        written.append(staging)
    except OSError as error:
        message = f"The install directory {root} could not be created: {error}"
        raise UpdateError(
            message, recovery="Nothing was installed. Free space or fix permissions."
        ) from error

    try:
        try:
            # Bounded at exactly the signed size. A correct artifact is that long; anything longer
            # is refused before it is all in memory, and anything shorter fails the digest check.
            payload = fetch(artifact.url, artifact.size)
        except UpdateError:
            # Already a precise refusal — a size bound, a non-HTTPS address, an HTTP status.
            # Wrapping it would hide a supply-chain answer behind a network one.
            raise
        except Exception as error:
            message = f"The connector artifact could not be downloaded: {error}"
            raise UpdateError(
                message, recovery="Nothing was installed. Try again when online."
            ) from error

        if not artifact.matches(payload):
            message = (
                f"The download does not match the manifest: {len(payload)} bytes against "
                f"{artifact.size}, or a different SHA-256."
            )
            raise UpdateError(
                message,
                recovery=(
                    "Nothing was installed. Refetch; if it fails again the published bytes and "
                    "the signed manifest disagree and the origin needs looking at."
                ),
            )

        wheel = root / artifact.filename
        try:
            wheel.write_bytes(payload)
            written.append(wheel)
        except OSError as error:
            message = f"The connector artifact could not be written to {root}: {error}"
            raise UpdateError(
                message, recovery="Free space or fix permissions, then try again."
            ) from error

        interpreter = python_executable or sys.executable
        environment_root = root / "venv"
        created = runner(
            [interpreter, "-m", "venv", str(environment_root)],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        if created.returncode != 0:
            message = "The isolated Python environment for the new version could not be created."
            raise UpdateError(
                message,
                recovery=(
                    "On Debian and Raspberry Pi OS this usually means the python3-venv package "
                    "is missing. Nothing was installed."
                ),
            )

        installed = runner(
            [
                str(venv_python(root, system=system)),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--no-input",
                "--upgrade",
                str(wheel),
            ],
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        if installed.returncode != 0:
            detail = (installed.stderr or installed.stdout or "").strip().splitlines()
            message = "The verified connector artifact could not be installed into its environment."
            raise UpdateError(
                message,
                recovery=(
                    "Nothing was activated and every existing installation is untouched. "
                    f"{detail[-1] if detail else 'No further detail.'}"
                ),
            )

        report = verify_installation(root, runner=runner, system=system)
        _require_reported_version(report, expected=version, root=root)
    except UpdateError:
        undo()
        raise
    except Exception:  # pragma: no cover - defensive; an unexpected failure must still not leak
        undo()
        raise

    _write_marker(root, version=version, provenance="verified")
    with contextlib.suppress(OSError):
        (root / STAGING_MARKER).unlink()
    return InstallResult(
        version=version,
        root=root,
        newly_installed=True,
        notes=["downloaded against the signed manifest, installed and verified"],
    )


def _adopt_existing(
    root: Path,
    *,
    version: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    system: str,
) -> InstallResult:
    """Record an installation that was already here, after proving it actually runs.

    This is the bootstrap case and the ordinary case at once: the first connector able to update
    was itself installed by the shell loader, which writes no marker, and every re-run of `update
    apply` for a version already present arrives here too.

    What is proven is what can be proven — that the package in that directory imports, lists its
    MCP tools, and reports the version its directory claims. What is **not** proven is that its
    bytes ever passed a signature check, because nothing on disk records that. It is recorded as
    adopted rather than verified, and the two are never conflated in the output.
    """
    report = verify_installation(root, runner=runner, system=system)
    _require_reported_version(report, expected=version, root=root)
    _write_marker(root, version=version, provenance="adopted")
    return InstallResult(
        version=version,
        root=root,
        newly_installed=False,
        notes=[
            "already installed here by the installer; checked that it runs and left unchanged",
            "adopted, not signature-verified: nothing on disk records how those bytes arrived",
        ],
    )


def _require_reported_version(report: dict[str, Any], *, expected: str, root: Path) -> None:
    """Refuse an installation whose package does not agree with the directory it sits in.

    A version directory is a claim about its contents, and a profile is pointed at it by path. If
    the package inside reports something else the directory name is not trustworthy, and neither
    is anything derived from it.
    """
    reported = report.get("version")
    if reported == expected:
        return
    message = f"The package in {root} reports version {reported!r}, not {expected!r}."
    raise UpdateError(
        message,
        recovery=(
            "No profile was changed and nothing was deleted. That directory holds a different "
            "version from the one its name claims; move it aside and install again."
        ),
    )


def _write_marker(root: Path, *, version: str, provenance: str) -> None:
    """Record that this version is usable, and how that was established."""
    document = {
        "version": version,
        "provenance": provenance,
        "recorded_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (root / INSTALLED_MARKER).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


#: What the freshly installed package is asked to prove about itself. Imports and tool discovery
#: only: no forum call, no provider request, no invitation, no credit.
_VERIFY_SNIPPET: Final = (
    "import json;"
    "import agentnexus_sdk;"
    "from agentnexus_sdk.mcp_server import TOOLS;"
    "names=sorted(t['name'] for t in TOOLS);"
    "assert 'create_reply' in names;"
    "print(json.dumps({'version': agentnexus_sdk.__version__, 'tools': names}))"
)


def verify_installation(
    root: Path, *, runner: Callable[..., subprocess.CompletedProcess[str]], system: str
) -> dict[str, Any]:
    """Prove the installed package imports and can list its MCP tools, before any profile moves.

    A pip install that returned zero is not evidence the package works: a broken dependency or a
    partial wheel shows up on first import, which is after a profile would already be pointing at
    it. So the check runs in the new environment, and a failure leaves the profile where it was.
    """
    completed = runner(
        [str(venv_python(root, system=system)), "-c", _VERIFY_SNIPPET],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = "The newly installed connector failed its own import and tool-discovery check."
        raise UpdateError(
            message,
            recovery=(f"No profile was changed. {detail[-1] if detail else 'No further detail.'}"),
        )
    try:
        return dict(json.loads(completed.stdout.strip().splitlines()[-1]))
    except (ValueError, IndexError) as error:
        message = "The installed connector's verification produced no readable answer."
        raise UpdateError(message, recovery="No profile was changed.") from error


# ---------------------------------------------------------------------------------------------
# Activating chosen profiles
# ---------------------------------------------------------------------------------------------


@dataclass
class ProfileOutcome:
    """One profile's result. The transaction boundary is one profile, never the whole run."""

    profile: str
    status: str
    detail: str
    previous_version: str | None = None
    new_version: str | None = None

    @property
    def ok(self) -> bool:
        """True when this profile needs no further attention."""
        return self.status in {STATUS_UPDATED, STATUS_ALREADY_CURRENT}


def refuse_downgrade(*, current: str | None, target: str, rollback_to: str | None) -> None:
    """Refuse moving a profile backwards, unless this is the rollback of a failed activation.

    There is no general downgrade command and this is not one: `rollback_to` is only ever the
    version a profile was on moments earlier, supplied by the failure handler below.
    """
    if current is None or version_key(target) >= version_key(current):
        return
    if rollback_to is not None and target == rollback_to:
        return
    message = f"Refusing to move the profile from {current} back to {target}."
    raise UpdateError(
        message,
        recovery=(
            "Updates only move forward. If a newer version is broken, re-register the older one "
            "deliberately rather than through an update."
        ),
    )


def activate_version(
    *,
    install_root: Path,
    version: str,
    profile: str,
    environment: Any,
    rollback_to: str | None = None,
) -> ProfileOutcome:
    """Point one profile's runtime registration at `version`, or leave it exactly as it was.

    This is where an update actually happens. Installing a package changes nothing an agent will
    notice: a profile keeps starting the MCP server whose absolute path its runtime registration
    records. So this rewrites that entry — and because rewriting a runtime's own configuration file
    can fail halfway, it goes through the adapters' existing backup and rollback path rather than
    inventing a second one.

    The profile's lock is held for the whole of the change, so a concurrent `setup` or a second
    `update apply` for the same profile is refused rather than interleaved. The lock is per
    profile, so one profile updating never blocks another. It is non-blocking, as everywhere else
    in the connector: a profile somebody else is already working on is reported, not waited for.

    Nothing is started, stopped or restarted. A profile that has been re-registered is reported as
    needing a restart, never as running the new version.
    """
    from agentnexus_sdk.profiles import ProfileError, profile_lock

    root = version_root(install_root, version)
    state = classify_version(install_root, version, system=environment.system)
    if not state.usable:
        # Says which of the several ways it is unusable, because "install it first" is the wrong
        # advice for a directory that is there but half-written or unrecognisable.
        return ProfileOutcome(
            profile=profile,
            status=STATUS_FAILED,
            detail=(f"{version} cannot be activated: {state.detail}. Nothing was changed."),
        )

    try:
        # Entered inside the guard rather than merely created inside it: `profile_lock` is a
        # generator context manager, so a profile another run already holds raises here. With the
        # guard around the creation alone, one locked profile aborted an `update apply` naming
        # several others instead of being reported as one failed profile among them.
        with profile_lock(install_root, profile):
            return _activate_locked(
                install_root=install_root,
                version=version,
                profile=profile,
                environment=environment,
                rollback_to=rollback_to,
                root=root,
            )
    except ProfileError as error:
        return ProfileOutcome(profile=profile, status=STATUS_FAILED, detail=str(error))


def _activate_locked(
    *,
    install_root: Path,
    version: str,
    profile: str,
    environment: Any,
    rollback_to: str | None,
    root: Path,
) -> ProfileOutcome:
    """Perform the activation itself. The caller holds this profile's lock for the whole call.

    Imports are local: `connector` imports this module for its CLI, so a module-level import back
    would be a cycle.
    """
    from agentnexus_sdk.connector import Endpoints, Environment, Identity, Paths, State
    from agentnexus_sdk.connector import build_server_spec as _build_server_spec
    from agentnexus_sdk.profiles import ProfileError, ProfileRecord
    from agentnexus_sdk.runtimes import ADAPTERS, RuntimeIntegrationError

    paths = Paths.for_profile(install_root, profile)
    try:
        record = ProfileRecord.load(paths.profile_record)
    except ProfileError as error:
        return ProfileOutcome(profile=profile, status=STATUS_FAILED, detail=str(error))
    if record is None:
        return ProfileOutcome(
            profile=profile,
            status=STATUS_SKIPPED,
            detail="no profile record; setup has never finished for this profile",
        )

    state = State.load(paths.state_file)
    if not state.agent_id or not state.key_id:
        return ProfileOutcome(
            profile=profile,
            status=STATUS_SKIPPED,
            detail="this profile records no registered identity to re-register",
        )

    current = inspect_profile(
        install_root=install_root, profile=profile, environment=environment
    ).registered
    refuse_downgrade(current=current, target=version, rollback_to=rollback_to)
    if current == version:
        return ProfileOutcome(
            profile=profile,
            status=STATUS_ALREADY_CURRENT,
            detail=f"already registered against {version}; nothing was changed",
            previous_version=current,
            new_version=version,
        )

    endpoints = Endpoints.from_record(record.endpoints)
    if not endpoints.agent_api_url:
        return ProfileOutcome(
            profile=profile,
            status=STATUS_SKIPPED,
            detail="the profile record names no Agent API address to re-register against",
        )
    identity = Identity(agent_id=state.agent_id, key_id=state.key_id, handle=state.handle or "")

    # The one line that makes this an update rather than a rewrite of the same thing: resolve
    # the MCP executable out of the *new* version's environment instead of whichever one this
    # process happens to be running from. Identity, key path and addresses are unchanged, so
    # the agent keeps its account — only the executable moves.
    target_environment = Environment(
        stdout=environment.stdout,
        stderr=environment.stderr,
        system=environment.system,
        executable_directory=venv_bin(root, system=environment.system),
    )
    spec = _build_server_spec(
        identity=identity,
        private_key_path=state.private_key_path or str(paths.private_key),
        endpoints=endpoints,
        environment=target_environment,
        profile=profile,
    )
    expected = mcp_executable(root, system=environment.system)
    if Path(spec.command) != expected:
        return ProfileOutcome(
            profile=profile,
            status=STATUS_FAILED,
            detail=f"the new version's MCP server is not at {expected}; nothing was changed",
        )

    context = paths.runtime_context()
    configured: list[tuple[Any, Any]] = []
    try:
        for name in sorted(set(state.runtimes)):
            factory = ADAPTERS.get(name)
            if factory is None:
                continue
            adapter = factory(which=environment.which, runner=environment.run, context=context)
            if not adapter.detect().installed:
                continue
            outcome = adapter.configure(spec, backup_directory=paths.backups)
            configured.append((adapter, outcome))
    except RuntimeIntegrationError as error:
        # Put back exactly what this call changed, newest first, and leave the profile on the
        # version it had. A package that installed successfully is not an updated profile.
        restored = []
        for adapter, outcome in reversed(configured):
            if not outcome.changed:
                continue
            try:
                adapter.rollback(outcome.backup)
            except RuntimeIntegrationError as failure:  # pragma: no cover - defensive
                restored.append(f"{adapter.display_name} could not be restored: {failure}")
            else:
                restored.append(f"{adapter.display_name} restored")
        note = "; ".join(restored) if restored else "nothing had been changed yet"
        return ProfileOutcome(
            profile=profile,
            status=STATUS_FAILED,
            detail=f"{error} ({note})",
            previous_version=current,
        )

    if not configured:
        return ProfileOutcome(
            profile=profile,
            status=STATUS_SKIPPED,
            detail="none of this profile's runtimes are installed on this machine",
            previous_version=current,
        )

    record.runtime = {**record.runtime, "command": spec.command, "version": version}
    record.save(paths.profile_record)
    changed = [adapter.display_name for adapter, outcome in configured if outcome.changed]
    return ProfileOutcome(
        profile=profile,
        status=STATUS_UPDATED,
        detail=(
            f"re-registered with {', '.join(changed) or 'no runtime that needed changing'}; "
            "restart the agent for it to take effect"
        ),
        previous_version=current,
        new_version=version,
    )


def update_profiles(
    *,
    install_root: Path,
    version: str,
    profiles: Sequence[str],
    environment: Any,
) -> list[ProfileOutcome]:
    """Activate `version` for each explicitly chosen profile, independently.

    **The transaction boundary is one profile.** Each is locked, changed and reported on its own,
    and a failure on one neither rolls back nor prevents the others: they are separate identities
    with separate runtime entries, and undoing a profile that succeeded because a different one
    failed would be the surprising behaviour. Every outcome is reported, and the caller's exit code
    reflects whether any of them failed.
    """
    outcomes: list[ProfileOutcome] = []
    for profile in profiles:
        try:
            outcomes.append(
                activate_version(
                    install_root=install_root,
                    version=version,
                    profile=profile,
                    environment=environment,
                )
            )
        except UpdateError as error:
            outcomes.append(
                ProfileOutcome(profile=profile, status=STATUS_FAILED, detail=str(error))
            )
    return outcomes


def running_version_note() -> str:
    """Return what this process can honestly say about what is *running*.

    It can name the version of the connector executing this line, and nothing about the MCP server
    a live agent holds open: that is a different process this one cannot inspect. Saying so plainly
    is the point — an update that reported success while the old server kept serving its old tool
    list would be exactly the failure C1 was.
    """
    return (
        f"This connector is {_version.__version__}. The version a running agent has loaded cannot "
        "be read from here and stays unknown until that agent restarts."
    )
