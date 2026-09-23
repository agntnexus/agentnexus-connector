#!/usr/bin/env python
r"""Build a signed Connector release from one commit of this repository, or reproduce one.

This is the only file in the repository that ever holds a private signing key, and it holds it only
in memory, only for as long as it takes to derive the two public coordinates and sign one manifest.
The key is passed as a path. It is never read from this repository or from any other Git work tree,
never printed, and never written anywhere: the output is scanned for it before the build reports
success.

Two commands:

``build``
    Build the release of the commit that is checked out, and write it into ``connector-release/``.
    Run by an operator, by hand, with a key outside every repository. CI never runs it.

``reproduce``
    Rebuild the wheel the committed manifest names, from the source commit recorded for it, with
    no private key at all, and require the result to be byte-identical to the committed wheel.
    Then check the whole committed release tree. CI runs this on every change.

What ``build`` refuses, each before anything is signed:

* a key inside this repository or inside any other Git work tree, or a key that is not P-256;
* a working tree with uncommitted or untracked changes, because it is not the commit it claims;
* an inherited ``SOURCE_DATE_EPOCH`` that is not the committer time of the commit being built;
* package, wheel and manifest versions that disagree, or a version other than the one expected;
* a version that already has a directory in the published release: published bytes never change;
* a public-agent gate that disagrees with ``--public-agent-endpoint-approved``;
* a key other than the one the published loaders already carry: rotation is not a side effect;
* two builds of the same commit that are not byte-identical;
* an updater that is unstamped or stamped with another key;
* a release tree that is not the previous one plus exactly one wheel, with a manifest on the
  loaders' origin, a signature that verifies, and loaders that are their source plus two lines;
* any private key material anywhere in the tree, including inside the wheel.

Reproducibility
---------------

The wheel is built from ``git archive`` of the commit, not from the working tree, so line-ending
conversion on a Windows checkout cannot reach it. Every timestamp is the commit's committer time,
the build backend is pinned, and the zip is normalised afterwards so that the file modes and the
"made by" system the platform wrote do not depend on which operating system built it. Anybody
holding the commit can therefore rebuild the same bytes, which is what ``reproduce`` does.

The manifest's ``released_at`` and the ECDSA signature are deliberately not reproducible: the first
records when the release was made and is a freshness floor against replay, the second is randomised
by construction. Both are verified rather than compared.

Usage::

    python scripts/build_release.py build --signing-key <path outside every repository> \\
        --expect-version 0.6.3 --public-agent-endpoint-approved
    python scripts/build_release.py reproduce
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]

RELEASE_DIRECTORY: Final = "connector-release"
INSTALLERS_DIRECTORY: Final = "installers"
MANIFEST_NAME: Final = "connector-release.json"
SIGNATURE_NAME: Final = f"{MANIFEST_NAME}.sig"
SCHEMA_VERSION: Final = 1

#: Where a release's source commit and epoch are recorded, so ``reproduce`` can find them.
STATE_FILE: Final = Path("docs") / "releases" / "connector-release-state.json"

#: Releases built in the historical repository before this builder existed. Their wheels are
#: published and immutable, and nothing here can rebuild them. Any later version must be
#: reproducible, and ``reproduce`` refuses one that has no recorded source.
LEGACY_RELEASES: Final = frozenset(
    {"0.1.0", "0.2.0", "0.2.1", "0.3.0", "0.4.0", "0.4.1", "0.4.2", "0.5.0", "0.6.0", "0.6.1"}
)

#: What a change to one of these paths changes in the wheel or the loaders. A release reproduced
#: from an earlier commit is only the same release if none of them moved in between.
RELEASE_INPUTS: Final = ("src", "pyproject.toml", "README.md", "LICENSE", INSTALLERS_DIRECTORY)

#: The build backend, pinned. `pyproject.toml` asks for `setuptools>=80`, which is right for
#: somebody installing from source and wrong for a digest other people have to reproduce: a newer
#: backend is free to write a different wheel from the same source. 84.0.0 built 0.6.1.
BUILD_CONSTRAINTS: Final = "setuptools==84.0.0\n"

EPOCH_VARIABLE: Final = "SOURCE_DATE_EPOCH"

LOADERS: Final = ("connect.ps1", "connect.sh", "remove.ps1")
PLACEHOLDER_X: Final = "REPLACE_RELEASE_PUBLIC_KEY_X"
PLACEHOLDER_Y: Final = "REPLACE_RELEASE_PUBLIC_KEY_Y"
UPDATER: Final = Path("src") / "agentnexus_sdk" / "updater.py"
UPDATER_IN_WHEEL: Final = "agentnexus_sdk/updater.py"

#: A stamped coordinate in either loader dialect: `RELEASE_PUBLIC_KEY_X="…"` or
#: `$ReleasePublicKeyX = '…'`. The same expression `ci/verify_release.py` uses.
COORDINATE: Final = re.compile(
    r"(?:RELEASE_PUBLIC_KEY_|ReleasePublicKey)(?P<axis>[XY])\b[^=]*=\s*"
    r"['\"]?(?P<value>[0-9a-fA-F]{64})['\"]?"
)
#: The origin the POSIX loader accepts. Read from the loader, never passed in: an artifact URL on
#: any other origin is refused by every installer, so a release naming one is broken on arrival.
ORIGIN: Final = re.compile(
    r"ORIGIN=\"\$\{AGENTNEXUS_ORIGIN:-(?P<origin>https://[A-Za-z0-9.:-]+)\}\""
)
PRIVATE_ARMOUR: Final = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")

#: Normalised zip attributes: a regular file, `rw-r--r--`, made by Unix.
NORMAL_MODE: Final = (stat.S_IFREG | 0o644) << 16
UNIX: Final = 3


class ReleaseBuildError(RuntimeError):
    """Raised when a release cannot be built or reproduced safely."""


# --- git ---------------------------------------------------------------------------------------


def git(repository: Path, *arguments: str) -> str:
    """Run git in `repository` and return its output, refusing on any failure."""
    completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell.
        ["git", "-C", str(repository), *arguments],  # noqa: S607 - git from PATH.
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        message = f"git {' '.join(arguments)} failed: {completed.stderr.strip()[-500:]}"
        raise ReleaseBuildError(message)
    return completed.stdout.strip()


def require_clean_tree(repository: Path) -> None:
    """Refuse a working tree that is not exactly the commit it is on."""
    if git(repository, "status", "--porcelain", "--untracked-files=all"):
        message = (
            f"{repository} has uncommitted or untracked changes, so it is not the commit it would "
            "claim to be -- an untracked file inside the package reaches the wheel like any other. "
            "Commit or remove them first."
        )
        raise ReleaseBuildError(message)


def commit_epoch(repository: Path, commit: str) -> int:
    """Return the committer time of `commit`, in whole seconds since the Unix epoch."""
    return int(git(repository, "log", "-1", "--format=%ct", commit))


def resolve_epoch(repository: Path, commit: str, *, environ: Mapping[str, str]) -> int:
    """Return the committer time of `commit`, refusing an inherited epoch that is anything else.

    An inherited variable is not overridden silently: somebody exported it and meant something by
    it, and a build that quietly used a different value would report a digest its operator cannot
    explain.
    """
    epoch = commit_epoch(repository, commit)
    inherited = environ.get(EPOCH_VARIABLE, "").strip()
    if inherited and inherited != str(epoch):
        message = (
            f"{EPOCH_VARIABLE} is set to {inherited} in the environment, but the committer time of "
            f"{commit[:12]} is {epoch}. A release is pinned to its commit's time and nothing else, "
            "or nobody holding the commit can rebuild it. Unset the variable."
        )
        raise ReleaseBuildError(message)
    return epoch


def export_commit(repository: Path, commit: str, destination: Path) -> None:
    """Write the committed bytes of `commit` into `destination`, without line-ending conversion."""
    arguments = ["-C", str(repository), "-c", "core.autocrlf=false", "archive", "--format=tar"]
    archive = subprocess.run(  # noqa: S603 - fixed argument list, no shell.
        ["git", *arguments, commit],  # noqa: S607 - git from PATH.
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        message = f"git archive {commit} failed: {archive.stderr.decode(errors='replace')[-500:]}"
        raise ReleaseBuildError(message)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(destination, filter="data")


# --- the key -----------------------------------------------------------------------------------


def refuse_key_location(path: Path, *, repository: Path) -> Path:
    """Return the resolved key path, refusing one that lives inside any Git work tree.

    A key in a work tree is one `git add -A` away from being published. This repository is public,
    so that would be the end of the release key; but a private repository is not a safe place for
    it either, which is why the rule is any work tree rather than this one.
    """
    resolved = path.resolve()
    work_trees = [repository.resolve()]
    work_trees += [parent for parent in resolved.parents if (parent / ".git").exists()]
    for work_tree in work_trees:
        if resolved.is_relative_to(work_tree):
            message = (
                f"The signing key is inside a Git work tree ({work_tree}). A release key must live "
                "outside every repository and must never be committed."
            )
            raise ReleaseBuildError(message)
    return resolved


def load_signing_key(path: Path, *, repository: Path) -> ec.EllipticCurvePrivateKey:
    """Load the P-256 release key from outside every work tree, naming nothing it holds."""
    resolved = refuse_key_location(path, repository=repository)
    try:
        key = serialization.load_pem_private_key(resolved.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as error:
        # The error names neither the file's contents nor anything derived from them.
        message = (
            f"The signing key could not be loaded as an unencrypted PEM key: {type(error).__name__}"
        )
        raise ReleaseBuildError(message) from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        message = "The release signing key must be an ECDSA P-256 private key."
        raise ReleaseBuildError(message)
    return key


def public_coordinates(key: ec.EllipticCurvePrivateKey) -> tuple[str, str]:
    """Return the two hex coordinates of the key's public half, which the loaders embed."""
    numbers = key.public_key().public_numbers()
    return format(numbers.x, "064x"), format(numbers.y, "064x")


def sign(key: ec.EllipticCurvePrivateKey, payload: bytes) -> bytes:
    """Return the raw 64-byte r||s signature the loaders and the updater verify."""
    r, s = utils.decode_dss_signature(key.sign(payload, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def private_markers(key: ec.EllipticCurvePrivateKey) -> list[bytes]:
    """Byte strings whose presence in the output would mean the private key leaked into it."""
    scalar = key.private_numbers().private_value
    hexadecimal = format(scalar, "064x")
    return [
        hexadecimal.encode("ascii"),
        hexadecimal.upper().encode("ascii"),
        str(scalar).encode("ascii"),
        scalar.to_bytes(32, "big"),
    ]


def scan_private_material(tree: Path, markers: list[bytes]) -> list[str]:
    """Return every place under `tree` that carries key armour or one of `markers`.

    Wheels are opened and every entry is scanned decompressed, because a key inside a zip is not
    visible to a byte search over the zip.
    """

    def findings(label: str, body: bytes) -> list[str]:
        """Return what one body of bytes gives away, labelled with where it was found."""
        found = [f"{label}: private key armour"] if PRIVATE_ARMOUR.search(body) else []
        if any(marker in body for marker in markers):
            found.append(f"{label}: the signing key's private scalar")
        return found

    found: list[str] = []
    for path in sorted(tree.rglob("*")):
        if not path.is_file():
            continue
        label = path.relative_to(tree).as_posix()
        body = path.read_bytes()
        found += findings(label, body)
        if path.suffix == ".whl":
            try:
                with zipfile.ZipFile(path) as archive:
                    for name in archive.namelist():
                        found += findings(f"{label}!{name}", archive.read(name))
            except zipfile.BadZipFile:
                found.append(f"{label}: not a readable wheel")
    return found


# --- the source --------------------------------------------------------------------------------


def read_version(source: Path, *, expected: str | None = None) -> str:
    """Return the one version `pyproject.toml` and `version.py` agree on."""
    declared = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    package = declared.get("project", {}).get("version")
    module = re.search(
        r'^__version__\s*(?::\s*Final\s*)?=\s*"(?P<v>[^"]+)"',
        (source / "src" / "agentnexus_sdk" / "version.py").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not isinstance(package, str) or module is None:
        message = "The package version cannot be read from pyproject.toml and version.py."
        raise ReleaseBuildError(message)
    if package != module.group("v"):
        message = (
            f"pyproject.toml says {package} and version.py says {module.group('v')}; they "
            "disagree, "
            "so the wheel's filename and the manifest would name different versions."
        )
        raise ReleaseBuildError(message)
    if expected is not None and package != expected:
        message = f"This commit builds {package}, expected {expected}."
        raise ReleaseBuildError(message)
    return package


def origin_of(installers: Path) -> str:
    """Return the origin the POSIX loader accepts, which every artifact URL must be on."""
    match = ORIGIN.search((installers / "connect.sh").read_text(encoding="utf-8"))
    if match is None:
        message = "installers/connect.sh no longer names the origin it accepts."
        raise ReleaseBuildError(message)
    return match.group("origin").rstrip("/")


def gate_is_open(source: Path) -> bool:
    """Ask the source being released whether it ships a selectable public agent endpoint."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agentnexus_sdk import transport; "
            "print(int(transport.public_agent_api_gate().is_open))",
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, "PYTHONPATH": str(source / "src")},
    )
    if completed.returncode != 0 or completed.stdout.strip() not in {"0", "1"}:
        message = f"The public-agent gate could not be read: {completed.stderr.strip()[-500:]}"
        raise ReleaseBuildError(message)
    return completed.stdout.strip() == "1"


def require_gate_approval(source: Path, *, approved: bool) -> None:
    """Refuse a public-agent gate the build's approval flag disagrees with.

    The gate is a build constant, so building is where it needs an owner's word.
    """
    is_open = gate_is_open(source)
    if is_open and not approved:
        message = (
            "This connector ships a selectable public agent API endpoint, and "
            "--public-agent-endpoint-approved was not given. That is an owner decision, not a "
            "side effect of running a build."
        )
        raise ReleaseBuildError(message)
    if approved and not is_open:
        message = (
            "--public-agent-endpoint-approved was given, but this connector ships with the public "
            "agent endpoint closed. The flag would record an approval for something it cannot do."
        )
        raise ReleaseBuildError(message)


def stamp(text: str, coordinates: tuple[str, str], *, name: str) -> str:
    """Substitute the public coordinates for the two placeholders, each present exactly once."""
    if text.count(PLACEHOLDER_X) != 1 or text.count(PLACEHOLDER_Y) != 1:
        message = f"{name} does not carry each release-key placeholder exactly once."
        raise ReleaseBuildError(message)
    x, y = coordinates
    return text.replace(PLACEHOLDER_X, x).replace(PLACEHOLDER_Y, y)


# --- the wheel ---------------------------------------------------------------------------------


def record_digest(body: bytes) -> str:
    """Return the digest form a wheel's RECORD uses: urlsafe base64 of SHA-256, unpadded."""
    encoded = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def normalise_wheel(path: Path, *, extra: Mapping[str, bytes] | None = None) -> None:
    """Rewrite a wheel so nothing in it depends on the platform that built it.

    Entry order, names and timestamps are kept exactly, and so is every byte of the package. What
    changes is what the build took from the machine rather than from the commit:

    * the file mode the zip writer recorded (Windows reports `rw-rw-rw-`), and the system that made
      the entry (0 on Windows, 3 elsewhere);
    * the line endings of ``METADATA``, which setuptools writes in text mode and therefore with CRLF
      on Windows only -- and with it the one ``RECORD`` line that pins that file, recomputed, so the
      wheel still verifies on installation.

    Every other entry comes from ``git archive`` bytes or from files the backend writes in binary
    mode, and is left alone. `extra` exists for tests that need a wheel nobody built from source.
    """
    with zipfile.ZipFile(path) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    fixed: dict[str, bytes] = {}
    for index, (info, body) in enumerate(entries):
        if info.filename.endswith(".dist-info/METADATA") and b"\r\n" in body:
            body = body.replace(b"\r\n", b"\n")
            fixed[info.filename] = body
            entries[index] = (info, body)
    if fixed:
        for index, (info, body) in enumerate(entries):
            if not info.filename.endswith(".dist-info/RECORD"):
                continue
            lines = body.decode("utf-8").splitlines()
            for position, line in enumerate(lines):
                name = line.rsplit(",", 2)[0]
                if name in fixed:
                    lines[position] = f"{name},{record_digest(fixed[name])},{len(fixed[name])}"
            entries[index] = (info, ("\n".join(lines) + "\n").encode("utf-8"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for info, body in entries:
            archive.writestr(normal_info(info.filename, info.date_time), body)
        for name, body in (extra or {}).items():
            archive.writestr(normal_info(name, entries[0][0].date_time), body)
    path.write_bytes(buffer.getvalue())


def normal_info(name: str, date_time: tuple[int, int, int, int, int, int]) -> zipfile.ZipInfo:
    """Return a zip entry header that records nothing about the machine that wrote it."""
    info = zipfile.ZipInfo(name, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = UNIX
    info.external_attr = NORMAL_MODE
    return info


def build_wheel(source: Path, *, epoch: int, scratch: Path) -> bytes:
    """Build the wheel of an exported source tree and return its normalised bytes."""
    constraints = scratch / "build-constraints.txt"
    constraints.write_text(BUILD_CONSTRAINTS, encoding="utf-8")
    output = scratch / "dist"
    environment = {
        **os.environ,
        EPOCH_VARIABLE: str(epoch),
        "PIP_CONSTRAINT": str(constraints),
        "PYTHONHASHSEED": "0",
    }
    completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell.
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(output), str(source)],
        capture_output=True,
        encoding="utf-8",
        check=False,
        env=environment,
    )
    if completed.returncode != 0:
        message = f"The wheel could not be built:\n{completed.stderr[-2000:]}"
        raise ReleaseBuildError(message)
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1:
        message = f"Expected exactly one wheel, found {len(wheels)}."
        raise ReleaseBuildError(message)
    normalise_wheel(wheels[0])
    return wheels[0].read_bytes()


def require_reproducible(first: bytes, second: bytes) -> None:
    """Refuse two builds of one commit that did not produce the same bytes."""
    if first != second:
        message = (
            "Two builds of the same commit are not reproducible: "
            f"{hashlib.sha256(first).hexdigest()} != {hashlib.sha256(second).hexdigest()}. "
            "A digest only this machine can produce is not one anybody else can check."
        )
        raise ReleaseBuildError(message)


def wheel_name(version: str) -> str:
    """Return the wheel filename a release of `version` carries."""
    return f"agentnexus_sdk-{version}-py3-none-any.whl"


def verify_wheel(wheel: Path, *, version: str, coordinates: tuple[str, str]) -> None:
    """Refuse a wheel for another version, or one whose updater could not verify a release."""
    if f"-{version}-" not in wheel.name:
        message = f"The wheel {wheel.name!r} does not carry version {version!r}."
        raise ReleaseBuildError(message)
    try:
        with zipfile.ZipFile(wheel) as archive:
            updater = archive.read(UPDATER_IN_WHEEL).decode("utf-8")
            metadata = archive.read(f"agentnexus_sdk-{version}.dist-info/METADATA").decode("utf-8")
    except (KeyError, OSError, UnicodeDecodeError, zipfile.BadZipFile) as error:
        message = f"{wheel.name} is missing its updater or its METADATA: {error}"
        raise ReleaseBuildError(message) from error
    if f"\nVersion: {version}\n" not in f"\n{metadata}":
        message = f"{wheel.name}'s METADATA does not declare version {version}."
        raise ReleaseBuildError(message)
    if PLACEHOLDER_X in updater or PLACEHOLDER_Y in updater:
        message = f"{wheel.name} carries an unstamped updater and could never verify a release."
        raise ReleaseBuildError(message)
    x, y = coordinates
    if f'"{x}"' not in updater or f'"{y}"' not in updater:
        message = f"{wheel.name} carries an updater stamped with a different key."
        raise ReleaseBuildError(message)


# --- the release tree --------------------------------------------------------------------------


def canonical_bytes(document: Mapping[str, object]) -> bytes:
    """Return the exact bytes the signature covers, serialised as `agentnexus_sdk.release` does."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def stamped_coordinates(tree: Path) -> tuple[str, str] | None:
    """Return the public coordinates stamped into a tree's POSIX loader, if any."""
    found = {
        m["axis"]: m["value"].lower()
        for m in COORDINATE.finditer(
            (tree / "connect.sh").read_text(encoding="utf-8", errors="replace")
        )
    }
    return (found["X"], found["Y"]) if set(found) == {"X", "Y"} else None


def files_of(tree: Path) -> set[str]:
    """Return every file under `tree` as a relative POSIX path."""
    return {path.relative_to(tree).as_posix() for path in tree.rglob("*") if path.is_file()}


def normalised_lines(body: bytes) -> list[str]:
    """Return a file's lines with CRLF folded to LF, so line endings are no difference."""
    return body.decode("utf-8", errors="replace").replace("\r\n", "\n").splitlines()


def check_release_tree(
    tree: Path,
    *,
    previous: Path,
    installers: Path,
    version: str,
    markers: list[bytes],
) -> list[str]:
    """Return every way `tree` fails to be `previous` plus exactly one correctly signed release."""
    failures: list[str] = []
    new_wheel = f"connector/{version}/{wheel_name(version)}"
    moving = {*LOADERS, f"connector/{MANIFEST_NAME}", f"connector/{SIGNATURE_NAME}"}

    before, after = files_of(previous), files_of(tree)
    if f"connector/{version}" in {name.rsplit("/", 1)[0] for name in before}:
        failures.append(f"{version} is already published; its bytes can never change")
    for name in sorted(before - after):
        failures.append(f"{name} was published before and is missing")
    for name in sorted(after - before - {new_wheel}):
        failures.append(f"{name} is not part of a release")
    if new_wheel not in after:
        failures.append(f"{new_wheel} is missing")
    for name in sorted((before & after) - moving):
        if (previous / name).read_bytes() != (tree / name).read_bytes():
            failures.append(f"{name} was published before and has changed")

    coordinates = stamped_coordinates(tree) if (tree / "connect.sh").is_file() else None
    if coordinates is None:
        failures.append("connect.sh carries no stamped public key")
        return failures
    earlier = stamped_coordinates(previous) if (previous / "connect.sh").is_file() else None
    if earlier is not None and earlier != coordinates:
        failures.append("the loaders carry a different key from the previous release's")

    for name in LOADERS:
        if not (tree / name).is_file():
            continue
        source = stamp(
            (installers / name).read_text(encoding="utf-8"), coordinates, name=f"installers/{name}"
        )
        if normalised_lines(source.encode("utf-8")) != normalised_lines((tree / name).read_bytes()):
            failures.append(f"{name} is not its source in installers/ plus the two coordinates")

    manifest_path = tree / "connector" / MANIFEST_NAME
    signature_path = tree / "connector" / SIGNATURE_NAME
    if manifest_path.is_file() and signature_path.is_file():
        failures += check_manifest(
            manifest_path.read_bytes(),
            signature_path.read_text(encoding="ascii", errors="replace").strip(),
            tree=tree,
            version=version,
            origin=origin_of(installers),
            coordinates=coordinates,
        )
    else:
        failures.append("the manifest or its signature is missing")

    if (tree / new_wheel).is_file():
        try:
            verify_wheel(tree / new_wheel, version=version, coordinates=coordinates)
        except ReleaseBuildError as error:
            failures.append(str(error))

    failures += scan_private_material(tree, markers)
    return failures


def check_manifest(
    raw: bytes,
    signature: str,
    *,
    tree: Path,
    version: str,
    origin: str,
    coordinates: tuple[str, str],
) -> list[str]:
    """Return every way the manifest fails to name this wheel on the loaders' origin."""
    failures: list[str] = []
    public = ec.EllipticCurvePublicNumbers(
        int(coordinates[0], 16), int(coordinates[1], 16), ec.SECP256R1()
    ).public_key()
    try:
        if len(signature) != 128:
            raise InvalidSignature
        public.verify(
            utils.encode_dss_signature(int(signature[:64], 16), int(signature[64:], 16)),
            raw,
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError):
        failures.append("the manifest signature does not verify against the loaders' key")

    document = json.loads(raw)
    if raw != canonical_bytes(document):
        failures.append("the manifest is not in canonical form")
    if document.get("schema_version") != SCHEMA_VERSION:
        failures.append(f"the manifest schema is {document.get('schema_version')!r}")
    if document.get("connector_version") != version:
        failures.append(f"the manifest names {document.get('connector_version')!r}, not {version}")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        failures.append("the manifest must name exactly one artifact")
        return failures
    artifact = artifacts[0]
    name = wheel_name(version)
    expected_url = f"{origin}/connector/{version}/{name}"
    if artifact.get("filename") != name or artifact.get("platform") != "any":
        failures.append(f"the manifest's artifact is not {name} for any platform")
    if artifact.get("url") != expected_url:
        failures.append(f"the artifact url is {artifact.get('url')!r}, not {expected_url!r}")
    wheel = tree / "connector" / version / name
    if wheel.is_file():
        body = wheel.read_bytes()
        if artifact.get("size") != len(body):
            failures.append(f"the manifest declares {artifact.get('size')} bytes, not {len(body)}")
        if artifact.get("sha256") != hashlib.sha256(body).hexdigest():
            failures.append("the manifest's sha256 is not the wheel's")
    return failures


def write_release(
    tree: Path,
    *,
    installers: Path,
    wheel: bytes,
    version: str,
    key: ec.EllipticCurvePrivateKey,
    released_at: str,
) -> dict[str, object]:
    """Add one release to `tree`: the wheel, the signed manifest and the stamped loaders."""
    coordinates = public_coordinates(key)
    origin = origin_of(installers)
    name = wheel_name(version)
    directory = tree / "connector" / version
    directory.mkdir(parents=True)
    (directory / name).write_bytes(wheel)
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "connector_version": version,
        "released_at": released_at,
        "artifacts": [
            {
                "platform": "any",
                "filename": name,
                "url": f"{origin}/connector/{version}/{name}",
                "size": len(wheel),
                "sha256": hashlib.sha256(wheel).hexdigest(),
            }
        ],
    }
    raw = canonical_bytes(document)
    (tree / "connector" / MANIFEST_NAME).write_bytes(raw)
    (tree / "connector" / SIGNATURE_NAME).write_text(sign(key, raw).hex(), encoding="ascii")
    for loader in LOADERS:
        text = (installers / loader).read_text(encoding="utf-8")
        # Byte for byte apart from the two coordinates: written without newline translation.
        (tree / loader).write_bytes(
            stamp(text, coordinates, name=f"installers/{loader}").encode("utf-8")
        )
    return document


def record_provenance(repository: Path, *, version: str, commit: str, epoch: int) -> None:
    """Record the source commit and epoch `reproduce` needs to rebuild this release."""
    path = repository / STATE_FILE
    state = json.loads(path.read_text(encoding="utf-8"))
    releases = state.setdefault("reproducible_releases", {})
    releases[version] = {
        "source_commit": commit,
        "source_date_epoch": epoch,
        "build_backend": BUILD_CONSTRAINTS.strip(),
    }
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- commands ----------------------------------------------------------------------------------


def build(
    repository: Path, *, key_path: Path, expected: str, approved: bool, environ: Mapping[str, str]
) -> list[str]:
    """Build, sign and check the release of HEAD, and write it into the release tree."""
    require_clean_tree(repository)
    commit = git(repository, "rev-parse", "HEAD")
    epoch = resolve_epoch(repository, commit, environ=environ)
    key = load_signing_key(key_path, repository=repository)
    coordinates = public_coordinates(key)
    markers = private_markers(key)

    with tempfile.TemporaryDirectory(prefix="agentnexus-connector-release-") as scratch_text:
        scratch = Path(scratch_text)
        wheels: list[bytes] = []
        for attempt in ("first", "second"):
            source = scratch / attempt / "source"
            export_commit(repository, commit, source)
            read_version(source, expected=expected)
            if attempt == "first":
                require_gate_approval(source, approved=approved)
                published = stamped_coordinates(source / RELEASE_DIRECTORY)
                if published is not None and published != coordinates:
                    message = (
                        "This key is not the one the published loaders carry. Rotating the release "
                        "key is its own owner decision, never a side effect of a build."
                    )
                    raise ReleaseBuildError(message)
            updater = source / UPDATER
            updater.write_bytes(
                stamp(updater.read_text(encoding="utf-8"), coordinates, name=str(UPDATER)).encode(
                    "utf-8"
                )
            )
            wheels.append(build_wheel(source, epoch=epoch, scratch=scratch / attempt))
        require_reproducible(wheels[0], wheels[1])

        previous = scratch / "first" / "source" / RELEASE_DIRECTORY
        installers = scratch / "first" / "source" / INSTALLERS_DIRECTORY
        candidate = scratch / "candidate"
        shutil.copytree(previous, candidate)
        if (candidate / "connector" / expected).exists():
            message = f"{expected} is already published; a published version is never rebuilt."
            raise ReleaseBuildError(message)
        (scratch / "wheel").mkdir()
        (scratch / "wheel" / wheel_name(expected)).write_bytes(wheels[0])
        verify_wheel(
            scratch / "wheel" / wheel_name(expected), version=expected, coordinates=coordinates
        )
        document = write_release(
            candidate,
            installers=installers,
            wheel=wheels[0],
            version=expected,
            key=key,
            released_at=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        failures = check_release_tree(
            candidate, previous=previous, installers=installers, version=expected, markers=markers
        )
        if failures:
            return failures

        target = repository / RELEASE_DIRECTORY
        for name in sorted(
            files_of(candidate) - files_of(previous)
            | {*LOADERS}
            | {f"connector/{MANIFEST_NAME}", f"connector/{SIGNATURE_NAME}"}
        ):
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate / name, target / name)
        record_provenance(repository, version=expected, commit=commit, epoch=epoch)

    artifact = document["artifacts"][0]  # type: ignore[index]
    moment = dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"release: connector {expected} from {commit}")
    print(f"release: {artifact['filename']}, {artifact['size']} bytes")
    print(f"release: sha256 {artifact['sha256']}")
    print(f"release: {EPOCH_VARIABLE}={epoch} ({moment}), the committer time of that commit")
    print(f"release: built twice, byte-identical; backend {BUILD_CONSTRAINTS.strip()}")
    print(f"release: manifest on {artifact['url'].rsplit('/connector/', 1)[0]}, signature verified")
    print("release: loaders stamped with the public key the published release already carries")
    print("release: no private key material anywhere in the tree or inside the wheel")
    return []


def reproduce(repository: Path) -> list[str]:
    """Rebuild the committed release from its recorded source, with no key, and check it."""
    tree = repository / RELEASE_DIRECTORY
    document = json.loads((tree / "connector" / MANIFEST_NAME).read_bytes())
    version = str(document.get("connector_version"))
    state = json.loads((repository / STATE_FILE).read_text(encoding="utf-8"))
    record = state.get("reproducible_releases", {}).get(version)
    if record is None:
        if version in LEGACY_RELEASES:
            print(
                f"reproduce: {version} was built in the historical repository before this builder "
                "existed; its bytes are published and immutable, and nothing here rebuilds them."
            )
            return []
        return [f"{version} has no recorded source commit in {STATE_FILE.as_posix()}"]

    commit = str(record["source_commit"])
    epoch = int(record["source_date_epoch"])
    failures: list[str] = []
    if record.get("build_backend") != BUILD_CONSTRAINTS.strip():
        failures.append(f"{version} records backend {record.get('build_backend')!r}")
    try:
        git(repository, "merge-base", "--is-ancestor", commit, "HEAD")
    except ReleaseBuildError:
        return [f"{commit} is not in this history, so {version} cannot be rebuilt from it"]
    if commit_epoch(repository, commit) != epoch:
        failures.append(f"the recorded epoch {epoch} is not the committer time of {commit[:12]}")
    moved = git(repository, "diff", "--name-only", commit, "HEAD", "--", *RELEASE_INPUTS)
    if moved:
        failures.append(f"the release's inputs changed after {commit[:12]}: {moved.split()[:5]}")
    coordinates = stamped_coordinates(tree)
    if coordinates is None:
        return [*failures, "the released loaders carry no public key"]

    with tempfile.TemporaryDirectory(prefix="agentnexus-connector-reproduce-") as scratch_text:
        scratch = Path(scratch_text)
        source = scratch / "source"
        export_commit(repository, commit, source)
        try:
            read_version(source, expected=version)
        except ReleaseBuildError as error:
            failures.append(str(error))
        updater = source / UPDATER
        updater.write_bytes(
            stamp(updater.read_text(encoding="utf-8"), coordinates, name=str(UPDATER)).encode()
        )
        rebuilt = build_wheel(source, epoch=epoch, scratch=scratch)
        committed = (tree / "connector" / version / wheel_name(version)).read_bytes()
        if rebuilt != committed:
            failures.append(
                f"the committed {wheel_name(version)} is not what {commit[:12]} builds: "
                f"{hashlib.sha256(committed).hexdigest()} != {hashlib.sha256(rebuilt).hexdigest()}"
            )
        failures += check_release_tree(
            tree,
            previous=source / RELEASE_DIRECTORY,
            installers=source / INSTALLERS_DIRECTORY,
            version=version,
            markers=[],
        )
        if not failures:
            print(
                f"reproduce: connector {version} rebuilt from {commit} "
                f"with {EPOCH_VARIABLE}={epoch}"
            )
            print(f"reproduce: byte-identical, sha256 {hashlib.sha256(rebuilt).hexdigest()}")
            print("reproduce: the tree is the previous release plus this one, signed by its key")
    return failures


def main(argv: list[str] | None = None) -> int:
    """Run one command and report refusals in sentences rather than tracebacks."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="Build and sign the checked-out commit.")
    build_parser.add_argument(
        "--signing-key",
        type=Path,
        required=True,
        help="PEM P-256 private key, outside every repository.",
    )
    build_parser.add_argument(
        "--expect-version", required=True, help="The version this commit must build."
    )
    build_parser.add_argument(
        "--public-agent-endpoint-approved",
        action="store_true",
        help="Required, and only accepted, when the connector's public gate is open.",
    )
    reproduce_parser = commands.add_parser("reproduce", help="Rebuild the committed release.")
    for command in (build_parser, reproduce_parser):
        # Tests point this at a throwaway clone. An operator never needs it.
        command.add_argument(
            "--repository", type=Path, default=REPOSITORY_ROOT, help=argparse.SUPPRESS
        )
    arguments = parser.parse_args(argv)
    repository = arguments.repository.resolve()

    try:
        if arguments.command == "build":
            failures = build(
                repository,
                key_path=arguments.signing_key,
                expected=arguments.expect_version,
                approved=arguments.public_agent_endpoint_approved,
                environ=os.environ,
            )
        else:
            failures = reproduce(repository)
    except ReleaseBuildError as error:
        failures = [str(error)]
    if failures:
        print(f"REFUSED: the {arguments.command} did not check out:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
