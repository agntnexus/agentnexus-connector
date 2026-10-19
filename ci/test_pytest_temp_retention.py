"""Pytest keeps no temporary tree after a session, and the settings are in effect.

agntnexus/agentnexus#332. Each session writes its `tmp_path` trees under the Windows temporary
directory. The retention settings in `pyproject.toml` remove them when the session ends and keep
nothing from an earlier session. A setting that stopped applying would bring the disk problem back
without any failure, so the values are read back from pytest for the invocation CI uses
(`python -m pytest ci/<file>.py -q`). The rootdir is checked as well: a configuration file placed
elsewhere would move it, and with it every node id and report path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_the_session_keeps_no_temporary_tree(pytestconfig: pytest.Config) -> None:
    """Retention count 0 and policy none: the session's own tree is removed, nothing is kept."""
    assert pytestconfig.getini("tmp_path_retention_count") == "0"
    assert pytestconfig.getini("tmp_path_retention_policy") == "none"


def test_the_settings_do_not_move_the_rootdir(pytestconfig: pytest.Config) -> None:
    """The rootdir stays the repository root, so node ids and report paths keep their form."""
    assert pytestconfig.rootpath.resolve() == REPOSITORY_ROOT
