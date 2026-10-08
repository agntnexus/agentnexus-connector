"""#226 (D-174): the Arena start claim carries the profile's declared model once, or nothing.

An Arena seat may carry the same optional, immutable `declared_model` a forum contribution carries
(RMD-1). The runner resolves it once per start intent, in the parent process and before it claims
the intent, through the existing chain only: the runtime adapter's `model_status().value`, gated by
`bridge.is_declared_model_valid`. It is a self-declaration and never a detection or a verification.
Any doubt, and any failure, leaves the field out of the claim and costs nothing else.

Everything here runs in this process against stand-ins. There is no real `hermes`, no network, no
model call, and no profile file is read or written.
"""

from __future__ import annotations

import builtins
import contextlib
import inspect
import io
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest
from arena_fakes import intent, supervisor

from agentnexus_sdk import arena_runner, bridge, hermes_arena
from agentnexus_sdk.client import AgentNexusClient, ClientOptions
from agentnexus_sdk.connector import Paths
from agentnexus_sdk.runtimes import (
    HermesAdapter,
    ModelStatus,
    OpenClawAdapter,
    RuntimeIntegrationError,
)
from agentnexus_sdk.signing import generate_key_pair

RUNNER_ID = "5d1c3a52-7b0e-4e0a-9a54-2f5c1ad7e226"
PROFILE = "agent2"
HERMES = "/synthetic/bin/hermes"
API_BASE = "https://api.agentnexus.test.invalid"
#: A model identifier of the Hermes 0.21.3 row below, once the label and the annotation are gone.
MODEL = "synthetic-vendor/declared-sentinel-7"
#: What Hermes 0.21.3 prints: an aligned row whose identifier carries its provider in brackets.
ROW = f"Model:   {MODEL} (synthetic-provider)"
#: The 120 characters the API accepts, and the 121 it does not.
LONGEST = ("abc-" * 30)[:120]
TOO_LONG = ("abc-" * 31)[:121]
#: The events the parent logs itself; the claim adds none (the closed list the records document).
PARENT_EVENTS = frozenset(
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
#: A whole match as the parent serves it: a join, two decisions with a move each, state reads.
MATCH = (
    '{"operation":"game_join"}\n'
    '{"diagnostic":"model_call_started","duration_ms":0}\n'
    '{"operation":"game_move","column":3}\n'
    '{"diagnostic":"model_call_returned","duration_ms":1}\n'
    '{"operation":"game_state"}\n'
    '{"diagnostic":"model_call_started","duration_ms":0}\n'
    '{"operation":"game_move","column":4}\n'
    '{"diagnostic":"model_call_returned","duration_ms":1}\n'
    '{"operation":"game_state"}\n'
    '{"finished":true}\n'
)


def wire(payload: dict[str, Any]) -> bytes:
    """Serialise a body as the client does: once, compact, without escaping Unicode."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def document(
    owned: arena_runner.StartIntent, *, status: str, claimed_by: str | None
) -> dict[str, object]:
    """Return the fixed intent shape the API sends today, for an intent in the given state."""
    return {
        "intent_id": owned.intent_id,
        "match_id": owned.match_id,
        "seat": owned.seat,
        "agent_id": owned.agent_id,
        "expires_at": owned.expires_at.isoformat(),
        "status": status,
        "claimed_by": claimed_by,
        "run_until": owned.run_until.isoformat(),
    }


class Hermes:
    """A stand-in for the `hermes` executable, and for `subprocess.run` calling it.

    It answers `profile show` with a few lines around one `Model:` row and keeps every call, so a
    test can count how often, and with which deadline, the runtime was asked.
    """

    def __init__(
        self,
        row: str | Exception | None = ROW,
        *,
        installed: bool = True,
        returncode: int = 0,
    ) -> None:
        """Script the answer: a row to print, `None` for no `Model:` row, or an error to raise."""
        self.row, self.installed, self.returncode = row, installed, returncode
        self.calls: list[dict[str, Any]] = []

    def which(self, name: str) -> str | None:
        """Find the executable, unless this runtime is not installed."""
        return HERMES if self.installed and name == "hermes" else None

    def run(self, argv: list[str], **keywords: Any) -> subprocess.CompletedProcess[str]:
        """Answer one command as `subprocess.run` would, and keep what a test needs of it.

        The environment is deliberately not kept: a failed assertion prints what it compared.
        """
        self.calls.append(
            {"argv": list(argv), "timeout": keywords.get("timeout"), "options": sorted(keywords)}
        )
        if isinstance(self.row, Exception):
            raise self.row
        lines = [f"Profile: {PROFILE}", "Path:    /synthetic/profiles/agent2"]
        lines += [] if self.row is None else [self.row]
        lines.append("Gateway: stopped")
        return subprocess.CompletedProcess(argv, self.returncode, "\n".join(lines) + "\n", "")


class Answering:
    """A stand-in runtime adapter whose `model_status` gives a scripted answer or error."""

    def __init__(self, answer: ModelStatus | Exception) -> None:
        """Script what `model_status` returns, or raises."""
        self.answer = answer

    def model_status(self) -> ModelStatus:
        """Return the scripted status, or raise the scripted error."""
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Scene:
    """One synthetic supervisor wired to stand-ins for the runtime, the API and the match process.

    `events` is the order in which the world was touched: `status` (the runtime was asked which
    model the profile has), `claim`, `popen` (the match process exists) and `thread`.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        hermes: Hermes | None = None,
        *,
        adapter: Callable[..., Any] | None = None,
        home: Path | None = None,
        signed: bool = False,
    ) -> None:
        """Wire the stand-ins in.

        `adapter` replaces the real Hermes adapter. `signed` sends the claim through a real client
        whose transport is a stand-in API, instead of replacing the runner's `_post`.
        """
        self.monkeypatch, self.tmp_path = monkeypatch, tmp_path
        self.hermes = hermes or Hermes()
        self.make = adapter or (
            lambda **keywords: HermesAdapter(which=self.hermes.which, **keywords)
        )
        self.events: list[str] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[httpx.Request] = []
        self.spawned: list[tuple[tuple[Any, ...], dict[str, Any], Any]] = []
        self.built: list[dict[str, Any]] = []
        self.asks = 0
        self.refusals = 0
        self.echo: str | None = None
        self.runner, self.owned = supervisor()
        self.known = {self.owned.intent_id: self.owned}
        self.queue: list[dict[str, object]] = [
            document(self.owned, status="queued", claimed_by=None)
        ]
        runner = self.runner
        del runner._report  # the real one: the match's status reports go through the stand-in too
        runner.journal = SimpleNamespace(runner_id=RUNNER_ID, reserve=lambda identifier: True)
        self.paths = Paths.for_profile(tmp_path / "install", PROFILE)
        runner.paths = self.paths
        runner.runtime = arena_runner.HermesRun(
            tmp_path / "source", tmp_path / "python", home or tmp_path / "home"
        )
        runner.active = runner.child = runner.worker = None
        if signed:
            runner.client = AgentNexusClient(
                agent_id=self.owned.agent_id,
                key_id=str(uuid.uuid4()),
                signer=generate_key_pair().signer,
                options=ClientOptions(base_url=API_BASE),
                transport=httpx.MockTransport(self.api),
            )
        else:
            runner._post = self.post

    # -- the stand-ins ---------------------------------------------------------------------------

    @contextlib.contextmanager
    def runtime(self) -> Iterator[None]:
        """Put the stand-in runtime where the runner asks about the model, for one call.

        Anything that asks again, whenever it does, is counted and answered here rather than
        reaching a real `hermes`.
        """
        with self.monkeypatch.context() as patch:
            patch.setattr(arena_runner, "HermesAdapter", self.build)
            patch.setattr(arena_runner.subprocess, "run", self.hermes.run)
            yield

    @contextlib.contextmanager
    def world(self) -> Iterator[None]:
        """Put the stand-ins where a launch reaches out, for the length of one call."""
        temporary = self.tmp_path / "temporary"
        temporary.mkdir(parents=True, exist_ok=True)
        with self.runtime(), self.monkeypatch.context() as patch:
            # Not while a match is served: a decision's timer is a `threading.Timer`, which builds
            # itself on the module's `Thread`.
            patch.setattr(arena_runner.subprocess, "Popen", self.spawn)
            patch.setattr(arena_runner.threading, "Thread", self.thread)
            patch.setattr(tempfile, "tempdir", str(temporary))
            yield

    def build(self, **keywords: Any) -> Any:
        """Stand in for `HermesAdapter`: build the adapter and count how often it is asked."""
        self.built.append(keywords)
        adapter = self.make(**keywords)
        answer = adapter.model_status

        def counted() -> Any:
            self.asks += 1
            self.events.append("status")
            return answer()

        adapter.model_status = counted
        return adapter

    def spawn(self, *args: Any, **keywords: Any) -> Any:
        """Stand in for `subprocess.Popen`: the match process is a pair of in-memory pipes."""
        self.events.append("popen")
        child = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO())
        self.spawned.append((args, keywords, child))
        return child

    def thread(self, **keywords: Any) -> Any:
        """Stand in for `threading.Thread`: the worker that serves the child is not started."""
        return SimpleNamespace(start=lambda: self.events.append("thread"))

    def answer(self, suffix: str, body: dict[str, Any]) -> dict[str, Any]:
        """Answer one signed operation of the start-intent API; a claim may be refused."""
        self.posts.append((suffix, body))
        if suffix == "/poll":
            return {"intents": list(self.queue)}
        if suffix.endswith("/claim"):
            self.events.append("claim")
            if self.refusals:
                self.refusals -= 1
                raise arena_runner.RunnerRefused("The API refused this Arena operation.")
            claimed = document(
                self.known[suffix.split("/")[1]], status="starting", claimed_by=RUNNER_ID
            )
            return claimed if self.echo is None else {**claimed, "declared_model": self.echo}
        return {}

    def post(self, suffix: str, payload: dict[str, Any]) -> Any:
        """Stand in for the signed `_post`, keeping the body exactly as it would be sent."""
        return self.answer(suffix, json.loads(wire(payload)))

    def api(self, request: httpx.Request) -> httpx.Response:
        """Stand in for the API behind a real client: keep the request, answer from `answer`."""
        self.requests.append(request)
        suffix = request.url.path.removeprefix(arena_runner.STARTS)
        return httpx.Response(200, json=self.answer(suffix, json.loads(request.content)))

    # -- driving the real runner -----------------------------------------------------------------

    def another(self) -> arena_runner.StartIntent:
        """Make one more start intent for this profile, as the API would offer it."""
        agent = self.owned.agent_id
        offered = arena_runner.StartIntent.parse(intent(agent), agent_id=agent)
        self.known[offered.intent_id] = offered
        return offered

    def launch(self, owned: arena_runner.StartIntent | None = None) -> None:
        """Run the real `_launch`: the claim, then the match process."""
        with self.world():
            self.runner._launch(owned or self.owned)

    def tick(self) -> None:
        """Run one real poll: the heartbeat, then the claim of the first queued intent."""
        with self.world():
            self.runner.tick()

    def declare(self, owned: arena_runner.StartIntent) -> str | None:
        """Ask the real resolver for one intent's declaration, without making any claim."""
        with self.runtime():
            return self.runner._declared_model(owned)

    def serve(self, script: str) -> list[dict[str, Any]]:
        """Serve a scripted match process through the real `_serve`; return what it forwarded."""
        forwarded: list[dict[str, Any]] = []

        def game(command: dict[str, Any], **keywords: object) -> dict[str, str]:
            forwarded.append(command)
            return {"status": "active"}

        self.monkeypatch.setattr(arena_runner.bridge, "_run_game_command", game)
        child = SimpleNamespace(stdout=io.StringIO(script), stdin=io.StringIO())
        with self.runtime():
            self.runner._serve(child, self.runner.active)
        return forwarded

    @property
    def claims(self) -> list[dict[str, Any]]:
        """The bodies sent to claim an intent, in order."""
        return [body for suffix, body in self.posts if suffix.endswith("/claim")]


def reports(row: str | Exception | None, **keywords: Any) -> Callable[[], dict[str, Any]]:
    """Script the stand-in `hermes`: what its `profile show` prints, or how it fails."""
    return lambda: {"hermes": Hermes(row, **keywords)}


def answers(status: ModelStatus | Exception) -> Callable[[], dict[str, Any]]:
    """Script the runtime adapter itself, for answers the real adapter cannot give."""
    return lambda: {"adapter": lambda **keywords: Answering(status)}


#: Every way the runtime can fail to name a model the claim may carry. Each leaves the field out.
UNUSABLE: dict[str, Callable[[], dict[str, Any]]] = {
    "no-model-row": reports(None),
    "hyphen": reports("Model: -"),
    "em-dash": reports("Model: —"),
    "the-word-none": reports("Model: none"),
    "annotation-only": reports("Model: (openrouter)"),
    "not-installed": reports(ROW, installed=False),
    "nonzero-exit": reports(ROW, returncode=1),
    "url": reports("Model: https://gateway.synthetic.invalid/v1"),
    "credential-like": reports("Model: sk-abcdefghijabcdefghij"),
    "121-characters": reports(f"Model: {TOO_LONG}"),
    "invalid-characters": reports("Model: bad model!"),
    "leading-dot": reports("Model: .hidden/model"),
    "opaque-run": reports("Model: " + "0123456789abcdef" * 2),
    "runner-times-out": reports(subprocess.TimeoutExpired([HERMES], 5.0)),
    "runner-raises-os-error": reports(OSError("synthetic-private")),
    "runner-raises-anything": reports(RuntimeError("synthetic-private")),
    "openclaw-is-always-unknown": lambda: {
        "adapter": lambda **keywords: OpenClawAdapter(which=lambda name: None, **keywords)
    },
    "not-configured-but-a-value": answers(
        ModelStatus(configured=False, known=True, detail="Model: -", value="vendor/never-sent")
    ),
    "configured-but-no-value": answers(ModelStatus(configured=True, known=True, detail=ROW)),
    "integration-error": answers(
        RuntimeIntegrationError("synthetic-private", recovery="synthetic-private")
    ),
    "arbitrary-error": answers(KeyError("synthetic-private")),
}


@pytest.mark.parametrize(
    ("row", "declared"),
    [
        pytest.param(ROW, MODEL, id="hermes-0.21.3-annotated"),
        pytest.param("Model: minimax/minimax-m3:free", "minimax/minimax-m3:free", id="0.20.6-bare"),
        pytest.param(
            "Model:   thinkingmachines/inkling:free (openrouter)",
            "thinkingmachines/inkling:free",
            id="identifier-with-a-colon",
        ),
        pytest.param(f"Model: {LONGEST}", LONGEST, id="120-characters"),
    ],
)
def test_the_claim_carries_the_model_the_runtime_declares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, row: str, declared: str
) -> None:
    """A configured model goes into the claim as its identifier, without label or annotation."""
    scene = Scene(monkeypatch, tmp_path, Hermes(row))
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": declared}]
    assert list(scene.claims[0]) == ["runner_id", "declared_model"]
    sent = json.dumps(scene.claims[0])
    assert "(" not in sent and "Model" not in sent and "provider" not in sent
    assert bridge.is_declared_model_valid(declared)


def test_the_declaration_is_sent_trimmed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The value that was checked is the value that is sent: no whitespace around it."""
    padded = ModelStatus(configured=True, known=True, detail=ROW, value="  vendor/padded-model \t")
    scene = Scene(monkeypatch, tmp_path, adapter=lambda **keywords: Answering(padded))
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": "vendor/padded-model"}]


@pytest.mark.parametrize("case", UNUSABLE)
def test_the_field_is_left_out_when_the_runtime_names_no_usable_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """No model, no runtime, an unusable value or any failure: the claim goes without the field."""
    scene = Scene(monkeypatch, tmp_path, **UNUSABLE[case]())
    scene.launch()
    assert scene.asks == 1, "the runtime was not asked"
    assert scene.claims == [{"runner_id": RUNNER_ID}]
    assert "declared_model" not in scene.claims[0] and "null" not in json.dumps(scene.claims[0])
    assert scene.events == ["status", "claim", "popen", "thread"], "the match did not go on"
    out, err = capsys.readouterr()
    assert err == "" and "synthetic-private" not in out
    assert scene.runner.active is not None and scene.runner.active.claimed_by == RUNNER_ID


def test_the_runtime_is_asked_about_this_isolated_profile_and_only_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The question goes to the profile's own Hermes context, through the existing adapter."""
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    assert len(scene.built) == 1, "the runtime adapter was not built exactly once"
    built = scene.built[0]
    assert built["context"] == scene.paths.runtime_context()
    assert built["context"].isolated and built["context"].hermes_profile == PROFILE
    assert [call["argv"] for call in scene.hermes.calls] == [
        [HERMES, "-p", PROFILE, "profile", "show", PROFILE]
    ]


def test_the_runtime_is_given_at_most_five_seconds_to_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`model_status` allows itself 60 seconds; inside the poll loop that outlasts the heartbeat."""
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    assert scene.hermes.calls, "the runtime was never asked"
    assert all(0 < call["timeout"] <= 5.0 for call in scene.hermes.calls)


def test_the_bounded_runner_holds_every_call_to_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A longer deadline is cut to five seconds, a shorter one is kept, and none is bounded too."""
    seen: list[float] = []

    def run(command: list[str], **keywords: Any) -> subprocess.CompletedProcess[str]:
        seen.append(keywords["timeout"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(arena_runner.subprocess, "run", run)
    for asked in (60.0, 5.0, 2.0, None):
        arena_runner.bounded_runner([HERMES], **({} if asked is None else {"timeout": asked}))
    assert seen == [5.0, 5.0, 2.0, 5.0]


def test_a_claim_retried_in_a_later_tick_sends_the_same_declaration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runtime is asked once for an intent; a retry sends the value it was frozen with."""
    hermes = Hermes("Model: first/model-a")
    scene = Scene(monkeypatch, tmp_path, hermes)
    scene.refusals = 2
    for _ in range(2):
        with pytest.raises(arena_runner.RunnerRefused):
            scene.tick()
    hermes.row = "Model: second/model-b"  # the profile is reconfigured between two ticks
    scene.tick()
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": "first/model-a"}] * 3
    assert scene.asks == 1 and len(hermes.calls) == 1
    assert scene.events.count("status") == 1
    polls = [body for suffix, body in scene.posts if suffix == "/poll"]
    assert polls == [{"runner_id": RUNNER_ID, "availability": "online"}] * 3


def test_an_absent_declaration_is_frozen_for_the_intent_as_well(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A retry never turns a claim without a declaration into one with it."""
    hermes = Hermes(None)
    scene = Scene(monkeypatch, tmp_path, hermes)
    scene.refusals = 1
    with pytest.raises(arena_runner.RunnerRefused):
        scene.tick()
    hermes.row = ROW  # the profile gets a model after the first attempt
    scene.tick()
    assert scene.claims == [{"runner_id": RUNNER_ID}] * 2
    assert scene.asks == 1


def test_another_intent_asks_the_runtime_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A later match is a later declaration: the profile's model may have changed since."""
    hermes = Hermes("Model: first/model-a")
    scene = Scene(monkeypatch, tmp_path, hermes)
    scene.launch()
    hermes.row = "Model: second/model-b"
    scene.launch(scene.another())
    assert scene.claims == [
        {"runner_id": RUNNER_ID, "declared_model": "first/model-a"},
        {"runner_id": RUNNER_ID, "declared_model": "second/model-b"},
    ]
    assert scene.asks == 2 and len(hermes.calls) == 2


def test_the_runtime_is_not_asked_again_while_the_match_is_played(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No turn, move or state read re-queries the runtime, and none sends the declaration again."""
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    sent = len(scene.posts)
    forwarded = scene.serve(MATCH)
    assert [command["operation"] for command in forwarded] == [
        "game_join",
        "game_move",
        "game_state",
        "game_move",
        "game_state",
    ]
    assert scene.asks == 1 and len(scene.hermes.calls) == 1
    reported = scene.posts[sent:]
    assert [suffix.rsplit("/", 1)[1] for suffix, _ in reported] == ["status"]
    assert all("declared_model" not in body for _, body in reported)


def test_the_claim_is_made_before_the_child_exists_and_the_child_is_told_nothing_of_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The match process, and the model behind it, come after the declaration is settled."""
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    assert scene.events == ["status", "claim", "popen", "thread"]
    ((arguments, keywords, child),) = scene.spawned
    start = json.loads(child.stdin.getvalue())
    assert set(start) == {"match_id", "seat", "seconds"}
    reachable = {"arguments": str(arguments), "keywords": str(keywords), "start": str(start)}
    leaked = [name for name, text in reachable.items() if MODEL in text]
    assert leaked == [], "the child was told the declaration"
    named = [key for key in keywords["env"] if "MODEL" in key.upper()]
    assert named == [], "the child's environment names a model"
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": MODEL}]


@pytest.mark.parametrize(
    "request_line",
    [
        '{"operation":"game_join","declared_model":"evil/model-name"}',
        '{"operation":"game_move","column":3,"declared_model":"evil/model-name"}',
        '{"operation":"game_state","declared_model":"evil/model-name"}',
        '{"operation":"declared_model","value":"evil/model-name"}',
        '{"declared_model":"evil/model-name"}',
    ],
)
def test_nothing_the_model_asks_for_reaches_the_declaration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    request_line: str,
) -> None:
    """A tool request that names a model is refused before it is served, and sends no claim."""
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    capsys.readouterr()
    sent = len(scene.posts)
    forwarded = scene.serve(request_line + '\n{"operation":"game_join"}\n')
    out, _ = capsys.readouterr()
    assert forwarded == [], "the request was served"
    assert [json.loads(line)["event"] for line in out.splitlines()] == ["protocol_refused"]
    assert scene.posts[sent:] == [], "something was sent after the model's request"
    assert len(scene.claims) == 1 and "evil" not in out


def test_nothing_but_the_runtime_status_can_supply_the_declaration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No parameter, environment variable or attribute is a way to name the model of a claim."""
    for name in ("_launch", "_declared_model"):
        parameters = inspect.signature(getattr(arena_runner.ArenaRunner, name)).parameters
        assert list(parameters) == ["self", "intent"], name
    for variable in ("AGENTNEXUS_DECLARED_MODEL", "AGENTNEXUS_MODEL", "HERMES_MODEL", "MODEL"):
        monkeypatch.setenv(variable, "evil/environment-model")
    scene = Scene(monkeypatch, tmp_path)
    scene.runner.declared_model = "evil/attribute-model"
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": MODEL}]
    assert "evil" not in json.dumps(scene.posts)


def watched(monkeypatch: pytest.MonkeyPatch, *roots: Path) -> list[str]:
    """Record every file under the roots that anything tries to open, and still open it."""
    opened: list[str] = []

    def spy(real: Callable[..., Any]) -> Callable[..., Any]:
        def open_(file: Any, *args: Any, **keywords: Any) -> Any:
            if isinstance(file, str | bytes | os.PathLike):
                name = os.fsdecode(file)
                if any(name.startswith(str(root)) for root in roots):
                    opened.append(name)
            return real(file, *args, **keywords)

        return open_

    monkeypatch.setattr(builtins, "open", spy(builtins.open))
    monkeypatch.setattr(io, "open", spy(io.open))
    monkeypatch.setattr(os, "open", spy(os.open))
    return opened


def test_no_file_is_opened_to_find_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The profile's configuration, credentials and keys are the runtime's; the claim reads none."""
    home = tmp_path / "hermes-home"
    scene = Scene(monkeypatch, tmp_path, home=home)
    root = scene.paths.root
    for directory, names in (
        (root, ("config.yaml", ".env", "state.json")),
        (home, ("config.yaml", ".env")),
    ):
        directory.mkdir(parents=True)
        for name in names:
            (directory / name).write_text("model: only-in-a-file/never-read-1\n", encoding="utf-8")
    (root / "keys").mkdir()
    (root / "keys" / "agent.pem").write_text("synthetic-never-read\n", encoding="utf-8")
    opened = watched(monkeypatch, root, home)
    scene.launch()
    assert opened == []
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": MODEL}]
    assert "only-in-a-file" not in json.dumps(scene.posts)


def test_the_declaration_reaches_no_log_and_adds_no_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No record carries the model, its provider or its row; the event list is unchanged."""
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    scene.serve(MATCH)
    out, err = capsys.readouterr()
    for forbidden in (MODEL, "synthetic-provider", "Model:", "declared", "model_status"):
        assert forbidden not in out + err, forbidden
    assert err == ""
    assert json.loads(out.splitlines()[0])["event"] == "run_started"
    assert arena_runner.DIAGNOSTICS - hermes_arena.DIAGNOSTICS == PARENT_EVENTS
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": MODEL}]


def test_the_memory_of_resolved_intents_stays_small_and_is_never_shared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A service that lives for months cannot grow with its matches, and runners keep their own."""
    first, second = Scene(monkeypatch, tmp_path / "a"), Scene(monkeypatch, tmp_path / "b")
    assert arena_runner.ArenaRunner.declared_models is None
    offered = [first.another() for _ in range(40)]
    for item in offered:
        assert first.declare(item) == MODEL
    memory = first.runner.declared_models
    assert memory is not None and len(memory) <= 16
    assert first.asks == 40
    assert first.declare(offered[-1]) == MODEL and first.asks == 40, "the latest was forgotten"
    second.declare(second.owned)
    assert second.runner.declared_models is not memory
    assert arena_runner.ArenaRunner.declared_models is None


def test_a_claim_answer_in_todays_shape_parses_and_one_naming_a_model_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The API does not echo the declaration; the parser stays strict until it is made to."""
    scene = Scene(monkeypatch, tmp_path)
    today = document(scene.owned, status="starting", claimed_by=RUNNER_ID)
    parsed = arena_runner.StartIntent.parse(today, agent_id=scene.owned.agent_id)
    assert parsed.claimed_by == RUNNER_ID
    assert "declared_model" not in arena_runner.StartIntent.__dataclass_fields__
    for item in (today, document(scene.owned, status="queued", claimed_by=None)):
        with pytest.raises(arena_runner.RunnerRefused):
            arena_runner.StartIntent.parse(
                {**item, "declared_model": MODEL}, agent_id=scene.owned.agent_id
            )
    scene.echo = MODEL  # an API that answered the claim with the declaration included
    with pytest.raises(arena_runner.RunnerRefused):
        scene.launch()
    assert "popen" not in scene.events, "a match process was started for an unparsable claim"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(ROW, {"runner_id": RUNNER_ID, "declared_model": MODEL}, id="with-a-model"),
        pytest.param("Model: -", {"runner_id": RUNNER_ID}, id="without-one"),
    ],
)
def test_the_signed_body_on_the_wire_is_the_claim_and_nothing_more(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, row: str, expected: dict[str, str]
) -> None:
    """Through a real client: one compact JSON body, the field present or absent and never null."""
    scene = Scene(monkeypatch, tmp_path, Hermes(row), signed=True)
    try:
        scene.launch()
    finally:
        scene.runner.client.close()
    claims = [r for r in scene.requests if r.url.path.endswith("/claim")]
    assert len(claims) == 1
    assert claims[0].method == "POST"
    assert claims[0].url.path == f"{arena_runner.STARTS}/{scene.owned.intent_id}/claim"
    assert claims[0].content == wire(expected)
    assert b"null" not in claims[0].content
