"""Shared pytest fixtures for Talos executor tests.

Fail-safe policy: tests MUST run against a temporary DB only. If no
explicit test DB path is provided via TALOS_TEST_DB, every test skips.
Tests NEVER fall back to any default real path.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Fail-safe guard: if TALOS_TEST_DB is not set, skip all tests.
# We do NOT check any production DB env var — the absence of a test DB
# is itself the signal to skip. This prevents any test from silently
# connecting to a production database.
_TEST_DB = os.environ.get("TALOS_TEST_DB")
if not _TEST_DB:
    pytest.skip(
        "TALOS_TEST_DB is not set — refusing to run tests without an "
        "explicit test database path. Set TALOS_TEST_DB=/tmp/test.db "
        "or use the in-memory fixture via `make_test_db()`.",
        allow_module_level=True,
    )

# NOTE: TALOS_TEST_DB only serves as a "we are in test mode" switch — its
# value is never used. The actual test database is created by make_test_db()
# via mkdtemp() in tests/fixtures.py.

# Ensure project root is importable
TALOS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TALOS_ROOT))

from tests.fixtures import make_test_db, insert_task, insert_run


@pytest.fixture
def conn():
    """Provide a fresh temporary kanban DB with real schema."""
    conn, _db_path = make_test_db()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def make_task(conn):
    """Insert a task and return its id."""
    created = []

    def _make(**kwargs) -> str:
        task_id = insert_task(conn, **kwargs)
        created.append(task_id)
        return task_id

    return _make


@pytest.fixture
def make_run(conn):
    """Insert a task_runs row and return its id."""
    def _make(**kwargs) -> int:
        # Support metadata as dict (auto-serialized)
        meta = kwargs.pop("metadata", None)
        if isinstance(meta, dict):
            meta = json.dumps(meta)
        if meta is not None:
            kwargs["metadata"] = meta
        return insert_run(conn, **kwargs)

    return _make
