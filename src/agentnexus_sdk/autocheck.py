"""Request-triggered update checking: notice that a release exists, and stop there.

This is C3-B. It answers one question — *is there a newer, authentic release?* — writes the answer
down, and does nothing else. It installs nothing, stages nothing, activates nothing, restarts
nothing and schedules nothing. Moving a profile stays what C2 made it: a command the owner types.

## Why the trigger is here and not in the bridge

The bridge is a short-lived subprocess, and `mcp_server.run_bridge` starts it with
`subprocess.run(..., capture_output=True)`, which waits for the process to exit before the tool
result exists. Anything the bridge did before exiting would therefore be added directly to the
latency of the tool call that triggered it. That rules the bridge out — not as a preference, but
because the call is synchronous.

The MCP server is the other half: a long-lived process whose `serve` loop handles one message at a
time. A check placed **after a response has been written and flushed** cannot affect that response,
because the client already has it.

## What that costs, stated rather than glossed over

`serve` is single-threaded, so a check running between two messages delays the *next* one if it
arrives while the check is in flight. That is bounded three ways and by nothing else:

* it happens at most once per interval, which is at least an hour and defaults to a day;
* the fetch runs under `FETCH_BUDGET_SECONDS`, enforced per request and again before the second
  request is started, so an unresponsive origin cannot hold the loop for a read timeout;
* every failure is caught. Nothing raised here can reach the session.

No thread is used. A worker thread would remove even that bounded delay, but it would put
concurrency into a process that has none today, and the cost it removes is a few seconds once a
day. The trade is not worth the class of bug it invites.

## The trust chain is the loader's, again

Nothing here decides what to trust. `updater.fetch_manifest` fetches and verifies exactly as
`update check` does: signature over the exact bytes before the document is parsed, re-canonicalised
so a reordered document cannot inherit a signature, artifact URLs pinned to the origin, and a build
carrying the key placeholders refusing to verify anything at all. No version comparison against a
package index, and no second opinion.

Two rules govern what happens afterwards, and the difference between them matters more than
anything else in this module:

* **A transient failure backs off.** Offline, DNS, timeout, HTTP 5xx, a truncated download: retry
  later, further away each time.
* **An untrusted answer halts.** A manifest that does not verify, an artifact URL off the origin, a
  replayed older release: stop, record why, and check nothing again until the owner intervenes.
  Retrying an unverifiable manifest on a timer is a loop that cannot succeed and that would bury
  the one condition the signature exists to surface.

## Replay

A signature proves authorship, not freshness. `release.py` validates `released_at` for shape only
and never compares it, so a correctly signed *older* manifest verifies. A person running
`update check` sees the version and notices; an unattended check would not. So this module keeps a
monotonic floor — the highest version and `released_at` it has ever accepted — and treats a
manifest below either as untrusted rather than as "up to date".

## What is never written here

No key, no invitation, no token, no provider credential, no forum content, no agent identity beyond
the profile name the runtime already put in its own configuration. The status document holds
version strings, timestamps, a state, a short error message and the public origin.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from agentnexus_sdk import updater
from agentnexus_sdk.updater import Fetcher, UpdateError

#: Where the answer is kept, beside the profiles rather than inside one: a release is a property of
#: the installation, not of an agent.
STATUS_FILE_NAME: Final = "update-check.json"
LOG_FILE_NAME: Final = "update-check.log"
STATUS_SCHEMA_VERSION: Final = 1

#: Default a day, floor an hour. Releases in this project are rare — eight versions across the
#: connector's whole life — so a daily question is already generous, and the floor exists so a
#: mistyped interval cannot turn an agent into a polling client.
DEFAULT_INTERVAL_SECONDS: Final = 86_400
MINIMUM_INTERVAL_SECONDS: Final = 3_600

#: Spread, as a fraction of the interval. Drawn per check from the ordinary random module and
#: never derived from a handle, agent id or key: a stable per-installation offset would be a
#: fingerprint handed to the origin on every request.
JITTER_FRACTION: Final = 0.15

#: First retry distance after a transient failure, doubled each time and capped at the interval.
BACKOFF_START_SECONDS: Final = 3_600

#: The whole network budget for one check, both requests together.
FETCH_BUDGET_SECONDS: Final = 8.0
CONNECT_TIMEOUT_SECONDS: Final = 3.0
READ_TIMEOUT_SECONDS: Final = 4.0

#: How often this process is even willing to look at the status file. Keeps the per-request cost at
#: zero for all but one message a minute, whatever the interval is set to.
LOCAL_POLL_SECONDS: Final = 60.0

MAX_LOG_BYTES: Final = 64 * 1024
MAX_MESSAGE_CHARS: Final = 200

STATE_DISABLED: Final = "disabled"
STATE_NEVER_CHECKED: Final = "never-checked"
STATE_UP_TO_DATE: Final = "up-to-date"
STATE_AVAILABLE: Final = "available"
STATE_BACKOFF: Final = "backoff"
STATE_HALTED: Final = "halted"
STATE_BLOCKED: Final = "blocked"

#: Transient: retry later.
CODE_OFFLINE: Final = "offline"
CODE_ORIGIN: Final = "origin-error"
#: Untrusted: stop until the owner intervenes.
CODE_UNTRUSTED: Final = "untrusted"
CODE_REPLAY: Final = "replay"
CODE_UNSUPPORTED_BUILD: Final = "unsupported-build"

TRANSIENT_CODES: Final = frozenset({CODE_OFFLINE, CODE_ORIGIN})
HALTING_CODES: Final = frozenset({CODE_UNTRUSTED, CODE_REPLAY, CODE_UNSUPPORTED_BUILD})

DEFAULT_ORIGIN: Final = "https://agntnexus.com"

#: Control characters and anything else that could confuse a terminal or a log reader.
_UNPRINTABLE: Final = re.compile(r"[\x00-\x1f\x7f]")


class TransientFetchError(UpdateError):
    """A network or origin problem, as opposed to something that failed to verify.

    It subclasses `UpdateError` so `updater.fetch_manifest` re-raises it untouched rather than
    folding it into its own generic message. That is what lets this module tell "the origin was
    unreachable" from "the manifest did not verify" without reading prose out of an exception.
    """

    def __init__(self, message: str, *, code: str) -> None:
        """Carry the classification beside the message."""
        super().__init__(message)
        self.code = code


def now_utc() -> dt.datetime:
    """Return the current time, timezone-aware, as everything persisted here is."""
    return dt.datetime.now(dt.UTC)


def format_time(moment: dt.datetime) -> str:
    """Render a moment the way every timestamp in this document is stored: UTC, second precision."""
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Kept as the module-internal spelling so the many uses below stay short.
_format = format_time


def _parse(value: str | None) -> dt.datetime | None:
    """Read a stored timestamp back, returning None for anything unreadable."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _sanitise(message: str) -> str:
    """Reduce a message to something safe to keep in a file and print to a terminal."""
    flattened = _UNPRINTABLE.sub(" ", message).strip()
    if len(flattened) > MAX_MESSAGE_CHARS:
        return flattened[: MAX_MESSAGE_CHARS - 1] + "…"
    return flattened


# ---------------------------------------------------------------------------------------------
# The status document
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    """What is known locally about releases. Every field is a version, a time, or a short label."""

    enabled: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    origin: str = DEFAULT_ORIGIN
    state: str = STATE_NEVER_CHECKED
    enabled_changed_at: str | None = None
    last_check_at: str | None = None
    next_check_after: str | None = None
    #: The version the origin published at the last successful check.
    available_version: str | None = None
    available_released_at: str | None = None
    #: The highest version whose manifest has ever verified here, and its release time. Together
    #: they are the replay floor: nothing below either is ever accepted again.
    floor_version: str | None = None
    floor_released_at: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    last_error_at: str | None = None
    consecutive_failures: int = 0
    #: The connector version that ran the last check. A scheduled or registered command names one
    #: version's executable for as long as nobody edits it, so this is how a stale one becomes
    #: visible instead of merely being true.
    checked_by_version: str | None = None
    #: Which available version the owner has already been told about, so the notice appears once.
    announced_version: str | None = None
    announced_halt: bool = False

    @property
    def owner_action_required(self) -> bool:
        """Report whether nothing further happens until a person does something."""
        return self.state in {STATE_HALTED, STATE_AVAILABLE}

    def to_document(self) -> dict[str, Any]:
        """Serialise. No secret can reach this document: no field here can hold one."""
        document: dict[str, Any] = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "origin": self.origin,
            "state": self.state,
            "enabled_changed_at": self.enabled_changed_at,
            "last_check_at": self.last_check_at,
            "next_check_after": self.next_check_after,
            "available_version": self.available_version,
            "available_released_at": self.available_released_at,
            "floor_version": self.floor_version,
            "floor_released_at": self.floor_released_at,
            "last_error_code": self.last_error_code,
            "last_error_message": self.last_error_message,
            "last_error_at": self.last_error_at,
            "consecutive_failures": self.consecutive_failures,
            "checked_by_version": self.checked_by_version,
            "announced_version": self.announced_version,
            "announced_halt": self.announced_halt,
        }
        return document

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> Status:
        """Read a status back, ignoring anything a different version wrote."""
        return cls(
            enabled=bool(document.get("enabled", False)),
            interval_seconds=int(document.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)),
            origin=str(document.get("origin", DEFAULT_ORIGIN)),
            state=str(document.get("state", STATE_NEVER_CHECKED)),
            enabled_changed_at=_optional_str(document.get("enabled_changed_at")),
            last_check_at=_optional_str(document.get("last_check_at")),
            next_check_after=_optional_str(document.get("next_check_after")),
            available_version=_optional_str(document.get("available_version")),
            available_released_at=_optional_str(document.get("available_released_at")),
            floor_version=_optional_str(document.get("floor_version")),
            floor_released_at=_optional_str(document.get("floor_released_at")),
            last_error_code=_optional_str(document.get("last_error_code")),
            last_error_message=_optional_str(document.get("last_error_message")),
            last_error_at=_optional_str(document.get("last_error_at")),
            consecutive_failures=int(document.get("consecutive_failures", 0)),
            checked_by_version=_optional_str(document.get("checked_by_version")),
            announced_version=_optional_str(document.get("announced_version")),
            announced_halt=bool(document.get("announced_halt", False)),
        )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def status_path(install_root: Path) -> Path:
    """Return where the status document lives for this installation."""
    return Path(install_root) / STATUS_FILE_NAME


def log_path(install_root: Path) -> Path:
    """Return where the bounded event log lives for this installation."""
    return Path(install_root) / LOG_FILE_NAME


def load(install_root: Path) -> Status | None:
    """Read the status, or return None when automatic checking was never enabled here.

    None is not an error and is not the same as a disabled status: it means no file exists, so
    nothing has ever been configured. Every existing installation is in that state, and it is why
    this whole module is inert until somebody opts in — there is no file to read, nothing is
    written, and no directory is created.
    """
    path = status_path(install_root)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema_version") != STATUS_SCHEMA_VERSION:
        return None
    return Status.from_document(document)


def save(install_root: Path, status: Status) -> None:
    """Persist the status atomically, so an interrupted write cannot truncate it."""
    from agentnexus_sdk.profiles import write_json_atomically

    write_json_atomically(status_path(install_root), status.to_document())


def append_event(install_root: Path, *, state: str, code: str | None, message: str) -> None:
    """Append one bounded line to the local log, rotating once when it grows past the cap.

    Local only. Nothing here is sent anywhere: no telemetry, no webhook, no mail, no notification
    service and no forum post. A failure to write the log is swallowed, because a log that cannot
    be written must not become the reason a check reports failure.
    """
    path = log_path(install_root)
    line = f"{_format(now_utc())}\t{state}\t{code or '-'}\t{_sanitise(message)}\n"
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size + len(line.encode("utf-8")) > MAX_LOG_BYTES:
            # One generation kept. Two files with a hard cap each is a bounded amount of disk
            # forever, which a date-stamped rotation would not be.
            os.replace(path, path.with_suffix(path.suffix + ".1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


# ---------------------------------------------------------------------------------------------
# Deciding whether to look at all
# ---------------------------------------------------------------------------------------------


def due(status: Status | None, *, now: dt.datetime) -> bool:
    """Report whether a check may run now.

    False for every reason that must not reach the network: never configured, switched off, halted
    after an untrusted answer, or simply not due yet.
    """
    if status is None or not status.enabled or status.state == STATE_HALTED:
        return False
    scheduled = _parse(status.next_check_after)
    return scheduled is None or now >= scheduled


def _next_time(status: Status, *, now: dt.datetime, jitter: Callable[[float], float]) -> str:
    """Return when the next check becomes due after a successful one."""
    interval = max(status.interval_seconds, MINIMUM_INTERVAL_SECONDS)
    return _format(now + dt.timedelta(seconds=interval + jitter(interval * JITTER_FRACTION)))


def _backoff_time(status: Status, *, now: dt.datetime, jitter: Callable[[float], float]) -> str:
    """Return when the next attempt becomes due after a transient failure.

    Doubling from an hour, capped at the interval so backoff can never push a check further away
    than the ordinary schedule would.
    """
    interval = max(status.interval_seconds, MINIMUM_INTERVAL_SECONDS)
    distance = min(BACKOFF_START_SECONDS * (2 ** max(status.consecutive_failures - 1, 0)), interval)
    return _format(now + dt.timedelta(seconds=distance + jitter(distance * JITTER_FRACTION)))


def default_jitter(span: float) -> float:
    """Return a spread within `span`, drawn fresh each time."""
    return random.uniform(0.0, max(span, 0.0))  # noqa: S311 - scheduling spread, not a secret


# ---------------------------------------------------------------------------------------------
# Interpreting an answer
# ---------------------------------------------------------------------------------------------


def _refuses_replay(status: Status, *, version: str, released_at: str) -> str | None:
    """Return why this manifest is a rollback, or None when it moves forward.

    A signature says who published a document, never when it was served. Without this, a correctly
    signed older manifest — replayed by anything sitting between the origin and this machine —
    would answer every automatic check indefinitely, and nobody is watching an automatic check.
    """
    moment = _parse(released_at)
    if moment is None:
        return f"the manifest's released_at ({released_at!r}) is not a time this can compare"
    if status.floor_version is not None and updater.version_key(version) < updater.version_key(
        status.floor_version
    ):
        return f"{version} is older than {status.floor_version}, which this installation has seen"
    floor_moment = _parse(status.floor_released_at)
    if floor_moment is not None and moment < floor_moment:
        return f"{version} was released before {status.floor_released_at}, which this has seen"
    return None


def evaluate(
    status: Status,
    *,
    version: str,
    released_at: str,
    installed: tuple[str, ...],
    now: dt.datetime,
    jitter: Callable[[float], float],
    checked_by_version: str | None,
) -> Status:
    """Fold one verified manifest into the status. Pure: it installs and downloads nothing."""
    replay = _refuses_replay(status, version=version, released_at=released_at)
    if replay is not None:
        return record_failure(status, code=CODE_REPLAY, message=replay, now=now, jitter=jitter)

    highest_installed = max(installed, key=updater.version_key, default=None)
    up_to_date = highest_installed is not None and updater.version_key(
        highest_installed
    ) >= updater.version_key(version)
    state = STATE_UP_TO_DATE if up_to_date else STATE_AVAILABLE

    floor_version = version
    if status.floor_version is not None and updater.version_key(
        status.floor_version
    ) > updater.version_key(version):  # pragma: no cover - refused as replay above
        floor_version = status.floor_version
    floor_released = released_at
    previous_floor = _parse(status.floor_released_at)
    if previous_floor is not None and previous_floor > (_parse(released_at) or previous_floor):
        floor_released = status.floor_released_at or released_at

    return replace(
        status,
        state=state,
        last_check_at=_format(now),
        next_check_after=_next_time(status, now=now, jitter=jitter),
        available_version=version,
        available_released_at=released_at,
        floor_version=floor_version,
        floor_released_at=floor_released,
        last_error_code=None,
        last_error_message=None,
        last_error_at=None,
        consecutive_failures=0,
        checked_by_version=checked_by_version or status.checked_by_version,
        # A different version than the one already announced re-arms the notice; the same one does
        # not, which is what makes the message appear once rather than on every check.
        announced_version=status.announced_version if status.announced_version == version else None,
        announced_halt=False,
    )


def record_failure(
    status: Status,
    *,
    code: str,
    message: str,
    now: dt.datetime,
    jitter: Callable[[float], float],
) -> Status:
    """Fold a failure into the status, backing off or halting according to its class."""
    halting = code in HALTING_CODES
    failures = status.consecutive_failures + 1
    updated = replace(
        status,
        state=STATE_HALTED if halting else STATE_BACKOFF,
        last_check_at=_format(now),
        last_error_code=code,
        last_error_message=_sanitise(message),
        last_error_at=_format(now),
        consecutive_failures=failures,
        announced_halt=False if halting else status.announced_halt,
    )
    if halting:
        # No next time at all. A halted check is not retried on a schedule; it waits for a person,
        # which is the entire difference between a transient failure and an untrusted answer.
        return replace(updated, next_check_after=None)
    return replace(updated, next_check_after=_backoff_time(updated, now=now, jitter=jitter))


# ---------------------------------------------------------------------------------------------
# Fetching, bounded
# ---------------------------------------------------------------------------------------------


def bounded_fetcher(
    *, budget_seconds: float = FETCH_BUDGET_SECONDS, monotonic: Callable[[], float] = time.monotonic
) -> Fetcher:
    """Return a fetcher that gives up quickly, so a slow origin cannot hold the session's loop.

    `updater.https_fetcher` is built for a person waiting at a terminal and allows a 30-second
    read. That is the right choice there and the wrong one here: this runs between two messages of
    a live session, so the budget is small and it is a deadline on the whole check — tested before
    each request *and* between chunks of the one in flight. The per-request timeouts stay, but they
    are not the bound: a read timeout limits the gap between two chunks, not their number.

    Failures are raised as `TransientFetchError`. Everything that reaches the caller as a plain
    `UpdateError` therefore came from verification, which is what lets the classification above be
    structural instead of a search through error prose.
    """
    import httpx2 as httpx

    from agentnexus_sdk.version import USER_AGENT

    started = monotonic()

    def fetch(url: str, limit: int) -> bytes:
        if not url.startswith("https://"):
            # Not transient: an origin that is not HTTPS is a configuration this must not follow,
            # and retrying it later would only repeat the refusal.
            message = f"Refusing to fetch a release over a non-HTTPS address: {url}"
            raise UpdateError(message)
        if monotonic() - started > budget_seconds:
            message = f"The update check's {budget_seconds:.0f} second budget was used up."
            raise TransientFetchError(message, code=CODE_OFFLINE)
        chunks: list[bytes] = []
        total = 0
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(
                        connect=CONNECT_TIMEOUT_SECONDS,
                        read=READ_TIMEOUT_SECONDS,
                        write=CONNECT_TIMEOUT_SECONDS,
                        pool=CONNECT_TIMEOUT_SECONDS,
                    ),
                    follow_redirects=False,
                    headers={"User-Agent": USER_AGENT},
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code != 200:
                    message = f"{url} answered HTTP {response.status_code}."
                    raise TransientFetchError(message, code=CODE_ORIGIN)
                for chunk in response.iter_bytes():
                    # The deadline belongs *inside* the download, not only in front of it. A read
                    # timeout bounds the gap between two chunks and says nothing about how many
                    # chunks there are, so an origin that trickles bytes satisfies every individual
                    # read and still never finishes — and `serve` handles one message at a time, so
                    # what it holds is the agent's next tool call.
                    if monotonic() - started > budget_seconds:
                        message = (
                            f"The update check's {budget_seconds:.0f} second budget was used up."
                        )
                        raise TransientFetchError(message, code=CODE_OFFLINE)
                    total += len(chunk)
                    if total > limit:
                        message = f"{url} returned more than the {limit} bytes this accepts."
                        raise TransientFetchError(message, code=CODE_ORIGIN)
                    chunks.append(chunk)
        except TransientFetchError:
            raise
        except Exception as error:  # every transport failure is the same answer
            message = f"{url} could not be fetched: {type(error).__name__}."
            raise TransientFetchError(message, code=CODE_OFFLINE) from error
        return _checked_signature(url, b"".join(chunks))

    return fetch


def _checked_signature(url: str, body: bytes) -> bytes:
    """Reject a signature that is not hex here, where it is still a transport problem.

    `updater.fetch_manifest` also rejects it, but as an undifferentiated `UpdateError`, and this
    module would then have to treat a truncated download as an untrusted answer and halt. A flaky
    connection would switch automatic checking off permanently. The check is on the *shape* of the
    download and decides nothing about trust: `verify_manifest` still performs the verification.
    """
    if not url.endswith(".sig"):
        return body
    try:
        signature = bytes.fromhex(body.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as error:
        message = f"{url} did not return hex text; the download looks truncated."
        raise TransientFetchError(message, code=CODE_ORIGIN) from error
    if len(signature) != 64:
        message = f"{url} returned {len(signature)} signature bytes rather than 64."
        raise TransientFetchError(message, code=CODE_ORIGIN)
    return body


# ---------------------------------------------------------------------------------------------
# The whole operation
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What one triggered check did. `notice` is the one line a person may be shown."""

    state: str
    checked: bool
    notice: str | None = None


def notice_for(status: Status) -> str | None:
    """Return the single line worth telling somebody, or None when there is nothing new.

    Deliberately short and deliberately not repeated: it is emitted once per available version and
    once per halt, and it never carries the result of anything the agent asked for.
    """
    if status.state == STATE_HALTED and not status.announced_halt:
        detail = status.last_error_message or "the reason was not recorded"
        return (
            f"AgentNexus: automatic update checking stopped — {detail} "
            "Run 'agentnexus-connector update status' for the detail."
        )
    if status.state == STATE_AVAILABLE and status.announced_version != status.available_version:
        return (
            f"AgentNexus: connector {status.available_version} is available. "
            "Nothing was installed. Run 'agentnexus-connector update apply --profile <name>' "
            "when it suits you."
        )
    return None


def announced(status: Status) -> Status:
    """Return the status with the current notice marked as delivered."""
    if status.state == STATE_HALTED:
        return replace(status, announced_halt=True)
    if status.state == STATE_AVAILABLE:
        return replace(status, announced_version=status.available_version)
    return status  # pragma: no cover - only called when a notice exists


def _classified(fetch: Fetcher) -> Fetcher:
    """Wrap a fetcher so every transport failure arrives carrying a code.

    `updater.fetch_manifest` folds any non-`UpdateError` from a fetcher into one generic message,
    which would leave this module unable to tell a dead network from a bad signature and — because
    the safe default is to halt — would switch checking off after a transport hiccup. Wrapping the
    fetcher instead of reading that message keeps the classification structural whatever fetcher a
    caller injects, `bounded_fetcher` included.
    """

    def wrapped(url: str, limit: int) -> bytes:
        try:
            return fetch(url, limit)
        except UpdateError:
            raise
        except Exception as error:  # any transport failure is the same answer
            message = f"{url} could not be fetched: {type(error).__name__}."
            raise TransientFetchError(message, code=CODE_OFFLINE) from error

    return wrapped


def perform_check(
    install_root: Path,
    status: Status,
    *,
    fetch: Fetcher,
    system: str,
    now: dt.datetime,
    jitter: Callable[[float], float],
    checked_by_version: str | None,
) -> Status:
    """Run one check against an already-loaded status and return the new one.

    The caller holds the check lock. Nothing is downloaded but the manifest and its signature: this
    never fetches a wheel, because staging is not part of C3-B.
    """
    try:
        # Asked before anything is fetched, so an unstamped build is recognised for what it is
        # rather than as an untrusted origin, and so it never makes the request at all.
        updater.trusted_release_key()
    except UpdateError as error:
        return record_failure(
            status, code=CODE_UNSUPPORTED_BUILD, message=str(error), now=now, jitter=jitter
        )

    try:
        manifest = updater.fetch_manifest(status.origin, fetch=_classified(fetch))
    except TransientFetchError as error:
        return record_failure(status, code=error.code, message=str(error), now=now, jitter=jitter)
    except UpdateError as error:
        # Everything the fetcher could raise arrives above carrying a code, and the unstamped build
        # was ruled out already. What is left is the trust chain refusing: a bad signature, a
        # re-canonicalisation mismatch, or an artifact URL off the origin.
        return record_failure(
            status, code=CODE_UNTRUSTED, message=str(error), now=now, jitter=jitter
        )

    installed = tuple(updater.installed_versions(install_root, system=system))
    return evaluate(
        status,
        version=manifest.connector_version,
        released_at=manifest.released_at,
        installed=installed,
        now=now,
        jitter=jitter,
        checked_by_version=checked_by_version,
    )


def maybe_check(
    install_root: Path,
    *,
    fetch: Fetcher | None = None,
    system: str,
    now: Callable[[], dt.datetime] = now_utc,
    jitter: Callable[[float], float] = default_jitter,
    checked_by_version: str | None = None,
) -> Outcome:
    """Check if one is due, and never raise whatever happens.

    This is the function a request path calls. It is written so that the worst outcome of anything
    going wrong — a corrupt status file, an unwritable disk, an origin that hangs, a bug in this
    module — is that no check happens.
    """
    try:
        return _maybe_check(
            Path(install_root),
            fetch=fetch,
            system=system,
            now=now,
            jitter=jitter,
            checked_by_version=checked_by_version,
        )
    except Exception:  # a request must never fail because of an update check
        return Outcome(state=STATE_DISABLED, checked=False)


def _maybe_check(
    install_root: Path,
    *,
    fetch: Fetcher | None,
    system: str,
    now: Callable[[], dt.datetime],
    jitter: Callable[[float], float],
    checked_by_version: str | None,
) -> Outcome:
    """Do the work `maybe_check` guards."""
    from agentnexus_sdk.profiles import ProfileError, update_check_lock

    status = load(install_root)
    if status is None:
        return Outcome(state=STATE_DISABLED, checked=False)

    moment = now()
    if not due(status, now=moment):
        # Still worth delivering a notice that was recorded earlier and never shown.
        return Outcome(state=status.state, checked=False, notice=_deliver(install_root, status))

    try:
        with update_check_lock(install_root):
            # Re-read under the lock: another process may have completed a check between the
            # decision above and the lock being granted, and repeating its network call would be
            # exactly the polling this module exists to avoid.
            current = load(install_root) or status
            if not due(current, now=moment):
                return Outcome(
                    state=current.state, checked=False, notice=_deliver(install_root, current)
                )
            updated = perform_check(
                install_root,
                current,
                fetch=fetch if fetch is not None else bounded_fetcher(),
                system=system,
                now=moment,
                jitter=jitter,
                checked_by_version=checked_by_version,
            )
            save(install_root, updated)
            append_event(
                install_root,
                state=updated.state,
                code=updated.last_error_code,
                message=updated.last_error_message
                or f"available {updated.available_version or '-'}",
            )
            return Outcome(
                state=updated.state, checked=True, notice=_deliver(install_root, updated)
            )
    except ProfileError:
        # Another check holds the lock. Not a failure, so it does not count toward backoff and
        # does not move the schedule: the run that holds the lock is doing the work.
        return Outcome(state=STATE_BLOCKED, checked=False)


def _deliver(install_root: Path, status: Status) -> str | None:
    """Return the pending notice and record that it was shown, or None."""
    notice = notice_for(status)
    if notice is None:
        return None
    with contextlib.suppress(OSError):
        save(install_root, announced(status))
    return notice


def record_installed_version(install_root: Path, version: str) -> None:
    """Tell the status that this version is now installed here. Best effort, never raising.

    `update apply` calls this after a successful install. Without it the status would keep saying
    a release is available after the owner had already installed it, and the one-time notice would
    reappear for a version that is on disk.

    It is also the only place the floor is raised without a manifest, which is safe in the one
    direction that matters: a version whose signature and digest `update apply` has just verified
    is at least as trustworthy as one a check merely read about. The floor rises even while the
    check is halted; the *state* does not, because those are different claims.

    Nothing here may fail the command that called it. An update that installed correctly has
    succeeded, whatever happens to a bookkeeping file afterwards.
    """
    from agentnexus_sdk.profiles import ProfileError, update_check_lock

    try:
        with update_check_lock(install_root):
            status = load(install_root)
            if status is None:
                return
            raised = status.floor_version
            if raised is None or updater.version_key(version) > updater.version_key(raised):
                raised = version
            state = status.state
            # Never out of a halt. A halt records that the *origin* served something that did not
            # verify, and installing a release answers a different question: the manifest this
            # command verified is not the one that failed. Clearing it here would resume automatic
            # checking without the owner ever acknowledging why it stopped, and would take the
            # "Owner action required" line out of `update status` while the reason was still
            # unresolved. `update auto --resume` is the one command that leaves this state.
            if (
                state != STATE_HALTED
                and status.available_version is not None
                and updater.version_key(version) >= updater.version_key(status.available_version)
            ):
                state = STATE_UP_TO_DATE
            save(
                install_root,
                replace(
                    status,
                    state=state,
                    floor_version=raised,
                    # The notice is for a release the owner has not acted on. They just did.
                    announced_version=status.available_version,
                ),
            )
    except (ProfileError, OSError):
        return


# ---------------------------------------------------------------------------------------------
# Locating the installation from inside a running MCP server
# ---------------------------------------------------------------------------------------------

#: `<install root>/connector/<version>/venv/<bin>/<executable>` — five levels between the running
#: executable and the installation root.
_PACKAGED_DEPTH: Final = 5


def installation_from_executable(executable: str | None) -> tuple[Path, str] | None:
    """Return the install root and running version for a packaged connector, or None.

    Deliberately conservative, in the same way `updater.version_from_command` is: it answers only
    when the executable really sits inside a `connector/<version>/venv/` tree. A development
    checkout, a `pipx` install or anything else returns None, and automatic checking is then simply
    not available rather than guessing at a root and writing files into it.
    """
    if not executable:
        return None
    try:
        resolved = Path(executable).resolve()
    except OSError:  # pragma: no cover - defensive
        return None
    parents = resolved.parents
    if len(parents) < _PACKAGED_DEPTH:
        return None
    version_directory = parents[2]
    if parents[1].name != "venv" or parents[3].name != "connector":
        return None
    return parents[4], version_directory.name


#: Guards how often a long-lived process even looks at the status file. Module level because the
#: MCP server is one process handling many messages, and the point is that the common case costs
#: nothing at all.
_next_local_poll: float = 0.0


def poll_is_allowed(*, monotonic: Callable[[], float] = time.monotonic) -> bool:
    """Report whether this process may consult the status file again yet.

    The interval decides when a *check* happens; this decides how often the question is even
    asked. Without it a busy session would read and parse a JSON file on every single tool call to
    be told "not due" every time.
    """
    global _next_local_poll
    current = monotonic()
    if current < _next_local_poll:
        return False
    _next_local_poll = current + LOCAL_POLL_SECONDS
    return True


def reset_local_poll() -> None:
    """Forget the local poll gate. For tests, which must not inherit another test's clock."""
    global _next_local_poll
    _next_local_poll = 0.0
