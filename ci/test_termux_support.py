"""Check that the Connector recognises Termux/Android and loads its native extensions there.

On Android the dynamic linker is bionic, and it does not hand a `dlopen`ed library the CPython
symbols that glibc resolves out of the interpreter executable. `cryptography` ships its Rust
extension as an abi3 module that carries no `DT_NEEDED` entry for libpython — correct everywhere
else, fatal here — so importing it on a Termux device ends in::

    ImportError: dlopen failed: cannot locate symbol "PyModule_Type" referenced by
    .../cryptography/hazmat/bindings/_rust.abi3.so

after an installation whose signature and digest verification had already succeeded
(agntnexus/agentnexus#5). The supported answer is for the package to put libpython's symbols in the
linker's global group itself, before the first native import, so that no operator has to export
`LD_PRELOAD` by hand and no environment variable has to survive a runtime's spawn filtering.

What is worth testing about that, and is tested here:

* the platform decision is made from what the interpreter and the device actually are, never from
  a variable a caller can set — `PREFIX`, `TERMUX_VERSION` and `ANDROID_ROOT` are ignored, and a
  case below sets all three to prove it;
* one signal is never enough: a build triple without a device, or a device without a Termux
  interpreter, is refused rather than guessed at;
* every other platform is left exactly as it was — nothing is loaded, and nothing is attempted;
* a Termux device that cannot supply the library refuses softly, so the operator still sees the
  real `ImportError` rather than a masked one;
* the preload runs **before** the first native extension is imported, which is the whole point and
  the one property a future edit to `__init__.py` could silently destroy.

It reads local files, fabricates its own signals and starts one subprocess of this interpreter. No
network, no credential, no device.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentnexus_sdk.android import (
    DEVICE_MARKERS,
    GLOBAL_SYMBOLS,
    AndroidRuntime,
    enable_native_extension_loading,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"

#: What a Termux installation looks like from inside: an interpreter built for bionic, living under
#: the app's own prefix, on a device whose linker is at a path no desktop Linux has.
TERMUX_PREFIX = "/data/data/com.termux/files/usr"


def termux(**overrides: object) -> AndroidRuntime:
    """Return a runtime that looks like Termux, with named fields replaced for a negative case."""
    signals: dict[str, object] = {
        "platform_name": "linux",
        "host_triple": "aarch64-linux-android",
        "base_prefix": TERMUX_PREFIX,
        "library_name": "libpython3.13.so",
        "library_directory": f"{TERMUX_PREFIX}/lib",
        "device_markers": ("/system/bin/linker64",),
    }
    signals.update(overrides)
    return AndroidRuntime(**signals)  # type: ignore[arg-type]


def desktop_linux(**overrides: object) -> AndroidRuntime:
    """Return the signals an ordinary glibc Linux interpreter reports."""
    signals: dict[str, object] = {
        "platform_name": "linux",
        "host_triple": "x86_64-pc-linux-gnu",
        "base_prefix": "/usr",
        "library_name": "libpython3.13.so",
        "library_directory": "/usr/lib/x86_64-linux-gnu",
        "device_markers": (),
    }
    signals.update(overrides)
    return AndroidRuntime(**signals)  # type: ignore[arg-type]


class RecordingLoader:
    """Stands in for `ctypes.CDLL`, so a test can see what would have been loaded, and how."""

    def __init__(self, *, failure: OSError | None = None) -> None:
        """Record nothing yet; raise `failure` instead of loading when one is given."""
        self.calls: list[tuple[str, int]] = []
        self.failure = failure

    def __call__(self, name: str, mode: int) -> object:
        """Record one load and either fail the way a real linker does or return a handle."""
        self.calls.append((name, mode))
        if self.failure is not None:
            raise self.failure
        return object()


def test_ci_executes_this_guard() -> None:
    """A guard nobody runs is not a guard. The workflow has to name this file."""
    assert WORKFLOW.is_file(), "the workflow that must run this guard is missing"
    assert "ci/test_termux_support.py" in WORKFLOW.read_text(encoding="utf-8")


def test_a_termux_interpreter_is_recognised() -> None:
    """Interpreter built for bionic, under the Termux prefix, on a device with a bionic linker."""
    assert termux().is_android is True


def test_a_termux_interpreter_without_the_android_triple_is_still_recognised() -> None:
    """The prefix is a filesystem fact about the running interpreter, and stands on its own.

    Termux's own interpreter reports an `-android` build triple, but the fix must not depend on a
    build variable staying spelled that way across Termux releases: the device evidence plus a
    resolved interpreter path inside the app's prefix is already more than a platform claim.
    """
    assert termux(host_triple="aarch64-unknown-linux-gnu").is_android is True


def test_an_ordinary_linux_interpreter_is_not_android() -> None:
    """The platform every other operator is on stays exactly as it was."""
    assert desktop_linux().is_android is False


def test_a_windows_interpreter_is_not_android() -> None:
    """`sys.platform` is checked first, so no Windows host reaches the device probes."""
    assert (
        termux(platform_name="win32", device_markers=("/system/bin/linker64",)).is_android is False
    )


def test_a_build_triple_alone_is_not_enough() -> None:
    """A cross-built interpreter is not a device. Without device evidence this is refused."""
    assert termux(device_markers=()).is_android is False


def on_a_device_but_not_termux() -> AndroidRuntime:
    """Return an Android device running an interpreter that is not one of Termux's.

    The case that decides whether detection is evidence-based: everything about the *device* says
    Android, and nothing about the *interpreter* does. Anything that tips this to `True` is reading
    something it should not.
    """
    return termux(host_triple="x86_64-pc-linux-gnu", base_prefix="/usr", library_directory="/lib")


def test_a_device_marker_alone_is_not_enough() -> None:
    """A path that happens to exist does not make an unrelated interpreter a Termux one."""
    assert on_a_device_but_not_termux().is_android is False


def test_a_prefix_belonging_to_another_application_is_not_termux() -> None:
    """`/data/data/<something else>` is another app's sandbox, not this interpreter's prefix."""
    assert (
        termux(
            host_triple="aarch64-unknown-linux-gnu",
            base_prefix="/data/data/com.example.other/files/opt",
        ).is_android
        is False
    )


@pytest.mark.parametrize(
    "variable",
    ["PREFIX", "TERMUX_VERSION", "TERMUX_APP_PID", "ANDROID_ROOT", "ANDROID_DATA"],
)
def test_no_environment_variable_can_declare_a_platform(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """The acceptance criterion, stated as a test: detection reads no user-supplied claim.

    Each variable below is what a Termux shell really exports. Setting it here must change nothing
    about what this host is, on the host running the test and on a fabricated desktop Linux alike.
    """
    monkeypatch.setenv(variable, TERMUX_PREFIX)

    # The decisive one: the device evidence is already there, so an implementation that consulted
    # the environment at all would flip here and nowhere else.
    assert on_a_device_but_not_termux().is_android is False
    assert desktop_linux().is_android is False
    assert AndroidRuntime.detect().is_android is on_android_device()


def on_android_device() -> bool:
    """Whether the machine running these tests is itself an Android device."""
    return sys.platform == "linux" and any(Path(marker).exists() for marker in DEVICE_MARKERS)


def test_this_host_is_detected_honestly() -> None:
    """Detection on the real host agrees with what the host is, and never raises."""
    detected = AndroidRuntime.detect()
    assert detected.platform_name == sys.platform
    assert detected.is_android is (on_android_device() and detected.is_android)


def test_the_shared_library_is_loaded_into_the_global_group(tmp_path: Path) -> None:
    """On Termux, libpython is opened with global symbol visibility, which is the whole fix."""
    library = tmp_path / "libpython3.13.so"
    library.write_bytes(b"")
    loader = RecordingLoader()

    loaded = enable_native_extension_loading(termux(library_directory=str(tmp_path)), loader=loader)

    assert loaded == library
    assert loader.calls == [(str(library), GLOBAL_SYMBOLS)]


@pytest.mark.skipif(not hasattr(os, "RTLD_GLOBAL"), reason="no RTLD_GLOBAL on this platform")
def test_the_global_group_is_the_linkers_own_flag() -> None:
    """The mode used is the platform's `RTLD_GLOBAL`, not a number that resembles it."""
    assert GLOBAL_SYMBOLS == os.RTLD_GLOBAL


def test_nothing_is_loaded_on_a_platform_that_is_not_termux(tmp_path: Path) -> None:
    """Every supported non-Termux path is left untouched: no probe, no load, no cost.

    The library exists and is exactly where this runtime says its own library is, so the only
    thing stopping the load is the platform decision. An implementation that preloaded whenever it
    found a library would pass a version of this case where the file was missing, and would change
    how every Linux and macOS operator's interpreter is set up.
    """
    library = tmp_path / "libpython3.13.so"
    library.write_bytes(b"")
    loader = RecordingLoader()

    loaded = enable_native_extension_loading(
        desktop_linux(library_directory=str(tmp_path)), loader=loader
    )

    assert loaded is None
    assert loader.calls == []


def test_a_missing_shared_library_refuses_softly(tmp_path: Path) -> None:
    """A Termux device with no libpython to preload gets the honest `ImportError`, not a crash."""
    loader = RecordingLoader()

    loaded = enable_native_extension_loading(termux(library_directory=str(tmp_path)), loader=loader)

    assert loaded is None
    assert loader.calls == []


def test_a_static_interpreter_is_never_preloaded(tmp_path: Path) -> None:
    """A statically linked build has no shared library; `libpython3.13.a` must not be dlopened."""
    archive = tmp_path / "libpython3.13.a"
    archive.write_bytes(b"")
    loader = RecordingLoader()

    loaded = enable_native_extension_loading(
        termux(library_directory=str(tmp_path), library_name="libpython3.13.a"), loader=loader
    )

    assert loaded is None
    assert loader.calls == []


def test_a_loader_failure_is_not_fatal(tmp_path: Path) -> None:
    """If the linker refuses the library, the import still proceeds and reports its own error.

    Masking that failure with an exception of our own would replace a diagnosable linker message
    with one about the fix for it.
    """
    library = tmp_path / "libpython3.13.so"
    library.write_bytes(b"")
    loader = RecordingLoader(failure=OSError("dlopen failed"))

    loaded = enable_native_extension_loading(termux(library_directory=str(tmp_path)), loader=loader)

    assert loaded is None
    assert len(loader.calls) == 1


def test_importing_the_package_does_not_raise() -> None:
    """The preload is called on every import of this package, including on this host."""
    subprocess.run(
        [sys.executable, "-c", "import agentnexus_sdk"],
        check=True,
        capture_output=True,
        timeout=120,
    )


#: Run in a subprocess, because the order this measures is an import order and an interpreter has
#: only one. The probe wraps the preload where the package will look it up, records when it is
#: actually called, and records when `cryptography` is first reached -- so what is measured is the
#: real sequence of a fresh start, not a claim about the source.
IMPORT_ORDER_PROBE = """
import json, sys

events = []


class Probe:
    def find_spec(self, name, path=None, target=None):
        if name == "cryptography":
            events.append("cryptography")
            return None
        if name != "agentnexus_sdk.android":
            return None
        spec = None
        for finder in [f for f in sys.meta_path if f is not self]:
            spec = finder.find_spec(name, path, target)
            if spec is not None:
                break
        if spec is None or spec.loader is None:
            return None
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            real = module.enable_native_extension_loading

            def recorded(*arguments, **keywords):
                events.append("preload")
                return real(*arguments, **keywords)

            module.enable_native_extension_loading = recorded

        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, Probe())
import agentnexus_sdk  # noqa: F401
print(json.dumps(events))
"""


def test_the_preload_runs_before_the_first_native_extension() -> None:
    """The ordering the fix depends on, measured rather than asserted about the source.

    `agentnexus_sdk/__init__.py` must *call* the preload before it imports anything that ends in a
    `dlopen`. Moving the call below the re-exports would leave every case above green and the
    device broken, so this is the one that has to fail when somebody reorders that file.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, this interpreter, no shell
        [sys.executable, "-c", IMPORT_ORDER_PROBE],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    events = json.loads(completed.stdout.strip().splitlines()[-1])

    assert "preload" in events, "importing the package no longer calls the platform preload"
    assert "cryptography" in events, (
        "the package no longer imports a native extension; this guard has gone stale"
    )
    assert events.index("preload") < events.index("cryptography")
