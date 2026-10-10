"""#228: the Arena knows runtimes by what they prove, never by which model or provider they run.

AgentNexus does not know models or providers; the selected runtime owns the provider, the model, the
authentication and the inference. A runtime driver is accepted for process, tool, deadline and
isolation capabilities. These tests hold the contract (exactly three operations, a killable worker,
a bounded cleanup, a deadline the supervisor keeps), the refusals (a fixed code, never a runtime's
own text), the choice of a driver (by the runtime's name and nothing else) and the static boundary
of the runtime-neutral code (it names no model, no provider and no runtime).
"""

from __future__ import annotations

import ast
import io
import json
import re
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from arena_fake_driver import FakeArenaDriver
from arena_fakes import expect_guard, intent, load_mutant, supervisor

from agentnexus_sdk import arena_driver, arena_driver_hermes, arena_match, arena_runner, runtimes
from agentnexus_sdk.arena_driver import CONTRACT, Capabilities, DriverRefusedError

SOURCE = Path(arena_match.__file__).parent
THREE = frozenset({"game_join", "game_state", "game_move"})


def write_behavior(folder: Path, **behavior: Any) -> tuple[Path, Path]:
    """Write a fake runtime's behaviour and a disposable profile with a fake credential."""
    profile = folder / "profile"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("SYNTHETIC_KEY=synthetic-disposable\n", encoding="utf-8")
    path = folder / "behavior.json"
    path.write_text(json.dumps({"mode": "fast", **behavior}), encoding="utf-8")
    return path, profile


# ---------------------------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------------------------


def test_the_contract_is_three_operations_a_killable_worker_a_bounded_cleanup_and_a_deadline() -> (
    None
):
    """Nothing else is a capability the Arena asks of a runtime, and nothing less is enough."""
    expected = Capabilities(
        tools=THREE, worker_killable=True, cleanup_bounded=True, deadline_external=True
    )
    assert expected == CONTRACT
    assert arena_match.TOOLS == THREE


def test_a_driver_that_declares_the_contract_is_accepted() -> None:
    """The reference drivers satisfy the contract."""
    arena_driver.require_contract(arena_driver_hermes.driver())
    arena_driver.require_contract(
        SimpleNamespace(capabilities=CONTRACT, display_name="Fake")  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("extra", ["shell", "read_file", "browser", "agentnexus_post"])
def test_a_driver_that_declares_a_fourth_tool_is_refused(extra: str) -> None:
    """A fourth operation is the end of the Arena's isolation, whatever the runtime calls it."""
    capabilities = Capabilities(
        tools=THREE | {extra}, worker_killable=True, cleanup_bounded=True, deadline_external=True
    )
    driver = SimpleNamespace(capabilities=capabilities, display_name="Fake")
    with pytest.raises(DriverRefusedError) as raised:
        arena_driver.require_contract(driver)  # type: ignore[arg-type]
    assert raised.value.code == "contract_violated"


def test_a_driver_that_declares_fewer_tools_is_refused() -> None:
    """A runtime that cannot play with all three operations cannot play."""
    capabilities = Capabilities(
        tools=THREE - {"game_state"},
        worker_killable=True,
        cleanup_bounded=True,
        deadline_external=True,
    )
    with pytest.raises(DriverRefusedError):
        arena_driver.require_contract(
            SimpleNamespace(capabilities=capabilities, display_name="Fake")  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("missing", ["worker_killable", "cleanup_bounded", "deadline_external"])
def test_a_driver_without_cleanup_or_deadline_capability_is_refused(missing: str) -> None:
    """A worker nobody can kill, an unbounded cleanup or a deadline the runtime keeps is refused."""
    flags = {"worker_killable": True, "cleanup_bounded": True, "deadline_external": True}
    flags[missing] = False
    driver = SimpleNamespace(capabilities=Capabilities(tools=THREE, **flags), display_name="Fake")
    with pytest.raises(DriverRefusedError) as raised:
        arena_driver.require_contract(driver)  # type: ignore[arg-type]
    assert raised.value.code == "contract_violated"


@pytest.mark.parametrize(
    ("tools", "accepted"),
    [
        (sorted(THREE), True),
        (sorted(THREE | {"shell"}), False),
        (sorted(THREE - {"game_move"}), False),
        ([], False),
    ],
)
def test_a_runtime_that_exposes_anything_but_the_three_tools_in_its_preflight_is_refused(
    tmp_path: Path, tools: list[str], accepted: bool
) -> None:
    """The preflight is the proof: the worker is asked, without inference, what it exposes."""
    behavior, profile = write_behavior(tmp_path, tools=tools)
    driver = FakeArenaDriver(behavior, profile)
    handle = driver.inspect(None)
    if accepted:
        arena_driver.check_preflight(driver, handle)
        return
    with pytest.raises(DriverRefusedError) as raised:
        arena_driver.check_preflight(driver, handle)
    assert raised.value.code == "contract_violated"


def test_a_runtime_whose_preflight_does_not_answer_is_refused_with_a_fixed_sentence(
    tmp_path: Path,
) -> None:
    """A worker that crashes says nothing the operator ever sees."""
    behavior, profile = write_behavior(tmp_path)
    broken = tmp_path / "broken_worker.py"
    broken.write_text("import sys\nsys.exit('synthetic-private-runtime-error')\n", encoding="utf-8")
    driver = FakeArenaDriver(behavior, profile, worker=broken)
    with pytest.raises(DriverRefusedError) as raised:
        arena_driver.check_preflight(driver, driver.inspect(None))
    assert raised.value.code == "preflight_refused"
    assert "synthetic-private" not in str(raised.value)
    assert str(raised.value) == "Fake refused the exact three-tool Arena preflight."


@pytest.mark.parametrize(
    "stdout",
    [
        '{"bounded": true, "tools": ["game_join", "game_move", "game_state"]}\n',
        'warning: a notice\n{"bounded": true, "tools": ["game_join", "game_move", "game_state"]}\n',
    ],
)
def test_the_preflight_document_is_read_from_its_last_line(stdout: str) -> None:
    """A runtime may print notices first; the closed document is the last JSON line."""
    assert arena_driver.parse_preflight(stdout) == THREE


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json\n",
        '{"bounded": true}\n',
        '{"bounded": false, "tools": []}\n',
        '{"bounded": true, "tools": [1, 2, 3]}\n',
        '{"bounded": true, "tools": [], "model": "synthetic-model"}\n',
        '{"bounded": true, "tools": ["game_join"]}\nstray text\n{"other": 1}\n',
    ],
)
def test_anything_but_the_closed_preflight_document_is_not_a_preflight(stdout: str) -> None:
    """Extra fields, other shapes and noise after the document all fail closed."""
    assert arena_driver.parse_preflight(stdout) is None


# ---------------------------------------------------------------------------------------------
# Refusals are fixed codes and fixed sentences
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(arena_driver.REFUSALS))
def test_every_refusal_is_a_code_with_a_fixed_sentence(code: str) -> None:
    """A refusal carries no runtime text: the sentence is the table's, with the display name."""
    refusal = arena_driver.DriverRefused(code, "Fake")
    assert refusal.code == code
    assert str(refusal) == arena_driver.REFUSALS[code].format(runtime="Fake")


def test_an_unknown_refusal_code_is_a_defect() -> None:
    """A driver cannot invent a reason: free text has nowhere to go."""
    with pytest.raises(ValueError, match="Unknown Arena driver refusal code"):
        arena_driver.DriverRefused("synthetic-private-runtime-error", "Fake")


def test_the_hermes_sentences_are_the_ones_operators_already_know() -> None:
    """Moving the checks behind the interface changed no sentence an operator reads."""
    driver = arena_driver_hermes.driver()
    sentences = {
        code: str(arena_driver.DriverRefused(code, driver.display_name))
        for code in ("not_isolated", "unreviewed", "preflight_refused")
    }
    assert sentences == {
        "not_isolated": "Automatic Arena play requires an isolated named Hermes profile.",
        "unreviewed": "This Hermes source has not passed the bounded Arena compatibility review.",
        "preflight_refused": "Hermes refused the exact three-tool Arena preflight.",
    }


# ---------------------------------------------------------------------------------------------
# A driver is chosen by the runtime's name and nothing else
# ---------------------------------------------------------------------------------------------


def test_the_registry_knows_drivers_by_the_runtimes_name() -> None:
    """The built-in table is names; the Hermes driver is imported only when it is asked for."""
    assert "hermes" in arena_driver.known()
    driver = arena_driver.driver_for("hermes")
    assert driver.name == "hermes" and driver.capabilities == CONTRACT


def test_an_unknown_runtime_has_no_driver() -> None:
    """Nothing is guessed: a name without a driver is refused with a fixed code."""
    with pytest.raises(DriverRefusedError) as raised:
        arena_driver.driver_for("synthetic-runtime")
    assert raised.value.code == "unknown_driver"


def test_a_registered_driver_is_found_by_its_name_and_can_be_removed(tmp_path: Path) -> None:
    """A second runtime plugs in by name, and unplugging it leaves no trace."""
    behavior, profile = write_behavior(tmp_path)
    fake = FakeArenaDriver(behavior, profile)
    arena_driver.register("fake", lambda: fake)
    try:
        assert arena_driver.driver_for("fake") is fake
        assert "fake" in arena_driver.known()
    finally:
        arena_driver.unregister("fake")
    assert "fake" not in arena_driver.known()


@pytest.mark.parametrize(
    ("recorded", "requested", "chosen"),
    [
        (["hermes"], None, "hermes"),
        (["hermes", "synthetic-other"], None, "hermes"),
        (["synthetic-other", "hermes"], "hermes", "hermes"),
    ],
)
def test_the_runtime_is_the_one_the_profile_was_set_up_with(
    recorded: list[str], requested: str | None, chosen: str
) -> None:
    """The profile's recorded runtime, or the one asked for, and no model or provider anywhere."""
    assert arena_driver.runtime_of(recorded, requested) == chosen


@pytest.mark.parametrize(
    ("recorded", "requested", "code"),
    [
        ([], None, "unknown_driver"),
        (["synthetic-other"], None, "unknown_driver"),
        (["hermes"], "synthetic-other", "unknown_driver"),
    ],
)
def test_a_runtime_without_a_driver_is_refused(
    recorded: list[str], requested: str | None, code: str
) -> None:
    """Refused before anything is inspected, claimed or started."""
    with pytest.raises(DriverRefusedError) as raised:
        arena_driver.runtime_of(recorded, requested)
    assert raised.value.code == code


def test_two_drivers_for_one_profile_need_a_decision_not_a_guess(tmp_path: Path) -> None:
    """A profile set up with two runtimes that have drivers must be told which one plays."""
    behavior, profile = write_behavior(tmp_path)
    arena_driver.register("fake", lambda: FakeArenaDriver(behavior, profile))
    try:
        with pytest.raises(DriverRefusedError) as raised:
            arena_driver.runtime_of(["hermes", "fake"], None)
        assert raised.value.code == "ambiguous_runtime"
        assert arena_driver.runtime_of(["hermes", "fake"], "fake") == "fake"
    finally:
        arena_driver.unregister("fake")


# ---------------------------------------------------------------------------------------------
# The declared model is the runtime's own text, through the one RMD-1 path
# ---------------------------------------------------------------------------------------------


class StubHermes:
    """A Hermes adapter that reports what a test says, so that no real Hermes is ever asked."""

    reported = "synthetic-model-a"

    def __init__(self, context: Any = None) -> None:
        """Remember the context the driver passed."""
        self.context = context

    def model_status(self) -> runtimes.ModelStatus:
        """Report the configured text, or nothing."""
        if self.reported is None:
            return runtimes.ModelStatus(configured=False, known=True, detail="none")
        return runtimes.ModelStatus(
            configured=True, known=True, detail=self.reported, value=self.reported
        )


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("synthetic-model-a", "synthetic-model-a"),
        ("vendor/model-1.5:free", "vendor/model-1.5:free"),
        (None, None),
        ("a model with spaces", None),
        ("x" * 500, None),
    ],
)
def test_the_hermes_driver_forwards_only_what_the_forum_would_send(
    monkeypatch: pytest.MonkeyPatch, reported: str | None, expected: str | None
) -> None:
    """The same text, under the same RMD-1 validator, or nothing: never a second rule."""
    monkeypatch.setattr(StubHermes, "reported", reported)
    monkeypatch.setattr(arena_driver_hermes, "HermesAdapter", StubHermes)
    handle = arena_driver_hermes.HermesRun(
        Path("."), Path("."), Path("."), runtimes.RuntimeContext.shared()
    )
    assert arena_driver_hermes.driver().declared_model(handle) == expected


def test_a_runtime_that_cannot_be_asked_costs_the_field_and_not_the_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any failure of the model report is a missing declaration."""

    class Broken(StubHermes):
        def model_status(self) -> runtimes.ModelStatus:
            raise RuntimeError("synthetic-private-runtime-error")

    monkeypatch.setattr(arena_driver_hermes, "HermesAdapter", Broken)
    handle = arena_driver_hermes.HermesRun(
        Path("."), Path("."), Path("."), runtimes.RuntimeContext.shared()
    )
    assert arena_driver_hermes.driver().declared_model(handle) is None


def test_a_handle_without_a_context_declares_no_model() -> None:
    """Nothing is looked up when there is nothing to ask."""
    handle = arena_driver_hermes.HermesRun(Path("."), Path("."), Path("."))
    assert arena_driver_hermes.driver().declared_model(handle) is None


# ---------------------------------------------------------------------------------------------
# The runtime-neutral code names no model, no provider and no runtime
# ---------------------------------------------------------------------------------------------

#: Model families, providers and routes. They may appear in a driver's fixtures and in prose about
#: a driver, never in code that decides what the Arena accepts.
#: Names distinctive enough to be refused anywhere, even inside an identifier or a longer word.
DISTINCTIVE_NAMES = (
    r"openai|anthropic|openrouter|codex|claude|ollama|gemini|mistral|llama|deepseek|qwen|"
    r"huggingface|perplexity|bedrock|moonshot|fireworks|zhipu|minimax|inkling"
)
#: Short names that are also words or parts of words: refused as whole words, where an underscore,
#: a digit or a hyphen ends a word as a letter does not.
SHORT_NAMES = r"gpt[-_ ]?\d|vertex|azure|groq|xai|grok|cohere|kimi|nvidia|luna|haiku|sonnet|opus"
MODEL_AND_PROVIDER_NAMES = re.compile(
    rf"{DISTINCTIVE_NAMES}|(?<![a-z])(?:{SHORT_NAMES})(?![a-z])", re.IGNORECASE
)
RUNTIME_NAMES = re.compile(r"hermes|openclaw", re.IGNORECASE)
NEUTRAL = ("arena_runner.py", "arena_match.py", "arena_driver.py")


@pytest.mark.parametrize("name", NEUTRAL)
def test_the_runtime_neutral_code_contains_no_model_or_provider_name(name: str) -> None:
    """Not in code, not in a docstring, not in a comment: the boundary is the whole file."""
    text = (SOURCE / name).read_text(encoding="utf-8")
    found = sorted({m.group(0).lower() for m in MODEL_AND_PROVIDER_NAMES.finditer(text)})
    assert found == [], f"{name} names a model or a provider: {found}"


@pytest.mark.parametrize("name", ["arena_runner.py", "arena_match.py"])
def test_the_supervisor_and_the_match_process_name_no_runtime_either(name: str) -> None:
    """Only the driver registry knows runtimes by name; the supervisor learns them from it."""
    text = (SOURCE / name).read_text(encoding="utf-8")
    found = sorted({m.group(0).lower() for m in RUNTIME_NAMES.finditer(text)})
    assert found == [], f"{name} names a runtime: {found}"


def test_the_registry_is_the_one_neutral_file_that_names_a_runtime() -> None:
    """A runtime appears in `arena_driver.py` only as a key of the table of drivers."""
    lines = (SOURCE / "arena_driver.py").read_text(encoding="utf-8").splitlines()
    named = [line for line in lines if RUNTIME_NAMES.search(line)]
    assert len(named) == 1 and named[0].lstrip().startswith("_BUILTIN")


def imported_modules(path: Path) -> set[str]:
    """Return every module name a source file imports statically."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.update(f"{base}.{alias.name}".strip(".") for alias in node.names)
            found.add(base)
    return found


def test_the_supervisor_imports_no_runtime_and_no_driver() -> None:
    """The parent that holds the signing key imports no agent, no adapter and no driver."""
    imported = imported_modules(SOURCE / "arena_runner.py")
    forbidden = {
        "agentnexus_sdk.runtimes",
        "agentnexus_sdk.hermes_arena",
        "agentnexus_sdk.arena_driver_hermes",
        "agentnexus_sdk.openclaw_arena",
        "agentnexus_sdk.arena_driver_openclaw",
    }
    assert not imported & forbidden, sorted(imported & forbidden)
    assert "agentnexus_sdk.arena_driver" in imported


def test_the_match_process_is_standard_library_only() -> None:
    """It runs isolated, under whichever interpreter a driver names, without the Connector."""
    stdlib = {
        "contextlib",
        "json",
        "os",
        "queue",
        "re",
        "subprocess",
        "sys",
        "threading",
        "time",
        "typing",
        "__future__",
    }
    modules = {name.split(".")[0] for name in imported_modules(SOURCE / "arena_match.py")}
    assert modules - {""} <= stdlib, sorted(modules - stdlib)


def test_the_driver_registry_imports_no_runtime() -> None:
    """The neutral contract imports the protocol it checks against and nothing of a runtime."""
    imported = imported_modules(SOURCE / "arena_driver.py")
    assert not {n for n in imported if n.startswith("agentnexus_sdk.runtimes")}
    assert not {n for n in imported if "hermes" in n or "openclaw" in n}


# ---------------------------------------------------------------------------------------------
# The signing key stays with the parent
# ---------------------------------------------------------------------------------------------


def launched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    driver: Any,
    module: ModuleType = arena_runner,
) -> tuple[dict[str, Any], io.StringIO]:
    """Launch a match with a driver while `Popen` is recorded instead of run; return what it got."""
    runner, owned = supervisor(module)
    runner.journal = SimpleNamespace(runner_id=str(uuid.uuid4()), reserve=lambda identifier: True)
    runner.paths = SimpleNamespace(root=tmp_path)
    arena_driver.require_contract(driver)
    runner.driver, runner.handle = driver, driver.inspect(None)
    document = {
        **intent(owned.agent_id),
        "status": "starting",
        "claimed_by": runner.journal.runner_id,
    }
    document.update(intent_id=owned.intent_id, match_id=owned.match_id)
    runner._post = lambda suffix, payload: document
    seen: dict[str, Any] = {}
    stdin = io.StringIO()

    def spawn(command: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs, command=command)
        return SimpleNamespace(stdin=stdin, stdout=io.StringIO())

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        module.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None)
    )
    monkeypatch.setattr(module.tempfile, "tempdir", str(tmp_path))
    runner._launch(owned)
    module.remove_scratch(runner.scratch)
    return seen, stdin


def test_the_runtime_gets_neither_the_signing_key_nor_the_parents_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only what the driver names reaches the runtime: no key path, no identity, no other secret."""
    key = tmp_path / "keys" / "agent.pem"
    monkeypatch.setenv("AGENTNEXUS_PRIVATE_KEY_FILE", str(key))
    monkeypatch.setenv("AGENTNEXUS_AGENT_ID", str(uuid.uuid4()))
    monkeypatch.setenv("AGENTNEXUS_KEY_ID", "synthetic-key-id")
    monkeypatch.setenv("SYNTHETIC_PROVIDER_API_KEY", "synthetic-secret")
    behavior, profile = write_behavior(tmp_path / "fake")
    seen, stdin = launched(monkeypatch, tmp_path, FakeArenaDriver(behavior, profile))
    environment = seen["env"]
    assert isinstance(environment, dict)
    assert not {k for k in environment if k.startswith("AGENTNEXUS_")}
    assert "SYNTHETIC_PROVIDER_API_KEY" not in environment
    everything = json.dumps([seen["command"], environment, stdin.getvalue()])
    assert "agent.pem" not in everything and "synthetic-key-id" not in everything
    assert "synthetic-secret" not in everything


def test_the_match_process_is_told_only_the_match_the_seat_and_the_run_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one line the supervisor writes to the match process is closed data."""
    behavior, profile = write_behavior(tmp_path / "fake")
    _, stdin = launched(monkeypatch, tmp_path, FakeArenaDriver(behavior, profile))
    request = json.loads(stdin.getvalue())
    assert set(request) == {"match_id", "seat", "seconds"} and request["seconds"] == 3600


def test_the_match_command_is_the_neutral_match_program_then_the_workers_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The supervisor starts one program, the neutral one; the runtime's worker is its argument."""
    behavior, profile = write_behavior(tmp_path / "fake")
    seen, _ = launched(monkeypatch, tmp_path, FakeArenaDriver(behavior, profile))
    command = seen["command"]
    assert Path(command[2]).name == "arena_match.py" and command[3] == "--"
    assert Path(command[6]).name == "fake_runtime_worker.py"


# ---------------------------------------------------------------------------------------------
# Mutation proofs: break the protected condition, require the guard to refuse
# ---------------------------------------------------------------------------------------------


def contract_oracle(module: ModuleType) -> None:
    """Require the contract to refuse a fourth tool and each missing capability."""
    flags = {"worker_killable": True, "cleanup_bounded": True, "deadline_external": True}
    cases = [Capabilities(tools=module.CONTRACT.tools | {"shell"}, **flags)]
    cases += [Capabilities(tools=module.CONTRACT.tools, **{**flags, name: False}) for name in flags]
    for capabilities in cases:
        driver = SimpleNamespace(capabilities=capabilities, display_name="Fake")
        try:
            module.require_contract(driver)
        except module.DriverRefusedError:
            continue
        raise AssertionError(f"a driver declaring {capabilities} was accepted")


def refusal_text_oracle(module: ModuleType) -> None:
    """Require a refusal to be a code of the table and its sentence, never free text."""
    try:
        module.DriverRefused("synthetic-private-runtime-error", "Fake")
    except ValueError as error:
        assert "synthetic-private" not in str(error)
        return
    raise AssertionError("a free-text refusal was accepted")


def tools_oracle(module: ModuleType, tmp_path: Path) -> None:
    """Require the preflight proof to refuse a runtime that exposes a fourth tool."""
    behavior, profile = write_behavior(tmp_path / uuid.uuid4().hex, tools=[*sorted(THREE), "shell"])
    driver = FakeArenaDriver(behavior, profile)
    try:
        module.check_preflight(driver, driver.inspect(None))
    except module.DriverRefusedError:
        return
    raise AssertionError("a runtime exposing a fourth tool passed its preflight")


@pytest.mark.parametrize(
    ("original", "replacement", "oracle"),
    [
        ("if driver.capabilities != CONTRACT:", "if False:", "contract"),
        (
            "if driver.capabilities != CONTRACT:",
            "if driver.capabilities.tools != CONTRACT.tools:",
            "contract",
        ),
        (
            "        if code not in REFUSALS:\n"
            '            raise ValueError("Unknown Arena driver refusal code.")\n'
            "        super().__init__(REFUSALS[code].format(runtime=runtime))\n",
            "        super().__init__(REFUSALS.get(code, code).format(runtime=runtime))\n",
            "refusal",
        ),
        (
            "    if tools != arena_match.TOOLS:\n",
            "    if not tools >= arena_match.TOOLS:\n",
            "tools",
        ),
    ],
    ids=[
        "contract-not-checked",
        "deadline-and-cleanup-left-to-the-driver",
        "free-text-refusal",
        "fourth-tool-allowed",
    ],
)
def test_a_weakened_contract_is_noticed(
    tmp_path: Path, original: str, replacement: str, oracle: str
) -> None:
    """Each condition of the contract is load-bearing: weakened, its own check fails."""
    mutant = load_mutant(tmp_path / "mutant", arena_driver, original, replacement)
    checks = {
        "contract": contract_oracle,
        "refusal": refusal_text_oracle,
        "tools": lambda module: tools_oracle(module, tmp_path),
    }
    try:
        expect_guard(checks[oracle], arena_driver, mutant)
    finally:
        sys.modules.pop(mutant.__name__, None)


def environment_oracle(module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require the runtime to start with the driver's environment and nothing of the parent's."""
    monkeypatch.setenv("AGENTNEXUS_PRIVATE_KEY_FILE", str(tmp_path / "keys" / "agent.pem"))
    behavior, profile = write_behavior(tmp_path / uuid.uuid4().hex)
    seen, _ = launched(monkeypatch, tmp_path, FakeArenaDriver(behavior, profile), module)
    environment = seen["env"]
    assert isinstance(environment, dict) and "AGENTNEXUS_PRIVATE_KEY_FILE" not in environment


def test_a_runner_that_hands_the_parents_environment_to_the_runtime_is_noticed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signing key's path lives in the parent's world; inheriting that world is how it leaks."""
    mutant = load_mutant(tmp_path / "mutant", arena_runner, "env=launch.environment,", "env=None,")
    try:
        expect_guard(
            lambda module: environment_oracle(module, tmp_path, monkeypatch), arena_runner, mutant
        )
    finally:
        sys.modules.pop(mutant.__name__, None)


def imported_text(text: str) -> set[str]:
    """Return the modules a source text imports statically."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.update(f"{base}.{alias.name}".strip(".") for alias in node.names)
            found.add(base)
    return found


@pytest.mark.parametrize(
    ("addition", "guard"),
    [
        ("\nfrom agentnexus_sdk.runtimes import HermesAdapter\n", "imports"),
        ("\nfrom agentnexus_sdk import hermes_arena\n", "imports"),
        ("\nfrom agentnexus_sdk.arena_driver_hermes import HermesRun\n", "imports"),
        ('\nif provider == "synthetic-openai":\n    pass\n', "names"),
        ("\nCODEX_PATH = True\n", "names"),
        ("\nif model.startswith('claude'):\n    pass\n", "names"),
        ('\nDISPLAY = {"gpt-5": "GPT-5"}\n', "names"),
    ],
)
def test_the_static_guards_notice_a_runtime_or_a_model_in_the_neutral_code(
    addition: str, guard: str
) -> None:
    """Adding a runtime import or a model or provider name to the supervisor fails its guard."""
    text = (SOURCE / "arena_runner.py").read_text(encoding="utf-8") + addition
    if guard == "imports":
        forbidden = {"runtimes", "hermes_arena", "arena_driver_hermes"}
        found = {name.rsplit(".", 1)[-1] for name in imported_text(text)}
        assert found & forbidden, "the guard would not have noticed"
    else:
        assert MODEL_AND_PROVIDER_NAMES.search(text), "the guard would not have noticed"
