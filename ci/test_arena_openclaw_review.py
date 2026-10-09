"""Review blockers of the OpenClaw runtime (agntnexus/agentnexus#228): privacy and job object.

* The profile and its state hold the runtime's own authentication store. On Windows the Connector
  proves, from the owner and the access list of the directories alone, that only the intended
  user (and the system accounts that can take any file anyway) can reach them, and refuses before a
  seat is claimed when it cannot prove it. Nothing inside a directory is opened.
* A runtime that starts children is ended with all of them. On Windows the process starts
  suspended, joins a job object before its first instruction, and anything that goes wrong on the
  way ends it and refuses.
"""

from __future__ import annotations

import builtins
import contextlib
import hashlib
import io
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arena_fakes import expect_guard, load_mutant, supervisor
from arena_openclaw_support import make_private, stand_in_handle
from fake_chat_model import FakeChatModel

from agentnexus_sdk import (
    arena_driver,
    arena_driver_openclaw,
    arena_match,
    arena_runner,
    openclaw_arena,
)

WINDOWS = sys.platform == "win32"
ME = "S-1-5-21-1-2-3-1001"
OTHER = "S-1-5-21-1-2-3-1002"
SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
EVERYONE = "S-1-1-0"
AUTHENTICATED = "S-1-5-11"
ALLOWED, DENIED, OBJECT_ALLOWED, CALLBACK_ALLOWED = 0, 1, 5, 9


# ---------------------------------------------------------------------------------------------
# The decision on an owner and an access list (any platform)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("owner", "entries", "private"),
    [
        (ME, [(ALLOWED, ME)], True),
        (ME, [(ALLOWED, ME), (ALLOWED, SYSTEM), (ALLOWED, ADMINISTRATORS)], True),
        (ME, [(ALLOWED, ME), (DENIED, EVERYONE)], True),
        (ME, [(ALLOWED, ME), (ALLOWED, USERS)], False),
        (ME, [(ALLOWED, ME), (ALLOWED, EVERYONE)], False),
        (ME, [(ALLOWED, ME), (ALLOWED, AUTHENTICATED)], False),
        (ME, [(ALLOWED, ME), (ALLOWED, OTHER)], False),
        (OTHER, [(ALLOWED, ME)], False),
        (ADMINISTRATORS, [(ALLOWED, ME)], False),
        (ME, None, False),
        (ME, [(OBJECT_ALLOWED, ME)], False),
        (ME, [(CALLBACK_ALLOWED, ME)], False),
        (ME, [(ALLOWED, "not a sid")], False),
        ("", [(ALLOWED, ME)], False),
    ],
)
def test_only_the_intended_user_and_the_system_accounts_may_reach_a_directory(
    owner: str, entries: Any, private: bool
) -> None:
    """The owner must be the user; each grant the user's, the system's or Administrators'."""
    assert openclaw_arena.acl_is_private(owner, entries, ME) is private


def test_an_unknown_user_makes_nothing_private() -> None:
    """With no way to say who the user is, no directory is proven private."""
    assert openclaw_arena.acl_is_private(ME, [(ALLOWED, ME)], "") is False


# ---------------------------------------------------------------------------------------------
# The real Windows access lists
# ---------------------------------------------------------------------------------------------


def lock_to_user(path: Path) -> None:
    """Remove inherited access and grant the directory to the current user alone."""
    sid = openclaw_arena.windows_current_sid()
    assert sid
    subprocess.run(  # noqa: S603 - fixed system tool on a test directory
        ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],  # noqa: S607
        check=True,
        capture_output=True,
    )


def share_with_users(path: Path) -> None:
    """Grant the built-in Users group read access, as a shared profile would have."""
    subprocess.run(  # noqa: S603 - fixed system tool on a test directory
        ["icacls", str(path), "/grant", f"*{USERS}:(OI)(CI)R"],  # noqa: S607
        check=True,
        capture_output=True,
    )


def profile_on_disk(root: Path) -> tuple[Path, Path, Path]:
    """Create a profile directory with a configuration and a state directory; return the three."""
    (root / "state").mkdir(parents=True)
    config = root / "openclaw.json"
    config.write_text("{}", encoding="utf-8")
    return root, config, root / "state"


@pytest.mark.skipif(not WINDOWS, reason="the access lists are Windows'")
def test_a_directory_locked_to_the_user_is_private(tmp_path: Path) -> None:
    """Private: the profile and its state, locked to the current user, are accepted."""
    root, config, state = profile_on_disk(tmp_path / "private")
    lock_to_user(root)
    assert openclaw_arena.windows_private(root) is True
    assert openclaw_arena.windows_private(state) is True
    assert openclaw_arena.profile_context_valid(root, config, state) is True


@pytest.mark.skipif(not WINDOWS, reason="the access lists are Windows'")
def test_a_directory_shared_with_another_group_is_not_private(tmp_path: Path) -> None:
    """Shared: one extra grant to the Users group, and the profile is refused."""
    root, config, state = profile_on_disk(tmp_path / "shared")
    lock_to_user(root)
    share_with_users(root)
    assert openclaw_arena.windows_private(root) is False
    assert openclaw_arena.profile_context_valid(root, config, state) is False


@pytest.mark.skipif(not WINDOWS, reason="the access lists are Windows'")
def test_a_state_directory_shared_below_a_private_profile_is_refused(tmp_path: Path) -> None:
    """The state holds the authentication store, so it is judged on its own."""
    root, config, state = profile_on_disk(tmp_path / "mixed")
    lock_to_user(root)
    share_with_users(state)
    assert openclaw_arena.windows_private(root) is True
    assert openclaw_arena.profile_context_valid(root, config, state) is False


@pytest.mark.skipif(not WINDOWS, reason="the access lists are Windows'")
def test_a_directory_whose_access_cannot_be_read_is_not_private(tmp_path: Path) -> None:
    """Not provable: a path that does not exist, or whose list cannot be read, is refused."""
    assert openclaw_arena.windows_private(tmp_path / "missing") is False


@pytest.mark.skipif(not WINDOWS, reason="the access lists are Windows'")
def test_a_profile_the_access_lists_cannot_prove_is_refused_before_anything_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the reader fails, the verdict is refusal, never acceptance."""
    root, config, state = profile_on_disk(tmp_path / "unreadable")
    lock_to_user(root)
    monkeypatch.setattr(openclaw_arena, "windows_security", lambda path: None)
    assert openclaw_arena.profile_context_valid(root, config, state) is False


@pytest.mark.skipif(not WINDOWS, reason="the access lists are Windows'")
def test_the_access_check_opens_nothing_inside_the_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A canary file in the state is never opened, read or listed by the privacy check."""
    root, _config, state = profile_on_disk(tmp_path / "canary")
    (state / "auth.sqlite").write_bytes(b"synthetic canary")
    lock_to_user(root)
    opened: list[str] = []
    real = Path.open

    def watched(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", watched)
    monkeypatch.setattr(Path, "iterdir", lambda self: pytest.fail("a directory was listed"))
    assert openclaw_arena.windows_private(state) is True
    assert not [name for name in opened if name.endswith("auth.sqlite")]


# ---------------------------------------------------------------------------------------------
# The Windows job object
# ---------------------------------------------------------------------------------------------


class FakeKernel:
    """A kernel that records its calls and answers as told; handles are small integers."""

    def __init__(self, fail: str = "") -> None:
        """Say which call fails, by name."""
        self.fail, self.calls = fail, []

    def _call(self, name: str, result: Any) -> Any:
        self.calls.append(name)
        return 0 if self.fail == name else result

    def CreateJobObjectW(self, *args: Any) -> int:  # noqa: N802
        """Answer like the Windows call of this name."""
        return self._call("create", 11)

    def SetInformationJobObject(self, *args: Any) -> int:  # noqa: N802
        """Answer like the Windows call of this name."""
        return self._call("limit", 1)

    def AssignProcessToJobObject(self, *args: Any) -> int:  # noqa: N802
        """Answer like the Windows call of this name."""
        return self._call("assign", 1)

    def TerminateJobObject(self, *args: Any) -> int:  # noqa: N802
        """Answer like the Windows call of this name."""
        return self._call("terminate", 1)

    def CloseHandle(self, *args: Any) -> int:  # noqa: N802
        """Answer like the Windows call of this name."""
        return self._call("close", 1)

    def NtResumeProcess(self, *args: Any) -> int:  # noqa: N802
        """Answer like the Windows call of this name."""
        self.calls.append("resume")
        return 1 if self.fail == "resume" else 0


@pytest.mark.skipif(not WINDOWS, reason="job objects are Windows'")
@pytest.mark.parametrize("fail", ["create", "limit"])
def test_a_job_that_cannot_be_made_or_limited_refuses(fail: str) -> None:
    """Every Windows answer is checked: a failed create or limit raises, and no job is trusted."""
    kernel = FakeKernel(fail)
    with pytest.raises(OSError, match="job"):
        openclaw_arena.WindowsJob(kernel=kernel, ntdll=kernel)


@pytest.mark.skipif(not WINDOWS, reason="job objects are Windows'")
@pytest.mark.parametrize("fail", ["assign", "resume"])
def test_a_process_that_cannot_join_its_job_is_ended_and_the_start_refuses(fail: str) -> None:
    """Assign or resume fails: the suspended process is killed, nothing runs, the start raises."""
    kernel = FakeKernel(fail)
    job = openclaw_arena.WindowsJob(kernel=kernel, ntdll=kernel)
    started: list[subprocess.Popen[Any]] = []
    real = subprocess.Popen

    def watched(*args: Any, **kwargs: Any) -> Any:
        started.append(real(*args, **kwargs))
        return started[-1]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(openclaw_arena.subprocess, "Popen", watched)
        with pytest.raises(OSError, match="job"):
            openclaw_arena.start_in_job([sys.executable, "-c", "import time; time.sleep(60)"], job)
    assert len(started) == 1
    started[0].wait(timeout=10)
    assert started[0].poll() is not None, "the process of a failed start is still alive"


@pytest.mark.skipif(not WINDOWS, reason="job objects are Windows'")
def test_a_process_starts_suspended_and_joins_its_job_before_it_runs() -> None:
    """The order is create, limit, assign, resume; the process is suspended until the resume."""
    kernel = FakeKernel()
    job = openclaw_arena.WindowsJob(kernel=kernel, ntdll=kernel)
    process = openclaw_arena.start_in_job([sys.executable, "-c", "print(1)"], job)
    try:
        assert kernel.calls == ["create", "limit", "assign", "resume"]
        time.sleep(1.0)
        assert process.poll() is None, "the process ran before it was resumed"
    finally:
        process.kill()
        process.wait(timeout=10)


CHILDREN = """\
import socket, subprocess, sys, time
port = int(sys.argv[1])
subprocess.Popen([sys.executable, "-c",
    "import socket, sys, time\\n"
    "s = socket.create_connection(('127.0.0.1', int(sys.argv[1])))\\n"
    "time.sleep(60)\\n", str(port)])
s = socket.create_connection(("127.0.0.1", port))
time.sleep(60)
"""


@pytest.mark.skipif(not WINDOWS, reason="job objects are Windows'")
def test_ending_a_run_on_windows_ends_the_child_and_the_grandchild(tmp_path: Path) -> None:
    """The real thing: both sockets of the tree close when the run is ended, none is left open."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted: list[socket.socket] = []

    def accept() -> None:
        with contextlib.suppress(OSError):
            while True:
                accepted.append(server.accept()[0])

    threading.Thread(target=accept, daemon=True).start()
    (tmp_path / "cwd").mkdir()
    script = tmp_path / "children.py"
    script.write_text(CHILDREN, encoding="utf-8")
    run = openclaw_arena.Run(
        [sys.executable, str(script), str(server.getsockname()[1])], dict(os.environ), tmp_path
    )
    try:
        deadline = time.monotonic() + 30
        while len(accepted) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(accepted) == 2, "the child and the grandchild did not both start"
        run.end()
        for connection in accepted:
            connection.settimeout(15)
            with contextlib.suppress(ConnectionResetError):
                assert connection.recv(1) == b""
    finally:
        for connection in accepted:
            connection.close()
        server.close()


@pytest.mark.skipif(not WINDOWS, reason="job objects are Windows'")
def test_a_failed_job_makes_the_openclaw_preflight_refuse_before_any_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The preflight runs the runtime in the same job: if that fails, the driver refuses."""
    server = FakeChatModel("move")

    def broken(self: Any, process: Any) -> None:
        raise OSError("The process could not be put in its job.")

    try:
        handle = stand_in_handle(tmp_path, server)
        monkeypatch.setattr(openclaw_arena.WindowsJob, "adopt", broken)
        with pytest.raises(arena_driver.DriverRefusedError):
            arena_driver.check_preflight(arena_driver_openclaw.OpenClawArenaDriver(), handle)
    finally:
        server.close()


# ---------------------------------------------------------------------------------------------
# Mutation proofs: weaken one condition, require the proof to notice
# ---------------------------------------------------------------------------------------------


def mutant_of(folder: Path, original: str, replacement: str) -> Any:
    """Load a copy of the OpenClaw module with one line weakened, beside the copy it loads."""
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(arena_match.__file__, folder / "arena_match.py")
    return load_mutant(folder, openclaw_arena, original, replacement)


def test_an_access_decision_that_ignores_who_was_granted_is_noticed(tmp_path: Path) -> None:
    """Mutation: with the grantee no longer checked, a profile shared with Users is private."""
    mutant = mutant_of(tmp_path / "mutant", "or sid not in allowed", "or False")
    try:
        shared = [(ALLOWED, ME), (ALLOWED, USERS)]
        assert openclaw_arena.acl_is_private(ME, shared, ME) is False
        assert mutant.acl_is_private(ME, shared, ME) is True
    finally:
        sys.modules.pop(mutant.__name__, None)


@pytest.mark.skipif(not WINDOWS, reason="the access lists are Windows'")
def test_a_profile_check_without_the_windows_privacy_gate_is_noticed(
    tmp_path: Path,
) -> None:
    """Mutation: without the gate, a profile shared with another group is accepted."""
    mutant = mutant_of(
        tmp_path / "mutant",
        'elif sys.platform == "win32" and not (windows_private(root) and windows_private(state)):',
        "elif False:",
    )
    try:
        root, config, state = profile_on_disk(tmp_path / "shared")
        lock_to_user(root)
        share_with_users(root)
        assert openclaw_arena.profile_context_valid(root, config, state) is False
        assert mutant.profile_context_valid(root, config, state) is True
    finally:
        sys.modules.pop(mutant.__name__, None)


@pytest.mark.skipif(not WINDOWS, reason="job objects are Windows'")
def test_a_process_that_never_joins_its_job_is_noticed(tmp_path: Path) -> None:
    """Mutation: with the assignment skipped, ending the run leaves the grandchild running."""
    mutant = mutant_of(
        tmp_path / "mutant",
        "if not self._kernel.AssignProcessToJobObject(self.handle, handle):",
        "if False:",
    )
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted: list[socket.socket] = []

    def accept() -> None:
        with contextlib.suppress(OSError):
            while True:
                accepted.append(server.accept()[0])

    threading.Thread(target=accept, daemon=True).start()
    (tmp_path / "cwd").mkdir()
    script = tmp_path / "children.py"
    script.write_text(CHILDREN.replace("time.sleep(60)", "time.sleep(20)"), encoding="utf-8")
    try:
        run = mutant.Run(
            [sys.executable, str(script), str(server.getsockname()[1])], dict(os.environ), tmp_path
        )
        deadline = time.monotonic() + 30
        while len(accepted) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(accepted) == 2
        run.end()
        survivors = 0
        for connection in accepted:
            connection.settimeout(3)
            try:
                if connection.recv(1) != b"":
                    survivors += 1
            except TimeoutError:
                survivors += 1
            except ConnectionResetError:
                pass
        assert survivors >= 1, "an unassigned process was ended with its run"
    finally:
        for connection in accepted:
            connection.close()
        server.close()
        sys.modules.pop(mutant.__name__, None)


# ---------------------------------------------------------------------------------------------
# The configuration file is pinned by what can be said about it without opening it
# ---------------------------------------------------------------------------------------------

FIELDS = {"path", "device", "inode", "size", "modified_ns", "changed_ns", "kind"}
CONTENT = '{"synthetic": "config-content-that-must-never-be-read"}'


def pinned_profile(root: Path) -> tuple[Path, Path, Path]:
    """Create a private profile with a configuration file and a state directory."""
    root_, config, state = profile_on_disk(root)
    config.write_text(CONTENT, encoding="utf-8")
    make_private(root_)
    return root_, config, state


def take(root: Path, config: Path, state: Path) -> dict[str, str] | None:
    """Take the fingerprint with the real function."""
    return openclaw_arena.config_fingerprint(root, config, state)


def test_the_fingerprint_names_the_file_by_metadata_alone(tmp_path: Path) -> None:
    """Path, identity, size and times; nothing of the content, and nothing derived from it."""
    root, config, state = pinned_profile(tmp_path / "profile")
    fingerprint = take(root, config, state)
    assert fingerprint is not None and set(fingerprint) == FIELDS
    assert fingerprint["kind"] == "file" and fingerprint["size"] == str(len(CONTENT))
    assert all(isinstance(value, str) for value in fingerprint.values())
    assert not [value for value in fingerprint.values() if "config-content" in value]
    assert take(root, config, state) == fingerprint


def test_a_changed_file_has_another_fingerprint(tmp_path: Path) -> None:
    """Any write is seen: the size and the times move."""
    root, config, state = pinned_profile(tmp_path / "profile")
    before = take(root, config, state)
    with config.open("ab") as handle:
        handle.write(b" ")
    assert take(root, config, state) != before


def test_a_replacement_of_the_same_size_has_another_identity(tmp_path: Path) -> None:
    """Same size, same modification time, another file: the identity differs."""
    root, config, state = pinned_profile(tmp_path / "profile")
    before = take(root, config, state)
    assert before is not None
    stamp = config.stat()
    other = config.with_name("other.json")
    other.write_text(CONTENT.replace("synthetic", "syntheti_"), encoding="utf-8")
    os.utime(other, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    os.replace(other, config)
    after = take(root, config, state)
    assert after is not None and after != before
    assert after["size"] == before["size"] and after["modified_ns"] == before["modified_ns"]
    assert (after["device"], after["inode"]) != (before["device"], before["inode"])


def test_a_missing_file_has_no_fingerprint(tmp_path: Path) -> None:
    """Nothing to pin, nothing proven."""
    root, config, state = pinned_profile(tmp_path / "profile")
    config.unlink()
    assert take(root, config, state) is None


@pytest.mark.skipif(WINDOWS, reason="a symbolic link needs a privilege on Windows")
def test_a_symbolic_link_in_place_of_the_file_has_no_fingerprint(tmp_path: Path) -> None:
    """The file turned into a link."""
    root, config, state = pinned_profile(tmp_path / "profile")
    real = root / "real.json"
    config.rename(real)
    config.symlink_to(real)
    assert take(root, config, state) is None


@pytest.mark.skipif(not WINDOWS, reason="junctions are Windows'")
def test_a_junction_in_the_path_has_no_fingerprint(tmp_path: Path) -> None:
    """A directory on the way down became a junction."""
    root = tmp_path / "profile"
    (root / "runtime").mkdir(parents=True)
    (root / "state").mkdir()
    config = root / "runtime" / "openclaw.json"
    config.write_text(CONTENT, encoding="utf-8")
    make_private(root)
    state = root / "state"
    assert take(root, config, state) is not None
    (root / "runtime").rename(root / "real-runtime")
    subprocess.run(  # noqa: S603 - a fixed system command on a test directory
        ["cmd", "/c", "mklink", "/J", str(root / "runtime"), str(root / "real-runtime")],  # noqa: S607
        check=True,
        capture_output=True,
    )
    assert take(root, config, state) is None


def test_a_file_whose_state_cannot_be_read_has_no_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the status cannot be read, the identity is not provable."""
    root, config, state = pinned_profile(tmp_path / "profile")
    real = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) == str(config):
            raise OSError("synthetic")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    assert take(root, config, state) is None


def test_a_file_without_an_identity_has_no_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file system that gives no file number cannot show that the file is the same."""
    root, config, state = pinned_profile(tmp_path / "profile")
    real = os.lstat

    def lstat(path: Any, *args: Any, **kwargs: Any) -> Any:
        info = real(path, *args, **kwargs)
        if str(path) != str(config):
            return info
        values = list(info)
        values[1] = 0  # st_ino
        return os.stat_result(values)

    monkeypatch.setattr(os, "lstat", lstat)
    assert take(root, config, state) is None


def test_a_profile_that_is_no_longer_private_has_no_fingerprint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The privacy boundary that was already checked is part of what is pinned."""
    root, config, state = pinned_profile(tmp_path / "profile")
    monkeypatch.setattr(openclaw_arena, "profile_context_valid", lambda *args: False)
    assert take(root, config, state) is None


def forbid_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any opening of a file, any read of a path's content and any digest an error."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("file content was touched")

    for owner, names in (
        (builtins, ("open",)),
        (io, ("open",)),
        (os, ("open",)),
        (Path, ("open", "read_bytes", "read_text")),
        (hashlib, ("sha256", "sha1", "md5", "blake2b", "new")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, refuse)


def no_content_oracle(module: Any, tmp_path: Path) -> None:
    """Take a fingerprint with the module while nothing may be opened, read or digested."""
    root, config, state = pinned_profile(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        forbid_content(patch)
        assert module.config_fingerprint(root, config, state) is not None


def test_the_fingerprint_opens_reads_and_digests_nothing(tmp_path: Path) -> None:
    """The oracle holds for the real module."""
    no_content_oracle(openclaw_arena, tmp_path / "real")


def test_a_fingerprint_that_adds_a_content_digest_is_noticed(tmp_path: Path) -> None:
    """Mutation: with a digest of the file in it, the same oracle refuses."""
    mutant = mutant_of(
        tmp_path / "mutant",
        '"kind": "file",  # metadata',
        '"kind": __import__("hashlib").sha256(Path(config).read_bytes()).hexdigest(),  # metadata',
    )
    try:
        expect_guard(
            lambda module: no_content_oracle(module, tmp_path / module.__name__),
            openclaw_arena,
            mutant,
        )
    finally:
        sys.modules.pop(mutant.__name__, None)


# ---------------------------------------------------------------------------------------------
# The relay forwards nothing once the file has changed
# ---------------------------------------------------------------------------------------------


def relayed(verify: Any, module: Any = openclaw_arena) -> tuple[list[dict[str, Any]], Any, Any]:
    """Send one move through the worker's relay with this check; return what went upstream."""
    state = module.Decision()
    incoming: queue.Queue[str] = queue.Queue()
    incoming.put(json.dumps({"result": {"status": "active"}}))
    sent: list[dict[str, Any]] = []
    request = {"op": "game_move", "arguments": {"move": "e2e4"}}
    reply = module.answer(request, state, incoming, sent.append, verify)
    return sent, reply, state


def test_the_relay_forwards_a_move_while_the_check_holds() -> None:
    """Control: with the check passing, the move goes upstream."""
    sent, reply, _ = relayed(lambda: True)
    assert len(sent) == 1 and not reply.get("error")


@pytest.mark.parametrize("how", ["false", "raises"])
def test_the_relay_forwards_nothing_when_the_check_fails_or_cannot_be_made(how: str) -> None:
    """A changed file, or a check that errors, sends nothing and ends the decision."""

    def verify() -> bool:
        if how == "raises":
            raise OSError("synthetic")
        return False

    sent, reply, state = relayed(verify)
    assert sent == [] and reply.get("error")
    assert state.changed.is_set() and not state.accepted.is_set()


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("if verify is not None and not verify():  # verify", "if False:  # verify"),
        ("except Exception:  # verify", "except KeyError:  # verify"),
    ],
    ids=["check-removed", "error-path-forwards"],
)
def test_a_relay_without_its_check_is_noticed(
    tmp_path: Path, original: str, replacement: str
) -> None:
    """Mutation: with the check gone, or an error let through, the move goes upstream."""
    mutant = mutant_of(tmp_path / "mutant", original, replacement)
    try:

        def oracle(module: Any) -> None:
            def verify() -> bool:
                raise OSError("synthetic")

            sent, _, _ = relayed(verify, module)
            assert sent == []

        expect_guard(oracle, openclaw_arena, mutant)
    finally:
        sys.modules.pop(mutant.__name__, None)


# ---------------------------------------------------------------------------------------------
# The supervisor checks the pin before it forwards
# ---------------------------------------------------------------------------------------------


def serve_a_move(
    monkeypatch: pytest.MonkeyPatch, module: Any, hook: Any, *, pin: str | None = "pin"
) -> tuple[list[str], Any]:
    """Serve one scripted move through the supervisor with this driver hook; list the forwards."""
    runner, owned = supervisor(module)
    runner.driver = SimpleNamespace() if hook is None else SimpleNamespace(still_pinned=hook)
    runner.handle, runner.pin = object(), pin
    runner.proven, runner.refused = "generation:abc", None
    runner._write_status = lambda: None
    forwarded: list[str] = []

    def game(command: dict[str, Any], **kwargs: object) -> dict[str, Any]:
        forwarded.append(command["operation"])
        return {"status": "active", "game_version": "connect-four-1-solo"}

    monkeypatch.setattr(module.bridge, "_run_game_command", game)
    lines = [
        {"diagnostic": "model_call_started", "duration_ms": 0},
        {"operation": "game_move", "column": 3},
        {"finished": True},
    ]
    text = "".join(json.dumps(line) + "\n" for line in lines)
    child = SimpleNamespace(stdout=io.StringIO(text), stdin=io.StringIO())
    module.ArenaRunner._serve(runner, child, owned)
    return forwarded, runner


def test_a_move_is_forwarded_while_the_driver_says_the_pin_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control, and the two cases that offer no check: no hook, or a driver with no pin."""
    for hook, pin in (
        (lambda handle, pinned: True, "pin"),
        (None, "pin"),
        (lambda h, p: False, None),
    ):
        forwarded, runner = serve_a_move(monkeypatch, arena_runner, hook, pin=pin)
        assert forwarded == ["game_move"] and runner.proven == "generation:abc"


@pytest.mark.parametrize("how", ["false", "raises"])
def test_a_move_is_not_forwarded_when_the_pin_fails_and_the_proof_is_dropped(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """A changed state, or a check that cannot be made, forwards nothing and voids the proof."""

    def hook(handle: Any, pin: str) -> bool:
        if how == "raises":
            raise OSError("synthetic")
        return False

    forwarded, runner = serve_a_move(monkeypatch, arena_runner, hook)
    assert forwarded == []
    assert runner.proven is None, "claims continue on a proof the file no longer matches"


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("if not self._pinned():", "if False:"),
        ("return False  # unverifiable", "return True  # unverifiable"),
    ],
    ids=["check-removed", "unverifiable-accepted"],
)
def test_a_supervisor_without_its_pin_check_is_noticed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, original: str, replacement: str
) -> None:
    """Mutation: with the check gone, or an unverifiable state accepted, the move is forwarded."""
    mutant = load_mutant(tmp_path, arena_runner, original, replacement)
    try:

        def oracle(module: Any) -> None:
            def hook(handle: Any, pin: str) -> bool:
                raise OSError("synthetic")

            with pytest.MonkeyPatch.context() as patch:
                forwarded, _ = serve_a_move(patch, module, hook)
            assert forwarded == []

        expect_guard(oracle, arena_runner, mutant)
    finally:
        sys.modules.pop(mutant.__name__, None)


# ---------------------------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------------------------


def test_the_driver_follows_the_file_and_the_generation_follows_the_identity(
    tmp_path: Path,
) -> None:
    """`still_pinned` is true until the file changes; the generation changes with the identity."""
    server = FakeChatModel("move")
    try:
        handle = stand_in_handle(tmp_path, server)
        driver = arena_driver_openclaw.OpenClawArenaDriver()
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        pin = driver.launch(handle, scratch).pin
        assert pin and driver.still_pinned(handle, pin) is True
        before = driver.generation(handle)
        assert before is not None
        config = Path(str(handle.config))
        stamp = config.stat()
        other = config.with_name("other.json")
        other.write_bytes(config.read_bytes())
        os.utime(other, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        os.replace(other, config)
        assert driver.still_pinned(handle, pin) is False
        assert driver.generation(handle) != before
        assert driver.still_pinned(handle, "not a pin") is False
    finally:
        server.close()


def test_a_driver_that_cannot_fingerprint_offers_no_generation_and_refuses_a_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing provable: no generation token, and the preflight refuses."""
    server = FakeChatModel("move")
    try:
        handle = stand_in_handle(tmp_path, server)
        monkeypatch.setattr(openclaw_arena, "config_fingerprint", lambda *args: None)
        driver = arena_driver_openclaw.OpenClawArenaDriver()
        assert driver.generation(handle) is None
        with pytest.raises(arena_driver.DriverRefusedError):
            driver.preflight(handle)
    finally:
        server.close()
