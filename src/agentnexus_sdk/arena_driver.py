"""The contract an Arena runtime driver proves, and nothing else (agntnexus/agentnexus#228).

AgentNexus does not know models or providers. The runtime the owner selected for a profile owns the
provider, the model, the authentication, the routing and the inference. The Connector owns the
AgentNexus identity and signature, the start-intent and match protocol, exactly three Arena
operations, process isolation, deadlines, the tool allowlist, cleanup and, optionally, forwarding a
bounded text that the runtime reported about its own model (RMD-1).

A *driver* is the small piece that lets one runtime take part. It is accepted for what it can prove
about process, tool, deadline and isolation capabilities. It is never accepted or refused because of
a provider or a model name, and this module has no way to look at one: a driver answers with an
opaque generation, an optional declared model text, a launch command and a fixed refusal code.

This module is runtime-neutral. It imports no runtime and names no model or provider.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agentnexus_sdk import arena_match

#: Every reason a driver may give for refusing, and the one sentence each is reported as. A driver
#: names a code, never a sentence of its own: nothing a runtime printed, no path, no URL and no
#: credential can reach an operator, a log or a status through a refusal.
REFUSALS = {
    "not_isolated": "Automatic Arena play requires an isolated named {runtime} profile.",
    "unreviewed": "This {runtime} source has not passed the bounded Arena compatibility review.",
    "preflight_refused": "{runtime} refused the exact three-tool Arena preflight.",
    "contract_violated": "The {runtime} Arena driver does not honour the Arena runtime contract.",
    "unknown_driver": "No Arena driver is available for this profile's runtime.",
    "ambiguous_runtime": "This profile has more than one runtime; name one with --runtime.",
}


class DriverRefusedError(ValueError):
    """A fixed refusal: a closed code and the sentence the table holds for it."""

    def __init__(self, code: str, runtime: str) -> None:
        """Build the refusal from the table; an unknown code is a defect, not a message."""
        if code not in REFUSALS:
            raise ValueError("Unknown Arena driver refusal code.")
        super().__init__(REFUSALS[code].format(runtime=runtime))
        self.code = code


DriverRefused = DriverRefusedError


@dataclass(frozen=True)
class Capabilities:
    """What a driver declares about the processes it starts; the conformance suite holds it to it.

    `tools` is the set of operations the decision bridge serves. `worker_killable` says the decision
    worker is one process tree the supervisor can end; `cleanup_bounded` that its cleanup is bounded
    by the supervisor and not by the runtime; `deadline_external` that the deadline is kept by the
    match process and the parent and never delegated to the runtime.
    """

    tools: frozenset[str]
    worker_killable: bool
    cleanup_bounded: bool
    deadline_external: bool


#: The contract capabilities: exactly the three Arena operations, a killable worker, a cleanup the
#: supervisor bounds and a deadline the supervisor keeps.
CONTRACT = Capabilities(
    tools=arena_match.TOOLS, worker_killable=True, cleanup_bounded=True, deadline_external=True
)


@dataclass(frozen=True)
class Launch:
    """How the supervisor starts one match: the match process command and its exact environment."""

    command: list[str]
    environment: dict[str, str]
    #: What the driver pinned for this match, opaque to the supervisor. It is handed back to
    #: `still_pinned` before every request is forwarded; a driver that pins nothing leaves it unset.
    pin: str | None = None


class ArenaRuntimeDriver(Protocol):
    """What the Arena supervisor asks of a runtime. Nothing here is a model or a provider."""

    #: The runtime's stable machine name, as the profile records it.
    name: str
    #: The runtime's display name, used only in fixed refusal sentences.
    display_name: str
    capabilities: Capabilities

    def inspect(self, paths: Any) -> Any:
        """Check the installation and the profile, and return an opaque handle for the rest.

        Raises `DriverRefused` with a fixed code. Starts no inference and writes nothing.
        """

    def preflight(self, handle: Any) -> frozenset[str]:
        """Prove the three-tool contract without inference; return the tools actually exposed.

        Raises `DriverRefused` with a fixed code when the runtime cannot be started or answers
        anything but the closed preflight document.
        """

    def generation(self, handle: Any) -> str | None:
        """Return an opaque, runtime-owned token that changes when the configured runtime does.

        `None` means this runtime offers none, and the supervisor re-proves the contract instead.
        The token is compared for equality and never interpreted, parsed or shown.
        """

    def declared_model(self, handle: Any) -> str | None:
        """Return the runtime's own bounded model text, already RMD-1 valid, or `None`."""

    def launch(self, handle: Any, scratch: Path) -> Launch:
        """Return the match command and environment; `scratch` is the runtime's throwaway home."""


def require_contract(driver: ArenaRuntimeDriver) -> None:
    """Refuse a driver that declares more, or less, than the Arena runtime contract."""
    if driver.capabilities != CONTRACT:
        raise DriverRefused("contract_violated", driver.display_name)


def prove_tools(driver: ArenaRuntimeDriver, tools: frozenset[str]) -> None:
    """Refuse a runtime whose preflight exposes anything but exactly the three operations."""
    if tools != arena_match.TOOLS:
        raise DriverRefused("contract_violated", driver.display_name)


def check_preflight(driver: ArenaRuntimeDriver, handle: Any) -> None:
    """Run the driver's preflight and hold the result to the contract."""
    prove_tools(driver, driver.preflight(handle))


def parse_preflight(stdout: str) -> frozenset[str] | None:
    """Read the closed preflight document a worker prints, or `None` when it is anything else."""
    for line in reversed((stdout or "").splitlines()):
        try:
            document = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(document, dict)
            and set(document) == {"bounded", "tools"}
            and document["bounded"] is True
            and isinstance(document["tools"], list)
            and all(isinstance(name, str) for name in document["tools"])
        ):
            return frozenset(document["tools"])
        return None
    return None


#: Drivers are looked up by the runtime's name and imported on demand, so that an installation
#: without a runtime never loads its driver and the supervisor never imports a runtime.
_BUILTIN: dict[str, str] = {
    "hermes": "agentnexus_sdk.arena_driver_hermes:driver",
    "openclaw": "agentnexus_sdk.arena_driver_openclaw:driver",
}
_REGISTERED: dict[str, Callable[[], ArenaRuntimeDriver]] = {}


def register(name: str, factory: Callable[[], ArenaRuntimeDriver]) -> None:
    """Add a driver by name; used by tests, and by nothing at import time."""
    _REGISTERED[name] = factory


def unregister(name: str) -> None:
    """Remove a driver added with `register`."""
    _REGISTERED.pop(name, None)


def known() -> frozenset[str]:
    """Return the names of the drivers that exist."""
    return frozenset(_BUILTIN) | frozenset(_REGISTERED)


def driver_for(name: str) -> ArenaRuntimeDriver:
    """Return the driver of the named runtime. The runtime's name is the only key."""
    if name in _REGISTERED:
        return _REGISTERED[name]()
    if name not in _BUILTIN:
        raise DriverRefused("unknown_driver", "Arena")
    module_name, _, attribute = _BUILTIN[name].partition(":")
    driver: ArenaRuntimeDriver = getattr(importlib.import_module(module_name), attribute)()
    return driver


def runtime_of(recorded: list[str], requested: str | None) -> str:
    """Choose the runtime for a profile: the one asked for, or the only one it was set up with."""
    candidates = sorted(set(recorded) & known())
    if requested is not None:
        if requested not in known():
            raise DriverRefused("unknown_driver", "Arena")
        return requested
    if len(candidates) == 1:
        return candidates[0]
    raise DriverRefused("ambiguous_runtime" if candidates else "unknown_driver", "Arena")
