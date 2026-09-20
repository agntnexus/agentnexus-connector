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

## Why this file also checks `AGENTS.md`

Because it is the only thing here that CI runs and that can refuse.

This repository has no test suite and its workflow never invokes one, so there is nowhere else to
put a check that the active instructions still name the right Issue tracker -- and adding a
workflow step or a new collected file is a change `agntnexus/agentnexus#22` does not authorise.

It is worth checking at all because instruction text rots silently. Nothing fails when it goes
stale: a contributor opens an Issue in a repository nobody watches any more, and the first sign is
that the work was never seen. There is no build to break and no runtime to observe.

The two questions stay separate. The release chain and the instructions are reported apart and
refused apart, so neither message can ever describe the other, and this half reads one local file
and nothing else -- no network, no credential, no signature, no publication.

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


#: The active instructions, and what they have to say. Read locally; nothing here reaches a network.
INSTRUCTIONS = ROOT / "AGENTS.md"

#: Where new portfolio work is tracked.
UMBRELLA_TRACKER = "agntnexus/agentnexus"

#: The account that stays forbidden for new Issue activity. Named rather than merely excluded: a
#: refusal that says "some other repository" is one nobody can act on.
RETIRED_ACCOUNT = "ppoinha/AIExperiment"

#: The monolith. Mentioning it is legitimate and often necessary; presenting it as the present-tense
#: authority is the regression, so the rule below is about how a line reads.
HISTORICAL_REPOSITORY = "proplaner/agentnexus-original"

#: A line naming the monolith is acceptable when it says, on that same line, that it is history.
HISTORICAL_MARKERS = ("historical", "archived", "superseded")

#: This repository is public, and that makes its runner rule stricter than the portfolio default
#: rather than softer. If the sentence stating it disappears, the file has lost the boundary that
#: exists because a public pull request can carry code nobody has reviewed.
LOCAL_SECURITY_BOUNDARY = "GitHub-hosted runners only"


def instruction_failures() -> list[str]:
    """Return every way the active instructions fail to state the current issue authority.

    Fail-closed. A missing or empty file is a refusal rather than a silent pass: a check that
    stopped reading anything would otherwise report success for ever.
    """
    if not INSTRUCTIONS.is_file():
        return [f"{INSTRUCTIONS.name} is missing; the active instructions cannot be checked"]

    text = INSTRUCTIONS.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return [f"{INSTRUCTIONS.name} is empty"]

    found: list[str] = []

    if "Issues for the portfolio belong in" not in text or UMBRELLA_TRACKER not in text:
        found.append(f"they do not name {UMBRELLA_TRACKER} as the active issue authority")

    if f"`{RETIRED_ACCOUNT}` is forbidden" not in text:
        found.append(f"they do not forbid new issue activity in {RETIRED_ACCOUNT} by name")

    stale = [
        line
        for line in text.splitlines()
        if HISTORICAL_REPOSITORY in line
        and not any(marker in line.lower() for marker in HISTORICAL_MARKERS)
    ]
    if stale:
        found.append(
            f"{len(stale)} line(s) name {HISTORICAL_REPOSITORY} without saying it is history: "
            + "; ".join(line.strip()[:70] for line in stale[:2])
        )

    if f"https://github.com/{UMBRELLA_TRACKER}/blob/main/docs/ai/" not in text:
        found.append("they do not link the portfolio rules in the umbrella")
    if f"https://github.com/{HISTORICAL_REPOSITORY}/blob/" in text:
        found.append("they still link rules out of the monolith")

    # Whitespace-normalised: the sentence is wrapped in the file, and a check that depended on
    # where the line breaks fall would fail the next time somebody reflows a paragraph.
    flattened = " ".join(text.split())
    if LOCAL_SECURITY_BOUNDARY not in flattened:
        found.append(
            f"they no longer state this repository's stricter boundary: {LOCAL_SECURITY_BOUNDARY!r}"
        )
    if "stricter local boundary is never overridden" not in flattened:
        found.append("they no longer say a stricter local boundary outranks the portfolio default")

    return found


def main() -> int:
    """Check the release chain, the loader stamps and the active instructions.

    Anything that does not add up is refused. The two questions are reported and refused apart, so
    neither message can be mistaken for the other.
    """
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

    # Kept apart from the release chain on purpose, so neither refusal describes the other.
    policy_failures = instruction_failures()
    if not policy_failures:
        print(f"instructions: {INSTRUCTIONS.name} names {UMBRELLA_TRACKER} as the issue authority")

    if failures:
        print("\nREFUSED: the published release does not check out:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
    if policy_failures:
        print(
            "\nREFUSED: the active instructions do not state the current issue authority:",
            file=sys.stderr,
        )
        for failure in policy_failures:
            print(f"  {failure}", file=sys.stderr)
    if failures or policy_failures:
        return 1

    print(
        "\nThe release chain verifies from published data alone, and the instructions "
        "name the current issue authority."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
