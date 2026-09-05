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
opinion. **Nothing is ever installed from a package index**: pip is given a verified file on disk.

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
#: would be a worse hole than having no updater. `scripts/build_connector_release.py` substitutes
#: them into the copy it builds the wheel from, so the first release that carries this module is
#: also the first one whose updater can verify a manifest.
RELEASE_PUBLIC_KEY_X: Final = "REPLACE_RELEASE_PUBLIC_KEY_X"
RELEASE_PUBLIC_KEY_Y: Final = "REPLACE_RELEASE_PUBLIC_KEY_Y"

#: Written into a version directory only after its package verified. A directory without it is a
#: partial install: interrupted, or verified and rejected. Neither may ever be activated.
INSTALLED_MARKER: Final = ".installed"

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


def is_installed(install_root: Path, version: str, *, system: str) -> bool:
    """Report whether a version finished installing *and* verified."""
    root = version_root(install_root, version)
    return (root / INSTALLED_MARKER).is_file() and mcp_executable(root, system=system).is_file()


def installed_versions(install_root: Path, *, system: str) -> list[str]:
    """Return every completely installed version, oldest first by version order."""
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
    """Install one verified release beside the others, or report it is already there.

    The order is the loader's: verify the manifest (done by the caller), then check the artifact's
    size and digest against it *before* anything reaches an interpreter, then install that file —
    never a name resolved against a package index.

    A version directory counts as installed only once its package has been verified and the marker
    written. An interrupted run therefore leaves a directory that no later call will activate.
    """
    version = manifest.connector_version
    artifact = manifest.artifact_for("any")
    root = version_root(install_root, version)

    if is_installed(install_root, version, system=system):
        return InstallResult(
            version=version,
            root=root,
            newly_installed=False,
            notes=["already installed and verified"],
        )

    # A leftover from an interrupted run is removed rather than trusted or merged into.
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    try:
        # Bounded at exactly the signed size. A correct artifact is that long; anything longer is
        # refused before it is all in memory, and anything shorter fails the digest check below.
        payload = fetch(artifact.url, artifact.size)
    except UpdateError:
        # Already a precise refusal — a size bound, a non-HTTPS address, an HTTP status. Wrapping
        # it in "could not be downloaded" would hide a supply-chain answer behind a network one.
        shutil.rmtree(root, ignore_errors=True)
        raise
    except Exception as error:
        shutil.rmtree(root, ignore_errors=True)
        message = f"The connector artifact could not be downloaded: {error}"
        raise UpdateError(
            message, recovery="Nothing was installed. Try again when online."
        ) from error

    if not artifact.matches(payload):
        shutil.rmtree(root, ignore_errors=True)
        message = (
            f"The download does not match the manifest: {len(payload)} bytes against "
            f"{artifact.size}, or a different SHA-256."
        )
        raise UpdateError(
            message,
            recovery=(
                "Nothing was installed. Refetch; if it fails again the published bytes and the "
                "signed manifest disagree and the origin needs looking at."
            ),
        )

    wheel = root / artifact.filename
    try:
        wheel.write_bytes(payload)
    except OSError as error:
        shutil.rmtree(root, ignore_errors=True)
        message = f"The connector artifact could not be written to {root}: {error}"
        raise UpdateError(
            message, recovery="Free space or fix permissions, then try again."
        ) from error

    interpreter = python_executable or sys.executable
    venv = root / "venv"
    created = runner(
        [interpreter, "-m", "venv", str(venv)], capture_output=True, encoding="utf-8", check=False
    )
    if created.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        message = "The isolated Python environment for the new version could not be created."
        raise UpdateError(
            message,
            recovery=(
                "On Debian and Raspberry Pi OS this usually means the python3-venv package is "
                "missing. Nothing was installed."
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
        shutil.rmtree(root, ignore_errors=True)
        message = "The verified connector artifact could not be installed into its environment."
        raise UpdateError(
            message, recovery="Nothing was activated. The previous version is untouched."
        )

    verify_installation(root, runner=runner, system=system)
    (root / INSTALLED_MARKER).write_text(f"{version}\n", encoding="utf-8")
    return InstallResult(
        version=version, root=root, newly_installed=True, notes=["installed and verified"]
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
    if not is_installed(install_root, version, system=environment.system):
        return ProfileOutcome(
            profile=profile,
            status=STATUS_FAILED,
            detail=(
                f"{version} is not installed and verified here, so nothing was activated. "
                "Install it first."
            ),
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

    endpoints = Endpoints(
        onboarding_base_url=str(record.endpoints.get("onboarding_base_url", "")),
        agent_api_url=str(record.endpoints.get("agent_api_url", "")),
        public_api_url=str(record.endpoints.get("public_api_url", "")) or None,
        observer_url=str(record.endpoints.get("observer_url", "")) or None,
    )
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
