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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentnexus_sdk import bridge, games
from agentnexus_sdk.errors import AgentNexusError
from agentnexus_sdk.profiles import ProfileRecord, profile_lock, write_json_atomically
from agentnexus_sdk.runtimes import HermesAdapter

STARTS = "/agent-api/v1/arena/start-intents"
STATUSES = frozenset(
    {"offline", "queued", "starting", "playing", "completed", "refused", "expired", "cancelled"}
)
HERMES_REVISION = "287c56e95afe5c528beacb7ca8f7ef0ad6216f2a"


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


class ArenaRunner:
    """Poll as one signed identity; supervise one bounded child through the whole game."""

    def __init__(self, paths: Any, providers: str, runtime: HermesRun) -> None:
        """Read only this profile's state and key; provider origins are local configuration."""
        from agentnexus_sdk.connector import State

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
        try:
            for line in iter(lambda: output.readline(4097), ""):
                if len(line) > 4096:
                    raise RunnerRefused("Hermes sent an oversized Arena request.")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise RunnerRefused("Hermes sent an invalid Arena request.")
                if request == {"finished": True}:
                    break
                operation = request.get("operation")
                expected = {"operation", "column"} if operation == "game_move" else {"operation"}
                if operation not in bridge.GAME_OPERATIONS or set(request) != expected:
                    raise RunnerRefused("Hermes attempted an operation outside this match.")
                command = {**request, "match_id": intent.match_id, "seat": intent.seat}
                try:
                    result = bridge._run_game_command(
                        command, config=self.config, client=self.client
                    )
                    if operation == "game_join" and not self.playing:
                        self._report("playing")
                        self.playing = True
                    if result.get("status") in {"ended", "aborted"}:
                        self.terminal = True
                    response = {"result": result}
                except games.GameRefusedError as error:
                    response = {"result": {"error": error.code}}
                child.stdin.write(json.dumps(response) + "\n")
                child.stdin.flush()
        except (OSError, ValueError, AgentNexusError):
            pass
        finally:
            self.finished.set()

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
                    stream.close()
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
                with contextlib.suppress(RunnerRefused, AgentNexusError):
                    self._report("completed" if self.terminal and self.playing else "refused")
                self.stop_child()
        if self.active is None:
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
                    except (OSError, ValueError, AgentNexusError):
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
