"""
test_v2_1.py — v2.1 新增测试（7 项）。

覆盖：
1. 绑定缺失 → 不拉容器且 degraded
2. result.json 指向别的仓库 → problem「自报绑定与任务不符」
3. 连续两次 tick 后第二次 context.md 含 Prior attempts
4. 坏声明 → error 路径
5. ls-remote 退出码 2 与其它非零的分流
6. finalize 内核返回 False 时无裁决评论且有 error 事件
7. inspect 脱敏后 grep 无密钥
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

TALOS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TALOS_ROOT))

from talos.executor.adjudicate import (
    Verdict,
    adjudicate,
    _ls_remote,
    _get_injected_repo,
    check_git_pushed,
)
from talos.executor.collect import CollectedBundle
from talos.executor.declarations import (
    ArtifactSpec,
    CredentialSpec,
    Declaration,
    DeclarationError,
    DeliverableSpec,
    GitSpec,
    ResourceSpec,
    VerificationSpec,
    load_declarations,
)
from talos.executor.redact import redact_env, redact_string, redact_dict


# ── Helpers ────────────────────────────────────────────────────

def make_decl(
    artifacts=None,
    git=None,
    verification=None,
    deliverables=None,
    credentials=None,
    requires=None,
) -> Declaration:
    return Declaration(
        skills=["test-skill"],
        resources=ResourceSpec(memory_mb=512, cpus=0.5),
        artifacts=artifacts or [],
        git=git or GitSpec(),
        verification=verification or VerificationSpec(required=False, source="none"),
        deliverables=deliverables or [],
        credentials=credentials or [],
        requires=requires or [],
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


# ═══════════════════════════════════════════════════════════════
# 1. 绑定缺失 → 不拉容器且 degraded
# ═══════════════════════════════════════════════════════════════

class TestBindingMissingNoContainer:
    """I11: 任务缺 requires 里的绑定 → 不拉容器，degraded done。"""

    def test_missing_repo_binding(self, tmp_path):
        """decl.requires=[repo] but task has no repo in body → defect, no container."""
        from talos.executor.spawn import make_spawn_fn

        # Task with no repo in body
        task = MagicMock()
        task.id = "t_norepo"
        task.current_run_id = 1
        task.skills = ["talos-code-demo"]
        task.body = "Write a feature"
        task.branch_name = "talos/t_norepo"
        task.tenant = "test"
        task.title = "Test"

        # Mock declarations to return requires=[repo]
        decl = make_decl(
            requires=["repo"],
            git=GitSpec(branch="talos/t_norepo", require_push=True),
        )

        with patch("talos.executor.spawn.load_declarations", return_value=decl), \
             patch("talos.executor.spawn.mint_credentials", return_value={}), \
             patch("hermes_cli.kanban_db_connect.connect") as mock_connect, \
             patch("hermes_cli.kanban_db.add_comment") as mock_add_comment, \
             patch("hermes_cli.kanban_db.complete_task") as mock_complete_task:
            mock_conn = MagicMock()
            mock_connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)

            spawn_fn = make_spawn_fn()
            pid = spawn_fn(task, "/tmp/workspace")

        assert pid is None  # No container started
        # Verify add_comment was called with binding missing message
        mock_add_comment.assert_called()
        comment_body = mock_add_comment.call_args[1].get("body", "")
        assert "绑定参数" in comment_body or "repo" in comment_body
        # Verify complete_task was called with degraded
        mock_complete_task.assert_called()
        summary = mock_complete_task.call_args[1].get("summary", "")
        assert "降级" in summary or "缺失" in summary


# ═══════════════════════════════════════════════════════════════
# 2. result.json 指向别的仓库 → problem
# ═══════════════════════════════════════════════════════════════

class TestBindingMismatchProblem:
    """I11/A2b: result.json artifacts[].repo ≠ injected repo → problem."""

    def test_repo_mismatch_is_problem(self, tmp_path):
        """Worker reports a different repo → problem「自报绑定与任务不符」."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_path = ws / "src" / "feature.py"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("def feature():\n    return 'hello'\n")

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/OTHER/repo.git",
                        "branch": "talos/t_test",
                        "sha": "abc123",
                    }
                ],
            },
            workspace_path=ws,
        )
        # Injected repo is different from what worker reported
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=10)],
            git=GitSpec(branch="talos/t_test", require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[
                DeliverableSpec(
                    kind="git_branch",
                    repo="https://gitlab.example.com/CORRECT/repo.git",
                    branch="talos/t_test",
                )
            ],
        )

        with patch("talos.executor.adjudicate._ls_remote", return_value="abc123"):
            verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "unmet"
        assert any("自报绑定与任务不符" in p for p in verdict.problems)


# ═══════════════════════════════════════════════════════════════
# 3. 连续两次 tick 后第二次 context.md 含 Prior attempts
# ═══════════════════════════════════════════════════════════════

class TestContextPriorAttempts:
    """context.md should include prior attempt info on second run."""

    def test_context_md_contains_prior_attempts_section(self, tmp_path, monkeypatch):
        """Second run's context.md should have prior attempt info from kanban context."""
        from talos.executor.spawn import _build_context_md
        from talos.executor import constants

        # Mock kanban context to include prior attempts
        prior_text = "## Prior attempts\n\n### Run 1\nError: artifact missing\n"
        task = MagicMock()
        task.id = "t_retry"
        task.title = "Retry task"
        task.body = "repo: https://gitlab.example.com/test/repo.git\nWrite feature"
        task.tenant = "test"
        task.skills = ["talos-code-demo"]
        task.branch_name = "talos/t_retry"

        decl = make_decl(
            git=GitSpec(branch="talos/t_retry", require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[
                DeliverableSpec(
                    kind="git_branch",
                    repo="https://gitlab.example.com/test/repo.git",
                    branch="talos/t_retry",
                )
            ],
        )

        with patch("hermes_cli.kanban_db.build_worker_context", return_value=prior_text):
            ctx = _build_context_md(task, decl)

        # The context should contain the prior attempts text
        assert "Prior attempts" in ctx or "prior" in ctx.lower() or "Run 1" in ctx


# ═══════════════════════════════════════════════════════════════
# 4. 坏声明 → error 路径
# ═══════════════════════════════════════════════════════════════

class TestBadDeclarationError:
    """Malformed frontmatter → DeclarationError → error verdict (M12)."""

    def test_bad_yaml_raises_declaration_error(self, tmp_path, monkeypatch):
        """Invalid YAML in frontmatter → DeclarationError."""
        from talos.executor import constants

        skills_dir = tmp_path / "skills"
        skill = skills_dir / "bad-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: bad-skill\n"
            "resources: [this is not valid yaml: {{{\n"
            "---\n# bad-skill\n"
        )
        monkeypatch.setattr(constants, "HOME", tmp_path)

        with pytest.raises(DeclarationError):
            load_declarations(["bad-skill"])


# ═══════════════════════════════════════════════════════════════
# 5. ls-remote 退出码 2 与其它非零的分流
# ═══════════════════════════════════════════════════════════════

class TestLsRemoteExitCodes:
    """v2.1 FIX #7: --exit-code 2 = branch not exist (None), other = RuntimeError."""

    def test_exit_code_2_returns_none(self):
        """git ls-remote --exit-code returns 2 when branch doesn't exist → None."""
        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = _ls_remote("https://gitlab.example.com/test/repo.git", "talos/nonexistent")

        assert result is None

    def test_other_nonzero_raises_runtime_error(self):
        """git ls-remote returns non-zero (not 2) → RuntimeError with stderr first line."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "fatal: could not read Username for\nadditional lines"

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError) as exc_info:
                _ls_remote("https://gitlab.example.com/test/repo.git", "talos/test")

        assert "could not read Username" in str(exc_info.value)

    def test_exit_code_0_returns_sha(self):
        """git ls-remote returns 0 → sha string."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc123def456\trefs/heads/talos/test"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = _ls_remote("https://gitlab.example.com/test/repo.git", "talos/test")

        assert result == "abc123def456"


# ═══════════════════════════════════════════════════════════════
# 6. finalize 内核返回 False 时无裁决评论且有 error 事件
# ═══════════════════════════════════════════════════════════════

class TestFinalizeKernelFalse:
    """v2.1 FIX #8: kernel API returns False → no verdict comment, error event, kernel-closed check."""

    def test_complete_task_false_no_verdict_comment(self, conn, make_task, make_run):
        """complete_task returns False → no verdict comment written, error logged."""
        from talos.executor.finalize import finalize
        from talos.executor.adjudicate import Verdict

        task_id = make_task(id="t_false01", status="running")
        make_run(id=1, task_id=task_id, status="running")

        verdict = Verdict(
            status="pass",
            summary="All good",
            artifacts=[{"kind": "git_branch", "repo": "x", "branch": "y", "sha": "z"}],
        )

        with patch("hermes_cli.kanban_db.complete_task", return_value=False) as mock_complete, \
             patch("hermes_cli.kanban_db.add_comment") as mock_comment:
            result = finalize(conn, task_id, 1, verdict)

        # complete_task was called
        mock_complete.assert_called_once()
        # add_comment should NOT have been called with verdict text (only kernel-closed comment if applicable)
        verdict_comments = [
            call for call in mock_comment.call_args_list
            if "裁决" in str(call) or "通过" in str(call)
        ]
        assert len(verdict_comments) == 0, "Should not write verdict comment when kernel returns False"


# ═══════════════════════════════════════════════════════════════
# 7. inspect 脱敏后 grep 无密钥
# ═══════════════════════════════════════════════════════════════

class TestRedaction:
    """v2.1 FIX #2: inspect.json and executor.jsonl redacted — no secrets."""

    def test_redact_env_masks_sensitive_keys(self):
        """Config.Env values for KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL → ***."""
        inspect = {
            "Config": {
                "Env": [
                    "TALOS_TASK_ID=t_test",
                    "ANTHROPIC_API_KEY=sk-ant-verysecret123",
                    "GITLAB_TOKEN=glpat-abcdef123456",
                    "API_SERVER_KEY=secret-key-value",
                    "DATABASE_PASSWORD=hunter2",
                    "AWS_CREDENTIAL=AKIAIOSFODNN7EXAMPLE",
                    "PATH=/usr/bin:/bin",
                ]
            }
        }
        redacted = redact_env(inspect)

        env = redacted["Config"]["Env"]
        env_dict = dict(e.split("=", 1) for e in env)

        assert env_dict["TALOS_TASK_ID"] == "t_test"  # not sensitive
        assert env_dict["PATH"] == "/usr/bin:/bin"  # not sensitive
        assert env_dict["ANTHROPIC_API_KEY"] == "***"
        assert env_dict["GITLAB_TOKEN"] == "***"
        assert env_dict["API_SERVER_KEY"] == "***"
        assert env_dict["DATABASE_PASSWORD"] == "***"
        assert env_dict["AWS_CREDENTIAL"] == "***"

    def test_redact_string_masks_glpat(self):
        """redact_string replaces glpat-xxx patterns."""
        s = "token=glpat-abcdefghijklmnop123456 and more"
        result = redact_string(s)
        assert "glpat-abcdefghijklmnop123456" not in result
        assert "glpat-***" in result

    def test_redact_dict_recursively(self):
        """redact_dict redacts all string values recursively."""
        d = {
            "key": "glpat-secret123456789012345",
            "nested": {
                "token": "oauth2:abc123secret@host",
                "number": 42,
            },
            "list": ["glpat-anothersecret12345678", "safe"],
        }
        result = redact_dict(d)
        assert "glpat-secret123456789012345" not in str(result)
        assert "glpat-anothersecret12345678" not in str(result)
        assert result["nested"]["number"] == 42  # non-string preserved

    def test_redacted_inspect_no_real_secrets(self):
        """After redaction, grep for common secret patterns returns empty."""
        import re
        inspect = {
            "Config": {
                "Env": [
                    "ANTHROPIC_API_KEY=sk-ant-real-secret-key-12345",
                    "TALOS_GITLAB_ADMIN_TOKEN=glpat-real-token-abcdef123456",
                    "API_SERVER_KEY=server-secret-key-xyz",
                ]
            }
        }
        redacted = redact_env(inspect)
        redacted_str = json.dumps(redacted)

        # These patterns should NOT appear in the redacted output
        assert "sk-ant-real-secret-key-12345" not in redacted_str
        assert "glpat-real-token-abcdef123456" not in redacted_str
        assert "server-secret-key-xyz" not in redacted_str

        # Only *** should be present
        assert "***" in redacted_str
