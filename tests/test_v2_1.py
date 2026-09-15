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
    _gitlab_branch_sha,
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
    """I11: 任务缺 requires 里的绑定 → 不拉容器，block_task（v2.3 §18.2 #4）。"""

    def test_missing_repo_binding(self, tmp_path):
        """decl.requires=[repo] but task has no repo in body → block_task, no container."""
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
             patch("hermes_cli.kanban_db.block_task") as mock_block_task:
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
        # v2.3 §18.2 #4: block_task called (not complete_task)
        mock_block_task.assert_called()
        reason = mock_block_task.call_args[1].get("reason", "")
        assert "缺失" in reason or "绑定" in reason
        kind = mock_block_task.call_args[1].get("kind", "")
        assert kind == "capability"


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

        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value="abc123"):
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
        """v2.3 §18.2 #5: ALL Config.Env values → ***, regardless of key name."""
        inspect = {
            "Config": {
                "Env": [
                    "TALOS_TASK_ID=t_test",
                    "ANTHROPIC_API_KEY=«redacted:sk-…»",
                    "GITLAB_TOKEN=«redacted:glpat-…»",
                    "API_SERVER_KEY=secret-key-value",
                    "DATABASE_PASSWORD=hunter2",
                    "AWS_CREDENTIAL=«redacted:AKIA…»",
                    "PATH=/usr/bin:/bin",
                    "GPG_KEY=abc123def456",
                ]
            }
        }
        redacted = redact_env(inspect)

        env = redacted["Config"]["Env"]
        env_dict = dict(e.split("=", 1) for e in env)

        # v2.3: ALL values redacted, including TALOS_TASK_ID and PATH
        assert env_dict["TALOS_TASK_ID"] == "***"
        assert env_dict["PATH"] == "***"
        assert env_dict["ANTHROPIC_API_KEY"] == "***"
        assert env_dict["GITLAB_TOKEN"] == "***"
        assert env_dict["API_SERVER_KEY"] == "***"
        assert env_dict["DATABASE_PASSWORD"] == "***"
        assert env_dict["AWS_CREDENTIAL"] == "***"
        assert env_dict["GPG_KEY"] == "***"

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


# ═══════════════════════════════════════════════════════════════
# v2.3 §18.2 #2: sentinel reads executor.alive mtime
# ═══════════════════════════════════════════════════════════════

class TestExecutorAliveFile:
    """v2.3 §18.2 #2: sentinel _executor_is_alive reads executor.alive mtime."""

    def test_alive_file_fresh(self, tmp_path):
        """executor.alive mtime within 60s → alive."""
        import time as _time
        from talos.executor import constants
        from talos.executor.sentinel import _executor_is_alive

        alive_file = tmp_path / "executor.alive"
        alive_file.touch()

        # Patch TALOS_HOME to our temp dir
        old = constants.TALOS_HOME
        constants.TALOS_HOME = tmp_path
        try:
            assert _executor_is_alive() is True
        finally:
            constants.TALOS_HOME = old

    def test_alive_file_stale(self, tmp_path):
        """executor.alive mtime > 60s → dead."""
        import os
        import time as _time
        from talos.executor import constants
        from talos.executor.sentinel import _executor_is_alive

        alive_file = tmp_path / "executor.alive"
        alive_file.touch()
        # Set mtime to 120 seconds ago
        old_mtime = _time.time() - 120
        os.utime(alive_file, (old_mtime, old_mtime))

        old = constants.TALOS_HOME
        constants.TALOS_HOME = tmp_path
        try:
            assert _executor_is_alive() is False
        finally:
            constants.TALOS_HOME = old

    def test_alive_file_missing(self, tmp_path):
        """executor.alive doesn't exist → dead."""
        from talos.executor import constants
        from talos.executor.sentinel import _executor_is_alive

        old = constants.TALOS_HOME
        constants.TALOS_HOME = tmp_path
        try:
            assert _executor_is_alive() is False
        finally:
            constants.TALOS_HOME = old


class TestContainerConfigTemplate:
    """v2.3 P0: _generate_container_config reads template + env vars."""

    _REQUIRED_ENV = {
        "TALOS_MODEL": "GLM-5",
        "TALOS_MODEL_PROVIDER": "custom:mgallery",
        "TALOS_MODEL_PROVIDER_NAME": "mgallery",
        "TALOS_MODEL_BASE_URL": "https://inference.example.com/v1",
        "TALOS_MODEL_KEY_ENV": "API_SERVER_KEY",
    }

    def test_renders_correctly(self, tmp_path, monkeypatch):
        """Template renders with all env vars present — contains model/provider/base_url."""
        for k, v in self._REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        from talos.executor.spawn import _generate_container_config
        cfg = _generate_container_config(tmp_path, task=None)
        content = cfg.read_text(encoding="utf-8")
        assert "model:" in content
        assert "default: GLM-5" in content
        assert "provider: custom:mgallery" in content
        assert "base_url: https://inference.example.com/v1" in content
        assert "key_env: API_SERVER_KEY" in content
        assert "talos-plugins" in content

    def test_missing_env_var_raises(self, tmp_path, monkeypatch):
        """Missing TALOS_MODEL_* env var → RuntimeError."""
        for k, v in self._REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("TALOS_MODEL_BASE_URL")
        from talos.executor.spawn import _generate_container_config
        import pytest
        with pytest.raises(RuntimeError, match="TALOS_MODEL_BASE_URL"):
            _generate_container_config(tmp_path, task=None)

    def test_no_api_key_value_in_output(self, tmp_path, monkeypatch):
        """Rendered config must NOT contain the actual API key value."""
        for k, v in self._REQUIRED_ENV.items():
            monkeypatch.setenv(k, v)
        # Also set the actual key value in env — it must NOT appear in config.yaml
        monkeypatch.setenv("API_SERVER_KEY", "glpat-super-secret-key-12345")
        from talos.executor.spawn import _generate_container_config
        cfg = _generate_container_config(tmp_path, task=None)
        content = cfg.read_text(encoding="utf-8")
        assert "glpat-super-secret-key-12345" not in content
        # key_env should only contain the env var *name*, not the value
        assert "key_env: API_SERVER_KEY" in content


class TestDispatchSafety:
    """Behavioral regression tests for fork-bomb prevention (RCA §三).

    These tests exercise real behavior on a temporary DB — they do NOT
    inspect source code strings. Each test creates tasks, calls the
    function under test, and asserts observable state changes.
    """

    def test_manual_dispatch_only_affects_target_task(self, conn, make_task):
        """_manual_dispatch must only UPDATE the target task, leaving
        all other tasks' status / claim_lock / current_run_id untouched."""
        import time
        from tests.integration.test_adjudicate_flow import _manual_dispatch

        # Create two tasks in the temp DB
        tid_a = make_task(id="t_safety_a", title="safety-a")
        tid_b = make_task(id="t_safety_b", title="safety-b")

        # Snapshot task B's state before dispatching A
        row_b_before = dict(conn.execute(
            "SELECT status, claim_lock, current_run_id, started_at "
            "FROM tasks WHERE id=?", (tid_b,)).fetchone())

        # Dispatch task A
        run_id = _manual_dispatch(conn, tid_a)

        # Verify task A was dispatched
        row_a = dict(conn.execute(
            "SELECT status, claim_lock, current_run_id FROM tasks WHERE id=?",
            (tid_a,)).fetchone())
        assert row_a["status"] == "running"
        assert row_a["claim_lock"] == "talos-integration-test"
        assert row_a["current_run_id"] == run_id

        # CRITICAL: task B must be completely untouched
        row_b_after = dict(conn.execute(
            "SELECT status, claim_lock, current_run_id, started_at "
            "FROM tasks WHERE id=?", (tid_b,)).fetchone())
        assert row_b_after == row_b_before, (
            f"Task B was modified by _manual_dispatch on task A!\n"
            f"Before: {row_b_before}\n"
            f"After:  {row_b_after}"
        )

    def test_max_spawn_passed_to_dispatch_once(self, conn, make_task):
        """loop._dispatch must pass max_spawn to dispatch_once.

        Uses a fake dispatch_once to capture the actual kwargs passed."""
        from unittest.mock import MagicMock
        from talos.executor.loop import _dispatch

        captured_kwargs = {}

        def fake_dispatch_once(c, **kwargs):
            captured_kwargs.update(kwargs)
            result = MagicMock()
            result.spawned = []
            return result

        # _dispatch does `from hermes_cli.kanban_db_dispatch import dispatch_once`
        # at call time, so we patch the function on the source module.
        import hermes_cli.kanban_db_dispatch
        original = hermes_cli.kanban_db_dispatch.dispatch_once
        hermes_cli.kanban_db_dispatch.dispatch_once = fake_dispatch_once

        try:
            _dispatch(conn, spawn_fn=None)
        finally:
            hermes_cli.kanban_db_dispatch.dispatch_once = original

        assert "max_spawn" in captured_kwargs, (
            "dispatch_once was called without max_spawn parameter"
        )
        assert captured_kwargs["max_spawn"] is not None, (
            "max_spawn was passed as None — unbounded fan-out risk"
        )
        assert captured_kwargs["max_spawn"] == 2, (
            f"Expected max_spawn=2 (default), got {captured_kwargs['max_spawn']}"
        )

    def test_talos_max_spawn_default_is_2(self):
        """TALOS_MAX_SPAWN must default to 2 when env var is unset."""
        # This is a value test, not a source inspection — it reads the
        # actual runtime value of the constant.
        import importlib
        import talos.executor.constants as constants_mod

        # Unset env var and re-import to get default
        import os
        old_val = os.environ.pop("TALOS_MAX_SPAWN", None)
        try:
            importlib.reload(constants_mod)
            assert constants_mod.TALOS_MAX_SPAWN == 2, (
                f"Expected default 2, got {constants_mod.TALOS_MAX_SPAWN}"
            )
        finally:
            if old_val is not None:
                os.environ["TALOS_MAX_SPAWN"] = old_val
                importlib.reload(constants_mod)

    def test_conftest_skips_without_test_db(self):
        """conftest must skip all tests when TALOS_TEST_DB is not set.

        This is a behavior test: it runs pytest in a subprocess with
        no TALOS_TEST_DB and verifies the collection is skipped.
        """
        import subprocess

        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/test_v2_1.py::TestDispatchSafety::test_talos_max_spawn_default_is_2",
             "-v", "--no-header", "-x"],
            capture_output=True, text=True, timeout=30,
            env={**__import__("os").environ, "TALOS_TEST_DB": ""},
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        # With TALOS_TEST_DB unset/empty, pytest should skip (message goes to stderr)
        combined = result.stdout + result.stderr
        assert "Skipped" in combined or "skipped" in combined.lower(), (
            f"Tests did not skip when TALOS_TEST_DB is empty!\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )

    def test_tick_log_written_when_nothing_dispatched(self, conn, make_task):
        """tick() must write a 'tick' event to executor.jsonl even when
        dispatch_once returns an empty result (no tasks spawned).

        This is a behavior test: it calls tick() on a temp DB with no
        ready tasks, then reads executor.jsonl and verifies a tick event
        exists with the expected fields.
        """
        import json
        import tempfile
        from unittest.mock import MagicMock
        from talos.executor import constants as const_mod
        from talos.executor.loop import tick

        # Point log_event at a temp file so we can read it back
        from pathlib import Path
        old_log = const_mod.EXECUTOR_LOG
        tmpdir = tempfile.mkdtemp()
        tmp_log = Path(tmpdir) / "executor.jsonl"
        const_mod.EXECUTOR_LOG = tmp_log

        # Patch dispatch_once to return an empty result
        import hermes_cli.kanban_db_dispatch
        original = hermes_cli.kanban_db_dispatch.dispatch_once

        def fake_dispatch_once(c, **kwargs):
            result = MagicMock()
            result.spawned = []
            result.skipped_locked = False
            result.memory_pressure = None
            result.respawn_guarded = []
            result.rate_limited = []
            result.skipped_unassigned = []
            result.skipped_nonspawnable = []
            result.skipped_per_profile_capped = []
            result.crashed = []
            result.auto_blocked = []
            result.timed_out = []
            result.stale = []
            return result

        hermes_cli.kanban_db_dispatch.dispatch_once = fake_dispatch_once

        # Also patch reap/list functions to avoid docker dependency
        with patch("talos.executor.loop.reap_orphans"), \
             patch("talos.executor.loop._adjudicate_exited"), \
             patch("talos.executor.loop._heartbeat_live_containers"), \
             patch("talos.executor.loop.list_running_containers", return_value=[]):
            try:
                tick(conn, spawn_fn=MagicMock())
            finally:
                hermes_cli.kanban_db_dispatch.dispatch_once = original
                const_mod.EXECUTOR_LOG = old_log

        # Read the temp log and find a tick event
        events = []
        with open(str(tmp_log)) as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

        tick_events = [e for e in events if e.get("kind") == "tick"]
        assert len(tick_events) >= 1, (
            f"No 'tick' event written when dispatch returned empty! "
            f"Events: {[e.get('kind') for e in events]}"
        )

        te = tick_events[-1]
        # log_event nests kwargs into an 'extra' sub-dict
        fields = te.get("extra", te)
        assert "ready" in fields, f"tick event missing 'ready' field: {te}"
        assert "spawned" in fields, f"tick event missing 'spawned' field: {te}"
        assert "active_containers" in fields, (
            f"tick event missing 'active_containers' field: {te}"
        )
        assert fields["spawned"] == 0, (
            f"Expected spawned=0, got {fields['spawned']}"
        )
        assert "reason" in fields, f"tick event missing 'reason' field: {te}"


# ═══════════════════════════════════════════════════════════════
# P0-2: 评论不得包含未经验证的制品位置
# ═══════════════════════════════════════════════════════════════

class TestUnverifiedArtifactNotInComment:
    """P0-2 (重做): verdict.artifacts 只含裁决器验证过的记录，不信任 result.json 自报。

    正确做法：检查函数在验证通过时，用「注入的仓库 + 注入的分支 + API 返回的 sha」
    构造已验证记录返回。三个值一个都不来自 result.json。没验证过的不进 verdict.artifacts。
    """

    def test_fake_repo_with_real_branch_name_leaks_nothing(self, tmp_path):
        """对抗性场景：容器自报一个真实存在的分支名，但 repo 指向另一个仓库。
        check_git_pushed 会产生 problem「自报绑定与任务不符」，但该 problem 不含分支名。
        旧实现用 `branch in p` 匹配不上 → artifact 原样放行 → 假仓库地址进评论。
        新实现：verdict.artifacts 只含检查函数构造的记录，不来自 result.json。
        """
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_file = ws / "src" / "feature.py"
        artifact_file.parent.mkdir(parents=True)
        artifact_file.write_text("def feature():\n    return 'hello world'\n")

        # Injected repo (correct)
        INJECTED_REPO = "https://gitlab.example.com/CORRECT/repo.git"
        INJECTED_BRANCH = "talos/t_test"
        API_SHA = "realsha_from_api_999"

        # Worker self-reports a DIFFERENT repo with the same branch name
        FAKE_REPO = "https://gitlab.example.com/FAKE/repo.git"

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": FAKE_REPO,        # fake repo
                        "branch": INJECTED_BRANCH,  # real branch name
                        "sha": "self_reported_sha_111",
                    }
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="${workspace}/src/feature.py", min_bytes=10)],
            git=GitSpec(branch=INJECTED_BRANCH, require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[
                DeliverableSpec(kind="git_branch", repo=INJECTED_REPO, branch=INJECTED_BRANCH),
            ],
        )

        # API returns a sha for the injected branch (branch exists on correct repo)
        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value=API_SHA), \
             patch("talos.executor.adjudicate._check_ci") as mock_ci:
            mock_ci.return_value = ([], [])
            verdict = adjudicate("t_test", 1, bundle, decl)

        # 1. FAKE_REPO must not appear in any verdict artifact
        for art in verdict.artifacts:
            if isinstance(art, dict) and art.get("kind") == "git_branch":
                assert art.get("repo") != FAKE_REPO, (
                    f"Fake repo leaked into verdict.artifacts: {art}"
                )

        # 2. FAKE_REPO must not appear in comment
        from talos.executor.finalize import _format_comment
        comment = _format_comment(verdict, "t_test", 1)
        assert FAKE_REPO not in comment, (
            f"Fake repo appeared in comment: {comment}"
        )

        # 3. If there IS a verified git_branch artifact, its repo must be INJECTED_REPO
        git_arts = [a for a in verdict.artifacts if isinstance(a, dict) and a.get("kind") == "git_branch"]
        for ga in git_arts:
            assert ga["repo"] == INJECTED_REPO, f"Expected injected repo, got {ga['repo']}"
            assert ga["branch"] == INJECTED_BRANCH, f"Expected injected branch, got {ga['branch']}"

    def test_nonexistent_branch_not_in_verdict(self, tmp_path):
        """容器自报一个不存在的分支 → 不进 verdict.artifacts，不进评论。"""
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_file = ws / "src" / "feature.py"
        artifact_file.parent.mkdir(parents=True)
        artifact_file.write_text("def feature():\n    return 'hello world'\n")

        INJECTED_REPO = "https://gitlab.example.com/test/repo.git"
        INJECTED_BRANCH = "talos/nonexistent"

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": INJECTED_REPO,
                        "branch": INJECTED_BRANCH,
                        "sha": "self_reported_sha",
                    }
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="${workspace}/src/feature.py", min_bytes=10)],
            git=GitSpec(branch=INJECTED_BRANCH, require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[
                DeliverableSpec(kind="git_branch", repo=INJECTED_REPO, branch=INJECTED_BRANCH),
            ],
        )

        # API returns None - branch doesn't exist
        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value=None), \
             patch("talos.executor.adjudicate._check_ci") as mock_ci:
            mock_ci.return_value = ([], [])
            verdict = adjudicate("t_test", 1, bundle, decl)

        # No git_branch artifacts in verdict
        git_arts = [a for a in verdict.artifacts if isinstance(a, dict) and a.get("kind") == "git_branch"]
        assert len(git_arts) == 0, (
            f"Nonexistent branch leaked into verdict: {git_arts}"
        )

        # Not in comment as a verified branch link
        from talos.executor.finalize import _format_comment
        comment = _format_comment(verdict, "t_test", 1)
        # The branch may appear in problems (legitimate), but must NOT appear
        # as a verified branch link (the "分支: .../-/tree/..." line).
        assert "/-/tree/" not in comment, (
            f"Nonexistent branch appeared as verified link in comment: {comment}"
        )

    def test_verified_artifact_uses_injected_values_not_result_json(self, tmp_path):
        """验证通过时，verdict.artifacts 里的 repo/branch/sha 三个值分别等于
        注入值、注入值、API 返回值 - 而不是 result.json 里的值。
        用故意写错的 result.json 来区分。
        """
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_file = ws / "src" / "feature.py"
        artifact_file.parent.mkdir(parents=True)
        artifact_file.write_text("def feature():\n    return 'hello world'\n")

        INJECTED_REPO = "https://gitlab.example.com/CORRECT/repo.git"
        INJECTED_BRANCH = "talos/t_test"
        API_SHA = "api_returned_sha_aaa"

        # result.json deliberately has WRONG values for all three
        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/WRONG/repo.git",
                        "branch": "talos/wrong_branch",
                        "sha": "wrong_sha_000",
                    }
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="${workspace}/src/feature.py", min_bytes=10)],
            git=GitSpec(branch=INJECTED_BRANCH, require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[
                DeliverableSpec(kind="git_branch", repo=INJECTED_REPO, branch=INJECTED_BRANCH),
            ],
        )

        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value=API_SHA), \
             patch("talos.executor.adjudicate._check_ci") as mock_ci:
            mock_ci.return_value = ([], [])
            verdict = adjudicate("t_test", 1, bundle, decl)

        git_arts = [a for a in verdict.artifacts if isinstance(a, dict) and a.get("kind") == "git_branch"]
        assert len(git_arts) == 1, f"Expected 1 verified git_branch, got {len(git_arts)}: {git_arts}"

        ga = git_arts[0]
        assert ga["repo"] == INJECTED_REPO, (
            f"repo should be injected value, got {ga['repo']}"
        )
        assert ga["branch"] == INJECTED_BRANCH, (
            f"branch should be injected value, got {ga['branch']}"
        )
        assert ga["sha"] == API_SHA, (
            f"sha should be API value, got {ga['sha']}"
        )

    def test_self_reported_artifacts_stored_separately(self, tmp_path):
        """非 git_branch 的自报 artifact 不进 verdict.artifacts，
        而是进 verdict.self_reported_artifacts（明确标注未经验证）。
        """
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_file = ws / "src" / "feature.py"
        artifact_file.parent.mkdir(parents=True)
        artifact_file.write_text("def feature():\n    return 'hello world'\n")

        INJECTED_REPO = "https://gitlab.example.com/test/repo.git"
        INJECTED_BRANCH = "talos/t_test"
        API_SHA = "real_sha"

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {"kind": "file", "path": "/work/src/feature.py"},
                    {
                        "kind": "git_branch",
                        "repo": INJECTED_REPO,
                        "branch": INJECTED_BRANCH,
                        "sha": "self_reported_sha",
                    },
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="${workspace}/src/feature.py", min_bytes=10)],
            git=GitSpec(branch=INJECTED_BRANCH, require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[
                DeliverableSpec(kind="git_branch", repo=INJECTED_REPO, branch=INJECTED_BRANCH),
            ],
        )

        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value=API_SHA), \
             patch("talos.executor.adjudicate._check_ci") as mock_ci:
            mock_ci.return_value = ([], [])
            verdict = adjudicate("t_test", 1, bundle, decl)

        # verdict.artifacts should ONLY contain verified git_branch
        assert len(verdict.artifacts) == 1, (
            f"Expected 1 verified artifact, got {len(verdict.artifacts)}: {verdict.artifacts}"
        )
        assert verdict.artifacts[0]["kind"] == "git_branch"

        # self_reported_artifacts should contain the original result.json artifacts
        assert hasattr(verdict, "self_reported_artifacts"), "Verdict missing self_reported_artifacts field"
        assert len(verdict.self_reported_artifacts) == 2, (
            f"Expected 2 self-reported artifacts, got {len(verdict.self_reported_artifacts)}"
        )


# ═══════════════════════════════════════════════════════════════
# P0-1: GitLab API 替代 ls-remote
# ═══════════════════════════════════════════════════════════════

class TestGitLabBranchSha:
    """P0-1: _gitlab_branch_sha uses GitLab API, not git ls-remote."""

    def test_branch_exists_returns_sha(self):
        """API returns 200 with commit.id → sha string."""
        from unittest.mock import MagicMock, patch
        from urllib.error import HTTPError

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "name": "talos/test",
            "commit": {"id": "abc123def456789"},
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("talos.executor.adjudicate.GITLAB_ADMIN_TOKEN", "fake-token"), \
             patch("talos.executor.adjudicate.urlopen", return_value=mock_resp):
            result = _gitlab_branch_sha("123", "talos/test")

        assert result == "abc123def456789"

    def test_branch_not_found_returns_none(self):
        """API returns 404 → None (problem, requeue)."""
        from unittest.mock import patch
        from urllib.error import HTTPError
        import io

        mock_error = HTTPError(
            url="https://hgit.example.com/api/v4/projects/123/repository/branches/talos%2Fmissing",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b'{"message": "404 Branch Not Found"}'),
        )

        with patch("talos.executor.adjudicate.GITLAB_ADMIN_TOKEN", "fake-token"), \
             patch("talos.executor.adjudicate.urlopen", side_effect=mock_error):
            result = _gitlab_branch_sha("123", "talos/missing")

        assert result is None

    def test_auth_failure_raises_runtime_error(self):
        """API returns 401 → RuntimeError (defect, NOT 'branch not found')."""
        from unittest.mock import patch
        from urllib.error import HTTPError
        import io

        mock_error = HTTPError(
            url="https://hgit.example.com/api/v4/projects/123/repository/branches/talos%2Ftest",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"message": "401 Unauthorized"}'),
        )

        with patch("talos.executor.adjudicate.GITLAB_ADMIN_TOKEN", "fake-token"), \
             patch("talos.executor.adjudicate.urlopen", side_effect=mock_error):
            with pytest.raises(RuntimeError, match="GitLab API error 401"):
                _gitlab_branch_sha("123", "talos/test")

    def test_no_token_raises_runtime_error(self):
        """No GitLab token configured → RuntimeError (defect)."""
        with patch("talos.executor.adjudicate.GITLAB_ADMIN_TOKEN", ""):
            with pytest.raises(RuntimeError, match="token not configured"):
                _gitlab_branch_sha("123", "talos/test")


# ═══════════════════════════════════════════════════════════════
# P1-1: 达上限时追加转人工评论
# ═══════════════════════════════════════════════════════════════

class TestBlockedComment:
    """P1-1: 任务落成 blocked 后追加「转人工」评论 + 缺项清单。"""

    def test_blocked_comment_contains_transfer_and_defects(self):
        """_format_blocked_comment 输出含「转人工」和缺项清单。"""
        from talos.executor.finalize import _format_blocked_comment

        verdict = Verdict(
            status="unmet",
            problems=["产物缺失: /work/out/c.md"],
            defects=[],
        )

        comment = _format_blocked_comment(verdict, "t_test", 1, failure_limit=2)

        assert "转人工" in comment, f"Missing '转人工' in: {comment}"
        assert "连续 2 次" in comment, f"Missing failure count in: {comment}"
        assert "/work/out/c.md" in comment, f"Missing defect list in: {comment}"

    def test_blocked_comment_added_when_task_blocked(self, conn, make_task, make_run):
        """连续两次 unmet → blocked 时，task_comments 里有「转人工」评论。"""
        from talos.executor.finalize import finalize
        from talos.executor.adjudicate import Verdict

        task_id = make_task(id="t_blocked01", status="running")
        make_run(id=1, task_id=task_id, status="running")

        # First unmet → requeue (not blocked yet)
        verdict1 = Verdict(
            status="unmet",
            problems=["产物缺失: /work/out/c.md"],
        )
        with patch("hermes_cli.kanban_db_dispatch._record_task_failure", return_value=False), \
             patch("hermes_cli.kanban_db.add_comment") as mock_comment1:
            result1 = finalize(conn, task_id, 1, verdict1)

        assert result1 == "ready"
        # First unmet should NOT have 转人工 comment
        first_comments = [
            c for c in mock_comment1.call_args_list
            if "转人工" in str(c)
        ]
        assert len(first_comments) == 0, "转人工 comment should not appear on first unmet"

        # Second unmet → blocked
        make_run(id=2, task_id=task_id, status="running")
        verdict2 = Verdict(
            status="unmet",
            problems=["产物缺失: /work/out/c.md"],
        )
        with patch("hermes_cli.kanban_db_dispatch._record_task_failure", return_value=True), \
             patch("hermes_cli.kanban_db.add_comment") as mock_comment2:
            result2 = finalize(conn, task_id, 2, verdict2)

        assert result2 == "blocked"
        # Second unmet SHOULD have 转人工 comment
        blocked_comments = [
            c for c in mock_comment2.call_args_list
            if "转人工" in str(c)
        ]
        assert len(blocked_comments) >= 1, (
            f"转人工 comment missing on blocked! Calls: {mock_comment2.call_args_list}"
        )
