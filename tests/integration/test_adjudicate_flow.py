"""Integration test: full adjudicate flow with mocked spawn.

This test exercises the real adjudicate → finalize → archive pipeline
against the real kanban DB, but replaces spawn_fn with a mock that creates
a simulated container (no real LLM calls). It does NOT write to
ACC_RESULT.md — it only verifies that the executor's data flow is correct
end-to-end.

Run: pytest tests/integration/test_adjudicate_flow.py -v
"""

from __future__ import annotations

import json
import os
import sys
import time
import sqlite3
import subprocess
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TALOS_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(TALOS_ROOT))
sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))

KANBAN_DB = os.environ.get(
    "HERMES_KANBAN_DB",
    str(Path.home() / ".hermes" / "kanban" / "kanban.db"),
)
TALOS_HOME = Path(os.environ.get("TALOS_HOME", str(Path.home() / ".hermes" / "talos")))
PILOT_REPO = "https://hgit.haier.net/S05190/talos-pilot.git"


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def conn():
    """Real kanban DB connection (read/write, auto-cleanup)."""
    c = sqlite3.connect(KANBAN_DB)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def make_task(conn):
    """Create a task in the real kanban DB; auto-cancel after test."""
    created = []

    def _make(title="integration-test", body="", skills=None, **kw):
        from hermes_cli import kanban_db as kb

        tid = kb.create_task(
            conn,
            title=title,
            body=body or f"repo: {PILOT_REPO}\nIntegration test.",
            assignee="default",
            skills=skills or [],
            created_by="talos-integration-test",
            **kw,
        )
        conn.commit()
        created.append(tid)
        return tid

    yield _make

    # Cleanup
    for tid in created:
        subprocess.run(
            ["docker", "rm", "-f"] +
            subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name=hermes-worker-{tid}",
                 "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=15
            ).stdout.strip().splitlines(),
            capture_output=True, timeout=30
        )
        tdir = TALOS_HOME / "tasks" / tid
        if tdir.exists():
            shutil.rmtree(tdir, ignore_errors=True)
        conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
        conn.commit()


def _simulate_container(tid, run_id, result_json=None, work_files=None):
    """Create a stopped container with result.json and work files."""
    cname = f"hermes-worker-{tid}-{run_id}"
    subprocess.run(
        ["docker", "run", "-d", "--name", cname, "--entrypoint", "sh",
         "hermes-worker:latest", "-c", "mkdir -p /task/out /work && sleep 600"],
        capture_output=True, text=True, timeout=60
    )
    # Write result.json via docker cp (avoids shell escaping)
    if result_json is not None:
        tmpf = f"/tmp/result_{tid}_{run_id}.json"
        with open(tmpf, "w") as f:
            json.dump(result_json, f)
        subprocess.run(["docker", "cp", tmpf, f"{cname}:/task/out/result.json"],
                       capture_output=True, timeout=15)
        os.unlink(tmpf)
    # Write work files
    if work_files:
        for path, content in work_files.items():
            tmpw = f"/tmp/workfile_{tid}_{run_id}"
            with open(tmpw, "w") as f:
                f.write(content)
            full_path = f"/work/{path}"
            parent = os.path.dirname(full_path)
            if parent != "/work":
                subprocess.run(["docker", "exec", cname, "mkdir", "-p", parent],
                               capture_output=True, timeout=10)
            subprocess.run(["docker", "cp", tmpw, f"{cname}:{full_path}"],
                           capture_output=True, timeout=15)
            os.unlink(tmpw)
    subprocess.run(["docker", "stop", cname], capture_output=True, timeout=30)
    time.sleep(0.5)


def _manual_dispatch(conn, tid):
    """Set task to running + assign run_id (bypasses real spawn)."""
    max_run = conn.execute("SELECT MAX(id) FROM task_runs").fetchone()[0] or 0
    run_id = max_run + 1
    conn.execute(
        "UPDATE tasks SET status='running', current_run_id=?, started_at=?, "
        "claim_lock=?, claim_expires=? WHERE id=?",
        (run_id, int(time.time()), "talos-integration-test",
         int(time.time()) + 300, tid),
    )
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at) VALUES (?, ?, 'running', ?)",
        (run_id, tid, int(time.time())),
    )
    conn.commit()
    return run_id


# ── Tests ────────────────────────────────────────────────────────


class TestAdjudicateFlowUnmet:
    """Full flow: dispatch → simulate container → collect → adjudicate → finalize."""

    def test_unmet_to_ready(self, conn, make_task):
        """No result.json → unmet → ready (requeue)."""
        tid = make_task(title="int-unmet", skills=["talos-code-demo"])
        run_id = _manual_dispatch(conn, tid)
        _simulate_container(tid, run_id, result_json=None)

        from talos.executor.collect import collect
        from talos.executor.adjudicate import adjudicate
        from talos.executor.declarations import load_declarations
        from talos.executor.finalize import finalize

        task_dict = dict(conn.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        class T:
            skills = None
        task = T()
        for k, v in task_dict.items():
            setattr(task, k, v)
        task.skills = json.loads(task_dict["skills"]) if task_dict["skills"] else []

        bundle = collect(tid, run_id)
        decl = load_declarations(task.skills, task)
        verdict = adjudicate(tid, run_id, bundle, decl)
        new_status = finalize(conn, tid, run_id, verdict)

        assert verdict.status == "unmet"
        assert new_status == "ready"
        # Executor comment should exist
        comments = conn.execute(
            "SELECT * FROM task_comments WHERE task_id=? AND author='talos-executor'",
            (tid,)).fetchall()
        assert len(comments) == 1


class TestAdjudicateFlowBlocked:
    """result.json status=blocked → error → block_task."""

    def test_blocked_to_error(self, conn, make_task):
        tid = make_task(title="int-blocked", skills=["talos-code-demo"])
        run_id = _manual_dispatch(conn, tid)
        _simulate_container(tid, run_id,
            result_json={"schema": 1, "status": "blocked",
                         "summary": "Missing credentials", "artifacts": []})

        from talos.executor.collect import collect
        from talos.executor.adjudicate import adjudicate
        from talos.executor.declarations import load_declarations
        from talos.executor.finalize import finalize

        task_dict = dict(conn.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        class T:
            skills = None
        task = T()
        for k, v in task_dict.items():
            setattr(task, k, v)
        task.skills = json.loads(task_dict["skills"]) if task_dict["skills"] else []

        bundle = collect(tid, run_id)
        decl = load_declarations(task.skills, task)
        verdict = adjudicate(tid, run_id, bundle, decl)
        new_status = finalize(conn, tid, run_id, verdict)

        assert verdict.status == "unmet"  # §7 v2: blocked → unmet path, not error
        assert verdict.result_status == "blocked"
        # _record_task_failure → ready (first occurrence, not blocked/triage)
        assert new_status in ("ready", "blocked")


class TestAdjudicateFlowArchive:
    """Archive directory contains result.json + inspect.json + verdict.json."""

    def test_archive_contains_all_files(self, conn, make_task):
        tid = make_task(title="int-archive", skills=["talos-code-demo"])
        run_id = _manual_dispatch(conn, tid)
        _simulate_container(tid, run_id,
            result_json={"schema": 1, "status": "done",
                         "summary": "ok", "artifacts": []})

        from talos.executor.collect import collect
        from talos.executor.adjudicate import adjudicate
        from talos.executor.declarations import load_declarations
        from talos.executor.finalize import finalize
        from talos.executor.archive import archive

        task_dict = dict(conn.execute(
            "SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        class T:
            skills = None
        task = T()
        for k, v in task_dict.items():
            setattr(task, k, v)
        task.skills = json.loads(task_dict["skills"]) if task_dict["skills"] else []

        bundle = collect(tid, run_id)
        decl = load_declarations(task.skills, task)
        verdict = adjudicate(tid, run_id, bundle, decl)
        finalize(conn, tid, run_id, verdict)

        adir = archive(tid, run_id, bundle, verdict)
        file_names = [f.name for f in adir.glob("*")] if adir.exists() else []

        assert "result.json" in file_names
        assert "inspect.json" in file_names
        assert "verdict.json" in file_names
