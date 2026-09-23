"""Termux/Android support: making a verified installation actually start on a phone.

## The failure this module exists to remove

On Android the dynamic linker is bionic, not glibc, and the two disagree about one thing that
matters here: where a freshly `dlopen`ed library may resolve CPython's symbols from. `cryptography`
publishes its Rust binding as an abi3 extension with no `DT_NEEDED` entry for libpython — the
correct choice on every platform where the interpreter exports its own symbols — so on a Termux
device the first import of it ends in::

    ImportError: dlopen failed: cannot locate symbol "PyModule_Type" referenced by
    .../cryptography/hazmat/bindings/_rust.abi3.so

and it ends there *after* the loader has verified the release signature, checked the artifact
digest, created the virtual environment and installed the wheel. Nothing about the trust chain
failed; the installation simply could not start.

The workaround an operator finds first is `LD_PRELOAD=$PREFIX/lib/libpython3.13.so`, and it is not
an acceptable permanent requirement: it has to be exported before every start, it hard-codes an
interpreter version, and it does not survive an agent runtime spawning the connector's MCP server
with a filtered environment — which is exactly how the connector is normally started.

## What is done instead

The same thing the preload does, done by the package itself and in-process: open the interpreter's
own shared library with `RTLD_GLOBAL` so its symbols are in the linker's global group before any
native extension is imported. `agentnexus_sdk/__init__.py` calls
`enable_native_extension_loading()` as its first statement, so every entry point inherits it — the
connector, the agent CLI and the MCP server a runtime spawns — with no variable to export and none
to lose.

## What is deliberately not done

Nothing is installed, downloaded, built or replaced, and no verification is relaxed, skipped or
reordered: this module runs after an artifact is already on disk and verified, and it has no
opinion about how it got there. On any platform that is not Android it does nothing at all.

## Why detection reads the interpreter and the device, never the environment

`PREFIX`, `TERMUX_VERSION` and `ANDROID_ROOT` are ordinary environment variables: a caller can set
them anywhere, so treating one as proof of a platform would let a caller choose which code path
this package takes. Nothing here reads any of them. What is read is what the interpreter was built
as, where it actually lives once symlinks are resolved, and which files the device has — and one
signal alone is never enough, because a cross-built interpreter is not a device and a path that
happens to exist is not an interpreter.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Files that exist on an Android device and on no desktop Linux. `/system/bin/linker64` and
#: `/system/bin/linker` are bionic itself; `/system/build.prop` is the platform's own description.
#: Probed as files rather than inferred from a variable, because a file is not a claim.
DEVICE_MARKERS: Final[tuple[str, ...]] = (
    "/system/bin/linker64",
    "/system/bin/linker",
    "/system/build.prop",
)

#: The `dlopen` mode that puts a library's symbols in the global group, which is the entire fix:
#: an extension loaded afterwards resolves `PyModule_Type` and its neighbours against it. Absent on
#: Windows, where `ctypes` has no such flag and this module never loads anything anyway.
GLOBAL_SYMBOLS: Final[int] = getattr(os, "RTLD_GLOBAL", 0)

#: An interpreter built for bionic: `aarch64-linux-android`, `armv7a-linux-androideabi`, and the
#: API-level suffixed spellings a toolchain may produce.
_ANDROID_TRIPLE: Final = re.compile(r"-android[a-z]*[0-9]*$")

#: A Termux-style userland: an application's own private directory, holding a `usr` prefix. The
#: package name is not pinned to `com.termux`, because the forks install themselves the same way
#: and the device evidence above is what makes this mean anything.
_TERMUX_PREFIX: Final = re.compile(
    r"^/data/data/[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*/files/usr(?:/|$)"
)

#: A shared object, including the versioned spellings such as `libpython3.13.so.1.0`. A static
#: build reports `libpython3.13.a`, which cannot be opened and must not be attempted.
_SHARED_OBJECT: Final = re.compile(r"\.so(?:\.[0-9]+)*$")

#: Anything that can open a library and publish its symbols. `ctypes.CDLL` in production; a test
#: passes its own so that the decision can be checked on a machine that is not a phone.
NativeLoader = Callable[[str, int], object]


def _configuration(name: str) -> str:
    """Return one interpreter build variable, or an empty string if this build has no such value.

    These come from the interpreter's own compiled-in configuration, not from the environment, and
    in a virtual environment they still describe the base interpreter — which is the one whose
    library has to be preloaded.
    """
    value = sysconfig.get_config_var(name)
    return str(value) if value else ""


@dataclass(frozen=True, slots=True)
class AndroidRuntime:
    """The runtime signals that decide whether this is a Termux/Android interpreter.

    A value rather than a function so that the decision is inspectable and testable: every case
    this package must get right can be written down as a set of signals, including the ones that
    have to be refused.
    """

    #: `sys.platform`. Termux reports `linux`, like any other bionic or glibc host.
    platform_name: str
    #: The build triple, for example `aarch64-linux-android`.
    host_triple: str
    #: Where the base interpreter lives once symlinks are resolved.
    base_prefix: str
    #: The interpreter's own library, for example `libpython3.13.so`.
    library_name: str
    #: The directory that library was installed into.
    library_directory: str
    #: Which of `DEVICE_MARKERS` are present on this machine.
    device_markers: tuple[str, ...]

    @classmethod
    def detect(cls) -> AndroidRuntime:
        """Read every signal from this interpreter and this machine. Reads no environment."""
        return cls(
            platform_name=sys.platform,
            host_triple=_configuration("HOST_GNU_TYPE"),
            base_prefix=os.path.realpath(sys.base_prefix),
            library_name=_configuration("LDLIBRARY"),
            library_directory=_configuration("LIBDIR"),
            device_markers=tuple(marker for marker in DEVICE_MARKERS if Path(marker).exists()),
        )

    @property
    def built_for_android(self) -> bool:
        """Whether this interpreter was compiled against bionic."""
        return bool(_ANDROID_TRIPLE.search(self.host_triple))

    @property
    def under_termux_prefix(self) -> bool:
        """Whether this interpreter runs out of a Termux-style application prefix."""
        return bool(_TERMUX_PREFIX.match(self.base_prefix))

    @property
    def is_android(self) -> bool:
        """Whether this is a Termux/Android runtime, on the evidence and not on a claim.

        Two independent kinds of evidence are required. The device has to be an Android device,
        and the interpreter has to be one of its own — built for bionic, or installed inside an
        application's prefix. Either alone is something else: a cross-compiled interpreter on a
        laptop, or an unrelated interpreter that happens to run on a phone.
        """
        if self.platform_name != "linux":
            return False
        if not self.device_markers:
            return False
        return self.built_for_android or self.under_termux_prefix

    def shared_library(self) -> Path | None:
        """Return the interpreter's own shared library, or `None` if there is nothing to preload.

        `None` is a normal answer rather than an error: an interpreter can be statically linked,
        and a build can report a library that was never installed. Both are conditions this package
        cannot fix, and neither is a reason to stop an import that might still succeed.
        """
        if not _SHARED_OBJECT.search(self.library_name):
            return None
        candidates = (
            Path(self.library_directory) / self.library_name if self.library_directory else None,
            Path(self.base_prefix) / "lib" / self.library_name if self.base_prefix else None,
        )
        for candidate in candidates:
            if candidate is not None and candidate.is_file():
                return candidate
        return None


def enable_native_extension_loading(
    runtime: AndroidRuntime | None = None,
    *,
    loader: NativeLoader | None = None,
) -> Path | None:
    """Put libpython's symbols in the linker's global group on Termux/Android. Do nothing elsewhere.

    Returns the library that was loaded, or `None` when nothing was needed or nothing could be
    done. Never raises: this runs at import time on every platform, and a failure here must leave
    the original import to report its own error rather than replace it with one about the fix.
    """
    runtime = AndroidRuntime.detect() if runtime is None else runtime
    if not runtime.is_android:
        return None
    library = runtime.shared_library()
    if library is None:
        return None
    load: NativeLoader = ctypes.CDLL if loader is None else loader
    try:
        load(str(library), GLOBAL_SYMBOLS)
    except OSError:
        return None
    return library
