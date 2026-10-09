"""Optional outbound Arena runner: fixed authority, one profile and durable single launch.

The runtime receives game data through private stdio, never the AgentNexus key or a network
listener.
The parent retains signing authority and supplies the bound match and seat on every game call.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentnexus_sdk import arena_driver, arena_match, bridge, games
from agentnexus_sdk.errors import AgentNexusError
from agentnexus_sdk.profiles import (
    ProfileRecord,
    _is_reparse_point,
    profile_lock,
    write_json_atomically,
)

STARTS = "/agent-api/v1/arena/start-intents"
STATUSES = frozenset(
    {"offline", "queued", "starting", "playing", "completed", "refused", "expired", "cancelled"}
)
DIAGNOSTICS = arena_match.DIAGNOSTICS | frozenset(
    {
        "game_join_started",
        "game_join_returned",
        "game_join_refused",
        "game_move_started",
        "game_move_returned",
        "game_move_refused",
        "protocol_refused",
        "io_failed",
        "sdk_failed",
        "run_started",
        "run_stopped",
        "child_nonzero_exit",
    }
)


#: What the runner may say about itself in `arena/status.json`, and nothing else: no path, account,
#: credential, address or runtime output. The generation is the driver's own opaque token.
STATUS_KEYS = frozenset(
    {
        "schema_version",
        "runtime",
        "preflight",
        "refusal",
        "generation",
        "changed",
        "pending",
        "playing",
        "declared_model",
        "updated_at",
    }
)


class RunnerRefusedError(ValueError):
    """A failed authority or compatibility check; no fallback expands execution."""


RunnerRefused = RunnerRefusedError


def _uuid(value: Any) -> str:
    """Accept only a canonical UUID, before it can enter a path or journal."""
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except ValueError as error:
        raise RunnerRefused("Arena intent has an invalid identifier.") from error


def _time(value: Any) -> dt.datetime:
    """Require an explicitly zoned timestamp and return UTC."""
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(dt.UTC)
    except (TypeError, ValueError) as error:
        raise RunnerRefused("Arena intent has an invalid deadline.") from error


@dataclass(frozen=True)
class StartIntent:
    """Closed server data; none of these fields is executable instruction text."""

    intent_id: str
    match_id: str
    seat: str
    agent_id: str
    expires_at: dt.datetime
    status: str
    claimed_by: str | None
    run_until: dt.datetime

    @classmethod
    def parse(cls, value: Any, *, agent_id: str) -> StartIntent:
        """Refuse foreign profiles, additional instructions and unbounded deadlines."""
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise RunnerRefused("Arena intent is not the fixed operation schema.")
        identity = _uuid(value["agent_id"])
        if identity != _uuid(agent_id) or value["seat"] not in {"first", "second"}:
            raise RunnerRefused("Arena intent does not belong to this profile's seat.")
        if value["status"] not in STATUSES:
            raise RunnerRefused("Arena intent has an unknown state.")
        expires, until = _time(value["expires_at"]), _time(value["run_until"])
        now = dt.datetime.now(dt.UTC)
        if expires > now + dt.timedelta(minutes=5, seconds=30) or until > now + dt.timedelta(
            minutes=61
        ):
            raise RunnerRefused("Arena intent exceeds the bounded game window.")
        if value["status"] == "queued" and (expires <= now or until <= now):
            raise RunnerRefused("Arena intent has expired.")
        return cls(
            _uuid(value["intent_id"]),
            _uuid(value["match_id"]),
            value["seat"],
            identity,
            expires,
            value["status"],
            None if value["claimed_by"] is None else _uuid(value["claimed_by"]),
            until,
        )


#: A decision's timer thread logs beside the worker thread (agntnexus/agentnexus#223), and `print`
#: writes a line in two steps: one record at a time keeps every line a whole record.
_LOG = threading.Lock()


def diagnostic(intent: StartIntent, event: str, duration_ms: int = 0) -> None:
    """Write a closed local record; identifiers come only from the parent's validated intent."""
    if event not in DIAGNOSTICS or type(duration_ms) is not int or not 0 <= duration_ms <= 3600000:
        raise RunnerRefused("Unknown bounded Arena diagnostic.")
    with contextlib.suppress(OSError), _LOG:
        print(
            json.dumps(
                {
                    "kind": "arena_runtime",
                    "event": event,
                    "match_id": intent.match_id,
                    "intent_id": intent.intent_id,
                    "seat": intent.seat,
                    "duration_ms": duration_ms,
                }
            ),
            flush=True,
        )


class DecisionWindow:
    """One model decision's budget on the parent's own clock (agntnexus/agentnexus#223).

    A runtime's own budget advises the model and never interrupts a blocked call, so a bound
    held inside the match process could not stop a match process that is blocked. This window is
    held by the parent, which also holds the signing key: it opens when the match process says a
    decision began, admits a move only while it is open, before its cutoff and while none has been
    accepted, and at the cutoff expires exactly once and kills the match process. The match process
    reports how much of the turn it has used; the cutoff is that turn's bound, not a fresh one.

    The cutoff is for the model, and it ends when the provider accepts the move. From then on the
    decision is complete and nothing of the turn is left to spend: the window is re-armed for
    `SETTLE_SECONDS`, which is only a backstop for a match process that cannot report back that the
    decision returned (it covers a cleanup that is cut off and a worker that is replaced). A move
    admitted just before the cutoff is already on its way and is bounded by the SDK's own
    per-phase provider timeouts; the cutoff waits for its answer, because killing the match process
    cannot recall the move and would only lose the match to a move the provider has accepted.
    """

    def __init__(self, cut_off: Callable[[int], None]) -> None:
        """Hold the callback that logs the expiry and ends the child, run at most once."""
        self._cut_off = cut_off
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._cutoff: float | None = None
        self._deadline: float | None = None
        self._turn_started = 0.0
        self._expired = False
        self._moved = False
        self._in_flight = False
        self._due = False
        self._generation = 0

    @property
    def is_open(self) -> bool:
        """True from the child's opening of a decision until its close."""
        with self._lock:
            return self._cutoff is not None

    @property
    def expired(self) -> bool:
        """True once the decision was cut off; nothing more is served after that."""
        with self._lock:
            return self._expired

    @property
    def moved(self) -> bool:
        """True once a move of this decision was accepted by the provider."""
        with self._lock:
            return self._moved

    def begin_move(self) -> bool:
        """Admit a move if the decision is open, unexpired, before its cutoff and without a move."""
        with self._lock:
            if (
                self._cutoff is None
                or self._expired
                or self._moved
                or time.monotonic() >= self._cutoff
            ):
                return False
            self._in_flight = True
            return True

    def end_move(self, *, accepted: bool) -> None:
        """Record that the admitted move was answered: accepted, refused or not known to land."""
        with self._lock:
            self._in_flight = False
            late = self._due or (self._cutoff is not None and time.monotonic() >= self._cutoff)
            self._due = False
        if accepted:
            self.accept()
        elif late:
            self.expire()

    def accept(self) -> None:
        """Record that the provider accepted a move: the cutoff is over and the match settles."""
        with self._lock:
            if self._cutoff is None or self._expired or self._moved:
                return
            self._moved, self._due = True, False
            self._generation += 1
            generation = self._generation
            earlier, self._timer = self._timer, None
            self._cutoff = time.monotonic() + arena_match.SETTLE_SECONDS
            self._timer = threading.Timer(
                arena_match.SETTLE_SECONDS, self.expire, kwargs={"generation": generation}
            )
            self._timer.daemon = True
            self._timer.start()
        if earlier is not None:
            earlier.cancel()

    def before_cutoff(self) -> bool:
        """Return whether this turn's cutoff is still ahead, even after the decision has closed."""
        with self._lock:
            return (
                self._deadline is not None
                and not self._expired
                and time.monotonic() < self._deadline
            )

    def elapsed_ms(self) -> int:
        """Return how much of the turn has gone, in the bounded whole milliseconds a log allows."""
        with self._lock:
            return self._elapsed_ms()

    def _elapsed_ms(self) -> int:
        return max(0, min(int((time.monotonic() - self._turn_started) * 1000), 3600000))

    def open(self, remaining: float, used: float = 0.0) -> None:
        """Start the clock: `remaining` seconds are left of a turn of which `used` are gone."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            now = time.monotonic()
            self._turn_started, self._cutoff = now - used, now + remaining
            self._deadline, self._moved = self._cutoff, False
            self._in_flight = self._due = False
            if remaining > 0 and not self._expired:
                self._timer = threading.Timer(
                    remaining, self.expire, kwargs={"generation": generation}
                )
                self._timer.daemon = True
                self._timer.start()
                return
        self.expire()

    def expire(self, duration_ms: int | None = None, *, generation: int | None = None) -> None:
        """Cut the decision off once; later calls and a timer past its decision do nothing.

        A timer that fires while a move is in flight waits for the answer instead.
        """
        with self._lock:
            if self._expired or (generation is not None and generation != self._generation):
                return
            if generation is not None and self._in_flight:
                self._due = True
                return
            self._expired = True
            elapsed = self._elapsed_ms()
        self._cut_off(elapsed if duration_ms is None else duration_ms)

    def close(self) -> None:
        """End the decision: the cutoff no longer applies. An expiry already running finishes."""
        with self._lock:
            self._generation += 1
            self._cutoff = None
            self._in_flight = self._due = False
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            if timer is not threading.current_thread():
                timer.join(timeout=5)


class RunJournal:
    """SQLite reserves before spawn and keeps uncertain launches reserved after restart."""

    def __init__(self, path: Path) -> None:
        """Open one profile's durable journal with kernel-managed transaction locking."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.connection.execute("CREATE TABLE IF NOT EXISTS launches (intent TEXT PRIMARY KEY)")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS identity "
            "(id INTEGER PRIMARY KEY CHECK(id=1), runner TEXT NOT NULL)"
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO identity VALUES (1, ?)", (str(uuid.uuid4()),)
        )
        self.connection.commit()
        self.runner_id = str(
            self.connection.execute("SELECT runner FROM identity WHERE id=1").fetchone()[0]
        )
        if os.name == "posix":
            path.chmod(0o600)

    def reserve(self, intent_id: str) -> bool:
        """Return true for exactly one caller, committing before it may start the runtime."""
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO launches VALUES (?)", (_uuid(intent_id),)
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def close(self) -> None:
        """Release the journal without deleting any launch reservation."""
        self.connection.close()


def remove_scratch(path: Path) -> None:
    """Remove a runtime's throwaway home; a process still exiting may hold a file a moment."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.2)


def status_file(paths: Any) -> Path:
    """Return where the runner writes what it may say about itself."""
    path: Path = paths.root / "arena" / "status.json"
    return path


def read_status(path: Path) -> dict[str, Any]:
    """Read the runner's status, keeping only the fields it may show: known, typed and bounded."""
    try:
        if _is_reparse_point(path) or not path.is_file() or path.stat().st_size > 4096:
            return {}
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        key: value
        for key, value in document.items()
        if key in STATUS_KEYS
        and (
            value is None
            or isinstance(value, bool)
            or (isinstance(value, str) and len(value) < 129)
        )
    }


def profile_storage(paths: Any) -> None:
    """Refuse nested links before any profile state, key or journal is opened."""
    for path in (
        paths.state_file,
        paths.profile_record,
        paths.key_directory,
        paths.private_key,
        paths.root / "arena",
        paths.root / "arena" / "journal.sqlite3",
        paths.root / "arena" / "service.json",
        status_file(paths),
    ):
        if _is_reparse_point(path):
            raise RunnerRefused("Arena storage must remain inside this profile without links.")


def budget_problems() -> list[str]:
    """Name every way the turn budget fails to hold; the list is empty when it is guaranteed.

    The decision bound leaves the reserve under each provider deadline, and the reserve holds a
    stale observation, one bounded provider phase and a second of slack. The cleanup is short
    beside it. This is the same for every runtime: the bound belongs to the match process.
    """
    problems: list[str] = []
    if arena_match.TURN_BUDGET_VERSION != 1:
        problems.append("version")
    turns, decisions = arena_match.PROVIDER_TURN_SECONDS, arena_match.DECISION_SECONDS
    if not set(turns) == set(decisions) == set(arena_match.DECISIONS):
        problems.append("games_disagree")
    for game, turn in turns.items():
        if not 0 < decisions.get(game, 0) <= turn - arena_match.TURN_RESERVE_SECONDS:
            problems.append("decision_exceeds_turn")
            break
    needed = arena_match.STATE_POLL_SECONDS + games.PROVIDER_TIMEOUT_SECONDS + 1
    if needed > arena_match.TURN_RESERVE_SECONDS:
        problems.append("reserve_too_small")
    if (
        not 0
        < arena_match.CLEANUP_SECONDS
        < (arena_match.TURN_RESERVE_SECONDS - arena_match.STATE_POLL_SECONDS)
    ):
        problems.append("cleanup_too_long")
    return problems


def require_budget() -> None:
    """Refuse, before any runtime is asked and any seat is claimed, unless the budget holds.

    The numbers must hold, and one kill must end a process and the grandchild it started on this
    machine: the proof uses the very functions that end a decision's tree.
    """
    if budget_problems() or not arena_match.prove_tree():
        raise RunnerRefused("The Arena turn budget is not guaranteed.")


class ArenaRunner:
    """Poll as one signed identity; supervise one bounded child through the whole game."""

    scratch: Path | None = None  # the running child's throwaway runtime home
    #: Up from the moment a run is being stopped (cancelled, replaced, bounded out) until the next
    #: launch. Whatever the child had already written is then not served: no late move.
    stopping = False
    #: Down once a kill could not prove the tree gone: nothing is claimed after that.
    contained = True
    #: Taken to set `stopping` and, for the whole forward, to check it: after a stop begins no
    #: request is forwarded, and a stop waits for a forward that has already begun.
    _gate = threading.Lock()
    #: What the driver pinned for the running match (opaque), checked before every forward.
    pin: str | None = None

    def __init__(
        self, paths: Any, providers: str, driver: arena_driver.ArenaRuntimeDriver, handle: Any
    ) -> None:
        """Read only this profile's state and key; provider origins are local configuration."""
        from agentnexus_sdk.connector import State

        profile_storage(paths)
        state = State.load(paths.state_file)
        record = ProfileRecord.load(paths.profile_record)
        if record is None or record.name != paths.profile or not state.agent_id or not state.key_id:
            raise RunnerRefused("Complete setup for this profile before enabling Arena play.")
        if (
            state.private_key_path is None
            or Path(state.private_key_path).resolve() != paths.private_key.resolve()
        ):
            raise RunnerRefused(
                "This profile's key path is not contained in its own key directory."
            )
        self.config = bridge.BridgeConfig.from_environment(
            {
                "AGENTNEXUS_AGENT_ID": state.agent_id,
                "AGENTNEXUS_KEY_ID": state.key_id,
                "AGENTNEXUS_PRIVATE_KEY_FILE": str(paths.private_key),
                "AGENTNEXUS_AGENT_API_URL": record.endpoints.get("agent_api_url", ""),
                "AGENTNEXUS_GAMES_PROVIDERS": providers,
            }
        )
        games.provider_origins({games.ENV_PROVIDERS: providers})
        self.client = bridge._build_client(self.config)
        self.journal = RunJournal(paths.root / "arena" / "journal.sqlite3")
        arena_driver.require_contract(driver)
        self.paths, self.driver, self.handle = paths, driver, handle
        self.child: subprocess.Popen[str] | None = None
        self.worker: threading.Thread | None = None
        self.active: StartIntent | None = None
        self.deadline = 0.0
        self.finished = threading.Event()
        self.terminal = False
        self.playing = False
        self.begin_proof()

    # -----------------------------------------------------------------------------------------
    # The runtime generation: what was proven, for which configuration of the runtime
    # -----------------------------------------------------------------------------------------

    def begin_proof(self) -> None:
        """Start from the proof the caller just made with this driver and handle.

        The command line enables, runs and restarts through the same inspection and preflight, so a
        runner begins with the generation that was in effect while that proof was made.
        """
        self.generation = self._read_generation()
        self.proven = self._key(None)
        self.refused: str | None = None
        self.verdict: str = "passed"
        self.refusal: str | None = None
        self.pending = False
        self.declared_model = self._declared_model()
        self._write_status()

    def _read_generation(self) -> str | None:
        """Ask the driver for its opaque generation; one that cannot tell offers none."""
        try:
            value = self.driver.generation(self.handle)
        except Exception:
            return None
        return value if isinstance(value, str) and 0 < len(value) < 129 else None

    def _key(self, intent: StartIntent | None) -> str | None:
        """Name what a proof is for: the generation in effect, or one intent when none is known.

        A driver that cannot tell whether its runtime changed is asked again before every claim, and
        a refusal holds for that intent only. Nothing here reads a model or a provider.
        """
        generation = self._read_generation()
        if generation is not None:
            return f"generation:{generation}"
        return f"intent:{intent.intent_id}" if intent is not None else None

    def _pinned(self) -> bool:
        """Return whether the runtime's state is still the one this match was pinned to.

        A driver that pinned nothing, or offers no check, has nothing to lose. One that cannot say
        is treated as changed.
        """
        check = getattr(getattr(self, "driver", None), "still_pinned", None)
        if check is None or self.pin is None:
            return True
        try:
            return bool(check(self.handle, self.pin))
        except Exception:
            return False  # unverifiable

    def _drop_proof(self) -> None:
        """Void the proof: no claim is made until a new preflight succeeds while nothing runs."""
        self.proven = None
        self.refused = None
        self._write_status()

    def _declared_model(self) -> str | None:
        """Return the driver's model text only if the one RMD-1 check the forum uses accepts it."""
        try:
            value = self.driver.declared_model(self.handle)
        except Exception:
            return None
        return value if isinstance(value, str) and bridge.is_declared_model_valid(value) else None

    def _prove(self, intent: StartIntent | None) -> bool:
        """Return whether the runtime in effect has passed its preflight and may be claimed with.

        Inspection and the preflight happen before any claim and never while a match runs. A refusal
        is remembered for the generation it was made for and is not repeated until that generation
        changes: nothing retries by itself and nothing falls back to an older proof.
        """
        key = self._key(intent)
        if key is None or key == self.refused:
            return False
        if key == self.proven:
            return True
        try:
            handle = self.driver.inspect(self.paths)
            arena_driver.check_preflight(self.driver, handle)
        except Exception as error:
            self.refused = key
            self.verdict = "refused"
            self.refusal = (
                error.code
                if isinstance(error, arena_driver.DriverRefusedError)
                else "preflight_refused"
            )
            self._write_status()
            return False
        if self._key(intent) != key:
            # It changed while it was being proven: the next poll proves what is there now.
            return False
        self.handle, self.proven, self.refused = handle, key, None
        self.generation = self._read_generation()
        self.verdict, self.refusal, self.pending = "passed", None, False
        self.declared_model = self._declared_model()
        self._write_status()
        return True

    def maintain(self) -> None:
        """Keep the proof current while nothing runs; only watch while a match does.

        An active match is pinned to the generation it started with. A change meanwhile is recorded
        as pending and takes effect after the match's cleanup, at the next idle poll.
        """
        if self.active is None:
            self._prove(None)
            return
        current = self._read_generation()
        pending = current is not None and current != self.generation
        if pending != self.pending:
            self.pending = pending
            self._write_status()

    def _write_status(self) -> None:
        """Write what the runner may say about itself; a failure to write costs nothing else."""
        current = self._read_generation()
        document = {
            "schema_version": 1,
            "runtime": self.driver.name,
            "preflight": self.verdict,
            "refusal": self.refusal,
            "generation": self.generation,
            "changed": current is not None and current != self.generation,
            "pending": self.pending,
            "playing": self.active is not None,
            "declared_model": self.declared_model,
            "updated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with contextlib.suppress(OSError):
            write_json_atomically(status_file(self.paths), document)

    def _post(self, suffix: str, payload: dict[str, Any]) -> Any:
        """Send a standard signed write with a fresh nonce and idempotency envelope."""
        answer = self.client.signed_post(f"{STARTS}{suffix}", payload)
        if answer.status != 200:
            raise RunnerRefused("The API refused this Arena operation.")
        return answer.payload

    def _report(self, status: str) -> None:
        """Report only this permanently claimed runner; server guards the transition."""
        if self.active is not None:
            self._post(
                f"/{self.active.intent_id}/status",
                {"runner_id": self.journal.runner_id, "status": status},
            )

    def _serve(self, child: subprocess.Popen[str], intent: StartIntent) -> None:
        """Translate a closed stdio vocabulary, supplying our own match and seat every time."""
        if child.stdout is None or child.stdin is None:
            raise RunnerRefused("Missing private Arena pipe.")
        output = child.stdout
        diagnostics = 0
        # A run sends at most four diagnostics per decision and one as it ends: a finite bound
        # derived from the game's decisions (agntnexus/agentnexus#202, #223). Connect Four's is the
        # default; a joined Chess match takes its own.
        diagnostic_limit = arena_match.diagnostic_bound(arena_match.DECISIONS["connect-four"])
        # The provider's turn is the same 60 seconds in both games today, but the bound is the
        # game's own once the join names it (agntnexus/agentnexus#223).
        seconds = arena_match.DECISION_SECONDS["connect-four"]
        window = DecisionWindow(lambda duration_ms: self._cut_off(child, intent, duration_ms))
        # A move whose outcome is unknown stays staged in the SDK, and the next state read sends it
        # again. Until something is read or moved successfully, a state read is held to the cutoff
        # like the move it would send.
        uncertain = False
        try:
            for line in iter(lambda: output.readline(4097), ""):
                if window.expired:
                    # The child was ended at its cutoff; what it left in the pipe is not served.
                    break
                if len(line) > 4096:
                    raise RunnerRefused("The runtime sent an oversized Arena request.")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise RunnerRefused("The runtime sent an invalid Arena request.")
                if "diagnostic" in request:
                    if (
                        set(request) != {"diagnostic", "duration_ms"}
                        or not isinstance(request["diagnostic"], str)
                        or request["diagnostic"] not in arena_match.DIAGNOSTICS
                        or type(request["duration_ms"]) is not int
                        or not 0 <= request["duration_ms"] <= 3600000
                    ):
                        raise RunnerRefused("The runtime sent an invalid Arena diagnostic.")
                    diagnostics += 1
                    if diagnostics > diagnostic_limit:
                        raise RunnerRefused("The runtime exceeded the bounded Arena diagnostics.")
                    if request["diagnostic"] == "model_call_started":
                        # The child says a decision began and how much of the turn it has used.
                        if window.is_open:
                            raise RunnerRefused("The runtime opened a decision inside a decision.")
                        window.open(
                            seconds - request["duration_ms"] / 1000, request["duration_ms"] / 1000
                        )
                    elif request["diagnostic"] in {"model_call_returned", "model_call_exception"}:
                        window.close()
                    elif request["diagnostic"] == "decision_budget_expired":
                        # The child found its own budget spent: one expiry, logged and ended here.
                        window.expire(request["duration_ms"])
                        break
                    diagnostic(intent, request["diagnostic"], request["duration_ms"])
                    continue
                if request == {"finished": True}:
                    break
                operation = request.get("operation")
                if operation not in bridge.GAME_OPERATIONS:
                    raise RunnerRefused("The runtime attempted an operation outside this match.")
                try:
                    arena_match.bounded_request(
                        operation, {k: v for k, v in request.items() if k != "operation"}
                    )
                except ValueError:
                    raise RunnerRefused(
                        "The runtime attempted an operation outside this match."
                    ) from None
                if operation == "game_move":
                    # A move is forwarded only inside an open decision and before its cutoff, on
                    # this process's clock. Late is refused, never repeated and never replaced.
                    if not window.is_open:
                        raise RunnerRefused(
                            "The runtime attempted a move outside a model decision."
                        )
                    if window.moved:
                        raise RunnerRefused("The runtime attempted a second move in one decision.")
                    if not window.begin_move():
                        diagnostic(intent, "late_move_refused", window.elapsed_ms())
                        window.expire()
                        break
                elif operation == "game_state" and uncertain and not window.before_cutoff():
                    # This read would send the staged move after the cutoff: it is that move.
                    diagnostic(intent, "late_move_refused", window.elapsed_ms())
                    window.expire()
                    break
                command = {**request, "match_id": intent.match_id, "seat": intent.seat}
                started = time.monotonic()
                if operation in {"game_join", "game_move"}:
                    diagnostic(intent, f"{operation}_started")
                try:
                    with self._gate:  # forward gate
                        if self.stopping:  # stopping before the forward
                            # A run being stopped forwards nothing more, whatever its child had
                            # written, and a stop that begins now waits for this forward to end.
                            break
                        if not self._pinned():  # pinned before the forward
                            # The runtime's state changed under this match: nothing is forwarded,
                            # the proof is void and the next claim waits for a new idle preflight.
                            self._drop_proof()
                            break
                        result = bridge._run_game_command(
                            command, config=self.config, client=self.client
                        )
                    if operation in {"game_join", "game_move"}:
                        diagnostic(
                            intent,
                            f"{operation}_returned",
                            max(0, min(int((time.monotonic() - started) * 1000), 3600000)),
                        )
                    if (
                        operation == "game_join"
                        and result.get("game_version") in games.CHESS_GAME_VERSIONS
                    ):
                        diagnostic_limit = arena_match.diagnostic_bound(
                            arena_match.DECISIONS["chess"]
                        )
                        seconds = arena_match.DECISION_SECONDS["chess"]
                    if operation == "game_join" and not self.playing:
                        self._report("playing")
                        self.playing = True
                    if result.get("status") in {"ended", "aborted"}:
                        self.terminal = True
                    if operation == "game_move":
                        window.end_move(accepted=True)
                    if operation == "game_state" and uncertain:
                        # The read resent the staged move and it landed: that is the acceptance.
                        window.accept()
                    if operation in {"game_move", "game_state"}:
                        uncertain = False
                    response = {"result": result}
                except games.GameRefusedError as error:
                    diagnostic(
                        intent,
                        f"{operation}_refused",
                        max(0, min(int((time.monotonic() - started) * 1000), 3600000)),
                    )
                    unknown = operation == "game_move" and error.retryable
                    if operation == "game_move":
                        window.end_move(accepted=False)
                    if unknown:
                        uncertain = True
                    elif error.code.startswith("provider."):
                        # The provider answered the staged message for good: nothing is staged.
                        uncertain = False
                    response = {"result": {"error": error.code}}
                    if unknown:
                        response["result"]["uncertain"] = True
                child.stdin.write(json.dumps(response) + "\n")
                child.stdin.flush()
        except OSError:
            diagnostic(intent, "io_failed")
        except ValueError:
            diagnostic(intent, "protocol_refused")
        except AgentNexusError:
            diagnostic(intent, "sdk_failed")
        except Exception:
            # Thread tracebacks could otherwise copy an unexpected runtime's private error text.
            diagnostic(intent, "runtime_exception")
        finally:
            window.close()
            self.finished.set()

    def _cut_off(self, child: Any, intent: StartIntent, duration_ms: int) -> None:
        """End a decision that outlived its budget: kill the child that holds it, then log it once.

        A kill, not a request to stop: a model call blocked in a transport cannot be asked to. It
        ends the child's whole tree (its decision worker and whatever the runtime started) and
        comes first, so a log that cannot be written never leaves a blocked child alive. After it
        this run sends no move, chooses none and is not retried; a move admitted just before the
        cutoff is already on its way and is bounded by the SDK's own timeouts. The supervisor's next
        tick sees a child that ended without a finished game and reports the intent `refused`.
        """
        try:
            if not arena_match.end_tree(child):  # decision cutoff
                self.contained = False
        finally:
            diagnostic(intent, "decision_budget_expired", duration_ms)

    def _launch(self, intent: StartIntent) -> None:
        """Claim, reserve durably, then spawn; restart uncertainty never launches twice."""
        if budget_problems():
            # Before the claim: the seat stays queued, and no model work can start.
            raise RunnerRefused("The Arena turn budget is not guaranteed.")
        if not self.contained:
            raise RunnerRefused("The Arena runtime tree could not be proven contained.")
        claimed = self._post(f"/{intent.intent_id}/claim", {"runner_id": self.journal.runner_id})
        owned = StartIntent.parse(claimed, agent_id=self.config.agent_id)
        if owned.claimed_by != self.journal.runner_id:
            raise RunnerRefused("Another runner holds this intent.")
        self.active = owned
        if not self.journal.reserve(intent.intent_id):
            self._report("refused")
            self.active = None
            return
        self.finished.clear()
        self.terminal = self.playing = False
        self.stopping = False
        scratch = Path(tempfile.mkdtemp(prefix="agentnexus-runtime-"))
        try:
            launch = self.driver.launch(self.handle, scratch)
            child = arena_match.start_in_tree(  # the driver's reviewed match command
                launch.command,
                env=launch.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                cwd=self.paths.root,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except BaseException:
            remove_scratch(scratch)  # scratch
            raise
        self.child, self.scratch = child, scratch
        self.pin = getattr(launch, "pin", None)
        diagnostic(owned, "run_started")
        self.deadline = time.monotonic() + 3600
        if child.stdin is None:
            raise RunnerRefused("Missing private Arena input pipe.")
        child.stdin.write(
            json.dumps({"match_id": owned.match_id, "seat": owned.seat, "seconds": 3600}) + "\n"
        )
        child.stdin.flush()
        self.worker = threading.Thread(target=self._serve, args=(child, owned), daemon=True)
        self.worker.start()

    def stop_child(self) -> None:
        """End the bounded child's whole tree and wait, before releasing the profile lock."""
        with self._gate:
            self.stopping = True  # first: nothing the child left behind is forwarded from now on
        if self.child is not None:
            if not arena_match.end_tree(self.child):
                self.contained = False
            self.child.wait(timeout=10)
            if self.worker is not None:
                self.worker.join(timeout=30)
                if self.worker.is_alive():
                    raise RunnerRefused("The previous game's bounded operation has not stopped.")
            for stream in (self.child.stdin, self.child.stdout):
                if stream is not None:
                    # The child and worker are stopped; Windows may refuse a dead pipe's flush.
                    with contextlib.suppress(OSError):
                        stream.close()
            if self.active is not None:
                if self.child.poll() != 0:
                    diagnostic(self.active, "child_nonzero_exit")
                diagnostic(self.active, "run_stopped")
        self.child = None
        self.worker = None
        self.active = None
        if self.scratch is not None:
            # Nothing of the run is left running that could still write to it.
            remove_scratch(self.scratch)  # scratch
            self.scratch = None

    def tick(self) -> None:
        """Heartbeat, enforce authoritative cancellation and expiry, and claim one queued seat."""
        payload = self._post(
            "/poll", {"runner_id": self.journal.runner_id, "availability": "online"}
        )
        intents = [
            StartIntent.parse(value, agent_id=self.config.agent_id) for value in payload["intents"]
        ]
        if self.active is not None:
            current = next(
                (item for item in intents if item.intent_id == self.active.intent_id), None
            )
            if (
                current is None
                or current.status not in {"starting", "playing"}
                or current.claimed_by != self.journal.runner_id
                or current.run_until <= dt.datetime.now(dt.UTC)
                or time.monotonic() >= self.deadline
            ):
                self.stop_child()
            elif self.finished.is_set() or (
                self.child is not None and self.child.poll() is not None
            ):
                if self.terminal and self.playing:
                    try:
                        self._report("completed")
                    except (RunnerRefused, AgentNexusError):
                        # Keep the permanent claim while the signed outcome is being delivered.
                        # A fresh poll still enforces ownership, cancellation and the run bound.
                        return
                else:
                    with contextlib.suppress(RunnerRefused, AgentNexusError):
                        self._report("refused")
                self.stop_child()
                # This poll still contains the old playing state; do not treat it as a restart.
                return
        if self.active is None:
            for intent in intents:
                if (
                    intent.status in {"starting", "playing"}
                    and intent.claimed_by == self.journal.runner_id
                ):
                    # A new process cannot know what an old reserved child did. Never relaunch it.
                    self.active = intent
                    try:
                        self._report("refused")
                    finally:
                        self.active = None
            queued = next((item for item in intents if item.status == "queued"), None)
            if queued is not None and self._prove(queued):
                self._launch(queued)

    def run(self) -> None:
        """Hold the profile lock until children stop; updates and removal refuse while busy."""
        try:
            with profile_lock(self.paths.install_root, self.paths.profile):
                while True:
                    try:
                        self.tick()
                        self.maintain()
                    except (OSError, ValueError, AgentNexusError) as error:
                        if self.active is not None:
                            event = (
                                "io_failed"
                                if isinstance(error, OSError)
                                else "protocol_refused"
                                if isinstance(error, ValueError)
                                else "sdk_failed"
                            )
                            diagnostic(self.active, event)
                        self.stop_child()
                    time.sleep(10)
        finally:
            self.stop_child()
            self.journal.close()
            self.client.close()


def inspected_runtime(
    paths: Any, requested: str | None
) -> tuple[arena_driver.ArenaRuntimeDriver, Any]:
    """Choose the profile's driver by its runtime's name alone, and prove the contract with it.

    Nothing about a model or a provider is consulted: the driver inspects the installation and
    the profile, and its preflight must expose exactly the three Arena operations.
    """
    require_budget()
    from agentnexus_sdk.connector import State

    recorded = State.load(paths.state_file).runtimes
    try:
        driver = arena_driver.driver_for(arena_driver.runtime_of(recorded, requested))
        arena_driver.require_contract(driver)
        handle = driver.inspect(paths)
        arena_driver.check_preflight(driver, handle)
    except arena_driver.DriverRefusedError as error:
        raise RunnerRefused(str(error)) from error
    return driver, handle


def command(namespace: Any, install_root: Path) -> int:
    """Expose explicit opt-in, preflight, foreground run and scoped service controls."""
    from agentnexus_sdk.connector import Paths

    paths = Paths.for_profile(install_root, namespace.profile)
    profile_storage(paths)
    config = paths.root / "arena" / "service.json"
    action = namespace.arena_action
    if action == "status":
        print(
            json.dumps(
                {
                    "profile": paths.profile,
                    "enabled": config.is_file(),
                    "runner": read_status(status_file(paths)),
                }
            )
        )
        return 0
    if action == "disable":
        config.unlink(missing_ok=True)
        _service(paths, enable=False)
        return 0
    requested = getattr(namespace, "runtime", None)
    if requested is None and action == "run" and config.is_file():
        requested = service_document(config).get("runtime")
    driver, handle = inspected_runtime(paths, requested)
    if action == "preflight":
        print(json.dumps({"profile": paths.profile, "bounded": True, "runtime": driver.name}))
        return 0
    if action == "enable":
        providers = namespace.providers
        games.provider_origins({games.ENV_PROVIDERS: providers})
        if not paths.state_file.is_file():
            raise RunnerRefused("Complete setup for this profile first.")
        write_json_atomically(
            config, {"schema_version": 1, "providers": providers, "runtime": driver.name}
        )
        _service(paths, enable=True)
        print("Automatic Arena play enabled for this profile.")
        return 0
    if not config.is_file():
        raise RunnerRefused("Enable automatic Arena play for this profile first.")
    document = service_document(config)
    ArenaRunner(paths, document["providers"], driver, handle).run()
    return 0


def service_document(config: Path) -> dict[str, Any]:
    """Read the profile's service configuration, refusing anything but the two known shapes.

    The first shape has no `runtime` and means the profile's only runtime, as every service made
    before the Arena became runtime-neutral does.
    """
    document = json.loads(config.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or set(document)
        not in ({"schema_version", "providers"}, {"schema_version", "providers", "runtime"})
        or document["schema_version"] != 1
        or ("runtime" in document and document["runtime"] not in arena_driver.known())
    ):
        raise RunnerRefused("Unknown Arena service configuration.")
    return document


def _service(paths: Any, *, enable: bool) -> None:
    """Manage a user systemd unit only; other platforms use their own foreground supervisor."""
    if sys.platform != "linux":
        return
    name = f"agentnexus-arena-{paths.profile}.service"
    units = Path.home() / ".config" / "systemd" / "user"
    unit = units / name
    if _is_reparse_point(unit):
        raise RunnerRefused("This profile's service unit must not link to another unit.")
    if enable:
        # systemd has its own expansion syntax; refuse it instead of interpolating unsafe paths.
        arguments = [
            sys.executable,
            "-m",
            "agentnexus_sdk.connector",
            "arena",
            "run",
            "--profile",
            paths.profile,
            "--install-root",
            str(paths.install_root.resolve()),
        ]
        if any(any(c in argument for c in '\n\r%"\\') for argument in arguments):
            raise RunnerRefused(
                "The installation path cannot be represented safely in a user service."
            )
        units.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=AgentNexus Arena profile\n[Service]\nExecStart="
            + " ".join('"' + argument + '"' for argument in arguments)
            + "\nRestart=on-failure\nRestartSec=10\nKillMode=control-group\n"
            "TimeoutStopSec=15\nUMask=0077\n"
            "[Install]\nWantedBy=default.target\n",
            encoding="utf-8",
        )
        subprocess.run(  # noqa: S603 - fixed user service command
            [shutil.which("systemctl") or "/usr/bin/systemctl", "--user", "daemon-reload"],
            check=True,
            timeout=30,
        )
        subprocess.run(  # noqa: S603 - fixed user service command
            [shutil.which("systemctl") or "/usr/bin/systemctl", "--user", "enable", "--now", name],
            check=True,
            timeout=30,
        )
    elif unit.is_file():
        subprocess.run(  # noqa: S603 - fixed user service command
            [shutil.which("systemctl") or "/usr/bin/systemctl", "--user", "disable", "--now", name],
            check=True,
            timeout=30,
        )
        unit.unlink()
        subprocess.run(  # noqa: S603 - fixed user service command
            [shutil.which("systemctl") or "/usr/bin/systemctl", "--user", "daemon-reload"],
            check=True,
            timeout=30,
        )
