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

**Verification status, stated plainly.** The Hermes adapter was exercised against a real Hermes
installation (v0.20.6). The OpenClaw adapter implements the `openclaw mcp` contract this project
was given — `add`, `list`, `status --verbose`, `doctor <name> --probe` — but no OpenClaw
installation was available while it was written, so it is covered by tests against a stub CLI that
implements that contract, not against the real tool. `RuntimeDetection.verified_against` records
this, and setup prints it, so nobody mistakes a stubbed adapter for a proven one.
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
    runner: Any, arguments: list[str], *, timeout: float = 120.0
) -> subprocess.CompletedProcess[str]:
    """Run a CLI with a fixed argument list. Never a shell, never an interpolated string."""
    completed: subprocess.CompletedProcess[str] = runner(
        arguments,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
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
        if self._config_path is not None:
            return self._config_path
        import os

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
        import yaml

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

    **Unverified against a real installation.** No OpenClaw was available while this was written,
    so the commands below are the contract this project was given — `add`, `list --json`,
    `status --verbose`, `doctor <name> --probe` — and the adapter is covered by tests against a
    stub CLI implementing exactly that contract. `detect()` reports this, and setup prints it, so a
    stubbed adapter is never mistaken for a proven one. The first real run should be treated as the
    acceptance test it is.

    The adapter deliberately does not fall back to editing OpenClaw's internal files when a command
    is missing: guessing an undocumented on-disk format is how an integration corrupts somebody's
    configuration. A missing command is reported with the version that lacks it.
    """

    name = "openclaw"
    display_name = "OpenClaw"

    #: Below this, the registry commands this adapter needs are assumed absent.
    minimum_version: Final = (0, 1, 0)

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
            verified_against=(
                "a stub CLI implementing the documented `openclaw mcp` contract, "
                "not a real OpenClaw installation"
            ),
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
                    f"OpenClaw {detection.version} is older than {minimum}, which is the first "
                    "version with the `openclaw mcp` registry commands this connector uses."
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
                    f"remove it with `openclaw mcp remove {MCP_SERVER_NAME}` and re-run setup. "
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

        completed = _run(
            self._runner,
            [
                executable,
                "mcp",
                "add",
                MCP_SERVER_NAME,
                "--transport",
                "stdio",
                "--command",
                spec.command,
                "--env",
                *spec.as_env_arguments(),
            ],
        )
        if completed.returncode != 0:
            self.rollback(backup)
            message = f"`openclaw mcp add` failed: {(completed.stderr or completed.stdout)[-400:]}"
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "No entry was added. Run `openclaw mcp status --verbose` to see the current "
                    "state; your key and identity are unaffected."
                ),
            )

        if self.existing_entry() is None:
            self.rollback(backup)
            message = "OpenClaw accepted the entry but does not list it."
            raise RuntimeIntegrationError(message, recovery="Run `openclaw mcp status --verbose`.")

        return ConfigurationOutcome(
            changed=True, backup=backup, detail=f"registered with {executable}"
        )

    def rollback(self, backup: Path | None) -> None:
        """Remove the entry this adapter added. OpenClaw owns its own file; we do not rewrite it."""
        executable = self._which("openclaw")
        if executable is None:
            return
        _run(self._runner, [executable, "mcp", "remove", MCP_SERVER_NAME])

    def verify(self) -> list[str]:
        """Ask the runtime to confirm the server, returning what it reported."""
        executable = self._require_supported()
        notes: list[str] = []

        status = _run(self._runner, [executable, "mcp", "status", "--verbose"])
        if status.returncode != 0:
            message = f"`openclaw mcp status --verbose` failed: {(status.stderr or '')[-300:]}"
            raise RuntimeIntegrationError(message)
        notes.append("`openclaw mcp status --verbose` reported the registry")

        probe = _run(self._runner, [executable, "mcp", "doctor", MCP_SERVER_NAME, "--probe"])
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

        missing = sorted(tool for tool in EXPECTED_TOOLS if tool not in combined)
        if missing:
            message = f"OpenClaw did not expose the expected AgentNexus tools: {missing}."
            raise RuntimeIntegrationError(
                message,
                recovery=(
                    "Start a new OpenClaw session so it re-discovers the server, then run "
                    f"`openclaw mcp doctor {MCP_SERVER_NAME} --probe` again."
                ),
            )
        notes.append(f"probe exposed {', '.join(sorted(EXPECTED_TOOLS))}")
        return notes


def _matches(entry: dict[str, Any], spec: ServerSpec) -> bool:
    """Whether an existing entry is already exactly what this setup would write."""
    command = entry.get("command")
    environment = entry.get("env") or entry.get("environment") or {}
    if not isinstance(environment, dict):
        return False
    return (
        command == spec.command
        and {str(key): str(value) for key, value in environment.items()} == spec.environment
    )


#: Every supported runtime, by the token `--runtime` accepts.
ADAPTERS: Final = {"hermes": HermesAdapter, "openclaw": OpenClawAdapter}
