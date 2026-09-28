"""The test suite never touches the real runtime (data/runtime/)."""

from __future__ import annotations

import sqlite3

import chromadb
import pytest
from conftest import REAL_RUNTIME, inside_real_runtime

from opspilot.config import get_settings


def test_runtime_is_a_temp_dir() -> None:
    settings = get_settings()
    for p in (
        settings.runtime_dir,
        settings.db_path,
        settings.chroma_dir,
        settings.checkpoint_path,
    ):
        assert not inside_real_runtime(p), p


def test_guard_blocks_real_sqlite() -> None:
    with pytest.raises(RuntimeError, match="real runtime database"):
        sqlite3.connect(str(REAL_RUNTIME / "opspilot.db"))


def test_guard_blocks_real_chroma() -> None:
    with pytest.raises(RuntimeError, match="real Chroma store"):
        chromadb.PersistentClient(path=str(REAL_RUNTIME / "chroma"))


def test_guard_allows_memory_and_temp() -> None:
    sqlite3.connect(":memory:").close()
    assert not inside_real_runtime(get_settings().db_path)
