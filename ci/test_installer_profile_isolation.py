"""Issue #195: an install or an update of one profile leaves the other profile's key alone.

These cases call the installer and the profile updater. They use synthetic marker bytes, never a
real key, and they open no listening socket. A device install stays outside this file.
"""

from __future__ import annotations

import socket
from io import StringIO
from pathlib import Path

import pytest

from agentnexus_sdk.connector import ConnectorError, Environment, Paths, State, run_profile_remove
from agentnexus_sdk.profiles import ProfileRecord
from agentnexus_sdk.release import Artifact, ReleaseManifest
from agentnexus_sdk.runtimes import ADAPTERS
from agentnexus_sdk.updater import UpdateError, install_release, update_profiles

ALPHA = b"alpha-profile-marker-bytes-195"
BETA = b"beta-profile-marker-bytes-195"


def _profile(install_root: Path, name: str, marker: bytes) -> Paths:
    paths = Paths.for_profile(install_root, name)
    paths.key_directory.mkdir(parents=True)
    paths.private_key.write_bytes(marker)
    State(
        agent_id=f"agent-{name}",
        key_id=f"key-{name}",
        handle=name,
        private_key_path=str(paths.private_key),
    ).save(paths.state_file)
    ProfileRecord(
        name=name,
        endpoints={"agent_api_url": "https://agent-api.example.invalid"},
    ).save(paths.profile_record)
    return paths


def _environment() -> tuple[Environment, StringIO, StringIO]:
    stdout, stderr = StringIO(), StringIO()
    environment = Environment(
        stdout=stdout,
        stderr=stderr,
        which=lambda _name: None,
        system="Linux",
    )
    return environment, stdout, stderr


def _markers_absent(text: str) -> None:
    assert ALPHA.decode("ascii") not in text
    assert BETA.decode("ascii") not in text


@pytest.fixture
def listening_is_refused(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record any attempt to accept a connection. An update must not make one."""
    attempts: list[str] = []
    real_socket = socket.socket

    class GuardedSocket(real_socket):
        def bind(self, address: object) -> None:  # type: ignore[override]
            attempts.append("bind")
            super().bind(address)

        def listen(self, backlog: int = 128) -> None:  # type: ignore[override]
            attempts.append("listen")
            super().listen(backlog)

    monkeypatch.setattr(socket, "socket", GuardedSocket)
    return attempts


def test_updating_one_profile_leaves_the_other_profiles_key_byte_for_byte(
    tmp_path: Path, listening_is_refused: list[str]
) -> None:
    """A failed activation of alpha does not read or rewrite beta's private-key file."""
    alpha = _profile(tmp_path, "alpha", ALPHA)
    beta = _profile(tmp_path, "beta", BETA)
    before = beta.private_key.read_bytes()
    environment, stdout, stderr = _environment()

    outcomes = update_profiles(
        install_root=tmp_path,
        version="9.9.9",
        profiles=["alpha"],
        environment=environment,
    )

    assert [outcome.status for outcome in outcomes] == ["failed"]
    assert "Nothing was changed." in outcomes[0].detail
    assert beta.private_key.read_bytes() == before == BETA
    assert alpha.private_key.read_bytes() == ALPHA
    _markers_absent(stdout.getvalue() + stderr.getvalue() + outcomes[0].detail)
    assert listening_is_refused == []


def test_a_usable_version_with_no_runtime_skips_without_touching_either_key(
    tmp_path: Path, listening_is_refused: list[str]
) -> None:
    """Pointing alpha at an installed version still leaves both key files as they were."""
    alpha = _profile(tmp_path, "alpha", ALPHA)
    beta = _profile(tmp_path, "beta", BETA)
    executable = tmp_path / "connector" / "0.13.1" / "venv" / "bin" / "agentnexus-agent-mcp"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    environment, stdout, stderr = _environment()

    outcomes = update_profiles(
        install_root=tmp_path,
        version="0.13.1",
        profiles=["alpha"],
        environment=environment,
    )

    assert outcomes[0].status == "skipped"
    assert alpha.private_key.read_bytes() == ALPHA
    assert beta.private_key.read_bytes() == BETA
    _markers_absent(stdout.getvalue() + stderr.getvalue() + outcomes[0].detail)
    assert listening_is_refused == []


def test_a_completed_update_rewrites_only_the_profile_it_was_asked_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listening_is_refused: list[str]
) -> None:
    """A stand-in runtime accepts alpha's new registration and is never handed beta's key."""
    alpha = _profile(tmp_path, "alpha", ALPHA)
    beta = _profile(tmp_path, "beta", BETA)
    State(
        agent_id="agent-alpha",
        key_id="key-alpha",
        handle="alpha",
        private_key_path=str(alpha.private_key),
        runtimes=["hermes"],
    ).save(alpha.state_file)
    executable = tmp_path / "connector" / "0.13.1" / "venv" / "bin" / "agentnexus-agent-mcp"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    seen: list[str] = []

    class _StandIn:
        display_name = "stand-in"

        def detect(self) -> object:
            return type("Detected", (), {"installed": True})()

        def existing_entry(self) -> None:
            return None

        def configure(self, spec: object, *, backup_directory: Path) -> object:
            seen.append(str(getattr(spec, "command", "")))
            text = repr(spec)
            assert ALPHA.decode("ascii") not in text
            assert BETA.decode("ascii") not in text
            assert str(beta.private_key) not in text
            return type("Outcome", (), {"changed": True, "backup": None})()

    monkeypatch.setitem(
        ADAPTERS,
        "hermes",
        lambda **_kwargs: _StandIn(),
    )
    environment, stdout, stderr = _environment()

    outcomes = update_profiles(
        install_root=tmp_path,
        version="0.13.1",
        profiles=["alpha"],
        environment=environment,
    )

    assert outcomes[0].status == "updated"
    assert seen == [str(executable)]
    assert alpha.private_key.read_bytes() == ALPHA
    assert beta.private_key.read_bytes() == BETA
    record = ProfileRecord.load(alpha.profile_record)
    assert record is not None
    assert record.runtime["version"] == "0.13.1"
    assert ProfileRecord.load(beta.profile_record).runtime == {}  # type: ignore[union-attr]
    _markers_absent(stdout.getvalue() + stderr.getvalue() + outcomes[0].detail)
    assert listening_is_refused == []


def test_removing_one_profile_quarantines_its_key_and_leaves_the_other(
    tmp_path: Path, listening_is_refused: list[str]
) -> None:
    """Profile removal moves alpha's key aside and does not open beta's directory."""
    alpha = _profile(tmp_path, "alpha", ALPHA)
    beta = _profile(tmp_path, "beta", BETA)
    beta_before = beta.private_key.read_bytes()
    environment, stdout, stderr = _environment()

    code = run_profile_remove(tmp_path, "alpha", environment, confirm="alpha")

    assert code == 0
    assert not alpha.root.exists()
    quarantined = [path for path in tmp_path.rglob("agent.pem") if path != beta.private_key]
    assert [path.read_bytes() for path in quarantined] == [ALPHA]
    assert beta.private_key.read_bytes() == beta_before == BETA
    assert beta.root.is_dir()
    _markers_absent(stdout.getvalue() + stderr.getvalue())
    assert listening_is_refused == []


def test_a_wrong_confirmation_and_key_destruction_leave_both_profiles(
    tmp_path: Path,
) -> None:
    """A mismatched name and the withdrawn destroy flag change no key file."""
    alpha = _profile(tmp_path, "alpha", ALPHA)
    beta = _profile(tmp_path, "beta", BETA)
    environment, _stdout, _stderr = _environment()

    with pytest.raises(ConnectorError, match="does not match"):
        run_profile_remove(tmp_path, "alpha", environment, confirm="beta")
    with pytest.raises(ConnectorError, match="disabled in this release"):
        run_profile_remove(tmp_path, "alpha", environment, destroy_key=True, confirm="alpha")

    assert alpha.private_key.read_bytes() == ALPHA
    assert beta.private_key.read_bytes() == BETA


def test_a_rejected_download_removes_only_the_attempt_it_started(tmp_path: Path) -> None:
    """A digest mismatch undoes the new version directory and does not open either key."""
    alpha = _profile(tmp_path, "alpha", ALPHA)
    beta = _profile(tmp_path, "beta", BETA)
    artifact = Artifact(
        platform="any",
        filename="connector.whl",
        url="https://example.invalid/connector.whl",
        size=1,
        sha256="0" * 64,
    )
    seen: list[str] = []

    def fetch(url: str, size: int) -> bytes:
        seen.append(url)
        assert size == artifact.size
        return b"not-the-artifact"

    with pytest.raises(UpdateError, match="does not match the manifest"):
        install_release(
            install_root=tmp_path,
            manifest=ReleaseManifest(
                connector_version="9.9.9",
                released_at="2026-10-11T00:00:00Z",
                artifacts=(artifact,),
            ),
            fetch=fetch,
            runner=lambda *_args, **_kwargs: pytest.fail("pip was started"),
            system="Linux",
        )

    assert seen == [artifact.url]
    assert not (tmp_path / "connector" / "9.9.9").exists()
    assert alpha.private_key.read_bytes() == ALPHA
    assert beta.private_key.read_bytes() == BETA


def test_an_unrecognised_directory_is_left_in_place(tmp_path: Path) -> None:
    """Removal is allowed only for this connector's own interrupted install."""
    kept = tmp_path / "connector" / "1.2.3" / "keep"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(BETA)
    _profile(tmp_path, "alpha", ALPHA)

    with pytest.raises(UpdateError, match="does not look like anything"):
        install_release(
            install_root=tmp_path,
            manifest=ReleaseManifest(
                connector_version="1.2.3",
                released_at="2026-10-11T00:00:00Z",
                artifacts=(
                    Artifact(
                        platform="any",
                        filename="connector.whl",
                        url="https://example.invalid/connector.whl",
                        size=1,
                        sha256="0" * 64,
                    ),
                ),
            ),
            fetch=lambda *_args: pytest.fail("a download started"),
            runner=lambda *_args, **_kwargs: pytest.fail("pip was started"),
            system="Linux",
        )

    assert kept.read_bytes() == BETA
