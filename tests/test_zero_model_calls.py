"""
test_zero_model_calls.py — 实例零次模型调用 → 环境缺陷，直接转人工（不重拉）。

设计侧结论（方案 1）：
  零次模型调用 = 实例根本没能开工 = 环境缺陷。
  重拉一百次网络也不会通，所以不进重拉路径；
  但什么都没交付也不能算完成，所以不是降级放行。
  终局与「缺绑定参数」「坏声明」同类：直接转人工、不重拉、评论说明原因。

三条测试：
  1. 零助手消息 → verdict=instance_not_started, task blocked, 评论含相应措辞, task_runs 只有一条
  2. 非零助手消息但产物缺失 → 仍走 unmet 重拉路径，不被新逻辑误挡
  3. 声明关掉 requires_model_call → 零消息时不触发新逻辑
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TALOS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TALOS_ROOT))

from talos.executor.adjudicate import adjudicate, Verdict
from talos.executor.collect import CollectedBundle
from talos.executor.declarations import (
    ArtifactSpec,
    Declaration,
    DeliverableSpec,
    GitSpec,
    ResourceSpec,
    VerificationSpec,
)


# ── Helpers ────────────────────────────────────────────────────

def make_decl(
    artifacts=None,
    git=None,
    verification=None,
    deliverables=None,
    requires_model_call=True,
) -> Declaration:
    return Declaration(
        skills=["test-skill"],
        resources=ResourceSpec(memory_mb=512, cpus=0.5),
        artifacts=artifacts or [],
        git=git or GitSpec(),
        verification=verification or VerificationSpec(required=False, source="none"),
        deliverables=deliverables or [],
        credentials=[],
        requires=[],
        requires_model_call=requires_model_call,
    )


def make_bundle(tmp_path, result_json=None, workspace_path=None, exit_code=0):
    tdir = tmp_path / "tdir"
    tdir.mkdir(parents=True, exist_ok=True)
    return CollectedBundle(
        task_id="t_test",
        run_id=1,
        tdir=tdir,
        result_json=result_json,
        result_raw=json.dumps(result_json) if result_json else None,
        result_path=tdir / "out" / "result.json" if result_json else None,
        workspace_path=workspace_path,
        exit_code=exit_code,
    )


def make_state_db(tdir: Path, assistant_count: int = 0) -> Path:
    """Create a minimal state.db with a messages table.

    assistant_count=0 → zero model calls (environment defect).
    assistant_count>0 → normal operation.
    """
    state_db = tdir / "state.db"
    conn = sqlite3.connect(str(state_db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT,
            session_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at INTEGER
        )
    """)
    conn.execute("INSERT INTO sessions (id, created_at) VALUES (?, ?)",
                 ("sess-1", 1000))
    for i in range(assistant_count):
        conn.execute(
            "INSERT INTO messages (role, content, session_id) VALUES (?, ?, ?)",
            ("assistant", f"response {i}", "sess-1"),
        )
    # Always insert a user message (the context injection)
    conn.execute(
        "INSERT INTO messages (role, content, session_id) VALUES (?, ?, ?)",
        ("user", "task context", "sess-1"),
    )
    conn.commit()
    conn.close()
    return state_db


# ═══════════════════════════════════════════════════════════════
# 1. 零助手消息 → instance_not_started, blocked, 不重拉
# ═══════════════════════════════════════════════════════════════

class TestZeroModelCallsBlocked:
    """零次模型调用 → 环境缺陷 → 直接转人工，不重拉。"""

    def test_zero_assistant_messages_verdict_instance_not_started(self, tmp_path):
        """state.db 中助手消息为零 → verdict.status = instance_not_started。"""
        bundle = make_bundle(tmp_path, exit_code=0)
        make_state_db(bundle.tdir, assistant_count=0)

        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=10)],
            git=GitSpec(branch="talos/t_test", require_push=False),
        )

        verdict = adjudicate("t_test", 1, bundle, decl)
        assert verdict.status == "instance_not_started", (
            f"Expected 'instance_not_started', got '{verdict.status}'"
        )

    def test_zero_assistant_messages_task_blocked_no_retry(self, conn, make_task, make_run, tmp_path):
        """零次模型调用 → task blocked, task_runs 只有一条（没有重拉）。"""
        from talos.executor.finalize import finalize

        task_id = make_task(id="t_zmc01", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        bundle = make_bundle(tmp_path, exit_code=0)
        make_state_db(bundle.tdir, assistant_count=0)

        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=10)],
            git=GitSpec(branch="talos/t_test", require_push=False),
        )

        verdict = adjudicate("t_test", 1, bundle, decl)

        with patch("hermes_cli.kanban_db.block_task") as mock_block, \
             patch("hermes_cli.kanban_db.add_comment") as mock_comment, \
             patch("hermes_cli.kanban_db_dispatch._record_task_failure") as mock_failure:
            result = finalize(conn, task_id, 1, verdict)

        # Task should be blocked
        assert result == "blocked", f"Expected 'blocked', got '{result}'"
        # block_task should be called (not _record_task_failure)
        mock_block.assert_called_once()
        mock_failure.assert_not_called()

        # task_runs should still have only 1 run (no retry)
        runs = conn.execute(
            "SELECT COUNT(*) as cnt FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert runs["cnt"] == 1, f"Expected 1 run (no retry), got {runs['cnt']}"

    def test_zero_assistant_messages_comment_wording(self, conn, make_task, make_run, tmp_path):
        """评论写明：实例未能启动（零次模型调用），疑似环境问题，提示检查网络与配置。
        不写「产物缺失」。"""
        from talos.executor.finalize import finalize

        task_id = make_task(id="t_zmc02", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        bundle = make_bundle(tmp_path, exit_code=0)
        make_state_db(bundle.tdir, assistant_count=0)

        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=10)],
            git=GitSpec(branch="talos/t_test", require_push=False),
        )

        verdict = adjudicate("t_test", 1, bundle, decl)

        with patch("hermes_cli.kanban_db.block_task"), \
             patch("hermes_cli.kanban_db.add_comment") as mock_comment, \
             patch("hermes_cli.kanban_db_dispatch._record_task_failure"):
            finalize(conn, task_id, 1, verdict)

        # Check comment content
        comment_calls = mock_comment.call_args_list
        all_comments = " ".join(str(c) for c in comment_calls)

        assert "实例未能启动" in all_comments or "零次模型调用" in all_comments, (
            f"Comment missing '实例未能启动' or '零次模型调用': {all_comments}"
        )
        assert "环境" in all_comments, (
            f"Comment missing '环境' hint: {all_comments}"
        )
        assert "网络" in all_comments or "配置" in all_comments, (
            f"Comment missing network/config hint: {all_comments}"
        )
        # Must NOT say "产物缺失"
        assert "产物缺失" not in all_comments, (
            f"Comment should NOT contain '产物缺失': {all_comments}"
        )


# ═══════════════════════════════════════════════════════════════
# 2. 非零助手消息但产物缺失 → 仍走 unmet 重拉路径
# ═══════════════════════════════════════════════════════════════

class TestNonZeroModelCallsNormalPath:
    """助手消息非零但产物缺失 → 仍走原来的 unmet 重拉路径，不被新逻辑误挡。"""

    def test_nonzero_messages_missing_artifact_goes_unmet(self, tmp_path):
        """有助手消息但产物缺失 → verdict=unmet（不是 instance_not_started）。"""
        bundle = make_bundle(tmp_path, exit_code=0)
        make_state_db(bundle.tdir, assistant_count=3)

        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=10)],
            git=GitSpec(branch="talos/t_test", require_push=False),
        )

        verdict = adjudicate("t_test", 1, bundle, decl)
        assert verdict.status == "unmet", (
            f"Expected 'unmet' (normal retry path), got '{verdict.status}'"
        )
        assert any("产物缺失" in p for p in verdict.problems), (
            f"Expected '产物缺失' in problems: {verdict.problems}"
        )

    def test_nonzero_messages_unmet_goes_through_retry(self, conn, make_task, make_run, tmp_path):
        """非零消息 + unmet → finalize 走 _record_task_failure（重拉路径），不走 block_task。"""
        from talos.executor.finalize import finalize

        task_id = make_task(id="t_nz01", status="running", current_run_id=1)
        make_run(id=1, task_id=task_id, status="running")

        bundle = make_bundle(tmp_path, exit_code=0)
        make_state_db(bundle.tdir, assistant_count=2)

        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=10)],
            git=GitSpec(branch="talos/t_test", require_push=False),
        )

        verdict = adjudicate("t_test", 1, bundle, decl)

        with patch("hermes_cli.kanban_db.block_task") as mock_block, \
             patch("hermes_cli.kanban_db.add_comment"), \
             patch("hermes_cli.kanban_db_dispatch._record_task_failure", return_value=False) as mock_failure:
            result = finalize(conn, task_id, 1, verdict)

        # Should go through _record_task_failure (retry path), NOT block_task
        mock_failure.assert_called_once()
        mock_block.assert_not_called()
        assert result == "ready", f"Expected 'ready' (requeue), got '{result}'"


# ═══════════════════════════════════════════════════════════════
# 3. 声明关掉 requires_model_call → 零消息时不触发新逻辑
# ═══════════════════════════════════════════════════════════════

class TestRequiresModelCallOptOut:
    """声明里关掉 requires_model_call → 零消息时不触发新逻辑。"""

    def test_opt_out_zero_messages_not_instance_not_started(self, tmp_path):
        """requires_model_call=False + 零消息 → 不是 instance_not_started。"""
        bundle = make_bundle(tmp_path, exit_code=0)
        make_state_db(bundle.tdir, assistant_count=0)

        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=10)],
            git=GitSpec(branch="talos/t_test", require_push=False),
            requires_model_call=False,
        )

        verdict = adjudicate("t_test", 1, bundle, decl)
        assert verdict.status != "instance_not_started", (
            f"With requires_model_call=False, should NOT trigger instance_not_started, "
            f"got '{verdict.status}'"
        )
