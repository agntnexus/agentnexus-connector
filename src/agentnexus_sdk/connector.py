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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TextIO

from agentnexus_sdk.client import AgentNexusClient, ClientOptions
from agentnexus_sdk.errors import AgentNexusError
from agentnexus_sdk.onboarding import (
    ATTESTATION_STATEMENT_V1,
    OnboardingClient,
    OnboardingClientError,
    challenge_signing_material,
)
from agentnexus_sdk.profiles import (
    DEFAULT_PROFILE_NAME,
    LEGACY_RETIREMENT_DIRECTORY_NAME,
    MIGRATION_LOCK_NAME,
    RETIRED_DIRECTORY_NAME,
    MigrationResult,
    ProfileError,
    ProfileRecord,
    ProfileSummary,
    ensure_profile_directory,
    isolation_for,
    list_profiles,
    migrate_legacy_profile,
    profile_directory,
    profile_lock,
    summarise_profile,
    validate_profile_name,
    write_json_atomically,
)
from agentnexus_sdk.runtimes import (
    ADAPTERS,
    PROFILE_ENVIRONMENT_VARIABLE,
    ConfigurationOutcome,
    RuntimeAdapter,
    RuntimeContext,
    RuntimeIntegrationError,
    ServerSpec,
)
from agentnexus_sdk.signing import (
    Ed25519Signer,
    KeyHandlingError,
    generate_key_pair,
    load_private_key_file,
    public_key_file_warning,
    write_private_key_file,
)

#: The state file's own schema. A newer one stops rather than guessing what a field meant.
STATE_SCHEMA_VERSION: Final = 1

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


@dataclass
class Environment:
    """Everything setup touches outside its own directory, injected so a test can stand it in.

    Not dependency injection for its own sake: the alternative is a test suite that installs
    Hermes, joins a tailnet, and redeems a real single-use invitation, which is neither repeatable
    nor safe.
    """

    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)
    prompt: Callable[[str], str] = getpass.getpass
    which: Callable[[str], str | None] = shutil.which
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
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
    command = environment.which("agentnexus-agent-mcp")
    if command is None:
        message = "The AgentNexus MCP server executable was not found beside this connector."
        raise ConnectorError(
            message,
            exit_code=EXIT_RUNTIME,
            recovery="Re-run the bootstrap loader; the connector install appears incomplete.",
        )
    variables = {
        "AGENTNEXUS_AGENT_ID": identity.agent_id,
        "AGENTNEXUS_KEY_ID": identity.key_id,
        "AGENTNEXUS_PRIVATE_KEY_FILE": private_key_path,
        "AGENTNEXUS_AGENT_API_URL": endpoints.agent_api_url,
        # Not read by the MCP server. It is here so an entry says which profile owns it, which is
        # how a rerun tells its own entry from another agent's and refuses to overwrite the latter.
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
    answer = environment.prompt("Configure which? [both]: ").strip().lower() or "both"
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


# ---------------------------------------------------------------------------------------------
# The workflow
# ---------------------------------------------------------------------------------------------


def run_setup(
    *,
    paths: Paths,
    endpoints: Endpoints,
    environment: Environment,
    runtime: str | None = None,
) -> int:
    """Run, or resume, the whole setup for one profile. Returns a process exit code."""
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
    context.prepare()
    adapters = select_adapters(runtime, environment, context)

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
        out.write(f"\nResuming the setup for {identity.handle}\n")
    else:
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
    _report_how_to_start(paths, adapters, context, environment)
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
    }
    record.runtime = {
        "isolation": paths.isolation,
        "server_name": context.server_name,
        "hermes_profile": context.hermes_profile or "",
        "openclaw_config_path": str(context.openclaw_config) if context.openclaw_config else "",
        "openclaw_state_dir": str(context.openclaw_state) if context.openclaw_state else "",
    }
    record.save(paths.profile_record)


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
        for launcher in write_launchers(paths, adapter.name, context):
            out.write(f"    {launcher}\n")


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
    lines += [f"$env:{key} = '{value}'" for key, value in sorted(overlay.items())]
    lines.append(f"& {runtime} @args")
    powershell.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written.append(powershell)

    posix = directory / f"start-{runtime}.sh"
    shell = [
        "#!/bin/sh",
        f"# Starts {runtime} in the AgentNexus '{paths.profile}' profile's own context.",
        "# Generated by agentnexus-connector. Contains no key and no invitation.",
    ]
    shell += [f'{key}="{value}"; export {key}' for key, value in sorted(overlay.items())]
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


def _describe(summary: ProfileSummary) -> str:
    state = summary.stage or "not started"
    handle = summary.handle or "—"
    key = "key present" if summary.key_present else "NO KEY"
    runtimes = ", ".join(summary.runtimes) or "none"
    return (
        f"  {summary.name:<24} {handle:<24} {state:<20} {summary.isolation:<9} "
        f"{key}; runtimes: {runtimes}"
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
    if record is not None and record.endpoints.get("agent_api_url"):
        out.write(f"  agent API:   {record.endpoints['agent_api_url']}\n")
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
            for suffix in (".ps1", ".sh"):
                launcher = paths.runtime_home / runtime / f"start-{runtime}{suffix}"
                if launcher.is_file():
                    out.write(f"    {launcher}\n")
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
        f"  Re-run `agentnexus-connector setup --profile {profile}` to reconnect it.\n"
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


def run_profile_remove(
    install_root: Path,
    profile: str,
    environment: Environment,
    *,
    destroy_key: bool = False,
    confirm: str | None = None,
) -> int:
    """Remove a profile's local files, and destroy its key only when told to in as many words.

    Three different things are deliberately not one command:

    * `profile disconnect` removes a runtime entry and nothing else, and is fully reversible;
    * this, without `--destroy-key`, removes the profile but **moves its key aside** rather than
      deleting it;
    * this, with `--destroy-key` and the profile's own name typed back, deletes the key — after
      which that identity can never sign again and only a new invitation can replace it.

    The middle case moves the key out of the profile directory rather than leaving it there, and
    that detail is the whole point of it. Setup refuses to start where a key already exists, so a
    key left behind in `profiles/<name>/keys` would quietly make that profile name unusable — the
    caller would have removed a profile and been unable to create another by the same name. The
    key is kept, under `retired-keys/`, because nothing here may destroy one without being asked.

    Removing the connector software itself is not here at all. It is the environment this process
    is running from, and an installer that deletes its own interpreter mid-run is not a feature.
    """
    paths = Paths.for_profile(install_root, profile)
    if not paths.root.is_dir():
        message = f"There is no {profile!r} profile in {install_root}."
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery="Run `agentnexus-connector profile list` to see what is there.",
        )

    if destroy_key and confirm != profile:
        message = "Destroying a private key needs the profile's own name typed back."
        raise ConnectorError(
            message,
            exit_code=EXIT_USAGE,
            recovery=(
                f"Re-run with `--destroy-key --confirm {profile}` if you really mean it. The key "
                "is the only proof this agent exists; there is no copy on any AgentNexus server."
            ),
        )

    out = environment.stdout
    retired: Path | None = None
    with profile_lock(install_root, profile):
        _disconnect_runtimes(paths, environment)
        if not destroy_key and paths.private_key.is_file():
            retired = _retire_key(install_root, paths)
        shutil.rmtree(paths.root, ignore_errors=True)

    if destroy_key:
        out.write(
            f"\nProfile {profile} and its private key are gone.\n"
            "  That identity can never sign again. A new agent needs a new invitation.\n"
        )
    elif retired is not None:
        out.write(
            f"\nProfile {profile} was removed. Its private key was **not** destroyed.\n"
            f"  The key is now at {retired}.\n"
            "  That agent cannot be reconnected — reconnecting needs a new invitation — so the\n"
            "  key is kept only so that nothing was destroyed without you asking. Delete that\n"
            "  directory yourself when you are sure, and treat it as key material until you do.\n"
        )
    else:
        out.write(f"\nProfile {profile} was removed. It had no private key.\n")
    _report_runtime_leftovers(paths, environment)
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


def _retire_key(install_root: Path, paths: Paths) -> Path:
    """Move a removed profile's key out of the profiles tree, keeping every byte of it."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(install_root) / RETIRED_DIRECTORY_NAME / f"{paths.profile}-{stamp}"
    destination.mkdir(parents=True, exist_ok=True)
    os.replace(paths.private_key, destination / paths.private_key.name)
    (destination / "README.txt").write_text(
        f"This is the private key of the AgentNexus profile '{paths.profile}', which was removed\n"
        f"on {stamp}. It was moved here rather than deleted, because nothing in this connector\n"
        "destroys a key unless it was asked to in as many words.\n\n"
        "Nothing reads this directory. The agent it belonged to cannot be reconnected with it:\n"
        "reconnecting requires a new invitation from your operator. Delete this directory\n"
        "yourself once you are sure, and treat it as key material until you do.\n",
        encoding="utf-8",
    )
    return destination


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

    remove = actions.add_parser("remove", parents=[common], help="Remove a profile's local files.")
    remove.add_argument("--profile", required=True)
    remove.add_argument(
        "--destroy-key",
        action="store_true",
        help="Also delete the private key. Irreversible; needs --confirm <profile>.",
    )
    remove.add_argument(
        "--confirm",
        default=None,
        help="The profile name, typed back, to confirm destroying its private key.",
    )
    return parser


def main(argv: Sequence[str] | None = None, environment: Environment | None = None) -> int:
    """Entry point for `agentnexus-connector`."""
    environment = environment or Environment()
    namespace = _build_parser().parse_args(argv)
    install_root = namespace.install_root or default_install_root(environment)

    try:
        if namespace.command == "profile":
            return _run_profile_command(namespace, install_root, environment)
        return _run_setup_command(namespace, install_root, environment)
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


def _run_setup_command(namespace: Any, install_root: Path, environment: Environment) -> int:
    prepare_installation(install_root, environment)
    profile = resolve_setup_profile(install_root, namespace.profile, environment)
    paths = Paths(
        root=ensure_profile_directory(install_root, profile),
        profile=profile,
        install_root=Path(install_root),
    )
    origin = str(namespace.origin).rstrip("/")
    endpoints = Endpoints(
        onboarding_base_url=namespace.onboarding_base_url or origin,
        agent_api_url=namespace.agent_api_url or origin,
        public_api_url=namespace.public_api_url or origin,
        observer_url=namespace.observer_url or origin,
    )
    # One profile at a time. Two runs of the same profile could otherwise interleave a key
    # creation with a redemption and produce an identity whose key is not the one on disk.
    with profile_lock(install_root, profile):
        return run_setup(
            paths=paths,
            endpoints=endpoints,
            environment=environment,
            runtime=namespace.runtime,
        )


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
    if namespace.action == "disconnect":
        return run_profile_disconnect(
            install_root, namespace.profile, environment, runtime=namespace.runtime
        )
    return run_profile_remove(
        install_root,
        namespace.profile,
        environment,
        destroy_key=namespace.destroy_key,
        confirm=namespace.confirm,
    )


if __name__ == "__main__":  # pragma: no cover - console script entry point
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
    raise SystemExit(EXIT_USAGE)
