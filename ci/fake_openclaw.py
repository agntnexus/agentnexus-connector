r"""Stand-in for the OpenClaw CLI, for Connector CI and local tests.

Run it as a script: ``python fake_openclaw.py <openclaw arguments>``.  It emulates exactly the
surface the Arena driver uses, as observed on a real OpenClaw 2026.9.9:

* ``--version``, ``config file``, ``config validate``, ``config get PATH --json``;
* ``models status [--check] [--plain]``;
* ``mcp probe NAME --json`` (a real stdio MCP handshake against ``mcp.servers.NAME``);
* ``agent exec`` -- one embedded turn with the real process topology.

Topology of ``agent exec`` (what the driver's kill ladder has to cope with)::

    launcher -> child -> runtime (own session on POSIX) -> bridge (the MCP server, own session)

The launcher and the child forward SIGTERM to their child and wait, like ``runRespawnedChild``.
SIGKILL of the launcher alone leaves child and runtime running, like the real thing.  The runtime
speaks OpenAI chat-completions over HTTP (streaming) to ``models.providers.<p>.baseUrl`` and runs
the model's tool calls through the MCP bridge.

Environment knobs (all optional):

``FAKE_OPENCLAW_RECORD``
    Path of a JSON-lines file.  Every process appends its role and pids; every model request
    appends the model-visible tool names, the message roles and the user message.
``FAKE_OPENCLAW_FAULT``
    Comma list of faults: ``hang``, ``slow=N``, ``ignore_sigterm``, ``leak_child``,
    ``cleanup_fail``, ``die_mid_call``, ``second_move``, ``garbage_stdout``, ``write_outside``,
    ``emit_hostname``, ``no_auth``, ``extra_tool_in_request``.
``FAKE_OPENCLAW_OUTSIDE``
    File the ``write_outside`` fault touches.
``FAKE_OPENCLAW_SANDBOX``
    When set, refuse (exit 64) if HOME or OPENCLAW_STATE_DIR resolve outside this directory.

Only fake credentials belong in the configs it reads.  The stand-in never opens anything else.
"""

import contextlib
import fnmatch
import json
import os
import platform
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VERSION_LINE = "OpenClaw 2026.9.9 (standin)"
EXIT_SANDBOX = 64
MCP_PROTOCOL = "2025-11-25"
ROLE_ENV = "FAKE_OPENCLAW_ROLE"
TIMEOUT_TEXT = (
    "Request timed out before a response was generated. Please try again, or increase "
    "`agents.defaults.timeoutSeconds` in your config."
)
NO_AUTH_TEXT = "No route-compatible authentication source is configured for openai."
CLEANUP_TEXT = "Agent exec cleanup failed: Agent runtime cleanup did not settle; "
CLEANUP_TEXT += "state ownership retained until this process exits"
VALUE_OPTIONS = frozenset(
    {
        "--message-file",
        "--timeout",
        "--config",
        "--state-dir",
        "--cwd",
        "--model",
        "--fallback",
        "--thinking",
        "--code-mode",
    }
)
POSIX = os.name == "posix"


class ConfigError(Exception):
    """Signal an unreadable or invalid configuration."""


class ModelError(Exception):
    """Signal a failed model request."""


class RunTimeoutError(Exception):
    """Signal that the ``--timeout`` deadline passed."""


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def faults() -> dict[str, str]:
    """Return the active faults from ``FAKE_OPENCLAW_FAULT`` as a name -> value mapping."""
    result: dict[str, str] = {}
    for item in os.environ.get("FAKE_OPENCLAW_FAULT", "").split(","):
        item = item.strip()
        if item:
            name, _, value = item.partition("=")
            result[name] = value
    return result


def record(event: dict[str, Any]) -> None:
    """Append one event to the ``FAKE_OPENCLAW_RECORD`` file, if configured."""
    target = os.environ.get("FAKE_OPENCLAW_RECORD")
    if not target:
        return
    line = json.dumps({"t": time.time(), "pid": os.getpid(), **event}) + "\n"
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(line)


def record_role(role: str, **extra: Any) -> None:
    """Record this process's role and its process identifiers."""
    ids: dict[str, Any] = {"ppid": os.getppid()}
    if POSIX:
        ids["pgid"] = os.getpgrp()
        ids["sid"] = os.getsid(0)
    record({"event": "role", "role": role, **ids, **extra})


def say(text: str) -> None:
    """Write a diagnostic line to stderr unless logging is silent."""
    argv = sys.argv
    pair = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--log-level"]
    if os.environ.get("OPENCLAW_LOG_LEVEL") == "silent" or "silent" in pair:
        return
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def emit(document: dict[str, Any]) -> None:
    """Print a JSON document to stdout."""
    sys.stdout.write(json.dumps(document, indent=2) + "\n")
    sys.stdout.flush()


def check_sandbox() -> None:
    """Exit 64 when HOME or OPENCLAW_STATE_DIR leave ``FAKE_OPENCLAW_SANDBOX``."""
    sandbox = os.environ.get("FAKE_OPENCLAW_SANDBOX")
    if not sandbox:
        return
    root = os.path.normcase(os.path.realpath(sandbox))
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    for label, value in (
        ("HOME", home),
        ("OPENCLAW_STATE_DIR", os.environ.get("OPENCLAW_STATE_DIR")),
    ):
        if value is None and label != "HOME":
            continue
        if not value or not _inside(os.path.realpath(value), root):
            sys.stderr.write(f"fake_openclaw: {label} is outside the sandbox; refusing\n")
            sys.exit(EXIT_SANDBOX)


def _inside(path: str, root: str) -> bool:
    """Report whether ``path`` lies at or below ``root`` (both already real paths)."""
    candidate = os.path.normcase(path)
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------


def read_json(path: str) -> dict[str, Any]:
    """Read a JSON object from ``path`` (no JSON5), raising ConfigError when invalid."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
        document = json.loads(text)
    except (OSError, ValueError) as error:
        message = f"cannot read config {path}: {error}"
        raise ConfigError(message) from error
    if not isinstance(document, dict):
        message = f"config {path} is not a JSON object"
        raise ConfigError(message)
    return document


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Merge ``over`` into a copy of ``base``: dicts recurse, everything else is replaced."""
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def include_allowed(target: str, including: str) -> bool:
    """Report whether ``target`` may be included from ``including``."""
    real = os.path.realpath(target)
    roots = [os.path.dirname(os.path.realpath(including))]
    roots += [r for r in os.environ.get("OPENCLAW_INCLUDE_ROOTS", "").split(os.pathsep) if r]
    return any(_inside(real, os.path.normcase(os.path.realpath(root))) for root in roots)


def load_config(path: str | None) -> dict[str, Any]:
    """Load the effective configuration: one ``$include`` level, siblings merged over it."""
    if not path:
        return {}
    document = read_json(path)
    include = document.pop("$include", None)
    if include is None:
        return document
    target = os.path.join(os.path.dirname(os.path.abspath(path)), str(include))
    if not include_allowed(target, os.path.abspath(path)):
        message = f"$include {include} is outside the allowed roots"
        raise ConfigError(message)
    included = read_json(target)
    if "$include" in included:
        message = "nested $include is not supported"
        raise ConfigError(message)
    return deep_merge(included, document)


def dig(document: Any, dotted: str) -> Any:
    """Return the value at a dotted path, or None when any step is missing."""
    current = document
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def primary_model(config: dict[str, Any]) -> str:
    """Return ``agents.defaults.model.primary`` (or a plain string value), else empty."""
    model = dig(config, "agents.defaults.model")
    if isinstance(model, dict):
        model = model.get("primary")
    return model if isinstance(model, str) else ""


def provider_for(config: dict[str, Any], ref: str) -> tuple[str, str, dict[str, Any] | None]:
    """Split ``provider/model`` and return (provider id, model id, usable provider or None)."""
    provider_id, _, model_id = ref.partition("/")
    provider = dig(config, f"models.providers.{provider_id}")
    usable = (
        isinstance(provider, dict)
        and bool(provider.get("baseUrl"))
        and isinstance(provider.get("apiKey"), str)
        and bool(provider["apiKey"])
        and bool(model_id)
    )
    return provider_id, model_id, provider if usable else None


# ---------------------------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------------------------


def server_env(spec: dict[str, Any]) -> dict[str, str]:
    """Build the sanitised environment of an MCP server: configured env plus a few basics."""
    env: dict[str, str] = {}
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if home:
        env["HOME"] = home
    env["PATH"] = os.environ.get("PATH", "")
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    env["USER"] = user
    env["LOGNAME"] = user
    for key in ("SHELL", "TERM", "LC_CTYPE"):
        if key in os.environ:
            env[key] = os.environ[key]
    if sys.platform == "win32":
        # Python cannot start on Windows without these; the real runtime passes them too.
        for key in ("SYSTEMROOT", "PATHEXT", "COMSPEC", "USERPROFILE"):
            if key in os.environ:
                env[key] = os.environ[key]
    configured = spec.get("env")
    if isinstance(configured, dict):
        env.update({str(k): str(v) for k, v in configured.items()})
    leaked = sorted(key for key in env if key.upper().startswith("OPENCLAW_"))
    if leaked:
        message = f"refusing to pass OPENCLAW_* variables to an MCP server: {leaked}"
        raise ConfigError(message)
    return env


class McpServer:
    """Drive one stdio MCP server over newline-delimited JSON-RPC."""

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        """Spawn the server described by ``spec`` and start reading its stdout."""
        self.name = name
        self.lines: queue.Queue[bytes | None] = queue.Queue()
        self.counter = 0
        command = spec.get("command")
        if not isinstance(command, str) or not command:
            message = f"mcp server {name} has no command"
            raise ConfigError(message)
        args = [str(a) for a in spec.get("args", [])]
        options: dict[str, Any] = {}
        if POSIX:
            options["start_new_session"] = True
        else:
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        self.process = subprocess.Popen(  # noqa: S603
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=server_env(spec),
            **options,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """Move stdout lines into the queue; mark the end with None."""
        stream = self.process.stdout
        if stream is None:
            self.lines.put(None)
            return
        for raw in iter(stream.readline, b""):
            self.lines.put(raw)
        self.lines.put(None)

    def send(self, message: dict[str, Any]) -> None:
        """Write one JSON-RPC message to the server."""
        stdin = self.process.stdin
        if stdin is None:
            return
        try:
            stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            stdin.flush()
        except OSError as error:
            text = f'bundle-mcp server "{self.name}" is not connected'
            raise ModelError(text) from error

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Send a request and return its ``result``, skipping notifications."""
        self.counter += 1
        identifier = self.counter
        self.send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                text = f'bundle-mcp server "{self.name}" timed out'
                raise ModelError(text)
            try:
                raw = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if raw is None:
                text = f'bundle-mcp server "{self.name}" is not connected'
                raise ModelError(text)
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("id") == identifier:
                if "error" in message:
                    raise ModelError(str(message["error"]))
                return message.get("result") or {}

    def handshake(self, timeout: float) -> list[dict[str, Any]]:
        """Run initialize, notifications/initialized and tools/list; return the tool list."""
        self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "openclaw-bundle-mcp", "version": "0.0.0"},
            },
            timeout,
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = self.request("tools/list", {}, timeout)
        tools = result.get("tools")
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    def kill(self) -> None:
        """Kill the server process outright."""
        with contextlib.suppress(OSError):
            self.process.kill()

    def close(self) -> None:
        """Close stdin, then terminate and kill the server if it lingers."""
        with contextlib.suppress(OSError):
            if self.process.stdin:
                self.process.stdin.close()
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.kill()
                self.process.wait()


def prefix_for(name: str) -> str:
    """Return the provider-safe tool prefix OpenClaw derives from a server name."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", name)
    return cleaned if cleaned[:1].isalpha() else f"mcp-{cleaned}"


def matches(name: str, patterns: list[str]) -> bool:
    """Report whether ``name`` matches any case-insensitive glob in ``patterns``."""
    return any(fnmatch.fnmatchcase(name.lower(), str(p).lower()) for p in patterns)


def filter_server_tools(spec: dict[str, Any], tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply a server's ``toolFilter`` include/exclude globs to its raw tool names."""
    tool_filter = spec.get("toolFilter")
    if not isinstance(tool_filter, dict):
        return tools
    include = tool_filter.get("include")
    exclude = tool_filter.get("exclude") or []
    kept = []
    for tool in tools:
        name = str(tool.get("name", ""))
        if isinstance(include, list) and not matches(name, include):
            continue
        if matches(name, exclude):
            continue
        kept.append(tool)
    return kept


def allowed_by_policy(config: dict[str, Any], prefixed: str) -> bool:
    """Apply ``tools.allow`` and ``tools.deny`` to a prefixed MCP tool name."""
    deny = dig(config, "tools.deny")
    if isinstance(deny, list) and matches(prefixed, deny):
        return False
    allow = dig(config, "tools.allow")
    if not isinstance(allow, list):
        return True
    if any(str(item).lower() in {"bundle-mcp", "group:plugins"} for item in allow):
        return True
    return matches(prefixed, allow)


def enabled_servers(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the configured MCP servers that are not disabled."""
    servers = dig(config, "mcp.servers")
    if not isinstance(servers, dict):
        return {}
    return {
        name: spec
        for name, spec in servers.items()
        if isinstance(spec, dict) and spec.get("enabled") is not False
    }


# ---------------------------------------------------------------------------------------------
# Simple commands
# ---------------------------------------------------------------------------------------------


def config_path_from_env() -> str | None:
    """Return ``$OPENCLAW_CONFIG_PATH`` when set."""
    return os.environ.get("OPENCLAW_CONFIG_PATH") or None


def command_config(args: list[str]) -> int:
    """Run ``config file|validate|get``."""
    action = args[0] if args else ""
    path = config_path_from_env()
    if action == "file":
        shown = path
        if not shown:
            state = os.environ.get("OPENCLAW_STATE_DIR") or os.path.join(
                os.environ.get("HOME", "~"), ".openclaw"
            )
            shown = os.path.join(state, "openclaw.json")
        sys.stdout.write(shown + "\n")
        return 0
    try:
        config = load_config(path)
    except ConfigError as error:
        sys.stderr.write(f"Config invalid: {error}\n")
        return 1
    if action == "validate":
        sys.stdout.write(f"Config valid: {path or '(none)'}\n")
        return 0
    if action == "get" and len(args) > 1:
        value = dig(config, args[1])
        if value is None:
            emit(
                {
                    "ok": False,
                    "error": {
                        "type": "cli_error",
                        "message": f"Config path is valid but unset: {args[1]}.",
                    },
                }
            )
            return 1
        if args[1].rsplit(".", 1)[-1].lower() in {"apikey", "token", "password"}:
            value = "__OPENCLAW_REDACTED__"
        sys.stdout.write(json.dumps(value, indent=2) + "\n")
        return 0
    sys.stderr.write("fake_openclaw: unsupported config command\n")
    return 1


def command_models(args: list[str]) -> int:
    """Run ``models status [--check] [--plain]``."""
    try:
        config = load_config(config_path_from_env())
    except ConfigError as error:
        sys.stderr.write(f"Config invalid: {error}\n")
        return 1
    ref = primary_model(config)
    if ref:
        sys.stdout.write(ref + "\n")
    else:
        sys.stdout.write("\n" if "--plain" in args else "")
    if "--check" in args:
        return 0 if ref and provider_for(config, ref)[2] is not None else 1
    return 0


def command_mcp(args: list[str]) -> int:
    """Run ``mcp probe NAME --json``."""
    if len(args) < 2 or args[0] != "probe":
        sys.stderr.write("fake_openclaw: unsupported mcp command\n")
        return 1
    name = args[1]
    try:
        config = load_config(config_path_from_env())
        spec = enabled_servers(config).get(name)
        if spec is None:
            emit({"ok": False, "error": {"message": f"MCP server not found or disabled: {name}"}})
            return 1
        server = McpServer(name, spec)
    except (ConfigError, OSError) as error:
        emit({"ok": False, "error": {"message": str(error)}})
        return 1
    try:
        raw = filter_server_tools(
            spec, server.handshake(float(spec.get("connectionTimeoutMs", 30000)) / 1000)
        )
    except ModelError as error:
        emit({"ok": False, "error": {"message": str(error)}})
        return 1
    finally:
        server.close()
    prefix = prefix_for(name)
    names = sorted(f"{prefix}__{t.get('name')}" for t in raw)
    emit({"servers": {name: {"tools": len(names)}}, "tools": names, "diagnostics": []})
    return 0


# ---------------------------------------------------------------------------------------------
# agent exec: argument parsing
# ---------------------------------------------------------------------------------------------


def split_globals(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split leading global flags (``--no-color``, ``--log-level X``, ...) from the command."""
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        flag = argv[index]
        if flag in {"--version", "-V", "--help", "-h"}:
            break
        index += 2 if flag in {"--log-level", "--profile", "--container"} else 1
    return argv[:index], argv[index:]


def parse_exec(args: list[str]) -> dict[str, Any]:
    """Parse the options of ``agent exec`` into a dict (``message`` holds a positional prompt)."""
    options: dict[str, Any] = {"message": None, "flags": set()}
    index = 0
    while index < len(args):
        item = args[index]
        if item in VALUE_OPTIONS and index + 1 < len(args):
            options[item.lstrip("-").replace("-", "_")] = args[index + 1]
            index += 2
            continue
        if item.startswith("--"):
            options["flags"].add(item)
        elif options["message"] is None:
            options["message"] = item
        index += 1
    return options


# ---------------------------------------------------------------------------------------------
# agent exec: process topology
# ---------------------------------------------------------------------------------------------


def forward_signals(target: subprocess.Popen[bytes]) -> None:
    """Forward SIGTERM-like signals to ``target``, like ``runRespawnedChild``."""

    def handler(signum: int, _frame: Any) -> None:
        with contextlib.suppress(OSError, ValueError):
            if POSIX:
                target.send_signal(signum)
            else:
                target.terminate()

    names = (
        ["SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"] if POSIX else ["SIGTERM", "SIGINT", "SIGBREAK"]
    )
    for name in names:
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, handler)


def wait_for(process: subprocess.Popen[bytes]) -> int:
    """Wait with a polling loop so signal handlers run, and map signal deaths to 128+N."""
    while process.poll() is None:
        time.sleep(0.05)
    code = process.returncode
    return 128 - code if code < 0 else code


def spawn_next(role: str, new_session: bool) -> subprocess.Popen[bytes]:
    """Re-run this script with ``FAKE_OPENCLAW_ROLE=role`` and inherited stdio."""
    env = dict(os.environ)
    env[ROLE_ENV] = role
    options: dict[str, Any] = {}
    if new_session:
        if POSIX:
            options["start_new_session"] = True
        else:
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return subprocess.Popen(  # noqa: S603
        [sys.executable, os.path.abspath(__file__), *sys.argv[1:]], env=env, **options
    )


def run_launcher() -> int:
    """Act as the top process: record, spawn the child, forward SIGTERM, mirror its exit."""
    record_role("launcher")
    if "ignore_sigterm" in faults():
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = spawn_next("child", new_session=False)
    else:
        child = spawn_next("child", new_session=False)
        forward_signals(child)
    return wait_for(child)


def run_child() -> int:
    """Act as the middle process: spawn the runtime in its own session and forward signals."""
    record_role("child")
    runtime = spawn_next("runtime", new_session=True)
    if "ignore_sigterm" not in faults():
        forward_signals(runtime)
    return wait_for(runtime)


# ---------------------------------------------------------------------------------------------
# agent exec: runtime
# ---------------------------------------------------------------------------------------------


class Runtime:
    """Run one embedded turn: bridge, model requests, tool execution, envelope."""

    def __init__(self, options: dict[str, Any], config: dict[str, Any]) -> None:
        """Prepare the run from parsed options and the effective config."""
        self.options = options
        self.config = config
        self.fault = faults()
        self.session = str(uuid.uuid4())
        timeout = float(options.get("timeout") or 600)
        self.deadline = time.monotonic() + timeout if timeout > 0 else None
        self.servers: dict[str, McpServer] = {}
        self.tools: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.turns = 0
        self.calls: list[str] = []
        self.failures = 0
        self.usage = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
        self.cleanup_error: str | None = None
        self.provider_id = ""
        self.model_id = ""

    def remaining(self) -> float | None:
        """Return seconds left before the run deadline, or None when unlimited."""
        if self.deadline is None:
            return None
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise RunTimeoutError
        return left

    def sleep(self, seconds: float) -> None:
        """Sleep, but wake with a timeout error when the deadline passes first."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.remaining()
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def start_bridges(self) -> None:
        """Spawn every enabled MCP server, run the handshake and build the visible tool map."""
        for name, spec in enabled_servers(self.config).items():
            server = McpServer(name, spec)
            self.servers[name] = server
            record({"event": "role", "role": "bridge", "pid": server.process.pid, "server": name})
            timeout = float(spec.get("connectionTimeoutMs", 30000)) / 1000
            for tool in filter_server_tools(spec, server.handshake(timeout)):
                prefixed = f"{prefix_for(name)}__{tool.get('name')}"
                if allowed_by_policy(self.config, prefixed):
                    self.tools[prefixed] = (name, str(tool.get("name")), tool)

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Return the OpenAI ``tools`` array for the current request."""
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": str(tool.get("description", "")),
                    "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                },
            }
            for prefixed, (_server, _raw, tool) in sorted(self.tools.items())
        ]
        if "extra_tool_in_request" in self.fault:
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "description": "Run a shell command.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            )
        return definitions

    def model_request(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
        """POST one streaming chat-completions request and return (text, tool calls)."""
        provider = dig(self.config, f"models.providers.{self.provider_id}") or {}
        url = str(provider["baseUrl"]).rstrip("/") + "/chat/completions"
        if not url.startswith(("http://", "https://")):
            message = f"unsupported provider url scheme: {url}"
            raise ModelError(message)
        body: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": 2048,
        }
        definitions = self.tool_definitions()
        if definitions:
            body["tools"] = definitions
            body["tool_choice"] = "auto"
        record(
            {
                "event": "request",
                "index": self.turns,
                "tools": [d["function"]["name"] for d in definitions],
                "roles": [m["role"] for m in messages],
                "user": next((m["content"] for m in messages if m["role"] == "user"), ""),
            }
        )
        request = urllib.request.Request(  # noqa: S310
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {provider['apiKey']}",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        text = ""
        calls: dict[int, dict[str, str]] = {}
        try:
            with opener.open(request, timeout=self.remaining()) as response:
                for raw in response:
                    self.remaining()
                    text += self._consume(raw, calls)
        except urllib.error.HTTPError as error:
            message = f"model request failed: HTTP {error.code}"
            raise ModelError(message) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise RunTimeoutError from error
            message = f"model request failed: {error}"
            raise ModelError(message) from error
        self.turns += 1
        return text, [calls[i] for i in sorted(calls)]

    def _consume(self, raw: bytes, calls: dict[int, dict[str, str]]) -> str:
        """Fold one SSE line into ``calls`` and return any content text it carried."""
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            return ""
        payload = line[5:].strip()
        if payload == "[DONE]":
            return ""
        try:
            chunk = json.loads(payload)
        except ValueError:
            return ""
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage["input"] += int(usage.get("prompt_tokens", 0))
            self.usage["output"] += int(usage.get("completion_tokens", 0))
            self.usage["total"] += int(usage.get("total_tokens", 0))
        text = ""
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                text += delta["content"]
            for fragment in delta.get("tool_calls") or []:
                slot = calls.setdefault(
                    int(fragment.get("index", 0)), {"id": "", "name": "", "arguments": ""}
                )
                function = fragment.get("function") or {}
                slot["id"] = fragment.get("id") or slot["id"]
                slot["name"] += function.get("name") or ""
                slot["arguments"] += function.get("arguments") or ""
        return text

    def execute(self, call: dict[str, str]) -> str:
        """Run one model tool call through its MCP bridge and return the result text."""
        name = call["name"]
        entry = self.tools.get(name)
        if entry is None:
            self.failures += 1
            return f"Tool {name} not found"
        server_name, raw_name, _tool = entry
        self.calls.append(name)
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except ValueError:
            self.failures += 1
            return "Invalid tool arguments"
        server = self.servers[server_name]
        if "die_mid_call" in self.fault:
            server.send(
                {
                    "jsonrpc": "2.0",
                    "id": 10_000,
                    "method": "tools/call",
                    "params": {"name": raw_name, "arguments": arguments},
                }
            )
            server.kill()
            self.cleanup_error = CLEANUP_TEXT
            self.failures += 1
            return f'bundle-mcp server "{server_name}" is not connected'
        try:
            result = server.request("tools/call", {"name": raw_name, "arguments": arguments}, 60.0)
        except ModelError as error:
            self.failures += 1
            return str(error)
        if result.get("isError"):
            self.failures += 1
        parts = result.get("content")
        texts = (
            [p.get("text", "") for p in parts if isinstance(p, dict)]
            if isinstance(parts, list)
            else []
        )
        return "\n".join(texts)

    def messages(self, prompt: str, ref: str) -> list[dict[str, Any]]:
        """Build the initial system and user messages."""
        host = socket.gethostname() if "emit_hostname" in self.fault else "standin-host"
        stamp = datetime.now(UTC).strftime("%a %Y-%m-%d %H:%M UTC")
        names = ", ".join(sorted(self.tools)) or "none"
        system = (
            "You are a personal assistant running inside OpenClaw (stand-in).\n"
            f"## Tooling\nTools policy-filtered: {names}\n"
        )
        user = (
            f"[{stamp}] {prompt}\n\nRuntime: agent=main"
            f" | session=agent:main:explicit:{self.session}"
            f" | host={host} | os={platform.system()} | model={ref} | default_model={ref}"
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def envelope(
        self, status: str, final: str, error: str | None, kind: str = "exception"
    ) -> dict[str, Any]:
        """Build the ``--json`` envelope for the given outcome."""
        ok = status == "ok"
        payload: dict[str, Any] = {"text": final, "mediaUrl": None}
        document: dict[str, Any] = {
            "ok": ok,
            "status": status,
            "final": final if ok else "",
            "payloads": [payload] if (ok or status == "timeout") else [],
        }
        if status == "timeout":
            payload["text"] = TIMEOUT_TEXT
            payload["isError"] = True
        if ok:
            document["usage"] = {**self.usage, "cost": {"total": 0}}
        document["codeModeEngaged"] = False
        if self.turns:
            document["assistantTurns"] = self.turns
        if ok and self.calls:
            document["toolSummary"] = {
                "calls": len(self.calls),
                "tools": list(dict.fromkeys(self.calls)),
                "failures": self.failures,
            }
        document["model"] = self.model_id or None
        document["provider"] = self.provider_id or None
        document["sessionId"] = self.session
        if error:
            document["error"] = {"message": error, "kind": kind}
        return document

    def conversation(self, messages: list[dict[str, Any]]) -> str:
        """Run model requests and tool calls until the model answers with text."""
        second_move_pending = "second_move" in self.fault
        while True:
            text, calls = self.model_request(messages)
            if calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]},
                            }
                            for c in calls
                        ],
                    }
                )
                for call in calls:
                    result = self.execute(call)
                    record(
                        {"event": "tool_call", "name": call["name"], "arguments": call["arguments"]}
                    )
                    messages.append({"role": "tool", "content": result, "tool_call_id": call["id"]})
                self.sleep(0.3)
                continue
            if second_move_pending:
                second_move_pending = False
                messages.append({"role": "assistant", "content": text})
                self.sleep(0.3)
                continue
            return text

    def run(self) -> int:
        """Execute the turn and print the envelope; return the process exit code."""
        options = self.options
        state_dir = options.get("state_dir")
        owned_state = None
        if state_dir is None:
            state_dir = owned_state = tempfile.mkdtemp(prefix="fake-openclaw-")
        if not os.path.isdir(state_dir):
            emit(self.envelope("error", "", f"State directory does not exist: {state_dir}"))
            return 1
        cwd = options.get("cwd")
        if cwd is not None and not os.path.isdir(cwd):
            emit(self.envelope("error", "", f"Working directory does not exist: {cwd}"))
            return 1
        try:
            return self._run(state_dir)
        finally:
            for server in self.servers.values():
                server.close()
            if owned_state:
                shutil.rmtree(owned_state, ignore_errors=True)

    def _run(self, state_dir: str) -> int:
        """Run the turn body inside ``run``'s cleanup scope."""
        options = self.options
        ref = options.get("model") or primary_model(self.config)
        self.provider_id, self.model_id, provider = provider_for(self.config, ref)
        if "no_auth" in self.fault or provider is None:
            self.provider_id = self.model_id = ""
            emit(self.envelope("error", "", NO_AUTH_TEXT))
            return 1
        prompt = self._prompt()
        for relative in ("state/openclaw.sqlite", "agents/main/agent/openclaw-agent.sqlite"):
            target = Path(state_dir, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-openclaw-state")
        if "write_outside" in self.fault and os.environ.get("FAKE_OPENCLAW_OUTSIDE"):
            Path(os.environ["FAKE_OPENCLAW_OUTSIDE"]).write_text("outside\n", encoding="utf-8")
        say("[agent/embedded] stand-in run started")
        try:
            self.start_bridges()
            if "hang" in self.fault:
                while True:
                    self.sleep(1.0)
            if "slow" in self.fault:
                self.sleep(float(self.fault["slow"] or 1))
            final = self.conversation(self.messages(prompt, f"{self.provider_id}/{self.model_id}"))
        except RunTimeoutError:
            emit(self.envelope("timeout", "", TIMEOUT_TEXT, "timeout"))
            return 2
        except (ModelError, ConfigError, OSError) as error:
            emit(self.envelope("error", "", str(error)))
            return 1
        if "cleanup_fail" in self.fault or self.cleanup_error:
            message = (
                self.cleanup_error or "Agent exec cleanup failed: EBUSY: resource busy or locked"
            )
            emit(self.envelope("error", "", message))
            return 1
        if "garbage_stdout" in self.fault:
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            return 0
        emit(self.envelope("ok", final, None))
        return 0

    def _prompt(self) -> str:
        """Return the prompt from ``--message-file`` (``-`` is stdin) or the positional text."""
        source = self.options.get("message_file")
        if source == "-":
            return sys.stdin.read()
        if source:
            return Path(source).read_text(encoding="utf-8-sig")
        return str(self.options.get("message") or "")


def run_runtime(argv: list[str]) -> int:
    """Act as the runtime process: install signal handling, then run the turn."""
    record_role("runtime")
    options = parse_exec(argv)
    try:
        config = load_config(options.get("config") or config_path_from_env())
    except ConfigError as error:
        sys.stderr.write(f"Config invalid: {error}\n")
        return 1
    runtime = Runtime(options, config)
    if "leak_child" in runtime.fault:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:

        def on_term(_signum: int, _frame: Any) -> None:
            for server in runtime.servers.values():
                server.close()
            os._exit(143)

        names = ["SIGTERM", "SIGINT"] if POSIX else ["SIGTERM", "SIGINT", "SIGBREAK"]
        for name in names:
            number = getattr(signal, name, None)
            if number is not None:
                signal.signal(number, on_term)
    return runtime.run()


def command_agent(args: list[str]) -> int:
    """Run ``agent exec``: dispatch on the process role."""
    if not args or args[0] != "exec":
        sys.stderr.write("fake_openclaw: only `agent exec` is supported (no Gateway here)\n")
        return 1
    role = os.environ.get(ROLE_ENV, "launcher")
    if role == "child":
        return run_child()
    if role == "runtime":
        return run_runtime(args[1:])
    return run_launcher()


def main(argv: list[str]) -> int:
    """Dispatch one OpenClaw command line and return the exit code."""
    check_sandbox()
    if "--version" in argv or "-V" in argv:
        sys.stdout.write(VERSION_LINE + "\n")
        return 0
    _globals, rest = split_globals(argv)
    if not rest:
        sys.stderr.write("fake_openclaw: no command\n")
        return 1
    command, args = rest[0], rest[1:]
    handlers = {
        "config": command_config,
        "models": command_models,
        "mcp": command_mcp,
        "agent": command_agent,
    }
    handler = handlers.get(command)
    if handler is None:
        sys.stderr.write(f"fake_openclaw: unknown command {command}\n")
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
