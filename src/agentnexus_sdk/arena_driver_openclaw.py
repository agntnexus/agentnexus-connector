"""The OpenClaw Arena driver (agntnexus/agentnexus#228).

OpenClaw owns the provider, the model, the authentication and the routing of the profile it runs;
this driver proves process, tool, deadline and isolation capabilities and nothing else, and names
no provider or model. One decision is one `openclaw agent exec` run started by the decision worker
(`openclaw_arena.py`) in a throwaway state, home and overlay configuration; the profile is only
included, read-only.

The proof that matters is what the model can see. OpenClaw prints no list of the tools it gives a
model, so the preflight runs the real runtime once against a model that is this module's own, on
loopback, with the same overlay and argv a decision uses, and reads the tool names off the request:
the allow-list or the switch that hides tools behind a search tool removed from the overlay shows
up there.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agentnexus_sdk import arena_match, openclaw_arena
from agentnexus_sdk.arena_driver import CONTRACT, Capabilities, DriverRefused, Launch
from agentnexus_sdk.bridge import is_declared_model_valid
from agentnexus_sdk.runtimes import OpenClawAdapter, model_identifier

#: The only OpenClaw releases the Arena contract was reviewed against. A release is added by
#: running the preflight and the process acceptance against it, not by editing this line.
REVIEWED_VERSIONS = frozenset({"2026.9.9"})
STEP_SECONDS = 180
CANARY_SECONDS = 300


@dataclass(frozen=True)
class OpenClawRun:
    """A reviewed OpenClaw and its profile-owned state/config context."""

    command: tuple[str, ...]
    version: str
    config: Path | None
    state: Path | None
    profile_root: Path | None = None


def resolve_command(executable: str) -> tuple[str, ...] | None:
    """Return the argv prefix that starts the runtime without a shell, or `None` if unknown.

    A Windows command shim is a batch file, and a batch file is run by a shell that re-parses its
    arguments. The shim only starts Node on the package's own entry file, so that is started.
    """
    path = Path(executable)
    if sys.platform != "win32" or path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return (str(path),)
    entry = path.parent / "node_modules" / "openclaw" / "openclaw.mjs"
    node = shutil.which("node") or str(path.parent / "node.exe")
    return (node, str(entry)) if entry.is_file() and Path(node).is_file() else None


class CanaryModel:
    """A model of the Connector's own on loopback that records what the runtime offers it."""

    def __init__(self) -> None:
        """Listen on a free loopback port; every request is answered with a short plain reply."""
        self.tools: list[list[str]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                """Say nothing."""

            def do_POST(self) -> None:
                """Record the tool names of a chat request and end the turn with text."""
                length = int(self.headers.get("content-length", "0"))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                if "chat/completions" not in self.path:
                    self.send_response(404)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                names = []
                for tool in body.get("tools") or []:
                    function = tool.get("function") or tool if isinstance(tool, dict) else {}
                    names.append(str(function.get("name")))
                owner.tools.append(sorted(names))
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("connection", "close")
                self.end_headers()
                for delta, finish in (({"role": "assistant", "content": "ok"}, None), ({}, "stop")):
                    chunk = {
                        "id": "x",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "canary",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def provider(self) -> dict[str, Any]:
        """Return the provider entry that points the runtime at this model."""
        return {
            "baseUrl": f"http://127.0.0.1:{self.server.server_address[1]}/v1",
            "apiKey": secrets.token_hex(8),
            "models": [
                {
                    "id": "canary",
                    "name": "Canary",
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": 32000,
                    "maxTokens": 2048,
                }
            ],
        }

    def close(self) -> None:
        """Stop listening."""
        self.server.shutdown()
        self.server.server_close()


class OpenClawArenaDriver:
    """OpenClaw through its reviewed installation, a throwaway world and a decision worker."""

    name = "openclaw"
    display_name = "OpenClaw"
    capabilities: Capabilities = CONTRACT

    def inspect(self, paths: Any) -> OpenClawRun:
        """Refuse shared profiles and any runtime release outside the reviewed ones."""
        if paths.isolation != "isolated":
            raise DriverRefused("not_isolated", self.display_name)
        context = paths.runtime_context()
        detection = OpenClawAdapter(context=context).detect()
        command = resolve_command(detection.executable) if detection.executable else None
        if not detection.installed or command is None or detection.version not in REVIEWED_VERSIONS:
            raise DriverRefused("unreviewed", self.display_name)
        config = context.openclaw_config
        state = context.openclaw_state
        profile_root = Path(paths.root)
        if (
            config is None
            or state is None
            or not openclaw_arena.profile_context_valid(profile_root, config, state)
        ):
            raise DriverRefused("not_isolated", self.display_name)
        return OpenClawRun(
            command,
            detection.version or "",
            config,
            state,
            profile_root,
        )

    def _run(
        self, handle: OpenClawRun, work: Path, config: Path, arguments: list[str], seconds: int
    ) -> subprocess.CompletedProcess[str]:
        """Run one runtime command with bounded output and a process-tree lifetime."""
        include_root = handle.config.parent if handle.config is not None else work
        argv = [*handle.command, *arguments]
        if handle.config is None or handle.state is None or handle.profile_root is None:
            raise DriverRefused("not_isolated", self.display_name)
        environment = openclaw_arena.cli_environment(
            dict(os.environ),
            work,
            config,
            include_root,
            profile_root=handle.profile_root,
            profile_config=handle.config,
            profile_state=handle.state,
        )
        returncode, stdout = openclaw_arena.bounded_run(argv, environment, work, seconds)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    def preflight(self, handle: OpenClawRun) -> frozenset[str]:
        """Prove the contract without a paid call and return the tools a model is offered.

        In order, each step a refusal when it fails: the overlay configuration validates; the
        bridge is the one server and exposes exactly the three tools; the runtime reports usable
        authentication for the route it would take; and, last, a run against a model of ours
        shows which tools reach a model.
        """
        refusal = DriverRefused("preflight_refused", self.display_name)
        canary = CanaryModel()
        try:
            with tempfile.TemporaryDirectory(
                prefix="agentnexus-openclaw-", ignore_cleanup_errors=True
            ) as scratch:
                work = Path(scratch)
                openclaw_arena.make_world(work)
                bridge = Path(openclaw_arena.__file__).resolve().with_name("openclaw_bridge.py")
                if handle.config is None or handle.state is None or handle.profile_root is None:
                    raise DriverRefused("not_isolated", self.display_name)
                others = openclaw_arena.server_names(
                    list(handle.command),
                    handle.config,
                    work,
                    profile_root=handle.profile_root,
                    profile_config=handle.config,
                    profile_state=handle.state,
                )

                def overlay(**extra: Any) -> Path:
                    document = openclaw_arena.overlay_document(
                        handle.config,
                        python=sys.executable,
                        bridge=bridge,
                        address=str(work / "r.sock"),
                        key=secrets.token_hex(32),
                        disabled=others,
                        **extra,
                    )
                    return openclaw_arena.write_overlay(work, document)

                path = overlay()
                environment = openclaw_arena.cli_environment(
                    dict(os.environ),
                    work,
                    path,
                    handle.config.parent,
                    profile_root=handle.profile_root,
                    profile_config=handle.config,
                    profile_state=handle.state,
                )
                entries = openclaw_arena.config_value(
                    list(handle.command), "agents.entries", environment, work, []
                )
                default_id = openclaw_arena.config_value(
                    list(handle.command),
                    "agents.defaults.systemAgent.agentId",
                    environment,
                    work,
                    "main",
                )
                if not openclaw_arena.agent_directories_valid(
                    entries, default_id, handle.profile_root, handle.state
                ):
                    raise refusal
                valid = self._run(handle, work, path, ["config", "validate"], STEP_SECONDS)
                probe = self._run(
                    handle,
                    work,
                    path,
                    ["mcp", "probe", openclaw_arena.SERVER, "--json"],
                    STEP_SECONDS,
                )
                if valid.returncode != 0 or probe.returncode != 0:
                    raise refusal
                seen = _probe_tools(probe.stdout)
                if seen != frozenset(openclaw_arena.PREFIXED):
                    raise refusal
                usable = self._run(
                    handle, work, path, ["models", "status", "--check", "--plain"], STEP_SECONDS
                )
                if usable.returncode != 0:
                    raise refusal
                prompt = work / "prompt.txt"
                prompt.write_text("Reply with the single word ok.", encoding="utf-8")
                path = overlay(provider=canary.provider)
                canary_run = self._run(
                    handle,
                    work,
                    path,
                    openclaw_arena.exec_arguments([], prompt, 120, path, work),
                    CANARY_SECONDS,
                )
                if canary_run.returncode != 0:
                    raise refusal
                offered = canary.tools[0] if canary.tools else None
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            raise refusal from error
        finally:
            canary.close()
        if offered is None:
            raise refusal
        prefix = f"{openclaw_arena.SERVER}__"
        return frozenset(name.removeprefix(prefix) for name in offered)

    def generation(self, handle: OpenClawRun) -> str | None:
        """Return an opaque token that changes when what OpenClaw plays with does.

        Metadata only, never content: the modification time and size of the profile's
        configuration and of the secrets file beside it and its state, and of the runtime's own
        entry file. Credentials kept in the runtime's own database are not part of it; OpenClaw
        reads them through its profile state while `agent exec --state-dir` keeps sessions
        disposable.
        """
        digest = hashlib.sha256()
        digest.update(handle.version.encode())
        for label, path in (
            ("config", handle.config),
            ("secrets", handle.config.parent / ".env" if handle.config is not None else None),
            ("state-secrets", handle.state / ".env" if handle.state is not None else None),
            ("entry", Path(handle.command[-1])),
        ):
            try:
                info = path.stat() if path is not None else None
            except OSError:
                info = None
            marker = "absent" if info is None else f"{info.st_mtime_ns}:{info.st_size}"
            digest.update(f"{label}={marker};".encode())
        return digest.hexdigest()[:32]

    def declared_model(self, handle: OpenClawRun) -> str | None:
        """Return the model text OpenClaw reports for the profile if RMD-1 accepts it, else `None`.

        Asked of the runtime itself, the one line of `models status --plain`, read-only. A failure
        or a line that is not a model identifier costs the optional field and nothing else.
        """
        if handle.config is None:
            return None
        try:
            with tempfile.TemporaryDirectory(
                prefix="agentnexus-openclaw-", ignore_cleanup_errors=True
            ) as scratch:
                work = Path(scratch)
                openclaw_arena.make_world(work)
                answer = self._run(
                    handle, work, handle.config, ["models", "status", "--plain"], STEP_SECONDS
                )
        except (OSError, subprocess.TimeoutExpired):
            return None
        lines = answer.stdout.strip().splitlines() if answer.returncode == 0 else []
        text = model_identifier(lines[0]) if lines else ""
        return text if is_declared_model_valid(text) else None

    def launch(self, handle: OpenClawRun, scratch: Path) -> Launch:
        """Return the match process command: it runs the OpenClaw worker as its decision worker."""
        match = Path(arena_match.__file__).resolve()
        worker = Path(openclaw_arena.__file__).resolve()
        if handle.config is None:
            raise DriverRefused("not_isolated", self.display_name)
        try:
            # One configuration for the whole match: a later change to the profile is for the next.
            pinned = openclaw_arena.pin_configuration(handle.config, scratch)
        except (OSError, ValueError) as error:
            raise DriverRefused("not_isolated", self.display_name) from error
        environment = {
            key: value for key, value in os.environ.items() if key.upper() in openclaw_arena.KEPT
        }
        environment.update(
            PYTHONUTF8="1",
            **{
                openclaw_arena.COMMAND_ENV: json.dumps(list(handle.command)),
                openclaw_arena.CONFIG_ENV: str(pinned),  # pinned
                openclaw_arena.SCRATCH_ENV: str(scratch),
                openclaw_arena.PROFILE_ROOT_ENV: str(handle.profile_root or ""),
                openclaw_arena.PROFILE_CONFIG_ENV: str(handle.config or ""),
                openclaw_arena.PROFILE_STATE_ENV: str(handle.state or ""),
            },
        )
        python = sys.executable
        return Launch(
            command=[python, "-I", str(match), "--", python, "-I", str(worker)],
            environment=environment,
        )


def _probe_tools(stdout: str) -> frozenset[str]:
    """Read the tool names of an `mcp probe --json` document; anything else yields none."""
    try:
        document = json.loads(stdout)
    except ValueError:
        return frozenset()
    names = document.get("tools") if isinstance(document, dict) else None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        return frozenset()
    return frozenset(names)


def driver() -> OpenClawArenaDriver:
    """Return the OpenClaw driver; the registry's entry point."""
    return OpenClawArenaDriver()
