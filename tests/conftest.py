"""Shared pytest fixtures for Talos executor tests."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Guard: refuse to run against the real kanban DB unless explicitly opted in.
if os.environ.get("HERMES_KANBAN_DB") and not os.environ.get("TALOS_TEST_ALLOW_REAL_DB"):
    pytest.skip(
        "HERMES_KANBAN_DB is set — refusing to run tests against a real DB. "
        "Unset it or set TALOS_TEST_ALLOW_REAL_DB=1 to override.",
        allow_module_level=True,
    )

# Ensure project root is importable
TALOS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TALOS_ROOT))

from tests.fixtures import make_test_db, insert_task, insert_run


@pytest.fixture
def conn():
    """Provide a fresh in-memory-style kanban DB with real schema."""
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
