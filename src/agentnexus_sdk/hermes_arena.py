"""The Hermes decision worker and its three-tool preflight (agntnexus/agentnexus#223, #228).

Standalone, compatible with Hermes' own Python interpreter. No Connector import: signing and
provider session keys remain in the supervising parent. Only locally constructed game prompts and a
closed stdio tool protocol enter this process. The runtime-neutral match process, the protocol, the
prompts and the contract live in the sibling `arena_match.py`, which this file loads by path.

Hermes runs in a throwaway home of the supervisor's (`HERMES_HOME`); the profile whose config and
credentials it plays with is named apart (`AGENTNEXUS_ARENA_PROFILE`) and only read.
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

#: The profile whose `config.yaml` and `.env` this adapter reads and nothing else. Hermes' own home
#: (`HERMES_HOME`) is a throwaway directory of the supervisor's, because Hermes fills its home with
#: state of its own the moment it starts (agntnexus/agentnexus#223).
PROFILE_ENV = "AGENTNEXUS_ARENA_PROFILE"


def assert_tools(definitions: Any) -> None:
    """Fail closed when any tool is added, renamed, missing or duplicated."""
    names = [item["function"]["name"] for item in definitions]
    if len(names) != 3 or set(names) != arena.TOOLS:
        raise ValueError("Hermes exposed tools outside the bounded Arena contract.")


def configure(handler: Any) -> tuple[Any, dict[str, Any]]:
    """Install closed config, three fixed tools and dispatch guards before constructing an agent."""
    config: Any = importlib.import_module("hermes_cli.config")
    safe = copy.deepcopy(config.DEFAULT_CONFIG)
    # Model choice is local; executable credential commands and custom transports are refused.
    home = Path(os.environ[PROFILE_ENV])
    yaml = importlib.import_module("yaml")
    local = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    model = local.get("model", {})
    if not isinstance(model, dict):
        raise ValueError("Select a model and a direct API provider in this Hermes profile.")
    provider = model.get("provider", "openrouter")
    if provider not in {"openrouter", "openai", "anthropic"}:
        raise ValueError("This provider has not passed bounded Arena transport review.")
    safe["model"] = {
        key: model[key]
        for key in ("default", "provider", "base_url", "context_length")
        if key in model
    }
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


def load_credentials(model: dict[str, Any]) -> dict[str, Any]:
    """Load only this profile's direct API keys and resolve the one reviewed model transport."""
    dotenv = importlib.import_module("dotenv")
    values = dotenv.dotenv_values(Path(os.environ[PROFILE_ENV]) / ".env", interpolate=False)
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if values.get(key):
            os.environ[key] = values[key]
    provider = model.get("provider", "openrouter")
    credentials: dict[str, Any] = importlib.import_module(
        "hermes_cli.runtime_provider"
    ).resolve_runtime_provider(
        requested=provider,
        target_model=model.get("default"),
        explicit_base_url=model.get("base_url"),
    )
    if (
        credentials.get("provider") not in {"openrouter", "openai", "anthropic"}
        or credentials.get("command")
        or credentials.get("acp_command")
    ):
        raise ValueError("Unreviewed external model transport.")
    return credentials


def run_decision(
    agent_type: Any,
    model: dict[str, Any],
    credentials: dict[str, Any],
    decision: dict[str, Any],
    send: Callable[[dict[str, Any]], None],
) -> None:
    """Make one decision with a fresh agent, then clean it up; every ending is one fixed message."""
    agent: Any = None
    try:
        try:
            agent = agent_type(
                model=model.get("default", ""),
                provider=credentials.get("provider"),
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
        credentials = load_credentials(model)
        send({"ready": True})
        while True:
            command = arena.line_document(incoming.get())
            if command == {"stop": True}:
                return 0
            run_decision(agent_type, model, credentials, arena.checked_decision(command), send)


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
        configure(lambda operation, arguments: None)
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
