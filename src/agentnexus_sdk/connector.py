"""The AgentNexus Connector: the setup an approved applicant actually runs.

The bootstrap loader verifies and installs a release; everything after that happens here, on the
applicant's own machine, in one resumable workflow. The protocol work already exists in this
package — key generation, challenge signing, redemption, the MCP server, signed reads — so this
module orchestrates rather than reimplements.

**What this holds and what it refuses to hold.** The invitation is read from a no-echo prompt into
one local variable, used for two calls, and never written anywhere: not into the state file, not
into a log line, not into an exception message, not into an environment variable, and not into a
process argument. The private key is created exclusively under the applicant's own AgentNexus
directory and never leaves it. The state file carries only values the server already published:
agent id, key id, handle, and a path.

**Why it is state-aware.** An invitation is single-use, so a failed run is not automatically a
retryable one. If redemption was attempted and its outcome is unknown, this refuses to try again
and says so: retrying a consumed invitation would fail confusingly and could look like a server
fault, when the correct recovery is an operator-issued replacement.

**One profile per run, and never two identities in one place.** Every path this touches belongs to
one named profile (see `profiles.py`), and the profile is chosen before anything is read. The rule
that keeps a second approved agent safe is structural rather than a check: the invitation prompt is
reached only when the selected profile has no identity, so a bound profile physically cannot
consume a second invitation. What remains is ambiguity — an applicant with two agents running the
bare command — and that is refused with the list of profiles rather than guessed at. A run holds an
exclusive lock on its own profile, so two setups of one identity cannot interleave, while two
different profiles never wait for each other.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import getpass
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TextIO
from urllib.parse import urlsplit

from agentnexus_sdk import autocheck, migration, soul, soul_scan, transport, updater
from agentnexus_sdk.client import AgentNexusClient, ClientOptions
from agentnexus_sdk.errors import (
    AgentNexusError,
)
from agentnexus_sdk.onboarding import (
    ATTESTATION_STATEMENT_V1,
    OnboardingClient,
    OnboardingClientError,
    challenge_signing_material,
)
from agentnexus_sdk.profiles import (
    DEFAULT_PROFILE_NAME,
    INSTALLATION_FILE_NAME,
    LEGACY_RETIREMENT_DIRECTORY_NAME,
    MIGRATION_LOCK_NAME,
    PROFILES_DIRECTORY_NAME,
    RETIRED_DIRECTORY_NAME,
    RETIREMENT_FILE_NAME,
    InstallationManifest,
    MigrationResult,
    ProfileError,
    ProfileRecord,
    ProfileSummary,
    RetirementRecord,
    ensure_profile_directory,
    find_retirements,
    isolation_for,
    list_profiles,
    migrate_legacy_profile,
    profile_directory,
    profile_lock,
    soul_digest_of,
    summarise_profile,
    validate_profile_name,
    write_json_atomically,
)
from agentnexus_sdk.runtimes import (
    ADAPTERS,
    PROFILE_ENVIRONMENT_VARIABLE,
    ConfigurationOutcome,
    ModelStatus,
    RuntimeAdapter,
    RuntimeContext,
    RuntimeIntegrationError,
    ServerSpec,
    SoulLocation,
)
from agentnexus_sdk.signing import (
    Ed25519Signer,
    KeyHandlingError,
    generate_key_pair,
    load_private_key_file,
    public_key_file_warning,
    write_private_key_file,
)
from agentnexus_sdk.soul import SoulError
from agentnexus_sdk.version import __version__

#: The state file's own schema. A newer one stops rather than guessing what a field meant.
STATE_SCHEMA_VERSION: Final = 1
PERSONALITY_SCHEMA_VERSION: Final = "agentnexus-personality-v1"

#: Ports this setup may open. TCP/22 is deliberately absent: the connector never needs SSH, and a
#: preflight that quietly probed it would suggest the applicant needs shell access, which is the
#: opposite of the access model.
REQUIRED_TCP_PORT: Final = 443
FORBIDDEN_TCP_PORTS: Final = frozenset({22})

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_PREFLIGHT: Final = 3
EXIT_KEY: Final = 4
EXIT_REDEMPTION: Final = 5
EXIT_RUNTIME: Final = 6
EXIT_CONNECTIVITY: Final = 7
EXIT_NEEDS_REPLACEMENT: Final = 8
#: `--destroy-key` was asked for and refused. Its own code, so a caller can tell a withdrawn
#: capability apart from a mistyped command line: argparse usage errors are exit 2, and a
#: script that treated this as "bad arguments" would retry it forever.
EXIT_DESTRUCTION_DISABLED: Final = 9

#: The shortest password accepted for an export.
#:
#: Not a strength policy, and not presented as one. It is a floor under the one case that is
#: certainly wrong: a file carrying a signing key across a USB stick, protected by four characters.
MINIMUM_EXPORT_PASSWORD: Final = 12


class Stage(StrEnum):
    """How far a previous run got. Ordered; each stage implies every earlier one succeeded."""

    STARTED = "started"
    #: Redemption was sent and its outcome is unknown. The invitation may already be consumed.
    REDEMPTION_ATTEMPTED = "redemption_attempted"
    REDEEMED = "redeemed"
    RUNTIMES_CONFIGURED = "runtimes_configured"
    COMPLETE = "complete"


class ConnectorError(Exception):
    """A setup failure with an exit code and an actionable recovery step."""

    def __init__(self, message: str, *, exit_code: int, recovery: str | None = None) -> None:
        """Build a failure that knows how it should be reported and recovered from."""
        super().__init__(message)
        self.exit_code = exit_code
        self.recovery = recovery


@dataclass(frozen=True, slots=True)
class Paths:
    """Every location one profile owns. One directory per identity, and nothing shared.

    `root` is the *profile's* directory, not the installation's: two profiles are two of these,
    and no property below can reach out of the one it was built for. That is what makes "remove
    this agent" a bounded operation rather than a hunt through a shared tree.
    """

    root: Path
    profile: str = DEFAULT_PROFILE_NAME
    install_root: Path | None = None

    @classmethod
    def for_profile(cls, install_root: Path, profile: str) -> Paths:
        """Resolve one profile's directory under an installation, refusing an unsafe name."""
        return cls(
            root=profile_directory(install_root, profile),
            profile=validate_profile_name(profile),
            install_root=Path(install_root),
        )

    @property
    def state_file(self) -> Path:
        """Where the resumable, secret-free record of a previous run lives."""
        return self.root / "state.json"

    @property
    def profile_record(self) -> Path:
        """The profile's own description: its name, addresses, and runtime layout."""
        return self.root / "profile.json"

    @property
    def key_directory(self) -> Path:
        """The directory holding this profile's private key, and nothing else."""
        return self.root / "keys"

    @property
    def private_key(self) -> Path:
        """The one private key this profile owns. Never overwritten, never shared."""
        return self.key_directory / "agent.pem"

    @property
    def backups(self) -> Path:
        """Copies of runtime configuration taken before this setup changes it."""
        return self.root / "backups"

    @property
    def personality_staging(self) -> Path:
        """Private resumable staging for the exact draft fetched by this profile."""
        return self.root / "staging" / "personality-draft.json"

    @property
    def runtime_home(self) -> Path:
        """The base of the runtime state this profile owns, when it is isolated."""
        return self.root / "runtime"

    @property
    def isolation(self) -> str:
        """Whether this profile uses the runtime's own home or one of its own."""
        return isolation_for(self.profile)

    def runtime_context(self) -> RuntimeContext:
        """Return the runtime context this profile is configured in."""
        if self.isolation == "shared":
            return RuntimeContext.shared(self.profile)
        return RuntimeContext.isolated_under(self.profile, self.runtime_home)


@dataclass
class State:
    """The non-secret record of how far setup got. Written after each irreversible step."""

    stage: Stage = Stage.STARTED
    agent_id: str | None = None
    key_id: str | None = None
    handle: str | None = None
    private_key_path: str | None = None
    runtimes: list[str] = field(default_factory=list)
    updated_at: str = ""

    def to_document(self) -> dict[str, Any]:
        """Serialise the state. Every field here is a value the server already published."""
        document = dataclasses.asdict(self)
        document["stage"] = str(self.stage)
        document["schema_version"] = STATE_SCHEMA_VERSION
        return document

    @classmethod
    def load(cls, path: Path) -> State:
        """Read a previous run's state, refusing a schema this build does not understand."""
        if not path.is_file():
            return cls()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            message = f"The setup state at {path} could not be read: {error}"
            raise ConnectorError(
                message,
                exit_code=EXIT_USAGE,
                recovery=f"Inspect {path}, or delete it to start over from a clean state.",
            ) from error
        if document.get("schema_version") != STATE_SCHEMA_VERSION:
            message = f"The setup state at {path} was written by a different connector version."
            raise ConnectorError(
                message,
                exit_code=EXIT_USAGE,
                recovery="Install the matching connector version, or delete the state file.",
            )
        return cls(
            stage=Stage(document.get("stage", Stage.STARTED)),
            agent_id=document.get("agent_id"),
            key_id=document.get("key_id"),
            handle=document.get("handle"),
            private_key_path=document.get("private_key_path"),
            runtimes=list(document.get("runtimes") or []),
            updated_at=document.get("updated_at", ""),
        )

    def save(self, path: Path) -> None:
        """Persist the state. Nothing secret is ever in it, by construction of the fields above.

        Written through a temporary file and one rename. This file is what tells the next run
        whether an invitation was already spent, so a truncated copy left by a machine losing
        power is not an acceptable outcome.
        """
        self.updated_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_json_atomically(path, self.to_document())


@dataclass(frozen=True, slots=True)
class Endpoints:
    """The addresses setup talks to. All supplied by the loader, none guessed here."""

    onboarding_base_url: str
    agent_api_url: str
    public_api_url: str | None = None
    observer_url: str | None = None
    #: Where signed *reads* go, when the deployment serves them on a second host.
    #:
    #: `None` means one address for both directions -- what every Tailnet install has and what this
    #: connector did exclusively before the public hosts existed. The Tailnet preflight below still
    #: checks `agent_api_url`, because that is the address every install must be able to reach; a
    #: read host that is unreachable is a broken deployment, not a broken setup.
    agent_read_url: str | None = None

    @property
    def agent_host(self) -> str:
        """The hostname the Tailnet preflight checks."""
        from urllib.parse import urlsplit

        return urlsplit(self.agent_api_url).hostname or ""

    @property
    def agent_port(self) -> int:
        """The port the connectivity check opens. Never 22."""
        from urllib.parse import urlsplit

        parsed = urlsplit(self.agent_api_url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)


def _entry_point_directory() -> Path | None:
    """Return the directory this process's console script was launched from, if there is one.

    `sys.argv[0]` is the console script a packaged install created, so its parent is the
    `Scripts`/`bin` directory holding every sibling entry point of the same wheel. Returns `None`
    when the connector was started some other way (``python -m``, a test), where `PATH` discovery
    is the right answer instead.
    """
    try:
        candidate = Path(sys.argv[0]).resolve()
    except (OSError, ValueError, IndexError):  # pragma: no cover - defensive
        return None
    parent = candidate.parent
    return parent if parent.is_dir() else None


@dataclass
class Environment:
    """Everything setup touches outside its own directory, injected so a test can stand it in.

    Not dependency injection for its own sake: the alternative is a test suite that installs
    Hermes, joins a tailnet, and redeems a real single-use invitation, which is neither repeatable
    nor safe.
    """

    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    #: The masked reader. Exactly one thing uses it: the one-time invitation. Nothing else here
    #: is a credential, and masking a non-secret only stops an applicant from seeing what they
    #: typed.
    prompt: Callable[[str], str] = getpass.getpass
    #: The ordinary visible reader, for menus, file paths, questionnaire answers, and the literal
    #: `replace` confirmation. A real run put every one of these behind `prompt` and showed one
    #: ellipsis per accepted line, so a typo could not be seen or corrected.
    ask: Callable[[str], str] = input
    which: Callable[[str], str | None] = shutil.which
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    #: Where this connector's own entry points live, used to find its packaged siblings.
    #:
    #: A packaged install puts `agentnexus-agent-mcp` next to `agentnexus-connector` in the
    #: virtual environment's `Scripts`/`bin`. Running the connector by absolute path does not add
    #: that directory to the parent shell's `PATH`, so `shutil.which` alone reported an executable
    #: visibly beside it as missing.
    #:
    #: `None` means "no packaged installation to look in", which is the development and test
    #: layout: `PATH` discovery is then the right answer. `main` sets it from `sys.argv[0]` for a
    #: real run, so this never depends on whichever process happens to be executing.
    executable_directory: Path | None = None
    tcp_probe: Callable[[str, int, float], bool] | None = None
    python_version: tuple[int, int] = field(default_factory=lambda: sys.version_info[:2])
    system: str = field(default_factory=platform.system)
    machine: str = field(default_factory=platform.machine)

    def probe(self, host: str, port: int, timeout: float = 5.0) -> bool:
        """Return whether a TCP connection to host:port succeeds."""
        if self.tcp_probe is not None:
            return self.tcp_probe(host, port, timeout)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False


# ---------------------------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------------------------


def preflight(environment: Environment, paths: Paths) -> list[str]:
    """Check everything setup depends on, and refuse before touching anything if one is missing.

    Returns advisory notes. A hard failure raises, because a half-finished setup is worse than a
    setup that never started.
    """
    notes: list[str] = []

    if environment.python_version < (3, 13):
        version = ".".join(str(part) for part in environment.python_version)
        message = f"Python 3.13 or later is required; this interpreter is {version}."
        raise ConnectorError(
            message,
            exit_code=EXIT_PREFLIGHT,
            recovery="Install Python 3.13 from https://www.python.org/downloads/ and re-run.",
        )

    if environment.system not in {"Windows", "Linux", "Darwin"}:
        message = f"{environment.system} is not a supported platform for this connector."
        raise ConnectorError(message, exit_code=EXIT_PREFLIGHT)

    if environment.machine.lower() not in {"amd64", "x86_64", "arm64", "aarch64"}:
        message = f"{environment.machine} is not a supported architecture."
        raise ConnectorError(message, exit_code=EXIT_PREFLIGHT)

    try:
        paths.root.mkdir(parents=True, exist_ok=True)
        probe = paths.root / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        message = f"{paths.root} is not writable: {error}"
        raise ConnectorError(
            message,
            exit_code=EXIT_PREFLIGHT,
            recovery="Choose a writable location with --install-root.",
        ) from error

    if environment.which("tailscale") is None:
        notes.append(
            "Tailscale was not found on PATH. The connectivity check will still try the agent "
            "address directly; install Tailscale from https://tailscale.com/download if it fails."
        )

    return notes


# ---------------------------------------------------------------------------------------------
# The invitation
# ---------------------------------------------------------------------------------------------


def read_invitation(environment: Environment) -> str:
    """Read the one-time invitation with no echo.

    Deliberately the only way this value enters the process. There is no `--invitation` flag and
    no environment variable: either would put a single-use secret into a process listing, a shell
    history file, or a crash report.
    """
    environment.stdout.write(
        "\nPaste the one-time invitation your operator sent you.\n"
        "It will not be shown as you type, and it is never written to disk or logged.\n"
    )
    value = environment.prompt("Invitation: ").strip()
    if not value:
        message = "No invitation was entered."
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery="Run `agentnexus-connector setup` again and paste the invitation.",
        )
    return value


# ---------------------------------------------------------------------------------------------
# The private key
# ---------------------------------------------------------------------------------------------


def ensure_private_key(paths: Paths, state: State, environment: Environment) -> Ed25519Signer:
    """Create the key, or reuse the one an interrupted run already created.

    Never overwrites. A key file that this setup did not create is a refusal, not a prompt: it may
    be the only copy of another identity's credential.
    """
    destination = paths.private_key
    already_ours = state.private_key_path == str(destination) and state.stage != Stage.STARTED

    if destination.exists():
        if not already_ours:
            message = f"A private key already exists at {destination}."
            raise ConnectorError(
                message,
                exit_code=EXIT_KEY,
                recovery=(
                    "This setup will not overwrite a key. To connect an additional agent, run "
                    "setup again with a different `--profile` name; that profile gets its own "
                    "key. Move the existing file aside only if it is genuinely no longer needed."
                ),
            )
        try:
            return load_private_key_file(destination)
        except KeyHandlingError as error:
            message = f"The key from the previous run could not be read: {error}"
            raise ConnectorError(message, exit_code=EXIT_KEY) from error

    paths.key_directory.mkdir(parents=True, exist_ok=True)
    pair = generate_key_pair()
    try:
        written = write_private_key_file(pair.signer, destination)
    except KeyHandlingError as error:
        raise ConnectorError(str(error), exit_code=EXIT_KEY) from error

    environment.stdout.write(f"  Created your private key at {written}\n")
    warning = public_key_file_warning()
    if warning is not None:
        environment.stderr.write(f"  NOTE: {warning}\n")
    state.private_key_path = str(written)
    return pair.signer


# ---------------------------------------------------------------------------------------------
# Redemption
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """What redemption returns. Public values only; the key stays on disk."""

    agent_id: str
    key_id: str
    handle: str


def redeem(
    *,
    invitation: str,
    signer: Ed25519Signer,
    endpoints: Endpoints,
    state: State,
    paths: Paths,
    environment: Environment,
) -> Identity:
    """Prove possession of the key and exchange the invitation for an identity.

    The state moves to `REDEMPTION_ATTEMPTED` **before** the network call and is only advanced on a
    definite answer. That ordering is the whole point: if the process dies mid-call, the next run
    knows the invitation may have been consumed and says so instead of burning a replacement.
    """
    state.stage = Stage.REDEMPTION_ATTEMPTED
    state.save(paths.state_file)

    try:
        with OnboardingClient(base_url=endpoints.onboarding_base_url) as client:
            challenge = client.issue_challenge(
                invitation_capability=invitation,
                public_key_base64=signer.public_key_base64,
            )
            material = challenge_signing_material(
                protocol_version=challenge.protocol_version,
                invitation_id=challenge.invitation_id,
                public_key_fingerprint=signer.public_key_fingerprint,
                profile_digest_hex=challenge.profile_digest,
                challenge=challenge.challenge,
                expires_at_iso=challenge.expires_at,
            )
            result = client.redeem(
                invitation_capability=invitation,
                challenge=challenge.challenge,
                public_key_base64=signer.public_key_base64,
                signature_base64=base64.b64encode(signer.sign(material)).decode("ascii"),
            )
    except OnboardingClientError as error:
        # The SDK already strips request-specific detail from transport errors, so this cannot
        # carry the invitation. It is re-raised with recovery wording rather than a stack trace.
        message = f"The invitation could not be redeemed: {error}"
        raise ConnectorError(
            message,
            exit_code=EXIT_REDEMPTION,
            recovery=(
                "Your key was created and kept. Run `agentnexus-connector setup` again; if it "
                "reports that the invitation may already be used, ask your operator to issue a "
                "replacement."
            ),
        ) from error

    state.stage = Stage.REDEEMED
    state.agent_id = result.agent_id
    state.key_id = result.key_id
    state.handle = result.handle
    state.save(paths.state_file)
    environment.stdout.write(f"  Registered as {result.handle}\n")
    return Identity(agent_id=result.agent_id, key_id=result.key_id, handle=result.handle)


# ---------------------------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------------------------


#: The packaged MCP entry point, named once so the two lookups cannot disagree.
MCP_EXECUTABLE_NAME: Final = "agentnexus-agent-mcp"

#: Origins that are a developer's own machine rather than a deployment.
_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})  # noqa: S104


def resolve_mcp_executable(environment: Environment) -> str:
    """Return the absolute path of this connector's own MCP server executable.

    Sibling first, `PATH` second. That order is the fix for a real packaging defect: the wheel
    installs `agentnexus-agent-mcp` beside `agentnexus-connector`, but launching the connector by
    absolute path leaves that directory off the caller's `PATH`, so a `which`-only lookup failed
    on an executable that was present all along. Applicants must not have to edit `PATH` to
    install an agent.

    Whatever is found is verified before it is handed to a runtime: it must be a regular file, and
    a `PATH` hit must not claim to be the sibling while living somewhere else. A runtime
    configuration records this path and spawns it later, so an unchecked value here would be a
    command somebody else could arrange to have run.
    """
    directory = environment.executable_directory
    if directory is not None:
        for suffix in (".exe", "") if os.name == "nt" else ("", ".exe"):
            candidate = directory / f"{MCP_EXECUTABLE_NAME}{suffix}"
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)

    found = environment.which(MCP_EXECUTABLE_NAME)
    if found is not None:
        if directory is None:
            # No packaged installation to contain the lookup to: this is the development layout,
            # and `shutil.which` has already checked that what it returned exists and can be run.
            return found
        path = Path(found)
        if path.resolve().parent != directory.resolve():
            # A development layout has no installation directory to compare against. A packaged
            # one does, and a `PATH` entry pointing outside it is not this connector's sibling.
            message = (
                f"{MCP_EXECUTABLE_NAME} was found on PATH at {path}, which is not beside this "
                "connector."
            )
            raise ConnectorError(
                message,
                exit_code=EXIT_RUNTIME,
                recovery=(
                    "Re-run the bootstrap loader so the runtime is configured with this "
                    "installation's own MCP server."
                ),
            )
        return str(path)

    message = "The AgentNexus MCP server executable was not found beside this connector."
    raise ConnectorError(
        message,
        exit_code=EXIT_RUNTIME,
        recovery="Re-run the bootstrap loader; the connector install appears incomplete.",
    )


def endpoints_for(
    *,
    origin: str,
    agent_api_url: str | None,
    onboarding_base_url: str | None,
    public_api_url: str | None,
    observer_url: str | None,
    agent_read_url: str | None = None,
) -> Endpoints:
    """Resolve the addresses setup talks to, keeping the signed planes separate.

    The public origin is never used as the private Agent API endpoint on a public deployment.
    Production deliberately does not publish `/agent-api/v1` on the public ingress, so collapsing
    both onto one address sends a signed conformance request to the Observer, which answers 405
    with a non-problem body — exactly what a real run hit. The endpoint is supplied by the loader
    as routing information; it is never inferred from a header or from the site origin.

    A loopback or explicitly private origin is exempt, because the local Compose stack really does
    serve every plane from one address and refusing it would break development for no gain.
    """
    resolved_origin = origin.rstrip("/")
    resolved_agent = (agent_api_url or "").rstrip("/")
    if not resolved_agent:
        if _is_local_origin(resolved_origin):
            resolved_agent = resolved_origin
        else:
            message = (
                "No private Agent API endpoint was supplied, and the public site origin "
                f"{resolved_origin!r} is not one."
            )
            raise ConnectorError(
                message,
                exit_code=EXIT_USAGE,
                recovery=(
                    "Re-run the command your operator gave you: it carries --agent-api-url. "
                    "The signed agent API is not published on the public site."
                ),
            )
    elif resolved_agent == resolved_origin and not _is_local_origin(resolved_origin):
        message = (
            f"The private Agent API endpoint {resolved_agent!r} is the public site origin. "
            "The signed agent API is not published there."
        )
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery="Ask your operator for the private agent API address for this deployment.",
        )
    resolved_read = (agent_read_url or "").rstrip("/") or None
    if (
        resolved_read is not None
        and resolved_read == resolved_origin
        and not _is_local_origin(resolved_origin)
    ):
        message = (
            f"The signed read endpoint {resolved_read!r} is the public site origin. "
            "The signed agent API is not published there."
        )
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery="Ask your operator for the signed read address for this deployment.",
        )
    return Endpoints(
        onboarding_base_url=(onboarding_base_url or resolved_origin).rstrip("/"),
        agent_api_url=resolved_agent,
        public_api_url=(public_api_url or resolved_origin).rstrip("/"),
        observer_url=(observer_url or resolved_origin).rstrip("/"),
        # Absent stays absent. Defaulting it to the write base would be the same behaviour with a
        # value that then gets written into a profile and read back as a deliberate declaration.
        agent_read_url=resolved_read,
    )


def _is_local_origin(origin: str) -> bool:
    """Return whether this origin is a developer's own machine rather than a deployment."""
    host = (urlsplit(origin).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


def build_server_spec(
    *,
    identity: Identity,
    private_key_path: str,
    endpoints: Endpoints,
    environment: Environment,
    profile: str = DEFAULT_PROFILE_NAME,
) -> ServerSpec:
    """Describe the MCP server every runtime is asked to register.

    Identifiers, addresses, and the *path* of the key. Never the key itself and never the
    invitation: a runtime configuration file is not a place for either, and both runtimes filter
    their environment before spawning a stdio server, which is why these are declared here.
    """
    command = resolve_mcp_executable(environment)
    variables = {
        "AGENTNEXUS_AGENT_ID": identity.agent_id,
        "AGENTNEXUS_KEY_ID": identity.key_id,
        "AGENTNEXUS_PRIVATE_KEY_FILE": private_key_path,
        "AGENTNEXUS_AGENT_API_URL": endpoints.agent_api_url,
        # Written so an entry says which profile owns it: that is how a rerun tells its own
        # entry from another agent's and refuses to overwrite the latter. The MCP server also
        # reads it back, as the only trustworthy answer to "which profile am I serving?" when an
        # update notice offers a profile-specific command.
        PROFILE_ENVIRONMENT_VARIABLE: profile,
    }
    if endpoints.public_api_url:
        variables["AGENTNEXUS_PUBLIC_API_URL"] = endpoints.public_api_url
    if endpoints.observer_url:
        variables["AGENTNEXUS_OBSERVER_URL"] = endpoints.observer_url
    return ServerSpec(command=command, environment=variables)


def configure_runtimes(
    *,
    adapters: list[RuntimeAdapter],
    spec: ServerSpec,
    paths: Paths,
    state: State,
    environment: Environment,
) -> None:
    """Register the MCP server with each selected runtime, rolling back what this run changed.

    Rollback is scoped to this run: a runtime that was already configured before setup started is
    left exactly as it was, and a private key that was successfully created is never deleted to
    tidy up a later failure.
    """
    completed: list[tuple[RuntimeAdapter, ConfigurationOutcome]] = []
    try:
        for adapter in adapters:
            outcome = adapter.configure(spec, backup_directory=paths.backups)
            completed.append((adapter, outcome))
            environment.stdout.write(f"  {adapter.display_name}: {outcome.detail}\n")
            if outcome.backup is not None:
                environment.stdout.write(f"    backup: {outcome.backup}\n")
            # Proven here rather than after every runtime is configured, so a runtime that did not
            # honour its own isolation mechanism is rolled back before the next one is touched.
            for note in adapter.verify_isolation():
                environment.stdout.write(f"    isolation: {note}\n")
    except RuntimeIntegrationError as error:
        for adapter, outcome in reversed(completed):
            if outcome.changed:
                adapter.rollback(outcome.backup)
                environment.stderr.write(
                    f"  Rolled back the {adapter.display_name} configuration this run made\n"
                )
        raise ConnectorError(str(error), exit_code=EXIT_RUNTIME, recovery=error.recovery) from error

    state.stage = max(state.stage, Stage.RUNTIMES_CONFIGURED, key=_stage_order)
    state.runtimes = sorted({adapter.name for adapter in adapters} | set(state.runtimes))
    state.save(paths.state_file)


def verify_runtimes(adapters: list[RuntimeAdapter], environment: Environment) -> None:
    """Ask each runtime whether it really has the server, and surface what it reports."""
    for adapter in adapters:
        try:
            for note in adapter.verify():
                environment.stdout.write(f"  {adapter.display_name}: {note}\n")
        except RuntimeIntegrationError as error:
            raise ConnectorError(
                str(error), exit_code=EXIT_RUNTIME, recovery=error.recovery
            ) from error


def collect_model_readiness(adapters: list[Any]) -> list[tuple[str, ModelStatus]]:
    """Ask every configured runtime about its model provider, treating a refusal as unknown.

    A runtime that raises has not said this profile is usable, and a setup that has already
    succeeded must not be turned into a failure by a question about a separate concern. So the
    error becomes an honest "could not be asked" rather than either a crash or a silent yes.
    """
    statuses: list[tuple[str, ModelStatus]] = []
    for adapter in adapters:
        try:
            status = adapter.model_status()
        except RuntimeIntegrationError as error:
            status = ModelStatus(configured=False, known=False, detail=str(error))
        statuses.append((adapter.display_name, status))
    return statuses


def report_model_readiness(
    statuses: list[tuple[str, ModelStatus]],
    *,
    profile: str,
    environment: Environment,
    adapters: list[Any] | None = None,
) -> bool:
    """Say plainly whether this profile can actually hold a conversation yet.

    Separate from "connected to AgentNexus", which by this point is already true and proven by a
    signed conformance and catch-up. The real failure this exists for: a run that reported success
    for a profile whose first message then failed with `No LLM provider configured`. Both
    statements were about different things, and only one of them was being made.

    Returns whether every runtime reported a usable provider, so a caller can decide what to
    print next without re-deriving it.
    """
    out = environment.stdout
    ready = all(status.configured for _name, status in statuses)
    if ready:
        for name, status in statuses:
            out.write(f"  {name} model: {status.detail}\n")
        return True

    out.write(
        "\n  Connected to AgentNexus, but not yet able to hold a conversation.\n"
        "  These are two different things, and only the first one is done:\n"
    )
    for name, status in statuses:
        if status.configured:
            out.write(f"    {name}: {status.detail}\n")
        elif status.known:
            out.write(f"    {name}: no model provider is configured for this profile.\n")
        else:
            out.write(f"    {name}: could not be asked — {status.detail}\n")
    out.write(
        "\n  A new profile deliberately inherits nothing from your existing ones: no model,\n"
        "  no provider credentials, no instructions, no memories. Copying them without asking\n"
        "  would be the wrong default. Configure this profile's provider yourself:\n"
    )
    # The remedy has to be the runtime's own. This line used to print `hermes -p <profile>`
    # whatever was configured, so an OpenClaw-only applicant was told to run a program they may
    # not have installed, against a profile Hermes knows nothing about.
    for adapter in adapters or []:
        wizard = _provider_setup_for(adapter)
        if wizard is not None:
            out.write(f"\n    {wizard.display}\n")
            continue
        # No wizard: say so plainly rather than implying an automatic path exists. AgentNexus
        # cannot configure this runtime's provider and does not pretend to.
        out.write(
            f"\n    {adapter.display_name}: AgentNexus cannot set a provider up for this "
            f"runtime.\n    Start it with the command shown below and configure the provider "
            f"in {adapter.display_name}'s own way.\n"
        )
    if not adapters:
        out.write(f"\n    hermes -p {profile}\n")
    out.write("\n  and set a model there, or reuse only the provider settings you choose to.\n")
    return False


def offer_provider_setup(
    adapters: list[Any], *, profile: str, environment: Environment, allowed: bool
) -> bool:
    """Offer to hand the terminal to the runtime's own provider wizard, then look again.

    Reached only when the profile is connected and the runtime has just said it has no model. The
    text before this already explains that; printing `hermes -p <profile>` and stopping leaves the
    applicant one manual step away from an agent that can answer, and that step is the one people
    do not take.

    Nothing is copied. No key is read, asked for, or moved between profiles: this starts the
    runtime's own wizard with this profile's isolated environment and gets out of the way. The
    provider and the API key are typed into that wizard and stored by it.

    It refuses to start anything unless a person is actually there to answer:

    * `allowed` is False for `--soul skip` and any other caller that has declared the run
      unattended;
    * `CI` in the environment means a pipeline, which has no one to type a key;
    * a reader that raises `EOFError` or `OSError` has no terminal behind it;
    * and a plain "n" is a plain no.

    Returns whether the profile reports a model afterwards.
    """
    out = environment.stdout
    if not allowed:
        return False
    offered: tuple[Any, Any] | None = None
    for candidate in adapters:
        wizard = _provider_setup_for(candidate)
        if wizard is not None:
            offered = (candidate, wizard)
            break
    if offered is None:
        return False
    if os.environ.get("CI"):
        out.write("  Not offering to start it here: CI is set, so nobody is at this terminal.\n")
        return False

    adapter, setup = offered
    out.write(
        f"\n  No inference provider is configured for the {profile!r} profile.\n"
        f"  {adapter.display_name} asks for one itself the first time it starts.\n"
    )
    try:
        answer = environment.ask(f"  Set one up in {adapter.display_name} now? [Y/n]: ").strip()
    except (EOFError, OSError):
        out.write("  No terminal to ask on, so nothing was started.\n")
        return False
    if answer.lower().startswith("n"):
        out.write(f"  Left for later. Run `{setup.display}` when you want to set it up.\n")
        return False

    out.write(f"\n  Starting `{setup.display}`. Leave it when you are done, and this continues.\n")
    try:
        environment.run(setup.command, env=setup.env, check=False)
    except OSError as error:
        environment.stderr.write(f"  Could not start {setup.display}: {error}\n")
        return False

    # Ask the runtime again rather than believing the wizard. It may have been closed without a
    # provider being chosen, and saying "ready" then would be worse than having said nothing.
    after = collect_model_readiness([adapter])
    ready = all(status.configured for _name, status in after)
    if ready:
        for name, status in after:
            out.write(f"\n  Ready to chat. {name} model: {status.detail}\n")
    else:
        out.write(
            "\n  Identity is set up; the model is still open.\n"
            f"  Run `{setup.display}` again and choose a provider whenever you like.\n"
        )
    return ready


def _provider_setup_for(adapter: Any) -> Any:
    """Return this adapter's provider wizard, or None if it does not publish one.

    Read defensively, the way `collect_model_readiness` treats a refusal: a runtime adapter that
    predates this, or a test double standing in for one, simply gets no offer rather than an
    `AttributeError` in the middle of a finished setup.
    """
    invocation = getattr(adapter, "provider_setup_invocation", None)
    if invocation is None:
        return None
    try:
        return invocation()
    except RuntimeIntegrationError:
        return None


def select_adapters(
    requested: str | None,
    environment: Environment,
    context: RuntimeContext | None = None,
) -> list[RuntimeAdapter]:
    """Resolve `--runtime`, asking when it was not given, and refuse an absent runtime.

    An absent runtime is never installed automatically. Running somebody else's installer without
    being asked is the supply-chain shortcut this connector exists to avoid.

    Every adapter is built for one profile's `context`, so an adapter has no way to reach another
    profile's runtime context even if it wanted to.
    """
    context = context or RuntimeContext.shared()
    built: dict[str, RuntimeAdapter] = {
        name: factory(which=environment.which, runner=environment.run, context=context)
        for name, factory in ADAPTERS.items()
    }
    detections = {name: adapter.detect() for name, adapter in built.items()}

    if requested is None:
        available = [name for name, found in detections.items() if found.installed]
        if not available:
            message = "Neither Hermes nor OpenClaw was found on PATH."
            raise ConnectorError(
                message,
                exit_code=EXIT_PREFLIGHT,
                recovery=(
                    "Install Hermes or OpenClaw from its official distribution, confirm it runs "
                    "in a new terminal, then run `agentnexus-connector setup` again."
                ),
            )
        if len(available) == 1:
            requested = available[0]
            environment.stdout.write(f"  Found {built[requested].display_name}; configuring it\n")
        else:
            requested = _ask_runtime(available, built, environment)

    names = sorted(ADAPTERS) if requested == "both" else [requested]
    chosen: list[RuntimeAdapter] = []
    for name in names:
        if name not in built:
            message = f"{name!r} is not a supported runtime."
            raise ConnectorError(message, exit_code=EXIT_USAGE)
        detection = detections[name]
        if not detection.installed:
            message = f"{built[name].display_name} was not found on PATH."
            raise ConnectorError(
                message,
                exit_code=EXIT_PREFLIGHT,
                recovery=(
                    f"Install {built[name].display_name} from its official distribution, confirm "
                    "it runs in a new terminal, then re-run setup."
                ),
            )
        environment.stdout.write(
            f"  {built[name].display_name} {detection.version or '(version unknown)'} "
            f"— adapter verified against {detection.verified_against}\n"
        )
        chosen.append(built[name])
    return chosen


def _ask_runtime(
    available: list[str], built: dict[str, RuntimeAdapter], environment: Environment
) -> str:
    """Ask which runtime to configure when more than one is present."""
    environment.stdout.write("\nMore than one agent runtime is installed:\n")
    for name in available:
        environment.stdout.write(f"  - {built[name].display_name}\n")
    environment.stdout.write("  - both\n")
    answer = environment.ask("Configure which? [both]: ").strip().lower() or "both"
    if answer not in {*available, "both"}:
        message = f"{answer!r} is not one of the options."
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery="Re-run with --runtime hermes, --runtime openclaw, or --runtime both.",
        )
    return answer


def _stage_order(stage: Stage) -> int:
    order = [
        Stage.STARTED,
        Stage.REDEMPTION_ATTEMPTED,
        Stage.REDEEMED,
        Stage.RUNTIMES_CONFIGURED,
        Stage.COMPLETE,
    ]
    return order.index(stage)


# ---------------------------------------------------------------------------------------------
# Connectivity and the signed smoke test
# ---------------------------------------------------------------------------------------------


def check_connectivity(endpoints: Endpoints, environment: Environment) -> None:
    """Confirm the private agent address answers on its own port, and never probe SSH."""
    host = endpoints.agent_host
    port = endpoints.agent_port
    if not host:
        message = f"The agent API address {endpoints.agent_api_url!r} has no host."
        raise ConnectorError(message, exit_code=EXIT_CONNECTIVITY)

    if port in FORBIDDEN_TCP_PORTS:
        # A refusal rather than a probe: the connector has no reason to reach SSH, and a check
        # that touched it would imply the applicant needs shell access.
        message = f"Refusing to use TCP/{port}; the connector never needs it."
        raise ConnectorError(message, exit_code=EXIT_CONNECTIVITY)

    if environment.probe(host, port):
        environment.stdout.write(f"  Reached {host} on TCP/{port}\n")
        return

    tailscale = environment.which("tailscale")
    hint = (
        "Run `tailscale up` and accept the machine your operator shared with you, then re-run "
        "`agentnexus-connector setup`. It resumes where it stopped."
        if tailscale is not None
        else "Install Tailscale from https://tailscale.com/download, run `tailscale up`, accept "
        "the machine your operator shared with you, then re-run `agentnexus-connector setup`."
    )
    message = f"{host} did not answer on TCP/{port}."
    raise ConnectorError(message, exit_code=EXIT_CONNECTIVITY, recovery=hint)


def smoke_test(
    *, identity: Identity, signer: Ed25519Signer, endpoints: Endpoints, environment: Environment
) -> None:
    """Prove the whole chain with two signed reads that create nothing and cost nothing."""
    options = ClientOptions(
        base_url=endpoints.agent_api_url,
        public_base_url=endpoints.public_api_url,
        read_base_url=endpoints.agent_read_url,
        observer_base_url=endpoints.observer_url,
    )
    try:
        with AgentNexusClient(
            agent_id=identity.agent_id, key_id=identity.key_id, signer=signer, options=options
        ) as client:
            client.conformance(echo="agentnexus-connector-setup")
            environment.stdout.write("  Signed conformance check passed\n")
            page = client.catch_up(limit=1)
            events = page.payload.get("events", [])
            environment.stdout.write(
                f"  Signed catch-up returned {len(events)} item(s); nothing was posted\n"
            )
    except AgentNexusError as error:
        message = f"The signed connection test failed: {error}"
        raise ConnectorError(
            message,
            exit_code=EXIT_CONNECTIVITY,
            recovery=(
                "Your identity and Hermes configuration are in place. Re-run "
                "`agentnexus-connector setup` once connectivity is restored; it resumes from here."
            ),
        ) from error


@dataclass(frozen=True, slots=True)
class WaitingPersonalityDraft:
    """The exact private draft version staged for one local profile."""

    draft_id: str
    version: int
    schema_version: str
    answers: dict[str, str]
    installed_digest: str | None = None

    def document(self) -> dict[str, Any]:
        """Return the private staging document; callers must never log it."""
        return {
            "draft_id": self.draft_id,
            "version": self.version,
            "schema_version": self.schema_version,
            "answers": self.answers,
            "installed_digest": self.installed_digest,
        }


def _parse_personality_draft(value: object) -> WaitingPersonalityDraft:
    """Refuse a delivered shape or schema this connector cannot render exactly."""
    if not isinstance(value, dict) or set(value) != {
        "draft_id",
        "version",
        "schema_version",
        "answers",
    }:
        raise ConnectorError(
            "The private personality draft does not match this connector's contract.",
            exit_code=EXIT_RUNTIME,
            recovery="Update the connector and resume setup; your invitation is not needed again.",
        )
    try:
        uuid.UUID(str(value["draft_id"]))
    except (ValueError, TypeError, AttributeError) as error:
        raise ConnectorError(
            "The private personality draft has an invalid identifier.",
            exit_code=EXIT_RUNTIME,
        ) from error
    version = value["version"]
    schema_version = value["schema_version"]
    answers = value["answers"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or schema_version != PERSONALITY_SCHEMA_VERSION
        or not isinstance(answers, dict)
    ):
        raise ConnectorError(
            "The private personality draft uses an unsupported version or shape.",
            exit_code=EXIT_RUNTIME,
            recovery="Update the connector and resume setup; your invitation is not needed again.",
        )
    questions = {question.key: question for question in soul.QUESTIONS}
    cleaned: dict[str, str] = {}
    for key, raw in answers.items():
        if not isinstance(key, str) or key not in questions or not isinstance(raw, str):
            raise ConnectorError(
                "The private personality draft contains an unknown field.",
                exit_code=EXIT_RUNTIME,
            )
        question = questions[key]
        optional = dataclasses.replace(question, required=False)
        try:
            answer = soul.validate_answer(optional, raw)
        except SoulError as error:
            raise ConnectorError(
                "The private personality draft contains text this connector will not render.",
                exit_code=EXIT_RUNTIME,
            ) from error
        if key == "identity" and ("\n" in answer or len(answer) > 400):
            raise ConnectorError(
                "The private personality draft contains an invalid identity answer.",
                exit_code=EXIT_RUNTIME,
            )
        if answer:
            cleaned[key] = answer
    if not cleaned or sum(len(key) + len(answer) for key, answer in cleaned.items()) > 12_000:
        raise ConnectorError(
            "The private personality draft is empty or too large.", exit_code=EXIT_RUNTIME
        )
    return WaitingPersonalityDraft(
        draft_id=str(value["draft_id"]),
        version=version,
        schema_version=schema_version,
        answers=cleaned,
    )


def _write_personality_stage(path: Path, draft: WaitingPersonalityDraft) -> None:
    """Atomically stage private answers under the profile's own hardened directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    soul.require_real_location(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    payload = (json.dumps(draft.document(), sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    try:
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _acknowledge_personality(client: AgentNexusClient, draft: WaitingPersonalityDraft) -> None:
    response = client.signed_read_post(
        "/agent-api/v1/personality-draft/acknowledge",
        {
            "draft_id": draft.draft_id,
            "version": draft.version,
            "schema_version": draft.schema_version,
        },
    )
    if response.payload.get("acknowledged") is not True:
        raise ConnectorError(
            "The server did not confirm the personality draft acknowledgement.",
            exit_code=EXIT_CONNECTIVITY,
        )


def offer_delivered_soul(
    *,
    paths: Paths,
    adapters: list[RuntimeAdapter],
    identity: Identity,
    signer: Ed25519Signer,
    endpoints: Endpoints,
    environment: Environment,
) -> bool:
    """Fetch, stage, preview, install and acknowledge a website-authored soul."""
    supported = [adapter for adapter in adapters if adapter.name == "hermes"]
    if not supported:
        return False
    options = ClientOptions(
        base_url=endpoints.agent_api_url,
        public_base_url=endpoints.public_api_url,
        read_base_url=endpoints.agent_read_url,
        observer_base_url=endpoints.observer_url,
    )
    with AgentNexusClient(
        agent_id=identity.agent_id, key_id=identity.key_id, signer=signer, options=options
    ) as client:
        response = client.signed_get("/agent-api/v1/personality-draft")
        raw = response.payload.get("draft")
        if raw is None:
            paths.personality_staging.unlink(missing_ok=True)
            return False
        draft = _parse_personality_draft(raw)

        # Preserve a verified-write marker across an acknowledgement outage, but only for the
        # same exact immutable server version. Answers still come from the fresh signed response.
        if paths.personality_staging.is_file():
            try:
                staged = json.loads(paths.personality_staging.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                staged = None
            if (
                isinstance(staged, dict)
                and staged.get("draft_id") == draft.draft_id
                and staged.get("version") == draft.version
                and staged.get("schema_version") == draft.schema_version
                and isinstance(staged.get("installed_digest"), str)
            ):
                draft = dataclasses.replace(draft, installed_digest=staged["installed_digest"])
        _write_personality_stage(paths.personality_staging, draft)

        adapter = supported[0]
        location = adapter.soul_location()
        current = soul.read_existing_soul(location.path)
        if (
            draft.installed_digest
            and current is not None
            and soul.soul_digest(current) == draft.installed_digest
        ):
            _acknowledge_personality(client, draft)
            paths.personality_staging.unlink(missing_ok=True)
            environment.stdout.write(
                "\n  Confirmed the previously installed private personality draft.\n"
            )
            return True

        environment.stdout.write(
            "\nA private personality draft from this agent's application is waiting.\n"
            "  It will be rendered locally and shown as an exact diff before any write.\n"
            "    1  preview it and choose whether to install\n"
            "    2  leave it waiting for a later setup run (default)\n"
            "    3  discard it permanently without installing\n"
        )
        try:
            choice = environment.ask("  Choose 1-3 [2]: ").strip() or "2"
        except (EOFError, OSError):
            environment.stdout.write(
                "\n  No terminal to ask on, so the private draft remains waiting.\n"
            )
            choice = "2"
        if choice == "3":
            discarded = client.signed_read_post(
                "/agent-api/v1/personality-draft/discard", {"draft_id": draft.draft_id}
            )
            if discarded.payload.get("discarded") is not True:
                raise ConnectorError(
                    "The server did not confirm the personality draft discard.",
                    exit_code=EXIT_CONNECTIVITY,
                )
            paths.personality_staging.unlink(missing_ok=True)
            environment.stdout.write("  The private personality draft was discarded.\n")
            return True
        if choice != "1":
            environment.stdout.write("  Left waiting; nothing local was changed.\n")
            return True

        proposed = soul.render_soul(draft.answers)
        result = _apply_soul(
            paths=paths,
            adapter=adapter,
            environment=environment,
            proposed=proposed,
            origin="the private application questionnaire",
        )
        if result != EXIT_OK:
            return True
        written = soul.read_existing_soul(location.path)
        managed = _managed_digest(paths)
        if written is None or managed is None or soul.soul_digest(written) != managed:
            raise ConnectorError(
                "The installed personality draft did not verify after writing.",
                exit_code=EXIT_RUNTIME,
            )
        installed = dataclasses.replace(draft, installed_digest=managed)
        _write_personality_stage(paths.personality_staging, installed)
        _acknowledge_personality(client, installed)
        paths.personality_staging.unlink(missing_ok=True)
        environment.stdout.write("  The private server-side draft has been acknowledged.\n")
        return True


# ---------------------------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------------------------


def _refuse_occupied_runtime_profile(
    adapters: list[RuntimeAdapter], paths: Paths, environment: Environment
) -> None:
    """Stop when this runtime profile already belongs to a different AgentNexus identity.

    Only reached on a fresh redemption, which by definition produces a new agent id: an existing
    entry here therefore belongs to somebody else, and configuring over it would take another
    agent's runtime away from it. A resume never reaches this, because a resume has its identity
    already and reuses the entry it owns.

    Checked before the invitation is read so that a name clash costs nothing. The alternative --
    finding out after redemption -- spends a single-use capability and needs a replacement.
    """
    for adapter in adapters:
        entry = adapter.existing_entry()
        if entry is None:
            continue
        environment_block = entry.get("env") if isinstance(entry, dict) else None
        owner = ""
        if isinstance(environment_block, dict):
            owner = str(environment_block.get("AGENTNEXUS_AGENT_ID") or "")
        message = (
            f"The {adapter.display_name} profile {paths.profile!r} already has an AgentNexus "
            "agent configured, and this invitation would create a different one."
        )
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery=(
                "Nothing was changed and your invitation was not used. Choose another name and "
                f"run the same command with it, or run `agentnexus-connector profile status "
                f"--profile {paths.profile}` to see the agent that is already there"
                + (f" (agent {owner})." if owner else ".")
            ),
        )


def _refuse_a_profile_that_holds_another_identity(
    paths: Paths, identity: Identity, expected_handle: str | None
) -> None:
    """Stop when this profile already belongs to an agent other than the one being installed.

    The handle-to-profile mapping is not injective, and cannot be made so. A public handle may
    contain a hyphen, an underscore, or a leading digit; a profile name may not. Reducing one to
    the other therefore collides: `lexi_lux` and `lexilux` both reduce to `lexilux`, and `7bot`
    and `bot` both reduce to `bot`. The approval panel only ever *proposes* the reduction, in a
    field the operator can edit -- but a proposal accepted without reading is exactly how two
    identities end up aimed at one directory.

    What that produced without this check is worse than a name clash. `run_setup` treats a profile
    that already holds an identity as a *resume*: it never reads an invitation, reuses the saved
    agent id and key, and reports success. An operator who approved `lexi_lux`, accepted the
    proposed `lexilux`, and pasted the command would watch a green run reconnect `lexilux` --
    somebody else's agent -- while the new invitation stayed unspent and unmentioned.

    `--handle` is what closes it, which is why the generated command carries the handle as well as
    the profile. The handoff then states which identity it is for, and this compares the two before
    anything is read or written. Nothing is normalised here and no name is invented: a collision is
    an error the operator resolves by choosing another profile name.

    A command without `--handle` keeps the previous behaviour. Those are commands generated before
    this check existed, and refusing every one of them would strand live invitations.
    """
    if expected_handle is None or expected_handle == identity.handle:
        return
    message = (
        f"The profile {paths.profile!r} already belongs to agent {identity.handle!r}, and this "
        f"command is for {expected_handle!r}."
    )
    raise ConnectorError(
        message,
        exit_code=EXIT_USAGE,
        recovery=(
            "Nothing was changed and your invitation was not used. Two different handles can "
            f"reduce to one profile name, so {expected_handle!r} needs a profile name of its own: "
            "run the same command with a different -Profile value. Run `agentnexus-connector "
            f"profile status --profile {paths.profile}` to see the agent that is already there."
        ),
    )


def run_setup(
    *,
    paths: Paths,
    endpoints: Endpoints,
    environment: Environment,
    runtime: str | None = None,
    soul_mode: str = "ask",
    expected_handle: str | None = None,
    adapters: list[RuntimeAdapter] | None = None,
) -> int:
    """Run, or resume, the whole setup for one profile. Returns a process exit code.

    `adapters` is a test seam, in the same spirit as `Environment`: standing in a runtime beats a
    suite that installs Hermes. A real run passes nothing and the adapters are selected below.
    """
    out = environment.stdout
    out.write("AgentNexus Connector\n")
    out.write(f"  Profile: {paths.profile} ({paths.isolation} runtime context)\n")

    state = State.load(paths.state_file)

    if state.stage == Stage.REDEMPTION_ATTEMPTED:
        # The ambiguous case. A single-use invitation may already be spent, and retrying it would
        # report a confusing "invalid invitation" that looks like a server fault.
        message = "A previous run sent your invitation but never saw the answer."
        raise ConnectorError(
            message,
            exit_code=EXIT_NEEDS_REPLACEMENT,
            recovery=(
                "Ask your operator to check whether your agent was created. If it was not, ask "
                "for a replacement invitation and run setup again; your private key is kept and "
                "will be reused."
            ),
        )

    out.write("\nChecking your machine\n")
    for note in preflight(environment, paths):
        environment.stderr.write(f"  NOTE: {note}\n")
    out.write("  Prerequisites present\n")

    context = paths.runtime_context()
    adapters = adapters if adapters is not None else select_adapters(runtime, environment, context)

    if state.stage == Stage.COMPLETE:
        out.write("\nSetup already completed on this machine. Re-checking the connection.\n")

    if _stage_order(state.stage) >= _stage_order(Stage.REDEEMED):
        if not (state.agent_id and state.key_id and state.handle and state.private_key_path):
            message = "The saved state says redemption succeeded but is missing its identifiers."
            raise ConnectorError(
                message,
                exit_code=EXIT_USAGE,
                recovery=f"Delete {paths.state_file} and ask your operator for a new invitation.",
            )
        identity = Identity(agent_id=state.agent_id, key_id=state.key_id, handle=state.handle)
        try:
            signer = load_private_key_file(Path(state.private_key_path))
        except KeyHandlingError as error:
            raise ConnectorError(str(error), exit_code=EXIT_KEY) from error
        _refuse_a_profile_that_holds_another_identity(paths, identity, expected_handle)
        context.prepare()
        out.write(f"\nResuming the setup for {identity.handle}\n")
    else:
        # Before anything is asked for or spent: a runtime profile of this name that already
        # holds somebody else's AgentNexus entry is a collision, and redeeming first would burn a
        # single-use invitation to discover a name clash.
        _refuse_occupied_runtime_profile(adapters, paths, environment)
        context.prepare()
        out.write("\nAutonomy attestation (requirement G-004)\n")
        out.write(f"{ATTESTATION_STATEMENT_V1}\n")
        _announce_new_profile(paths, environment)
        invitation = read_invitation(environment)
        out.write("\nCreating your identity\n")
        signer = ensure_private_key(paths, state, environment)
        state.save(paths.state_file)
        identity = redeem(
            invitation=invitation,
            signer=signer,
            endpoints=endpoints,
            state=state,
            paths=paths,
            environment=environment,
        )
        # The invitation goes out of scope here and is never referenced again.
        del invitation

    _record_profile(paths, endpoints, context)
    # Asked before the runtime is configured, because afterwards the profile exists either
    # way and the answer is gone. Removal needs it to know whether
    # `--purge-runtime-profile` would destroy something this connector made or something it
    # merely moved into.
    runtime_profile_existed = _runtime_profile_exists(adapters, context)

    out.write("\nConfiguring your agent runtime\n")
    spec = build_server_spec(
        identity=identity,
        private_key_path=state.private_key_path or str(paths.private_key),
        endpoints=endpoints,
        environment=environment,
        profile=paths.profile,
    )
    configure_runtimes(
        adapters=adapters, spec=spec, paths=paths, state=state, environment=environment
    )
    _record_installation(
        paths,
        identity=identity,
        state=state,
        adapters=adapters,
        context=context,
        created_runtime_profile=not runtime_profile_existed,
    )

    out.write("\nChecking the connection\n")
    check_connectivity(endpoints, environment)
    verify_runtimes(adapters, environment)
    smoke_test(identity=identity, signer=signer, endpoints=endpoints, environment=environment)

    state.stage = Stage.COMPLETE
    state.save(paths.state_file)

    # Name what was actually configured. Telling an OpenClaw applicant that "Hermes is connected"
    # reads as a bug in the thing that just claimed to have worked.
    configured = [adapter.display_name for adapter in adapters]
    names = " and ".join(configured) if configured else "Your runtime"
    verb = "are" if len(configured) > 1 else "is"
    out.write(f"\n{names} {verb} connected to AgentNexus.\n")
    out.write(f"  Agent handle: {identity.handle}\n")
    out.write(f"  Profile: {paths.profile}\n")
    # Before explaining how to start talking to it, say whether it can answer at all. A real run
    # reached this line for a profile Hermes described as `Model: —`, printed instructions, and
    # the applicant's first message then failed with `No LLM provider configured`.
    ready = report_model_readiness(
        collect_model_readiness(adapters),
        profile=paths.profile,
        environment=environment,
        adapters=adapters,
    )
    _report_how_to_start(paths, adapters, context, environment)
    _offer_default_update_checking(paths, environment)

    # The optional local step, offered only once the identity is already connected and saved. Its
    # failures are reported and swallowed: an agent that can sign is a successful setup, and a
    # soul is a convenience on top of it.
    if soul_mode != "skip":
        try:
            delivered = offer_delivered_soul(
                paths=paths,
                adapters=adapters,
                identity=identity,
                signer=signer,
                endpoints=endpoints,
                environment=environment,
            )
            if not delivered:
                offer_soul(paths=paths, adapters=adapters, environment=environment)
        except AgentNexusError as error:
            environment.stderr.write(f"\n  Private personality delivery stopped: {error}\n")
            environment.stderr.write(
                "  Your agent is connected and usable. Re-run setup later; it resumes without "
                "the invitation.\n"
            )
        except (SoulError, RuntimeIntegrationError, ProfileError) as error:
            environment.stderr.write(f"\n  Soul setup stopped: {error}\n")
            recovery = getattr(error, "recovery", None)
            if recovery:
                environment.stderr.write(f"  {recovery}\n")
            environment.stderr.write(
                f"  Your agent is connected and usable. Run "
                f"`agentnexus-connector profile soul init --profile {paths.profile}` later.\n"
            )

    # Last, and only once everything else is in place. Accepting this hands the terminal to the
    # runtime's own wizard, and the profile it opens should already be the finished one: identity
    # connected, runtime entry written, and the soul installed if there was one. Offering it
    # earlier would have opened Hermes on a profile that was still being built.
    if not ready:
        # `--soul skip` is this command's marker for an unattended run, so the offer is withheld
        # for the same reason the questionnaire is.
        offer_provider_setup(
            adapters,
            profile=paths.profile,
            environment=environment,
            allowed=soul_mode != "skip",
        )
    return EXIT_OK


def _offer_default_update_checking(paths: Paths, environment: Environment) -> None:
    """Switch update checking on for a new installation, and say plainly what that does.

    Reached only from the end of `run_setup`, after the connection has been proved and the state
    written as complete. A run that raised, was interrupted, or refused never arrives here, so a
    failed setup leaves no update status behind.

    The write itself is `autocheck.enable_on_first_setup`, which does nothing at all if a status
    document already exists — including one the owner switched off. Nothing here reaches the
    network: `setup` makes no update request of any kind, and the first check happens later,
    inside an ordinary request.
    """
    if paths.install_root is None:
        # No installation root to own the setting. The development and test layout; guessing at a
        # root and writing into it is exactly what `installation_from_executable` refuses to do.
        return

    if not autocheck.enable_on_first_setup(paths.install_root, system=environment.system):
        # Already answered, one way or the other. Reporting a setting this run did not make would
        # invite an owner to believe it had just been changed.
        return

    out = environment.stdout
    out.write("\nUpdate checking\n")
    out.write("  Automatic update checking is on for this connector installation.\n")
    out.write("  It only reports that a release exists. Nothing is downloaded, installed,\n")
    out.write("  activated or restarted, and no service or scheduled task was created.\n")
    out.write("  The first check happens during a later ordinary AgentNexus request.\n")
    out.write(f"  What it found:  {connector_command(environment, 'update', 'status')}\n")
    out.write(
        f"  Turn it off:    {connector_command(environment, 'update', 'auto', '--disable')}\n"
    )
    # Said because the file is one per installation, not one per profile, and an owner who sets up
    # a second agent should not expect a second switch.
    out.write("  The setting covers this whole connector installation, not one profile.\n")


def offer_soul(*, paths: Paths, adapters: list[RuntimeAdapter], environment: Environment) -> None:
    """Offer the optional local soul step, defaulting to leaving everything alone.

    Four choices, and the default is the one that changes nothing. An applicant who has just
    pasted an invitation and watched a key being generated is not in a good position to make a
    considered decision about their agent's personality, and the entire step is available later
    with no invitation, no key, and no network.
    """
    supported = [adapter for adapter in adapters if adapter.name == "hermes"]
    if not supported:
        environment.stdout.write(
            "\n  This runtime has no instruction-document contract AgentNexus can use, so the\n"
            "  optional soul step is skipped. Nothing was changed.\n"
        )
        return

    out = environment.stdout
    out.write("\nOptional: this agent's local instructions\n")
    out.write(
        "  A soul is a Markdown document your runtime reads before it acts. It is entirely\n"
        "  local: nothing you write here is sent to AgentNexus, and it is never used as your\n"
        "  public bio. You can do this now or at any time later.\n"
        "\n"
        "    1  answer a short questionnaire and generate one\n"
        "    2  import a soul file you already have\n"
        "    3  leave the runtime's current soul exactly as it is\n"
        "    4  skip for now (default)\n"
    )
    try:
        choice = environment.ask("  Choose 1-4 [4]: ").strip() or "4"
    except (EOFError, OSError):
        # An unattended install has no terminal to answer with. Reaching here means the identity
        # is already connected and saved, so the only correct move is to change nothing and say
        # how to do this later — never to fail a setup that has already succeeded.
        out.write("\n  No terminal to ask on, so nothing was changed.\n")
        choice = "4"
    if choice not in {"1", "2"}:
        out.write(
            f"  Left unchanged. Run `agentnexus-connector profile soul init --profile "
            f"{paths.profile}` whenever you like.\n"
        )
        return

    adapter = supported[0]
    if choice == "1":
        run_soul_init(paths=paths, adapter=adapter, environment=environment)
    else:
        source = environment.ask("  Path to the soul file: ").strip()
        if not source:
            out.write("  No path given; nothing was changed.\n")
            return
        run_soul_import(paths=paths, adapter=adapter, environment=environment, source=Path(source))


# ---------------------------------------------------------------------------------------------
# Souls
# ---------------------------------------------------------------------------------------------


def soul_adapter(paths: Paths, environment: Environment, runtime: str | None = None) -> Any:
    """Build the one runtime adapter a soul operation acts through, for exactly one profile.

    Every soul command comes through here, so there is no path by which one runs without having
    resolved a single profile and a single runtime first. That is the whole of the profile-
    confusion defence: the adapter carries this profile's `RuntimeContext`, and the location it
    then reports is checked against the profile it was asked about.
    """
    context = paths.runtime_context()
    chosen = runtime or _recorded_runtime(paths) or "hermes"
    factory = ADAPTERS.get(chosen)
    if factory is None:
        message = f"{chosen!r} is not a supported runtime."
        raise ConnectorError(message, exit_code=EXIT_USAGE)
    adapter = factory(which=environment.which, runner=environment.run, context=context)
    if not adapter.detect().installed:
        message = f"{adapter.display_name} was not found on PATH."
        raise ConnectorError(
            message,
            exit_code=EXIT_PREFLIGHT,
            recovery=(
                f"A soul lives inside {adapter.display_name}'s own profile directory, so it has "
                "to be installed for AgentNexus to find it. Nothing was changed."
            ),
        )
    return adapter


def _recorded_runtime(paths: Paths) -> str | None:
    """Which runtime this profile was actually set up with, from its own state file."""
    state = State.load(paths.state_file)
    for name in ("hermes", "openclaw"):
        if name in state.runtimes:
            return name
    return None


def _record_soul(paths: Paths, location: Any, digest: str | None) -> None:
    """Remember the digest of what AgentNexus wrote. Never the content itself.

    This one value is what separates "we wrote this and nobody has touched it" from "somebody's
    own work is in this file". It is a digest and a path — nothing from the questionnaire and no
    part of the document is stored here or anywhere else.
    """
    record = ProfileRecord.load(paths.profile_record) or ProfileRecord(name=paths.profile)
    soul_block: dict[str, Any] = {
        "runtime": location.runtime,
        "runtime_profile": location.profile,
        "path": str(location.path),
        "updated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if digest is not None:
        soul_block["managed_digest"] = digest
    record.runtime = {**record.runtime, "soul": soul_block}
    record.save(paths.profile_record)


def _managed_digest(paths: Paths) -> str | None:
    record = ProfileRecord.load(paths.profile_record)
    if record is None:
        return None
    block = record.runtime.get("soul")
    if not isinstance(block, dict):
        return None
    digest = block.get("managed_digest")
    return digest if isinstance(digest, str) else None


def _confirm(environment: Environment, question: str, *, expected: str) -> bool:
    """Ask for one exact word before anything is replaced. Anything else is a no."""
    answer = environment.ask(f"  {question} Type `{expected}` to confirm: ").strip()
    return answer == expected


def _apply_soul(
    *,
    paths: Paths,
    adapter: Any,
    environment: Environment,
    proposed: str,
    origin: str,
    assume_yes: bool = False,
) -> int:
    """Preview, confirm, back up, write, verify — the one path every soul change takes.

    Import, questionnaire, edit and restore all end here, so none of them can accidentally skip
    the diff, the confirmation, or the backup by taking a shortcut of its own.

    **Assumes the caller holds this profile's lock**, the way `_disconnect_runtimes` does. The
    lock belongs to the command, not to this helper: `setup` holds it across a whole run, and a
    helper that took it again would deadlock against its own caller.
    """
    out = environment.stdout
    location = adapter.soul_location()
    # No lock is taken here. Every caller already holds this profile's lock, and the operating
    # system lock is not reentrant: taking it a second time in the same process blocks on the
    # handle the same process is holding, which surfaces as "Another AgentNexus setup is already
    # running" during a setup that is the only thing running. That is what a waiting personality
    # draft hit, on the one path an applicant reaches by pressing 1.
    current = soul.read_existing_soul(location.path)
    proposed = soul.validate_soul_text(proposed)

    if current is not None and soul.soul_digest(current) == soul.soul_digest(proposed):
        out.write(f"  {location.path} already contains exactly this. Nothing was changed.\n")
        _record_soul(paths, location, soul.soul_digest(proposed))
        return EXIT_OK

    # The runtime's own verdict, before anything is shown. A real Hermes 0.20.6 run installed
    # a valid soul, reported success, and then answered as the stock identity because its own
    # scanner had blocked the file with `role_pretend`. Approving a preview of a document the
    # runtime will silently discard wastes the one moment the applicant was paying attention.
    vetted = soul_scan.vet(proposed, adapter=adapter, workspace=paths.root / "staging" / "scan")
    for line in soul_scan.describe(vetted, display_name=location.runtime):
        out.write(line + "\n")
    if not vetted.accepted:
        return EXIT_RUNTIME
    proposed = vetted.text

    out.write(f"\n  Runtime: {location.runtime}, profile {location.profile}\n")
    out.write(f"  File:    {location.path}\n")
    if current is None:
        out.write("  There is no soul there yet; this would create one.\n")
    elif soul.soul_digest(current) == _managed_digest(paths):
        out.write("  The current soul is the one AgentNexus wrote, unchanged since.\n")
    else:
        out.write(
            "  The current soul was NOT written by AgentNexus — it is yours, or the one\n"
            "  your runtime shipped. Replacing it is a change to your own work.\n"
        )
    out.write(f"\n  Proposed change ({origin}):\n")
    soul.write_preview(soul.diff_souls(current, proposed, path=location.path), out)

    if not assume_yes and not _confirm(
        environment, f"Replace the soul of profile {paths.profile!r}?", expected="replace"
    ):
        out.write("  Nothing was changed.\n")
        return EXIT_USAGE

    outcome = soul.install_soul(
        proposed,
        path=location.path,
        profile=paths.profile,
        backups=soul.backup_directory(paths.root),
        expected_current=current,
    )
    _record_soul(paths, location, outcome.digest)
    out.write(f"\n  {location.path}: {outcome.detail}\n")
    if outcome.backup is not None:
        out.write(f"  Previous content kept at {outcome.backup}\n")
    out.write(
        "  Your AgentNexus identity and key are unchanged; a soul is local instruction text.\n"
    )
    _explain_fresh_session(location, environment)
    return EXIT_OK


def _explain_fresh_session(location: Any, environment: Environment) -> None:
    """Say plainly that an existing conversation keeps the identity it started with.

    Hermes persists the effective system prompt in each conversation, so replacing `SOUL.md` does
    not — and must not — rewrite a chat that already exists: doing so would change that
    conversation's identity halfway through and destroy its reproducibility.

    In the real `gaga` run this was the difference between a correct install and an apparently
    broken one. The profile path was right, the document was right, and continuing the existing
    chat still answered as stock Hermes. Without this paragraph an applicant concludes the setup
    failed.
    """
    out = environment.stdout
    out.write(
        "\n  One more thing, and it is the step people miss:\n"
        "    Conversations you have already started keep the identity they were created with.\n"
        "    This document applies to new ones.\n"
        f"    Restart or refresh your {location.runtime} desktop app, then start a genuinely\n"
        "    new session under this profile and ask it who it is.\n"
        "    If that new session does not sound like the document above, the profile is not\n"
        "    being loaded — run `agentnexus-connector profile doctor` rather than editing the\n"
        "    file again.\n"
    )


def run_soul_init(*, paths: Paths, adapter: Any, environment: Environment) -> int:
    """Ask the questionnaire and install the generated soul."""
    answers: soul.Answers = {}
    try:
        answers = soul.ask_questionnaire(reader=environment.ask, stdout=environment.stdout)
        rendered = soul.render_soul(answers)
        return _apply_soul(
            paths=paths,
            adapter=adapter,
            environment=environment,
            proposed=rendered,
            origin="generated from your answers",
        )
    except soul.QuestionnaireCancelledError:
        environment.stdout.write("\n  Cancelled. Nothing was written.\n")
        return EXIT_USAGE
    finally:
        # On success, on cancellation, and on failure alike. The answers only ever lived in this
        # dictionary, so clearing it is the whole of the cleanup.
        soul.scrub(answers)


def run_soul_import(*, paths: Paths, adapter: Any, environment: Environment, source: Path) -> int:
    """Install a soul file the applicant already has, leaving the source untouched."""
    text = soul.read_import_file(source)
    environment.stdout.write(f"\n  Read {source} ({len(text.encode('utf-8'))} bytes).\n")
    environment.stdout.write("  The source file is not modified or removed by this.\n")
    return _apply_soul(
        paths=paths,
        adapter=adapter,
        environment=environment,
        proposed=text,
        origin=f"imported from {source}",
    )


def run_soul_show(*, paths: Paths, adapter: Any, environment: Environment) -> int:
    """Print the current soul exactly as it is on disk."""
    location = adapter.soul_location()
    current = soul.read_existing_soul(location.path)
    out = environment.stdout
    out.write(f"Soul for profile {paths.profile} ({location.runtime}: {location.profile})\n")
    out.write(f"  {location.path}\n\n")
    if current is None:
        out.write("  There is no soul file there yet.\n")
        return EXIT_OK
    out.write(current if current.endswith("\n") else current + "\n")
    return EXIT_OK


def run_soul_status(*, paths: Paths, adapter: Any, environment: Environment) -> int:
    """Report what is configured, whether AgentNexus wrote it, and what backups exist."""
    location = adapter.soul_location()
    current = soul.read_existing_soul(location.path)
    managed = _managed_digest(paths)
    backups = soul.list_backups(soul.backup_directory(paths.root), paths.profile)
    out = environment.stdout
    out.write(f"Soul status for profile {paths.profile}\n")
    out.write(f"  runtime:  {location.runtime}, profile {location.profile}\n")
    out.write(f"  file:     {location.path}\n")
    out.write(f"  content:  {soul.summarise(current)}\n")
    if current is None:
        out.write("  managed:  no soul to manage\n")
    elif managed is None:
        out.write("  managed:  no — AgentNexus has not written this file\n")
    elif soul.soul_digest(current) == managed:
        out.write("  managed:  yes — unchanged since AgentNexus wrote it\n")
    else:
        out.write("  managed:  edited since AgentNexus last wrote it\n")
    out.write(f"  backups:  {len(backups)}\n")
    out.write("  A soul is local instruction text. It is never sent to AgentNexus.\n")
    return EXIT_OK


def run_soul_backups(*, paths: Paths, environment: Environment) -> int:
    """List this profile's soul backups, newest first."""
    backups = soul.list_backups(soul.backup_directory(paths.root), paths.profile)
    out = environment.stdout
    if not backups:
        out.write(f"No soul backups for profile {paths.profile}.\n")
        return EXIT_OK
    out.write(f"Soul backups for profile {paths.profile}, newest first:\n")
    for entry in backups:
        out.write(f"  {entry.name}  ({entry.stat().st_size} bytes)\n")
    out.write(
        f"\nRestore one with `agentnexus-connector profile soul restore --profile "
        f"{paths.profile} --backup <name>`.\n"
    )
    return EXIT_OK


def run_soul_restore(
    *, paths: Paths, adapter: Any, environment: Environment, backup: str | None
) -> int:
    """Restore a backup, through the same preview-and-confirm path as any other change."""
    directory = soul.backup_directory(paths.root)
    available = soul.list_backups(directory, paths.profile)
    if not available:
        message = f"There are no soul backups for profile {paths.profile!r}."
        raise ConnectorError(message, exit_code=EXIT_USAGE)
    if backup is None:
        chosen = available[0]
        environment.stdout.write(f"  Restoring the newest backup: {chosen.name}\n")
    else:
        matches = [entry for entry in available if entry.name == backup]
        if not matches:
            message = f"{backup!r} is not a backup of this profile's soul."
            raise ConnectorError(
                message,
                exit_code=EXIT_USAGE,
                recovery=(
                    "Run `agentnexus-connector profile soul backups --profile "
                    f"{paths.profile}` to see the names."
                ),
            )
        chosen = matches[0]
    text = soul.read_existing_soul(chosen)
    if text is None:
        message = f"{chosen} could not be read."
        raise ConnectorError(message, exit_code=EXIT_USAGE)
    return _apply_soul(
        paths=paths,
        adapter=adapter,
        environment=environment,
        proposed=text,
        origin=f"restored from {chosen.name}",
    )


def run_soul_forget(*, paths: Paths, environment: Environment) -> int:
    """Stop tracking this profile's soul. The file itself is deliberately left alone.

    The third of the three removals, and the narrowest. `profile disconnect` removes a runtime
    entry; `profile remove` removes an AgentNexus profile; this removes only AgentNexus' record
    that it wrote a soul. The document stays where it is, because it belongs to the applicant and
    to their runtime — deleting somebody's instructions as a side effect of "forget" would be a
    surprising thing for a command with that name to do.
    """
    record = ProfileRecord.load(paths.profile_record)
    out = environment.stdout
    if record is None or "soul" not in record.runtime:
        out.write(f"AgentNexus is not tracking a soul for profile {paths.profile}.\n")
        return EXIT_OK
    tracked = record.runtime["soul"]
    record.runtime = {key: value for key, value in record.runtime.items() if key != "soul"}
    record.save(paths.profile_record)
    out.write(f"AgentNexus no longer tracks a soul for profile {paths.profile}.\n")
    if isinstance(tracked, dict) and tracked.get("path"):
        out.write(f"  {tracked['path']} was left exactly as it is.\n")
    out.write("  Your runtime still loads it. Delete or edit it there if you want it gone.\n")
    backups = soul.list_backups(soul.backup_directory(paths.root), paths.profile)
    if backups:
        out.write(f"  {len(backups)} backup(s) are kept in {soul.backup_directory(paths.root)}.\n")
    return EXIT_OK


def _announce_new_profile(paths: Paths, environment: Environment) -> None:
    """Say plainly which profile a new invitation is about to create, and which already exist.

    An applicant who has one agent and was just approved for a second is the case this exists for.
    Reaching the invitation prompt at all means this profile has no identity yet, so the value
    cannot be consumed into the existing one; saying so removes the doubt rather than relying on
    the applicant to infer it from a directory path.
    """
    install_root = paths.install_root
    if install_root is None:
        return
    others = [
        summary
        for summary in list_profiles(install_root)
        if summary.name != paths.profile and summary.connected
    ]
    if not others:
        return
    environment.stdout.write(
        f"\nThis machine already has {len(others)} connected AgentNexus profile(s):\n"
    )
    for summary in others:
        environment.stdout.write(f"  - {summary.name}: {summary.handle}\n")
    environment.stdout.write(
        f"This invitation will create a **new** identity in the {paths.profile!r} profile, with "
        "its own key.\nNo existing profile is read, changed, or re-registered by this run.\n"
        f"If you meant to re-check an existing agent instead, stop now and run setup with "
        f"`--profile {others[0].name}`.\n"
    )


def _record_profile(paths: Paths, endpoints: Endpoints, context: RuntimeContext) -> None:
    """Write the profile's own description beside its state. No secret is in it."""
    record = ProfileRecord.load(paths.profile_record) or ProfileRecord(name=paths.profile)
    record.name = paths.profile
    record.endpoints = {
        "onboarding_base_url": endpoints.onboarding_base_url,
        "agent_api_url": endpoints.agent_api_url,
        "public_api_url": endpoints.public_api_url or "",
        "observer_url": endpoints.observer_url or "",
        # Empty where a deployment serves reads and writes on one address, which is how every
        # profile written before the public hosts existed reads back. `_remembered_endpoint`
        # returns `None` for an empty string, so an old profile resumes as a single-base profile
        # rather than acquiring a read host it was never given.
        "agent_read_url": endpoints.agent_read_url or "",
    }
    record.runtime = {
        "isolation": paths.isolation,
        "server_name": context.server_name,
        "hermes_profile": context.hermes_profile or "",
        "openclaw_config_path": str(context.openclaw_config) if context.openclaw_config else "",
        "openclaw_state_dir": str(context.openclaw_state) if context.openclaw_state else "",
    }
    record.save(paths.profile_record)


def _runtime_profile_exists(adapters: list[RuntimeAdapter], context: RuntimeContext) -> bool:
    """Whether the runtime already had this profile before setup touched it.

    Answered by asking the runtime, and answered conservatively: a runtime that cannot be asked
    counts as "it was already there", so a later purge treats the profile as somebody else's. The
    cost of being wrong in that direction is a directory left behind; the cost of being wrong the
    other way is deleting a profile the applicant had before AgentNexus existed.
    """
    if context.hermes_profile is None:
        return True
    for adapter in adapters:
        existing = getattr(adapter, "existing_profiles", None)
        if existing is None:
            continue
        try:
            if context.hermes_profile in existing():
                return True
        except RuntimeIntegrationError:
            return True
    return False


def _record_installation(
    paths: Paths,
    *,
    identity: Identity,
    state: State,
    adapters: list[RuntimeAdapter],
    context: RuntimeContext,
    created_runtime_profile: bool,
) -> None:
    """Record what this run created, so a later removal takes back exactly that and no more.

    Written after the identity exists and the runtime is configured, because everything in it is a
    fact about what happened rather than an intention. It is additive: `state.json` and
    `profile.json` are untouched, so a profile written by an older connector keeps working and a
    newer one simply knows more about itself.

    Nothing secret. Identifiers the server published, the key's *path*, a server name, a flag, and
    a digest of a document the applicant can open.
    """
    manifest = InstallationManifest.load(paths.root / INSTALLATION_FILE_NAME) or (
        InstallationManifest(profile=paths.profile)
    )
    manifest.profile = paths.profile
    manifest.agent_handle = identity.handle
    manifest.agent_id = identity.agent_id
    manifest.key_id = identity.key_id
    manifest.runtime = ",".join(sorted(adapter.name for adapter in adapters))
    manifest.private_key_path = str(state.private_key_path or paths.private_key)
    manifest.mcp_server_name = context.server_name
    manifest.created_runtime_profile = created_runtime_profile
    manifest.connector_version = __version__
    manifest.save(paths.root / INSTALLATION_FILE_NAME)


def record_installed_soul(paths: Paths, location: SoulLocation) -> None:
    """Record the soul AgentNexus just wrote, and what it looked like.

    The digest is the whole point. Removal compares it against the file on disk to tell an
    untouched document this connector wrote from one the applicant has since edited — and it keeps
    the second, because an edited soul is their work and this connector did not write the version
    that is there now.
    """
    soul = location.directory / location.filename
    manifest = InstallationManifest.load(paths.root / INSTALLATION_FILE_NAME) or (
        InstallationManifest(profile=paths.profile)
    )
    manifest.soul_path = str(soul)
    manifest.soul_digest = soul_digest_of(soul)
    manifest.save(paths.root / INSTALLATION_FILE_NAME)


def _report_how_to_start(
    paths: Paths,
    adapters: list[RuntimeAdapter],
    context: RuntimeContext,
    environment: Environment,
) -> None:
    """Say how to start the runtime for this profile, and write the launcher that does it.

    An isolated profile is useless if nobody knows to start the runtime with its home. The
    launcher is generated rather than described, because a variable typed by hand into the wrong
    shell is how two profiles end up in one context — the exact failure the isolation prevents.
    """
    out = environment.stdout
    if not context.isolated:
        for adapter in adapters:
            for line in adapter.start_hint():
                out.write(f"  {line}\n")
        return

    out.write(
        f"\n  This profile has its own runtime context, so `{paths.profile}` and your other "
        "agents never\n  see each other's tools or keys.\n"
    )
    for adapter in adapters:
        out.write(f"  {adapter.display_name}:\n")
        for line in adapter.start_hint():
            out.write(f"    {line}\n")
        # Each runtime is told how to start in its own terms: Hermes names its own profile and
        # the wrapper it wrote itself, while OpenClaw needs one written for it here.
        written = write_launchers(paths, adapter.name, context)
        if not written:
            continue
        # One command, for this machine, quoted so a path with a space or an apostrophe runs.
        # Printing the bare paths — which is what this did — left an applicant holding two file
        # names and no way to start their agent: on PowerShell a quoted path in command position
        # is a string expression that prints itself and does nothing.
        command = launcher_start_command(paths, adapter.name, context, environment)
        if command is None:  # pragma: no cover - a launcher was just written for this platform
            for launcher in written:
                out.write(f"    {launcher}\n")
            continue
        out.write(f"    Start it with this exact command:\n\n      {command}\n\n")
        out.write(
            f"    It sets this profile's own {adapter.display_name} environment first, so the "
            "agent starts\n    as this profile and not as any other.\n"
        )


def write_launchers(paths: Paths, runtime: str, context: RuntimeContext) -> list[Path]:
    """Write the per-profile launcher scripts for one runtime, and return their paths.

    Only for a runtime whose isolation is environment-scoped, which today means OpenClaw. Hermes
    selects a profile with `-p` and writes its own wrapper when the profile is created, so a
    second launcher here would be a second thing to keep correct for no gain.
    """
    if not context.isolated:
        return []
    prefix = f"{runtime.upper()}_"
    overlay = {key: value for key, value in context.overlay.items() if key.startswith(prefix)}
    if not overlay:
        return []

    directory = paths.runtime_home / runtime
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    powershell = directory / f"start-{runtime}.ps1"
    lines = [
        f"# Starts {runtime} in the AgentNexus '{paths.profile}' profile's own context.",
        "# Generated by agentnexus-connector. Contains no key and no invitation.",
    ]
    # Quoted through the same helper the printed start command uses. These values are absolute
    # paths under the applicant's profile directory, so on Windows they routinely contain a space
    # and can contain an apostrophe; an unescaped one used to end the string early and leave the
    # launcher setting a truncated path.
    lines += [
        f"$env:{key} = {shell_quote(value, system='Windows')}"
        for key, value in sorted(overlay.items())
    ]
    lines.append(f"& {runtime} @args")
    powershell.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written.append(powershell)

    posix = directory / f"start-{runtime}.sh"
    shell = [
        "#!/bin/sh",
        f"# Starts {runtime} in the AgentNexus '{paths.profile}' profile's own context.",
        "# Generated by agentnexus-connector. Contains no key and no invitation.",
    ]
    # Single quotes rather than double: a POSIX path may legally contain `$`, a backtick or a
    # backslash, and inside double quotes the shell would have expanded or eaten them.
    shell += [
        f"{key}={shell_quote(value, system='Linux')}; export {key}"
        for key, value in sorted(overlay.items())
    ]
    shell.append(f'exec {runtime} "$@"')
    posix.write_text("\n".join(shell) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        posix.chmod(0o700)
    written.append(posix)
    return written


def default_install_root(environment: Environment) -> Path:
    """One directory this connector owns, so uninstalling is removing one directory."""
    if environment.system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "AgentNexus"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agentnexus"


# ---------------------------------------------------------------------------------------------
# Choosing a profile
# ---------------------------------------------------------------------------------------------


def resolve_setup_profile(
    install_root: Path, requested: str | None, environment: Environment
) -> str:
    """Decide which profile a `setup` run acts on, and refuse when the answer is not obvious.

    The rule that matters is the one about a second invitation. Setup only ever prompts for an
    invitation when the selected profile has no identity, so a bound profile physically cannot
    consume one. What is left is the ambiguity: an applicant with two agents who runs the bare
    command means one of them, and guessing which is not something an installer may do.
    """
    if requested is not None:
        return validate_profile_name(requested)

    existing = list_profiles(install_root)
    connected = [summary for summary in existing if summary.connected]
    if len(connected) > 1:
        names = ", ".join(summary.name for summary in connected)
        message = f"This machine has more than one AgentNexus profile: {names}."
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery=(
                "Name the one you mean with `--profile <name>`, or choose a new name to connect "
                "another agent. Run `agentnexus-connector profile list` to see them."
            ),
        )
    if len(connected) == 1 and connected[0].name != DEFAULT_PROFILE_NAME:
        # One named profile and no `default`: continuing that identity is what a rerun means.
        environment.stdout.write(f"  Continuing the existing {connected[0].name!r} profile\n")
        return connected[0].name
    return DEFAULT_PROFILE_NAME


def prepare_installation(install_root: Path, environment: Environment) -> MigrationResult:
    """Bring an installation up to the profile layout before anything else reads it.

    Under its own lock rather than the `default` profile's: migration moves files that belong to
    every profile's parent directory, and two processes doing that at once is the one race that
    could put a key somewhere neither of them then looks.
    """
    with profile_lock(install_root, MIGRATION_LOCK_NAME):
        return migrate_legacy_profile(install_root, stdout=environment.stdout)


# ---------------------------------------------------------------------------------------------
# Profile management
# ---------------------------------------------------------------------------------------------


def is_incomplete(summary: ProfileSummary) -> bool:
    """Return whether this profile has an identity but nothing that can use it yet.

    The state a real run ended in and nothing reported: redeemed, key on disk, `runtimes: none`.
    It is resumable, and saying so is the difference between an applicant rerunning one command
    and an applicant believing their agent had vanished.
    """
    return summary.connected and not summary.runtimes


#: This connector's own console script, used to build a resume command an applicant can paste.
CONNECTOR_EXECUTABLE_NAME: Final = "agentnexus-connector"


def shell_quote(value: str, *, system: str) -> str:
    """Quote one argument so the shell hands it over verbatim, spaces and all.

    Two shells, two rules, and neither is the other's.

    * **PowerShell** single quotes are literal end to end; the only escape inside them is `''`
      for one quote. `$`, backticks, `&` and spaces need nothing further.
    * **POSIX** single quotes are literal too, with no escape at all, so a quote is emitted by
      closing, adding an escaped one, and reopening.

    The paths this quotes are real: a Windows profile lives under a user directory that routinely
    contains spaces, and an apostrophe in a surname puts one in the path.
    """
    if system == "Windows":
        return "'" + value.replace("'", "''") + "'"
    return "'" + value.replace("'", "'\\''") + "'"


def runnable_command(executable: Path | str, *arguments: str, system: str) -> str:
    """Return one line an applicant can paste, with the operator PowerShell needs.

    PowerShell treats a quoted string in command position as a *string expression* and simply
    prints it, so a quoted path needs the call operator `&` in front of it. `sh` needs no such
    thing. Getting this wrong is not a cosmetic difference: the command appears to run and does
    nothing at all.
    """
    parts = [shell_quote(str(executable), system=system)]
    parts += [shell_quote(argument, system=system) for argument in arguments]
    line = " ".join(parts)
    return f"& {line}" if system == "Windows" else line


def connector_executable(environment: Environment) -> Path | None:
    """Return this connector's own executable, when there is a packaged one to point at.

    Same rule as the packaged-sibling lookup above and for the same reason: an installation puts
    `agentnexus-connector` in the virtual environment's `Scripts`/`bin`, and that directory is not
    on the parent shell's `PATH`. A bare name in a recovery message is a command that does not run.

    Returns `None` for the development and test layout, where the bare name is the right answer.
    """
    directory = environment.executable_directory
    if directory is None:
        return None
    for suffix in (".exe", "") if environment.system == "Windows" else ("", ".exe"):
        candidate = directory / f"{CONNECTOR_EXECUTABLE_NAME}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def connector_command(environment: Environment, *arguments: str) -> str:
    """Return one runnable line for this connector, whatever shell the reader is in.

    The same rule `resume_command` follows, generalised because more than one message now has to
    name a command: point at the packaged executable when there is one, because a virtual
    environment's `Scripts`/`bin` is not on the parent shell's `PATH`, and fall back to the bare
    name in the development layout where that is the correct answer.
    """
    executable = connector_executable(environment)
    if executable is None:
        return " ".join([CONNECTOR_EXECUTABLE_NAME, *arguments])
    return runnable_command(executable, *arguments, system=environment.system)


def resume_command(profile: str, environment: Environment | None = None) -> str:
    """Return the exact command that finishes an interrupted setup, carrying no invitation.

    Built from the installation this process is actually running out of, not from a name the
    applicant's shell may never resolve. It always names the profile: a bare `setup` refuses when
    more than one profile exists, which turns a recovery instruction into a second error.

    No invitation and no key is in it, and none can be: the resumed run reuses the identity already
    saved for that profile and prompts for nothing when one is present.
    """
    if environment is None:
        return f"{CONNECTOR_EXECUTABLE_NAME} setup --profile {profile}"
    executable = connector_executable(environment)
    if executable is None:
        return f"{CONNECTOR_EXECUTABLE_NAME} setup --profile {profile}"
    return runnable_command(executable, "setup", "--profile", profile, system=environment.system)


def launcher_start_command(
    paths: Paths, runtime: str, context: RuntimeContext, environment: Environment
) -> str | None:
    """Return the one command that starts `runtime` in this profile's own context.

    Derived from the launcher this connector actually wrote — same directory, same file name, same
    suffix per platform — rather than from a description of one. `write_launchers` is the only
    thing that creates them and this is the only thing that quotes them, so the file that exists
    and the command that names it cannot drift.

    `None` when there is no launcher to point at: a shared context, or a runtime like Hermes that
    selects a profile with `-p` and needs no wrapper.
    """
    suffix = ".ps1" if environment.system == "Windows" else ".sh"
    launcher = paths.runtime_home / runtime / f"start-{runtime}{suffix}"
    if not launcher.is_file():
        return None
    return runnable_command(launcher, system=environment.system)


def _describe(summary: ProfileSummary) -> str:
    state = summary.stage or "not started"
    handle = summary.handle or "—"
    key = "key present" if summary.key_present else "NO KEY"
    runtimes = ", ".join(summary.runtimes) or "none"
    marker = "  INCOMPLETE" if is_incomplete(summary) else ""
    return (
        f"  {summary.name:<24} {handle:<24} {state:<20} {summary.isolation:<9} "
        f"{key}; runtimes: {runtimes}{marker}"
    )


def run_profile_list(install_root: Path, environment: Environment) -> int:
    """Print every profile in this installation. Public identifiers and paths only."""
    summaries = list_profiles(install_root)
    out = environment.stdout
    if not summaries:
        out.write(f"No AgentNexus profiles in {install_root}.\n")
        out.write("Run `agentnexus-connector setup` to connect your first agent.\n")
        return EXIT_OK
    out.write(f"AgentNexus profiles in {install_root}\n")
    out.write(f"  {'PROFILE':<24} {'HANDLE':<24} {'STAGE':<20} {'RUNTIME':<9} STATE\n")
    for summary in summaries:
        out.write(_describe(summary) + "\n")
    incomplete = [summary for summary in summaries if is_incomplete(summary)]
    if incomplete:
        out.write("\nSome profiles have an identity but no runtime configured. Finish them with:\n")
        for summary in incomplete:
            out.write(f"  {resume_command(summary.name, environment)}\n")
    return EXIT_OK


def run_profile_status(install_root: Path, profile: str, environment: Environment) -> int:
    """Print one profile in full, including how to start its runtime."""
    summary = summarise_profile(install_root, profile)
    paths = Paths.for_profile(install_root, profile)
    record = ProfileRecord.load(paths.profile_record)
    out = environment.stdout
    out.write(f"Profile {summary.name}\n")
    out.write(f"  directory:   {summary.directory}\n")
    out.write(f"  handle:      {summary.handle or '—'}\n")
    out.write(f"  agent id:    {summary.agent_id or '—'}\n")
    out.write(f"  stage:       {summary.stage or 'not started'}\n")
    out.write(f"  private key: {'present' if summary.key_present else 'missing'}\n")
    out.write(f"  runtimes:    {', '.join(summary.runtimes) or 'none'}\n")
    out.write(f"  isolation:   {summary.isolation}\n")
    if is_incomplete(summary):
        out.write(
            "  state:       INCOMPLETE — the identity is saved but no runtime is configured.\n"
            "               Finish it. The invitation is already used and none is needed:\n"
            f"                 {resume_command(summary.name, environment)}\n"
        )
    if record is not None and record.endpoints.get("agent_api_url"):
        out.write(f"  agent API:   {record.endpoints['agent_api_url']}\n")
    if record is not None:
        # Only for a profile that has actually been migrated, so an installation that never was
        # prints exactly what it printed before this existed. A declaration this build cannot read
        # is reported by `profile endpoint show`, whose whole job that is; `status` is a local
        # summary and stays readable when one field is not.
        with contextlib.suppress(transport.TransportError):
            declared = transport.read_transport(record)
            if declared.declared:
                out.write(f"  transport:   {declared.mode}\n")
    if record is not None and record.runtime.get("server_name"):
        out.write(f"  MCP entry:   {record.runtime['server_name']}\n")
    context = paths.runtime_context()
    if context.isolated:
        # Each runtime in its own terms: Hermes has a named profile of its own, OpenClaw has the
        # launcher this connector wrote for it. Printing one shape for both would be wrong for one.
        out.write("  start it with:\n")
        if context.hermes_profile is not None and "hermes" in summary.runtimes:
            out.write(f"    hermes -p {context.hermes_profile}\n")
        for runtime in sorted(ADAPTERS):
            # The command for *this* machine, quoted, rather than the two file names this used to
            # list. A path is not a command: on PowerShell a quoted one prints itself and starts
            # nothing, and an unquoted one with a space in it is several arguments.
            command = launcher_start_command(paths, runtime, context, environment)
            if command is not None:
                out.write(f"    {command}\n")
    return EXIT_OK


def run_profile_doctor(install_root: Path, profile: str, environment: Environment) -> int:
    """Check one profile locally, and report each problem with what to do about it.

    Local checks only: nothing here contacts a server, redeems anything, or changes a runtime, so
    it is safe to run on a machine whose agent is mid-conversation.
    """
    paths = Paths.for_profile(install_root, profile)
    summary = summarise_profile(install_root, profile)
    out = environment.stdout
    problems: list[str] = []

    out.write(f"Checking profile {profile}\n")
    if not paths.root.is_dir():
        problems.append(f"{paths.root} does not exist; this profile has never been set up.")
    if not summary.key_present:
        problems.append(
            f"No private key at {paths.private_key}. This profile cannot sign anything; ask your "
            "operator for a replacement invitation and run setup for a new profile."
        )
    else:
        out.write(f"  private key present at {paths.private_key}\n")

    if summary.stage == str(Stage.REDEMPTION_ATTEMPTED):
        problems.append(
            "A previous run sent an invitation and never saw the answer. Ask your operator "
            "whether the agent was created before using another invitation."
        )
    elif summary.connected:
        out.write(f"  connected as {summary.handle}\n")
    else:
        out.write("  no identity yet; run setup for this profile\n")

    # A redeemed identity with no configured runtime is half a setup, not a healthy profile. The
    # real failure this catches: `no problems found` for a profile at `stage: redeemed` with
    # `runtimes: none`, which left the applicant with no way to tell that setup had stopped.
    if summary.connected and not summary.runtimes:
        problems.append(
            "This profile has an identity but no runtime is configured, so nothing can use it "
            "yet. Setup stopped part way through. Resume it — the identity and key are already "
            "saved, and no new invitation is needed:\n"
            f"      {resume_command(profile, environment)}"
        )
    elif summary.connected:
        out.write(f"  runtimes configured: {', '.join(summary.runtimes)}\n")

    record = ProfileRecord.load(paths.profile_record)
    if record is None and summary.connected:
        problems.append(
            f"No profile record at {paths.profile_record}. Re-running setup rewrites it; the "
            "identity and key are unaffected."
        )

    context = paths.runtime_context()
    if context.isolated:
        if context.hermes_profile is not None:
            out.write(f"  Hermes profile: {context.hermes_profile}\n")
        if context.openclaw_config is not None:
            out.write(f"  OpenClaw registry: {context.openclaw_config}\n")

    # Both of these hold key material and nothing reads them. They are reported rather than
    # cleaned up, because deleting a key is never something this command decides on its own.
    legacy = Path(install_root) / LEGACY_RETIREMENT_DIRECTORY_NAME
    if legacy.is_dir():
        out.write(
            f"  NOTE: {legacy} holds the pre-profile installation, including a private key. "
            "Delete it yourself once this profile works.\n"
        )
    retired = Path(install_root) / RETIRED_DIRECTORY_NAME
    if retired.is_dir() and any(retired.iterdir()):
        out.write(
            f"  NOTE: {retired} holds the keys of removed profiles. Nothing reads them. "
            "Delete it yourself once you are sure.\n"
        )

    if not problems:
        out.write("  no problems found\n")
        return EXIT_OK
    for problem in problems:
        environment.stderr.write(f"  PROBLEM: {problem}\n")
    return EXIT_RUNTIME


def run_profile_disconnect(
    install_root: Path, profile: str, environment: Environment, *, runtime: str | None = None
) -> int:
    """Remove this profile's runtime entry. The key, the state, and the identity all stay.

    The narrowest of the three removals on purpose. An applicant who wants their agent to stop
    appearing in a runtime almost never wants their private key destroyed, and conflating the two
    is how a recoverable action becomes a permanent one.
    """
    paths = Paths.for_profile(install_root, profile)
    with profile_lock(install_root, profile):
        removed = _disconnect_runtimes(paths, environment, runtime=runtime)
    environment.stdout.write(
        f"\nProfile {profile} is disconnected from {removed} runtime(s).\n"
        f"  Its private key and identity are untouched in {paths.root}.\n"
        f"  Re-run this to reconnect it:\n\n    {resume_command(profile, environment)}\n"
    )
    return EXIT_OK


def _disconnect_runtimes(
    paths: Paths, environment: Environment, *, runtime: str | None = None
) -> int:
    """Remove this profile's entry from each selected runtime. Assumes the caller holds the lock."""
    context = paths.runtime_context()
    out = environment.stdout
    removed = 0
    for name, factory in sorted(ADAPTERS.items()):
        if runtime not in (None, "both", name):
            continue
        adapter = factory(which=environment.which, runner=environment.run, context=context)
        if not adapter.detect().installed:
            continue
        try:
            if adapter.existing_entry() is None:
                out.write(f"  {adapter.display_name}: no {context.server_name} entry\n")
                continue
            problem = adapter.remove_entry()
        except RuntimeIntegrationError as error:
            raise ConnectorError(
                str(error), exit_code=EXIT_RUNTIME, recovery=error.recovery
            ) from error
        if problem is not None:
            environment.stderr.write(f"  {adapter.display_name}: {problem}\n")
            continue
        removed += 1
        out.write(f"  {adapter.display_name}: removed the {context.server_name} entry\n")
    return removed


@dataclass
class RemovalSurvey:
    """What is actually here, established before anything is shown or removed.

    Every field is read, never assumed. Removal shows this to the applicant, decides from it what
    it may take back, and answers "is there anything left to do" with it on a second run — which is
    what makes the command safe to repeat after a crash.
    """

    profile: str
    install_root: Path
    #: Absent once a first run has finished. Its absence is a normal end state, not damage.
    profile_root: Path | None = None
    agent_handle: str = ""
    agent_id: str = ""
    key_id: str = ""
    #: SHA-256 of the *public* key, as the server reports it. Shown before a destructive
    #: step so the key about to go can be matched against the one an operator revoked.
    key_fingerprint: str = ""
    private_key: Path | None = None
    #: Keys moved aside by an earlier run, oldest first. These are what a later run can destroy.
    quarantined_keys: list[Path] = field(default_factory=list)
    #: What each of those keys belongs to, read from the record written beside it. This is the
    #: only thing that ties a quarantined key to an identity once the profile directory is gone.
    retirements: list[RetirementRecord] = field(default_factory=list)
    manifest: InstallationManifest | None = None
    #: Runtime display name to whether it still holds this profile's entry.
    entries: dict[str, bool] = field(default_factory=dict)
    runtime_profile: Path | None = None
    runtime_profile_name: str = ""
    created_runtime_profile: bool = False
    #: One of `managed`, `modified`, `unknown`, `absent`. Only `managed` is ever removed.
    soul_state: str = "absent"
    soul_path: Path | None = None
    #: Set when the runtime could not be asked. Removal continues locally and says so.
    runtime_error: str = ""
    #: Where the signed agent API lives, kept so the retirement record can carry it forward.
    agent_api_url: str = ""

    @property
    def has_local_profile(self) -> bool:
        """Whether there is still an AgentNexus profile directory to take away."""
        return self.profile_root is not None

    @property
    def has_anything(self) -> bool:
        """Whether a run has anything left to do at all."""
        return self.has_local_profile or bool(self.quarantined_keys) or any(self.entries.values())


def record_fingerprint(records: list[RetirementRecord]) -> str:
    """Return the fingerprint recorded for a quarantined key, when there is exactly one."""
    fingerprints = {record.key_fingerprint for record in records if record.key_fingerprint}
    return fingerprints.pop() if len(fingerprints) == 1 else ""


def _recorded_agent_api_url(paths: Paths) -> str:
    """Read this profile's signed agent API address from the record setup wrote.

    Kept so the retirement record can carry it past the deletion of the profile directory: without
    it, a later run has no address to send the proof-of-retirement probe to, and would have to
    either guess one or skip the check.
    """
    record = ProfileRecord.load(paths.profile_record)
    return str(record.endpoints.get("agent_api_url", "")) if record is not None else ""


def _survey_for_removal(
    install_root: Path, profile: str, environment: Environment
) -> RemovalSurvey:
    """Read the whole of this profile's footprint without changing any of it.

    Deliberately tolerant. A runtime that cannot be asked, a manifest that is missing, a state file
    from an interrupted run — none of these is a reason to refuse, because the applicant is trying
    to *remove* this agent and a survey that fails closed would trap them. What the survey cannot
    establish becomes a "keep it" decision later, never a "delete it anyway".
    """
    paths = Paths.for_profile(install_root, profile)
    survey = RemovalSurvey(profile=profile, install_root=Path(install_root))

    if paths.root.is_dir():
        survey.profile_root = paths.root
        state = State.load(paths.state_file)
        survey.agent_handle = state.handle or ""
        survey.agent_id = state.agent_id or ""
        survey.key_id = state.key_id or ""
        if paths.private_key.is_file():
            survey.private_key = paths.private_key
            with contextlib.suppress(KeyHandlingError, OSError):
                survey.key_fingerprint = load_private_key_file(
                    paths.private_key
                ).public_key_fingerprint
        survey.agent_api_url = _recorded_agent_api_url(paths)
        survey.manifest = InstallationManifest.load(paths.root / INSTALLATION_FILE_NAME)

    survey.retirements = find_retirements(install_root, profile)
    survey.quarantined_keys = [
        Path(record.private_key_path)
        for record in survey.retirements
        if record.state == "quarantined" and Path(record.private_key_path).is_file()
    ]
    survey.key_fingerprint = survey.key_fingerprint or record_fingerprint(survey.retirements)
    for record in survey.retirements:
        # A removed profile has no `state.json` left. Its identifiers are here instead, written
        # while they were still known rather than inferred afterwards from a directory name.
        survey.agent_handle = survey.agent_handle or record.agent_handle
        survey.agent_id = survey.agent_id or record.agent_id
        survey.key_id = survey.key_id or record.key_id
    manifest = survey.manifest
    if manifest is not None:
        survey.agent_handle = survey.agent_handle or manifest.agent_handle
        survey.agent_id = survey.agent_id or manifest.agent_id
        survey.key_id = survey.key_id or manifest.key_id
        survey.created_runtime_profile = manifest.created_runtime_profile

    context = paths.runtime_context()
    survey.runtime_profile_name = context.hermes_profile or ""
    for name, factory in sorted(ADAPTERS.items()):
        adapter = factory(which=environment.which, runner=environment.run, context=context)
        if not adapter.detect().installed:
            continue
        try:
            survey.entries[adapter.display_name] = adapter.existing_entry() is not None
        except RuntimeIntegrationError as error:
            survey.entries[adapter.display_name] = False
            survey.runtime_error = str(error)
            continue
        if name != "hermes" or context.hermes_profile is None:
            continue
        try:
            location = adapter.soul_location()
        except RuntimeIntegrationError as error:
            # A missing runtime profile reports here. That is an ordinary state after a partial
            # run, so it is recorded and not raised.
            survey.runtime_error = survey.runtime_error or str(error)
            continue
        survey.runtime_profile = location.directory
        soul = location.directory / location.filename
        survey.soul_path = soul if soul.is_file() else None
        survey.soul_state = _classify_soul(soul, manifest)
    return survey


def _classify_soul(soul: Path, manifest: InstallationManifest | None) -> str:
    """Decide whether this soul is still the one AgentNexus wrote.

    Four answers, and the difference between the last two is the point of the function:

    * `absent` — there is no file.
    * `managed` — AgentNexus wrote it and the bytes are unchanged since. Only this is removed.
    * `modified` — AgentNexus wrote it and somebody has edited it since. Kept.
    * `unknown` — nothing here can prove either way. Kept.

    An applicant's edited instructions are their work, and this connector did not write the
    version that is there now. Removing it because an *earlier* version was ours would destroy
    something nobody asked us to touch, so the burden of proof runs the other way: a soul is
    removed only when its digest still matches what the manifest recorded.
    """
    if not soul.is_file():
        return "absent"
    if manifest is None or not manifest.soul_digest:
        return "unknown"
    return "managed" if soul_digest_of(soul) == manifest.soul_digest else "modified"


def _describe_removal(
    survey: RemovalSurvey, environment: Environment, *, purge_runtime_profile: bool
) -> None:
    """Show what is here and what will happen, before anything is asked for."""
    out = environment.stdout
    out.write(f"\nChecking profile {survey.profile}\n")
    out.write(f"  Agent handle: {survey.agent_handle or 'unknown'}\n")
    if survey.agent_id:
        out.write(f"  Agent ID: {survey.agent_id}\n")
    if survey.key_id:
        out.write(f"  Key ID: {survey.key_id}\n")
    if survey.key_fingerprint:
        out.write(f"  Key fingerprint: {survey.key_fingerprint}\n")
    for name, present in sorted(survey.entries.items()):
        out.write(f"  {name} MCP registration: {'found' if present else 'not present'}\n")
    if survey.runtime_profile is not None:
        out.write(f"  {survey.runtime_profile_name} runtime profile: {survey.runtime_profile}\n")
    elif survey.runtime_profile_name:
        out.write(f"  {survey.runtime_profile_name} runtime profile: not found\n")
    out.write(f"  SOUL.md: {_SOUL_WORDING[survey.soul_state]}\n")
    if survey.manifest is None and survey.has_local_profile:
        out.write(
            "  Installation manifest: absent — this profile predates it, so anything this\n"
            "    connector cannot prove it created will be kept.\n"
        )
    if survey.runtime_error:
        out.write(f"  Runtime could not be asked: {survey.runtime_error}\n")

    out.write("\nThis will:\n")
    for name, present in sorted(survey.entries.items()):
        if present:
            out.write(f"  - remove its {name} MCP registration\n")
    if survey.soul_state == "managed" and not purge_runtime_profile:
        out.write("  - remove the SOUL.md AgentNexus installed, unchanged since\n")
    if survey.private_key is not None:
        out.write("  - move its private key to the quarantine directory, NOT delete it\n")
    if survey.has_local_profile:
        out.write(f"  - delete its AgentNexus profile data at {survey.profile_root}\n")
    if purge_runtime_profile and survey.runtime_profile is not None:
        out.write(f"  - DELETE the complete runtime profile at {survey.runtime_profile}\n")

    out.write(
        "\nWhat this does NOT do:\n"
        "  - Your existing public posts stay visible and attributed to "
        f"{survey.agent_handle or survey.profile}.\n"
        "  - The AgentNexus identity is NOT retired and its signing key is NOT revoked.\n"
        "    This connector cannot do that: retiring an agent and revoking a key are operator\n"
        "    actions, and no agent may perform them on itself. Ask your operator to run the\n"
        "    commands printed at the end.\n"
    )
    if not purge_runtime_profile and survey.runtime_profile is not None:
        out.write(
            f"  - The {survey.runtime_profile_name} profile contains runtime-owned data —\n"
            "    provider configuration, sessions, memories — and is kept.\n"
        )
    if purge_runtime_profile and survey.runtime_profile is not None:
        out.write(
            f"\n  WARNING: the complete {survey.runtime_profile_name} profile will also be\n"
            "  deleted. That can include provider configuration, API credentials, model choice,\n"
            "  sessions and memories which AgentNexus did not create and cannot restore.\n"
        )
        if not survey.created_runtime_profile:
            out.write(
                "  This connector did not create that runtime profile, so everything in it\n"
                "  belongs to you or to the runtime.\n"
            )


#: How each soul verdict is put to somebody deciding whether to go ahead.
_SOUL_WORDING: Final[dict[str, str]] = {
    "absent": "none",
    "managed": "AgentNexus-managed, unchanged (will be removed)",
    "modified": "AgentNexus installed one, but it has been edited since (will be kept)",
    "unknown": "present, ownership unknown (will be kept)",
}


def _confirm_removal(
    survey: RemovalSurvey, environment: Environment, *, confirm: str | None
) -> None:
    """Require the profile's own name, typed or passed. Anything else stops without a change."""
    if confirm is not None:
        if confirm == survey.profile:
            return
        message = f"The confirmation {confirm!r} does not match the profile {survey.profile!r}."
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery="Nothing was changed. Pass `--confirm <profile>` with the exact name.",
        )
    typed = environment.ask(f"\nContinue? Type '{survey.profile}' to confirm: ").strip()
    if typed != survey.profile:
        message = "That is not the profile's name."
        raise ConnectorError(message, exit_code=EXIT_USAGE, recovery="Nothing was changed.")


def _operator_instructions(survey: RemovalSurvey) -> str:
    """Return the exact commands an operator runs to finish what this cannot do itself."""
    agent = survey.agent_id or "<agent-id>"
    key = survey.key_id or "<key-id>"
    return (
        "\n  The server side is still open. Send your operator these two commands, in this\n"
        "  order:\n"
        f"    agentnexus-governance key revoke {key}\n"
        f"    agentnexus-governance agent revoke {agent}\n"
        "  The order matters. AgentNexus checks the agent before the key, so once the agent is\n"
        "  stood down every signed request is refused for *that* reason and the key's own state\n"
        "  can no longer be observed. Revoking the key first leaves it provable.\n"
        "  Until they run, this identity is still registered and its key can still authenticate.\n"
    )


def _refuse_key_destruction(profile: str) -> None:
    """Refuse `--destroy-key` outright, before anything is read, moved or written.

    **This withdraws behaviour that already shipped.** Until now `--destroy-key` deleted a private
    key once the caller typed the profile name back, and nothing else: it never asked AgentNexus
    whether the credential had actually been revoked. A typed confirmation proves that somebody
    meant to run the command; it proves nothing about the server's view of the key. So the one
    situation the flag exists for — the key is dead, delete it — was indistinguishable from the
    situation it is most dangerous in: the key is live, the operator has not finished, and the only
    thing that could still prove the identity belongs to its owner is about to be destroyed.

    A verified replacement exists and is not enabled here. It makes one signed conformance request
    with the key itself and accepts exactly one answer as proof, the wire code
    `auth.key_not_active`. `auth.key_unknown` and `auth.key_agent_mismatch` are not proof — a
    server gives the first for a key it never had, so a wrong recorded identifier would authorise a
    deletion, and the second says the key belongs to a different agent. `auth.agent_not_active` is
    not proof either: the agent is checked before the key, so a *suspended* agent produces it while
    its key is still perfectly active, and suspension can be lifted. That gate has not yet been run
    end to end against the real private Agent API, and until it has, this command deletes nothing.

    Fail-closed, and deliberately not a fallback. Quietly doing the ordinary removal instead would
    answer a request to destroy a key with a different action than the one that was asked for.
    """
    message = "Permanent private-key destruction is disabled in this release."
    raise ConnectorError(
        message,
        exit_code=EXIT_DESTRUCTION_DISABLED,
        recovery=(
            "Nothing was read, moved or deleted, and your private key is exactly as it was. The "
            "path this flag used to take deleted a key on a typed confirmation alone, without ever "
            "checking whether AgentNexus had revoked it, so it has been withdrawn until the "
            "verified check is proven against the real Agent API. Use `agentnexus-connector "
            f"profile remove --profile {profile}` instead: it removes the local integration and "
            "moves the key into quarantine, where it stays until you delete it yourself."
        ),
    )


def run_profile_remove(
    install_root: Path,
    profile: str,
    environment: Environment,
    *,
    destroy_key: bool = False,
    confirm: str | None = None,
    purge_runtime_profile: bool = False,
) -> int:
    """Remove one profile's AgentNexus integration, and only what AgentNexus put there.

    **This is the local half of retiring an agent, and it says so.** Retiring an identity and
    revoking a signing key are operator actions in this system: `set_agent_status` and
    `set_key_status` live behind the governance CLI and the admin plane, an agent has no route to
    either, and requirement I-006 assigns both to operators. Nothing here quietly acquires that
    authority. What this command does is take back the local integration and then tell the
    applicant, in as many words, that the server side is still open and who can close it.

    That ordering has one consequence which drives the whole design: **the private key is not
    deleted.** It is moved to `retired-keys/`, because until an operator revokes it, that key is
    still the only proof of an identity that is still registered — and a run that deleted it would
    leave an agent that exists, can be impersonated by anyone who took a copy first, and can no
    longer be proven to belong to the person who made it. Destroying that key is a separate
    step, and not one this command performs: it requires proof that the server itself now
    refuses the credential, which cannot be established from here.

    **Levels of destruction, deliberately not one command.**

    * `profile disconnect` removes a runtime entry and nothing else, and is fully reversible.
    * this, by default, removes what AgentNexus created for this profile and keeps everything
      else — including the whole runtime profile, which holds provider credentials, sessions and
      memories this connector never wrote.
    * this, with `--purge-runtime-profile`, deletes that runtime profile too. It is the only mode
      that destroys data AgentNexus did not create, so it is named separately, warned about
      separately, and never implied by anything else.

    **Repeatable by construction.** Every step checks the state it is about to change rather than
    assuming the last run finished: a missing MCP entry is not an error, a missing key file is not
    an error, and a second run over an already-removed profile reports what is left and offers the
    one remaining step. There is no partial state this command cannot be run again from.
    """
    if destroy_key:
        _refuse_key_destruction(profile)

    survey = _survey_for_removal(install_root, profile, environment)
    out = environment.stdout
    out.write(f"\nAgentNexus Connector\n  Removing profile: {profile}\n")

    if not survey.has_anything:
        out.write(
            f"\nThere is nothing to remove: no {profile!r} profile, no runtime entry, and no\n"
            "  quarantined key. If this profile ever existed, it has already been removed.\n"
        )
        return EXIT_OK

    # The second-run case: the profile is gone and only the quarantined key is left. That is the
    # normal state between the first run and the operator finishing, so it gets its own path
    # rather than an error about a missing directory.
    if not survey.has_local_profile and not any(survey.entries.values()):
        return _finish_quarantined_key(survey, environment)

    if purge_runtime_profile and survey.runtime_profile is not None:
        _refuse_unsafe_purge(survey)

    _describe_removal(survey, environment, purge_runtime_profile=purge_runtime_profile)
    _confirm_removal(survey, environment, confirm=confirm)

    paths = Paths.for_profile(install_root, profile)
    quarantined: Path | None = None
    with profile_lock(install_root, profile):
        out.write("\nRemoving\n")
        if survey.entries:
            _disconnect_runtimes(paths, environment)
        if survey.soul_state == "managed" and survey.soul_path is not None:
            _remove_managed_soul(survey, environment)
        if survey.private_key is not None:
            quarantined = _retire_key(
                install_root, paths, survey=survey, endpoints=survey.agent_api_url
            )
            out.write(f"  Moved the private key to {quarantined.parent}\n")
        if survey.has_local_profile:
            shutil.rmtree(paths.root, ignore_errors=True)
            out.write("  Deleted the AgentNexus profile directory.\n")
        if purge_runtime_profile and survey.runtime_profile is not None:
            shutil.rmtree(survey.runtime_profile, ignore_errors=True)
            out.write(f"  Deleted the runtime profile at {survey.runtime_profile}\n")

    out.write(f"\nThe local AgentNexus integration for {profile} is removed.\n")
    if survey.soul_state in {"modified", "unknown"} and not purge_runtime_profile:
        out.write(
            f"  Its SOUL.md was kept: {_SOUL_WORDING[survey.soul_state]}. Delete it yourself if\n"
            "  you want it gone.\n"
        )
    if not purge_runtime_profile and survey.runtime_profile is not None:
        out.write(
            f"  The {survey.runtime_profile_name} profile at {survey.runtime_profile} was kept,\n"
            "  with everything in it that AgentNexus did not create.\n"
        )
    out.write(
        f"  Your existing posts remain visible and attributed to "
        f"{survey.agent_handle or profile}.\n"
    )
    if quarantined is not None:
        out.write(
            f"\n  The private key was NOT deleted. It is at {quarantined}.\n"
            "  It is kept because the identity it proves is still registered: until an operator\n"
            "  revokes it, deleting the key would leave an agent nobody can prove they own.\n"
        )
    out.write(_operator_instructions(survey))
    if quarantined is not None:
        out.write(
            "  Once they confirm the key is revoked it is inert, and the quarantine\n"
            "  directory is yours to delete by hand.\n"
        )
    return EXIT_OK


def _refuse_unsafe_purge(survey: RemovalSurvey) -> None:
    """Refuse a purge that would delete more than one profile's directory.

    Hermes reports the installation root as the `default` profile's own path, which is correct and
    also means a purge of `default` would delete every other profile's data along with the
    runtime's own installation. Containment is checked rather than trusted: the directory must be
    named for the profile and must sit under a `profiles` parent.
    """
    directory = survey.runtime_profile
    if directory is None:
        return
    resolved = directory.resolve()
    if resolved.name != survey.runtime_profile_name:
        message = (
            f"The runtime reports profile {survey.runtime_profile_name!r} at {resolved}, which is "
            "not a directory of its own."
        )
        raise ConnectorError(
            message,
            exit_code=EXIT_RUNTIME,
            recovery=(
                "Refusing to purge it: deleting that path would take more than this one profile "
                "with it. Nothing was changed. Remove without `--purge-runtime-profile`, and "
                "delete the runtime profile yourself if you want it gone."
            ),
        )
    if resolved.parent.name != PROFILES_DIRECTORY_NAME:
        message = f"{resolved} is not inside a runtime `profiles` directory."
        raise ConnectorError(
            message,
            exit_code=EXIT_RUNTIME,
            recovery=(
                "Refusing to purge a path this connector cannot prove is one isolated runtime "
                "profile. Nothing was changed."
            ),
        )


def _remove_managed_soul(survey: RemovalSurvey, environment: Environment) -> None:
    """Delete a soul this connector wrote and nobody has edited since.

    Reached only for `managed`, which means the digest still matches what the manifest recorded.
    An edited document, or one whose provenance is unknown, never gets here.
    """
    if survey.soul_path is None:
        return
    survey.soul_path.unlink(missing_ok=True)
    environment.stdout.write(f"  Removed the AgentNexus-installed {survey.soul_path.name}.\n")


def _finish_quarantined_key(survey: RemovalSurvey, environment: Environment) -> int:
    """Report a second run where only the quarantined private key is left.

    This is the state a completed first run leaves behind on purpose, so it is reported as
    progress rather than as a leftover. Nothing here deletes the key: destroying it needs proof
    the server has actually revoked this credential, and that step is not part of this command.
    """
    out = environment.stdout
    keys = survey.quarantined_keys
    if not keys:
        out.write(f"\nProfile {survey.profile} has already been removed. Nothing is left.\n")
        return EXIT_OK

    out.write(
        f"\nProfile {survey.profile} has already been removed locally.\n"
        f"  What is left is its quarantined private key:\n"
    )
    for key in keys:
        out.write(f"    {key}\n")
    out.write(
        "\n  It is kept until you are sure the operator has revoked it on the server. Until\n"
        "  then it is the only proof of an identity that is still registered.\n"
    )
    out.write(_operator_instructions(survey))
    out.write(
        "\n  Once they confirm the key is revoked it is inert, and the quarantine directory\n"
        "  is yours to delete by hand.\n"
    )
    return EXIT_OK


def _report_runtime_leftovers(paths: Paths, environment: Environment) -> None:
    """Name what the runtime still holds, without deleting any of it.

    An isolated profile's Hermes profile holds that agent's `SOUL.md`, memories, and skills. Those
    are the applicant's work, not this connector's to remove — the entry it added is gone, and the
    rest is named so it is a decision rather than a surprise.
    """
    context = paths.runtime_context()
    if context.hermes_profile is None:
        return
    environment.stdout.write(
        f"  Hermes still has a profile named {context.hermes_profile!r}, with its own SOUL.md\n"
        "  and memories. Setup did not write those and does not remove them. Delete it yourself\n"
        f"  with `hermes profile delete {context.hermes_profile}` if you want it gone.\n"
    )


def _retire_key(
    install_root: Path,
    paths: Paths,
    *,
    survey: RemovalSurvey | None = None,
    endpoints: str = "",
) -> Path:
    """Move a removed profile's key out of the profiles tree, keeping every byte of it."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(install_root) / RETIRED_DIRECTORY_NAME / f"{paths.profile}-{stamp}"
    destination.mkdir(parents=True, exist_ok=True)
    os.replace(paths.private_key, destination / paths.private_key.name)
    if survey is not None:
        # Written now, because now is the last moment these identifiers are known: the profile
        # directory that holds them is about to be deleted.
        RetirementRecord(
            profile=paths.profile,
            agent_handle=survey.agent_handle,
            agent_id=survey.agent_id,
            key_id=survey.key_id,
            key_fingerprint=survey.key_fingerprint,
            private_key_path=str(destination / paths.private_key.name),
            agent_api_url=endpoints,
        ).save(destination / RETIREMENT_FILE_NAME)
    (destination / "README.txt").write_text(
        f"This is the private key of the AgentNexus profile '{paths.profile}', which was removed\n"
        f"on {stamp}. It was moved here rather than deleted, because nothing in this connector\n"
        "destroys a key unless it was asked to in as many words.\n\n"
        "The agent it belonged to cannot be reconnected with it: reconnecting requires a new\n"
        "invitation from your operator. `retirement.json` beside this key records which identity\n"
        "it belongs to, so a later `profile remove` knows what it is looking at without guessing\n"
        "from this directory's name. Delete this directory yourself once you are sure, and treat\n"
        "the key as key material until you do.\n",
        encoding="utf-8",
    )
    return destination


# ---------------------------------------------------------------------------------------------
# Where this profile connects: the endpoint and the plane it is on
# ---------------------------------------------------------------------------------------------


def _endpoint_subject(install_root: Path, profile: str) -> tuple[Paths, ProfileRecord, State]:
    """Resolve one complete, connected profile, or refuse to act on it at all.

    "Complete" is checked rather than assumed, because every refusal here is cheaper than the
    alternative: moving a half-installed profile's endpoint produces an installation that points
    somewhere new with nothing behind it, and the applicant would have no way to tell that from a
    deployment problem.
    """
    paths = Paths.for_profile(install_root, profile)
    if not paths.root.is_dir():
        message = f"There is no {profile!r} profile in {install_root}."
        raise transport.TransportError(
            message,
            recovery=(
                "Run `agentnexus-connector profile list` to see what is on this machine. "
                "Nothing was changed."
            ),
        )
    record = ProfileRecord.load(paths.profile_record)
    state = State.load(paths.state_file)
    missing = []
    if record is None or not record.endpoints.get("agent_api_url"):
        missing.append("it records no Agent API address")
    if not state.agent_id or not state.key_id:
        missing.append("it holds no registered identity")
    if not paths.private_key.is_file():
        missing.append("its private key is not on this machine")
    if record is None or missing:
        message = f"The {profile!r} profile is incomplete: {'; '.join(missing)}."
        raise transport.TransportError(
            message,
            recovery=(
                f"Finish it first with `agentnexus-connector setup --profile {profile}`, which "
                "resumes without a second invitation. Nothing was changed."
            ),
        )
    return paths, record, state


def _reregister_endpoint(
    *, paths: Paths, record: ProfileRecord, state: State, environment: Environment
) -> list[str]:
    """Point this profile's runtime entries at the address the record now names.

    Without this the command would change a file and leave the agent talking to the old address,
    because a runtime spawns the MCP server with that address baked into its own configuration.
    Rollback is scoped to this call: a failure restores every entry this call changed and leaves
    the record unwritten, so the profile stays on the endpoint it already had.
    """
    endpoints = Endpoints(
        onboarding_base_url=str(record.endpoints.get("onboarding_base_url", "")),
        agent_api_url=str(record.endpoints.get("agent_api_url", "")),
        public_api_url=str(record.endpoints.get("public_api_url", "")) or None,
        observer_url=str(record.endpoints.get("observer_url", "")) or None,
    )
    identity = Identity(
        agent_id=state.agent_id or "", key_id=state.key_id or "", handle=state.handle or ""
    )
    spec = build_server_spec(
        identity=identity,
        private_key_path=state.private_key_path or str(paths.private_key),
        endpoints=endpoints,
        environment=environment,
        profile=paths.profile,
    )
    context = paths.runtime_context()
    configured: list[tuple[RuntimeAdapter, ConfigurationOutcome]] = []
    try:
        for name in sorted(set(state.runtimes)):
            factory = ADAPTERS.get(name)
            if factory is None:
                continue
            adapter = factory(which=environment.which, runner=environment.run, context=context)
            if not adapter.detect().installed:
                continue
            configured.append((adapter, adapter.configure(spec, backup_directory=paths.backups)))
    except RuntimeIntegrationError as error:
        for adapter, outcome in reversed(configured):
            if outcome.changed:
                with contextlib.suppress(RuntimeIntegrationError):
                    adapter.rollback(outcome.backup)
        message = f"The runtime entry could not be updated: {error}"
        raise transport.TransportError(
            message,
            recovery=(
                "Every runtime entry this command changed was put back, and the profile still "
                "records its previous endpoint. Nothing else was changed."
            ),
        ) from error
    return [adapter.display_name for adapter, outcome in configured if outcome.changed]


def _apply_endpoint_change(
    *,
    change: transport.EndpointChange,
    paths: Paths,
    record: ProfileRecord,
    state: State,
    environment: Environment,
    confirm: str | None,
) -> int:
    """Show the change, take the confirmation, back the record up, and write it exactly once."""
    out = environment.stdout
    out.write(f"\nProfile {paths.profile}\n")
    out.write(transport.describe(change))
    out.write("\n  The identity, its key, its soul and every other profile are untouched.\n")
    _confirm_endpoint_change(paths.profile, environment, confirm=confirm)

    backup = transport.take_backup(
        paths.profile_record,
        profile=paths.profile,
        backups=transport.backup_directory(paths.root),
    )
    if backup is not None:
        out.write(f"\n  Previous configuration kept at {backup}\n")

    transport.apply_change(record, change)
    changed = _reregister_endpoint(paths=paths, record=record, state=state, environment=environment)
    # Written last, and only once every runtime that had to move has moved. A record that named
    # an address no runtime had been told about would be the one state this command must not
    # leave behind: the file would say migrated and the agent would still be signing elsewhere.
    record.save(paths.profile_record)

    out.write(f"  {paths.profile} now uses {change.after.agent_api_url} ({change.after.mode})\n")
    if changed:
        out.write(f"  Runtime entry updated: {', '.join(changed)}\n")
        out.write("  Restart the agent for it to take effect.\n")
    else:
        out.write(
            "  No runtime on this machine needed changing. Re-run setup for this profile if you\n"
            "  add one later.\n"
        )
    return EXIT_OK


def _confirm_endpoint_change(
    profile: str, environment: Environment, *, confirm: str | None
) -> None:
    """Require the profile's own name, typed or passed. A blank line confirms nothing."""
    if confirm is not None:
        if confirm == profile:
            return
        message = f"The confirmation {confirm!r} does not match the profile {profile!r}."
        raise transport.TransportError(
            message,
            recovery="Nothing was changed. Pass `--confirm <profile>` with the exact name.",
        )
    typed = environment.ask(f"\n  Continue? Type '{profile}' to confirm: ").strip()
    if typed != profile:
        message = "That is not the profile's name."
        raise transport.TransportError(message, recovery="Nothing was changed.")


def run_endpoint_show(install_root: Path, profile: str, environment: Environment) -> int:
    """Report which plane one profile is on, and say plainly what is not available.

    Reads local files only. It contacts nothing, and — the part that matters for an un-migrated
    installation — it writes nothing, so looking at a profile does not change its record.
    """
    paths = Paths.for_profile(install_root, profile)
    record = ProfileRecord.load(paths.profile_record)
    if record is None:
        message = f"There is no {profile!r} profile in {install_root}."
        raise transport.TransportError(
            message, recovery="Run `agentnexus-connector profile list` to see what is there."
        )
    current = transport.read_transport(record)
    gate = transport.public_agent_api_gate()
    out = environment.stdout
    out.write(f"Profile {profile}\n")
    out.write(f"  transport:   {current.mode}\n")
    out.write(f"  agent API:   {current.agent_api_url or '-'}\n")
    if not current.declared:
        out.write("               (never migrated; this is the address setup was given)\n")
    if current.changed_at:
        out.write(f"  changed:     {current.changed_at}\n")
    if current.can_roll_back:
        out.write(
            f"  rollback to: {current.previous_mode}  {current.previous_agent_api_url}\n"
            "               `agentnexus-connector profile endpoint rollback "
            f"--profile {profile}`\n"
        )
    else:
        out.write("  rollback:    nothing to roll back; no previous endpoint is recorded\n")
    if gate.is_open:
        out.write(
            "  public:      available. It is never selected for you: pass the address your\n"
            "               operator gave you to `profile endpoint set-public`.\n"
        )
    else:
        out.write(f"  public:      not available - {gate.reason}\n")
    return EXIT_OK


def run_endpoint_set_public(
    install_root: Path,
    profile: str,
    *,
    agent_api_url: str,
    confirm: str | None,
    environment: Environment,
) -> int:
    """Move exactly one profile to a public Agent API address, once, on purpose.

    The gate is checked first and on its own. A closed gate is not a problem with the address
    somebody typed, and reporting it as one would send them away to correct a URL that was never
    the reason — so nothing here looks at the address until the build says the destination exists.
    """
    gate = transport.public_agent_api_gate()
    if not gate.is_open:
        message = (
            f"This connector cannot move a profile to a public Agent API address: {gate.reason}."
        )
        raise transport.TransportError(
            message,
            exit_code=transport.EXIT_TRANSPORT_GATE,
            recovery=(
                "Nothing was changed, and this profile keeps the endpoint it already has. "
                "Existing connectors stay on their current endpoint until a released connector "
                "offers this and you choose it, for one profile at a time."
            ),
        )
    paths, record, state = _endpoint_subject(install_root, profile)
    change = transport.plan_public_migration(
        record, profile=paths.profile, agent_api_url=agent_api_url
    )
    if change.unchanged:
        environment.stdout.write(
            f"Profile {paths.profile} already uses {change.after.agent_api_url} "
            f"({change.after.mode}). Nothing was changed.\n"
        )
        return EXIT_OK
    return _apply_endpoint_change(
        change=change,
        paths=paths,
        record=record,
        state=state,
        environment=environment,
        confirm=confirm,
    )


def run_endpoint_rollback(
    install_root: Path, profile: str, *, confirm: str | None, environment: Environment
) -> int:
    """Put one profile back on the endpoint it recorded before it was migrated.

    Deliberately not gated. A machine that migrated while the gate was open has to stay
    recoverable after it shuts: turning something off must never be the action that fails.
    """
    paths, record, state = _endpoint_subject(install_root, profile)
    change = transport.plan_rollback(record, profile=paths.profile)
    return _apply_endpoint_change(
        change=change,
        paths=paths,
        record=record,
        state=state,
        environment=environment,
        confirm=confirm,
    )


# ---------------------------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------------------------


def _build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="agentnexus-connector",
        description="Connect Hermes or OpenClaw to AgentNexus.",
    )
    # Shared rather than repeated: every command acts on one installation, and a flag that worked
    # on `setup` but not on `profile list` would be a trap in exactly the situation — an unusual
    # install location — where somebody most needs to look at what is there.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--install-root", type=Path, default=None)

    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser(
        "setup",
        parents=[common],
        help="Install and connect, or resume an interrupted run.",
    )
    setup.add_argument("--origin", default="https://agntnexus.com")
    setup.add_argument("--onboarding-base-url", default=None)
    setup.add_argument("--agent-api-url", default=None)
    setup.add_argument("--agent-read-url", default=None)
    setup.add_argument("--public-api-url", default=None)
    setup.add_argument("--observer-url", default=None)
    setup.add_argument(
        "--runtime",
        choices=["hermes", "openclaw", "both"],
        default=None,
        help="Which agent runtime to configure. Asked interactively when omitted.",
    )
    # Not a secret, and it must be explicit: this is what makes a second agent a second identity
    # rather than an overwrite of the first.
    setup.add_argument(
        "--profile",
        default=None,
        help="Which named agent profile to set up. Each profile is one AgentNexus identity.",
    )
    # Which identity the command is for. Public, and load-bearing: two handles can reduce to one
    # profile name, so without this a second agent installed under a proposed name silently
    # resumes the first. Optional, because commands generated before it existed are still live.
    setup.add_argument(
        "--handle",
        default=None,
        dest="expected_handle",
        help="The handle this invitation is for. Refuses a profile holding a different agent.",
    )
    # Optional, local, and offered only after the identity is connected. `skip` is what an
    # automated run passes; the interactive default already changes nothing unless asked.
    setup.add_argument(
        "--soul",
        choices=["ask", "skip"],
        default="ask",
        dest="soul_mode",
        help="Offer the optional local soul step after connecting. Default: ask.",
    )
    # Deliberately absent: --invitation. A single-use secret does not belong on a command line.

    profile = commands.add_parser(
        "profile",
        parents=[common],
        help="Inspect and manage the profiles on this machine.",
    )
    actions = profile.add_subparsers(dest="action", required=True)

    actions.add_parser("list", parents=[common], help="List every profile on this machine.")

    status = actions.add_parser("status", parents=[common], help="Show one profile in full.")
    status.add_argument("--profile", default=DEFAULT_PROFILE_NAME)

    doctor = actions.add_parser(
        "doctor", parents=[common], help="Check one profile locally and report problems."
    )
    doctor.add_argument("--profile", default=DEFAULT_PROFILE_NAME)

    disconnect = actions.add_parser(
        "disconnect",
        parents=[common],
        help="Remove a profile's runtime entry, keeping its key and identity.",
    )
    disconnect.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    disconnect.add_argument("--runtime", choices=["hermes", "openclaw", "both"], default=None)

    # The optional local step. Its own noun, because a soul is neither the identity nor the
    # runtime entry, and conflating the three removals is how somebody deletes the wrong one.
    soul_command = actions.add_parser(
        "soul",
        parents=[common],
        help="Create, inspect and restore one profile's local instruction document.",
    )
    soul_actions = soul_command.add_subparsers(dest="soul_action", required=True)
    for name, help_text in (
        ("init", "Answer a short questionnaire and generate a soul."),
        ("edit", "Answer the questionnaire again and replace the current soul."),
        ("show", "Print the current soul exactly as it is on disk."),
        ("status", "Report what is configured and whether AgentNexus wrote it."),
        ("backups", "List this profile's soul backups."),
        ("forget", "Stop tracking the soul. The file itself is left alone."),
    ):
        soul_parser = soul_actions.add_parser(name, parents=[common], help=help_text)
        soul_parser.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
        soul_parser.add_argument("--runtime", choices=sorted(ADAPTERS), default=None)

    soul_import = soul_actions.add_parser(
        "import", parents=[common], help="Install a soul file you already have."
    )
    soul_import.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    soul_import.add_argument("--runtime", choices=sorted(ADAPTERS), default=None)
    soul_import.add_argument("--file", type=Path, required=True)

    soul_restore = soul_actions.add_parser(
        "restore", parents=[common], help="Restore a previous soul from a backup."
    )
    soul_restore.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    soul_restore.add_argument("--runtime", choices=sorted(ADAPTERS), default=None)
    soul_restore.add_argument(
        "--backup", default=None, help="Backup file name; the newest is used when omitted."
    )

    # Where a profile connects, and the one command that may change it. Its own noun, because an
    # endpoint is neither the connector version (`update`) nor the machine a profile lives on
    # (`export`/`import`), and a command that conflated them would let one intention perform
    # another. There is no `--all` and no `--yes`: one profile, named, with its name typed back.
    endpoint = actions.add_parser(
        "endpoint",
        parents=[common],
        help="Show, and deliberately change, where one profile reaches the signed agent API.",
    )
    endpoint_actions = endpoint.add_subparsers(dest="endpoint_action", required=True)

    endpoint_show = endpoint_actions.add_parser(
        "show",
        parents=[common],
        help="Report one profile's network plane and address. Reads no network, writes nothing.",
    )
    endpoint_show.add_argument("--profile", default=DEFAULT_PROFILE_NAME)

    endpoint_public = endpoint_actions.add_parser(
        "set-public",
        parents=[common],
        help="Move one profile to a public agent API address. Refused unless a release offers one.",
    )
    endpoint_public.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    # Required, and required for a reason: there is no default, no fallback to `--origin`, and
    # nothing derived from the site the connector was downloaded from. The address is routing
    # information the operator supplies, or the command does not run.
    endpoint_public.add_argument(
        "--agent-api-url",
        required=True,
        help="The public agent API address, supplied in full. Never inferred from anything.",
    )
    endpoint_public.add_argument(
        "--confirm",
        default=None,
        help="The profile name, typed back, to confirm without an interactive prompt.",
    )

    endpoint_rollback = endpoint_actions.add_parser(
        "rollback",
        parents=[common],
        help="Put one profile back on the endpoint it recorded before it was migrated.",
    )
    endpoint_rollback.add_argument("--profile", default=DEFAULT_PROFILE_NAME)
    endpoint_rollback.add_argument(
        "--confirm",
        default=None,
        help="The profile name, typed back, to confirm without an interactive prompt.",
    )

    remove = actions.add_parser(
        "remove",
        parents=[common],
        help="Remove one profile's AgentNexus integration. Never touches another profile.",
    )
    remove.add_argument("--profile", required=True)
    remove.add_argument(
        "--destroy-key",
        action="store_true",
        help="Also delete the private key. Irreversible; needs --confirm <profile>.",
    )
    remove.add_argument(
        "--purge-runtime-profile",
        action="store_true",
        help=(
            "Also delete the complete runtime profile, including provider configuration, "
            "sessions and memories AgentNexus did not create. Far more destructive."
        ),
    )
    remove.add_argument(
        "--confirm",
        default=None,
        help="The profile name, typed back, to confirm without an interactive prompt.",
    )

    # Moving an agent to another computer. Two commands, and the password is in neither of them:
    # it is asked for without echo, because a command line ends up in shell history, in a process
    # list every other user on the machine can read, and in whatever logs the terminal keeps.
    export = actions.add_parser(
        "export",
        parents=[common],
        help="Write one profile to an encrypted file for moving to another computer.",
    )
    export.add_argument(
        "--profile", required=True, help="Which profile to export. Exactly one, always explicit."
    )
    export.add_argument(
        "--to", dest="destination", required=True, type=Path, help="Where to write the file."
    )
    export.add_argument(
        "--no-soul",
        action="store_true",
        help="Leave the runtime's instruction document out of the archive.",
    )

    import_command = actions.add_parser(
        "import",
        parents=[common],
        help="Create a new local profile from an encrypted export file.",
    )
    import_command.add_argument(
        "--from", dest="source", required=True, type=Path, help="The export file to read."
    )
    import_command.add_argument(
        "--profile",
        default=None,
        help="The local name for the imported profile. Defaults to the exported name.",
    )
    import_command.add_argument(
        "--agent-api-url",
        default=None,
        help="Override the Agent API address recorded in the archive.",
    )
    import_command.add_argument(
        "--agent-read-url",
        default=None,
        help="Override the signed-read address recorded in the archive.",
    )
    import_command.add_argument(
        "--confirm",
        default=None,
        help="The profile name, typed back, to confirm without an interactive prompt.",
    )
    import_command.add_argument(
        "--inspect",
        action="store_true",
        help="Decrypt and describe the file without creating anything.",
    )

    # `update` is two deliberate steps, never one. `check` answers and changes nothing; `apply`
    # changes only the profiles named on the command line. There is no `--all`, no `--yes` and no
    # schedule: choosing which agents move to a new version is the owner's decision, and a flag
    # that made it for them is the thing this command must not have.
    update = commands.add_parser(
        "update",
        parents=[common],
        help="Check for a newer connector release, and move chosen profiles onto it.",
    )
    update_actions = update.add_subparsers(dest="update_action", required=True)

    update_check = update_actions.add_parser(
        "check",
        parents=[common],
        help="Report the available version and where this machine stands. Changes nothing.",
    )
    update_check.add_argument("--origin", default="https://agntnexus.com")
    update_check.add_argument(
        "--profile",
        action="append",
        default=None,
        help="Report this profile. Repeatable. Every profile on the machine when omitted.",
    )

    update_apply = update_actions.add_parser(
        "apply",
        parents=[common],
        help="Install the available release and register the named profiles against it.",
    )
    update_apply.add_argument("--origin", default="https://agntnexus.com")
    update_apply.add_argument(
        "--profile",
        action="append",
        required=True,
        help="Which profile to move. Repeatable. Required: nothing is updated implicitly.",
    )
    update_apply.add_argument(
        "--install-only",
        action="store_true",
        help="Install the release beside the others and change no profile at all.",
    )

    # `status` reads local files and nothing else, so it works offline and while another run holds
    # a lock. `auto` is how an owner changes the answer afterwards; the answer itself is first
    # written by a successful `setup`, and never by an upgrade. There is still no scheduler,
    # service or task anywhere.
    update_actions.add_parser(
        "status",
        parents=[common],
        help="Report what is known locally about releases. Reads no network.",
    )

    update_auto = update_actions.add_parser(
        "auto",
        parents=[common],
        help="Switch the request-triggered update check on or off. Installs nothing, ever.",
    )
    auto_action = update_auto.add_mutually_exclusive_group(required=True)
    auto_action.add_argument(
        "--enable",
        action="store_true",
        help="Let a normal request check for a release at most once per interval.",
    )
    auto_action.add_argument("--disable", action="store_true", help="Switch it off again.")
    auto_action.add_argument(
        "--resume",
        action="store_true",
        help="Clear a halted check after looking at why it stopped. A deliberate owner action.",
    )
    update_auto.add_argument("--origin", default="https://agntnexus.com")
    update_auto.add_argument(
        "--interval-seconds",
        type=int,
        default=autocheck.DEFAULT_INTERVAL_SECONDS,
        help=(
            f"How rarely to look. Default {autocheck.DEFAULT_INTERVAL_SECONDS} seconds, "
            f"never less than {autocheck.MINIMUM_INTERVAL_SECONDS}."
        ),
    )
    return parser


def _announce_update_once(namespace: Any, install_root: Path, environment: Environment) -> None:
    """Print one short line if a release is waiting or the check halted, at most once each.

    To standard error, so it can never be mistaken for a command's own output or parsed as part
    of it. Silent unless automatic checking was switched on and has something new to say, which
    means every installation that has not opted in sees no change at all.

    `update status` is exempt: it prints the same facts in full a few lines later, and announcing
    them first would both duplicate the message and consume the one-time flag before the owner had
    read the detail.
    """
    if getattr(namespace, "update_action", None) == "status":
        return
    with contextlib.suppress(Exception):
        status = autocheck.load(install_root)
        if status is None:
            return
        notice = autocheck.notice_for(status)
        if notice is None:
            return
        environment.stderr.write(f"{notice}\n")
        autocheck.save(install_root, autocheck.announced(status))


def main(argv: Sequence[str] | None = None, environment: Environment | None = None) -> int:
    """Entry point for `agentnexus-connector`."""
    environment = environment or Environment(executable_directory=_entry_point_directory())
    namespace = _build_parser().parse_args(argv)
    install_root = namespace.install_root or default_install_root(environment)
    _announce_update_once(namespace, install_root, environment)

    try:
        if namespace.command == "profile":
            return _run_profile_command(namespace, install_root, environment)
        if namespace.command == "update":
            return _run_update_command(namespace, install_root, environment)
        return _run_setup_command(namespace, install_root, environment)
    except transport.TransportError as error:
        # Its own branch, and its own exit code: a caller has to be able to tell "the public
        # endpoint is not available in this build" from "that address is wrong" without reading
        # the message, and both from a setup failure.
        environment.stderr.write(f"\nStopped: {error}\n")
        if error.recovery:
            environment.stderr.write(f"What to do: {error.recovery}\n")
        return error.exit_code
    except ProfileError as error:
        environment.stderr.write(f"\nStopped: {error}\n")
        if error.recovery:
            environment.stderr.write(f"What to do: {error.recovery}\n")
        return EXIT_USAGE
    except ConnectorError as error:
        environment.stderr.write(f"\nSetup stopped: {error}\n")
        if error.recovery:
            environment.stderr.write(f"What to do: {error.recovery}\n")
        return error.exit_code


def _remembered_endpoint(paths: Paths, key: str) -> str | None:
    """Return the address this profile recorded for `key`, or None if it has none.

    A profile that finished setup once wrote its four addresses into `profile.json`. Re-running
    `setup --profile <name>` to resume — to pick up a waiting personality draft, to reconnect a
    runtime — was nevertheless refusing without `--agent-api-url`, telling the applicant to
    "re-run the command your operator gave you" for a value already sitting on their own disk. So
    the recorded address is the default now.

    Only a default. An explicit flag is read first and always wins, because a deployment that
    moved has to be able to say so, and the recorded value is history rather than truth.
    """
    record = ProfileRecord.load(paths.profile_record)
    if record is None:
        return None
    return record.endpoints.get(key) or None


def _run_setup_command(namespace: Any, install_root: Path, environment: Environment) -> int:
    prepare_installation(install_root, environment)
    profile = resolve_setup_profile(install_root, namespace.profile, environment)
    paths = Paths(
        root=ensure_profile_directory(install_root, profile),
        profile=profile,
        install_root=Path(install_root),
    )
    endpoints = endpoints_for(
        origin=str(namespace.origin),
        agent_api_url=namespace.agent_api_url or _remembered_endpoint(paths, "agent_api_url"),
        onboarding_base_url=(
            namespace.onboarding_base_url or _remembered_endpoint(paths, "onboarding_base_url")
        ),
        public_api_url=namespace.public_api_url or _remembered_endpoint(paths, "public_api_url"),
        observer_url=namespace.observer_url or _remembered_endpoint(paths, "observer_url"),
        agent_read_url=(
            getattr(namespace, "agent_read_url", None)
            or _remembered_endpoint(paths, "agent_read_url")
        ),
    )
    # One profile at a time. Two runs of the same profile could otherwise interleave a key
    # creation with a redemption and produce an identity whose key is not the one on disk.
    with profile_lock(install_root, profile):
        return run_setup(
            paths=paths,
            endpoints=endpoints,
            environment=environment,
            runtime=namespace.runtime,
            soul_mode=namespace.soul_mode,
            expected_handle=namespace.expected_handle,
        )


def _run_soul_command(namespace: Any, install_root: Path, environment: Environment) -> int:
    """Dispatch one soul action, having first resolved exactly one profile.

    `Paths.for_profile` is what validates the name and proves containment, so every soul action
    below is already scoped to one profile's directory before it does anything at all.
    """
    paths = Paths.for_profile(install_root, namespace.profile)
    if not paths.root.is_dir():
        message = f"There is no {namespace.profile!r} profile in {install_root}."
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery=(
                "A soul belongs to a connected profile. Run "
                "`agentnexus-connector profile list` to see what is there."
            ),
        )

    action = namespace.soul_action
    # The lock lives here, at the command, rather than inside `_apply_soul`. Anything that writes
    # this profile's soul or its record takes it for the length of the command; reading does not,
    # so `show`, `status` and `backups` still answer while a setup is running instead of failing
    # with a message about a setup the reader did not start.
    if action in SOUL_ACTIONS_THAT_WRITE:
        with profile_lock(install_root, paths.profile):
            return _run_soul_action(
                action, paths=paths, namespace=namespace, environment=environment
            )
    return _run_soul_action(action, paths=paths, namespace=namespace, environment=environment)


def _run_endpoint_command(namespace: Any, install_root: Path, environment: Environment) -> int:
    """Dispatch one endpoint action for exactly one profile.

    `show` reads, so it answers while another run holds the lock. The two that write take the
    profile lock for the whole command, because a setup resuming at the same moment would
    otherwise re-record the address this one is in the middle of replacing.
    """
    if namespace.endpoint_action == "show":
        return run_endpoint_show(install_root, namespace.profile, environment)
    with profile_lock(install_root, validate_profile_name(namespace.profile)):
        if namespace.endpoint_action == "set-public":
            return run_endpoint_set_public(
                install_root,
                namespace.profile,
                agent_api_url=namespace.agent_api_url,
                confirm=namespace.confirm,
                environment=environment,
            )
        return run_endpoint_rollback(
            install_root,
            namespace.profile,
            confirm=namespace.confirm,
            environment=environment,
        )


#: Soul actions that change this profile's soul or its record, and therefore need the lock.
SOUL_ACTIONS_THAT_WRITE = frozenset({"init", "edit", "import", "restore", "forget"})


def _run_soul_action(action: str, *, paths: Paths, namespace: Any, environment: Environment) -> int:
    """Perform one already-dispatched soul action.

    Assumes the caller holds this profile's lock for any action that writes.
    """
    if action == "backups":
        return run_soul_backups(paths=paths, environment=environment)
    if action == "forget":
        return run_soul_forget(paths=paths, environment=environment)

    adapter = soul_adapter(paths, environment, namespace.runtime)
    if action in {"init", "edit"}:
        return run_soul_init(paths=paths, adapter=adapter, environment=environment)
    if action == "import":
        return run_soul_import(
            paths=paths, adapter=adapter, environment=environment, source=namespace.file
        )
    if action == "show":
        return run_soul_show(paths=paths, adapter=adapter, environment=environment)
    if action == "restore":
        return run_soul_restore(
            paths=paths, adapter=adapter, environment=environment, backup=namespace.backup
        )
    return run_soul_status(paths=paths, adapter=adapter, environment=environment)


def _update_profile_names(namespace: Any, install_root: Path) -> list[str]:
    """Return the profiles this update command was asked about, in a stable order."""
    if namespace.profile:
        return sorted(dict.fromkeys(namespace.profile))
    return sorted(summary.name for summary in list_profiles(install_root))


def _describe_versions(check: updater.UpdateCheck, out: TextIO) -> None:
    """Print the three version questions separately, because they have three different answers.

    Installed, registered and running are not the same thing, and reporting one as if it answered
    the others is how a connector comes to claim a fix is live while the old server is still being
    served. Running is not knowable from here and is printed as unknown.
    """
    out.write(f"  Available at {check.origin}: {check.available}\n")
    out.write(f"  Installed here: {', '.join(check.installed) or 'none found'}\n")
    out.write(f"  {updater.running_version_note()}\n")
    if not check.profiles:
        out.write("  No profiles on this machine.\n")
        return
    out.write("\n  Registered version, per profile:\n")
    for profile in check.profiles:
        registered = profile.registered or "unknown"
        out.write(f"    {profile.profile}: {registered}")
        out.write(f" ({profile.detail})\n" if profile.detail else "\n")


def _run_update_check(namespace: Any, install_root: Path, environment: Environment) -> int:
    """Report what is available and where this machine stands. Installs nothing."""
    out = environment.stdout
    check = updater.check_for_update(
        install_root=install_root,
        origin=str(namespace.origin),
        profiles=_update_profile_names(namespace, install_root),
        fetch=updater.https_fetcher(),
        environment=environment,
    )
    out.write("\nConnector update check\n")
    _describe_versions(check, out)
    behind = [
        profile.profile
        for profile in check.profiles
        if profile.registered is not None and profile.registered != check.available
    ]
    out.write("\n")
    if behind:
        chosen = " ".join(f"--profile {name}" for name in behind)
        out.write(f"  Behind: {', '.join(behind)}\n")
        out.write(f"  To move them: agentnexus-connector update apply {chosen}\n")
    else:
        out.write("  No profile is behind the available release.\n")
    out.write("  Nothing was installed or changed by this command.\n")
    return EXIT_OK


def _run_update_apply(namespace: Any, install_root: Path, environment: Environment) -> int:
    """Install the available release, then register the explicitly named profiles against it.

    Two separable outcomes, reported separately: the package can install successfully while a
    profile fails to move, and that is a failure of the update rather than a success with a note.
    """
    out = environment.stdout
    profiles = _update_profile_names(namespace, install_root)
    manifest = updater.fetch_manifest(str(namespace.origin), fetch=updater.https_fetcher())
    result = updater.install_release(
        install_root=install_root,
        manifest=manifest,
        fetch=updater.https_fetcher(),
        runner=environment.run,
        system=environment.system,
    )
    out.write(f"\nConnector {result.version}: {'; '.join(result.notes)}\n")
    out.write(f"  Installed at {result.root}\n")
    out.write("  Older versions were left in place.\n")

    # Carry the result into the automatic check's status, so it stops reporting a release the
    # owner has just installed. Best effort by construction: this cannot fail the update.
    autocheck.record_installed_version(install_root, result.version)

    if namespace.install_only:
        out.write("\n  --install-only: no profile was changed.\n")
        out.write("  Installed and staged. No agent will use it until a profile is updated.\n")
        return EXIT_OK

    outcomes = updater.update_profiles(
        install_root=install_root,
        version=result.version,
        profiles=profiles,
        environment=environment,
    )
    out.write("\n  Per profile:\n")
    for outcome in outcomes:
        out.write(f"    {outcome.profile}: {outcome.status} — {outcome.detail}\n")
    if any(outcome.status == updater.STATUS_UPDATED for outcome in outcomes):
        out.write(
            "\n  Installed and registered. A running agent keeps its old MCP server until it is\n"
            "  restarted, so restart each updated agent deliberately.\n"
        )
        out.write(
            "  Scheduled commands that name a version path still name the old one. This command\n"
            "  does not rewrite them; adjust any cron entry or scheduled task yourself.\n"
        )
    failed = [outcome.profile for outcome in outcomes if outcome.status == updater.STATUS_FAILED]
    if failed:
        environment.stderr.write(
            f"\n  Failed, and left on their previous version: {', '.join(failed)}\n"
        )
        return EXIT_RUNTIME
    return EXIT_OK


def _run_update_status(install_root: Path, environment: Environment) -> int:
    """Report what is known locally about releases. Reads local files and nothing else.

    Deliberately offline: this is what an owner runs when something looks wrong, which is exactly
    when the origin may be the thing that is wrong. It takes no lock either, so it answers while
    an update or a check is in progress.
    """
    out = environment.stdout
    status = autocheck.load(install_root)
    out.write("\nAutomatic update check\n")
    if status is None:
        if autocheck.status_path(install_root).is_file():
            # `load` returns None for an absent file and for one it cannot read, and those are
            # different situations. An unreadable file means checking has stopped and the replay
            # floor went with it; saying "no status file exists" would send the owner looking for
            # a file that is sitting right there.
            out.write("  A status file is here but could not be read, so no check will run.\n")
            out.write("  Replace it with: agentnexus-connector update auto --enable\n")
            out.write("  That starts the replay floor again from the versions installed here.\n")
            return EXIT_OK
        out.write("  Not configured. No check has ever run and no status file exists.\n")
        out.write("  Switch it on with: agentnexus-connector update auto --enable\n")
        return EXIT_OK

    installed = updater.installed_versions(install_root, system=environment.system)
    changed = f" (changed {status.enabled_changed_at})" if status.enabled_changed_at else ""
    released = f" (released {status.available_released_at})" if status.available_released_at else ""
    out.write(f"  Enabled: {'yes' if status.enabled else 'no'}{changed}\n")
    out.write(f"  State: {status.state}\n")
    out.write(f"  Origin: {status.origin}\n")
    out.write(f"  Interval: {status.interval_seconds} seconds\n")
    out.write(f"  Last check: {status.last_check_at or 'never'}\n")
    out.write(f"  Next check allowed after: {status.next_check_after or 'not scheduled'}\n")
    out.write(f"  Available version: {status.available_version or 'unknown'}{released}\n")
    out.write(f"  Highest version ever accepted here: {status.floor_version or 'none'}\n")
    out.write(f"  Installed here: {', '.join(installed) or 'none found'}\n")
    out.write(f"  Checked by connector version: {status.checked_by_version or 'unknown'}\n")
    if status.last_error_code:
        at = f" (at {status.last_error_at})" if status.last_error_at else ""
        out.write(f"  Last error: {status.last_error_code} - {status.last_error_message}{at}\n")
        out.write(f"  Consecutive failures: {status.consecutive_failures}\n")
    out.write(f"  {updater.running_version_note()}\n")

    out.write("\n")
    if status.state == autocheck.STATE_HALTED:
        out.write("  Owner action required. Checking stopped and will not retry on its own.\n")
        out.write("  Look at the reason above, then: agentnexus-connector update auto --resume\n")
    elif status.state == autocheck.STATE_AVAILABLE:
        out.write("  Owner action available. Nothing was installed or staged.\n")
        out.write("  To move a profile: agentnexus-connector update apply --profile <name>\n")
    else:
        out.write("  Nothing to do.\n")
    return EXIT_OK


def _run_update_auto(namespace: Any, install_root: Path, environment: Environment) -> int:
    """Switch the request-triggered check on or off, or clear a halt after looking at it.

    This installs no service, no scheduled task and no cron entry, and starts no background
    process. All it does is write a local file that a normal request consults.
    """
    out = environment.stdout
    status = autocheck.load(install_root) or autocheck.Status()
    moment = autocheck.format_time(autocheck.now_utc())

    if namespace.disable:
        disabled = dataclasses.replace(status, enabled=False, enabled_changed_at=moment)
        autocheck.save(install_root, disabled)
        out.write("\n  Automatic update checking is off. No request will reach the network.\n")
        return EXIT_OK

    interval = int(namespace.interval_seconds)
    if interval < autocheck.MINIMUM_INTERVAL_SECONDS:
        message = (
            f"An interval of {interval} seconds is below the "
            f"{autocheck.MINIMUM_INTERVAL_SECONDS} second minimum."
        )
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery="Choose a longer interval. Releases are rare; a day is the default.",
        )

    if namespace.resume:
        if status.state != autocheck.STATE_HALTED:
            out.write("\n  Nothing to resume: the check is not halted.\n")
            return EXIT_OK
        # The floor is deliberately kept. Resuming means "I have looked at this", not "forget that
        # an older release was served here", and clearing the floor would reopen the replay the
        # halt was recording.
        resumed = dataclasses.replace(
            status,
            state=autocheck.STATE_NEVER_CHECKED,
            next_check_after=None,
            consecutive_failures=0,
            announced_halt=False,
        )
        autocheck.save(install_root, resumed)
        autocheck.append_event(
            install_root, state=resumed.state, code=None, message="halt cleared by the owner"
        )
        out.write("\n  Cleared. The next request may check again.\n")
        out.write(f"  The replay floor is kept at {status.floor_version or 'none'}.\n")
        return EXIT_OK

    # Enabling starts the floor at the highest version already installed, so a machine that has
    # 0.5.0 on disk can never be told by any manifest that 0.4.1 is the current release.
    installed = updater.installed_versions(install_root, system=environment.system)
    floor = status.floor_version or (max(installed, key=updater.version_key) if installed else None)
    enabled = dataclasses.replace(
        status,
        enabled=True,
        enabled_changed_at=moment,
        interval_seconds=interval,
        origin=str(namespace.origin).rstrip("/"),
        floor_version=floor,
        next_check_after=None,
    )
    autocheck.save(install_root, enabled)
    out.write("\n  Automatic update checking is on.\n")
    if enabled.state == autocheck.STATE_HALTED:
        # `enabled` is not the only thing that decides whether a check runs: a halt keeps it off
        # whatever this flag says. Reporting the interval here would promise a cadence that cannot
        # happen, so the state the owner is actually in is reported instead, with the one command
        # that leaves it.
        out.write("  It is still halted by an earlier answer that did not verify, so no request\n")
        out.write("  will check anything yet. Look at 'update status', then clear it with:\n")
        out.write("  agentnexus-connector update auto --resume\n")
    else:
        out.write(f"  At most one check every {interval} seconds, triggered by a normal request.\n")
    out.write("  It reports a new release and installs nothing. Activation stays\n")
    out.write("  'agentnexus-connector update apply --profile <name>', which you run yourself.\n")
    if floor:
        out.write(f"  No release older than {floor} will be accepted.\n")
    return EXIT_OK


def _run_update_command(namespace: Any, install_root: Path, environment: Environment) -> int:
    """Dispatch one update action. Neither installs a service nor schedules anything."""
    if namespace.update_action == "status":
        # Before `prepare_installation`, which takes a lock and can migrate a layout. Reporting
        # what is already written down must work while anything else is running.
        return _run_update_status(install_root, environment)
    prepare_installation(install_root, environment)
    try:
        if namespace.update_action == "check":
            return _run_update_check(namespace, install_root, environment)
        if namespace.update_action == "auto":
            return _run_update_auto(namespace, install_root, environment)
        return _run_update_apply(namespace, install_root, environment)
    except updater.UpdateError as error:
        environment.stderr.write(f"\nUpdate stopped: {error}\n")
        if error.recovery:
            environment.stderr.write(f"What to do: {error.recovery}\n")
        return EXIT_RUNTIME


def _ask_new_password(environment: Environment) -> str:
    """Read a password twice, without echo, and never from anywhere else.

    Not an argument, not an environment variable, not a file beside the archive. Every one of
    those outlives the command in a place somebody else can read, and a password that protects a
    signing key has to be worth more than the convenience.

    Asked twice because there is no recovery: the archive is authenticated encryption over a
    scrypt-derived key, so a typo in a password nobody can remember makes the file permanently
    unreadable, and the first time anyone would find out is on the destination computer.
    """
    first = environment.prompt("  Password to encrypt the export with: ")
    if len(first) < MINIMUM_EXPORT_PASSWORD:
        message = f"The password is shorter than {MINIMUM_EXPORT_PASSWORD} characters."
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery=(
                "This file will hold a private signing key and may cross a USB stick or a share. "
                "Nothing was written."
            ),
        )
    if environment.prompt("  Type it again: ") != first:
        message = "The two passwords do not match."
        raise ConnectorError(
            message, exit_code=EXIT_USAGE, recovery="Nothing was written. Run the command again."
        )
    return first


def _run_profile_export(namespace: Any, install_root: Path, environment: Environment) -> int:
    """Write one profile to an encrypted file. Changes nothing on this computer."""
    out = environment.stdout
    out.write(f"\nExporting the {namespace.profile!r} profile.\n")
    out.write(
        "  The profile lock stops another connector command from writing while this reads. It\n"
        "  cannot stop a running agent or a scheduled job — no operating system offers that —\n"
        "  so stop those first if you want a consistent copy.\n\n"
    )
    password = _ask_new_password(environment)
    try:
        result = migration.export_profile(
            install_root=install_root,
            profile=namespace.profile,
            destination=namespace.destination,
            password=password,
            environment=environment,
            include_soul=not namespace.no_soul,
        )
    except migration.MigrationError as error:
        raise ConnectorError(str(error), exit_code=EXIT_USAGE, recovery=error.recovery) from error

    out.write(f"\n  Wrote {result.path} ({result.size} bytes)\n")
    out.write(f"  Agent id:    {result.plan.identity['agent_id']}\n")
    out.write(f"  Handle:      {result.plan.identity['handle'] or '(none recorded)'}\n")
    out.write(f"  Key fingerprint: {result.plan.identity['public_key_fingerprint']}\n")
    out.write("\n  In the archive:\n")
    for name in result.plan.included_names:
        out.write(f"    {name}\n")
    out.write("\n  Deliberately not in it:\n")
    for category in migration.EXCLUDED_CATEGORIES:
        out.write(f"    {category}\n")
    if result.plan.notes:
        out.write("\n  Notes:\n")
        for note in result.plan.notes:
            out.write(f"    {note}\n")
    out.write(f"\n  {migration.SECOND_COPY_WARNING}\n")
    out.write("\n  This profile was not changed, disconnected or removed.\n")
    return EXIT_OK


def _describe_archive(contents: Any, out: TextIO) -> None:
    """Show what an archive holds before anything is created from it."""
    identity = contents.identity
    source = contents.source
    out.write("\n  This archive carries:\n")
    out.write(f"    Profile:     {source.get('profile', '(unnamed)')}\n")
    out.write(f"    Agent id:    {identity.get('agent_id', '')}\n")
    out.write(f"    Handle:      {identity.get('handle') or '(none recorded)'}\n")
    out.write(f"    Key fingerprint: {identity.get('public_key_fingerprint', '')}\n")
    out.write(
        f"    Written by connector {source.get('connector_version', '?')} "
        f"on {source.get('system', '?')}\n"
    )
    out.write("\n  Contents:\n")
    for entry in contents.manifest.get("included", []):
        out.write(f"    {entry.get('name')} ({entry.get('size')} bytes)\n")
    out.write("\n  Not in it:\n")
    for category in contents.manifest.get("excluded", []):
        out.write(f"    {category}\n")
    notes = contents.manifest.get("notes") or []
    if notes:
        out.write("\n  Notes from the export:\n")
        for note in notes:
            out.write(f"    {note}\n")


def _run_profile_import(namespace: Any, install_root: Path, environment: Environment) -> int:
    """Create a new local profile from an encrypted export file.

    Reads, decrypts and validates first, shows what it found, and only then asks whether to create
    anything. Nothing is written before the confirmation, and an existing profile is never
    replaced.
    """
    out = environment.stdout
    source = Path(namespace.source)
    try:
        blob = source.read_bytes()
    except OSError as error:
        message = f"{source} could not be read: {error}"
        raise ConnectorError(message, exit_code=EXIT_USAGE, recovery="Check the path.") from error

    password = environment.prompt(f"  Password for {source.name}: ")
    try:
        contents = migration.read_archive(blob, password)
    except migration.MigrationError as error:
        raise ConnectorError(str(error), exit_code=EXIT_USAGE, recovery=error.recovery) from error

    _describe_archive(contents, out)
    if namespace.inspect:
        out.write("\n  --inspect: nothing was created.\n")
        return EXIT_OK

    profile = namespace.profile or str(contents.source.get("profile") or "")
    if not profile:
        message = "The archive records no profile name and none was given."
        raise ConnectorError(
            message, exit_code=EXIT_USAGE, recovery="Pass --profile with a name for it here."
        )
    out.write(f"\n  It will be created here as the {profile!r} profile.\n")
    out.write(f"  {migration.SECOND_COPY_WARNING}\n\n")
    if not _confirm_import(namespace, profile, environment):
        out.write("  Nothing was created.\n")
        return EXIT_USAGE

    try:
        result = migration.import_profile(
            install_root=install_root,
            profile=profile,
            contents=contents,
            environment=environment,
            agent_api_url=namespace.agent_api_url,
            agent_read_url=namespace.agent_read_url,
        )
    except migration.MigrationError as error:
        # What a failed import could not undo is printed here rather than buried in the
        # exception: it is a list of places on this computer the operator has to look at.
        # Paths and runtime names only, never key material or configuration content.
        if error.residue:
            environment.stderr.write("\n  Still on this computer after the failure:\n")
            for item in error.residue:
                environment.stderr.write(f"    {item}\n")
        raise ConnectorError(str(error), exit_code=EXIT_RUNTIME, recovery=error.recovery) from error

    out.write(f"\n  Created {result.root}\n")
    out.write(f"  Registered with: {', '.join(result.registered) or 'no runtime'}\n")
    for note in result.notes:
        out.write(f"  {note}\n")
    return EXIT_OK


def _confirm_import(namespace: Any, profile: str, environment: Environment) -> bool:
    """Require the profile name typed back before a key is written anywhere."""
    if namespace.confirm is not None:
        return bool(namespace.confirm == profile)
    answer = environment.ask(f"  Type {profile!r} to create it, or anything else to stop: ")
    return answer.strip() == profile


def _run_profile_command(namespace: Any, install_root: Path, environment: Environment) -> int:
    if namespace.action == "list":
        prepare_installation(install_root, environment)
        return run_profile_list(install_root, environment)
    if namespace.action == "status":
        prepare_installation(install_root, environment)
        return run_profile_status(install_root, namespace.profile, environment)
    if namespace.action == "doctor":
        prepare_installation(install_root, environment)
        return run_profile_doctor(install_root, namespace.profile, environment)
    if namespace.action == "endpoint":
        prepare_installation(install_root, environment)
        return _run_endpoint_command(namespace, install_root, environment)
    if namespace.action == "soul":
        prepare_installation(install_root, environment)
        return _run_soul_command(namespace, install_root, environment)
    if namespace.action == "export":
        prepare_installation(install_root, environment)
        return _run_profile_export(namespace, install_root, environment)
    if namespace.action == "import":
        prepare_installation(install_root, environment)
        return _run_profile_import(namespace, install_root, environment)
    if namespace.action == "disconnect":
        return run_profile_disconnect(
            install_root, namespace.profile, environment, runtime=namespace.runtime
        )
    return run_profile_remove(
        install_root,
        namespace.profile,
        environment,
        confirm=namespace.confirm,
        destroy_key=namespace.destroy_key,
        purge_runtime_profile=namespace.purge_runtime_profile,
    )


if __name__ == "__main__":  # pragma: no cover - console script entry point
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
    raise SystemExit(EXIT_USAGE)
