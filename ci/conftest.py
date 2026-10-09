"""Markers of the CI tests."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker of the tests the Windows security job must run."""
    config.addinivalue_line(
        "markers",
        "windows_security: proves a Windows boundary; the windows-latest job never skips it",
    )
