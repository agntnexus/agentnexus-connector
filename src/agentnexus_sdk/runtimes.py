"""Agent-runtime adapters: the only part of the connector that knows about Hermes or OpenClaw.

Everything protocol-shaped — key handling, redemption, signing, conformance, catch-up, the Tailnet
preflight — lives in `connector.py` and knows nothing about either runtime. An adapter's whole job
is to register one already-built stdio MCP server with one runtime, idempotently, and to say
whether it worked. Adding a third runtime means adding an adapter, not a second client.

**What every adapter must do, and why each rule is here.**

* Use the runtime's own CLI when the installed version supports the operation. A CLI validates the
  entry, discovers tools, and writes the file the way that runtime expects; editing an undocumented
  internal file skips all of that and breaks on the next release.
* Inspect before mutating, so a rerun is a no-op and a *different* existing entry is a conflict the
  applicant is told about rather than something silently replaced.
* Back up before writing, and restore on failure, so a half-written configuration never survives.
* Carry only identifiers, addresses, and the private-key **path**. Never the key, never the
  invitation. Both runtimes filter their environment before spawning a stdio server, which is why
  those variables are declared rather than inherited.
* Configure exactly one profile, inside that profile's own runtime context, and prove afterwards
  that the runtime honoured it. An adapter holds a `RuntimeContext` and has no way to name another
  profile's entry or reach another profile's context.

**Why a profile is a runtime *context* and not just a second entry name.** Two MCP entries in one
runtime context are two tools offered to one agent, and that agent can then sign as either
identity. Renaming them changes nothing. Each runtime's supported answer was read off a real
installation, and the two are not the same shape:

* **Hermes has first-class profiles** — `hermes profile create/list/alias` and a global `-p <name>`
  selector, with a `config.yaml`, `.env`, `SOUL.md`, and skills of its own per profile. That is the
  mechanism this adapter uses. `HERMES_HOME` is *not*: it relocates the whole installation, so a
  per-profile home would hand each agent a Hermes with no credentials and no skills.
* **OpenClaw relocates its registry and state** with `OPENCLAW_CONFIG_PATH` and
  `OPENCLAW_STATE_DIR` — the surface a real OpenClaw 2026.8.1 was driven under during I-017.

`verify_isolation` checks the runtime actually applied it: the entry is present in this profile's
own configuration and absent from the shared one. If a runtime ever answered somewhere else, the
adapter rolls back and refuses rather than leaving two identities in one context.

**Verification status, stated plainly.** Both adapters have now driven a real installation end to
end — Hermes v0.20.6 and OpenClaw 2026.8.1 — from a clean wheel install rather than a repository
checkout, each runtime held inside its own official isolation so no working configuration was
touched. Hermes contributed two failures of its own that only a real run could produce: `hermes mcp
add` asks "Enable all 10 tools?" with no flag to skip it and cancels on EOF, saving nothing while
appearing to succeed; and its configuration file is per profile and moves with `HERMES_HOME`, so a
guessed path inspected and backed up a file that was never the one being written. The profile
surface used here — `hermes profile list/create`, `hermes -p <name>` before the subcommand, and
`hermes -p <name> config path` reporting `<HERMES_HOME>/profiles/<name>/config.yaml` while
`-p default` reports the root file — was read off that same real v0.20.6 installation, including
its non-zero exit for an unknown profile.

The two-profile *write* path has since been driven against that same real Hermes, inside a
throwaway `HERMES_HOME`: two profiles created, an entry registered in each, neither profile's
configuration containing the other's entry and the root configuration containing none, an
idempotent rerun, a same-identity update with a real backup, and a refusal of an entry belonging
to a different agent id. That run is also what showed `hermes profile create` writing a wrapper
into `~/.local/bin`, which is why `--no-alias` is passed.

The OpenClaw adapter was written first against a *described* contract and
then corrected against a real OpenClaw 2026.8.1, installed from its official npm distribution and
driven under OpenClaw's own profile isolation. That run found three defects a stub could never
have surfaced, because a stub answers to whatever it was told to answer to:

* `mcp add` has no `--transport stdio`. Its `--transport` selects an *HTTP* transport, and a stdio
  server is expressed by `--command` alone. The stubbed call was rejected outright.
* `--env` takes exactly one `KEY=VALUE` and is repeated. Passing several after one flag makes the
  extras positional, and OpenClaw refuses the command.
* There is no `mcp remove`. Removal is `mcp unset`, so the rollback path was a silent no-op and the
  recovery text named a command that does not exist.

Tool visibility is read from `mcp probe --json`, which lists discovered capabilities;
`doctor --probe` reports health and does not enumerate tools.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from agentnexus_sdk.profiles import DEFAULT_PROFILE_NAME

#: The MCP server name the `default` profile uses, and the only name installations made before
#: named profiles ever had. Idempotency for that profile is still defined by this exact name.
MCP_SERVER_NAME: Final = "agentnexus"

#: Tools an AgentNexus MCP server is expected to expose. Checked after configuration so a runtime
#: that accepted the entry but discovered nothing is a failure rather than a silent success.
EXPECTED_TOOLS: Final = frozenset({"catch_up", "conformance"})

#: The environment variable each entry carries so it names the profile it belongs to. It is how a
#: rerun tells its own entry from another profile's, and it is not read by the MCP server.
PROFILE_ENVIRONMENT_VARIABLE: Final = "AGENTNEXUS_PROFILE"

#: The variable whose value decides which identity an entry signs with. Two entries that agree on
#: it are the same agent; two that disagree are two agents, and one may never overwrite the other.
IDENTITY_ENVIRONMENT_VARIABLE: Final = "AGENTNEXUS_AGENT_ID"

#: What Hermes calls a profile's instruction document. Read off a real v0.20.6, which reports
#: `SOUL.md:` in `hermes profile show` and names the same file in its `--ignore-rules` help.
HERMES_SOUL_FILENAME: Final = "SOUL.md"


def mcp_server_name(profile: str) -> str:
    """Return the MCP entry name one profile owns. Deterministic, and unique per profile.

    `default` keeps the bare `agentnexus`, because every installation made before this slice
    registered exactly that and renaming it on upgrade would disconnect a working agent.
    """
    if profile == DEFAULT_PROFILE_NAME:
        return MCP_SERVER_NAME
    return f"{MCP_SERVER_NAME}-{profile}"


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Which context inside one runtime installation a profile is configured in.

    This is the part of the multi-profile design that has to be real rather than cosmetic. Two MCP
    entries in one runtime context are two tools offered to **one** agent, so that agent can sign
    as either identity; renaming the entries changes nothing about it.

    **The two runtimes answer this differently, and each answer is its own.** The mapping was read
    off the installations, not chosen for symmetry:

    * **Hermes has first-class profiles.** `hermes profile create/list/use/alias`, a global
      `-p/--profile` selector accepted before the subcommand, and one `config.yaml`, `.env`,
      `SOUL.md`, and skill set per profile under `<HERMES_HOME>/profiles/<name>`. `hermes -p X
      config path` reports that profile's file, and an unknown profile exits non-zero with the
      command to create it. That *is* the soul-and-workspace isolation this requirement asks for,
      so the adapter uses it rather than relocating `HERMES_HOME`. Relocating the home would give
      a profile a whole empty Hermes installation — no provider credentials, no skills — which is
      isolation by amputation and not what the runtime intends.
    * **OpenClaw relocates its registry and state** with `OPENCLAW_CONFIG_PATH` and
      `OPENCLAW_STATE_DIR`, which is how a real OpenClaw 2026.8.1 was driven during I-017. No
      OpenClaw is installed on the machine this slice was written on, so that mapping rests on
      that recorded evidence and on stub tests; it has not been re-verified here.

    Either way the adapter proves afterwards that the runtime honoured it, and fails closed if it
    did not, rather than leaving two identities in one context.
    """

    profile: str = DEFAULT_PROFILE_NAME
    server_name: str = MCP_SERVER_NAME
    isolated: bool = False
    #: The Hermes profile this AgentNexus profile maps to, passed as `-p`. `None` means the
    #: runtime's own active profile, which is what `default` uses.
    hermes_profile: str | None = None
    openclaw_config: Path | None = None
    openclaw_state: Path | None = None
    overlay: dict[str, str] = field(default_factory=dict)

    @classmethod
    def shared(cls, profile: str = DEFAULT_PROFILE_NAME) -> RuntimeContext:
        """Build a context using the runtime's own active context, for `default` alone."""
        return cls(profile=profile, server_name=mcp_server_name(profile), isolated=False)

    @classmethod
    def isolated_under(cls, profile: str, home: Path) -> RuntimeContext:
        """Build a context this profile alone owns, in the form each runtime supports.

        `home` is the AgentNexus profile's own directory and is used for OpenClaw only: Hermes
        keeps its profiles inside its own installation, where its skills and credentials are.
        """
        openclaw_home = Path(home) / "openclaw"
        return cls(
            profile=profile,
            server_name=mcp_server_name(profile),
            isolated=True,
            hermes_profile=profile,
            openclaw_config=openclaw_home / "openclaw.json",
            openclaw_state=openclaw_home / "state",
            overlay={
                "OPENCLAW_CONFIG_PATH": str(openclaw_home / "openclaw.json"),
                "OPENCLAW_STATE_DIR": str(openclaw_home / "state"),
            },
        )

    @property
    def hermes_arguments(self) -> list[str]:
        """The profile selector every `hermes` invocation for this profile carries."""
        return [] if self.hermes_profile is None else ["-p", self.hermes_profile]

    def prepare(self) -> None:
        """Create the directories this context points at, before a runtime is asked to use one."""
        if not self.isolated:
            return
        if self.openclaw_state is not None:
            self.openclaw_state.mkdir(parents=True, exist_ok=True)
        if self.openclaw_config is not None:
            self.openclaw_config.parent.mkdir(parents=True, exist_ok=True)

    def process_environment(self) -> dict[str, str] | None:
        """Return the environment a runtime CLI is invoked with, or `None` to inherit it.

        Merged onto the caller's own environment rather than replacing it: a runtime still needs
        `PATH`, a home directory, and whatever its own installation depends on.
        """
        if not self.overlay:
            return None
        return {**os.environ, **self.overlay}


class RuntimeIntegrationError(Exception):
    """A runtime-integration failure carrying an actionable recovery step."""

    def __init__(self, message: str, *, recovery: str | None = None) -> None:
        """Build the adapter, with the process seams a test can stand in."""
        super().__init__(message)
        self.recovery = recovery


@dataclass(frozen=True, slots=True)
class RuntimeDetection:
    """Whether a runtime is installed, which version, and how far it has been proven."""

    installed: bool
    version: str | None = None
    executable: str | None = None
    #: Honest provenance for the adapter itself, printed during setup.
    verified_against: str = "not verified against a real installation"


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """Everything a runtime needs to spawn the AgentNexus MCP server. No secret is in here."""

    command: str
    environment: dict[str, str]

    def as_env_arguments(self) -> list[str]:
        """`KEY=VALUE` pairs, in a stable order so two runs produce the same command."""
        return [f"{key}={value}" for key, value in sorted(self.environment.items())]

    def as_repeated_env_flags(self, flag: str = "--env") -> list[str]:
        """Return the same pairs, each behind its own flag.

        Hermes accepts one `--env` followed by every pair; OpenClaw's takes exactly one pair and is
        repeated, and treats the extras as positional arguments it then refuses. Two CLIs, two
        spellings of the same data, so the difference is stated here rather than guessed at twice.
        """
        return [
            item
            for key, value in sorted(self.environment.items())
            for item in (flag, f"{key}={value}")
        ]


@dataclass(frozen=True, slots=True)
class SoulLocation:
    """Where one profile's instruction document lives, and how that was established.

    `directory` is what the runtime itself reported, never a guess at a home directory. The
    distinction matters: `HERMES_HOME` moves the whole installation and a profile moves inside it,
    so a path assembled from environment variables would be right until the day it silently was
    not — and the thing being overwritten is a document somebody wrote.
    """

    runtime: str
    profile: str
    directory: Path
    filename: str

    @property
    def path(self) -> Path:
        """The soul file itself."""
        return self.directory / self.filename


@dataclass(frozen=True, slots=True)
class ConfigurationOutcome:
    """What an adapter did, so the caller can report and roll back precisely."""

    changed: bool
    backup: Path | None
    detail: str


class RuntimeAdapter(Protocol):
    """One agent runtime. Implementations hold no protocol logic of their own."""

    name: str
    display_name: str

    def detect(self) -> RuntimeDetection:
        """Report whether this runtime is installed, and which version."""
        ...

    def existing_entry(self) -> dict[str, Any] | None:
        """Return the AgentNexus MCP entry this runtime already has, if any."""
        ...

    def configure(self, spec: ServerSpec, *, backup_directory: Path) -> ConfigurationOutcome:
        """Register or update the entry, backing up whatever it replaces."""
        ...

    def rollback(self, backup: Path | None) -> None:
        """Undo what this run changed."""
        ...

    def remove_entry(self) -> str | None:
        """Remove this profile's entry, returning what stopped it if anything did."""
        ...

    def verify(self) -> list[str]:
        """Ask the runtime to confirm the server, returning what it reported."""
        ...

    def verify_isolation(self) -> list[str]:
        """Prove this profile's entry lives in its own runtime context, or refuse."""
        ...

    def start_hint(self) -> list[str]:
        """Say how the applicant starts this runtime as this profile."""
        ...

    def soul_location(self) -> SoulLocation:
        """Where this profile's instruction document lives, as the runtime itself reports it."""
        ...


#: What Hermes' own confirmation prompts take as their documented default.
#:
#: `mcp remove` asks `Remove server '<name>'? [Y/n]`, and `mcp add` asks `Enable all N tools?
#: [Y/n/select]`. Neither has a flag to skip it, and both read from stdin. An empty line accepts
#: the default in each case, which is what a non-interactive run wants: remove the entry we are
#: replacing, and enable the tools of the server we just registered.
CONFIRM_DEFAULT: Final = "\n"


def _run(
    runner: Any,
    arguments: list[str],
    *,
    timeout: float = 120.0,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a CLI with a fixed argument list. Never a shell, never an interpolated string.

    `stdin` exists for one real case: Hermes v0.20.6 asks "Enable all 10 tools? [Y/n/select]" after
    it discovers a server, and offers no flag to skip it. With nothing on stdin it reads EOF and
    cancels, so a non-interactive run silently saved nothing. See `HermesAdapter.configure`.

    `env` carries a profile's isolated runtime state. It is the caller's whole environment with the
    overlay applied, never a bare overlay: a runtime invoked without `PATH` fails in ways that look
    like the integration rather than the invocation.
    """
    extra: dict[str, Any] = {"input": stdin} if stdin is not None else {}
    if env is not None:
        extra["env"] = env
    try:
        completed: subprocess.CompletedProcess[str] = runner(
            arguments,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            **extra,
        )
    except subprocess.TimeoutExpired as error:
        # A hung runtime is a normal failure, not a crash. Letting `TimeoutExpired` escape put a
        # Python traceback in front of an applicant who then had no idea whether their identity
        # had survived — and on Windows the trace was the last thing on screen before the window
        # closed. The command is named because it is the useful part; nothing else about it is.
        name = Path(arguments[0]).name if arguments else "the runtime"
        message = (
            f"{name} did not respond within {timeout:.0f} seconds and was stopped. "
            "It may be waiting for an answer to a prompt this connector cannot see."
        )
        raise RuntimeIntegrationError(
            message,
            recovery=(
                "Run the same command yourself in a terminal to see what it is asking, then "
                "re-run setup for this profile. Nothing was changed."
            ),
        ) from error
    return completed


def _timestamped(directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"{stem}-{stamp}"


# ---------------------------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------------------------


class HermesAdapter:
    """Hermes Agent, through its own `hermes mcp` CLI and its own profiles.

    Verified against a real Hermes v0.20.6 installation: `hermes mcp add` takes `--command`,
    `--env KEY=VALUE ...` and `--connect-timeout`, and `hermes mcp list` reports the configured
    servers. The CLI is used rather than the YAML file because it validates the entry and performs
    tool discovery as it writes.

    **Profiles are Hermes' own, read off that same installation.** `hermes profile list` enumerates
    them, `hermes profile create <name>` makes one (and writes a wrapper script for it unless told
    not to), and a global `-p <name>` before the subcommand selects one for a single invocation —
    which is exactly what Hermes' own generated aliases do. Each profile owns a `config.yaml`, a
    `.env`, a `SOUL.md`, and its own skills under `<HERMES_HOME>/profiles/<name>`; `-p default`
    means the installation's root `config.yaml`. An unknown profile exits non-zero and names the
    command to create it.

    That surface is why this adapter selects a profile rather than relocating `HERMES_HOME`.
    `HERMES_HOME` moves the whole installation — every profile, the provider credentials, the
    skills — so a per-profile home would hand each agent an empty Hermes. Selecting the profile
    isolates exactly what has to be isolated and leaves the applicant's own Hermes intact.
    """

    name = "hermes"
    display_name = "Hermes"

    def __init__(
        self,
        *,
        which: Any = shutil.which,
        runner: Any = subprocess.run,
        config_path: Path | None = None,
        shared_config_path: Path | None = None,
        context: RuntimeContext | None = None,
    ) -> None:
        """Build the adapter, with the process seams a test can stand in."""
        self._which = which
        self._runner = runner
        self._config_path = config_path
        #: Where Hermes keeps the configuration when no profile home is imposed. Only the
        #: isolation proof reads it, and only to confirm this profile's entry is *not* there.
        self._shared_config_path = shared_config_path
        self._context = context or RuntimeContext.shared()
        self._server_name = self._context.server_name

    @property
    def context(self) -> RuntimeContext:
        """The Hermes profile this adapter configures."""
        return self._context

    def _environment(self) -> dict[str, str] | None:
        """Return the environment every `hermes` invocation here runs with."""
        return self._context.process_environment()

    def _command(self, executable: str, *arguments: str) -> list[str]:
        """Build one `hermes` invocation with this profile's selector already in place.

        Every call goes through here, so there is no path by which a command reaches Hermes
        without naming the profile it is meant for.
        """
        return [executable, *self._context.hermes_arguments, *arguments]

    def detect(self) -> RuntimeDetection:
        """Report whether this runtime is installed, and which version."""
        executable = self._which("hermes")
        if executable is None:
            return RuntimeDetection(installed=False)
        completed = _run(self._runner, [executable, "--version"], timeout=60.0)
        version = None
        if completed.returncode == 0:
            match = re.search(r"v?(\d+\.\d+\.\d+)", completed.stdout or "")
            version = match.group(1) if match else (completed.stdout or "").strip()[:64]
        return RuntimeDetection(
            installed=True,
            version=version,
            executable=executable,
            verified_against="a real Hermes v0.20.6 installation",
        )

    def _config(self, *, shared: bool = False) -> Path:
        """Where Hermes keeps the file this adapter inspects, backs up, and checks after writing.

        Asked of Hermes rather than guessed, and asked *for this profile*: `hermes -p X config
        path` reports the file `hermes -p X mcp add` writes. A guess inspects one file while the
        CLI writes another — which is how a real run here ended in "Hermes accepted the entry but
        the configuration does not contain it", and, worse, how a backup could be taken of a file
        that was never the one at risk.

        `shared=True` asks the same question with no profile selector, which the isolation proof
        uses to confirm this profile's entry is *not* in the installation's own configuration.
        """
        if shared and self._shared_config_path is not None:
            return self._shared_config_path
        if not shared and self._config_path is not None:
            return self._config_path

        executable = self._which("hermes")
        if executable is not None:
            arguments = (
                [executable, "config", "path"]
                if shared
                else self._command(executable, "config", "path")
            )
            completed = _run(self._runner, arguments, env=self._environment())
            reported = (completed.stdout or "").strip().splitlines()
            if completed.returncode == 0 and reported:
                candidate = Path(reported[-1].strip())
                if candidate.name:
                    return candidate

        # Older Hermes releases without `config path`: fall back to its documented locations.
        home = os.environ.get("HERMES_HOME")
        if home:
            return Path(home) / "config.yaml"
        base = os.environ.get("LOCALAPPDATA")
        if base:
            candidate = Path(base) / "hermes" / "config.yaml"
            if candidate.parent.exists():
                return candidate
        return Path.home() / ".hermes" / "config.yaml"

    def _document(self) -> dict[str, Any]:
        return self._document_at(self._config())

    def _document_at(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            import yaml
        except ModuleNotFoundError as error:  # pragma: no cover - a broken installation
            # This surfaced on a clean wheel install, where a bare ModuleNotFoundError traceback
            # came out of the middle of setup. `hermes mcp list` prints a table with no machine
            # form, so inspecting the file is the only way to tell an existing entry apart from
            # ours, and refusing to guess is the whole point of inspecting first.
            message = "Reading the Hermes configuration needs PyYAML, which is not installed."
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "Re-run the bootstrap loader; the connector install appears incomplete. "
                    "Nothing was changed."
                ),
            ) from error

        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as error:
            message = f"The Hermes configuration at {path} could not be parsed: {error}"
            raise RuntimeIntegrationError(
                message,
                recovery="Fix or move that file, then re-run setup. Nothing was changed.",
            ) from error
        if not isinstance(document, dict):
            message = f"The Hermes configuration at {path} is not a mapping."
            raise RuntimeIntegrationError(message)
        return document

    def existing_entry(self) -> dict[str, Any] | None:
        """Return this profile's AgentNexus MCP entry, if the runtime already has it."""
        return self._entry_in(self._document(), self._server_name)

    def _entry_in(self, document: dict[str, Any], name: str) -> dict[str, Any] | None:
        servers = document.get("mcp_servers")
        if not isinstance(servers, dict):
            return None
        entry = servers.get(name)
        return entry if isinstance(entry, dict) else None

    def configure(self, spec: ServerSpec, *, backup_directory: Path) -> ConfigurationOutcome:
        """Register or update the entry, backing up whatever it replaces."""
        executable = self._which("hermes")
        if executable is None:
            message = "Hermes disappeared from PATH between preflight and configuration."
            raise RuntimeIntegrationError(message)

        self._context.prepare()
        self._ensure_profile(executable)
        self._require_isolated_profile()

        existing = self.existing_entry()
        if existing is not None and _matches(existing, spec):
            return ConfigurationOutcome(
                changed=False, backup=None, detail="already configured; left unchanged"
            )
        if existing is not None and not _is_same_identity(existing, spec):
            # Fail closed. An entry under this name that signs as a different agent belongs to
            # another profile or another person's setup, and replacing it would retire that agent
            # silently — the one outcome a second applicant on the same machine must never cause.
            raise _conflict(self._server_name, existing, runtime="Hermes")

        backup = None
        config = self._config()
        if config.is_file():
            backup = _timestamped(backup_directory, f"hermes-config-{self._context.profile}")
            backup = backup.with_suffix(".yaml")
            shutil.copy2(config, backup)

        # Everything from here on can leave the configuration part-changed, so every failure —
        # including a runtime that hangs and is stopped — restores the backup before it is
        # reported. Removing our old entry and failing to add the new one would otherwise leave
        # the profile with no AgentNexus server at all.
        try:
            return self._replace_entry(executable, spec, existing=existing, backup=backup)
        except RuntimeIntegrationError:
            self.rollback(backup)
            raise

    def _replace_entry(
        self,
        executable: str,
        spec: ServerSpec,
        *,
        existing: dict[str, Any] | None,
        backup: Path | None,
    ) -> ConfigurationOutcome:
        """Remove any previous entry and register this one. Caller restores the backup on error."""
        if existing is not None:
            # Replacing our own earlier entry rather than adding beside it: `hermes mcp add`
            # refuses a duplicate name, and two AgentNexus servers in one profile is exactly
            # the duplication to avoid.
            removal = _run(
                self._runner,
                self._command(executable, "mcp", "remove", self._server_name),
                # Hermes v0.20.6 asks `Remove server '<name>'? [Y/n]` and offers no flag to skip
                # it. With captured output the prompt is invisible, and with no stdin the child
                # waits for an answer that never comes until the timeout. A bare newline takes the
                # documented default, which is the answer we want.
                stdin=CONFIRM_DEFAULT,
                env=self._environment(),
            )
            # Verified rather than assumed: `mcp add` refuses a duplicate name, so continuing
            # after a removal that silently failed would fail confusingly one step later.
            if removal.returncode != 0 or self.existing_entry() is not None:
                detail = (removal.stderr or removal.stdout or "").strip()[-200:]
                message = f"The previous `{self._server_name}` entry could not be removed: {detail}"
                raise RuntimeIntegrationError(
                    message,
                    recovery=(
                        f"Run `hermes mcp remove {self._server_name}` yourself, then re-run "
                        "setup for this profile."
                    ),
                )

        # Hermes v0.20.6 discovers the server's tools and then asks, with no flag to skip it:
        #   "Enable all 10 tools? [Y/n/select]"  — and, if the probe failed instead,
        #   "Save config anyway (you can test later)? [y/N]"
        # A bare newline takes each documented default, which is the answer we want both times:
        # enable the tools of the server we just registered, and do not save one that would not
        # start. Answering with a literal "y" would save a broken entry. Nothing is trusted to the
        # answer either way: the configuration is re-read below and rolled back if our entry is
        # not in it.
        completed = _run(
            self._runner,
            self._command(
                executable,
                "mcp",
                "add",
                self._server_name,
                "--command",
                spec.command,
                "--connect-timeout",
                "60",
                "--env",
                *spec.as_env_arguments(),
            ),
            timeout=300.0,
            stdin="\n",
            env=self._environment(),
        )
        if completed.returncode != 0:
            self.rollback(backup)
            message = f"`hermes mcp add` failed: {(completed.stderr or completed.stdout)[-400:]}"
            raise RuntimeIntegrationError(
                message,
                recovery="Your previous Hermes configuration was restored. Nothing else changed.",
            )

        if not _matches(self.existing_entry() or {}, spec):
            self.rollback(backup)
            message = "Hermes accepted the entry but the configuration does not contain it."
            raise RuntimeIntegrationError(message, recovery="Your configuration was restored.")

        return ConfigurationOutcome(
            changed=True, backup=backup, detail=f"registered with {executable}"
        )

    def existing_profiles(self) -> set[str]:
        """Return the Hermes profiles this installation already has, by name."""
        executable = self._which("hermes")
        if executable is None:
            return set()
        completed = _run(self._runner, [executable, "profile", "list"])
        if completed.returncode != 0:
            message = "`hermes profile list` failed, so this connector cannot tell which Hermes "
            raise RuntimeIntegrationError(
                message + "profiles exist.",
                recovery="Run `hermes profile list` yourself. Nothing was changed.",
            )
        # A rendered table, not a machine format: read the first column of each row and ignore
        # the header, the rules, and the marker Hermes puts beside the active profile.
        names: set[str] = set()
        for line in (completed.stdout or "").splitlines():
            stripped = line.strip().lstrip("◆*>").strip()
            if not stripped or stripped.startswith(("─", "-", "=")):
                continue
            first = stripped.split()[0]
            if first.lower() in {"profile", "name"}:
                continue
            names.add(first)
        return names

    def _ensure_profile(self, executable: str) -> None:
        """Create this profile in Hermes if it does not exist yet, using Hermes' own command.

        Created rather than assumed, because `hermes -p <unknown>` exits non-zero: without this a
        named profile's very first setup would fail with Hermes' "does not exist" message and no
        obvious next step.

        Two flags, and the reasoning for each. `--no-skills` is deliberately *not* passed: a new
        agent with no skills is not a working agent. `--no-alias` *is*, because Hermes' wrapper
        script lands in `~/.local/bin`, outside the one directory this connector owns — verified
        on a real installation, which wrote `~/.local/bin/agent3.bat` during this slice's own
        acceptance run. Creating files outside its own root as a side effect is not something an
        installer should do unasked, and the applicant can always add one later with
        `hermes profile alias <name>`.
        """
        target = self._context.hermes_profile
        if target is None or target in self.existing_profiles():
            return
        completed = _run(
            self._runner,
            [
                executable,
                "profile",
                "create",
                target,
                "--no-alias",
                "--description",
                "AgentNexus agent profile",
            ],
            timeout=300.0,
            stdin="\n",
        )
        if completed.returncode != 0 or target not in self.existing_profiles():
            detail = (completed.stderr or completed.stdout or "").strip()[-300:]
            message = f"`hermes profile create {target}` failed: {detail}"
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    f"Hermes would not create a profile named {target!r}, so this agent has "
                    "nowhere isolated to live and nothing was changed. Hermes profile names are "
                    "lower-case and alphanumeric; re-run setup with a profile name it accepts."
                ),
            )

    def _require_isolated_profile(self) -> None:
        """Refuse to configure a named profile unless Hermes really selected it.

        This is the check that makes a second profile safe rather than merely differently named.
        Hermes is asked where its configuration is *while running as this profile*; if the answer
        is not that profile's own file, the selector did not take effect and both identities would
        end up in one context. That is a refusal, not a warning: the alternative is one agent
        holding two signing keys.
        """
        target = self._context.hermes_profile
        if not self._context.isolated or target is None:
            return
        resolved = self._config().resolve()
        if resolved == self._config(shared=True).resolve() or target not in resolved.parts:
            message = (
                f"Hermes reports its configuration for profile {target!r} at {resolved}, which is "
                "not that profile's own file."
            )
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "This Hermes installation did not apply `-p`, so a second AgentNexus identity "
                    "would share one agent's context with the first. Nothing was changed. Check "
                    f"`hermes -p {target} config path`, and upgrade Hermes if it does not report "
                    "that profile's own configuration."
                ),
            )

    def rollback(self, backup: Path | None) -> None:
        """Undo only what this run changed."""
        if backup is not None and backup.is_file():
            shutil.copy2(backup, self._config())

    def remove_entry(self) -> str | None:
        """Remove this profile's entry through Hermes' own CLI, and confirm it is gone."""
        executable = self._which("hermes")
        if executable is None:
            return None
        completed = _run(
            self._runner,
            self._command(executable, "mcp", "remove", self._server_name),
            # Same confirmation prompt as the replacement path above.
            stdin=CONFIRM_DEFAULT,
            env=self._environment(),
        )
        if self.existing_entry() is None:
            return None
        detail = (completed.stderr or completed.stdout or "").strip()
        return (
            f"The `{self._server_name}` entry could not be removed and is still registered: "
            f"{detail[-200:]}"
        )

    def verify(self) -> list[str]:
        """Ask the runtime to confirm the server, returning what it reported."""
        executable = self._which("hermes")
        if executable is None:
            return []
        completed = _run(
            self._runner, self._command(executable, "mcp", "list"), env=self._environment()
        )
        if completed.returncode != 0 or self._server_name not in (completed.stdout or ""):
            message = "Hermes does not list an AgentNexus MCP server after configuration."
            raise RuntimeIntegrationError(
                message,
                recovery=f"Run `{self._spelling('mcp list')}` yourself to see what it reports.",
            )
        return [f"`{self._spelling('mcp list')}` shows {self._server_name}"]

    def _spelling(self, arguments: str) -> str:
        """How an applicant would type one of these commands, profile selector included."""
        return " ".join(["hermes", *self._context.hermes_arguments, arguments])

    def verify_isolation(self) -> list[str]:
        """Prove this profile's entry is in its own Hermes profile and not in the shared one."""
        target = self._context.hermes_profile
        if not self._context.isolated or target is None:
            return []
        self._require_isolated_profile()
        notes = [f"Hermes profile {target!r} keeps its configuration at {self._config()}"]

        if self._entry_in(self._document_at(self._config(shared=True)), self._server_name):
            message = (
                f"The {self._server_name!r} entry also appears in the shared Hermes configuration."
            )
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "Two identities must not be visible to one agent. Remove that entry with "
                    f"`hermes mcp remove {self._server_name}` and re-run setup for this profile."
                ),
            )
        notes.append("the shared Hermes configuration does not carry this profile's entry")
        return notes

    def soul_location(self) -> SoulLocation:
        """Ask Hermes where this profile lives, and put its soul file inside that directory.

        `hermes profile show <name>` prints a `Path:` line naming the profile's own directory, and
        a `SOUL.md:` line confirming that the file is a thing this runtime has. Both were read off
        a real v0.20.6. That command is the contract used here — not `HERMES_HOME`, not a guessed
        `~/.hermes`, and not the `-p` selector's implied location — because the thing about to be
        rewritten is a document the applicant wrote, and a path that is merely usually right is not
        good enough for that.

        The answer is then checked against the profile it was asked about: a named profile whose
        directory does not carry its own name is a refusal, since it would mean writing one agent's
        soul into another agent's context.
        """
        executable = self._which("hermes")
        if executable is None:
            message = "Hermes is not on PATH, so it cannot say where this profile's soul lives."
            raise RuntimeIntegrationError(
                message, recovery="Install Hermes, confirm `hermes --version` runs, and try again."
            )
        target = self._context.hermes_profile or DEFAULT_PROFILE_NAME
        completed = _run(self._runner, [executable, "profile", "show", target], timeout=120.0)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-200:]
            message = f"`hermes profile show {target}` failed: {detail}"
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    f"Run `hermes profile show {target}` yourself. Nothing was changed. If the "
                    "profile does not exist, re-run setup for this AgentNexus profile first."
                ),
            )

        directory: Path | None = None
        for line in (completed.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("path:"):
                reported = stripped.split(":", 1)[1].strip()
                if reported:
                    directory = Path(reported)
                break
        if directory is None:
            message = f"`hermes profile show {target}` did not report a path."
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "This Hermes does not report a profile path in the form this connector reads, "
                    "so it cannot prove which soul file it would write. Nothing was changed."
                ),
            )
        if not directory.is_dir():
            message = f"Hermes reports profile {target!r} at {directory}, which is not a directory."
            raise RuntimeIntegrationError(message, recovery="Nothing was changed.")

        # A named profile must live in a directory that names it. `default` deliberately does not:
        # a real Hermes reports the installation root for it, which is exactly right.
        if target != DEFAULT_PROFILE_NAME and target not in directory.resolve().parts:
            message = (
                f"Hermes reports profile {target!r} at {directory}, which is not that profile's "
                "own directory."
            )
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "Writing there could put this agent's instructions into another agent's "
                    f"context. Nothing was changed. Check `hermes profile show {target}`."
                ),
            )
        return SoulLocation(
            runtime=self.name, profile=target, directory=directory, filename=HERMES_SOUL_FILENAME
        )

    def start_hint(self) -> list[str]:
        """Say how the applicant starts Hermes as this profile, in Hermes' own terms."""
        target = self._context.hermes_profile
        if not self._context.isolated or target is None:
            return ["Start a new Hermes session so it loads the AgentNexus tools."]
        return [
            f"Start this agent with `hermes -p {target}`.",
            f"For a shorter command, `hermes profile alias {target}` writes a wrapper script "
            "into ~/.local/bin. Setup does not create one, because that is outside the directory "
            "AgentNexus owns.",
        ]


# ---------------------------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------------------------


class OpenClawAdapter:
    """OpenClaw, through its official `openclaw mcp` registry commands.

    **Verified against a real OpenClaw 2026.8.1**, installed from its official npm distribution and
    driven under OpenClaw's own profile isolation so no working configuration was touched. The
    command surface used here — `mcp add` with `--command`/`--arg`/repeated `--env`, `mcp list
    --json`, `mcp status --verbose`, `mcp doctor <name> --probe`, `mcp probe <name> --json`, and
    `mcp unset <name>` — was read off that installation, not off a description of it.

    The adapter deliberately does not fall back to editing OpenClaw's internal files when a command
    is missing: guessing an undocumented on-disk format is how an integration corrupts somebody's
    configuration. A missing command is reported with the version that lacks it.
    """

    name = "openclaw"
    display_name = "OpenClaw"

    #: The oldest OpenClaw this adapter has actually been run against. Older releases may well
    #: work, but `mcp unset` and `mcp probe --json` were confirmed here and nowhere else, and an
    #: installer that quietly assumes an unverified command surface is how configurations get
    #: half-written. Raise this only after running against the version you are raising it to.
    minimum_version: Final = (2026, 8, 1)

    #: The exact installation this adapter was proven against.
    verified_version: Final = "2026.8.1"

    def __init__(
        self,
        *,
        which: Any = shutil.which,
        runner: Any = subprocess.run,
        context: RuntimeContext | None = None,
    ) -> None:
        """Build the adapter, with the process seams a test can stand in."""
        self._which = which
        self._runner = runner
        self._context = context or RuntimeContext.shared()
        self._server_name = self._context.server_name

    @property
    def context(self) -> RuntimeContext:
        """The profile home this adapter configures OpenClaw inside."""
        return self._context

    def _environment(self) -> dict[str, str] | None:
        """Return the environment every `openclaw` invocation here runs with."""
        return self._context.process_environment()

    def detect(self) -> RuntimeDetection:
        """Report whether this runtime is installed, and which version."""
        executable = self._which("openclaw")
        if executable is None:
            return RuntimeDetection(installed=False)
        completed = _run(self._runner, [executable, "--version"], timeout=60.0)
        version = None
        if completed.returncode == 0:
            match = re.search(r"v?(\d+\.\d+\.\d+)", completed.stdout or "")
            version = match.group(1) if match else (completed.stdout or "").strip()[:64]
        return RuntimeDetection(
            installed=True,
            version=version,
            executable=executable,
            verified_against=f"a real OpenClaw {self.verified_version} installation",
        )

    def _require_supported(self) -> str:
        detection = self.detect()
        if not detection.installed or detection.executable is None:
            message = "OpenClaw was not found on PATH."
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "Install OpenClaw from its official distribution, confirm `openclaw --version` "
                    "runs in a new terminal, then re-run setup with --runtime openclaw."
                ),
            )
        if detection.version is not None:
            parts = tuple(int(part) for part in re.findall(r"\d+", detection.version)[:3])
            if parts and parts < self.minimum_version:
                minimum = ".".join(str(part) for part in self.minimum_version)
                message = (
                    f"OpenClaw {detection.version} is older than {minimum}, which is the oldest "
                    "release this connector has been run against."
                )
                raise RuntimeIntegrationError(
                    message, recovery="Upgrade OpenClaw, then re-run setup."
                )
        return detection.executable

    def existing_entry(self) -> dict[str, Any] | None:
        """Return this profile's AgentNexus MCP entry, if the runtime already has it."""
        return self._entry_named(self._server_name, environment=self._environment())

    def _entry_named(
        self, name: str, *, environment: dict[str, str] | None
    ) -> dict[str, Any] | None:
        executable = self._which("openclaw")
        if executable is None:
            return None
        completed = _run(self._runner, [executable, "mcp", "list", "--json"], env=environment)
        if completed.returncode != 0:
            return None
        try:
            document = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as error:
            message = "`openclaw mcp list --json` did not return JSON."
            raise RuntimeIntegrationError(
                message, recovery="Run that command yourself to see what OpenClaw reports."
            ) from error
        servers = document.get("servers", document)
        if isinstance(servers, dict):
            entry = servers.get(name)
            return entry if isinstance(entry, dict) else None
        if isinstance(servers, list):
            for candidate in servers:
                if isinstance(candidate, dict) and candidate.get("name") == name:
                    return candidate
        return None

    def configure(self, spec: ServerSpec, *, backup_directory: Path) -> ConfigurationOutcome:
        """Register or update the entry, backing up whatever it replaces."""
        executable = self._require_supported()
        self._context.prepare()

        existing = self.existing_entry()
        if existing is not None:
            if _matches(existing, spec):
                return ConfigurationOutcome(
                    changed=False, backup=None, detail="already configured; left unchanged"
                )
            if not _is_same_identity(existing, spec):
                # A different AgentNexus entry is a conflict, not something to overwrite: it points
                # at another identity's key, and replacing it would silently retire that agent.
                raise _conflict(self._server_name, existing, runtime="OpenClaw")

        # The recoverable state OpenClaw itself can produce, rather than a guess at its file layout.
        backup: Path | None = _timestamped(
            backup_directory, f"openclaw-mcp-list-{self._context.profile}"
        ).with_suffix(".json")
        listing = _run(self._runner, [executable, "mcp", "list", "--json"], env=self._environment())
        if backup is not None and listing.returncode == 0:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(listing.stdout or "{}", encoding="utf-8")
        else:
            backup = None

        if existing is not None:
            # Our own entry for this identity, with settings that have since moved — the migrated
            # key path is the real case. `mcp add` refuses a duplicate name, so it is unset first.
            self._remove_entry()

        # A stdio server is `--command` alone: OpenClaw's `--transport` selects an HTTP transport,
        # and passing `stdio` to it is rejected. `--env` carries one pair and is repeated.
        completed = _run(
            self._runner,
            [
                executable,
                "mcp",
                "add",
                self._server_name,
                "--command",
                spec.command,
                "--connect-timeout",
                "60",
                *spec.as_repeated_env_flags(),
            ],
            env=self._environment(),
        )
        if completed.returncode != 0:
            problem = self._remove_entry()
            message = f"`openclaw mcp add` failed: {(completed.stderr or completed.stdout)[-400:]}"
            raise RuntimeIntegrationError(
                message,
                recovery=problem
                or (
                    "No entry was added. Run `openclaw mcp status --verbose` to see the current "
                    "state; your key and identity are unaffected."
                ),
            )

        if self.existing_entry() is None:
            problem = self._remove_entry()
            message = "OpenClaw accepted the entry but does not list it."
            raise RuntimeIntegrationError(
                message, recovery=problem or "Run `openclaw mcp status --verbose`."
            )

        return ConfigurationOutcome(
            changed=True, backup=backup, detail=f"registered with {executable}"
        )

    def rollback(self, backup: Path | None) -> None:
        """Remove the entry this adapter added. OpenClaw owns its own file; we do not rewrite it.

        `mcp unset`, not `mcp remove`: OpenClaw has no `remove`, so the earlier spelling left a
        half-written entry in place while reporting a clean rollback.
        """
        self._remove_entry()

    def remove_entry(self) -> str | None:
        """Remove this profile's entry, returning what stopped it if anything did."""
        return self._remove_entry()

    def _remove_entry(self) -> str | None:
        """Remove the entry, returning what stopped it if anything did.

        OpenClaw guards its own configuration file against a sharp size drop and will refuse the
        write — a real refusal seen here, `size-drop:1218->358`, when the AgentNexus entry is most
        of the file. That refusal has to surface: reporting a clean rollback while the entry is
        still registered is worse than reporting the problem. We do not work around the guard by
        editing the file ourselves, which is exactly the thing this adapter exists not to do.
        """
        executable = self._which("openclaw")
        if executable is None:
            return None
        completed = _run(
            self._runner,
            [executable, "mcp", "unset", self._server_name],
            env=self._environment(),
        )
        if self.existing_entry() is None:
            return None
        detail = (completed.stderr or completed.stdout or "").strip()
        if "size-drop" in detail or "Config write rejected" in detail:
            return (
                "OpenClaw refused to rewrite its configuration file, because removing the entry "
                "would shrink it sharply and that is its own guard against a truncated config. "
                f"The `{self._server_name}` entry is still registered. Look at the file OpenClaw "
                f"named, then remove the entry yourself with `openclaw mcp unset "
                f"{self._server_name}`."
            )
        return (
            f"The `{self._server_name}` entry could not be removed and is still registered: "
            f"{detail[-200:]}"
        )

    def verify(self) -> list[str]:
        """Ask the runtime to confirm the server, returning what it reported."""
        executable = self._require_supported()
        notes: list[str] = []

        status = _run(
            self._runner,
            [executable, "mcp", "status", "--verbose"],
            env=self._environment(),
        )
        if status.returncode != 0:
            message = f"`openclaw mcp status --verbose` failed: {(status.stderr or '')[-300:]}"
            raise RuntimeIntegrationError(message)
        notes.append("`openclaw mcp status --verbose` reported the registry")

        probe = _run(
            self._runner,
            [executable, "mcp", "doctor", self._server_name, "--probe"],
            timeout=180.0,
            env=self._environment(),
        )
        combined = f"{probe.stdout or ''}\n{probe.stderr or ''}"
        if probe.returncode != 0:
            if re.search(r"sandbox|tool profile|permission|policy|denied", combined, re.I):
                # Named explicitly rather than worked around: broadening a sandbox policy without
                # the owner asking is not a decision an installer gets to make.
                message = "OpenClaw's tool profile or sandbox policy is blocking the probe."
                raise RuntimeIntegrationError(
                    message,
                    recovery=(
                        f"Ask the OpenClaw owner to allow the {self._server_name!r} MCP server in "
                        "the active tool profile — the minimum change is enabling that one server, "
                        "not widening the profile — then re-run setup. Setup made no permission "
                        "change of its own."
                    ),
                )
            message = f"`openclaw mcp doctor {self._server_name} --probe` failed."
            raise RuntimeIntegrationError(
                message,
                recovery=f"Run `openclaw mcp doctor {self._server_name} --probe` for the detail.",
            )

        notes.append(f"`openclaw mcp doctor {self._server_name} --probe` reported no issues")

        # `doctor` answers "is it healthy"; it does not enumerate tools. `probe --json` does, so
        # the tool check reads a structured capability list rather than grepping prose that a
        # future release is free to reword.
        discovered = _run(
            self._runner,
            [executable, "mcp", "probe", self._server_name, "--json"],
            timeout=180.0,
            env=self._environment(),
        )
        if discovered.returncode != 0:
            message = f"`openclaw mcp probe {self._server_name} --json` could not connect."
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    f"Run `openclaw mcp probe {self._server_name} --json` to see what it reports. "
                    "Your key and identity are unaffected."
                ),
            )
        exposed = _tool_names(discovered.stdout or "")
        missing = sorted(
            tool for tool in EXPECTED_TOOLS if not any(tool in name for name in exposed)
        )
        if missing:
            message = f"OpenClaw did not expose the expected AgentNexus tools: {missing}."
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "Start a new OpenClaw session so it re-discovers the server, then run "
                    f"`openclaw mcp probe {MCP_SERVER_NAME} --json` again."
                ),
            )
        notes.append(f"probe exposed {', '.join(sorted(EXPECTED_TOOLS))}")
        return notes

    def verify_isolation(self) -> list[str]:
        """Prove this profile's entry is in its own registry and not in the shared one.

        OpenClaw's supported isolation is a relocated configuration file and state directory, so
        the proof is the one that matters: the entry is visible when OpenClaw runs under this
        profile's `OPENCLAW_CONFIG_PATH`, and *not* visible when it runs without it. If it were
        visible in both, one agent could sign as either identity.
        """
        if not self._context.isolated or self._context.openclaw_config is None:
            return []

        configuration = self._context.openclaw_config
        if not configuration.is_file():
            message = f"OpenClaw did not write a registry at {configuration}."
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "This OpenClaw installation does not honour OPENCLAW_CONFIG_PATH, so a second "
                    "AgentNexus identity would share one agent's context with the first. Nothing "
                    "was changed. Upgrade OpenClaw, or run this profile under a different "
                    "operating-system user."
                ),
            )
        notes = [f"OpenClaw registry for this profile is {configuration}"]

        if self._entry_named(self._server_name, environment=None) is not None:
            message = (
                f"The {self._server_name!r} entry also appears in the shared OpenClaw registry."
            )
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "Two identities must not be visible to one agent. Remove that entry with "
                    f"`openclaw mcp unset {self._server_name}` and re-run setup for this profile."
                ),
            )
        notes.append("the shared OpenClaw registry does not carry this profile's entry")
        return notes

    def soul_location(self) -> SoulLocation:
        """Refuse: no OpenClaw instruction-document contract has been established.

        This is the honest answer rather than a placeholder. The OpenClaw command surface used
        elsewhere in this adapter was read off a real 2026.8.1 installation; nothing equivalent is
        known for an instruction document, no OpenClaw is installed on the machine this was written
        on, and this repository holds no authoritative material describing one.

        Guessing a filename would be worse than refusing. The operation this feeds is "back up and
        replace a document the applicant wrote", and pointing that at a path nobody verified is how
        a connector destroys somebody's work while reporting success.
        """
        message = "AgentNexus does not know where OpenClaw keeps a profile's instruction document."
        raise RuntimeIntegrationError(
            message,
            recovery=(
                "Nothing was read or changed. The soul commands support Hermes today. Configure "
                "this agent's instructions through OpenClaw's own documented mechanism, or run "
                "the soul commands against a Hermes profile with `--runtime hermes`."
            ),
        )

    def start_hint(self) -> list[str]:
        """Say how the applicant starts OpenClaw as this profile.

        OpenClaw's isolation is two environment variables rather than a named profile, so unlike
        Hermes it has no wrapper script of its own to point at. The connector writes one, because
        an isolated registry nobody starts OpenClaw against is not isolation.
        """
        if not self._context.isolated or self._context.openclaw_config is None:
            return ["Start a new OpenClaw session so it loads the AgentNexus tools."]
        return [
            "Start this agent through the launcher written beside its profile, which sets "
            "OPENCLAW_CONFIG_PATH and OPENCLAW_STATE_DIR for it.",
        ]


def _tool_names(payload: str) -> set[str]:
    """Every tool name in an `openclaw mcp probe --json` document.

    Names are matched loosely by the caller because a runtime may namespace them; what matters
    here is reading the structured list rather than the surrounding prose.
    """
    try:
        document = json.loads(payload or "{}")
    except json.JSONDecodeError as error:
        message = "`openclaw mcp probe --json` did not return JSON."
        raise RuntimeIntegrationError(
            message, recovery="Run that command yourself to see what OpenClaw reports."
        ) from error
    names: set[str] = set()
    for tool in document.get("tools", []) if isinstance(document, dict) else []:
        if isinstance(tool, str):
            names.add(tool)
        elif isinstance(tool, dict):
            for key in ("name", "toolName", "qualifiedName"):
                value = tool.get(key)
                if isinstance(value, str):
                    names.add(value)
    return names


def _entry_environment(entry: dict[str, Any]) -> dict[str, str]:
    """Return the environment an existing entry declares, in whichever key its runtime uses."""
    environment = entry.get("env") or entry.get("environment") or {}
    if not isinstance(environment, dict):
        return {}
    return {str(key): str(value) for key, value in environment.items()}


def _matches(entry: dict[str, Any], spec: ServerSpec) -> bool:
    """Whether an existing entry is already exactly what this setup would write."""
    # This adapter never registers arguments, so an entry carrying any is somebody else's, and
    # treating it as ours would let a rerun report "already configured" over a different server.
    if entry.get("args"):
        return False
    return entry.get("command") == spec.command and _entry_environment(entry) == spec.environment


def _is_same_identity(entry: dict[str, Any], spec: ServerSpec) -> bool:
    """Whether an existing entry is *this* profile's own entry, differing only in its settings.

    The distinction decides whether a rerun may rewrite an entry or has to stop, so it is drawn on
    the only field that identifies an agent: the AgentNexus agent id the entry signs as. Same id
    and same profile means the entry is ours and the difference is something legitimate — the
    migrated key path is the real example. Anything else is another agent's entry, and this
    connector does not get to retire another agent to make room for itself.
    """
    if entry.get("args"):
        return False
    environment = _entry_environment(entry)
    identity = spec.environment.get(IDENTITY_ENVIRONMENT_VARIABLE)
    if not identity or environment.get(IDENTITY_ENVIRONMENT_VARIABLE) != identity:
        return False
    expected_profile = spec.environment.get(PROFILE_ENVIRONMENT_VARIABLE)
    declared_profile = environment.get(PROFILE_ENVIRONMENT_VARIABLE)
    # An entry written before profiles existed declares none, and belongs to `default`.
    return declared_profile is None or declared_profile == expected_profile


def _conflict(name: str, entry: dict[str, Any], *, runtime: str) -> RuntimeIntegrationError:
    """Build the refusal used when an entry under this profile's name is somebody else's."""
    other = _entry_environment(entry).get(IDENTITY_ENVIRONMENT_VARIABLE)
    whose = f" It is registered for agent {other}." if other else ""
    unset = "hermes mcp remove" if runtime == "Hermes" else "openclaw mcp unset"
    message = f"{runtime} already has an MCP server named {name!r} with different settings.{whose}"
    return RuntimeIntegrationError(
        message,
        recovery=(
            f"Nothing was changed. That entry may belong to another AgentNexus profile, and "
            f"replacing it would retire that agent. Use a different profile name, or — only if "
            f"the entry is genuinely unused — remove it with `{unset} {name}` and re-run setup."
        ),
    )


#: Every supported runtime, by the token `--runtime` accepts.
ADAPTERS: Final = {"hermes": HermesAdapter, "openclaw": OpenClawAdapter}
