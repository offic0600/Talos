"""
test_suite_rules.py — 验收套件规则判定的单元测试。

覆盖四条判定规则：
  1. 任一断言为 False → 未验（details 含 FAIL 标记）
  2. 任一证据路径不存在 → 未验
  3. 源码检查类断言注册被拒（AccItem.__post_init__ 抛 ValueError）
  4. 不适用缺理由 → 未验

这些测试针对套件框架的规则引擎，不跑真实验收项。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
TALOS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TALOS_ROOT))

# Set required env vars for _config.py before importing run_all
os.environ.setdefault("TALOS_GITLAB_URL", "https://gitlab.example.com")
os.environ.setdefault("TALOS_PILOT_REPO", "https://gitlab.example.com/group/project.git")
os.environ.setdefault("TALOS_PILOT_PROJECT_ID", "group%2Fproject")
os.environ.setdefault("TALOS_ACC_RESULTS_FILE", "/tmp/acc_test_results.jsonl")


# ── Import the suite framework ────────────────────────────────

from tests.acceptance.run_all import (
    AccResult,
    AccItem,
    evaluate_result,
    PASS,
    UNVERIFIED,
    NOT_APPLICABLE,
    _SOURCE_CHECK_REJECTION_MSG,
)


class TestRuleEngineFalseAssertion:
    """规则 1: 任一断言为 False → 未验（details 含 FAIL 标记）"""

    def test_all_pass_returns_pass(self):
        """全部断言通过 → 通过"""
        result = AccResult(
            item_id="TEST-001",
            description="测试项",
            category="auto",
            conclusion=PASS,
            evidence="db: tasks表有记录",
            details="断言1: PASS; 断言2: PASS",
        )
        verdict = evaluate_result(result)
        assert verdict == PASS

    def test_one_fail_marks_unverified(self):
        """任一断言失败 → 未验"""
        result = AccResult(
            item_id="TEST-002",
            description="测试项",
            category="auto",
            conclusion=PASS,
            evidence="db: tasks表有记录",
            details="断言1: PASS; 断言2: FAIL: 值不匹配",
        )
        verdict = evaluate_result(result)
        assert verdict == UNVERIFIED

    def test_fail_detail_preserved(self):
        """失败断言保留在 details 中"""
        result = AccResult(
            item_id="TEST-003",
            description="测试项",
            category="auto",
            conclusion=PASS,
            evidence="",
            details="关键断言: FAIL: 期望10实际20",
        )
        verdict = evaluate_result(result)
        assert verdict == UNVERIFIED
        assert "FAIL" in result.details


class TestRuleEngineMissingEvidence:
    """规则 2: 任一证据路径不存在 → 未验"""

    def test_all_paths_exist_passes(self):
        """全部证据路径存在 → 通过"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ev1 = Path(tmpdir) / "evidence1.json"
            ev2 = Path(tmpdir) / "evidence2.log"
            ev1.write_text("{}")
            ev2.write_text("log entry")

            result = AccResult(
                item_id="TEST-004",
                description="测试项",
                category="auto",
                conclusion=PASS,
                evidence=f"{ev1}; {ev2}",
                details="断言1: PASS",
            )
            verdict = evaluate_result(result)
            assert verdict == PASS

    def test_missing_path_marks_unverified(self):
        """任一证据路径不存在 → 未验"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ev1 = Path(tmpdir) / "exists.json"
            ev1.write_text("{}")
            missing = str(Path(tmpdir) / "does_not_exist.json")

            result = AccResult(
                item_id="TEST-005",
                description="测试项",
                category="auto",
                conclusion=PASS,
                evidence=f"{ev1}; {missing}",
                details="断言1: PASS",
            )
            verdict = evaluate_result(result)
            assert verdict == UNVERIFIED


class TestRuleEngineSourceCodeRejection:
    """规则 3: 源码检查类断言注册被拒"""

    def test_source_grep_evidence_rejected(self):
        """evidence_sources 含 grep source → AccItem 构造被拒"""
        with pytest.raises(ValueError, match=_SOURCE_CHECK_REJECTION_MSG):
            AccItem(
                item_id="BAD-001",
                description="源码检查项",
                category="auto",
                fn=lambda: None,
                evidence_sources=["grep source code for pattern"],
            )

    def test_docstring_evidence_rejected(self):
        """evidence_sources 含 docstring → AccItem 构造被拒"""
        with pytest.raises(ValueError, match=_SOURCE_CHECK_REJECTION_MSG):
            AccItem(
                item_id="BAD-002",
                description="docstring检查项",
                category="auto",
                fn=lambda: None,
                evidence_sources=["read docstring of loop.py"],
            )

    def test_runtime_evidence_accepted(self):
        """运行时产物证据 → AccItem 构造成功"""
        item = AccItem(
            item_id="GOOD-001",
            description="运行时检查项",
            category="auto",
            fn=lambda: None,
            evidence_sources=["db: tasks表", "log: executor.jsonl"],
        )
        assert item.item_id == "GOOD-001"


class TestRuleEngineNotApplicable:
    """规则 4: 不适用缺理由 → 未验"""

    def test_not_applicable_with_reason(self):
        """不适用 + 附理由 → 不适用"""
        result = AccResult(
            item_id="TEST-006",
            description="测试项",
            category="auto",
            conclusion=NOT_APPLICABLE,
            evidence="",
            details="本环境无 systemd，M1 的自动拉起验证不适用",
        )
        verdict = evaluate_result(result)
        assert verdict == NOT_APPLICABLE

    def test_not_applicable_without_reason_marks_unverified(self):
        """不适用 + 无理由 → 未验"""
        result = AccResult(
            item_id="TEST-007",
            description="测试项",
            category="auto",
            conclusion=NOT_APPLICABLE,
            evidence="",
            details="",
        )
        verdict = evaluate_result(result)
        assert verdict == UNVERIFIED

    def test_not_applicable_none_reason_marks_unverified(self):
        """不适用 + details=None → 未验"""
        result = AccResult(
            item_id="TEST-008",
            description="测试项",
            category="auto",
            conclusion=NOT_APPLICABLE,
            evidence="",
            details=None,
        )
        verdict = evaluate_result(result)
        assert verdict == UNVERIFIED


class TestResultJsonFieldLevelCheck:
    """规则 5: result.json 字段级核对——结构字段必须相等，自由文本不比"""

    def test_structural_field_mismatch_fails(self):
        """status 字段不匹配 → verify_worker_output 返回 False"""
        from tests.acceptance.run_all import verify_worker_output
        import tempfile, json
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            # Simulate archive dir with result.json
            archive = Path(tmpdir) / "archived" / "t_test" / "1"
            archive.mkdir(parents=True)
            result_data = {
                "schema": 1,
                "status": "done",  # expected: done
                "summary": "worker wrote this",
                "artifacts": [],
                "subtasks": [],
                "request_review": False,
                "comments": ["some comment"],
                "self_check": {"verification_ran": True, "notes": "ok"},
            }
            (archive / "result.json").write_text(json.dumps(result_data))

            expected = {
                "schema": 1,
                "status": "blocked",  # mismatch!
                "summary": "different summary",
                "artifacts": [],
                "subtasks": [],
                "request_review": False,
                "comments": [],
                "self_check": {"verification_ran": False},
            }

            # Patch archive_dir to point to our temp dir
            with patch("tests.acceptance.run_all.archive_dir", return_value=archive):
                with patch("tests.acceptance.run_all.task_dir", return_value=archive):
                    ok, detail, ev_src = verify_worker_output(
                        "t_test", 1, expected, None
                    )
            assert not ok
            assert "status" in detail
            assert "mismatch" in detail

    def test_free_text_fields_ignored(self):
        """summary/comments/self_check.notes 不同 → 仍通过"""
        from tests.acceptance.run_all import verify_worker_output
        import tempfile, json
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "archived" / "t_test" / "1"
            archive.mkdir(parents=True)
            result_data = {
                "schema": 1,
                "status": "done",
                "summary": "worker's own summary text",
                "artifacts": [],
                "subtasks": [],
                "request_review": False,
                "comments": ["worker's comment"],
                "self_check": {"verification_ran": True, "notes": "worker notes"},
            }
            (archive / "result.json").write_text(json.dumps(result_data))

            expected = {
                "schema": 1,
                "status": "done",
                "summary": "different summary",  # ignored
                "artifacts": [],
                "subtasks": [],
                "request_review": False,
                "comments": ["different comment"],  # ignored
                "self_check": {"verification_ran": True, "notes": "different"},  # ignored
            }

            with patch("tests.acceptance.run_all.archive_dir", return_value=archive):
                with patch("tests.acceptance.run_all.task_dir", return_value=archive):
                    ok, detail, ev_src = verify_worker_output(
                        "t_test", 1, expected, None
                    )
            assert ok, f"Should pass: {detail}"
            assert ev_src.startswith("archive:")

    def test_evidence_source_format(self):
        """evidence_source 只能是 archive: 或 task_dir: 前缀"""
        from tests.acceptance.run_all import verify_worker_output
        import tempfile, json
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "archived" / "t_test" / "1"
            archive.mkdir(parents=True)
            (archive / "result.json").write_text(json.dumps({"status": "done"}))

            with patch("tests.acceptance.run_all.archive_dir", return_value=archive):
                with patch("tests.acceptance.run_all.task_dir", return_value=archive):
                    ok, _, ev_src = verify_worker_output(
                        "t_test", 1, {"status": "done"}, None
                    )
            assert ok
            assert ev_src.startswith("archive:") or ev_src.startswith("task_dir:")
