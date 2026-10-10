"""#228: the runtime in effect is proven before a claim; a match stays on what it began with.

The owner may change what a runtime plays with - its model, its sign-in - while the Arena service
is idle or while a match runs. The service knows nothing about models or providers, so it asks the
driver for an opaque generation and treats it as a name for a proof:

* a generation that is the one already proven is reused and nothing is run again;
* a changed generation is inspected and preflighted first, and only then may a seat be claimed;
* a refused generation claims nothing, is not retried by itself and never becomes active;
* a match that runs is pinned: a change meanwhile is only recorded, and takes effect when the match
  has been cleaned up;
* a driver that offers no generation is asked again before each claim, and a refusal holds for that
  one intent.

A scripted driver stands for the runtime. No process, no model, no credential and no network.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from arena_fakes import expect_guard, intent, load_mutant

from agentnexus_sdk import arena_driver, arena_match, arena_runner

CANARY = "synthetic-credential-canary-0001"


class ScriptedDriver:
    """A driver whose generation, verdict and declared text a test sets, and whose calls it logs."""

    name = "scripted"
    display_name = "Scripted"
    capabilities = arena_driver.CONTRACT

    def __init__(self, calls: list[str]) -> None:
        """Start with a generation, a passing preflight and nothing declared."""
        self.calls = calls
        self.generation_value: str | None = "g1"
        self.refuse = False
        self.declared: str | None = None
        self.inside_preflight: Any = None
        self.serial = 0

    def inspect(self, paths: Any) -> SimpleNamespace:
        """Return a new handle every time, so a replaced handle is visible."""
        del paths
        self.calls.append("inspect")
        self.serial += 1
        return SimpleNamespace(serial=self.serial)

    def preflight(self, handle: Any) -> frozenset[str]:
        """Pass, or refuse with the closed code; optionally change something while it runs."""
        del handle
        self.calls.append("preflight")
        if self.inside_preflight is not None:
            self.inside_preflight()
        if self.refuse:
            raise arena_driver.DriverRefused("preflight_refused", self.display_name)
        return arena_match.TOOLS

    def generation(self, handle: Any) -> str | None:
        """Return the opaque token the test set, or none."""
        del handle
        return self.generation_value

    def declared_model(self, handle: Any) -> str | None:
        """Return the text the test set."""
        del handle
        return self.declared

    def launch(self, handle: Any, scratch: Path) -> Any:
        """Never called: the claim is a stand-in here."""
        raise AssertionError((handle, scratch))


class Case:
    """A runner of the given module, its scripted driver and the intents the server shows."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        module: ModuleType = arena_runner,
    ) -> None:
        """Build a runner without a profile, a key or a connection, proven at generation g1."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.module = module
        self.calls: list[str] = []
        self.driver = ScriptedDriver(self.calls)
        self.agent = str(uuid.uuid4())
        self.root = tmp_path / "profile"
        self.runner = object.__new__(module.ArenaRunner)
        runner = self.runner
        runner.config = SimpleNamespace(agent_id=self.agent)
        runner.journal = module.RunJournal(tmp_path / "journal.sqlite3")
        runner.paths = SimpleNamespace(root=self.root)
        runner.driver = self.driver
        runner.handle = self.driver.inspect(None)
        self.calls.clear()
        runner.active = None
        runner.child = None
        runner.begin_proof()
        self.documents: list[dict[str, Any]] = []
        self.stopped = False
        monkeypatch.setattr(runner, "_post", self.post)
        monkeypatch.setattr(runner, "_launch", self.claim)

    def post(self, path: str, payload: Any) -> Any:
        """Answer the poll with whatever the server shows; nothing else may be sent."""
        assert path == "/poll", path
        return {"intents": self.documents}

    def claim(self, queued: Any) -> None:
        """Stand for claiming and launching: record it, and run as a match from now on."""
        self.calls.append("claim")
        self.claimed = queued

    def queue(self) -> dict[str, Any]:
        """Show one more queued intent."""
        document = intent(self.agent)
        self.documents.append(document)
        return document

    def run_match(self) -> None:
        """Make the runner an active one, as a claim and launch leave it."""
        document = self.queue()
        document.update(status="playing", claimed_by=self.runner.journal.runner_id)
        self.runner.active = arena_runner.StartIntent.parse(document, agent_id=self.agent)
        self.runner.deadline = float("inf")
        self.runner.finished = threading.Event()
        self.runner.playing = self.runner.terminal = False
        self.runner.child = SimpleNamespace(poll=lambda: None)
        self.runner._write_status()

    def end_match(self) -> None:
        """Make the runner idle again, as its cleanup leaves it."""
        self.runner.active = None
        self.runner.child = None
        self.documents.clear()

    def status(self) -> dict[str, Any]:
        """Return the status file as a reader would see it."""
        return arena_runner.read_status(self.root / "arena" / "status.json")

    def close(self) -> None:
        """Release the journal."""
        self.runner.journal.close()


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Provide a runner on the real module, closed afterwards."""
    built = Case(tmp_path, monkeypatch)
    yield built
    built.close()


# ---------------------------------------------------------------------------------------------
# What the generation decides
# ---------------------------------------------------------------------------------------------


def idle_unchanged(case: Case) -> None:
    """Require an unchanged idle runtime to be reused, nothing inspected or run again."""
    case.queue()
    case.runner.tick()
    case.runner.tick()
    case.runner.maintain()
    assert case.calls == ["claim", "claim"], case.calls


def test_an_unchanged_runtime_is_reused_and_claimed_without_a_new_proof(case: Case) -> None:
    """Idle and unchanged: no inspection and no preflight, the claim goes ahead."""
    idle_unchanged(case)


def idle_changed(case: Case) -> None:
    """Require a changed idle runtime to be proven and its handle replaced before the claim."""
    case.driver.generation_value = "g2"
    case.queue()
    case.runner.tick()
    assert case.calls == ["inspect", "preflight", "claim"], case.calls
    assert case.runner.handle.serial == 2, "the handle of the new generation was not adopted"
    assert case.runner.generation == "g2"
    assert case.status()["generation"] == "g2" and case.status()["preflight"] == "passed"


def test_a_changed_idle_runtime_is_proven_and_its_worker_replaced_before_the_claim(
    case: Case,
) -> None:
    """Never a claim on a generation nobody has proven."""
    idle_changed(case)


def refused_claims_nothing(case: Case) -> None:
    """Require a refused generation to claim nothing, not to retry and not to become active."""
    case.driver.generation_value = "g2"
    case.driver.refuse = True
    case.queue()
    for _ in range(3):
        case.runner.tick()
        case.runner.maintain()
    assert case.calls == ["inspect", "preflight"], case.calls
    assert case.runner.generation == "g1", "the refused generation became active"
    status = case.status()
    assert status["preflight"] == "refused" and status["refusal"] == "preflight_refused"
    assert status["changed"] is True and status["generation"] == "g1"


def test_a_refused_generation_claims_nothing_and_is_not_retried_by_itself(case: Case) -> None:
    """One preflight for the generation, one refusal, no claim, no retry."""
    refused_claims_nothing(case)


def test_a_new_generation_after_a_refusal_is_proven_again_and_then_claims(case: Case) -> None:
    """The owner repairs the runtime: that is a new generation, and it is proven once."""
    case.driver.generation_value = "g2"
    case.driver.refuse = True
    case.queue()
    case.runner.tick()
    case.driver.generation_value = "g3"
    case.driver.refuse = False
    case.runner.tick()
    case.runner.tick()
    assert case.calls == ["inspect", "preflight", "inspect", "preflight", "claim", "claim"]
    assert case.status()["preflight"] == "passed" and case.status()["generation"] == "g3"
    assert case.status()["refusal"] is None


def test_the_proof_comes_before_the_claim_in_every_order_of_events(case: Case) -> None:
    """Inspection, then preflight, then the claim - for each of two changes in a row."""
    case.queue()
    for generation in ("g2", "g3"):
        case.driver.generation_value = generation
        case.runner.tick()
    assert case.calls == ["inspect", "preflight", "claim"] * 2, case.calls


# ---------------------------------------------------------------------------------------------
# A match is pinned to what it started on
# ---------------------------------------------------------------------------------------------


def active_is_pinned(case: Case) -> None:
    """Require a change during a match to be only pending: nothing proven, swapped or stopped."""
    case.run_match()
    handle = case.runner.handle
    case.driver.generation_value = "g2"
    case.runner.tick()
    case.runner.maintain()
    assert case.calls == [], case.calls
    assert case.runner.handle is handle and case.runner.child is not None
    status = case.status()
    assert status["pending"] is True and status["generation"] == "g1" and status["playing"] is True


def test_a_change_during_a_match_stays_pending_and_touches_nothing(case: Case) -> None:
    """Pinned: no new preflight, the same handle, the same child, a pending flag in the status."""
    active_is_pinned(case)


def test_the_pending_change_takes_effect_after_the_matchs_cleanup(case: Case) -> None:
    """Terminal cleanup, then the next idle poll proves the new generation and clears pending."""
    active_is_pinned(case)
    case.end_match()
    case.runner.maintain()
    assert case.calls == ["inspect", "preflight"], case.calls
    assert case.runner.generation == "g2" and case.runner.handle.serial == 2
    status = case.status()
    assert status["pending"] is False and status["changed"] is False
    assert status["generation"] == "g2" and status["playing"] is False


def test_a_pending_change_that_fails_its_proof_leaves_the_runtime_refused_not_active(
    case: Case,
) -> None:
    """After the match the changed runtime is refused: nothing is claimed with the old proof."""
    case.run_match()
    case.driver.generation_value = "g2"
    case.driver.refuse = True
    case.runner.maintain()
    case.end_match()
    case.queue()
    case.runner.maintain()
    case.runner.tick()
    assert case.calls == ["inspect", "preflight"], case.calls
    assert case.status()["preflight"] == "refused" and case.runner.generation == "g1"


def a_change_during_the_proof(case: Case) -> None:
    """Change the runtime during its proof: that proof must not be used for the claim."""

    def change() -> None:
        case.driver.generation_value = "g3"

    case.driver.generation_value = "g2"
    case.driver.inside_preflight = change
    case.queue()
    case.runner.tick()
    assert case.calls == ["inspect", "preflight"], "a claim followed a proof of another generation"
    assert case.runner.generation == "g1"
    case.driver.inside_preflight = None
    case.runner.tick()
    assert case.calls == ["inspect", "preflight", "inspect", "preflight", "claim"], case.calls
    assert case.runner.generation == "g3"


def test_a_change_between_the_proof_and_the_claim_is_proven_again_first(case: Case) -> None:
    """The race between a change and a claim is closed on the claiming side."""
    a_change_during_the_proof(case)


# ---------------------------------------------------------------------------------------------
# A driver that cannot tell what changed
# ---------------------------------------------------------------------------------------------


def unknown_generation(case: Case) -> None:
    """With no generation, each claim is preceded by a proof, and a refusal holds for its intent."""
    case.driver.generation_value = None
    first = case.queue()
    case.runner.maintain()
    assert case.calls == [], "a proof was made with nothing to claim"
    case.runner.tick()
    case.runner.tick()
    assert case.calls == ["inspect", "preflight", "claim", "claim"], case.calls
    case.calls.clear()
    case.documents.remove(first)
    case.driver.refuse = True
    refused = case.queue()
    case.runner.tick()
    case.runner.tick()
    assert case.calls == ["inspect", "preflight"], "a refusal was retried for the same intent"
    case.calls.clear()
    case.documents.remove(refused)
    case.driver.refuse = False
    case.queue()
    case.runner.tick()
    assert case.calls == ["inspect", "preflight", "claim"], case.calls


def test_a_driver_with_no_generation_is_proven_per_intent_and_never_retried_for_one(
    case: Case,
) -> None:
    """Unknown is not unchanged."""
    unknown_generation(case)


# ---------------------------------------------------------------------------------------------
# Restart, status and the declared text
# ---------------------------------------------------------------------------------------------


def test_a_restarted_service_begins_with_the_proof_the_command_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enable, run and restart prove through one call: a new runner needs no second proof."""
    restarted = Case(tmp_path, monkeypatch)
    restarted.driver.generation_value = "g2"  # changed while the service was down
    restarted.runner.begin_proof()  # what constructing the runner after the command's proof does
    restarted.queue()
    restarted.runner.tick()
    assert restarted.calls == ["claim"], restarted.calls
    restarted.close()


def status_is_safe(case: Case) -> None:
    """Require the status to hold the allowed fields and no path, account, address or sentence."""
    case.driver.generation_value = "g2"
    case.driver.refuse = True
    case.queue()
    case.runner.tick()
    case.run_match()
    path = case.root / "arena" / "status.json"
    text = path.read_text(encoding="utf-8")
    document = json.loads(text)
    assert set(document) <= arena_runner.STATUS_KEYS
    assert all(value is None or isinstance(value, (str, bool, int)) for value in document.values())
    assert str(case.root) not in text and CANARY not in text
    assert "refused the exact" not in text, "a runtime sentence reached the status"
    assert document["refusal"] == "preflight_refused"


def test_the_status_says_what_is_allowed_and_nothing_else(case: Case) -> None:
    """Runtime, generation, changed or pending, verdict, the optional declared text."""
    status_is_safe(case)


def declared_text(case: Case) -> None:
    """Require a text the one RMD-1 check refuses never to be shown, whoever offers it."""
    case.driver.declared = "a perfectly fine model text"
    case.driver.generation_value = "g2"
    case.runner.maintain()
    assert case.status().get("declared_model") is None, "an invalid text was shown"
    case.driver.declared = "model-x/1.0"
    case.driver.generation_value = "g3"
    case.runner.maintain()
    assert case.status()["declared_model"] == "model-x/1.0"
    case.driver.declared = None
    case.driver.generation_value = "g4"
    case.runner.maintain()
    assert case.status().get("declared_model") is None


def test_the_declared_text_is_shown_only_when_the_forums_check_accepts_it(case: Case) -> None:
    """Optional, bounded, and the same RMD-1 gate as the discussion forum."""
    declared_text(case)


def test_the_status_command_shows_only_what_is_allowed(tmp_path: Path) -> None:
    """A hostile status file contributes known, typed and bounded fields and nothing more."""
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "runtime": "scripted",
                "preflight": "passed",
                "generation": "x" * 500,
                "secret": CANARY,
                "pending": True,
                "nested": {"k": CANARY},
                "declared_model": ["list"],
            }
        ),
        encoding="utf-8",
    )
    assert arena_runner.read_status(path) == {
        "runtime": "scripted",
        "preflight": "passed",
        "pending": True,
    }
    path.write_text("not json", encoding="utf-8")
    assert arena_runner.read_status(path) == {}
    assert arena_runner.read_status(tmp_path / "missing.json") == {}


# ---------------------------------------------------------------------------------------------
# Mutation proofs: break each guard and require its proof to refuse
# ---------------------------------------------------------------------------------------------

#: Name -> (guarded line of the runner, what it is replaced by, the proof that must then fail).
MUTATIONS: dict[str, tuple[str, str, Any]] = {
    "claim-without-a-proof": (
        "            if queued is not None and self._prove(queued):\n",
        "            if queued is not None:\n",
        refused_claims_nothing,
    ),
    "a-refused-generation-becomes-active": (
        "            self.refused = key\n",
        "            self.refused = key\n            self.generation = self._read_generation()\n",
        refused_claims_nothing,
    ),
    "a-refusal-is-retried-by-itself": (
        "        if key is None or key == self.refused:\n",
        "        if key is None:\n",
        refused_claims_nothing,
    ),
    "a-proof-is-made-for-every-poll": (
        "        if key == self.proven:\n            return True\n",
        "",
        idle_unchanged,
    ),
    "the-new-handle-is-not-adopted": (
        "        self.handle, self.proven, self.refused = handle, key, None\n",
        "        self.proven, self.refused = key, None\n",
        idle_changed,
    ),
    "a-running-match-is-not-pinned": (
        "        if self.active is None:\n            self._prove(None)\n            return\n",
        "        if True:\n            self._prove(None)\n            return\n",
        active_is_pinned,
    ),
    "a-change-during-the-proof-is-not-noticed": (
        "        if self._key(intent) != key:\n",
        "        if False:\n",
        a_change_during_the_proof,
    ),
    "unknown-is-treated-as-unchanged": (
        '        return f"intent:{intent.intent_id}" if intent is not None else None\n',
        "        return None\n",
        unknown_generation,
    ),
    "the-status-names-a-path": (
        '            "runtime": self.driver.name,\n',
        '            "runtime": self.driver.name,\n            "path": str(self.paths.root),\n',
        status_is_safe,
    ),
    "the-declared-text-is-not-checked": (
        "isinstance(value, str) and bridge.is_declared_model_valid(value) else None",
        "isinstance(value, str) else None",
        declared_text,
    ),
}


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_a_broken_generation_guard_is_noticed(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof holds for the real runner, and fails for the runner with this one guard broken."""
    original, replacement, proof = MUTATIONS[name]
    mutant = load_mutant(tmp_path / "mutant", arena_runner, original, replacement)

    def oracle(module: ModuleType) -> None:
        built = Case(tmp_path / module.__name__, monkeypatch, module)
        try:
            proof(built)
        finally:
            built.close()

    expect_guard(oracle, arena_runner, mutant)
