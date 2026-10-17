"""Read one local image for an upload, and refuse everything else before a key is loaded.

agntnexus/agentnexus#266. A runtime names a file by its path; the bridge reads it. The path comes
from a model, so every check here treats it as untrusted input:

* the path is a short, absolute, ordinary local path: not a network or device path, not relative,
  and free of control characters;
* the file is a regular file, not a symbolic link, directory, pipe or device, and is not the
  agent's own private key;
* its size is judged from metadata before any byte is read, and it is between 1 byte and the
  server's limit;
* its type is judged from its content, never its name, and only JPEG, PNG and WebP pass.

Every refusal is a :class:`MediaSourceError` whose message never contains the path, so a refused
path cannot reach a log, a result or a model.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agentnexus_sdk.multipart import MAX_SOURCE_BYTES

#: Longest path accepted, in characters. Bounds what the file system is asked to resolve.
MAX_PATH_CHARACTERS: Final = 4096

#: How much is read per system call once the size has been judged.
_READ_CHUNK_BYTES: Final = 65_536

_PATH_SYNTAX: Final = (
    "file_path must be a non-empty string of at most "
    f"{MAX_PATH_CHARACTERS} characters without control characters."
)
_NETWORK_OR_DEVICE: Final = "file_path must not name a network or device path."
_RELATIVE: Final = "file_path must be an absolute path on this machine."
_MISSING: Final = "file_path does not name a readable file."
_SYMLINK: Final = "file_path must not be a symbolic link."
_NOT_REGULAR: Final = "file_path must name a regular file, not a directory, device or pipe."
_EMPTY: Final = "The image file is empty."
_TOO_LARGE: Final = f"The image file must be at most {MAX_SOURCE_BYTES} bytes."
_CHANGED: Final = "The image file changed while it was being opened; nothing was sent."
_WITHHELD: Final = "file_path must not name the agent's private key file."
_NOT_AN_IMAGE: Final = "The image file must be a JPEG, PNG or WebP image, judged from its content."


class MediaSourceError(ValueError):
    """The named file cannot be uploaded. The message never carries the path itself."""


@dataclass(frozen=True, slots=True)
class ImageSource:
    """One image, read and judged, ready for the signed upload."""

    data: bytes
    mime: str


def detect_image_mime(data: bytes) -> str | None:
    """Return the MIME type the content declares, or None for anything not supported.

    Magic bytes only. The bridge never trusts a file name, and the server validates the image
    again; this decides only what the bridge is willing to send at all.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_image_source(path: object, *, withheld: Iterable[Path] = ()) -> ImageSource:
    """Read and judge one image named by `path`. Nothing is read until every metadata check passes.

    `withheld` lists files that must never be read as an image, such as the agent's own key. The
    comparison uses resolved paths, so a differently spelt route to the same file is refused too.
    """
    if (
        not isinstance(path, str)
        or not 0 < len(path) <= MAX_PATH_CHARACTERS
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise MediaSourceError(_PATH_SYNTAX)
    if path.startswith(("\\\\", "//")):
        # A UNC path makes Windows contact a remote host; a `\\.\` path names a device.
        raise MediaSourceError(_NETWORK_OR_DEVICE)
    candidate = Path(path)
    if not candidate.is_absolute():
        raise MediaSourceError(_RELATIVE)

    try:
        before = os.lstat(candidate)
    except OSError:
        raise MediaSourceError(_MISSING) from None
    if stat.S_ISLNK(before.st_mode):
        raise MediaSourceError(_SYMLINK)
    if not stat.S_ISREG(before.st_mode):
        raise MediaSourceError(_NOT_REGULAR)
    if before.st_size == 0:
        raise MediaSourceError(_EMPTY)
    if before.st_size > MAX_SOURCE_BYTES:
        raise MediaSourceError(_TOO_LARGE)
    if _is_withheld(candidate, withheld):
        raise MediaSourceError(_WITHHELD)

    data = _read_regular_file(candidate, before)
    mime = detect_image_mime(data)
    if mime is None:
        raise MediaSourceError(_NOT_AN_IMAGE)
    return ImageSource(data=data, mime=mime)


def _is_withheld(candidate: Path, withheld: Iterable[Path]) -> bool:
    """Report whether the candidate resolves to one of the withheld files."""
    resolved = os.path.normcase(os.path.realpath(candidate))
    return any(os.path.normcase(os.path.realpath(item)) == resolved for item in withheld)


def _read_regular_file(candidate: Path, before: os.stat_result) -> bytes:
    """Open the file without following a link, check it is still the same regular file, and read.

    The open refuses a final symbolic link where the platform can (`O_NOFOLLOW`) and never blocks
    on a pipe (`O_NONBLOCK`). The identity and size are checked again on the descriptor, so a file
    swapped between the first check and the open is refused rather than read.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(candidate, flags)
    except OSError:
        raise MediaSourceError(_MISSING) from None
    chunks: list[bytes] = []
    total = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise MediaSourceError(_CHANGED)
        if opened.st_size > MAX_SOURCE_BYTES:
            raise MediaSourceError(_TOO_LARGE)
        # One byte more than the limit is requested, so a file that grew is seen to be too large.
        while total <= MAX_SOURCE_BYTES:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(descriptor)
    if total > MAX_SOURCE_BYTES:
        raise MediaSourceError(_TOO_LARGE)
    if total == 0:
        raise MediaSourceError(_EMPTY)
    return b"".join(chunks)
