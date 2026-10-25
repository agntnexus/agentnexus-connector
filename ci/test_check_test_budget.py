"""The test-budget guard refuses what it must (D-222; agntnexus/agentnexus#462, decision D-230).

It refuses a job that runs pytest without a timeout inside the ceiling, a pytest step without its
own timeout inside it, a workflow that runs no pytest step, a test file that no pytest step runs,
and a count of test files or test functions above the recorded ceiling. Synthetic workflows and
trees exercise each refusal; the repository itself is checked as well.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "ci"
if str(CI) not in sys.path:
    sys.path.insert(0, str(CI))
import check_test_budget as guard  # noqa: E402 -- ci/ is not a package, so its path is set up first.


def _workflow(
    step_timeout: Any = 8,
    job_timeout: Any = 8,
    run: str = "python -m pytest ci/test_synthetic_0.py -q",
) -> dict[str, Any]:
    """Build a one-job workflow whose single pytest step carries the given timeouts."""
    step: dict[str, Any] = {"name": "Run the tests", "run": run}
    if step_timeout is not None:
        step["timeout-minutes"] = step_timeout
    job: dict[str, Any] = {"runs-on": "ubuntu-latest", "steps": [step]}
    if job_timeout is not None:
        job["timeout-minutes"] = job_timeout
    return {"jobs": {"job": job}}


def _tree(
    root: Path,
    *,
    functions: int,
    record_functions: int,
    files: int = 1,
    record_files: int = 5,
) -> Path:
    """Build a synthetic tree: its first test file holds `functions` tests, `files` files in all."""
    (root / "ci").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    names = [f"test_synthetic_{number}.py" for number in range(files)]
    for number, name in enumerate(names):
        count = functions if number == 0 else 0
        body = "".join(f"def test_case_{index}() -> None:\n    pass\n\n" for index in range(count))
        (root / "ci" / name).write_text(body or "VALUE = 1\n", encoding="utf-8")
    run = "python -m pytest " + " ".join(f"ci/{name}" for name in names) + " -q"
    workflow = yaml.safe_dump(_workflow(run=run), sort_keys=False)
    (root / ".github" / "workflows" / "ci.yml").write_text(workflow, encoding="utf-8")
    record = {
        "decision": "D-230",
        "ceiling_seconds": 480,
        "max_test_files": record_files,
        "max_test_functions": record_functions,
    }
    (root / "ci" / "test_budget.json").write_text(json.dumps(record), encoding="utf-8")
    return root


def test_a_pytest_job_and_step_within_the_ceiling_pass() -> None:
    """Accept a job and a step whose timeouts are both at the ceiling."""
    assert guard.timeout_problems(_workflow(step_timeout=8, job_timeout=8)) == []


def test_a_pytest_step_over_the_ceiling_is_refused_by_name() -> None:
    """Refuse a pytest step over the ceiling, and name the step in the refusal."""
    problems = guard.timeout_problems(_workflow(step_timeout=9, job_timeout=8))
    assert len(problems) == 1
    assert "exceeds the 8-minute ceiling" in problems[0]
    assert "Run the tests" in problems[0]


def test_a_pytest_step_without_a_timeout_is_refused() -> None:
    """Refuse a pytest step that declares no timeout of its own."""
    problems = guard.timeout_problems(_workflow(step_timeout=None, job_timeout=8))
    assert len(problems) == 1
    assert "must declare timeout-minutes" in problems[0]


def test_a_job_running_pytest_over_the_ceiling_is_refused_by_name() -> None:
    """Refuse a job that runs pytest with a timeout above the ceiling, and name the job."""
    problems = guard.timeout_problems(_workflow(step_timeout=8, job_timeout=9))
    assert len(problems) == 1
    assert "job: timeout-minutes 9 exceeds the 8-minute ceiling" in problems[0]


def test_a_job_running_pytest_without_a_timeout_is_refused() -> None:
    """Refuse a job that runs pytest and declares no job timeout."""
    problems = guard.timeout_problems(_workflow(step_timeout=8, job_timeout=None))
    assert len(problems) == 1
    assert "must declare timeout-minutes" in problems[0]


def test_a_boolean_or_fractional_timeout_is_not_a_bound() -> None:
    """Refuse a boolean or a fractional timeout, since neither is a whole number of minutes."""
    assert guard.timeout_problems(_workflow(step_timeout=True, job_timeout=8)) != []
    assert guard.timeout_problems(_workflow(step_timeout=8, job_timeout=2.5)) != []


def test_a_workflow_that_runs_no_pytest_step_is_refused() -> None:
    """Refuse a workflow with no pytest step, since nothing in it would be time-bounded."""
    workflow = {"jobs": {"job": {"steps": [{"name": "Lint", "run": "ruff check ."}]}}}
    assert guard.timeout_problems(workflow) != []


def test_a_path_that_merely_names_pytest_is_not_a_pytest_step() -> None:
    """Refuse a step that only mentions pytest.ini, because it does not run pytest."""
    workflow = {"jobs": {"job": {"steps": [{"name": "Hygiene", "run": "grep pytest.ini"}]}}}
    assert guard.timeout_problems(workflow) != []


def test_a_test_file_that_no_pytest_step_runs_is_refused(tmp_path: Path) -> None:
    """Name a test file that no pytest step runs, so that it guards nothing."""
    root = _tree(tmp_path / "tree", functions=1, record_functions=1, files=2)
    workflow = _workflow(run="python -m pytest ci/test_synthetic_0.py -q")
    assert guard.unrun_test_files(root, workflow) == ["test_synthetic_1.py"]


def test_a_test_file_run_by_a_step_of_another_job_counts_as_run(tmp_path: Path) -> None:
    """Count a test file as run when a pytest step of any job names it."""
    root = _tree(tmp_path / "tree", functions=0, record_functions=0, files=2)
    workflow = {
        "jobs": {
            "one": {
                "timeout-minutes": 8,
                "steps": [
                    {"run": "python -m pytest ci/test_synthetic_0.py -q", "timeout-minutes": 8}
                ],
            },
            "two": {
                "timeout-minutes": 8,
                "steps": [
                    {"run": "python -m pytest ci/test_synthetic_1.py -q", "timeout-minutes": 8}
                ],
            },
        }
    }
    assert guard.unrun_test_files(root, workflow) == []


def test_the_repository_workflow_meets_the_ceiling() -> None:
    """Accept the repository's own workflow: every pytest job and step is bounded in time."""
    workflow = yaml.safe_load((ROOT / guard.WORKFLOW).read_text(encoding="utf-8"))
    assert guard.timeout_problems(workflow) == []


def test_the_repository_runs_every_test_file() -> None:
    """Accept the repository only when every ci/test_*.py file is run by a pytest step."""
    workflow = yaml.safe_load((ROOT / guard.WORKFLOW).read_text(encoding="utf-8"))
    assert guard.unrun_test_files(ROOT, workflow) == []


def test_a_test_count_at_the_recorded_ceiling_passes(tmp_path: Path) -> None:
    """Accept a tree whose counts are exactly the recorded ceilings."""
    root = _tree(tmp_path / "tree", functions=3, record_functions=3)
    assert guard.check(root) == []


def test_a_test_function_beyond_the_recorded_ceiling_is_refused(tmp_path: Path) -> None:
    """Refuse one test function beyond the recorded ceiling, naming the decision."""
    root = _tree(tmp_path / "tree", functions=4, record_functions=3)
    problems = guard.check(root)
    assert len(problems) == 1
    assert "4 test functions exceed the recorded ceiling of 3" in problems[0]
    assert "D-230" in problems[0]
    assert guard.main(["--root", str(root)]) == 1


def test_a_test_file_beyond_the_recorded_ceiling_is_refused(tmp_path: Path) -> None:
    """Refuse one test file beyond the recorded file ceiling."""
    root = _tree(tmp_path / "tree", functions=1, record_functions=1, files=2, record_files=1)
    problems = guard.check(root)
    assert len(problems) == 1
    assert "2 test files exceed the recorded ceiling of 1" in problems[0]


def test_a_record_that_names_another_ceiling_is_refused(tmp_path: Path) -> None:
    """Refuse a record whose ceiling_seconds is not the 480 seconds of D-222 statement 3."""
    root = _tree(tmp_path / "tree", functions=1, record_functions=1)
    record = json.loads((root / "ci" / "test_budget.json").read_text(encoding="utf-8"))
    record["ceiling_seconds"] = 900
    (root / "ci" / "test_budget.json").write_text(json.dumps(record), encoding="utf-8")
    problems = guard.check(root)
    assert any("ceiling_seconds 900" in problem for problem in problems)


@pytest.mark.parametrize("keyword", ["def", "async def"])
def test_a_test_method_counts_whatever_its_keyword(tmp_path: Path, keyword: str) -> None:
    """Count a test method of a test class, whether it is a plain or an async function."""
    root = _tree(tmp_path / "tree", functions=0, record_functions=0)
    source = f"class TestGroup:\n    {keyword} test_a_method(self) -> None:\n        pass\n"
    (root / "ci" / "test_synthetic_0.py").write_text(source, encoding="utf-8")
    assert guard.count_tests(root) == (1, 1)


def test_the_recorded_decision_and_ceiling_are_the_ones_this_slice_states() -> None:
    """Name D-230 and the 480-second ceiling in the record of this repository."""
    record = json.loads((ROOT / guard.RECORD).read_text(encoding="utf-8"))
    assert record["decision"] == "D-230"
    assert record["ceiling_seconds"] == 480


def test_the_repository_is_within_its_recorded_counts() -> None:
    """Accept the repository's own test counts against its recorded ceilings."""
    assert guard.check(ROOT) == []
    assert guard.main(["--root", str(ROOT)]) == 0
