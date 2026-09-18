"""The local soul: a profile's personality and operating instructions, written on this machine.

A soul is the instruction document an agent runtime loads before it does anything — Hermes calls it
`SOUL.md` and auto-injects it. This module builds one from a short questionnaire, or installs one
the applicant already wrote, and it never sends any of it anywhere. Nothing here opens a socket.

**What a soul is not.** It is instruction text a runtime reads, not an enforcement boundary. Asking
an agent in prose to stay inside some limit does not make it do so, and this module says as much in
the document it generates. Nothing in AgentNexus checks, signs, or relies on a soul: the identity,
the key, and the signed request path are unaffected by every operation here.

**Ownership, because it decides what may be overwritten.** The file belongs to the applicant, not
to AgentNexus, and it usually exists before this module ever runs — `hermes profile create` writes
its own stock template into every new profile. So "no soul yet" is the rare case, not the common
one, and an unconditional write would routinely destroy somebody's work. What this module tracks
instead is a digest of the content *it* last wrote, kept in the profile record:

* the file matches that digest — AgentNexus wrote it and nobody has edited it since, so an
  identical rewrite is a no-op and a different one is an ordinary update;
* the file does not match, or there is no digest — the content is the applicant's or the runtime's,
  and replacing it needs a diff, an explicit confirmation, and a backup, every time.

Removing an AgentNexus profile never deletes a runtime soul: the file lives in the runtime's own
profile directory, which this connector did not create and does not own.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import os
import re
import shutil
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

#: The largest answer the questionnaire accepts, in characters. Generous for a paragraph, far
#: below anything that would make the generated document unreadable.
MAX_ANSWER_LENGTH: Final = 2000

#: The largest soul this module will write. A runtime injects the whole file into every prompt, so
#: an enormous one is a bill and a truncation risk rather than a richer personality.
MAX_SOUL_BYTES: Final = 64 * 1024

#: The largest file `soul import` will read. Deliberately larger than what it will then write, so
#: an oversized import is refused with "too large" rather than silently truncated.
MAX_IMPORT_BYTES: Final = 256 * 1024

#: How many backups of one profile's soul are kept before the oldest is pruned. Enough to undo a
#: bad afternoon; not so many that a soul directory becomes an archive nobody reads.
MAX_BACKUPS: Final = 20

#: The characters allowed in soul text besides printable ones: a tab and a newline. Everything
#: else in the C0/C1 range is refused rather than stripped — in particular `\x1b`, which is how a
#: crafted document repaints somebody's terminal while they are reading a "preview".
ALLOWED_CONTROL_CHARACTERS: Final = frozenset({"\t", "\n"})


class SoulError(Exception):
    """A soul operation that must not proceed, with the one thing to do about it."""

    def __init__(self, message: str, *, recovery: str | None = None) -> None:
        """Build a failure that knows how it should be recovered from."""
        super().__init__(message)
        self.recovery = recovery


# ---------------------------------------------------------------------------------------------
# Text safety
# ---------------------------------------------------------------------------------------------


def _describe_control_character(character: str) -> str:
    """Name a refused character in a way that is safe to print and useful to read."""
    if character == "\x1b":
        return "an escape character (0x1B), which a terminal would interpret as a command"
    return f"a control character (0x{ord(character):02X})"


def reject_control_characters(text: str, *, what: str) -> None:
    """Refuse text carrying control characters, naming the first one found.

    Refused rather than stripped, for two reasons. Stripping changes content the applicant wrote
    without telling them, and a document that renders differently from what they approved defeats
    the point of the preview. And an escape sequence in a file that is about to be shown as a
    "diff" is an attack on the reviewer, not a formatting quirk.
    """
    for index, character in enumerate(text):
        if character in ALLOWED_CONTROL_CHARACTERS:
            continue
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"} or character == "\x7f":
            message = (
                f"{what} contains {_describe_control_character(character)} at position {index}."
            )
            raise SoulError(
                message,
                recovery=(
                    "Remove it and try again. Ordinary text, tabs and line breaks are fine; "
                    "nothing else is, because this text is shown in a terminal."
                ),
            )


def normalise_newlines(text: str) -> str:
    """Return `text` with one newline convention, so a digest means the same on every platform.

    Without this, the identical soul written on Windows and read on Linux would compare unequal,
    and "did anyone edit this since we wrote it" would answer wrongly on every checkout.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def soul_digest(text: str) -> str:
    """Digest content the way this module compares it: normalised text, never raw bytes."""
    return hashlib.sha256(normalise_newlines(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# The questionnaire
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Question:
    """One question, and everything needed to ask it and to render its answer."""

    key: str
    heading: str
    prompt: str
    guidance: str
    multiline: bool = False
    required: bool = False


#: The questionnaire. Eight questions, ordered so the easy ones come first and the applicant is
#: warmed up by the time they reach autonomy and escalation. Short enough to finish in one sitting
#: — a questionnaire nobody completes produces no soul at all, which is the real failure mode.
QUESTIONS: Final[tuple[Question, ...]] = (
    Question(
        key="identity",
        heading="Who this agent is",
        prompt="In a sentence or two, who is this agent?",
        guidance="A name and a short self-description. This opens the document.",
        required=True,
    ),
    Question(
        key="purpose",
        heading="Purpose and responsibilities",
        prompt="What is it for, and what is it responsible for?",
        guidance="What it should be doing on AgentNexus and anywhere else it operates.",
        multiline=True,
        required=True,
    ),
    Question(
        key="interests",
        heading="Subjects and interests",
        prompt="Which subjects should it engage with?",
        guidance="Topics, categories, kinds of discussion. Comma-separated is fine.",
        multiline=True,
    ),
    Question(
        key="voice",
        heading="Voice and communication style",
        prompt="How should it write?",
        guidance="Language, tone, length, formality. For example: English, concise, plain.",
        multiline=True,
    ),
    Question(
        key="principles",
        heading="Behavioural principles",
        prompt="What principles should guide its behaviour?",
        guidance="One per line. These are the things it should hold to when judgement is needed.",
        multiline=True,
    ),
    Question(
        key="boundaries",
        heading="Boundaries and prohibited behaviour",
        prompt="What must it never do?",
        guidance="One per line. Be concrete; vague limits are the ones that get argued around.",
        multiline=True,
    ),
    Question(
        key="uncertainty",
        heading="Uncertainty, disagreement and mistakes",
        prompt="How should it handle being unsure, disagreed with, or wrong?",
        guidance="What to do when it does not know, when someone pushes back, when it erred.",
        multiline=True,
    ),
    Question(
        key="autonomy",
        heading="Autonomy and human escalation",
        prompt="How much may it decide alone, and when must it ask a human?",
        guidance="Name the situations that require you, specifically, before it acts.",
        multiline=True,
        required=True,
    ),
)

#: Answers, by question key. Deliberately a plain mapping: it lives in memory for the length of
#: one command and is never written to disk, so there is nothing to clean up afterwards.
Answers = dict[str, str]


class QuestionnaireCancelledError(Exception):
    """The applicant chose to stop. Nothing has been written, and nothing needs undoing."""


def validate_answer(question: Question, value: str) -> str:
    """Return the cleaned answer, or refuse it. Cleaning is whitespace only."""
    text = normalise_newlines(value).strip()
    reject_control_characters(text, what=f"The answer to {question.key!r}")
    if len(text) > MAX_ANSWER_LENGTH:
        message = (
            f"The answer to {question.key!r} is {len(text)} characters; "
            f"the maximum is {MAX_ANSWER_LENGTH}."
        )
        raise SoulError(message, recovery="Shorten it and answer again.")
    if question.required and not text:
        message = f"{question.heading} is required."
        raise SoulError(message, recovery="Answer it, or cancel with Ctrl+C.")
    return text


def ask_questionnaire(
    *,
    reader: Callable[[str], str],
    stdout: TextIO,
    questions: Sequence[Question] = QUESTIONS,
) -> Answers:
    """Ask every question, returning the answers. Raises `QuestionnaireCancelledError` on `cancel`.

    A refused answer is asked again rather than aborting the whole questionnaire: losing seven
    good answers because the eighth was too long is not a reasonable thing to do to somebody.
    """
    stdout.write(
        "\nThis builds a local instruction document for this agent.\n"
        "Everything you type stays on this computer. None of it is sent to AgentNexus.\n"
        "Press Enter to leave an optional question blank, or type `cancel` to stop.\n"
    )
    answers: Answers = {}
    for number, question in enumerate(questions, start=1):
        stdout.write(f"\n[{number}/{len(questions)}] {question.heading}\n")
        stdout.write(f"  {question.guidance}\n")
        if question.multiline:
            stdout.write("  Several lines are fine; finish with a blank line.\n")
        for attempt in range(1, MAX_ANSWER_ATTEMPTS + 1):
            raw = _read_answer(reader, question, stdout)
            if raw.strip().lower() == "cancel":
                raise QuestionnaireCancelledError
            try:
                answers[question.key] = validate_answer(question, raw)
            except SoulError as error:
                stdout.write(f"  {error}\n")
                if error.recovery:
                    stdout.write(f"  {error.recovery}\n")
                if attempt == MAX_ANSWER_ATTEMPTS:
                    # Bounded rather than "until it is right". An input that has reached EOF —
                    # a closed pipe, a script that ran out of lines — answers the same way for
                    # ever, and an unbounded retry would hang the connector instead of stopping.
                    message = (
                        f"{question.heading} was not answered acceptably after "
                        f"{MAX_ANSWER_ATTEMPTS} attempts."
                    )
                    raise SoulError(
                        message,
                        recovery="Nothing was written. Run the command again when ready.",
                    ) from error
                continue
            break
    return answers


#: The most continuation lines one multi-line answer may have. A bound rather than a preference:
#: without it, a reader that never returns a blank line — a piped script, a stuck terminal — spins
#: for ever inside a loop that is waiting for a human to press Enter.
MAX_ANSWER_LINES: Final = 200

#: How many times one question may be re-asked after a refused answer. Enough for a person to
#: correct a typo; bounded so a reader that has hit EOF cannot spin here for ever.
MAX_ANSWER_ATTEMPTS: Final = 5


def _read_answer(reader: Callable[[str], str], question: Question, stdout: TextIO) -> str:
    """Read one answer, gathering continuation lines for a multi-line question.

    `cancel` ends the *whole* questionnaire, so it has to be recognised here as well as by the
    caller: a multi-line question that treated it as ordinary text would keep asking for the next
    line and never reach the check.
    """
    first = reader(f"  {question.prompt} ")
    if not question.multiline or first.strip().lower() == "cancel":
        return first
    lines = [first]
    while lines[-1].strip() and len(lines) <= MAX_ANSWER_LINES:
        nxt = reader("  … ")
        if nxt.strip().lower() == "cancel":
            return "cancel"
        lines.append(nxt)
    if len(lines) > MAX_ANSWER_LINES:
        stdout.write(f"  Stopping this answer at {MAX_ANSWER_LINES} lines.\n")
    return "\n".join(line for line in lines if line.strip())


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------

#: The line that marks a document this connector generated. It carries no timestamp and no
#: identifier on purpose: the rendering has to be a pure function of the answers, or "did anyone
#: change this since we wrote it" could never be answered by comparing digests.
GENERATED_MARKER: Final = "<!-- Generated by agentnexus-connector from a local questionnaire. -->"

#: Printed inside every generated soul. An agent's instructions are not a security control, and a
#: document that implied otherwise would be worse than one that said nothing.
_LIMITS_NOTE: Final = (
    "These are instructions, not enforcement. They describe how this agent is meant to behave; "
    "they cannot make it behave that way, and AgentNexus does not read, verify, or rely on this "
    "file. Real limits come from what the agent is given access to."
)


def render_soul(answers: Answers, *, questions: Sequence[Question] = QUESTIONS) -> str:
    """Render answers into Markdown. A pure function: same answers, same bytes, every time.

    Nothing generated here varies — no date, no host, no profile name, no ordering that depends on
    a dictionary's insertion order. That is what makes an unchanged soul detectable as unchanged.
    """
    lines: list[str] = ["# Soul", "", GENERATED_MARKER, ""]
    for question in questions:
        answer = answers.get(question.key, "").strip()
        if not answer:
            continue
        lines.append(f"## {question.heading}")
        lines.append("")
        lines.extend(_render_answer(answer))
        lines.append("")
    lines.append("## About this document")
    lines.append("")
    lines.append(_LIMITS_NOTE)
    lines.append("")
    return "\n".join(lines)


def _render_answer(answer: str) -> list[str]:
    """Render one answer, turning a genuinely multi-line answer into a list.

    A single line stays a paragraph. Several lines become bullets, because that is what somebody
    typing one principle per line meant, and a paragraph of run-together principles reads as one.
    """
    parts = [line.strip() for line in answer.split("\n") if line.strip()]
    if len(parts) <= 1:
        return [parts[0]] if parts else []
    return [f"- {part}" for part in parts]


def is_generated(text: str) -> bool:
    """Whether this content came from the questionnaire rather than from a person or a runtime."""
    return GENERATED_MARKER in text


def validate_soul_text(text: str, *, what: str = "The soul") -> str:
    """Check content that is about to be written, and return it normalised."""
    normalised = normalise_newlines(text)
    reject_control_characters(normalised, what=what)
    encoded = normalised.encode("utf-8")
    if len(encoded) > MAX_SOUL_BYTES:
        message = f"{what} is {len(encoded)} bytes; the maximum is {MAX_SOUL_BYTES}."
        raise SoulError(message, recovery="Shorten it and try again.")
    if not normalised.strip():
        message = f"{what} is empty."
        raise SoulError(message, recovery="An empty soul would say nothing; write something.")
    return normalised


# ---------------------------------------------------------------------------------------------
# Reading what is already there
# ---------------------------------------------------------------------------------------------


def _is_reparse_point(path: Path) -> bool:
    """Whether `path` is a symlink, a junction, or any other reparse point.

    The same check `profiles.py` applies to profile directories, for the same reason: on Windows
    a junction is reported as an ordinary directory, and it is the cheapest way to make a write
    land somewhere its author did not choose.
    """
    try:
        info = path.lstat()
    except OSError:
        return False
    import stat as stat_module

    if stat_module.S_ISLNK(info.st_mode):
        return True
    attributes = int(getattr(info, "st_file_attributes", 0))
    return bool(attributes & int(getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def require_real_location(path: Path) -> None:
    """Refuse a soul path whose *directory* is a link, not only one whose file is.

    Checked separately because the two escapes are different, and the directory one is the
    dangerous half: a junction on `…/profiles/agent2` leaves `…/agent2/SOUL.md` looking like a
    perfectly ordinary file while every read and write lands somewhere else entirely. On Windows a
    junction needs no privilege to create and `is_symlink()` reports it as a directory.
    """
    directory = path.parent
    if _is_reparse_point(directory):
        message = f"{directory} is a link rather than a real directory."
        raise SoulError(
            message,
            recovery=(
                "AgentNexus will not read or write a soul through a link, because the file it "
                "reached would not be the one it named. Nothing was changed."
            ),
        )


def read_existing_soul(path: Path) -> str | None:
    """Read the soul already at `path`, or `None` when there is none.

    A path that is not a plain file is a refusal, not a `None`: a soul that is a symlink, a
    junction or a device is not a soul this module may back up and replace, and treating it as
    absent would lead straight to writing through it.
    """
    require_real_location(path)
    if not path.exists() and not path.is_symlink():
        return None
    if _is_reparse_point(path):
        message = f"{path} is a link rather than a real file."
        raise SoulError(
            message,
            recovery=(
                "AgentNexus will not write through a link. Inspect that path and replace the "
                "link with a real file, or leave the soul alone."
            ),
        )
    if not path.is_file():
        message = f"{path} is not a regular file."
        raise SoulError(message, recovery="Inspect it; AgentNexus will not overwrite it.")
    try:
        raw = path.read_bytes()
    except OSError as error:
        message = f"{path} could not be read: {error}"
        raise SoulError(message) from error
    if len(raw) > MAX_IMPORT_BYTES:
        message = f"{path} is {len(raw)} bytes, over the {MAX_IMPORT_BYTES} limit."
        raise SoulError(message, recovery="AgentNexus will not read a file that large.")
    try:
        return normalise_newlines(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        message = f"{path} is not UTF-8 text."
        raise SoulError(message, recovery="A soul is a Markdown document.") from error


def read_import_file(path: Path) -> str:
    """Read a file the applicant explicitly named, refusing everything that is not one.

    Only the named path is read. Nothing is followed, globbed, or discovered: an import that went
    looking for files would be an import that could be pointed at one the applicant did not mean.
    The source is never modified and never deleted, here or anywhere else in this module.
    """
    candidate = Path(path).expanduser()
    if not candidate.exists() and not candidate.is_symlink():
        message = f"{candidate} does not exist."
        raise SoulError(message, recovery="Check the path and try again.")
    if _is_reparse_point(candidate):
        message = f"{candidate} is a link rather than a real file."
        raise SoulError(
            message,
            recovery="Point at the real file instead; AgentNexus does not follow links here.",
        )
    if candidate.is_dir():
        message = f"{candidate} is a directory."
        raise SoulError(message, recovery="Name the Markdown file itself.")
    if not candidate.is_file():
        # A device, a pipe, a socket. `read_bytes` on one of these can block for ever.
        message = f"{candidate} is not a regular file."
        raise SoulError(message, recovery="Name an ordinary Markdown file.")
    raw = candidate.read_bytes()
    if len(raw) > MAX_IMPORT_BYTES:
        message = f"{candidate} is {len(raw)} bytes, over the {MAX_IMPORT_BYTES} limit."
        raise SoulError(message, recovery="Import a smaller file.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        message = f"{candidate} is not UTF-8 text."
        raise SoulError(message, recovery="A soul is a UTF-8 Markdown document.") from error
    # Validated as content, not as instructions. Markdown is never executed, rendered, or
    # interpreted here; it is text that gets checked, shown, and written.
    return validate_soul_text(text, what=f"{candidate}")


# ---------------------------------------------------------------------------------------------
# Diffs and previews
# ---------------------------------------------------------------------------------------------


def diff_souls(before: str | None, after: str, *, path: Path) -> list[str]:
    """Build the unified diff a person reviews before anything is replaced."""
    original = normalise_newlines(before or "").splitlines(keepends=True)
    updated = normalise_newlines(after).splitlines(keepends=True)
    return list(
        difflib.unified_diff(
            original,
            updated,
            fromfile=f"{path} (current)",
            tofile=f"{path} (proposed)",
            n=3,
        )
    )


def write_preview(lines: Sequence[str], stdout: TextIO, *, limit: int = 200) -> None:
    """Print a diff, truncating a very long one rather than scrolling it out of reach."""
    if not lines:
        stdout.write("  (no change)\n")
        return
    for line in lines[:limit]:
        stdout.write(f"  {line.rstrip()}\n")
    if len(lines) > limit:
        stdout.write(f"  … {len(lines) - limit} more diff lines\n")


# ---------------------------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------------------------

#: A backup file name: the profile it belongs to, then when it was taken. The profile is in the
#: name as well as in the directory so a file that is moved is still attributable.
#: The `-<n>` tail is not decoration: two replacements inside one second collide on the timestamp,
#: and a pattern that did not admit the counter would quietly stop listing the second backup —
#: which is the one somebody would be looking for.
_BACKUP_PATTERN: Final = re.compile(
    r"^soul-(?P<profile>[a-z][a-z0-9]{0,31})-(?P<stamp>[0-9]{8}T[0-9]{6}Z(?:-\d+)?)\.md$"
)


def backup_directory(profile_root: Path) -> Path:
    """Where one profile's soul backups live. Inside that profile, never shared."""
    return profile_root / "backups" / "soul"


def take_backup(path: Path, *, profile: str, backups: Path) -> Path | None:
    """Copy the current soul aside, byte for byte, before anything replaces it."""
    if not path.is_file():
        return None
    backups.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backups / f"soul-{profile}-{stamp}.md"
    counter = 1
    while destination.exists():
        destination = backups / f"soul-{profile}-{stamp}-{counter}.md"
        counter += 1
    shutil.copy2(path, destination)
    _prune_backups(backups, profile)
    return destination


def _prune_backups(backups: Path, profile: str) -> None:
    """Keep the newest `MAX_BACKUPS`, so a soul directory does not grow without end."""
    existing = list_backups(backups, profile)
    for stale in existing[MAX_BACKUPS:]:
        stale.unlink(missing_ok=True)


def list_backups(backups: Path, profile: str) -> list[Path]:
    """List this profile's backups, newest first. Another profile's are never returned."""
    if not backups.is_dir():
        return []
    matched = [
        entry
        for entry in backups.iterdir()
        if entry.is_file()
        and (match := _BACKUP_PATTERN.match(entry.name)) is not None
        and match.group("profile") == profile
    ]
    return sorted(matched, key=lambda entry: entry.name, reverse=True)


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """What a write did, so the caller can report it precisely and record the digest."""

    changed: bool
    digest: str
    backup: Path | None
    detail: str


def install_soul(
    text: str,
    *,
    path: Path,
    profile: str,
    backups: Path,
    expected_current: str | None = None,
) -> WriteOutcome:
    """Write a soul atomically, keeping a backup, and restore it if anything goes wrong.

    The ordering is the design:

    1. validate the new content, so nothing is touched on account of text that cannot be written;
    2. re-read the current file *now* rather than trusting what the caller saw, because the diff
       the applicant approved may be older than the file;
    3. back it up byte for byte;
    4. write a temporary file in the same directory and `os.replace` it into place, which is
       atomic on both platforms — a reader sees the old file or the new one, never a half-written
       one, and an interrupted run leaves the original intact;
    5. read it back and compare digests, restoring the backup if it does not match.

    `expected_current` is the content the caller showed the applicant. If the file has changed
    since, this refuses rather than replacing something nobody reviewed.
    """
    content = validate_soul_text(text)
    digest = soul_digest(content)

    current = read_existing_soul(path)
    if expected_current is not None and normalise_newlines(expected_current) != (current or ""):
        message = f"{path} changed while it was being reviewed."
        raise SoulError(
            message,
            recovery="Nothing was written. Run the command again to see the current content.",
        )

    if current is not None and soul_digest(current) == digest:
        return WriteOutcome(
            changed=False, digest=digest, backup=None, detail="already exactly this; unchanged"
        )

    directory = path.parent
    if not directory.is_dir():
        message = f"{directory} does not exist."
        raise SoulError(
            message,
            recovery="The runtime profile it belongs to is missing. Re-run setup for this profile.",
        )

    backup = take_backup(path, profile=profile, backups=backups)
    temporary = directory / f".{path.name}.agentnexus-{os.getpid()}"
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
        written = read_existing_soul(path)
        if written is None or soul_digest(written) != digest:
            message = f"{path} does not contain what was just written."
            raise SoulError(message)
    except BaseException:
        temporary.unlink(missing_ok=True)
        _restore(backup, path)
        raise
    return WriteOutcome(
        changed=True,
        digest=digest,
        backup=backup,
        detail="written" if current is None else "replaced",
    )


def _restore(backup: Path | None, path: Path) -> None:
    """Put a backup back, best effort, on the failure path."""
    if backup is not None and backup.is_file():
        shutil.copy2(backup, path)


def restore_backup(backup: Path, *, path: Path, profile: str, backups: Path) -> WriteOutcome:
    """Restore one backup, through the same write path everything else uses.

    Restoring is an ordinary replacement: it takes its own backup first, so undoing a restore is
    possible too, and it goes through `install_soul` rather than copying the file directly.
    """
    if not backup.is_file():
        message = f"{backup} is not a file."
        raise SoulError(message, recovery="List the backups again and choose one of those.")
    match = _BACKUP_PATTERN.match(backup.name)
    if match is None or match.group("profile") != profile:
        message = f"{backup.name} is not a backup of the {profile!r} profile's soul."
        raise SoulError(
            message,
            recovery=(
                "A soul is restored only into the profile it was taken from. Check the profile "
                "name, or choose a backup from this profile's list."
            ),
        )
    text = read_existing_soul(backup)
    if text is None:
        message = f"{backup} could not be read."
        raise SoulError(message)
    return install_soul(text, path=path, profile=profile, backups=backups)


def summarise(text: str | None) -> str:
    """One line describing a soul, for a status listing. Never the content itself."""
    if text is None:
        return "none"
    kind = "generated by the questionnaire" if is_generated(text) else "custom"
    lines = len(normalise_newlines(text).strip().splitlines())
    return f"{kind}, {len(text.encode('utf-8'))} bytes, {lines} lines"


def answers_fingerprint(answers: Answers) -> str:
    """Digest the answers, so a test can assert determinism without printing any content."""
    payload = "".join(f"{key}={answers[key]}" for key in sorted(answers))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def scrub(answers: Answers) -> None:
    """Drop questionnaire answers as soon as they are no longer needed.

    They only ever lived in this dictionary — nothing writes them to disk — so clearing it is the
    whole of the cleanup, on success, on cancellation, and on failure alike.
    """
    answers.clear()


__all__ = [
    "ALLOWED_CONTROL_CHARACTERS",
    "GENERATED_MARKER",
    "MAX_ANSWER_LENGTH",
    "MAX_BACKUPS",
    "MAX_IMPORT_BYTES",
    "MAX_SOUL_BYTES",
    "QUESTIONS",
    "Answers",
    "Question",
    "QuestionnaireCancelledError",
    "SoulError",
    "WriteOutcome",
    "answers_fingerprint",
    "ask_questionnaire",
    "backup_directory",
    "diff_souls",
    "install_soul",
    "is_generated",
    "list_backups",
    "normalise_newlines",
    "read_existing_soul",
    "read_import_file",
    "reject_control_characters",
    "render_soul",
    "require_real_location",
    "restore_backup",
    "scrub",
    "soul_digest",
    "summarise",
    "take_backup",
    "validate_answer",
    "validate_soul_text",
    "write_preview",
]
