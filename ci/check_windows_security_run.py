"""Refuse a Windows security job that did not really run its tests.

The public Connector repository runs only on GitHub-hosted runners. A focused `windows-latest` job
runs the tests that prove the Windows boundaries (the owner and access list of a profile, the job
object a runtime starts in, the pinned state of its files, and that nothing is forwarded after a
change) and writes a JUnit report. A security test that is skipped did not pass, and a symbolic-link
test that needs a privilege must not hide the others, so this reads the report and refuses it
unless every required test passed, nothing failed, only skips that cannot apply on Windows occurred,
and enough tests ran.

    python ci/check_windows_security_run.py windows-security.xml
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

#: Tests that must have run and passed on Windows, by name (parameters are folded into the name).
REQUIRED = frozenset(
    {
        "test_only_the_intended_user_and_the_system_accounts_may_reach_a_directory",
        "test_a_directory_an_elevated_administrator_created_is_private_to_that_user",
        "test_a_directory_locked_to_the_user_is_private",
        "test_a_directory_shared_with_another_group_is_not_private",
        "test_a_state_directory_shared_below_a_private_profile_is_refused",
        "test_a_directory_whose_access_cannot_be_read_is_not_private",
        "test_a_profile_the_access_lists_cannot_prove_is_refused_before_anything_else",
        "test_the_access_check_opens_nothing_inside_the_directory",
        "test_the_job_calls_are_typed_and_the_handle_is_a_handle",
        "test_a_job_that_cannot_be_made_or_limited_refuses",
        "test_a_process_that_cannot_join_its_job_is_ended_and_the_start_refuses",
        "test_a_process_starts_suspended_and_joins_its_job_before_it_runs",
        "test_ending_a_run_on_windows_ends_the_child_and_the_grandchild",
        "test_a_run_ends_a_child_a_grandchild_and_one_in_a_session_of_its_own",
        "test_a_failed_job_makes_the_openclaw_preflight_refuse_before_any_claim",
        "test_the_proof_of_containment_holds_on_this_machine",
        "test_the_fingerprint_covers_the_configuration_and_the_optional_env_separately",
        "test_an_env_created_removed_or_replaced_changes_the_fingerprint",
        "test_a_file_owned_by_someone_else_has_no_fingerprint",
        "test_a_changed_configuration_ends_the_runtime_path_and_forwards_no_move",
        "test_a_profile_env_created_removed_or_replaced_during_the_match_forwards_no_move",
        "test_a_change_after_the_first_move_stops_the_match_before_its_second",
        "test_the_cutoff_ends_every_process_the_runtime_started",
        "test_a_cleanup_that_is_cut_off_ends_the_whole_tree_of_the_worker_it_replaces",
        "test_the_parent_ends_the_whole_tree_of_a_match_process_it_cuts_off",
        "test_the_capability_proof_holds_a_detached_grandchild_too",
        "test_a_stop_that_could_not_prove_containment_blocks_every_later_claim",
        "test_a_changed_state_found_while_a_stop_begins_forwards_no_move",
        "test_a_stop_cannot_begin_between_the_check_and_the_forward",
        "test_a_profile_file_changed_after_the_decision_forwards_no_move_as_a_stop_begins",
        "test_a_broken_gate_at_the_first_proof_asks_the_runtime_nothing",
        "test_a_new_generation_under_a_broken_gate_asks_nothing_and_shows_no_new_value",
        "test_a_valid_path_asks_exactly_once_and_claims_the_frozen_value",
        "test_the_launch_check_stays_a_second_defence",
        "test_a_valid_text_appears_exactly_in_the_status",
        "test_a_missing_text_is_honestly_absent_and_no_null_is_written",
        "test_an_invalid_text_does_not_appear_in_the_status",
        "test_a_text_with_a_control_character_does_not_appear_in_the_status",
        "test_a_timed_out_text_appears_in_neither_the_status_nor_the_claim",
        "test_raw_driver_output_reaches_neither_the_status_nor_a_diagnostic",
        "test_the_status_file_sits_inside_the_profile_and_the_profile_is_private",
    }
)
#: How many tests must have passed in all: a job that ran a handful proved a handful.
MINIMUM = 60
#: The only skips allowed: this test, with exactly this reason. A security test that is not
#: listed here cannot be skipped, whatever its skip text says; one that is listed cannot be skipped
#: for another reason. Each is a thing that cannot apply on Windows.
_POSIX_TABLE = "the process table is the POSIX way to find a tree"
ALLOWED_SKIPS = {
    "test_a_symbolic_link_in_place_of_the_file_has_no_fingerprint": (
        "a symbolic link needs a privilege on Windows"
    ),
    "test_a_run_whose_tree_cannot_be_listed_does_not_report_success": _POSIX_TABLE,
    "test_without_a_process_table_the_proof_of_containment_fails": _POSIX_TABLE,
    "test_a_process_table_that_cannot_be_read_is_an_error_never_an_empty_proof": _POSIX_TABLE,
    "test_a_table_of_only_valid_lines_is_parsed_whole": _POSIX_TABLE,
    "test_a_partly_broken_table_gives_a_kill_that_is_not_complete": _POSIX_TABLE,
    "test_the_process_table_walk_finds_children_and_grandchildren": _POSIX_TABLE,
}


def problems(report: Path) -> list[str]:
    """Return everything wrong with this JUnit report; empty when the job really ran."""
    try:
        root = ElementTree.parse(report).getroot()  # noqa: S314 - the job's own pytest report
    except (OSError, ElementTree.ParseError) as error:
        return [f"The report {report} cannot be read: {error}"]
    passed: dict[str, int] = {}
    missing: dict[str, int] = {}
    found: list[str] = []
    failures = 0
    for case in root.iter("testcase"):
        name = case.get("name", "")
        base = name.split("[", 1)[0]
        if case.find("failure") is not None or case.find("error") is not None:
            failures += 1
            missing[base] = missing.get(base, 0) + 1
            continue
        skipped = case.find("skipped")
        if skipped is not None:
            reason = skipped.get("message", "") or (skipped.text or "")
            if ALLOWED_SKIPS.get(base) != reason:
                found.append(f"{name} was skipped, which is not allowed for it: {reason!r}")
            missing[base] = missing.get(base, 0) + 1
            continue
        passed[base] = passed.get(base, 0) + 1
    if failures:
        found.append(f"{failures} test(s) failed")
    for name in sorted(REQUIRED):
        if not passed.get(name) or missing.get(name):
            found.append(f"required test {name} did not run and pass")
    total = sum(passed.values())
    if total < MINIMUM:
        found.append(f"at least {MINIMUM} passing tests were expected, {total} ran")
    return found


def main(argv: list[str]) -> int:
    """Check the report named on the command line."""
    if len(argv) != 2:
        sys.stderr.write("usage: check_windows_security_run.py REPORT.xml\n")
        return 2
    found = problems(Path(argv[1]))
    for line in found:
        sys.stderr.write(f"REFUSED: {line}\n")
    if not found:
        sys.stdout.write("The Windows security job ran every required test and none was skipped.\n")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
