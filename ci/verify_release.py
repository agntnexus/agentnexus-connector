#!/usr/bin/env python
"""Verify the published release chain using nothing but what this repository already publishes.

This repository now owns what a Connector installation downloads, which means it owns the promise
that the download is checkable. The promise is only worth something if it is checked, so CI checks
it on every run.

The chain, end to end:

1. `installers/` carries the loader source with `REPLACE_RELEASE_PUBLIC_KEY_X/Y` placeholders;
   `connector-release/` carries the same loaders with the **public** P-256 coordinates stamped in.
2. Those coordinates verify the ECDSA signature over the release manifest.
3. The manifest declares a filename, a size and a SHA-256 for the artifact of the current release.
4. The artifact on disk matches both.

Nothing here signs anything and nothing needs a secret: a private key would let this file *create* a
release, which is exactly what it must never be able to do. It reads a public key out of a published
file and checks a signature that already exists.

The released loaders are also compared against `installers/`, because "stamped with a public key" is
a claim that should be measurable rather than trusted: the only permitted difference is the two
coordinate lines.

Usage::

    python ci/verify_release.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "connector-release"
INSTALLERS = ROOT / "installers"

#: The loaders that carry a stamped key, and their unstamped source.
LOADERS = ("connect.sh", "connect.ps1", "remove.ps1")

#: A stamped coordinate, 64 hex characters. Two spellings, because the POSIX loader and the
#: PowerShell loaders name the same value differently: `RELEASE_PUBLIC_KEY_X` and
#: `$ReleasePublicKeyX`. Matching only the first is how this check first reported that two loaders
#: carried "a different key" when they carried the same one.
COORDINATE = re.compile(
    r"(?:RELEASE_PUBLIC_KEY_|ReleasePublicKey)(?P<axis>[XY])\b[^=]*=\s*"
    r"['\"]?(?P<value>[0-9a-fA-F]{64})['\"]?"
)

#: How many lines of a loader may differ from its source. Two: the X and Y coordinate.
PERMITTED_LOADER_DIFFERENCES = 2


def stamped_key(text: str) -> dict[str, str]:
    """Return the public coordinates stamped into one loader."""
    return {match.group("axis"): match.group("value") for match in COORDINATE.finditer(text)}


def lines(path: Path) -> list[str]:
    """Return a file's lines with line endings normalised, so CRLF is not a difference."""
    return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").splitlines()


def main() -> int:
    """Check the release chain and the loader stamps, and refuse anything that does not add up."""
    failures: list[str] = []

    released = RELEASE / "connect.sh"
    if not released.is_file():
        print(f"REFUSED: no released loader at {released}", file=sys.stderr)
        return 1
    coordinates = stamped_key(released.read_text(encoding="utf-8", errors="replace"))
    if set(coordinates) != {"X", "Y"}:
        print("REFUSED: the released loader carries no stamped public key", file=sys.stderr)
        return 1
    print(f"public key : x={coordinates['X'][:16]}... y={coordinates['Y'][:16]}...")

    # Every stamped loader must carry the same key, and differ from its source only by the stamp.
    for name in LOADERS:
        source = INSTALLERS / name
        target = RELEASE / name
        if not target.is_file() or not source.is_file():
            failures.append(f"{name}: missing from installers/ or connector-release/")
            continue
        if stamped_key(target.read_text(encoding="utf-8", errors="replace")) != coordinates:
            failures.append(f"{name}: stamped with a different key")
        differing = sum(
            1 for left, right in zip(lines(source), lines(target), strict=False) if left != right
        )
        if len(lines(source)) != len(lines(target)):
            failures.append(f"{name}: differs from installers/ in length, not only in the stamp")
        elif differing != PERMITTED_LOADER_DIFFERENCES:
            failures.append(
                f"{name}: differs from installers/ on {differing} lines, expected only the two "
                "coordinate lines"
            )
        else:
            print(f"loader     : {name} is its source plus the stamp, and nothing else")

    manifest_path = RELEASE / "connector" / "connector-release.json"
    signature_path = RELEASE / "connector" / "connector-release.json.sig"
    manifest_bytes = manifest_path.read_bytes()

    # Constructing the key can fail, and a negative control found it: coordinates that are not a
    # point on P-256 raise here rather than failing a signature check later. The exit code was
    # already right, but a traceback is a worse diagnostic than a sentence, and this file exists to
    # tell somebody what is wrong.
    try:
        public = ec.EllipticCurvePublicNumbers(
            x=int(coordinates["X"], 16), y=int(coordinates["Y"], 16), curve=ec.SECP256R1()
        ).public_key()
    except ValueError:
        print(
            "REFUSED: the coordinates stamped into the loaders are not a point on P-256.\n"
            "  A release signed by a real key cannot have produced them.",
            file=sys.stderr,
        )
        return 1

    # r||s as fixed-width hex, which a PowerShell 5.1 verifier can consume without an ASN.1 parser.
    raw = signature_path.read_text(encoding="ascii").strip()
    if len(raw) != 128:
        failures.append(f"signature is {len(raw)} characters, expected 128")
    else:
        signature = utils.encode_dss_signature(int(raw[:64], 16), int(raw[64:], 16))
        try:
            public.verify(signature, manifest_bytes, ec.ECDSA(hashes.SHA256()))
            print("signature  : verifies against the published key")
        except InvalidSignature:
            failures.append("the manifest signature does not verify against the published key")

    manifest = json.loads(manifest_bytes)
    version = manifest["connector_version"]
    print(f"manifest   : connector {version}, released {manifest['released_at']}")

    for artifact in manifest["artifacts"]:
        name = artifact["filename"]
        candidate = RELEASE / "connector" / version / name
        if not candidate.is_file():
            failures.append(f"{name}: named by the manifest but not present")
            continue
        body = candidate.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != artifact["sha256"]:
            failures.append(f"{name}: sha256 {digest} != {artifact['sha256']}")
        elif len(body) != artifact["size"]:
            failures.append(f"{name}: size {len(body)} != {artifact['size']}")
        else:
            print(f"artifact   : {name} matches its declared digest and size")

    if failures:
        print("\nREFUSED: the published release does not check out:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print("\nThe release chain verifies from published data alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
