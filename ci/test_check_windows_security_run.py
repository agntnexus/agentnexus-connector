"""The Windows security job must have really run its tests, and a skip must not hide one.

A security test that is skipped is not a security test that passed. The focused `windows-latest`
job writes a JUnit file, and `check_windows_security_run.py` refuses it unless every test that
proves a Windows boundary ran and passed, nothing failed, and the only skips are the ones that
cannot apply on Windows (a POSIX notion, or a symbolic link where the account has no privilege).
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import quoteattr

import check_windows_security_run as checker


def junit(
    passed: list[str],
    *,
    failed: list[str] | None = None,
    skipped: list[tuple[str, str]] | None = None,
) -> str:
    """Return a JUnit document with these test names."""
    cases = [f'<testcase classname="ci.t" name={quoteattr(name)}/>' for name in passed]
    cases += [
        f'<testcase classname="ci.t" name={quoteattr(name)}><failure message="x"/></testcase>'
        for name in failed or []
    ]
    cases += [
        f'<testcase classname="ci.t" name={quoteattr(name)}>'
        f"<skipped message={quoteattr(reason)}/></testcase>"
        for name, reason in skipped or []
    ]
    body = "".join(cases)
    return f'<?xml version="1.0"?><testsuites><testsuite name="p">{body}</testsuite></testsuites>'


def write(tmp_path: Path, text: str) -> Path:
    """Write a JUnit file."""
    path = tmp_path / "windows.xml"
    path.write_text(text, encoding="utf-8")
    return path


def all_required() -> list[str]:
    """Return the names the check demands, each as a plain test name."""
    return sorted(checker.REQUIRED)


def filler(count: int) -> list[str]:
    """Return more passing tests, to reach the minimum."""
    return [f"test_filler_{index}" for index in range(count)]


def test_a_run_that_passed_everything_it_must_is_accepted(tmp_path: Path) -> None:
    """Control: every required test passed, nothing failed, no skip."""
    path = write(tmp_path, junit(all_required() + filler(checker.MINIMUM)))
    assert checker.problems(path) == []


def test_a_failed_test_is_refused(tmp_path: Path) -> None:
    """One failure is a failed job."""
    path = write(tmp_path, junit(all_required() + filler(checker.MINIMUM), failed=["test_x"]))
    assert any("failed" in problem for problem in checker.problems(path))


def test_a_required_test_that_was_skipped_is_refused(tmp_path: Path) -> None:
    """The access-list and job tests may not hide behind a skip."""
    required = all_required()
    skipped = [(required[0], "the access lists are Windows'")]
    path = write(tmp_path, junit(required[1:] + filler(checker.MINIMUM), skipped=skipped))
    problems = checker.problems(path)
    assert any(required[0] in problem for problem in problems)


def test_a_required_test_that_is_missing_is_refused(tmp_path: Path) -> None:
    """A test that was never collected is not a test that passed."""
    required = all_required()
    path = write(tmp_path, junit(required[1:] + filler(checker.MINIMUM)))
    assert any(required[0] in problem for problem in checker.problems(path))


def registered() -> list[tuple[str, str]]:
    """Return every (test, reason) the check allows to be skipped, with a plain reason text."""
    return [(name, reason) for name, reason in sorted(checker.ALLOWED_SKIPS.items())]


def test_a_skip_registered_for_that_test_with_that_reason_is_allowed(tmp_path: Path) -> None:
    """Each allowed skip is a named test and its exact reason; nothing else."""
    assert checker.ALLOWED_SKIPS, "the Windows job allows no skip at all, which is not the case"
    for name, reason in registered():
        path = write(
            tmp_path,
            junit(all_required() + filler(checker.MINIMUM), skipped=[(name, reason)]),
        )
        assert checker.problems(path) == [], (name, reason)


def test_a_test_that_is_not_registered_cannot_be_skipped_with_a_registered_reason(
    tmp_path: Path,
) -> None:
    """A foreign test whose skip text sounds right is refused: the name has to be registered."""
    _, reason = registered()[0]
    path = write(
        tmp_path,
        junit(
            all_required() + filler(checker.MINIMUM),
            skipped=[("test_an_unregistered_security_test", reason)],
        ),
    )
    assert any("test_an_unregistered_security_test" in p for p in checker.problems(path))


def test_a_registered_test_skipped_for_another_reason_is_refused(tmp_path: Path) -> None:
    """The right name with a reason that is not its own is a hole."""
    name, reason = registered()[0]
    other = next(r for n, r in registered() if r != reason)
    for wrong in (other, reason + " today", "privilege on Windows", "POSIX"):
        path = write(
            tmp_path,
            junit(all_required() + filler(checker.MINIMUM), skipped=[(name, wrong)]),
        )
        assert any(name in p for p in checker.problems(path)), wrong


def test_a_registered_skip_never_excuses_a_required_test(tmp_path: Path) -> None:
    """Even with its own text, a required test that was skipped is refused."""
    required = all_required()[0]
    _, reason = registered()[0]
    path = write(
        tmp_path,
        junit(all_required()[1:] + filler(checker.MINIMUM), skipped=[(required, reason)]),
    )
    assert any(required in p for p in checker.problems(path))


def test_every_registered_skip_names_a_test_that_exists_and_is_marked(tmp_path: Path) -> None:
    """A registered name that no longer exists would allow a skip nobody can check."""
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in Path(__file__).parent.glob("test_*.py")
    )
    for name in checker.ALLOWED_SKIPS:
        assert f"def {name}(" in text, f"{name} is registered as skippable but does not exist"


def test_a_skip_for_any_other_reason_is_refused(tmp_path: Path) -> None:
    """A skip that is not registered is a hole."""
    path = write(
        tmp_path,
        junit(
            all_required() + filler(checker.MINIMUM),
            skipped=[("test_something", "not worth running here")],
        ),
    )
    assert any("skip" in problem for problem in checker.problems(path))


def test_too_few_tests_are_refused(tmp_path: Path) -> None:
    """A job that ran almost nothing proved almost nothing."""
    path = write(tmp_path, junit(all_required()))
    assert any("at least" in problem for problem in checker.problems(path))


def test_an_unreadable_report_is_refused(tmp_path: Path) -> None:
    """No report, no proof."""
    assert checker.problems(tmp_path / "missing.xml")
    assert checker.problems(write(tmp_path, "not xml"))


def test_the_check_names_tests_that_exist() -> None:
    """Every required name is a test in this repository, so renaming one cannot silently pass."""
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in Path(__file__).parent.glob("test_*.py")
    )
    for name in checker.REQUIRED:
        base = name.split("[", 1)[0]
        assert f"def {base}(" in text, f"{base} is required by the Windows job but does not exist"
