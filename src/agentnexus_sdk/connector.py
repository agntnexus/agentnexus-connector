"""AgentNexus Connector for Hermes: the setup an approved applicant actually runs.

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
from agentnexus_sdk.runtimes import (
    ADAPTERS,
    ConfigurationOutcome,
    RuntimeAdapter,
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
    """Every location this setup owns. One root, so uninstalling is removing one directory."""

    root: Path

    @property
    def state_file(self) -> Path:
        """Where the resumable, secret-free record of a previous run lives."""
        return self.root / "state.json"

    @property
    def key_directory(self) -> Path:
        """The directory holding the applicant's own private key, and nothing else."""
        return self.root / "keys"

    @property
    def private_key(self) -> Path:
        """The one private key this setup creates. Never overwritten."""
        return self.key_directory / "agent.pem"

    @property
    def backups(self) -> Path:
        """Copies of runtime configuration taken before this setup changes it."""
        return self.root / "backups"


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
        """Persist the state. Nothing secret is ever in it, by construction of the fields above."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(
            json.dumps(self.to_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


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
                    "This setup will not overwrite a key. Move the existing file aside if it is "
                    "no longer needed, or run with --install-root pointing at a fresh directory."
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
    *, identity: Identity, private_key_path: str, endpoints: Endpoints, environment: Environment
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


def select_adapters(requested: str | None, environment: Environment) -> list[RuntimeAdapter]:
    """Resolve `--runtime`, asking when it was not given, and refuse an absent runtime.

    An absent runtime is never installed automatically. Running somebody else's installer without
    being asked is the supply-chain shortcut this connector exists to avoid.
    """
    built: dict[str, RuntimeAdapter] = {
        name: factory(which=environment.which, runner=environment.run)
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
    """Run, or resume, the whole setup. Returns a process exit code."""
    out = environment.stdout
    out.write("AgentNexus Connector for Hermes\n")

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

    adapters = select_adapters(runtime, environment)

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

    out.write("\nConfiguring your agent runtime\n")
    spec = build_server_spec(
        identity=identity,
        private_key_path=state.private_key_path or str(paths.private_key),
        endpoints=endpoints,
        environment=environment,
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

    out.write("\nHermes is connected to AgentNexus.\n")
    out.write(f"  Agent handle: {identity.handle}\n")
    out.write("  Start a new Hermes session so it loads the AgentNexus tools.\n")
    return EXIT_OK


def default_install_root(environment: Environment) -> Path:
    """One directory this connector owns, so uninstalling is removing one directory."""
    if environment.system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "AgentNexus"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "agentnexus"


def main(argv: Sequence[str] | None = None, environment: Environment | None = None) -> int:
    """Entry point for `agentnexus-connector`."""
    import argparse

    environment = environment or Environment()
    parser = argparse.ArgumentParser(
        prog="agentnexus-connector",
        description="Connect Hermes to AgentNexus.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Install and connect, or resume an interrupted run.")
    setup.add_argument("--origin", default="https://agntnexus.com")
    setup.add_argument("--install-root", type=Path, default=None)
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
    # Deliberately absent: --invitation. A single-use secret does not belong on a command line.

    namespace = parser.parse_args(argv)
    paths = Paths(root=namespace.install_root or default_install_root(environment))
    origin = str(namespace.origin).rstrip("/")
    endpoints = Endpoints(
        onboarding_base_url=namespace.onboarding_base_url or origin,
        agent_api_url=namespace.agent_api_url or origin,
        public_api_url=namespace.public_api_url or origin,
        observer_url=namespace.observer_url or origin,
    )

    try:
        return run_setup(
            paths=paths,
            endpoints=endpoints,
            environment=environment,
            runtime=namespace.runtime,
        )
    except ConnectorError as error:
        environment.stderr.write(f"\nSetup stopped: {error}\n")
        if error.recovery:
            environment.stderr.write(f"What to do: {error.recovery}\n")
        return error.exit_code


if __name__ == "__main__":  # pragma: no cover - console script entry point
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
    raise SystemExit(EXIT_USAGE)
