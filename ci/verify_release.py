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
from typing import Final

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


#: The component documentation index, and the records #16 assigned to this repository. Written out
#: rather than derived from the index: deriving it would make the index agree with itself, and the
#: point of the list is that something outside the document says what the document must account for.
DOCUMENTATION = ROOT / "docs"
DOCUMENTATION_INDEX = DOCUMENTATION / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

ASSIGNED_RECORDS: Final = {
    "docs/public-connector/INSTALL.md": "already here",
    "docs/public-connector/VERIFY.md": "already here",
    "docs/public-connector/BEHAVIOUR.md": "already here",
    "docs/public-connector/TROUBLESHOOTING.md": "already here",
    "docs/public-connector/SECURITY.md": "already here",
    "docs/integration/CONNECTOR.md": "adopted",
    "docs/integration/PROFILE_MIGRATION.md": "adopted",
    "docs/integration/PROFILE_MIGRATION_ACCEPTANCE.md": "adopted",
    "docs/integration/CONNECTOR_RELEASE_0_5_0_NOTES.md": "adopted",
    "docs/releases/connector-0.5.0.md": "adopted",
    "docs/releases/connector-0.6.0.md": "adopted",
    "docs/releases/connector-0.6.1.md": "adopted",
    "docs/releases/connector-release-state.json": "adopted",
    "docs/ai/CONNECTOR_AUTOMATIC_UPDATE_SAFETY_PLAN.md": "adopted",
    "docs/ai/CONNECTOR_RELEASE_READINESS.md": "adopted",
    "docs/ai/PROFILE_MIGRATION_SLICE.md": "adopted",
    "docs/ai/PUBLIC_CONNECTOR_RELEASE_REPOSITORY_PLAN.md": "adopted",
    "docs/ai/REVIEW_C3B_REQUEST_UPDATE_SAFETY.md": "adopted",
    "docs/ai/RUNTIME_MODEL_DECLARATION_PLAN.md": "adopted",
    "docs/ai/SOUL_APPLICATION_HANDOFF.md": "adopted",
}

#: Every page an "adopted" row has to have produced, relative to `docs/`.
ADOPTED_PAGES: Final = tuple(
    record[len("docs/") :]
    for record, disposition in ASSIGNED_RECORDS.items()
    if disposition == "adopted"
)

#: A line naming the monolith has to say which kind of claim it is making. It is no longer the issue
#: tracker or the documentation authority, but it *is* still the upstream source of the mirrored
#: code. Erasing those sentences would make the documentation wrong to make a check pass.
DOCUMENT_MARKERS: Final = (
    "historical",
    "archived",
    "superseded",
    "history",
    "evidence",
    "upstream",
    "mirror",
    "source",
    "generated",
    "canonical",
)
CITATION: Final = re.compile(r"/(issues|pull|actions/runs)/\d+")
DIRECTING: Final = re.compile(
    r"\b(open|create|file|report|track|raise)\b[^.]{0,60}\bissue", re.IGNORECASE
)

#: Things that are never legitimate in a document this repository publishes. Deliberately narrow:
#: a rule wide enough to tell an example persona's home directory from a real operator's would
#: need the real account name, and writing that name into a public repository is the disclosure
#: it is meant to prevent. Operator paths are a review step, recorded in `docs/README.md`.
NEVER_PUBLIC: Final = {
    "a tailnet hostname": re.compile(r"[a-z0-9-]+\.ts\.net", re.IGNORECASE),
    "a private network address": re.compile(
        r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"
    ),
    "private key material": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def documentation_pages() -> list[Path]:
    """Every markdown page under `docs/`, in a stable order."""
    return sorted(DOCUMENTATION.rglob("*.md"))


def prose(page: Path) -> list[tuple[int, str]]:
    """Return the lines of a page that make a claim, which is not all of them.

    Fenced blocks are skipped. A diagram or a shell command naming the monolith is not asserting who
    the authority is, and a rule that treated it as prose would push the documentation towards
    writing worse diagrams rather than truer sentences.
    """
    kept: list[tuple[int, str]] = []
    fenced = False
    for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            kept.append((number, line))
    return kept


def declared_expression() -> str | None:
    """Pull the declared-file expression out of the workflow that enforces it.

    Found by the one page the rule has always named rather than by a variable, because the
    expression is piped straight into `grep` here. Restating it would create a second truth, and the
    first thing it would fail to notice is the workflow changing.
    """
    if not WORKFLOW.is_file():
        return None
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        if "MIRROR" not in line or line.count("'") < 2 or "grep" not in line:
            continue
        return line[line.index("'") + 1 : line.rindex("'")] or None
    return None


def documentation_failures() -> list[str]:
    """Return every way the documentation fails to account for what was assigned to it.

    Fail-closed. A missing or empty index is a refusal, not a silent pass: an adoption can look
    complete while being empty, and a guard that stopped reading would report success for ever.
    """
    if not DOCUMENTATION_INDEX.is_file():
        return ["docs/README.md is missing; the documentation cannot be checked"]
    index = DOCUMENTATION_INDEX.read_text(encoding="utf-8")
    if not index.strip():
        return ["docs/README.md is empty"]

    pages = documentation_pages()
    if not pages:
        return ["docs/ holds no pages"]

    found: list[str] = []

    for record, disposition in ASSIGNED_RECORDS.items():
        rows = [
            line for line in index.splitlines() if line.startswith("|") and f"`{record}`" in line
        ]
        if len(rows) != 1:
            found.append(f"{record}: {len(rows)} ledger rows, expected exactly one")
        elif disposition not in rows[0]:
            found.append(f"{record}: the ledger does not say {disposition!r}")

    for relative in ADOPTED_PAGES:
        page = DOCUMENTATION / relative
        if not page.is_file():
            found.append(f"{relative} is claimed adopted but absent")
        elif page.stat().st_size == 0:
            found.append(f"{relative} is empty")

    for page in pages:
        for match in re.finditer(r"\]\(([^)\s]+)\)", page.read_text(encoding="utf-8")):
            target = match.group(1)
            if target.startswith(("http", "#", "mailto:")):
                continue
            if not (page.parent / target.split("#")[0]).exists():
                found.append(f"{page.name}: broken link to {target}")

    for page in pages:
        for number, line in prose(page):
            lowered = line.lower()
            unmarked = not any(marker in lowered for marker in DOCUMENT_MARKERS)
            if "agentnexus-original" in line and not CITATION.search(line) and unmarked:
                found.append(f"{page.name}:{number} reads as current authority")

            names_retired = "agentnexus-original" in line or "ppoinha/AIExperiment" in line
            unexcused = not any(
                excuse in lowered for excuse in ("historical", "archived", "superseded", "citation")
            )
            if names_retired and DIRECTING.search(line) and unexcused:
                found.append(f"{page.name}:{number} directs new issue activity to a dead tracker")

    for path in sorted(DOCUMENTATION.rglob("*")):
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in NEVER_PUBLIC.items():
            if pattern.search(body):
                found.append(f"{path.name} publishes {label}")

    declared = declared_expression()
    if declared is None:
        found.append("the workflow no longer declares which documents belong to this repository")
    else:
        rule = re.compile(declared)
        for relative in (*ADOPTED_PAGES, "README.md"):
            if not rule.search(f"docs/{relative}"):
                found.append(f"docs/{relative} would be refused as undeclared")
        # The negative probe. Widening the declared set is only safe if it stayed a set: an
        # enumeration that quietly became "anything under docs/" would satisfy every check above
        # while admitting a document no migration record accounts for.
        for stray in (
            "docs/NOT_AN_ASSIGNED_RECORD.md",
            "docs/notes/scratch.md",
            "docs/ai/UNASSIGNED_PLAN.md",
            "docs/releases/connector-9.9.9.md",
        ):
            if rule.search(stray):
                found.append(f"the declared set admits {stray}, which nothing assigned")

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
    documentation = documentation_failures()
    if not documentation:
        print(
            f"documentation: {len(ASSIGNED_RECORDS)} assigned records accounted for, "
            f"{len(ADOPTED_PAGES)} adopted pages present"
        )

    if documentation:
        print(
            "\nREFUSED: the documentation does not account for what was assigned to it:",
            file=sys.stderr,
        )
        for failure in documentation:
            print(f"  {failure}", file=sys.stderr)
    if failures or policy_failures or documentation:
        return 1

    print(
        "\nThe release chain verifies from published data alone, the instructions name the "
        "current issue authority, and the documentation accounts for every assigned record."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
