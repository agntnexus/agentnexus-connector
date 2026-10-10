"""The Hermes decision worker and its three-tool preflight (agntnexus/agentnexus#223, #228).

Standalone, compatible with Hermes' own Python interpreter. No Connector import: signing and
provider session keys remain in the supervising parent. Only locally constructed game prompts and a
closed stdio tool protocol enter this process. The runtime-neutral match process, the protocol, the
prompts and the contract live in the sibling `arena_match.py`, which this file loads by path.

Hermes runs in a throwaway home of the supervisor's (`HERMES_HOME`); the profile whose model and
credential it plays with is named apart (`AGENTNEXUS_ARENA_PROFILE`). Which provider, model and
authentication that profile has is Hermes' business: this worker asks Hermes' own functions to
resolve them and holds the result to the Arena contract (exactly three tools, no external process).
It names no provider, no model and no credential file (agntnexus/agentnexus#228).
"""

from __future__ import annotations

import contextlib
import copy
import importlib
import importlib.util
import inspect
import json
import os
import queue
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _arena_match() -> Any:
    """Load the sibling `arena_match.py` by path: this runs isolated, under Hermes' interpreter."""
    path = Path(__file__).resolve().with_name("arena_match.py")
    spec = importlib.util.spec_from_file_location("arena_match", path)
    if spec is None or spec.loader is None:
        raise ImportError("The shared Arena match module is missing.")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("arena_match", module)
    spec.loader.exec_module(module)
    return module


arena = _arena_match()

#: The profile whose model section this worker reads and whose provider and credential Hermes
#: resolves. Hermes' own home (`HERMES_HOME`) is a throwaway directory of the supervisor's, because
#: Hermes fills its home with state of its own the moment it starts (agntnexus/agentnexus#223).
PROFILE_ENV = "AGENTNEXUS_ARENA_PROFILE"

#: What a match pinned about the model, in the throwaway home. The supervisor removes it with the
#: home when the match is over, and a worker that replaces another one inside the same match reads
#: it instead of the profile, so that match is played with the model it started with.
PIN_FILE = "arena-pinned-model.json"

#: The only keys of the profile's model section that reach Hermes. Nothing else of the profile does.
MODEL_KEYS = ("default", "provider", "base_url", "context_length")


def assert_tools(definitions: Any) -> None:
    """Fail closed when any tool is added, renamed, missing or duplicated."""
    names = [item["function"]["name"] for item in definitions]
    if len(names) != 3 or set(names) != arena.TOOLS:
        raise ValueError("Hermes exposed tools outside the bounded Arena contract.")


def profile_model() -> dict[str, Any]:
    """Return the model section of the profile, cut down to the four keys Hermes may be given."""
    yaml = importlib.import_module("yaml")
    path = Path(os.environ[PROFILE_ENV]) / "config.yaml"
    local = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    model = local.get("model", {}) if isinstance(local, dict) else None
    if not isinstance(model, dict):
        raise ValueError("Select a model in this Hermes profile.")
    return {key: model[key] for key in MODEL_KEYS if key in model}


def pinned_model() -> dict[str, Any]:
    """Return the model this match plays with: the profile's when the match began, then that one.

    The first worker of a match reads the profile and pins what it found in the throwaway home. A
    worker that replaces it - after a decision that did not end in time - finds the pin and plays
    the same model, whatever the owner changed in the meantime. The change applies from the next
    match, whose throwaway home is a new one. Only the four model keys are pinned, never a secret.
    """
    pin = Path(os.environ["HERMES_HOME"]) / PIN_FILE
    if pin.is_file():
        pinned = json.loads(pin.read_text(encoding="utf-8"))
        if isinstance(pinned, dict) and set(pinned) <= set(MODEL_KEYS):
            return pinned
        raise ValueError("The pinned model is not valid.")
    model = profile_model()
    pin.write_text(json.dumps(model), encoding="utf-8")
    return model


def configure(handler: Any) -> tuple[Any, dict[str, Any]]:
    """Install closed config, three fixed tools and dispatch guards before constructing an agent."""
    config: Any = importlib.import_module("hermes_cli.config")
    safe = copy.deepcopy(config.DEFAULT_CONFIG)
    # The model is the profile's own, whatever provider it names; Hermes resolves it. What Hermes
    # may be given of the profile is this one section: executable credential commands, custom
    # transports and named providers of the profile never reach it.
    safe["model"] = pinned_model()
    safe["tool_search"] = {"enabled": "off"}
    safe["mcp_servers"] = {}
    config.load_config = lambda *args, **kwargs: copy.deepcopy(safe)
    config.load_config_readonly = config.load_config
    search: Any = importlib.import_module("tools.tool_search")
    search.load_config = lambda: search.ToolSearchConfig.from_raw({"enabled": "off"})
    registry = importlib.import_module("tools.registry").registry
    for name in sorted(arena.TOOLS):
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        if name == "game_move":
            parameters.update(
                properties={
                    "column": {"type": "integer", "minimum": 0, "maximum": 6},
                    "move": {"type": "string", "pattern": "^[a-h][1-8][a-h][1-8][qrbn]?$"},
                    "claim": {"enum": sorted(arena.CLAIMS)},
                },
                required=[],
            )
        else:
            parameters["required"] = []
        schema = {
            "name": name,
            "description": {
                "game_join": "Join only the assigned match and seat.",
                "game_state": "Read only the assigned game; waits briefly between reads.",
                "game_move": (
                    "Make one legal move in only the assigned game: a column in Connect Four, "
                    "a move in UCI and optionally a listed draw claim in Chess."
                ),
            }[name],
            "parameters": parameters,
        }
        registry.register(
            name=name,
            toolset="arena_runner",
            schema=schema,
            handler=lambda arguments, *args, _name=name, **kwargs: json.dumps(
                handler(_name, arguments)
            ),
        )
    toolsets = importlib.import_module("toolsets")
    toolsets.create_custom_toolset(
        "arena_runner", "Only this Arena seat", tools=sorted(arena.TOOLS)
    )
    model_tools: Any = importlib.import_module("model_tools")
    original_definitions = model_tools.get_tool_definitions

    def definitions(*args: Any, **kwargs: Any) -> Any:
        """Revalidate every tool assembly, including later refreshes."""
        result = original_definitions(*args, **kwargs)
        assert_tools(result)
        return result

    def dispatch(function_name: str, function_args: Any, *args: Any, **kwargs: Any) -> str:
        """Bypass generic tool dispatch and its hooks; only the three bound handlers execute."""
        arena.bounded_request(function_name, function_args)
        return json.dumps(handler(function_name, function_args))

    model_tools.get_tool_definitions = definitions
    model_tools.handle_function_call = dispatch
    runner: Any = importlib.import_module("run_agent")
    runner.get_tool_definitions = definitions
    runner.handle_function_call = dispatch
    assert_tools(definitions(enabled_toolsets=["arena_runner"], quiet_mode=True))
    required = {
        "enabled_toolsets",
        "max_iterations",
        "run_budget_seconds",
        "skip_context_files",
        "skip_memory",
        "skip_background_review",
    }
    if not required <= set(inspect.signature(runner.AIAgent).parameters):
        raise ValueError("Hermes no longer exposes the reviewed runtime bounds.")
    return runner.AIAgent, safe["model"]


class DecisionComplete(BaseException):
    """Unwind one Hermes conversation once the provider accepted its move.

    Deliberately not an `Exception`: Hermes turns those into a tool error and asks the model again,
    which is the very closing request this ends. Checked against the reviewed revision: a
    `BaseException` raised by a tool handler passes its tool executor and its turn facade, which
    clean up and re-raise it, and the same process then makes the next decision with a fresh agent.
    """


def refuse_external_transport(credentials: dict[str, Any]) -> None:
    """Refuse by shape what would leave the three-tool bound; no provider is named or judged.

    A resolved command, an ACP command, a runtime that hands the whole turn to a subprocess and an
    external-process URL all run a program of their own with tools of their own. Hermes may offer
    them to a profile; the Arena accepts a plain model endpoint and nothing that executes.
    """
    base_url = str(credentials.get("base_url") or "")
    scheme = base_url.partition("://")[0].lower() if "://" in base_url else "https"
    if (
        credentials.get("command")
        or credentials.get("acp_command")
        or "app_server" in str(credentials.get("api_mode") or "")
        or scheme not in {"http", "https"}
    ):
        raise ValueError("Unreviewed external model transport.")


def resolve_credentials(model: dict[str, Any]) -> dict[str, Any]:
    """Ask Hermes' own runtime to resolve the profile's provider, endpoint and authentication.

    No provider, key name or token shape is known here. Two steps, the narrower first:

    1. Hermes resolves in the throwaway home, with the profile's secrets file made visible through
       Hermes' own secret scope (read by Hermes, never by this worker). A provider whose key lives
       there resolves, and nothing was written to the profile.
    2. Only when Hermes answers with its own authentication error - its store is not in the
       throwaway home - the same call is made once more inside the shortest window in which the
       profile is Hermes' home: the context override and the process variable that Hermes' own
       cron ticker and gateway set to serve a profile. Hermes then reads, and when it is due
       refreshes, its credential store under its own lock, exactly as if the owner had started it.
       The window is closed before anything else runs, so logs, caches and the state database of
       this process still land in the throwaway home.

    The credential never leaves this process except in the request Hermes itself makes to the
    resolved endpoint. Nothing is copied, linked or logged. The resolver's credential pool belongs
    to the profile's store: a refresh made through it later, in the throwaway home, would spend a
    single-use token where the profile can never read the rotated one. It is never handed on.
    """
    profile = Path(os.environ[PROFILE_ENV])
    constants: Any = importlib.import_module("hermes_constants")
    resolver: Any = importlib.import_module("hermes_cli.runtime_provider")
    secrets: Any = importlib.import_module("agent.secret_scope")
    refused: Any = importlib.import_module("hermes_cli.auth").AuthError

    def ask() -> dict[str, Any]:
        runtime: dict[str, Any] = resolver.resolve_runtime_provider(
            requested=model.get("provider"),
            target_model=model.get("default"),
            explicit_base_url=model.get("base_url"),
        )
        return runtime

    credentials: dict[str, Any] | None = None
    scope = secrets.set_secret_scope(secrets.build_profile_secret_scope(profile))
    try:
        credentials = ask()
    except refused:
        credentials = None
    finally:
        secrets.reset_secret_scope(scope)
    if credentials is None:
        saved = os.environ["HERMES_HOME"]
        override = constants.set_hermes_home_override(str(profile))
        scope = secrets.set_secret_scope(secrets.build_profile_secret_scope(profile))
        os.environ["HERMES_HOME"] = str(profile)
        try:
            credentials = ask()
        finally:
            os.environ["HERMES_HOME"] = saved
            secrets.reset_secret_scope(scope)
            constants.reset_hermes_home_override(override)
    refuse_external_transport(credentials)
    credentials.pop("credential_pool", None)
    return credentials


def run_decision(
    agent_type: Any,
    model: dict[str, Any],
    decision: dict[str, Any],
    send: Callable[[dict[str, Any]], None],
) -> None:
    """Make one decision with a fresh agent, then clean it up; every ending is one fixed message.

    Hermes resolves the credential again just before the decision: it refreshes a grant only when
    one is about to expire, so no decision straddles an expiry and a rotated grant is the one used.
    """
    agent: Any = None
    try:
        try:
            credentials = resolve_credentials(model)
            agent = agent_type(
                model=model.get("default", ""),
                provider=credentials.get("provider"),
                requested_provider=credentials.get("requested_provider"),
                base_url=credentials.get("base_url"),
                api_key=credentials.get("api_key"),
                api_mode=credentials.get("api_mode"),
                enabled_toolsets=["arena_runner"],
                max_iterations=3,
                run_budget_seconds=decision["seconds"],
                max_tokens=2048,
                skip_context_files=True,
                skip_memory=True,
                skip_background_review=True,
                load_soul_identity=False,
                quiet_mode=True,
                save_trajectories=False,
                checkpoints_enabled=False,
                ephemeral_system_prompt=arena.system_prompt(decision["game"]),
            )
            agent._skip_mcp_refresh = True
            agent._persist_disabled = True
            assert_tools(agent.tools)
            result = agent.run_conversation(
                arena.decision_prompt(
                    decision["game"], decision["role"], decision["seat"], decision["state"]
                )
            )
        except DecisionComplete:
            send({"decision": "completed"})
        except Exception:
            send({"decision": "exception"})
        else:
            if not isinstance(result, dict):
                outcome = "invalid"
            elif result.get("failed") or result.get("error"):
                # The runtime already classified the failure. Never restart its retry budget.
                outcome = "failed"
            else:
                outcome = "ok"
            send({"decision": "returned", "outcome": outcome})
    finally:
        cleanup = "closed"
        if agent is not None:
            try:
                agent.close()
            except Exception:
                cleanup = "close_failed"
        send({"decision": cleanup})


def worker_main(
    input_stream: Any = None,
    output: Any = None,
    exit_hard: Callable[[int], object] = os._exit,
) -> int:
    """Serve decisions for the match process until it stops us, or until it is gone."""
    input_stream = sys.stdin if input_stream is None else input_stream
    output = sys.stdout if output is None else output
    incoming: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        # The match process closing this pipe means it is gone. Nothing may outlive it, not even
        # a model call blocked in a transport; only a thread outside that call can end it.
        with contextlib.suppress(OSError, ValueError):
            for line in iter(lambda: input_stream.readline(65537), ""):
                incoming.put(line)
        exit_hard(3)

    def send(document: dict[str, Any]) -> None:
        output.write(json.dumps(document) + "\n")
        output.flush()

    def tool(operation: str, arguments: Any) -> Any:
        """Use private stdio; no URL, key, shell or alternate match can be supplied."""
        send(arena.bounded_request(operation, arguments))
        reply = arena.line_document(incoming.get())
        if reply == {"complete": True}:
            raise DecisionComplete
        if set(reply) != {"result"}:
            raise ValueError("Invalid supervised game response.")
        return reply["result"]

    threading.Thread(target=pump, daemon=True).start()
    with contextlib.redirect_stdout(sys.stderr):
        agent_type, model = configure(tool)
        # Ready means the profile's credential resolved: a worker that could not play never joins.
        resolve_credentials(model)
        send({"ready": True})
        while True:
            command = arena.line_document(incoming.get())
            if command == {"stop": True}:
                return 0
            run_decision(agent_type, model, arena.checked_decision(command), send)


def homes_are_apart() -> bool:
    """Return whether Hermes has a home of its own, which is not the profile this adapter reads."""
    home, profile = os.environ.get("HERMES_HOME"), os.environ.get(PROFILE_ENV)
    if not home or not profile:
        return False
    return os.path.normcase(os.path.realpath(home)) != os.path.normcase(os.path.realpath(profile))


def main() -> int:
    """Preflight without inference, or serve decisions as the worker."""
    if len(sys.argv) != 3 or sys.argv[2] not in {"--decision", "--preflight"}:
        return 2
    if any(
        os.environ.get(name) != "1"
        for name in ("HERMES_SAFE_MODE", "HERMES_IGNORE_RULES", "HERMES_IGNORE_USER_CONFIG")
    ):
        return 2
    if not homes_are_apart():
        # Hermes would fill the profile it plays from. Refuse before it is imported.
        return 2
    sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
    if sys.argv[2] == "--decision":
        return worker_main()
    with contextlib.redirect_stdout(sys.stderr):
        _, model = configure(lambda operation, arguments: None)
        # The credential path is part of the proof: a profile whose provider Hermes cannot
        # authenticate is refused before an intent is claimed, not after.
        resolve_credentials(model)
    sys.stdout.write(json.dumps({"bounded": True, "tools": sorted(arena.TOOLS)}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Do not expose provider errors, credentials, config or model output in service logs.
        with contextlib.suppress(OSError):
            arena.diagnostic(sys.stdout, "runtime_exception")
        raise SystemExit(3) from None
