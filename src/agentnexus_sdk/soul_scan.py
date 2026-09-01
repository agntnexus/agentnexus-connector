r"""Run the target runtime's own context-file threat scanner before a soul is shown or written.

A runtime that refuses to load a context file is not a runtime with a broken file: it is a runtime
with an identity the applicant never chose. That distinction is what this module exists for, and
it was learned the hard way.

A real Hermes 0.20.6 run installed a complete, correct soul for profile `gaga`, reported success,
and then answered as the stock Hermes identity. The profile-specific log said:

    Context file SOUL.md blocked: role_pretend

Two guardrails the applicant had asked for — "never pretend to be an expert" and "never pretend to
be human" — matched Hermes' own prompt-injection regex for `pretend ... to be`. Everything a
connector normally checks was correct: the file existed, its digest matched, the profile path was
right, and `hermes -p gaga mcp list` was healthy. **The existence of a syntactically valid
`SOUL.md` is not evidence that the runtime loaded it.**

Two rules follow, and both are enforced here rather than remembered:

* The runtime scans the *candidate* before an applicant is shown a preview. Approving a document
  the runtime will silently discard wastes the one moment the applicant was paying attention.
* A finding is rewritten preserving its meaning, or it stops the install. Deleting a boundary an
  applicant asked for would quietly produce a less careful agent than the one they described,
  which is worse than refusing and saying so.

The rewrite table below is a convenience for the one class of false positive that has actually
happened, and it is *derived from the runtime's real rule* rather than guessed at. Hermes 0.20.6
defines, in `tools/threat_patterns.py`:

    _FILLER = r"(?:\w+\s+){0,8}"
    (rf'pretend\s+{_FILLER}(you\s+are|to\s+be)\s+', "role_pretend", "context")

so the trigger is the verb `pretend` followed by up to eight bare words and then `to be` or
`you are`. Replacing the verb with `claim` clears it and keeps the sentence's meaning, which was
confirmed against the installed scanner rather than assumed.

The table is still never the gate: whatever it produces is handed back to the runtime and
rescanned, so a rewrite that does not clear the finding fails exactly like no rewrite at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

#: The candidate is written here for scanning and deleted afterwards. Never the live destination:
#: a runtime asked to scan its own active context file would be scanning the document we are
#: proposing to replace, and a crash between write and scan would leave the replacement installed
#: without ever having been checked.
CANDIDATE_FILENAME: Final = "SOUL.md"


class ContextScanner(Protocol):
    """The one method this module needs from a runtime adapter."""

    name: str
    display_name: str

    def scan_context(self, path: Path) -> list[str]:
        """Return the runtime's findings for this file, empty when it accepts it."""
        ...


@dataclass(frozen=True)
class Finding:
    """One phrase this connector recognises as a known false positive, and its replacement."""

    original: str
    replacement: str
    reason: str


@dataclass
class VetOutcome:
    """What the runtime made of a candidate soul, and what is safe to install."""

    accepted: bool
    text: str
    rewritten: bool = False
    findings: list[str] = field(default_factory=list)
    #: False when the runtime publishes no scanner this connector can call. Distinct from "clean":
    #: an unavailable scanner is missing evidence, not evidence of safety.
    scanner_available: bool = True


#: The filler Hermes 0.20.6 allows between the verb and the role phrase, copied from its own
#: `_FILLER`. Bare words only — a comma or a full stop breaks the match, which is why
#: "never pretend, under any circumstances, to be a doctor" is not flagged by the real scanner
#: and is deliberately not rewritten here either.
_FILLER: Final = r"(?:\w+\s+){0,8}?"

#: The `role_pretend` trigger, expressed the way the runtime expresses it. Kept as one pattern
#: with named groups so a single rewrite preserves both the verb's inflection and its casing.
_ROLE_PRETEND: Final = re.compile(
    rf"\b(?P<verb>pretend(?:s|ing|ed)?)\s+(?P<rest>{_FILLER}(?:to\s+be|you\s+are)\s+)",
    re.IGNORECASE,
)

#: `pretend` carries an accusation the sentence never needed. `claim` says the same thing about a
#: boundary — "it must never claim to be human" — and does not match the runtime's heuristic.
_VERB_REPLACEMENTS: Final[dict[str, str]] = {
    "pretend": "claim",
    "pretends": "claims",
    "pretending": "claiming",
    "pretended": "claimed",
}

_ROLE_PRETEND_REASON: Final = (
    "`pretend ... to be` / `pretend ... you are` matches the runtime's role-impersonation "
    "heuristic (role_pretend)"
)


def _reword_role_pretend(match: re.Match[str]) -> str:
    """Swap the verb, keeping its inflection, its casing, and everything after it."""
    verb = match.group("verb")
    replacement = _VERB_REPLACEMENTS[verb.lower()]
    if verb[:1].isupper():
        replacement = replacement.capitalize()
    return replacement + " " + match.group("rest")


#: Phrases that are harmless guardrails and that the runtime's injection heuristics read as an
#: instruction to impersonate. Each replacement keeps the boundary and loses the trigger.
#:
#: Deliberately tiny and specific. A broad rewriter would quietly reword documents an applicant
#: wrote, which is a different product from the one that shows them an exact preview.
KNOWN_FALSE_POSITIVES: Final[
    tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str], str], ...]
] = ((_ROLE_PRETEND, _reword_role_pretend, _ROLE_PRETEND_REASON),)


def find_known_false_positives(text: str) -> list[Finding]:
    """Return the phrases this connector can rewrite without changing what the document means."""
    findings: list[Finding] = []
    for pattern, reword, reason in KNOWN_FALSE_POSITIVES:
        for match in pattern.finditer(text):
            findings.append(
                Finding(original=match.group(0), replacement=reword(match), reason=reason)
            )
    return findings


def rewrite_known_false_positives(text: str) -> str:
    """Return the document with every known false positive replaced, meaning preserved.

    Deterministic: the same input always produces the same output, because an applicant who
    approves a preview must get exactly that document installed.
    """
    rewritten = text
    for pattern, reword, _reason in KNOWN_FALSE_POSITIVES:
        rewritten = pattern.sub(reword, rewritten)
    return rewritten


def vet(text: str, *, adapter: Any, workspace: Path) -> VetOutcome:
    """Ask the runtime whether it would load this document, rewriting one known class if not.

    The sequence is scan, rewrite, **rescan**. The second scan is the point: this module's own
    pattern list is a reading of somebody else's heuristics, and a rewrite that did not actually
    clear the finding must fail exactly like no rewrite at all.

    A runtime with no scanner is not accepted. Returning "clean" there would disable the one gate
    that catches this failure on precisely the installations that lack it.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    candidate = workspace / CANDIDATE_FILENAME
    try:
        candidate.write_text(text, encoding="utf-8", newline="\n")
        try:
            findings = adapter.scan_context(candidate)
        except NotImplementedError as error:
            return VetOutcome(
                accepted=False,
                text=text,
                findings=[str(error)],
                scanner_available=False,
            )
        if not findings:
            return VetOutcome(accepted=True, text=text)

        rewritten = rewrite_known_false_positives(text)
        if rewritten == text:
            # Nothing this connector knows how to fix. Stop rather than install a document the
            # runtime has already said it will discard.
            return VetOutcome(accepted=False, text=text, findings=list(findings))

        candidate.write_text(rewritten, encoding="utf-8", newline="\n")
        remaining = adapter.scan_context(candidate)
        if remaining:
            return VetOutcome(
                accepted=False, text=rewritten, rewritten=True, findings=list(remaining)
            )
        return VetOutcome(accepted=True, text=rewritten, rewritten=True)
    finally:
        candidate.unlink(missing_ok=True)


def describe(outcome: VetOutcome, *, display_name: str) -> list[str]:
    """Return the lines an applicant is shown about a scan, in their own terms."""
    if outcome.accepted and not outcome.rewritten:
        return [f"  {display_name} checked this document and raised nothing."]
    if outcome.accepted:
        return [
            f"  {display_name} refused an earlier wording of this document.",
            "  The affected phrases were reworded to keep the same boundary without tripping its",
            "  impersonation check, and it accepted the result. The preview below is what",
            "  will be installed.",
        ]
    if not outcome.scanner_available:
        return [
            f"  {display_name} publishes no context-file check this connector can run, so there is",
            "  no way to know whether it would load this document. Nothing was written.",
        ]
    return [
        f"  {display_name} refused this document and would silently ignore it:",
        *[f"    {finding}" for finding in outcome.findings],
        "  Nothing was written. Reword the affected instruction and try again.",
    ]
