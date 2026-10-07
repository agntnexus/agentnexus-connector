"""Optional outbound Arena runner: fixed authority, one profile and durable single launch.

Hermes receives game data through private stdio, never the AgentNexus key or a network listener.
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
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentnexus_sdk import bridge, games, hermes_arena
from agentnexus_sdk.errors import AgentNexusError
from agentnexus_sdk.profiles import (
    ProfileRecord,
    _is_reparse_point,
    profile_lock,
    write_json_atomically,
)
from agentnexus_sdk.runtimes import HermesAdapter

STARTS = "/agent-api/v1/arena/start-intents"
STATUSES = frozenset(
    {"offline", "queued", "starting", "playing", "completed", "refused", "expired", "cancelled"}
)
HERMES_REVISION = "287c56e95afe5c528beacb7ca8f7ef0ad6216f2a"
DIAGNOSTICS = hermes_arena.DIAGNOSTICS | frozenset(
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

    Hermes' `run_budget_seconds` advises the model and never interrupts a blocked call, so a bound
    held inside the child could not stop a child that is blocked. This window is held by the parent,
    which also holds the signing key: it opens when the child says a decision began, admits a move
    only while it is open, before its cutoff and while none has been accepted, and at the cutoff
    expires exactly once and kills the child. The child reports how much of the turn it has used;
    the cutoff is that turn's bound, not a fresh one.

    The bound is on admission. A move admitted just before the cutoff is already on its way, bounded
    by the SDK's own per-phase provider timeouts and by the reserve; killing the child cannot
    recall it, and it does not claim to.
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

    def mark_moved(self) -> None:
        """Record an accepted move: a decision makes one."""
        with self._lock:
            self._moved = True

    def allows_move(self) -> bool:
        """Return whether a move may go now: open, unexpired and before the cutoff."""
        with self._lock:
            return (
                self._cutoff is not None and not self._expired and time.monotonic() < self._cutoff
            )

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
            if remaining > 0 and not self._expired:
                self._timer = threading.Timer(
                    remaining, self.expire, kwargs={"generation": generation}
                )
                self._timer.daemon = True
                self._timer.start()
                return
        self.expire()

    def expire(self, duration_ms: int | None = None, *, generation: int | None = None) -> None:
        """Cut the decision off once; later calls and a timer past its decision do nothing."""
        with self._lock:
            if self._expired or (generation is not None and generation != self._generation):
                return
            self._expired = True
            elapsed = self._elapsed_ms()
        self._cut_off(elapsed if duration_ms is None else duration_ms)

    def close(self) -> None:
        """End the decision: the cutoff no longer applies. An expiry already running finishes."""
        with self._lock:
            self._generation += 1
            self._cutoff = None
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
        """Return true for exactly one caller, committing before it may start Hermes."""
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO launches VALUES (?)", (_uuid(intent_id),)
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def close(self) -> None:
        """Release the journal without deleting any launch reservation."""
        self.connection.close()


def hermes_environment(home: Path) -> dict[str, str]:
    """Pass OS essentials only; select the one profile before Hermes is imported."""
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        HERMES_HOME=str(home),
        HERMES_SAFE_MODE="1",
        HERMES_IGNORE_RULES="1",
        HERMES_IGNORE_USER_CONFIG="1",
        PYTHONUTF8="1",
    )
    return environment


@dataclass(frozen=True)
class HermesRun:
    """A verified installed runtime and one isolated credentials home."""

    source: Path
    interpreter: Path
    home: Path

    @classmethod
    def inspect(cls, paths: Any) -> HermesRun:
        """Refuse shared profiles and any runtime source outside the reviewed revision."""
        if paths.isolation != "isolated":
            raise RunnerRefused("Automatic Arena play requires an isolated named Hermes profile.")
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
            version != "0.21.3"
            or revision.stdout.strip() != HERMES_REVISION
            or clean.returncode != 0
        ):
            raise RunnerRefused(
                "This Hermes source has not passed the bounded Arena compatibility review."
            )
        result = cls(
            source, adapter._scanner_interpreter(source), adapter._config().resolve().parent
        )
        probe = subprocess.run(  # noqa: S603 - fixed local runtime or service command
            result.command("--preflight"),
            env=hermes_environment(result.home),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if probe.returncode != 0 or '"bounded": true' not in probe.stdout:
            raise RunnerRefused("Hermes refused the exact three-tool Arena preflight.")
        return result

    def command(self, *arguments: str) -> list[str]:
        """Use Hermes' interpreter with this wheel's standalone compatible adapter."""
        return [
            str(self.interpreter),
            "-I",
            str(Path(__file__).with_name("hermes_arena.py")),
            str(self.source),
            *arguments,
        ]


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
    ):
        if _is_reparse_point(path):
            raise RunnerRefused("Arena storage must remain inside this profile without links.")


class ArenaRunner:
    """Poll as one signed identity; supervise one bounded child through the whole game."""

    def __init__(self, paths: Any, providers: str, runtime: HermesRun) -> None:
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
        self.paths, self.runtime = paths, runtime
        self.child: subprocess.Popen[str] | None = None
        self.worker: threading.Thread | None = None
        self.active: StartIntent | None = None
        self.deadline = 0.0
        self.finished = threading.Event()
        self.terminal = False
        self.playing = False

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
        # Connect Four's run stays within 256; a joined Chess match may send its own whole run's
        # diagnostics, a finite bound derived from its decisions (agntnexus/agentnexus#202).
        diagnostic_limit = 256
        # The provider's turn is the same 60 seconds in both games today, but the bound is the
        # game's own once the join names it (agntnexus/agentnexus#223).
        seconds = hermes_arena.DECISION_SECONDS["connect-four"]
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
                    raise RunnerRefused("Hermes sent an oversized Arena request.")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise RunnerRefused("Hermes sent an invalid Arena request.")
                if "diagnostic" in request:
                    if (
                        set(request) != {"diagnostic", "duration_ms"}
                        or not isinstance(request["diagnostic"], str)
                        or request["diagnostic"] not in hermes_arena.DIAGNOSTICS
                        or type(request["duration_ms"]) is not int
                        or not 0 <= request["duration_ms"] <= 3600000
                    ):
                        raise RunnerRefused("Hermes sent an invalid Arena diagnostic.")
                    diagnostics += 1
                    if diagnostics > diagnostic_limit:
                        raise RunnerRefused("Hermes exceeded the bounded Arena diagnostics.")
                    if request["diagnostic"] == "model_call_started":
                        # The child says a decision began and how much of the turn it has used.
                        if window.is_open:
                            raise RunnerRefused("Hermes opened a decision inside a decision.")
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
                    raise RunnerRefused("Hermes attempted an operation outside this match.")
                try:
                    hermes_arena.bounded_request(
                        operation, {k: v for k, v in request.items() if k != "operation"}
                    )
                except ValueError:
                    raise RunnerRefused(
                        "Hermes attempted an operation outside this match."
                    ) from None
                if operation == "game_move":
                    # A move is forwarded only inside an open decision and before its cutoff, on
                    # this process's clock. Late is refused, never repeated and never replaced.
                    if not window.is_open:
                        raise RunnerRefused("Hermes attempted a move outside a model decision.")
                    if not window.allows_move():
                        diagnostic(intent, "late_move_refused", window.elapsed_ms())
                        window.expire()
                        break
                    if window.moved:
                        raise RunnerRefused("Hermes attempted a second move in one decision.")
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
                        diagnostic_limit = hermes_arena.diagnostic_bound(
                            hermes_arena.DECISIONS["chess"]
                        )
                        seconds = hermes_arena.DECISION_SECONDS["chess"]
                    if operation == "game_join" and not self.playing:
                        self._report("playing")
                        self.playing = True
                    if result.get("status") in {"ended", "aborted"}:
                        self.terminal = True
                    if operation == "game_move":
                        window.mark_moved()
                    if operation in {"game_move", "game_state"}:
                        uncertain = False
                    response = {"result": result}
                except games.GameRefusedError as error:
                    diagnostic(
                        intent,
                        f"{operation}_refused",
                        max(0, min(int((time.monotonic() - started) * 1000), 3600000)),
                    )
                    if operation == "game_move" and error.retryable:
                        uncertain = True
                    response = {"result": {"error": error.code}}
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
        comes first, so a log that cannot be written never leaves a blocked child alive. After it
        this run sends no move, chooses none and is not retried; a move admitted just before the
        cutoff is already on its way and is bounded by the SDK's own timeouts. The supervisor's next
        tick sees a child that ended without a finished game and reports the intent `refused`.
        """
        try:
            with contextlib.suppress(OSError):
                child.kill()  # decision cutoff
        finally:
            diagnostic(intent, "decision_budget_expired", duration_ms)

    def _launch(self, intent: StartIntent) -> None:
        """Claim, reserve durably, then spawn; restart uncertainty never launches twice."""
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
        child = subprocess.Popen(  # noqa: S603 - reviewed interpreter and shipped adapter
            self.runtime.command(),
            env=hermes_environment(self.runtime.home),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            cwd=self.paths.root,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        self.child = child
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
        """Terminate the bounded child and wait, before releasing the profile lock."""
        if self.child is not None:
            if self.child.poll() is None:
                self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()
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
            for intent in intents:
                if intent.status == "queued":
                    self._launch(intent)
                    break

    def run(self) -> None:
        """Hold the profile lock until children stop; updates and removal refuse while busy."""
        try:
            with profile_lock(self.paths.install_root, self.paths.profile):
                while True:
                    try:
                        self.tick()
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


def command(namespace: Any, install_root: Path) -> int:
    """Expose explicit opt-in, preflight, foreground run and scoped service controls."""
    from agentnexus_sdk.connector import Paths

    paths = Paths.for_profile(install_root, namespace.profile)
    profile_storage(paths)
    config = paths.root / "arena" / "service.json"
    action = namespace.arena_action
    if action == "status":
        print(json.dumps({"profile": paths.profile, "enabled": config.is_file()}))
        return 0
    if action == "disable":
        config.unlink(missing_ok=True)
        _service(paths, enable=False)
        return 0
    runtime = HermesRun.inspect(paths)
    if action == "preflight":
        print(json.dumps({"profile": paths.profile, "bounded": True, "hermes": "0.21.3"}))
        return 0
    if action == "enable":
        providers = namespace.providers
        games.provider_origins({games.ENV_PROVIDERS: providers})
        if not paths.state_file.is_file():
            raise RunnerRefused("Complete setup for this profile first.")
        write_json_atomically(config, {"schema_version": 1, "providers": providers})
        _service(paths, enable=True)
        print("Automatic Arena play enabled for this profile.")
        return 0
    if not config.is_file():
        raise RunnerRefused("Enable automatic Arena play for this profile first.")
    document = json.loads(config.read_text(encoding="utf-8"))
    if set(document) != {"schema_version", "providers"} or document["schema_version"] != 1:
        raise RunnerRefused("Unknown Arena service configuration.")
    ArenaRunner(paths, document["providers"], runtime).run()
    return 0


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
