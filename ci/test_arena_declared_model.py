"""#226 (D-174) on the #228 stack: the Arena claim carries the declared model once, or nothing.

An Arena seat may carry the same optional, opaque `declared_model` a forum contribution carries
(RMD-1). The runtime driver reports it, the runner holds it from the proof that precedes any claim,
and the claim forwards it once per start intent: frozen for the intent's retries, left out whenever
it is unknown or unusable, checked again at the claim because a driver's answer is untrusted, and
never written to a log. It is a self-declaration, never a detection, a verification or a decision:
no capability, trust or routing depends on it, and the runtime-neutral supervisor imports no runtime
to find it.

Everything here runs in this process against stand-ins. There is no real runtime, no network, no
model call, and no profile file is read.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest
from arena_boundary import imported_text
from arena_fakes import intent, supervisor

from agentnexus_sdk import (
    arena_driver,
    arena_driver_hermes,
    arena_driver_openclaw,
    arena_match,
    arena_runner,
    bridge,
    runtimes,
)
from agentnexus_sdk.client import AgentNexusClient, ClientOptions
from agentnexus_sdk.connector import Paths
from agentnexus_sdk.signing import generate_key_pair

RUNNER_ID = "5d1c3a52-7b0e-4e0a-9a54-2f5c1ad7e226"
PROFILE = "agent2"
HERMES = "/synthetic/bin/hermes"
API_BASE = "https://api.agentnexus.test.invalid"
#: A model identifier of the shape a runtime reports; the sentinel the log tests look for.
MODEL = "synthetic-vendor/declared-sentinel-7"
#: The 120 characters the API accepts, and the 121 it does not.
LONGEST = ("abc-" * 30)[:120]
TOO_LONG = ("abc-" * 31)[:121]
SOURCE = Path(arena_match.__file__).parent
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
#: Texts the declaration must never carry, however a driver or a bug came by them.
UNUSABLE_TEXT = {
    "url": "https://gateway.synthetic.invalid/v1",
    "credential-like": "sk-abcdefghijabcdefghij",
    "121-characters": TOO_LONG,
    "invalid-characters": "bad model!",
    "leading-dot": ".hidden/model",
    "opaque-run": "0123456789abcdef" * 2,
    "empty": "",
    "blank": "   ",
}
#: Answers that are not text at all.
NOT_TEXT = {"integer": 42, "list": ["vendor/model"], "bytes": b"vendor/model", "mapping": {}}


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


class Driver:
    """A runtime driver whose declared text a test scripts, and whose every question it counts."""

    name = "scripted"
    display_name = "Scripted"
    capabilities = arena_driver.CONTRACT

    def __init__(self, answer: object, events: list[str]) -> None:
        """Answer with `answer`, or raise it when it is an exception."""
        self.answer, self.events, self.asked = answer, events, 0
        self.generation_value = "g1"

    def inspect(self, paths: Any) -> SimpleNamespace:
        """Return a handle."""
        del paths
        return SimpleNamespace()

    def preflight(self, handle: Any) -> frozenset[str]:
        """Pass: the contract's three tools."""
        del handle
        return arena_match.TOOLS

    def generation(self, handle: Any) -> str:
        """Return the opaque token the test set."""
        del handle
        return self.generation_value

    def declared_model(self, handle: Any) -> Any:
        """Count the question, and answer it as scripted."""
        del handle
        self.asked += 1
        self.events.append("driver")
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def launch(self, handle: Any, scratch: Path) -> arena_driver.Launch:
        """Return a command that is never run: the match process is a stand-in."""
        del handle, scratch
        return arena_driver.Launch(command=[sys.executable, "-c", "pass"], environment={})


class Scene:
    """One synthetic supervisor wired to a scripted driver and stand-ins for the API and the child.

    `events` is the order in which the world was touched: `driver` (the runtime was asked), `claim`,
    `popen` (the match process exists) and `thread`.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        answer: object = MODEL,
        *,
        signed: bool = False,
    ) -> None:
        """Build a runner proven with the driver, as the command line proves it before running."""
        self.monkeypatch, self.tmp_path = monkeypatch, tmp_path
        self.events: list[str] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[httpx.Request] = []
        self.spawned: list[tuple[tuple[Any, ...], dict[str, Any], Any]] = []
        self.refusals = 0
        self.echo: str | None = None
        self.driver = Driver(answer, self.events)
        self.runner, self.owned = supervisor()
        self.known = {self.owned.intent_id: self.owned}
        self.queue: list[dict[str, object]] = [
            document(self.owned, status="queued", claimed_by=None)
        ]
        runner = self.runner
        del runner._report  # the real one: the match's status reports go through the stand-in too
        runner.journal = SimpleNamespace(runner_id=RUNNER_ID, reserve=lambda identifier: True)
        runner.paths = SimpleNamespace(root=tmp_path / "profile")
        runner.driver, runner.handle = self.driver, self.driver.inspect(None)
        runner.active = runner.child = runner.worker = None
        runner.begin_proof()
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
    def world(self) -> Iterator[None]:
        """Put the stand-ins where a launch reaches out, for the length of one call."""
        temporary = self.tmp_path / "temporary"
        temporary.mkdir(parents=True, exist_ok=True)
        with self.monkeypatch.context() as patch:
            # Not `subprocess.Popen`: a launch starts its child through the tree seam (#223), which
            # needs a real process to put in a job object or a session of its own. The seam is
            # replaced as a whole, so the stand-in child never needs to be killable.
            patch.setattr(arena_runner.arena_match, "start_in_tree", self.spawn)
            patch.setattr(arena_runner.threading, "Thread", self.thread)
            patch.setattr(tempfile, "tempdir", str(temporary))
            yield

    def spawn(self, *args: Any, **keywords: Any) -> Any:
        """Stand in for `arena_match.start_in_tree`: the match process is in-memory pipes."""
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
        """Stand in for the signed `_post`, keeping the body exactly as it is when it is sent."""
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

    def serve(self, script: str) -> list[dict[str, Any]]:
        """Serve a scripted match process through the real `_serve`; return what it forwarded."""
        forwarded: list[dict[str, Any]] = []

        def game(command: dict[str, Any], **keywords: object) -> dict[str, str]:
            forwarded.append(command)
            return {"status": "active"}

        self.monkeypatch.setattr(arena_runner.bridge, "_run_game_command", game)
        child = SimpleNamespace(stdout=io.StringIO(script), stdin=io.StringIO())
        self.runner._serve(child, self.runner.active)
        return forwarded

    @property
    def claims(self) -> list[dict[str, Any]]:
        """The bodies sent to claim an intent, in order."""
        return [body for suffix, body in self.posts if suffix.endswith("/claim")]


# ---------------------------------------------------------------------------------------------
# What the claim carries
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [MODEL, "minimax/minimax-m3:free", LONGEST])
def test_the_claim_carries_the_text_the_driver_declares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str
) -> None:
    """The text a driver reports goes into the claim as it is: the driver layer shapes it."""
    scene = Scene(monkeypatch, tmp_path, text)
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": text}]
    assert list(scene.claims[0]) == ["runner_id", "declared_model"]
    assert bridge.is_declared_model_valid(text)


@pytest.mark.parametrize("case", sorted(UNUSABLE_TEXT))
def test_the_field_is_left_out_when_the_driver_names_an_unusable_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """An unusable text from the driver: the claim goes without the key, never null or empty."""
    scene = Scene(monkeypatch, tmp_path, UNUSABLE_TEXT[case])
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID}]
    assert "declared_model" not in scene.claims[0] and "null" not in json.dumps(scene.claims[0])
    assert scene.events == ["driver", "claim", "popen", "thread"], "the match did not go on"


@pytest.mark.parametrize("case", sorted(UNUSABLE_TEXT))
def test_the_claim_checks_the_text_again_whatever_the_runner_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """A driver's answer is untrusted: what the runner holds is validated again at the claim."""
    scene = Scene(monkeypatch, tmp_path, None)
    scene.runner.declared_model = UNUSABLE_TEXT[case]
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID}]
    assert scene.events == ["driver", "claim", "popen", "thread"], "the match did not go on"


@pytest.mark.parametrize("case", sorted(NOT_TEXT))
def test_the_claim_never_sends_what_is_not_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    """A driver that answers with anything but text is not heard, whoever holds the answer."""
    scene = Scene(monkeypatch, tmp_path, NOT_TEXT[case])
    assert scene.runner.declared_model is None
    scene.runner.declared_model = NOT_TEXT[case]  # as if it had got past the proof
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID}]
    assert "popen" in scene.events and "thread" in scene.events


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("synthetic-private"),
        OSError("synthetic-private"),
        KeyError("synthetic-private"),
    ],
)
def test_a_driver_that_raises_costs_the_field_and_not_the_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """Optional metadata must never cost a seat its claim, nor leave a trace of the failure."""
    scene = Scene(monkeypatch, tmp_path, error)
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID}]
    assert scene.events == ["driver", "claim", "popen", "thread"]
    out, err = capsys.readouterr()
    assert "synthetic-private" not in out + err and err == ""


# ---------------------------------------------------------------------------------------------
# Once per intent, and never asked of the runtime again
# ---------------------------------------------------------------------------------------------


def test_a_claim_retried_in_a_later_tick_sends_the_same_declaration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What the first attempt sent is what every retry sends, whatever changed meanwhile."""
    scene = Scene(monkeypatch, tmp_path, "first/model-a")
    scene.refusals = 2
    for _ in range(2):
        with pytest.raises(arena_runner.RunnerRefused):
            scene.tick()
    scene.runner.declared_model = "second/model-b"  # the proof moved on between two ticks
    scene.driver.answer = "third/model-c"
    scene.tick()
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": "first/model-a"}] * 3
    assert scene.driver.asked == 1
    polls = [body for suffix, body in scene.posts if suffix == "/poll"]
    assert polls == [{"runner_id": RUNNER_ID, "availability": "online"}] * 3


def test_an_absent_declaration_is_frozen_for_the_intent_as_well(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A retry never turns a claim without a declaration into one with it."""
    scene = Scene(monkeypatch, tmp_path, None)
    scene.refusals = 1
    with pytest.raises(arena_runner.RunnerRefused):
        scene.tick()
    scene.runner.declared_model = MODEL  # the runtime got a model after the first attempt
    scene.tick()
    assert scene.claims == [{"runner_id": RUNNER_ID}] * 2


def test_another_intent_takes_the_declaration_the_runner_holds_then(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A later match is a later declaration: the profile's model may have changed since."""
    scene = Scene(monkeypatch, tmp_path, "first/model-a")
    scene.launch()
    scene.runner.declared_model = "second/model-b"
    scene.launch(scene.another())
    assert scene.claims == [
        {"runner_id": RUNNER_ID, "declared_model": "first/model-a"},
        {"runner_id": RUNNER_ID, "declared_model": "second/model-b"},
    ]


def test_the_driver_is_not_asked_in_launch_per_tick_or_per_move(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one question belongs to the proof; no claim, tick, turn or move asks it again."""
    scene = Scene(monkeypatch, tmp_path)
    assert scene.driver.asked == 1, "the proof asks once"
    scene.tick()  # the claim and the launch of the first intent
    assert scene.driver.asked == 1
    sent = len(scene.posts)
    forwarded = scene.serve(MATCH)
    assert [command["operation"] for command in forwarded] == [
        "game_join",
        "game_move",
        "game_state",
        "game_move",
        "game_state",
    ]
    scene.runner.maintain()  # a match is on: it only watches
    scene.runner.active = None
    scene.runner.maintain()  # idle, and nothing changed: the proof stands
    offered = scene.another()
    scene.queue = [document(offered, status="queued", claimed_by=None)]
    scene.tick()  # the next intent, claimed in a later tick
    assert scene.driver.asked == 1
    reported = scene.posts[sent:]
    assert [suffix.rsplit("/", 1)[1] for suffix, _ in reported[:1]] == ["status"]
    assert all("declared_model" not in body for _, body in reported[:1])
    assert scene.claims[-1] == {"runner_id": RUNNER_ID, "declared_model": MODEL}


def test_a_new_proof_is_the_only_thing_that_asks_the_driver_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A changed generation is proven again, and that proof is where the answer is renewed."""
    scene = Scene(monkeypatch, tmp_path, "first/model-a")
    scene.driver.answer, scene.driver.generation_value = "second/model-b", "g2"
    scene.tick()
    assert scene.driver.asked == 2
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": "second/model-b"}]


def test_a_generation_change_during_a_match_does_not_touch_the_intents_declaration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The match keeps what its claim declared; only the next idle preflight renews the answer."""
    scene = Scene(monkeypatch, tmp_path, "first/model-a")
    scene.launch()
    owned = scene.owned
    scene.driver.answer, scene.driver.generation_value = "second/model-b", "g2"
    scene.runner.maintain()  # a match is on: the change is only noted, and nothing is asked
    assert scene.runner.pending is True
    assert scene.driver.asked == 1
    assert scene.runner.declared_models == {owned.intent_id: "first/model-a"}
    scene.runner.active = None  # the match ended
    scene.runner.maintain()  # the next idle preflight proves the new generation
    assert scene.driver.asked == 2
    assert scene.runner.declared_models == {owned.intent_id: "first/model-a"}
    assert scene.runner._declaration_for(scene.another()) == "second/model-b"


def test_the_memory_of_resolved_intents_stays_small_and_is_never_shared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A service that lives for months cannot grow with its matches, and runners keep their own."""
    first, second = Scene(monkeypatch, tmp_path / "a"), Scene(monkeypatch, tmp_path / "b")
    assert arena_runner.ArenaRunner.declared_models is None
    assert arena_runner.DECLARATIONS_KEPT == 8
    offered = [first.owned] + [first.another() for _ in range(8)]
    for item in offered:
        first.launch(item)
    memory = first.runner.declared_models
    assert memory is not None and len(memory) == 8
    assert offered[0].intent_id not in memory, "the 9th intent did not evict the 1st"
    assert all(item.intent_id in memory for item in offered[1:])
    first.runner.declared_model = "later/model-z"
    first.launch(offered[-1])  # still remembered: frozen
    first.launch(offered[0])  # evicted: it is a new sight
    assert first.claims[-2] == {"runner_id": RUNNER_ID, "declared_model": MODEL}
    assert first.claims[-1] == {"runner_id": RUNNER_ID, "declared_model": "later/model-z"}
    second.launch()
    assert second.runner.declared_models is not memory
    assert arena_runner.ArenaRunner.declared_models is None


# ---------------------------------------------------------------------------------------------
# Before the child, and nothing the child says
# ---------------------------------------------------------------------------------------------


def test_the_claim_is_made_before_the_child_exists_and_the_child_is_told_nothing_of_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The match process, and the model behind it, come after the declaration is settled."""
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    assert scene.events == ["driver", "claim", "popen", "thread"]
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


def test_nothing_but_the_driver_through_the_proof_can_supply_the_declaration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No parameter, environment variable or other path names the model of a claim."""
    parameters = inspect.signature(arena_runner.ArenaRunner._launch).parameters
    assert list(parameters) == ["self", "intent"]
    for variable in ("AGENTNEXUS_DECLARED_MODEL", "AGENTNEXUS_MODEL", "HERMES_MODEL", "MODEL"):
        monkeypatch.setenv(variable, "evil/environment-model")
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": MODEL}]
    assert "evil" not in json.dumps(scene.posts)


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
    ("answer", "expected"),
    [
        pytest.param(MODEL, {"runner_id": RUNNER_ID, "declared_model": MODEL}, id="with-a-model"),
        pytest.param(None, {"runner_id": RUNNER_ID}, id="without-one"),
    ],
)
def test_the_signed_body_on_the_wire_is_the_claim_and_nothing_more(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: str | None, expected: dict[str, str]
) -> None:
    """Through a real client: one compact JSON body, the field present or absent and never null."""
    scene = Scene(monkeypatch, tmp_path, answer, signed=True)
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


# ---------------------------------------------------------------------------------------------
# Never logged
# ---------------------------------------------------------------------------------------------


def test_the_declaration_reaches_no_log_and_adds_no_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No record carries the model or a runtime's output; the event list is unchanged."""
    scene = Scene(monkeypatch, tmp_path)
    scene.launch()
    scene.serve(MATCH)
    out, err = capsys.readouterr()
    for forbidden in (MODEL, "vendor", "declared", "synthetic"):
        assert forbidden not in out + err, forbidden
    assert err == ""
    assert json.loads(out.splitlines()[0])["event"] == "run_started"
    assert arena_runner.DIAGNOSTICS - arena_match.DIAGNOSTICS == PARENT_EVENTS
    assert scene.claims == [{"runner_id": RUNNER_ID, "declared_model": MODEL}]


# ---------------------------------------------------------------------------------------------
# The runtime-neutral core asks no runtime
# ---------------------------------------------------------------------------------------------


def test_the_supervisor_imports_no_runtime_driver_or_server_by_name() -> None:
    """The text arrives from the driver stack; the core never builds an adapter to find it."""
    text = (SOURCE / "arena_runner.py").read_text(encoding="utf-8")
    imported = imported_text(text)
    forbidden = {
        "agentnexus_sdk.runtimes",
        "agentnexus_sdk.mcp_server",
        "agentnexus_sdk.hermes_arena",
        "agentnexus_sdk.openclaw_arena",
        "agentnexus_sdk.arena_driver_hermes",
        "agentnexus_sdk.arena_driver_openclaw",
    }
    assert not imported & forbidden, sorted(imported & forbidden)
    for name in ("HermesAdapter", "OpenClawAdapter", "bounded_runner", "MODEL_QUERY_TIMEOUT"):
        assert name not in text, name


# ---------------------------------------------------------------------------------------------
# Where a runtime is actually asked: at most five seconds
# ---------------------------------------------------------------------------------------------


class Hermes:
    """A stand-in for the `hermes` executable, and for `subprocess.run` calling it."""

    def __init__(self) -> None:
        """Keep every call, so a test can see with which deadline the runtime was asked."""
        self.calls: list[dict[str, Any]] = []

    def which(self, name: str) -> str | None:
        """Find the executable."""
        return HERMES if name == "hermes" else None

    def run(self, argv: list[str], **keywords: Any) -> subprocess.CompletedProcess[str]:
        """Answer `profile show` with one aligned `Model:` row, as Hermes 0.21.3 prints it."""
        self.calls.append({"argv": list(argv), "timeout": keywords.get("timeout")})
        lines = [
            f"Profile: {PROFILE}",
            f"Model:   {MODEL} (synthetic-provider)",
            "Gateway: stopped",
        ]
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")


def test_the_hermes_driver_asks_its_runtime_for_at_most_five_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`model_status` allows itself 60 seconds; inside the poll loop that outlasts the heartbeat."""
    hermes = Hermes()
    monkeypatch.setattr(arena_driver_hermes.subprocess, "run", hermes.run)
    monkeypatch.setattr(
        arena_driver_hermes,
        "HermesAdapter",
        # The stand-in answers for a driver that supplies no runner of its own.
        lambda **keywords: runtimes.HermesAdapter(
            which=hermes.which, **{"runner": hermes.run, **keywords}
        ),
    )
    context = Paths.for_profile(tmp_path / "install", PROFILE).runtime_context()
    handle = arena_driver_hermes.HermesRun(tmp_path, tmp_path, tmp_path, context)
    assert arena_driver_hermes.driver().declared_model(handle) == MODEL
    assert [call["argv"] for call in hermes.calls] == [
        [HERMES, "-p", PROFILE, "profile", "show", PROFILE]
    ]
    assert all(0 < call["timeout"] <= 5.0 for call in hermes.calls)


def test_the_hermes_bounded_runner_holds_every_call_to_five_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A longer deadline is cut to five seconds, a shorter one is kept, and none is bounded too."""
    seen: list[float] = []

    def run(command: list[str], **keywords: Any) -> subprocess.CompletedProcess[str]:
        seen.append(keywords["timeout"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(arena_driver_hermes.subprocess, "run", run)
    for asked in (60.0, 5.0, 3.0, None):
        arena_driver_hermes.bounded_runner(
            [HERMES], **({} if asked is None else {"timeout": asked})
        )
    assert seen == [5.0, 5.0, 3.0, 5.0]


def test_the_openclaw_driver_asks_its_runtime_for_at_most_five_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The model question is not a step of the preflight: it is held to five seconds, not 180."""
    asked: list[tuple[list[str], int]] = []
    driver = arena_driver_openclaw.OpenClawArenaDriver()

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        asked.append((args[3], args[4]))
        return subprocess.CompletedProcess(args[3], 0, stdout="vendor/model-1\n", stderr="")

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(arena_driver_openclaw.openclaw_arena, "make_world", lambda work: None)
    handle = arena_driver_openclaw.OpenClawRun(
        ("openclaw",), "2026.9.9", tmp_path / "c.json", tmp_path / "s", tmp_path
    )
    assert driver.declared_model(handle) == "vendor/model-1"
    assert asked == [(["models", "status", "--plain"], asked[0][1])]
    assert 0 < asked[0][1] <= 5
    assert arena_driver_openclaw.STEP_SECONDS == 180, "the preflight's timing is not touched"
