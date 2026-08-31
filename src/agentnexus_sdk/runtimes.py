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

**Verification status, stated plainly.** Both adapters have now driven a real installation end to
end — Hermes v0.20.6 and OpenClaw 2026.8.1 — from a clean wheel install rather than a repository
checkout, each runtime held inside its own official isolation so no working configuration was
touched. Hermes contributed two failures of its own that only a real run could produce: `hermes mcp
add` asks "Enable all 10 tools?" with no flag to skip it and cancels on EOF, saving nothing while
appearing to succeed; and its configuration file moves with `HERMES_HOME`, so a guessed path
inspected and backed up a file that was never the one being written.

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
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

#: The MCP server name both runtimes use. Idempotency is defined by this name.
MCP_SERVER_NAME: Final = "agentnexus"

#: Tools an AgentNexus MCP server is expected to expose. Checked after configuration so a runtime
#: that accepted the entry but discovered nothing is a failure rather than a silent success.
EXPECTED_TOOLS: Final = frozenset({"catch_up", "conformance"})


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

    def verify(self) -> list[str]:
        """Ask the runtime to confirm the server, returning what it reported."""
        ...


def _run(
    runner: Any, arguments: list[str], *, timeout: float = 120.0, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a CLI with a fixed argument list. Never a shell, never an interpolated string.

    `stdin` exists for one real case: Hermes v0.20.6 asks "Enable all 10 tools? [Y/n/select]" after
    it discovers a server, and offers no flag to skip it. With nothing on stdin it reads EOF and
    cancels, so a non-interactive run silently saved nothing. See `HermesAdapter.configure`.
    """
    extra: dict[str, Any] = {"input": stdin} if stdin is not None else {}
    completed: subprocess.CompletedProcess[str] = runner(
        arguments,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        **extra,
    )
    return completed


def _timestamped(directory: Path, stem: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"{stem}-{stamp}"


# ---------------------------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------------------------


class HermesAdapter:
    """Hermes Agent, through its own `hermes mcp` CLI.

    Verified against a real Hermes v0.20.6 installation: `hermes mcp add` takes `--command`,
    `--env KEY=VALUE ...` and `--connect-timeout`, and `hermes mcp list` reports the configured
    servers. The CLI is used rather than the YAML file because it validates the entry and performs
    tool discovery as it writes.
    """

    name = "hermes"
    display_name = "Hermes"

    def __init__(
        self,
        *,
        which: Any = shutil.which,
        runner: Any = subprocess.run,
        config_path: Path | None = None,
    ) -> None:
        """Build the adapter, with the process seams a test can stand in."""
        self._which = which
        self._runner = runner
        self._config_path = config_path

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

    def _config(self) -> Path:
        """Where Hermes keeps the file this adapter inspects, backs up, and checks after writing.

        Asked of Hermes rather than guessed. `HERMES_HOME` moves that file, and a guess that
        ignores it inspects one file while `hermes mcp add` writes another — which is how a real
        run here ended in "Hermes accepted the entry but the configuration does not contain it",
        and, worse, how a backup could be taken of a file that was never the one at risk.
        """
        if self._config_path is not None:
            return self._config_path

        executable = self._which("hermes")
        if executable is not None:
            completed = _run(self._runner, [executable, "config", "path"])
            reported = (completed.stdout or "").strip().splitlines()
            if completed.returncode == 0 and reported:
                candidate = Path(reported[-1].strip())
                if candidate.name:
                    return candidate

        # Older Hermes releases without `config path`: fall back to its documented locations.
        import os

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
        path = self._config()
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
        """Return the AgentNexus MCP entry this runtime already has, if any."""
        servers = self._document().get("mcp_servers")
        if not isinstance(servers, dict):
            return None
        entry = servers.get(MCP_SERVER_NAME)
        return entry if isinstance(entry, dict) else None

    def configure(self, spec: ServerSpec, *, backup_directory: Path) -> ConfigurationOutcome:
        """Register or update the entry, backing up whatever it replaces."""
        executable = self._which("hermes")
        if executable is None:
            message = "Hermes disappeared from PATH between preflight and configuration."
            raise RuntimeIntegrationError(message)

        existing = self.existing_entry()
        if existing is not None and _matches(existing, spec):
            return ConfigurationOutcome(
                changed=False, backup=None, detail="already configured; left unchanged"
            )

        backup = None
        config = self._config()
        if config.is_file():
            backup = _timestamped(backup_directory, "hermes-config").with_suffix(".yaml")
            shutil.copy2(config, backup)

        if existing is not None:
            # Replacing rather than adding beside: `hermes mcp add` refuses a duplicate name, and
            # two AgentNexus servers in one runtime is exactly the duplication to avoid.
            _run(self._runner, [executable, "mcp", "remove", MCP_SERVER_NAME])

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
            [
                executable,
                "mcp",
                "add",
                MCP_SERVER_NAME,
                "--command",
                spec.command,
                "--connect-timeout",
                "60",
                "--env",
                *spec.as_env_arguments(),
            ],
            timeout=300.0,
            stdin="\n",
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

    def rollback(self, backup: Path | None) -> None:
        """Undo only what this run changed."""
        if backup is not None and backup.is_file():
            shutil.copy2(backup, self._config())

    def verify(self) -> list[str]:
        """Ask the runtime to confirm the server, returning what it reported."""
        executable = self._which("hermes")
        if executable is None:
            return []
        completed = _run(self._runner, [executable, "mcp", "list"])
        if completed.returncode != 0 or MCP_SERVER_NAME not in (completed.stdout or ""):
            message = "Hermes does not list an AgentNexus MCP server after configuration."
            raise RuntimeIntegrationError(
                message,
                recovery="Run `hermes mcp list` yourself to see what it reports.",
            )
        return [f"`hermes mcp list` shows {MCP_SERVER_NAME}"]


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

    def __init__(self, *, which: Any = shutil.which, runner: Any = subprocess.run) -> None:
        """Build the adapter, with the process seams a test can stand in."""
        self._which = which
        self._runner = runner

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
        """Return the AgentNexus MCP entry this runtime already has, if any."""
        executable = self._which("openclaw")
        if executable is None:
            return None
        completed = _run(self._runner, [executable, "mcp", "list", "--json"])
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
            entry = servers.get(MCP_SERVER_NAME)
            return entry if isinstance(entry, dict) else None
        if isinstance(servers, list):
            for candidate in servers:
                if isinstance(candidate, dict) and candidate.get("name") == MCP_SERVER_NAME:
                    return candidate
        return None

    def configure(self, spec: ServerSpec, *, backup_directory: Path) -> ConfigurationOutcome:
        """Register or update the entry, backing up whatever it replaces."""
        executable = self._require_supported()

        existing = self.existing_entry()
        if existing is not None:
            if _matches(existing, spec):
                return ConfigurationOutcome(
                    changed=False, backup=None, detail="already configured; left unchanged"
                )
            # A different AgentNexus entry is a conflict, not something to overwrite: it may point
            # at another identity's key, and replacing it would silently retire that agent.
            message = (
                f"OpenClaw already has an MCP server named {MCP_SERVER_NAME!r} with different "
                "settings."
            )
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    f"Inspect it with `openclaw mcp status --verbose`. If it is no longer needed, "
                    f"remove it with `openclaw mcp unset {MCP_SERVER_NAME}` and re-run setup. "
                    "Nothing was changed."
                ),
            )

        # The recoverable state OpenClaw itself can produce, rather than a guess at its file layout.
        backup: Path | None = _timestamped(backup_directory, "openclaw-mcp-list").with_suffix(
            ".json"
        )
        listing = _run(self._runner, [executable, "mcp", "list", "--json"])
        if backup is not None and listing.returncode == 0:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(listing.stdout or "{}", encoding="utf-8")
        else:
            backup = None

        # A stdio server is `--command` alone: OpenClaw's `--transport` selects an HTTP transport,
        # and passing `stdio` to it is rejected. `--env` carries one pair and is repeated.
        completed = _run(
            self._runner,
            [
                executable,
                "mcp",
                "add",
                MCP_SERVER_NAME,
                "--command",
                spec.command,
                "--connect-timeout",
                "60",
                *spec.as_repeated_env_flags(),
            ],
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
        completed = _run(self._runner, [executable, "mcp", "unset", MCP_SERVER_NAME])
        if self.existing_entry() is None:
            return None
        detail = (completed.stderr or completed.stdout or "").strip()
        if "size-drop" in detail or "Config write rejected" in detail:
            return (
                "OpenClaw refused to rewrite its configuration file, because removing the entry "
                "would shrink it sharply and that is its own guard against a truncated config. "
                f"The `{MCP_SERVER_NAME}` entry is still registered. Look at the file OpenClaw "
                f"named, then remove the entry yourself with `openclaw mcp unset "
                f"{MCP_SERVER_NAME}`."
            )
        return (
            f"The `{MCP_SERVER_NAME}` entry could not be removed and is still registered: "
            f"{detail[-200:]}"
        )

    def verify(self) -> list[str]:
        """Ask the runtime to confirm the server, returning what it reported."""
        executable = self._require_supported()
        notes: list[str] = []

        status = _run(self._runner, [executable, "mcp", "status", "--verbose"])
        if status.returncode != 0:
            message = f"`openclaw mcp status --verbose` failed: {(status.stderr or '')[-300:]}"
            raise RuntimeIntegrationError(message)
        notes.append("`openclaw mcp status --verbose` reported the registry")

        probe = _run(
            self._runner, [executable, "mcp", "doctor", MCP_SERVER_NAME, "--probe"], timeout=180.0
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
                        f"Ask the OpenClaw owner to allow the {MCP_SERVER_NAME!r} MCP server in "
                        "the active tool profile — the minimum change is enabling that one server, "
                        "not widening the profile — then re-run setup. Setup made no permission "
                        "change of its own."
                    ),
                )
            message = f"`openclaw mcp doctor {MCP_SERVER_NAME} --probe` failed."
            raise RuntimeIntegrationError(
                message,
                recovery=f"Run `openclaw mcp doctor {MCP_SERVER_NAME} --probe` to see the detail.",
            )

        notes.append(f"`openclaw mcp doctor {MCP_SERVER_NAME} --probe` reported no issues")

        # `doctor` answers "is it healthy"; it does not enumerate tools. `probe --json` does, so
        # the tool check reads a structured capability list rather than grepping prose that a
        # future release is free to reword.
        discovered = _run(
            self._runner, [executable, "mcp", "probe", MCP_SERVER_NAME, "--json"], timeout=180.0
        )
        if discovered.returncode != 0:
            message = f"`openclaw mcp probe {MCP_SERVER_NAME} --json` could not connect."
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    f"Run `openclaw mcp probe {MCP_SERVER_NAME} --json` to see what it reports. "
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


def _matches(entry: dict[str, Any], spec: ServerSpec) -> bool:
    """Whether an existing entry is already exactly what this setup would write."""
    command = entry.get("command")
    environment = entry.get("env") or entry.get("environment") or {}
    if not isinstance(environment, dict):
        return False
    # This adapter never registers arguments, so an entry carrying any is somebody else's, and
    # treating it as ours would let a rerun report "already configured" over a different server.
    if entry.get("args"):
        return False
    return (
        command == spec.command
        and {str(key): str(value) for key, value in environment.items()} == spec.environment
    )


#: Every supported runtime, by the token `--runtime` accepts.
ADAPTERS: Final = {"hermes": HermesAdapter, "openclaw": OpenClawAdapter}
