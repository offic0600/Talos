"""
test_subtask_creation.py — 子任务创建的安全边界（P0）。

实例能通过 result.json 的 subtasks 字段凭空制造任务：
没有数量上限、没有校验、建出来就是 ready 下一 tick 即派发。
这跟派发事故同类——无界的任务来源，只是这次来源是容器自报内容。

四条测试：
  1. 正常数量正常创建
  2. 超限时截断且记 error 事件
  3. title 为空跳过
  4. create_task 抛错时不中断其余项
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

TALOS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TALOS_ROOT))

from talos.executor.adjudicate import Verdict
from talos.executor.constants import log_event


# ── Helpers ────────────────────────────────────────────────────

def make_verdict(subtasks: list[dict]) -> Verdict:
    return Verdict(
        status="pass",
        problems=[],
        defects=[],
        result_status="done",
        summary="ok",
        artifacts=[],
        subtasks=subtasks,
    )


# ═══════════════════════════════════════════════════════════════
# 1. 正常数量正常创建
# ═══════════════════════════════════════════════════════════════

class TestNormalSubtaskCreation:
    """正常数量的子任务 → 全部创建，created_by 标注来源。"""

    def test_creates_subtasks_within_limit(self, conn, make_task, make_run, tmp_path):
        from talos.executor.finalize import _create_subtasks

        task_id = make_task(id="t_sub01", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        subtasks = [
            {"title": f"子任务-{i}", "body": f"body-{i}"}
            for i in range(3)
        ]
        verdict = make_verdict(subtasks)

        with patch("hermes_cli.kanban_db.create_task", side_effect=lambda conn, **kw: f"t_child_{kw['title'][-1]}") as mock_create, \
             patch("hermes_cli.kanban_db.link_tasks") as mock_link:
            _create_subtasks(conn, task_id, verdict)

        assert mock_create.call_count == 3
        assert mock_link.call_count == 3

    def test_created_by_marks_executor_and_parent(self, conn, make_task, make_run, tmp_path):
        """created_by 必须包含 talos-executor 和父任务号，方便追溯。"""
        from talos.executor.finalize import _create_subtasks

        task_id = make_task(id="t_sub02", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        verdict = make_verdict([{"title": "子任务-X"}])

        captured_kwargs = []
        def capture_create(conn, **kw):
            captured_kwargs.append(kw)
            return "t_child_x"

        with patch("hermes_cli.kanban_db.create_task", side_effect=capture_create), \
             patch("hermes_cli.kanban_db.link_tasks"):
            _create_subtasks(conn, task_id, verdict)

        assert len(captured_kwargs) == 1
        created_by = captured_kwargs[0].get("created_by", "")
        assert "talos-executor" in created_by, f"created_by should contain 'talos-executor', got: {created_by}"
        assert "t_sub02" in created_by, f"created_by should contain parent task id, got: {created_by}"


# ═══════════════════════════════════════════════════════════════
# 2. 超限时截断且记 error 事件
# ═══════════════════════════════════════════════════════════════

class TestSubtaskLimitTruncation:
    """超出上限 → 截断 + 记 error 事件。"""

    def test_truncates_when_exceeding_limit(self, conn, make_task, make_run, tmp_path):
        from talos.executor.finalize import _create_subtasks

        task_id = make_task(id="t_sub03", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        # Create more subtasks than the default limit (10)
        subtasks = [
            {"title": f"子任务-{i}", "body": f"body-{i}"}
            for i in range(20)
        ]
        verdict = make_verdict(subtasks)

        created_titles = []
        def capture_create(conn, **kw):
            created_titles.append(kw["title"])
            return f"t_child_{len(created_titles)}"

        with patch("hermes_cli.kanban_db.create_task", side_effect=capture_create), \
             patch("hermes_cli.kanban_db.link_tasks"):
            _create_subtasks(conn, task_id, verdict)

        # Should only create 10 (the default limit)
        assert len(created_titles) == 10, (
            f"Expected 10 subtasks (truncated), got {len(created_titles)}"
        )

    def test_logs_error_when_truncated(self, conn, make_task, make_run, tmp_path):
        """超限截断时必须记 error 事件，文案含请求数量和上限。"""
        from talos.executor.finalize import _create_subtasks

        task_id = make_task(id="t_sub04", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        subtasks = [
            {"title": f"子任务-{i}"}
            for i in range(15)
        ]
        verdict = make_verdict(subtasks)

        log_events = []
        original_log = log_event
        def mock_log(kind, *, task_id=None, **extra):
            log_events.append({"kind": kind, "task_id": task_id, **extra})
            original_log(kind, task_id=task_id, **extra)

        with patch("talos.executor.finalize.log_event", side_effect=mock_log), \
             patch("hermes_cli.kanban_db.create_task", return_value="t_child"), \
             patch("hermes_cli.kanban_db.link_tasks"):
            _create_subtasks(conn, task_id, verdict)

        # Find the truncation error event
        truncation_events = [
            e for e in log_events
            if e["kind"] == "error" and "超出上限" in e.get("msg", "")
        ]
        assert len(truncation_events) == 1, (
            f"Expected 1 truncation error event, got {len(truncation_events)}"
        )
        msg = truncation_events[0]["msg"]
        assert "15" in msg, f"Error message should mention requested count 15: {msg}"
        assert "10" in msg, f"Error message should mention limit 10: {msg}"


# ═══════════════════════════════════════════════════════════════
# 3. title 为空跳过
# ═══════════════════════════════════════════════════════════════

class TestEmptyTitleSkipped:
    """title 为空 → 跳过该子任务。"""

    def test_empty_title_skipped(self, conn, make_task, make_run, tmp_path):
        from talos.executor.finalize import _create_subtasks

        task_id = make_task(id="t_sub05", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        subtasks = [
            {"title": "", "body": "empty"},
            {"title": "有效子任务", "body": "ok"},
            {"title": "   ", "body": "whitespace"},
            {"body": "no title key"},
        ]
        verdict = make_verdict(subtasks)

        created_titles = []
        def capture_create(conn, **kw):
            created_titles.append(kw["title"])
            return f"t_child_{len(created_titles)}"

        with patch("hermes_cli.kanban_db.create_task", side_effect=capture_create), \
             patch("hermes_cli.kanban_db.link_tasks"):
            _create_subtasks(conn, task_id, verdict)

        assert len(created_titles) == 1, (
            f"Expected 1 subtask (only non-empty title), got {len(created_titles)}"
        )
        assert created_titles[0] == "有效子任务"


# ═══════════════════════════════════════════════════════════════
# 4. create_task 抛错时不中断其余项
# ═══════════════════════════════════════════════════════════════

class TestCreateErrorDoesNotBlockRest:
    """create_task 抛错 → 记 error，继续创建其余子任务。"""

    def test_one_failure_does_not_block_rest(self, conn, make_task, make_run, tmp_path):
        from talos.executor.finalize import _create_subtasks

        task_id = make_task(id="t_sub06", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        subtasks = [
            {"title": "子任务-A"},
            {"title": "子任务-B"},
            {"title": "子任务-C"},
        ]
        verdict = make_verdict(subtasks)

        call_count = 0
        def fail_on_second(conn, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("simulated DB error")
            return f"t_child_{call_count}"

        with patch("hermes_cli.kanban_db.create_task", side_effect=fail_on_second), \
             patch("hermes_cli.kanban_db.link_tasks"):
            _create_subtasks(conn, task_id, verdict)

        # All 3 should have been attempted (failure on #2 doesn't stop #3)
        assert call_count == 3, (
            f"Expected 3 create_task calls (failure doesn't block rest), got {call_count}"
        )
