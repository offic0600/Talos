"""Test fixtures: real 38-column tasks schema, 16-column task_runs, etc.

Schema source: PRAGMA table_info on the real kanban.db (§12, M12 — real schema).
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

# ── Real schema (38 columns, from PRECHECK context) ──────────────────────

TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT,
    assignee TEXT,
    status TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch',
    workspace_path TEXT,
    claim_lock TEXT,
    claim_expires INTEGER,
    tenant TEXT,
    result TEXT,
    idempotency_key TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER,
    worker_started_at INTEGER,
    last_failure_error TEXT,
    max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER,
    current_run_id INTEGER,
    workflow_template_id TEXT,
    current_step_key TEXT,
    skills TEXT,
    max_retries INTEGER,
    branch_name TEXT,
    project_id TEXT,
    model_override TEXT,
    provider_override TEXT,
    reasoning_effort TEXT,
    goal_mode INTEGER NOT NULL DEFAULT 0,
    goal_max_turns INTEGER,
    session_id TEXT,
    block_kind TEXT,
    block_recurrences INTEGER NOT NULL DEFAULT 0,
    completion_contract TEXT
);
"""

TASK_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    profile TEXT,
    step_key TEXT,
    status TEXT NOT NULL,
    claim_lock TEXT,
    claim_expires INTEGER,
    worker_pid INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    outcome TEXT,
    summary TEXT,
    metadata TEXT,
    error TEXT
);
"""

TASK_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER NOT NULL
);
"""

TASK_COMMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_comments (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

# Task dependencies (for link_tasks)
TASK_LINKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
"""

# Task artifacts (for completion)
TASK_ARTIFACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_artifacts (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT,
    repo TEXT,
    branch TEXT,
    sha TEXT,
    path TEXT,
    created_at INTEGER NOT NULL
);
"""


def make_test_db() -> tuple[sqlite3.Connection, Path]:
    """Create a temporary kanban DB with the real schema.

    Returns (conn, db_path). Caller is responsible for closing conn.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="talos-test-"))
    db_path = tmpdir / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(TASKS_SCHEMA)
    conn.execute(TASK_RUNS_SCHEMA)
    conn.execute(TASK_EVENTS_SCHEMA)
    conn.execute(TASK_COMMENTS_SCHEMA)
    conn.execute(TASK_LINKS_SCHEMA)
    conn.execute(TASK_ARTIFACTS_SCHEMA)
    conn.commit()
    return conn, db_path


def insert_task(
    conn: sqlite3.Connection,
    *,
    id: str = "t_test01",
    title: str = "Test task",
    body: str = "",
    status: str = "running",
    assignee: str = "default",
    skills: list[str] | None = None,
    current_run_id: int | None = 1,
    max_retries: int | None = 2,
    branch_name: str | None = None,
    workspace_path: str | None = None,
    claim_lock: str | None = "host1:executor",
    consecutive_failures: int = 0,
    workspace_kind: str = "scratch",
    **extra,
) -> str:
    """Insert a task row for testing."""
    import json
    import time
    cols = {
        "id": id,
        "title": title,
        "body": body,
        "assignee": assignee,
        "status": status,
        "priority": 0,
        "created_by": "test",
        "created_at": int(time.time()),
        "started_at": int(time.time()),
        "workspace_kind": workspace_kind,
        "workspace_path": workspace_path,
        "claim_lock": claim_lock,
        "claim_expires": int(time.time()) + 300,
        "skills": json.dumps(skills) if skills else None,
        "current_run_id": current_run_id,
        "max_retries": max_retries,
        "branch_name": branch_name,
        "consecutive_failures": consecutive_failures,
    }
    cols.update(extra)
    col_names = ", ".join(cols.keys())
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(f"INSERT INTO tasks ({col_names}) VALUES ({placeholders})",
                 tuple(cols.values()))
    conn.commit()
    return id


def insert_run(
    conn: sqlite3.Connection,
    *,
    id: int = 1,
    task_id: str = "t_test01",
    status: str = "running",
    started_at: int | None = None,
    outcome: str | None = None,
    metadata: str | None = None,
    error: str | None = None,
    **extra,
) -> int:
    """Insert a task_runs row for testing."""
    import time
    cols = {
        "id": id,
        "task_id": task_id,
        "status": status,
        "started_at": started_at or int(time.time()),
        "outcome": outcome,
        "metadata": metadata,
        "error": error,
    }
    cols.update(extra)
    col_names = ", ".join(cols.keys())
    placeholders = ", ".join(["?"] * len(cols))
    conn.execute(f"INSERT INTO task_runs ({col_names}) VALUES ({placeholders})",
                 tuple(cols.values()))
    conn.commit()
    return id
