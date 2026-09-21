"""Check the governance this repository states about itself, and that CI checks it.

Issue-authority text is the kind that rots silently. Nothing fails when it goes stale: a
contributor simply opens an Issue in a repository nobody watches any more, and the first sign is
that the work was never seen. There is no build to break and no runtime to observe, which is
exactly why it is worth a test (agntnexus/agentnexus#46).

Five of the six component repositories already carry a guard like this one. This repository was
the exception, and not by oversight: it had no test harness at all, so there was nothing for a
guard to ride in. `pytest` was already pinned in `pyproject.toml` and never run. This file is the
harness, and the workflow step that runs it is the other half -- a guard nobody executes is a
guard that does not exist, which is why the first case below asserts that CI executes this file.

It reads local files and nothing else: no network, no credential, no repository but this one.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = REPOSITORY_ROOT / "AGENTS.md"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"

#: The tracker for the portfolio.
UMBRELLA = "agntnexus/agentnexus"

#: Forbidden for all new Issue activity, named rather than merely excluded: "some other
#: repository" is a refusal nobody can act on.
FORBIDDEN = "ppoinha/AIExperiment"

#: Historical. Active instructions may name it; none may present it as the current authority.
HISTORICAL = "proplaner/agentnexus-original"

#: A line naming the historical repository is acceptable when it says, on that same line, that it
#: is history. Anything else is an active instruction treating it as present tense. A rule that
#: banned the name outright would make the cutover unrecordable.
HISTORICAL_MARKERS = ("historical", "history", "archived", "archive", "superseded", "no longer")

#: Sending work somewhere is the thing that matters, so this looks for the sentence shape that
#: does it rather than for the bare name.
ROUTES_WORK = re.compile(
    r"(?i)\b(issues?|pull requests?|work|changes?)\b[^.\n]{0,80}"
    r"\b(belong|live|go|are tracked|are traced|are opened)\b[^.\n]{0,80}"
    r"(" + re.escape(HISTORICAL) + "|" + re.escape(FORBIDDEN) + ")"
)


def instructions() -> str:
    """Read the active instruction file, the way CI reads it."""
    return INSTRUCTIONS.read_text(encoding="utf-8")


def governance_problems(text: str) -> list[str]:
    """Everything wrong with an instruction text, or an empty list.

    A pure function over the text so that the mutation cases below can hand it a deliberately
    broken copy without writing to the repository.
    """
    found: list[str] = []
    if UMBRELLA not in text:
        found.append(f"does not name {UMBRELLA} as the tracker")
    if f"`{FORBIDDEN}` is forbidden" not in text:
        found.append(f"does not forbid {FORBIDDEN} by name")
    for number, line in enumerate(text.splitlines(), start=1):
        if HISTORICAL in line and not any(marker in line.lower() for marker in HISTORICAL_MARKERS):
            found.append(f"line {number} presents {HISTORICAL} as current")
    if ROUTES_WORK.search(text):
        found.append("routes new work to a retired or forbidden repository")
    return found


def test_ci_executes_this_guard() -> None:
    """A guard nobody runs is a guard that does not exist.

    This repository had `pytest` pinned and no test to run and no step to run it, so a governance
    guard committed here would have sat unexecuted and nothing would have said so.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "pytest" in workflow, "no workflow step runs pytest"
    assert "ci/test_governance.py" in workflow, "CI does not name this guard"


def test_ci_runs_the_guard_on_pull_requests_and_pushes() -> None:
    """Both, because either alone leaves a route into `main` unchecked."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert re.search(r"(?m)^on:", workflow), "the workflow declares no triggers"
    assert re.search(r"(?m)^  pull_request:", workflow), "no pull_request trigger"
    assert re.search(r"(?m)^  push:", workflow), "no push trigger"


def test_the_guard_runs_on_a_github_hosted_runner() -> None:
    """The public-repository boundary, asserted where it is easiest to break by accident.

    A public pull request can contain code nobody has reviewed. Letting it execute on an
    operator-maintained machine would hand that machine to whoever opened the pull request, so
    this repository selects a GitHub-hosted runner and never the organisation group.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "runs-on: ubuntu-latest" in workflow
    for forbidden in ("private-ci", "self-hosted", "agntnexus-linux", "agntnexus-windows"):
        assert forbidden not in workflow, f"the workflow selects {forbidden}"


def test_new_work_is_tracked_in_the_umbrella() -> None:
    """Where an Issue goes, stated by the instructions a contributor actually opens."""
    text = instructions()

    assert "Issues for the portfolio belong in" in text
    assert UMBRELLA in text


def test_the_forbidden_account_is_named() -> None:
    """Naming it is the point. It is reachable from old links scattered through the history."""
    assert f"`{FORBIDDEN}` is forbidden" in instructions()


def test_the_historical_repository_is_only_historical() -> None:
    """Mentioning it is fine and often necessary; presenting it as current is the regression."""
    assert governance_problems(instructions()) == []


def test_the_guard_reads_a_file_that_exists() -> None:
    """Fail closed: a guard over a file that is missing or empty has checked nothing."""
    assert INSTRUCTIONS.is_file(), INSTRUCTIONS
    assert instructions().strip(), "AGENTS.md is empty"


def test_a_restored_stale_tracker_claim_is_refused() -> None:
    """The mutation proof: the sentence this repository used to carry, put back.

    Restored against a copy in memory rather than against the file, so the repository is never
    mutated to prove that a check works.
    """
    mutated = instructions().replace(
        "**Issues for the portfolio belong in",
        f"**All issues belong in [`{HISTORICAL}`](https://github.com/{HISTORICAL}/issues).**\n\n"
        "**Issues for the portfolio belong in",
        1,
    )

    assert mutated != instructions(), "the mutation changed nothing; this case would prove nothing"
    assert governance_problems(mutated) != []


def test_losing_the_umbrella_is_refused() -> None:
    """The other half of the same regression: the tracker simply disappearing from the text."""
    mutated = instructions().replace(UMBRELLA, "somewhere/else")

    assert governance_problems(mutated) != []


def test_losing_the_forbidden_account_is_refused() -> None:
    """Softening the refusal is the same regression as dropping the name."""
    mutated = instructions().replace(f"`{FORBIDDEN}` is forbidden", "that account is discouraged")

    assert governance_problems(mutated) != []


def test_explicitly_historical_wording_is_accepted() -> None:
    """The counter-probe.

    A rule that refused the name outright would make it impossible to say what this repository
    moved away from, and an instruction file that cannot record its own history is one nobody can
    check against the past.
    """
    accepted = (
        instructions()
        + f"\n\nThe historical repository `{HISTORICAL}` is archived in the umbrella.\n"
    )

    assert governance_problems(accepted) == []


def test_routing_work_to_the_forbidden_account_is_refused() -> None:
    """Naming it as forbidden is required; sending anything there is not the same sentence."""
    mutated = instructions() + f"\n\nPull requests belong in `{FORBIDDEN}`.\n"

    assert governance_problems(mutated) != []
