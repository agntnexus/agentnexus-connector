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
_TOO_LARGE: Final = (
    f"The image file is larger than {MAX_SOURCE_BYTES} bytes. Produce a smaller image, or save it "
    "as a JPEG at a lower quality, and upload that instead."
)
_CHANGED: Final = "The image file changed while it was being opened; nothing was sent."
_WITHHELD: Final = "file_path must not name the agent's private key file."
_NOT_AN_IMAGE: Final = "The image file must be a JPEG, PNG or WebP image, judged from its content."
_TRAVERSAL: Final = "file_path must not contain '.' or '..' components."
_OUTSIDE: Final = "file_path must lie inside the configured upload directory."
_LINK_ON_PATH: Final = "file_path must not pass through a symbolic link or junction."

_DIRECTORY_UNSET: Final = (
    "AGENTNEXUS_MEDIA_UPLOAD_DIR is not set, so this runtime cannot upload images. Set it to the "
    "one absolute directory that images may be uploaded from."
)
_DIRECTORY_RELATIVE: Final = "AGENTNEXUS_MEDIA_UPLOAD_DIR must be an absolute path on this machine."
_DIRECTORY_NOT_NORMAL: Final = (
    "AGENTNEXUS_MEDIA_UPLOAD_DIR must not contain '.' or '..' components."
)
_DIRECTORY_MISSING: Final = "AGENTNEXUS_MEDIA_UPLOAD_DIR does not name an existing directory."
_DIRECTORY_NOT_REAL: Final = (
    "AGENTNEXUS_MEDIA_UPLOAD_DIR must name a real directory, not a file or a link."
)

#: The Windows attribute that marks a reparse point, which includes a directory junction.
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400


class MediaSourceError(ValueError):
    """The named file cannot be uploaded. The message never carries the path itself."""


class UploadDirectoryError(ValueError):
    """The operator's upload directory is unusable. The message never carries the setting."""


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


def resolve_upload_directory(value: str | None) -> Path:
    """Return the operator's upload directory, or refuse a setting that cannot be one.

    The directory must be an absolute, normalised path to a real directory: not a file, not a link,
    and not a network path. Its value is never echoed in a refusal.
    """
    text = (value or "").strip()
    if not text:
        raise UploadDirectoryError(_DIRECTORY_UNSET)
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise UploadDirectoryError(_DIRECTORY_RELATIVE)
    if text.startswith(("\\\\", "//")):
        raise UploadDirectoryError(_DIRECTORY_RELATIVE)
    directory = Path(text)
    if not directory.is_absolute():
        raise UploadDirectoryError(_DIRECTORY_RELATIVE)
    if _has_dot_components(text):
        raise UploadDirectoryError(_DIRECTORY_NOT_NORMAL)
    try:
        status = os.lstat(directory)
    except OSError:
        raise UploadDirectoryError(_DIRECTORY_MISSING) from None
    if (
        stat.S_ISLNK(status.st_mode)
        or _is_reparse_point(status)
        or not stat.S_ISDIR(status.st_mode)
    ):
        raise UploadDirectoryError(_DIRECTORY_NOT_REAL)
    return directory


def read_image_source(
    path: object, *, directory: Path, withheld: Iterable[Path] = ()
) -> ImageSource:
    """Read and judge one image named by `path`, which must lie inside `directory`.

    Nothing is read until every check on the path and the file's metadata has passed. The path is
    judged by its components, never by a string prefix, and a link anywhere on it is refused: the
    walk from the root of the path to the file, and a second check on fully resolved paths, must
    both agree that the file lies inside the directory.

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
    if _has_dot_components(path):
        raise MediaSourceError(_TRAVERSAL)
    if not _is_within(candidate, directory):
        raise MediaSourceError(_OUTSIDE)
    _refuse_linked_ancestors(candidate)

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
    # The second containment check, on fully resolved paths: it must agree with the walk above.
    if not _is_within(os.path.realpath(candidate), Path(os.path.realpath(directory))):
        raise MediaSourceError(_OUTSIDE)
    if _is_withheld(candidate, withheld):
        raise MediaSourceError(_WITHHELD)

    data = _read_regular_file(candidate, before)
    mime = detect_image_mime(data)
    if mime is None:
        raise MediaSourceError(_NOT_AN_IMAGE)
    return ImageSource(data=data, mime=mime)


def _has_dot_components(text: str) -> bool:
    """Report whether a path, as written, contains a `.` or `..` component."""
    return any(part in (".", "..") for part in text.replace("\\", "/").split("/"))


def _is_within(child: str | Path, parent: Path) -> bool:
    """Report whether `child` lies strictly inside `parent`, comparing whole path components.

    A string prefix would accept `/a/images2/x.png` for `/a/images`; comparing components does not.
    The comparison is case-insensitive on platforms whose file names are.
    """
    child_parts = [os.path.normcase(part) for part in Path(child).parts]
    parent_parts = [os.path.normcase(part) for part in parent.parts]
    return len(child_parts) > len(parent_parts) and child_parts[: len(parent_parts)] == parent_parts


def _is_reparse_point(status: os.stat_result) -> bool:
    """Report whether a file-system entry is a reparse point, such as a junction (Windows only)."""
    return bool(getattr(status, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _refuse_linked_ancestors(candidate: Path) -> None:
    """Refuse a path on which any directory, from the root down to the file, is a link.

    The file itself is not included here: its own link is refused with its own message.
    """
    parts = candidate.parts
    for depth in range(1, len(parts)):
        ancestor = Path(*parts[:depth])
        try:
            status = os.lstat(ancestor)
        except OSError:
            raise MediaSourceError(_MISSING) from None
        if stat.S_ISLNK(status.st_mode) or _is_reparse_point(status):
            raise MediaSourceError(_LINK_ON_PATH)


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
