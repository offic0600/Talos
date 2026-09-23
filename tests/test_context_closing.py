"""Test: context.md closing requirements driven by declaration (DD §5.3 correction).

Two tests that MUST fail on the old code (which always emits push/clone text
regardless of decl.git.require_push) and pass after the fix.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from talos.executor.declarations import (
    Declaration,
    DeliverableSpec,
    GitSpec,
    ResourceSpec,
    VerificationSpec,
)


def _make_task(task_id="t_test001", title="test task", body="do something"):
    """Create a minimal mock task object compatible with _build_context_md."""
    task = MagicMock()
    task.id = task_id
    task.title = title
    task.body = body
    task.branch_name = ""
    task.tenant = ""
    task.current_run_id = 1
    task.repo = ""
    return task


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Prevent actual DB connection inside _build_context_md."""
    def _fake_build_context(conn, tid):
        return f"# Kanban task {tid}\n## Body\nfake body"

    monkeypatch.setattr(
        "talos.executor.spawn.build_worker_context",
        _fake_build_context,
        raising=False,
    )
    # Also patch the import inside the function
    import builtins
    real_import = builtins.__import__

    def _patched_import(name, *args, **kwargs):
        if name == "hermes_cli":
            mod = types.ModuleType("hermes_cli")
            mod.kanban_db_connect = MagicMock()
            return mod
        if name == "hermes_cli.kanban_db":
            mod = types.ModuleType("hermes_cli.kanban_db")
            mod.build_worker_context = _fake_build_context
            mod.kanban_db_connect = MagicMock()
            return mod
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _patched_import)


from talos.executor.spawn import _build_context_md


class TestContextClosingRequirements:
    """DD §5.3: closing requirements must be driven by declaration."""

    def test_no_push_when_require_push_false(self):
        """decl.git.require_push=False and no git_branch deliverable →
        context.md must NOT contain push/clone/git_branch artifact text."""
        decl = Declaration(
            skills=["talos-doc-demo"],
            resources=ResourceSpec(),
            artifacts=[],
            git=GitSpec(branch="", require_push=False),
            verification=VerificationSpec(),
            deliverables=[],
        )
        task = _make_task()
        ctx = _build_context_md(task, decl)

        # Must NOT contain push instructions
        assert "推到分支" not in ctx, f"Found push instruction when require_push=False:\n{ctx}"
        assert "git push" not in ctx, f"Found 'git push' when require_push=False:\n{ctx}"
        # Must NOT contain clone instructions
        assert "git clone" not in ctx, f"Found 'git clone' when require_push=False:\n{ctx}"
        # result.json example must NOT contain git_branch artifact
        assert '"git_branch"' not in ctx, (
            f"Found git_branch artifact in result.json example when require_push=False:\n{ctx}"
        )
        # Must NOT contain fallback talos/<id> branch name
        assert f"talos/{task.id}" not in ctx, (
            f"Found fallback branch name talos/{task.id} when no branch declared:\n{ctx}"
        )

    def test_push_when_require_push_true(self):
        """decl.git.require_push=True and has git_branch deliverable →
        context.md MUST contain push/clone text and git_branch artifact example."""
        decl = Declaration(
            skills=["talos-code-demo"],
            resources=ResourceSpec(),
            artifacts=[],
            git=GitSpec(branch="talos/feat-001", require_push=True),
            verification=VerificationSpec(),
            deliverables=[
                DeliverableSpec(kind="git_branch", repo="https://example.com/repo.git",
                                branch="talos/feat-001"),
            ],
        )
        task = _make_task()
        ctx = _build_context_md(task, decl)

        # MUST contain push instructions
        assert "推到分支" in ctx or "git push" in ctx, (
            f"Missing push instruction when require_push=True:\n{ctx}"
        )
        # MUST contain clone instructions
        assert "git clone" in ctx, (
            f"Missing 'git clone' when require_push=True:\n{ctx}"
        )
        # result.json example MUST contain git_branch artifact
        assert '"git_branch"' in ctx, (
            f"Missing git_branch artifact in result.json example when require_push=True:\n{ctx}"
        )
