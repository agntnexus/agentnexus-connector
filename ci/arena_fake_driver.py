"""A second Arena runtime driver, for the conformance suite (agntnexus/agentnexus#228).

The driver of `fake_runtime_worker.py`. It knows no model and no provider, as no driver may; what it
reports as its generation and as its declared model are plain files in the fake profile that a test
writes, so that the supervisor's handling of both can be driven without any real runtime.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
from agentnexus_sdk.bridge import is_declared_model_valid

WORKER = Path(__file__).with_name("fake_runtime_worker.py")
ESSENTIALS = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
)


@dataclass(frozen=True)
class FakeRuntime:
    """The fake runtime's installation, as `inspect` found it."""

    worker: Path
    behavior: Path
    profile: Path


class FakeArenaDriver:
    """The fake runtime through its worker program, with a configurable set of capabilities."""

    name = "fake"
    display_name = "Fake"

    def __init__(
        self,
        behavior: Path,
        profile: Path,
        *,
        capabilities: Capabilities = CONTRACT,
        worker: Path = WORKER,
    ) -> None:
        """Hold the behaviour file and the profile this fake runtime plays from."""
        self.behavior, self.profile, self.worker = behavior, profile, worker
        self.capabilities = capabilities
        self.inspected = 0
        self.preflights = 0

    def _environment(self, scratch: Path) -> dict[str, str]:
        environment = {k: v for k, v in os.environ.items() if k.upper() in ESSENTIALS}
        environment.update(
            FAKE_RUNTIME_BEHAVIOR=str(self.behavior),
            FAKE_RUNTIME_HOME=str(scratch),
            FAKE_RUNTIME_PROFILE=str(self.profile),
        )
        return environment

    def inspect(self, paths: Any) -> FakeRuntime:
        """Find the fake installation; there is nothing to refuse."""
        del paths
        self.inspected += 1
        return FakeRuntime(self.worker, self.behavior, self.profile)

    def preflight(self, handle: FakeRuntime) -> frozenset[str]:
        """Ask the worker which tools it exposes; it makes no inference."""
        self.preflights += 1
        probe = subprocess.run(  # noqa: S603 - this interpreter and a test's own worker
            [sys.executable, "-I", str(handle.worker), "--preflight"],
            env=self._environment(handle.profile),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        tools = parse_preflight(probe.stdout) if probe.returncode == 0 else None
        if tools is None:
            raise DriverRefused("preflight_refused", self.display_name)
        return tools

    def generation(self, handle: FakeRuntime) -> str | None:
        """Return the opaque token the test wrote, or `None` when it wrote none."""
        path = handle.profile / "generation"
        return path.read_text(encoding="utf-8").strip() if path.is_file() else None

    def declared_model(self, handle: FakeRuntime) -> str | None:
        """Return the text the fake runtime reports about itself, if RMD-1 would send it."""
        path = handle.profile / "reported-model.txt"
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value if is_declared_model_valid(value) else None

    def launch(self, handle: FakeRuntime, scratch: Path) -> Launch:
        """Return the match command: it runs the fake worker as its decision worker."""
        match = Path(arena_match.__file__).resolve()
        worker = [sys.executable, "-I", str(handle.worker)]
        return Launch(
            command=[sys.executable, "-I", str(match), "--", *worker],
            environment=self._environment(scratch),
        )
