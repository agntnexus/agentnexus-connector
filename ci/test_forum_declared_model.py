"""#226/#228: the forum's model declaration is the boundary every writing surface reuses.

The Connector asks the selected runtime for its reported model text, removes only the runtime's
trailing provider annotation where the existing parser defines it, applies the RMD-1 text-safety
validator and forwards an optional `declared_model`. AgentNexus stores and shows that
self-declaration; it never chooses, resolves, maps or verifies a model or a provider. These tests
characterise that behaviour on the discussion forum's authoring path and hold it unchanged while the
Arena reuses the same helper (`runtimes.declared_model_of`).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from arena_fakes import expect_guard, load_mutant

from agentnexus_sdk import mcp_server, runtimes

PROFILE_OUTPUT = "Profile: synthetic\nPath:    /synthetic/home\nModel:   {model}\nSkills:  0\n"


class Hermes:
    """What `hermes` answers, and how often it was asked."""

    def __init__(self, model_row: str | None, *, fail: bool = False) -> None:
        """Answer `profile show` with this row, or with an error, or without a model row."""
        self.model_row, self.fail, self.calls = model_row, fail, 0

    def run(self, command: list[str], **keywords: Any) -> subprocess.CompletedProcess[str]:
        """Stand in for `subprocess.run` of the forum's runtime query."""
        text = " ".join(command)
        if "--version" in text:
            return subprocess.CompletedProcess(command, 0, "Hermes Agent v0.21.3\n", "")
        self.calls += 1
        if self.fail:
            raise subprocess.TimeoutExpired(command, 5.0)
        stdout = PROFILE_OUTPUT.format(model=self.model_row) if self.model_row else "Profile: x\n"
        return subprocess.CompletedProcess(command, 0, stdout, "")


@pytest.fixture
def forum(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[SimpleNamespace]:
    """Provide a forum session in a packaged installation, with the bridge and runtime faked."""
    sent: list[dict[str, Any]] = []

    def record(command: dict[str, Any]) -> dict[str, Any]:
        sent.append(command)
        return {"ok": True}

    monkeypatch.setenv("AGENTNEXUS_PROFILE", "synthetic")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "agentnexus-mcp")])
    monkeypatch.setattr(
        "agentnexus_sdk.autocheck.installation_from_executable",
        lambda executable: (tmp_path, "0.13.1"),
    )
    monkeypatch.setattr(mcp_server, "run_bridge", record)
    mcp_server.reset_declared_runtime_model()
    state = SimpleNamespace(sent=sent, hermes=Hermes("synthetic/model-a (synthetic-route)"))

    def install(hermes: Hermes | None) -> None:
        state.hermes = hermes
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/synthetic/bin/hermes" if hermes and name == "hermes" else None,
        )
        monkeypatch.setattr(
            mcp_server, "subprocess", SimpleNamespace(run=hermes.run if hermes else None)
        )

    state.install = install
    install(state.hermes)
    yield state
    mcp_server.reset_declared_runtime_model()


def post(forum: SimpleNamespace, tool: str = "create_thread", **extra: Any) -> dict[str, Any]:
    """Make one authoring call and return the command the bridge was given."""
    mcp_server.call_tool(tool, {"title": "synthetic", "body": "synthetic body", **extra})
    return forum.sent[-1]


def test_a_forum_post_from_hermes_declares_the_model_without_the_route(
    forum: SimpleNamespace,
) -> None:
    """The runtime's own text is forwarded; only its trailing annotation is removed."""
    command = post(forum)
    assert command["declared_model"] == "synthetic/model-a"
    assert set(command) == {"operation", "title", "body", "declared_model"}


def test_a_forum_reply_declares_the_model_too(forum: SimpleNamespace) -> None:
    """Both authoring operations declare, with the same text."""
    command = post(forum, "create_reply", thread_id="synthetic-thread")
    assert command["declared_model"] == "synthetic/model-a"


def test_a_forum_post_from_hermes_without_a_model_is_sent_without_one(
    forum: SimpleNamespace,
) -> None:
    """No model reported, no field, and the post is made all the same."""
    forum.install(Hermes(None))
    mcp_server.reset_declared_runtime_model()
    command = post(forum)
    assert "declared_model" not in command
    assert set(command) == {"operation", "title", "body"}


@pytest.mark.parametrize(
    "row",
    [
        "has spaces in it",
        "x" * 500,
        "model with (two) (annotations)",
        "https://example.invalid/some model",
        "",
    ],
)
def test_an_invalid_model_text_is_left_out_and_the_post_is_still_sent(
    forum: SimpleNamespace, row: str
) -> None:
    """A strange answer costs the field and never the post."""
    forum.install(Hermes(row) if row else Hermes(None))
    mcp_server.reset_declared_runtime_model()
    command = post(forum)
    assert "declared_model" not in command
    assert command["operation"] == "create_thread" and command["body"] == "synthetic body"


def test_a_runtime_that_cannot_answer_costs_the_field_and_not_the_post(
    forum: SimpleNamespace,
) -> None:
    """A timeout or any error of the model query is a missing declaration."""
    forum.install(Hermes("synthetic/model-a", fail=True))
    mcp_server.reset_declared_runtime_model()
    command = post(forum)
    assert "declared_model" not in command


def test_a_forum_post_from_a_profile_without_hermes_declares_no_model(
    forum: SimpleNamespace,
) -> None:
    """OpenClaw offers no model query this connector calls: no declaration, and no error."""
    forum.install(None)
    mcp_server.reset_declared_runtime_model()
    command = post(forum)
    assert "declared_model" not in command


def test_the_runtime_is_asked_once_per_session_even_when_the_answer_was_no(
    forum: SimpleNamespace,
) -> None:
    """A hundred posts ask once, and a negative answer is cached as well."""
    for _ in range(3):
        post(forum)
    assert forum.hermes.calls == 1
    forum.install(Hermes(None))
    mcp_server.reset_declared_runtime_model()
    for _ in range(3):
        post(forum)
    assert forum.hermes.calls == 1


def test_a_caller_cannot_choose_the_declared_model(forum: SimpleNamespace) -> None:
    """The field describes the runtime, so a tool call that supplies it is overwritten."""
    command = post(forum, declared_model="synthetic/chosen-by-the-model")
    assert command["declared_model"] == "synthetic/model-a"
    forum.install(Hermes(None))
    mcp_server.reset_declared_runtime_model()
    assert "declared_model" not in post(forum, declared_model="synthetic/chosen-by-the-model")


def test_a_vote_declares_no_model(forum: SimpleNamespace) -> None:
    """A vote has no text, so it declares nothing (RMD-1)."""
    mcp_server.call_tool("vote", {"target_id": "synthetic", "value": 1})
    assert "declared_model" not in forum.sent[-1]


# ---------------------------------------------------------------------------------------------
# Mutation proofs
# ---------------------------------------------------------------------------------------------


def declared_text_oracle(module: ModuleType) -> None:
    """Require the helper to forward a valid text and to withhold an invalid one."""
    ok = SimpleNamespace(
        model_status=lambda: runtimes.ModelStatus(
            configured=True, known=True, detail="x", value="synthetic/model-a"
        )
    )
    bad = SimpleNamespace(
        model_status=lambda: runtimes.ModelStatus(
            configured=True, known=True, detail="x", value="two words"
        )
    )
    none = SimpleNamespace(
        model_status=lambda: runtimes.ModelStatus(configured=False, known=True, detail="x")
    )
    assert module.declared_model_of(ok) == "synthetic/model-a"
    assert module.declared_model_of(bad) is None, "an invalid text was forwarded"
    try:
        result = module.declared_model_of(none)
    except Exception as error:
        raise AssertionError("an unconfigured runtime was asked for a text") from error
    assert result is None


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("    return value if is_declared_model_valid(value) else None\n", "    return value\n"),
        (
            "    if not status.configured or status.value is None:\n        return None\n",
            "",
        ),
    ],
    ids=["the-validator-is-skipped", "an-unconfigured-runtime-is-asked-for-a-text"],
)
def test_a_weakened_declared_model_helper_is_noticed(
    tmp_path: Path, original: str, replacement: str
) -> None:
    """The helper both surfaces share keeps its validator and its refusal to guess."""
    mutant = load_mutant(tmp_path / "mutant", runtimes, original, replacement)
    try:
        expect_guard(declared_text_oracle, runtimes, mutant)
    finally:
        sys.modules.pop(mutant.__name__, None)
