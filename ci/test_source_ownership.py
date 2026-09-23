"""Check that this repository claims the Connector source, and sends nobody upstream for it.

`src/agentnexus_sdk/**` is owned here. That was settled by owner decision under
agntnexus/agentnexus#5, and it matters beyond bookkeeping: the active documents said the opposite.
They named a private repository as the canonical source, described the source tree here as frozen,
and said a pull request against it would be declined and overwritten the next time the source was
published. Acting on those sentences means one of two failures — the fix is written into a private
archive nobody may change any more, or it is written here and believed to be temporary.

A wrong ownership claim rots exactly the way a wrong tracker does: nothing fails, no build breaks,
and the first sign is a change that went to the wrong place or was never made at all. So it is
worth a guard, and the guard has to be the kind that fails when somebody restores the old sentence.

What is checked:

* no active rule-carrying document claims a canonical source, an upstream or a frozen path for the
  code this repository holds;
* `docs/MIRROR.md` says plainly that this repository is canonical for `src/agentnexus_sdk/**`, and
  that the historical repository is an archive and never an upstream;
* every line that names the historical repository says, on that line, that it is history;
* the release-integrity rules are **not** collateral damage: the released loaders are still their
  source plus the two stamped coordinates, the manifest still pins size and SHA-256, and no private
  signing key may ever be here. Those rules are about `installers/**` and `connector-release/**`,
  they are unaffected by who owns the source, and a correction that dropped them would have traded
  one defect for a worse one;
* `SECURITY.md` tells a reader the same thing `docs/MIRROR.md` does -- that this is where the
  Connector is developed -- and keeps every security rule it states while saying so.

`SECURITY.md` was not in the first version of this guard, and that is how the retired claim
survived there after `docs/MIRROR.md` was corrected (agntnexus/agentnexus#68). It made the claim in
words none of the phrases below matched, on the page a vulnerability reporter reads first: that the
repository was a transparency mirror of a private development repository, and that a change made
here would be overwritten. A guard over the wrong list of documents reports success for exactly the
document it should have read.

The cases at the end feed the retired sentences back in and require a refusal, so this guard is
proven to fail rather than assumed to. It reads local files only: no network, no credential, no
other repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
MIRROR = REPOSITORY_ROOT / "docs" / "MIRROR.md"

#: The source this repository owns, spelled the way the documents spell it.
OWNED_SOURCE = "src/agentnexus_sdk/**"

#: Private, unchanged, and an archive. Never an upstream, never a destination for new work.
HISTORICAL = "proplaner/agentnexus-original"

#: A line naming the historical repository is acceptable when it says on that line that it is
#: history. Deliberately narrower than the marker list `ci/verify_release.py` applies to the
#: documentation at large: `upstream`, `mirror`, `source` and `canonical` are precisely the words
#: the retired claim was made with, so they cannot also be what excuses it.
HISTORY_MARKERS = ("historical", "history", "archived", "archive", "superseded", "no longer")

#: The documents that carry a rule somebody could act on. Dated records under `docs/ai/`,
#: `docs/integration/` and `docs/releases/` are not here: they describe what was true when they
#: were written, and rewriting them would be editing the past to make the present tidy.
RULE_CARRYING_DOCUMENTS = (
    "docs/MIRROR.md",
    "SECURITY.md",
    "AGENTS.md",
    ".github/workflows/ci.yml",
    "ci/verify_release.py",
    "ruff.toml",
    "constraints-ci.txt",
)

#: Sentence shapes that hand this source to another repository. Matched as phrases rather than by
#: single words, because `mirror`, `source` and `canonical` all have honest uses here — the page is
#: called `MIRROR.md`, the release manifest has canonical bytes, and `verify_release.py` reads a
#: signature over them.
RETIRED_CLAIMS: tuple[tuple[str, str], ...] = (
    ("canonical source", "names a canonical source for this code somewhere else"),
    ("the canonical repository", "defers to another repository as the owner of this code"),
    ("changed upstream", "says this code is changed upstream"),
    ("adopted upstream", "says a change made here is not adopted upstream"),
    ("frozen path", "says a change to a path here would be declined"),
    ("mirrored from", "says this code is mirrored from somewhere else"),
    ("upstream source", "names an upstream source for this code"),
    ("the next time the source is published", "expects this code to be overwritten from elsewhere"),
    # The shapes `SECURITY.md` used for the same claim (agntnexus/agentnexus#68). Still phrases, not
    # words: "mirror" names a page here, and "private" is how a vulnerability report starts.
    ("transparency mirror", "calls this repository a mirror of source kept elsewhere"),
    ("mirrored one way", "says this code arrives one way from somewhere else"),
    ("overwritten rather than adopted", "says a change made here would be overwritten"),
    (
        "made in the private development repository",
        "sends changes to this code to a private repository",
    ),
)

#: What `docs/MIRROR.md` has to say, now that it is no longer a page about being a mirror.
REQUIRED_OWNERSHIP = (
    f"This repository is canonical for `{OWNED_SOURCE}`",
    "never an upstream",
)

#: What the same page must not lose while saying it. These are release-integrity rules about
#: `installers/**` and `connector-release/**`; who owns the source does not touch them.
REQUIRED_RELEASE_INTEGRITY = (
    "the released loaders are `installers/` plus **two lines**",
    "`ci/verify_release.py` refuses any other difference",
    "declared size and SHA-256 must match the artifact's bytes",
    "No private signing key is here",
)


SECURITY = REPOSITORY_ROOT / "SECURITY.md"

#: The ownership statement `SECURITY.md` has to make, and the one it has to share with
#: `docs/MIRROR.md`. Checked in both, whitespace-normalised, because agreement between the two pages
#: is the property that was missing: each was consistent on its own, and together they were not.
SHARED_OWNERSHIP = "where the Connector is developed"

#: What else `SECURITY.md` must say about ownership: where the list of owned paths lives, so the
#: page points at it rather than growing a second copy of it.
REQUIRED_SECURITY_OWNERSHIP = (SHARED_OWNERSHIP, "`docs/MIRROR.md`")

#: The security rules `SECURITY.md` states. Correcting who owns the source is no reason to lose any
#: of them, and the easiest way to lose one is to rewrite the paragraph it sits in -- which is what
#: this correction does. "Not an installation source" sits in that very paragraph.
REQUIRED_SECURITY_POLICY = (
    "not an installation source",
    "Report privately first.",
    "Send nothing sensitive in a report.",
    "GitHub private vulnerability reporting",
    "Only the **current released version** is supported.",
    "Already-published signed artifact bytes are never replaced under an existing version",
    "`https://agntnexus.com` is the only origin a Connector is installed or updated from.",
    "accepts **no** patches to the platform",
)


def normalised(text: str) -> str:
    """Collapse whitespace, so a rule survives the page being rewrapped at a hundred columns."""
    return " ".join(text.split())


def security_problems(text: str) -> list[str]:
    """Return every ownership statement or security rule `SECURITY.md` has stopped making.

    Fail-closed, like the others: an empty page states no policy, and that is a refusal.
    """
    if not text.strip():
        return ["the page is empty; the security policy cannot be checked"]
    flat = normalised(text)
    return [
        f"it does not say: {required!r}"
        for required in (*REQUIRED_SECURITY_OWNERSHIP, *REQUIRED_SECURITY_POLICY)
        if required not in flat
    ]


def document(relative: str) -> str:
    """Read one rule-carrying document, the way CI reads it."""
    return (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")


def retired_claims(text: str) -> list[str]:
    """Return every retired ownership claim a document still makes.

    A pure function over the text, so the mutation cases below can hand it a deliberately restored
    sentence without writing anything to the repository.

    Whitespace is collapsed first. The page wraps at a hundred columns, and a phrase that happens to
    straddle a line break is still the same sentence: `SECURITY.md` broke "made in the private
    development repository" across exactly that boundary (agntnexus/agentnexus#68).
    """
    lowered = " ".join(text.split()).lower()
    return [complaint for phrase, complaint in RETIRED_CLAIMS if phrase in lowered]


def unmarked_mentions(text: str) -> list[str]:
    """Return every line that names the historical repository without saying it is history."""
    return [
        f"line {number} names {HISTORICAL} without saying it is history"
        for number, line in enumerate(text.splitlines(), start=1)
        if HISTORICAL in line and not any(marker in line.lower() for marker in HISTORY_MARKERS)
    ]


def ownership_problems(text: str) -> list[str]:
    """Return every way `docs/MIRROR.md` fails to state who owns this source.

    Fail-closed: an empty or missing page is a refusal rather than a silent pass, because a guard
    that stopped reading would report success for ever.
    """
    if not text.strip():
        return ["the page is empty; ownership cannot be checked"]
    return [
        f"it does not say: {required!r}" for required in REQUIRED_OWNERSHIP if required not in text
    ]


def release_integrity_problems(text: str) -> list[str]:
    """Return every release-integrity rule the page has stopped stating."""
    if not text.strip():
        return ["the page is empty; the release rules cannot be checked"]
    return [
        f"it no longer states: {required!r}"
        for required in REQUIRED_RELEASE_INTEGRITY
        if required not in text
    ]


def test_ci_executes_this_guard() -> None:
    """A guard nobody runs is not a guard. The workflow has to name this file."""
    assert WORKFLOW.is_file(), "the workflow that must run this guard is missing"
    assert "ci/test_source_ownership.py" in WORKFLOW.read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", RULE_CARRYING_DOCUMENTS)
def test_no_active_document_sends_this_source_upstream(relative: str) -> None:
    """No rule anybody could act on may point at another repository for this code."""
    assert retired_claims(document(relative)) == []


@pytest.mark.parametrize("relative", RULE_CARRYING_DOCUMENTS)
def test_every_mention_of_the_historical_repository_says_it_is_history(relative: str) -> None:
    """Naming the archive is fine. Presenting it as somewhere work still goes is not."""
    assert unmarked_mentions(document(relative)) == []


def test_the_mirror_page_states_who_owns_this_source() -> None:
    """The positive half: somebody arriving with a fix has to be told where it belongs."""
    assert MIRROR.is_file(), "the page that states ownership is missing"
    assert ownership_problems(MIRROR.read_text(encoding="utf-8")) == []


def test_the_release_integrity_rules_survive_the_correction() -> None:
    """Ownership moved; the rules that keep a release checkable did not."""
    assert release_integrity_problems(MIRROR.read_text(encoding="utf-8")) == []


def test_the_security_policy_states_who_owns_this_source_and_keeps_its_rules() -> None:
    """The page a reporter reads first has to send a fix to the right place, and lose nothing."""
    assert SECURITY.is_file(), "the security policy is missing"
    assert security_problems(SECURITY.read_text(encoding="utf-8")) == []


def test_the_security_policy_and_the_mirror_page_agree() -> None:
    """Read both. Each page being right on its own is what let them disagree."""
    for page in (SECURITY, MIRROR):
        assert SHARED_OWNERSHIP in normalised(page.read_text(encoding="utf-8")), page.name


#: The two passages `SECURITY.md` carried until agntnexus/agentnexus#68, verbatim, so that restoring
#: either one is what the cases below detect.
RETIRED_SECURITY_IDENTITY = """\
It is a transparency mirror. It is not the platform, not an installation source, and not a place
where the running service is developed. The source is mirrored one way out of a private
development repository, so a change made here would be overwritten rather than adopted; see
`docs/BEHAVIOUR.md` for what the software does and `docs/VERIFY.md` for how to check a release.
"""

RETIRED_SECURITY_SUPPORT = """\
This repository accepts **no** patches to the platform, no feature requests for the service, and
no requests for access. It is a mirror, and changes to the Connector are made in the private
development repository and arrive here as a new reviewed release.
"""


def test_the_retired_security_identity_is_refused() -> None:
    """The mutation this change exists for: the old paragraph, put back verbatim."""
    problems = retired_claims(RETIRED_SECURITY_IDENTITY)

    assert "calls this repository a mirror of source kept elsewhere" in problems
    assert "says this code arrives one way from somewhere else" in problems
    assert "says a change made here would be overwritten" in problems


def test_the_retired_security_support_boundary_is_refused() -> None:
    """The second passage made the same claim sixty lines later, in different words."""
    assert retired_claims(RETIRED_SECURITY_SUPPORT) == [
        "sends changes to this code to a private repository"
    ]


def test_restoring_a_retired_passage_into_the_real_page_is_refused() -> None:
    """Not only the phrases in isolation: the real page with either passage put back must fail.

    The guard reads `SECURITY.md` through `RULE_CARRYING_DOCUMENTS`; this proves the list and the
    phrases catch it together, which is the pair that was missing.
    """
    assert "SECURITY.md" in RULE_CARRYING_DOCUMENTS
    text = SECURITY.read_text(encoding="utf-8")
    for retired in (RETIRED_SECURITY_IDENTITY, RETIRED_SECURITY_SUPPORT):
        assert retired_claims(text + "\n" + retired), retired[:40]


def test_losing_the_ownership_statement_from_the_security_policy_is_refused() -> None:
    """Deleting the corrected sentence must fail as loudly as restoring the wrong one."""
    text = normalised(SECURITY.read_text(encoding="utf-8"))
    for required in REQUIRED_SECURITY_OWNERSHIP:
        assert security_problems(text.replace(required, "")) == [f"it does not say: {required!r}"]


@pytest.mark.parametrize("required", REQUIRED_SECURITY_POLICY)
def test_losing_a_security_rule_is_refused(required: str) -> None:
    """Rewriting the paragraph is exactly how a security rule gets lost. Each one is checked."""
    text = normalised(SECURITY.read_text(encoding="utf-8"))
    assert security_problems(text.replace(required, "")) == [f"it does not say: {required!r}"]


def test_an_empty_security_policy_is_refused() -> None:
    """Fail closed. A page that says nothing states no policy."""
    assert security_problems("") == ["the page is empty; the security policy cannot be checked"]


#: The claim as it actually stood before agntnexus/agentnexus#5, quoted so that restoring it is
#: what the cases below detect — not a paraphrase that could drift away from the real sentence.
RETIRED_PARAGRAPH = """## The canonical source

`proplaner/agentnexus-original` is private and remains the single canonical source for everything
mirrored here, until an explicit later cutover. The flow is one-directional.

Nothing flows back. A change committed here to a mirrored path is **not adopted upstream** — it is
overwritten the next time the source is published.
"""

RETIRED_FROZEN_RULE = (
    "A pull request against a frozen path will be declined however good the change is, because "
    "merging it would create a second version of a file that already has an owner.\n"
)


def test_the_retired_upstream_claim_is_refused() -> None:
    """The mutation this guard exists for: the old paragraph, put back verbatim."""
    problems = retired_claims(RETIRED_PARAGRAPH)

    assert "names a canonical source for this code somewhere else" in problems
    assert "says a change made here is not adopted upstream" in problems
    assert "expects this code to be overwritten from elsewhere" in problems


def test_the_retired_frozen_path_rule_is_refused() -> None:
    """A page that would decline a fix to this source is refused whatever else it says."""
    assert retired_claims(RETIRED_FROZEN_RULE) == ["says a change to a path here would be declined"]


def test_an_unmarked_mention_of_the_historical_repository_is_refused() -> None:
    """The word that excuses the name has to be on the same line as the name."""
    assert unmarked_mentions(f"The source lives in `{HISTORICAL}` and is published from there.")
    assert unmarked_mentions(f"`{HISTORICAL}` is historical, private and unchanged.") == []


def test_losing_the_ownership_statement_is_refused() -> None:
    """Deleting the sentence is the other way to lose the rule, and it fails too."""
    text = MIRROR.read_text(encoding="utf-8")
    for required in REQUIRED_OWNERSHIP:
        assert ownership_problems(text.replace(required, "")) == [f"it does not say: {required!r}"]
    assert ownership_problems("") == ["the page is empty; ownership cannot be checked"]


def test_losing_a_release_integrity_rule_is_refused() -> None:
    """Correcting ownership may not quietly cost the release its checkable properties."""
    text = MIRROR.read_text(encoding="utf-8")
    for required in REQUIRED_RELEASE_INTEGRITY:
        assert release_integrity_problems(text.replace(required, "")) == [
            f"it no longer states: {required!r}"
        ]
