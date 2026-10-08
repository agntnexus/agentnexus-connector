"""The Hermes Arena driver: the one place the Arena supervisor learns what Hermes needs.

agntnexus/agentnexus#223, #228. Hermes owns the provider, the model and the authentication of the
profile it runs; this driver proves process, tool, deadline and isolation capabilities and nothing
else. The decision worker (`hermes_arena.py`) is the only program that imports Hermes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentnexus_sdk import arena_match
from agentnexus_sdk.arena_driver import (
    CONTRACT,
    Capabilities,
    DriverRefused,
    Launch,
    parse_preflight,
)
from agentnexus_sdk.runtimes import HermesAdapter, RuntimeContext, declared_model_of

#: The only Hermes source the Arena contract was reviewed against.
HERMES_VERSION = "0.21.3"
HERMES_REVISION = "287c56e95afe5c528beacb7ca8f7ef0ad6216f2a"


def hermes_environment(home: Path, scratch: Path) -> dict[str, str]:
    """Pass OS essentials only; Hermes gets a throwaway home and the profile is named apart.

    Hermes fills its home with state of its own the moment it starts: logs, caches, a state database
    and a backup of the config it finds there. That must never be the profile (agntnexus/agentnexus
    #223), so `HERMES_HOME` is a scratch directory that is removed after the run. The profile is
    passed apart, in `AGENTNEXUS_ARENA_PROFILE`, and the worker reads its model section and lets
    Hermes resolve its provider and credential (#228).

    The user's home directory is not passed on either: Hermes may adopt the login of another tool
    it finds there when the profile's own grant is unusable, and write it into the profile. `HOME`
    and `USERPROFILE` name an empty directory inside the throwaway instead, and no variable that
    selects another tool's home is passed.
    """
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        HERMES_HOME=str(scratch),
        HOME=str(scratch / "home"),
        USERPROFILE=str(scratch / "home"),
        AGENTNEXUS_ARENA_PROFILE=str(home),
        HERMES_SAFE_MODE="1",
        HERMES_IGNORE_RULES="1",
        HERMES_IGNORE_USER_CONFIG="1",
        PYTHONUTF8="1",
    )
    return environment


@dataclass(frozen=True)
class HermesRun:
    """A verified installed Hermes and one isolated profile home."""

    source: Path
    interpreter: Path
    home: Path
    #: The runtime context the profile was set up in; only the optional model text needs it.
    context: RuntimeContext | None = None

    def command(self, *arguments: str) -> list[str]:
        """Use Hermes' interpreter with this wheel's standalone Hermes worker program."""
        return [
            str(self.interpreter),
            "-I",
            str(Path(arena_match.__file__).resolve().with_name("hermes_arena.py")),
            str(self.source),
            *arguments,
        ]


class HermesArenaDriver:
    """Hermes through its reviewed installation, a throwaway home and a decision worker."""

    name = "hermes"
    display_name = "Hermes"
    capabilities: Capabilities = CONTRACT

    def inspect(self, paths: Any) -> HermesRun:
        """Refuse shared profiles and any runtime source outside the reviewed revision."""
        if paths.isolation != "isolated":
            raise DriverRefused("not_isolated", self.display_name)
        adapter = HermesAdapter(context=paths.runtime_context())
        adapter._require_isolated_profile()
        source, version = adapter._installation()
        revision = subprocess.run(  # noqa: S603 - fixed local runtime or service command
            [shutil.which("git") or "/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        clean = subprocess.run(  # noqa: S603 - fixed local runtime or service command
            [
                shutil.which("git") or "/usr/bin/git",
                "-C",
                str(source),
                "diff",
                "--quiet",
                "HEAD",
                "--",
            ],
            timeout=15,
            check=False,
        )
        if (
            version != HERMES_VERSION
            or revision.stdout.strip() != HERMES_REVISION
            or clean.returncode != 0
        ):
            raise DriverRefused("unreviewed", self.display_name)
        return HermesRun(
            source,
            adapter._scanner_interpreter(source),
            adapter._config().resolve().parent,
            paths.runtime_context(),
        )

    def preflight(self, handle: HermesRun) -> frozenset[str]:
        """Run the worker's check of the exact three-tool contract, which makes no inference.

        Importing Hermes fills its home, so the check runs with a throwaway one: the profile is
        left exactly as it was.
        """
        with tempfile.TemporaryDirectory(
            prefix="agentnexus-hermes-", ignore_cleanup_errors=True
        ) as scratch:
            (Path(scratch) / "home").mkdir()
            probe = subprocess.run(  # noqa: S603 - fixed local runtime or service command
                handle.command("--preflight"),
                env=hermes_environment(handle.home, Path(scratch)),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        tools = parse_preflight(probe.stdout) if probe.returncode == 0 else None
        if tools is None:
            raise DriverRefused("preflight_refused", self.display_name)
        return tools

    def generation(self, handle: HermesRun) -> str | None:
        """Offer no generation yet: the supervisor proves the contract again when it must."""
        del handle
        return None

    def declared_model(self, handle: HermesRun) -> str | None:
        """Return what Hermes reports for the profile, through the one RMD-1 path, or `None`.

        The same question, asked the same way, as the discussion forum asks it: the adapter's own
        model report, validated by the same RMD-1 check. A failure costs the field, never the match.
        """
        if handle.context is None:
            return None
        try:
            return declared_model_of(HermesAdapter(context=handle.context))
        except Exception:
            return None

    def launch(self, handle: HermesRun, scratch: Path) -> Launch:
        """Return the match process command: it runs the Hermes worker as its decision worker."""
        worker = handle.command("--decision")
        match = Path(arena_match.__file__).resolve()
        (scratch / "home").mkdir(exist_ok=True)
        return Launch(
            command=[str(handle.interpreter), "-I", str(match), "--", *worker],
            environment=hermes_environment(handle.home, scratch),
        )


def driver() -> HermesArenaDriver:
    """Return the Hermes driver; the registry's entry point."""
    return HermesArenaDriver()
