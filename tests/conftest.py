"""Shared fixtures: an isolated runtime dir seeded once per session, plus per-test DB resets.

Isolation guard: tests must never open the real runtime (data/runtime/). Every SQLite and
Chroma open is checked, and the configured runtime is verified before and after each test.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REAL_RUNTIME = (Path(__file__).resolve().parents[1] / "data" / "runtime").resolve()

_RUNTIME = Path(tempfile.mkdtemp(prefix="opspilot-test-")).resolve()
os.environ["OPSPILOT_RUNTIME_DIR"] = str(_RUNTIME)
os.environ["OPSPILOT_TODAY"] = "2026-09-27"
os.environ["OPSPILOT_PROVIDER"] = "auto"
os.environ["GROQ_API_KEY"] = ""
os.environ["GOOGLE_API_KEY"] = ""


def inside_real_runtime(target: Any) -> bool:
    """True if a filesystem path (str or Path) points into data/runtime/."""
    text = str(target)
    if not text or text == ":memory:" or text.startswith("file::memory:"):
        return False
    try:
        return Path(text).resolve().is_relative_to(REAL_RUNTIME)
    except (OSError, ValueError):
        return False


@pytest.fixture(scope="session", autouse=True)
def guard_real_runtime() -> Iterator[None]:
    """Fail loudly if any code under test opens SQLite or Chroma inside data/runtime/."""
    import chromadb

    real_connect = sqlite3.connect
    real_client = chromadb.PersistentClient

    def guarded_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if inside_real_runtime(database):
            raise RuntimeError(f"test opened the real runtime database: {database}")
        return real_connect(database, *args, **kwargs)

    def guarded_client(*args: Any, **kwargs: Any) -> Any:
        target = kwargs.get("path", args[0] if args else "")
        if inside_real_runtime(target):
            raise RuntimeError(f"test opened the real Chroma store: {target}")
        return real_client(*args, **kwargs)

    patch = pytest.MonkeyPatch()
    patch.setattr(sqlite3, "connect", guarded_connect)
    patch.setattr(chromadb, "PersistentClient", guarded_client)
    yield
    patch.undo()


@pytest.fixture(scope="session", autouse=True)
def seeded_runtime(guard_real_runtime: None) -> Iterator[Path]:
    """Seed SQLite + Chroma once into a temp runtime directory."""
    from opspilot.seeding import seed_all

    seed_all()
    yield _RUNTIME


@pytest.fixture(autouse=True)
def runtime_stays_isolated() -> Iterator[None]:
    """The configured runtime must be the temp dir before and after every test."""
    from opspilot.config import get_settings

    def check(when: str) -> None:
        runtime = get_settings().runtime_dir
        assert runtime == _RUNTIME and not inside_real_runtime(runtime), (
            f"{when} test: runtime is {runtime}, expected the isolated {_RUNTIME}"
        )

    check("before")
    yield
    check("after")


@pytest.fixture(autouse=True)
def reset_business_data(seeded_runtime: Path) -> Path:
    """Reset SQLite business data before every test (about 70 ms), so no test depends on
    what an earlier test refunded, cancelled or stored. Chroma is shared and read-only."""
    from opspilot.seeding import seed_database

    seed_database()
    return seeded_runtime


@pytest.fixture()
def fresh_db(reset_business_data: Path) -> Path:
    """Marks tests that mutate business data; the reset itself is autouse."""
    return reset_business_data
