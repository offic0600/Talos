"""
test_db_path_selfcheck.py — 账本路径自检 + 影子库告警（按最终决策：未显式配置即拒绝启动）。

设计侧结论：
  HERMES_KANBAN_DB 未设置或为空 → 启动自检失败，打印明确原因，退出。
  不回落到任何默认路径——起不来是响亮的失败，起在错的库上是静默的失败。
  影子库（~/.hermes/kanban.db）存在 → 告警（不阻止启动），写 executor.jsonl。
  归档文件（kanban.db.archived-* / .bak / -wal / -shm）不算影子库。

两条测试：
  1. 未设 HERMES_KANBAN_DB → _self_check 失败退出（断言退出码与错误消息）
  2. 影子库存在时告警、只有归档文件时不告警
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

TALOS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TALOS_ROOT))


# ═══════════════════════════════════════════════════════════════
# 1. HERMES_KANBAN_DB 未设置 → 自检失败退出
# ═══════════════════════════════════════════════════════════════

class TestKanbanDBPathRequired:
    """HERMES_KANBAN_DB 未设或为空 → 启动自检 hard fail。"""

    def test_missing_env_exits(self, monkeypatch):
        """HERMES_KANBAN_DB 未设置 → 调用 _resolve_kanban_db 抛 SystemExit。"""
        # Import first (module already cached with env set), then remove env
        from talos.executor.constants import _resolve_kanban_db
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            _resolve_kanban_db()
        # Exit code must be non-zero
        assert exc_info.value.code != 0, (
            f"Expected non-zero exit code, got {exc_info.value.code}"
        )

    def test_empty_env_exits(self, monkeypatch):
        """HERMES_KANBAN_DB 设为空字符串 → 同样退出。"""
        from talos.executor.constants import _resolve_kanban_db
        monkeypatch.setenv("HERMES_KANBAN_DB", "")
        with pytest.raises(SystemExit) as exc_info:
            _resolve_kanban_db()
        assert exc_info.value.code != 0

    def test_set_env_returns_path(self, monkeypatch):
        """HERMES_KANBAN_DB 正确设置 → 返回 Path，不退出。"""
        from talos.executor.constants import _resolve_kanban_db
        monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/test-kanban.db")
        result = _resolve_kanban_db()
        assert isinstance(result, Path)
        assert str(result) == "/tmp/test-kanban.db"


# ═══════════════════════════════════════════════════════════════
# 2. 影子库告警
# ═══════════════════════════════════════════════════════════════

class TestShadowDBWarning:
    """影子库检查：~/.hermes/kanban.db 存在 → 告警；归档文件不触发。"""

    def test_shadow_db_exists_warns(self, tmp_path):
        """精确路径 kanban.db 存在 → 返回告警消息。"""
        fake_home = tmp_path / "hermes"
        fake_home.mkdir()
        shadow = fake_home / "kanban.db"
        shadow.write_bytes(b"\x00" * 64)

        from talos.executor.constants import _check_shadow_db
        warning = _check_shadow_db(home=fake_home)
        assert warning is not None, (
            "Expected shadow DB warning when kanban.db exists"
        )
        assert "kanban.db" in warning

    def test_only_archive_files_no_warning(self, tmp_path):
        """只有 kanban.db.archived-* / .bak / -wal / -shm → 不告警。"""
        fake_home = tmp_path / "hermes"
        fake_home.mkdir()
        (fake_home / "kanban.db.archived-20260917").write_bytes(b"\x00" * 64)
        (fake_home / "kanban.db.bak").write_bytes(b"\x00" * 64)
        (fake_home / "kanban.db-wal").write_bytes(b"\x00" * 64)
        (fake_home / "kanban.db-shm").write_bytes(b"\x00" * 64)

        from talos.executor.constants import _check_shadow_db
        warning = _check_shadow_db(home=fake_home)
        assert warning is None, (
            f"Should NOT warn when only archive/backup files exist, got: {warning}"
        )

    def test_no_files_no_warning(self, tmp_path):
        """没有任何 kanban.db 文件 → 不告警。"""
        fake_home = tmp_path / "hermes"
        fake_home.mkdir()

        from talos.executor.constants import _check_shadow_db
        warning = _check_shadow_db(home=fake_home)
        assert warning is None
