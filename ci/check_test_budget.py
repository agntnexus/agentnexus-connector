"""Test-budget ceiling guard (D-222; agntnexus/agentnexus#462, decision D-230).

The Connector counts toward the ten-minute test budget of D-222 like every other component. Three
things are checked on every pull request, from local files only: no network, no credential, no
clock, so a loaded runner gives the same answer as an idle one.

1. Time. Every job that runs pytest declares `timeout-minutes` no larger than the ceiling, and so
   does every pytest step. The runner ends a job or a step that runs longer, so a chain of test
   steps cannot run past the ceiling without the run failing.
2. Coverage. Every `ci/test_*.py` file is run by a pytest step of some job. A test file that no
   step runs counts toward the budget and guards nothing.
3. Count. `ci/test_budget.json` records how many test files and test functions this repository may
   hold. Only a decision raises the record; a larger count is refused, and the record names the
   decision that set it.

usage: check_test_budget.py [--root <repository>]   (exit 0 when within the ceilings)
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

import yaml

#: D-222 statement 3: the critical path of each component suite, in seconds.
CEILING_SECONDS = 480
CEILING_MINUTES = CEILING_SECONDS // 60
TEST_DIRECTORY = Path("ci")
TEST_FILE_PATTERN = "test_*.py"
WORKFLOW = Path(".github") / "workflows" / "ci.yml"
RECORD = Path("ci") / "test_budget.json"


def _bounded(value: Any) -> bool:
    """Return whether `value` is a usable timeout: a whole number of minutes, not a boolean."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def pytest_steps(workflow: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Return every step of every job whose command runs pytest, as (job, step name, step)."""
    found = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in (job or {}).get("steps") or []:
            # The invocation itself; a path that only names pytest, like pytest.ini, is not one.
            if "-m pytest" in str(step.get("run", "")):
                found.append((job_name, str(step.get("name", "<unnamed>")), step))
    return found


def timeout_problems(workflow: dict[str, Any]) -> list[str]:
    """Return every job and pytest step that is unbounded, or bounded above the ceiling, by name."""
    steps = pytest_steps(workflow)
    if not steps:
        return ["the workflow runs no pytest step at all; nothing would be time-bounded"]
    problems = []
    for job_name in dict.fromkeys(job for job, _, _ in steps):
        timeout = (workflow["jobs"][job_name] or {}).get("timeout-minutes")
        if not _bounded(timeout):
            problems.append(f"{job_name}: a job that runs pytest must declare timeout-minutes")
        elif timeout > CEILING_MINUTES:
            problems.append(
                f"{job_name}: timeout-minutes {timeout} exceeds the "
                f"{CEILING_MINUTES}-minute ceiling"
            )
    for job_name, step_name, step in steps:
        timeout = step.get("timeout-minutes")
        if not _bounded(timeout):
            problems.append(
                f"{job_name} / {step_name!r}: a pytest step must declare timeout-minutes"
            )
        elif timeout > CEILING_MINUTES:
            problems.append(
                f"{job_name} / {step_name!r}: timeout-minutes {timeout} exceeds the "
                f"{CEILING_MINUTES}-minute ceiling"
            )
    return problems


def repository_test_files(root: Path) -> list[str]:
    """Return the names of the repository's test files, sorted."""
    return sorted(path.name for path in (root / TEST_DIRECTORY).glob(TEST_FILE_PATTERN))


def unrun_test_files(root: Path, workflow: dict[str, Any]) -> list[str]:
    """Return each test file that no pytest step of any job names, so that nothing runs it."""
    named: set[str] = set()
    for _, _, step in pytest_steps(workflow):
        named.update(str(step.get("run", "")).split())
    return [
        name
        for name in repository_test_files(root)
        if f"{TEST_DIRECTORY.as_posix()}/{name}" not in named
    ]


def count_tests(root: Path) -> tuple[int, int]:
    """Return the (test files, test functions) of the repository, counted from the source."""
    files = [root / TEST_DIRECTORY / name for name in repository_test_files(root)]
    functions = 0
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        functions += sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
            for node in ast.walk(tree)
        )
    return len(files), functions


def record_problems(record: dict[str, Any]) -> list[str]:
    """Return a problem if the record names a ceiling other than the one this guard enforces."""
    if record.get("ceiling_seconds") != CEILING_SECONDS:
        return [
            f"the record's ceiling_seconds {record.get('ceiling_seconds')} is not the "
            f"{CEILING_SECONDS} seconds of D-222 statement 3"
        ]
    return []


def count_problems(root: Path, record: dict[str, Any]) -> list[str]:
    """Return each count above its recorded ceiling, naming the decision that set the ceiling."""
    files, functions = count_tests(root)
    problems = []
    if files > record["max_test_files"]:
        problems.append(
            f"{files} test files exceed the recorded ceiling of {record['max_test_files']} "
            f"({record['decision']})"
        )
    if functions > record["max_test_functions"]:
        problems.append(
            f"{functions} test functions exceed the recorded ceiling of "
            f"{record['max_test_functions']} ({record['decision']})"
        )
    return problems


def check(root: Path) -> list[str]:
    """Return every problem with the time ceiling, the coverage and the counts at root."""
    workflow = yaml.safe_load((root / WORKFLOW).read_text(encoding="utf-8"))
    record = json.loads((root / RECORD).read_text(encoding="utf-8"))
    return (
        timeout_problems(workflow)
        + [
            f"{name} is a test file that no pytest step runs; it counts toward the budget "
            "and guards nothing"
            for name in unrun_test_files(root, workflow)
        ]
        + record_problems(record)
        + count_problems(root, record)
    )


def main(argv: list[str] | None = None) -> int:
    """Print the verdict, and return 0 within the ceilings or 1 when one is exceeded."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parents[1]
    if arguments[:1] == ["--root"]:
        root = Path(arguments[1]).resolve()
    problems = check(root)
    if problems:
        print("REFUSED: the test budget is exceeded (D-222, D-230):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    files, functions = count_tests(root)
    print(
        f"test budget within its ceilings: {files} test files, {functions} test functions; "
        f"every pytest job and step is bounded by {CEILING_MINUTES} minutes, and every test "
        "file is run."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
