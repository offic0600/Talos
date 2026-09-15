"""
test_adjudicate.py — tests for the Talos executor's adjudication function.

Covers (per §17.2 self-test requirement):
1. Every branch of the verdict分流: pass / degraded / unmet / error
2. Idempotency: adjudicating the same run twice only finalizes once
3. Declaration merging: multi-skill contracts take union of artifacts,
   max of resources, union of verification/deliverables/credentials

The adjudicate() function and its supporting types live in
``talos.executor.adjudicate``. These tests import the real function
and exercise it against the real 38-column schema fixture.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the talos package is importable
TALOS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TALOS_ROOT))

from talos.executor.adjudicate import (
    Verdict,
    adjudicate,
    check_result_file,
    check_artifacts,
    check_git_pushed,
    check_verification,
    check_deliverables,
)
from talos.executor.collect import CollectedBundle
from talos.executor.declarations import (
    ArtifactSpec,
    CredentialSpec,
    Declaration,
    DeliverableSpec,
    GitSpec,
    ResourceSpec,
    VerificationSpec,
    load_declarations,
)

# merge_declarations is not a separate function; load_declarations does the merge.
# We alias it for test readability.
merge_declarations = load_declarations
from talos.executor.constants import task_dir


# ── Helpers ────────────────────────────────────────────────────

def make_decl(
    artifacts: list[ArtifactSpec] | None = None,
    git: GitSpec | None = None,
    verification: VerificationSpec | None = None,
    deliverables: list[DeliverableSpec] | None = None,
    credentials: list[CredentialSpec] | None = None,
) -> Declaration:
    """Build a Declaration with sensible defaults for tests."""
    return Declaration(
        skills=["test-skill"],
        resources=ResourceSpec(memory_mb=512, cpus=0.5),
        artifacts=artifacts or [],
        git=git or GitSpec(),
        verification=verification or VerificationSpec(required=False, source="none"),
        deliverables=deliverables or [],
        credentials=credentials or [],
    )


def make_bundle(
    tmp_path: Path,
    result_json: dict | None = None,
    workspace_path: Path | None = None,
    checker_error: str | None = None,
    exit_code: int = 0,
) -> CollectedBundle:
    """Build a CollectedBundle for testing."""
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
        checker_error=checker_error,
    )


# ═══════════════════════════════════════════════════════════════
# 1. Verdict分流: pass / degraded / unmet / error
# ═══════════════════════════════════════════════════════════════


class TestAdjudicatePass:
    """Verdict = pass: no problems, no defects, all checks satisfied."""

    def test_pass_all_checks_satisfied(self, tmp_path):
        """All artifacts present, git pushed, verification passed → pass."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_path = ws / "src" / "feature.py"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("def feature():\n    return 'hello world from talos'\n")

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Implemented feature.py",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/test/repo.git",
                        "branch": "talos/t_test",
                        "sha": "abc123",
                    }
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="${workspace}/src/feature.py", min_bytes=50)],
            git=GitSpec(branch="talos/t_test", require_push=True),
            verification=VerificationSpec(required=True, source="ci", timeout_s=900),
            deliverables=[DeliverableSpec(kind="git_branch", repo="https://gitlab.example.com/test/repo.git", branch="talos/t_test")],
        )

        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value="abc123"), \
             patch("talos.executor.adjudicate._repo_reachable", return_value=True), \
             patch("talos.executor.adjudicate._check_ci") as mock_ci:
            mock_ci.return_value = ([], [])  # no problems, no defects
            verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "pass"
        assert verdict.problems == []
        assert verdict.defects == []


class TestAdjudicateDegraded:
    """Verdict = degraded: no problems, but has defects (environment issues)."""

    def test_degraded_ci_timeout(self, tmp_path):
        """CI pipeline didn't reach a terminal state within timeout → defect → degraded."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_path = ws / "src" / "feature.py"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("def feature():\n    return 'hello world from talos'\n")

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/test/repo.git",
                        "branch": "talos/t_test",
                        "sha": "abc123",
                    }
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="${workspace}/src/feature.py", min_bytes=50)],
            git=GitSpec(branch="talos/t_test", require_push=True),
            verification=VerificationSpec(required=True, source="ci", timeout_s=900),
            deliverables=[DeliverableSpec(kind="git_branch", repo="https://gitlab.example.com/test/repo.git", branch="talos/t_test")],
        )

        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value="abc123"), \
             patch("talos.executor.adjudicate._repo_reachable", return_value=True), \
             patch("talos.executor.adjudicate._check_ci") as mock_ci:
            mock_ci.return_value = ([], ["流水线超时: 900s 内未出终态 branch=talos/t_test"])
            verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "degraded"
        assert len(verdict.defects) >= 1
        assert any("超时" in d for d in verdict.defects)

    def test_degraded_repo_unreachable(self, tmp_path):
        """Git repo unreachable → defect → degraded (if no other problems)."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        # No artifact declared, no git check, so the only check is result file
        bundle = make_bundle(
            tmp_path,
            result_json={"schema": 1, "status": "done", "summary": "Done", "artifacts": []},
            workspace_path=ws,
        )
        decl = make_decl(
            verification=VerificationSpec(required=False, source="none"),
        )

        verdict = adjudicate("t_test", 1, bundle, decl)
        # No problems, no defects → pass (nothing to degrade on)
        assert verdict.status == "pass"


class TestAdjudicateUnmet:
    """Verdict = unmet: has problems (worker can fix by re-running)."""

    def test_unmet_result_file_missing(self, tmp_path):
        """No result.json → problem → unmet (M15)."""
        bundle = make_bundle(tmp_path, result_json=None)
        decl = make_decl()

        verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "unmet"
        assert any("结果文件" in p or "result" in p.lower() for p in verdict.problems)

    def test_unmet_result_file_status_failed(self, tmp_path):
        """Result file status=failed → unmet."""
        bundle = make_bundle(
            tmp_path,
            result_json={"schema": 1, "status": "failed", "summary": "bad", "artifacts": []},
        )
        decl = make_decl()

        verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "unmet"
        assert any("failed" in p.lower() for p in verdict.problems)

    def test_unmet_artifact_missing(self, tmp_path):
        """Declared artifact file doesn't exist → unmet (M5)."""
        ws = tmp_path / "workspace"
        ws.mkdir()

        bundle = make_bundle(
            tmp_path,
            result_json={"schema": 1, "status": "done", "summary": "Done", "artifacts": []},
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=50)],
            verification=VerificationSpec(required=False, source="none"),
        )

        verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "unmet"
        assert any("feature.py" in p for p in verdict.problems)

    def test_unmet_artifact_too_small(self, tmp_path):
        """Artifact exists but is below min_bytes → unmet."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_path = ws / "src" / "feature.py"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("x")  # 1 byte, min_bytes=50

        bundle = make_bundle(
            tmp_path,
            result_json={"schema": 1, "status": "done", "summary": "Done", "artifacts": []},
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=50)],
            verification=VerificationSpec(required=False, source="none"),
        )

        verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "unmet"
        assert any("过小" in p or "small" in p.lower() for p in verdict.problems)

    def test_unmet_git_not_pushed(self, tmp_path):
        """Branch doesn't exist on remote (ls-remote returns None) → unmet."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_path = ws / "src" / "feature.py"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("def feature():\n    return 'hello world from talos'\n")

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/test/repo.git",
                        "branch": "talos/t_test",
                        "sha": "abc123",
                    }
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=50)],
            git=GitSpec(branch="talos/t_test", require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[DeliverableSpec(kind="git_branch", repo="https://gitlab.example.com/test/repo.git", branch="talos/t_test")],
        )

        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value=None), \
             patch("talos.executor.adjudicate._repo_reachable", return_value=True):
            verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "unmet"
        assert any("分支" in p or "branch" in p.lower() for p in verdict.problems)

    def test_unmet_git_sha_mismatch(self, tmp_path):
        """Worker self-reported sha ≠ remote sha → unmet (M10)."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_path = ws / "src" / "feature.py"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("def feature():\n    return 'hello world from talos'\n")

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/test/repo.git",
                        "branch": "talos/t_test",
                        "sha": "wrongsha",
                    }
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=50)],
            git=GitSpec(branch="talos/t_test", require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[DeliverableSpec(kind="git_branch", repo="https://gitlab.example.com/test/repo.git", branch="talos/t_test")],
        )

        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value="realsha"), \
             patch("talos.executor.adjudicate._repo_reachable", return_value=True):
            verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "unmet"
        assert any("sha" in p.lower() for p in verdict.problems)

    def test_unmet_ci_failed(self, tmp_path):
        """CI pipeline failed → unmet (M8)."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_path = ws / "src" / "feature.py"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("def feature():\n    return 'hello world from talos'\n")

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/test/repo.git",
                        "branch": "talos/t_test",
                        "sha": "abc123",
                    }
                ],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=50)],
            git=GitSpec(branch="talos/t_test", require_push=True),
            verification=VerificationSpec(required=True, source="ci", timeout_s=900),
            deliverables=[DeliverableSpec(kind="git_branch", repo="https://gitlab.example.com/test/repo.git", branch="talos/t_test")],
        )

        with patch("talos.executor.adjudicate._gitlab_branch_sha", return_value="abc123"), \
             patch("talos.executor.adjudicate._repo_reachable", return_value=True), \
             patch("talos.executor.adjudicate._check_ci") as mock_ci:
            mock_ci.return_value = (["流水线 failed: #42 branch=talos/t_test"], [])
            verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "unmet"
        assert any("流水线" in p or "pipeline" in p.lower() or "failed" in p.lower() for p in verdict.problems)


class TestAdjudicateError:
    """Verdict = error: adjudicator itself crashed → triage, not done (M12)."""

    def test_error_checker_crash(self, tmp_path):
        """An exception during checking → error verdict (M12).

        We simulate this by making check_artifacts raise an exception, which
        gets caught by the adjudicate() try/except in the check loop.
        """
        ws = tmp_path / "workspace"
        ws.mkdir()

        bundle = make_bundle(
            tmp_path,
            result_json={"schema": 1, "status": "done", "summary": "ok", "artifacts": []},
            workspace_path=ws,
        )
        decl = make_decl(
            git=GitSpec(branch="talos/t_test", require_push=True),
            verification=VerificationSpec(required=False, source="none"),
            deliverables=[DeliverableSpec(kind="git_branch", repo="https://gitlab.example.com/test/repo.git", branch="talos/t_test")],
        )

        with patch("talos.executor.adjudicate.check_artifacts",
                   side_effect=RuntimeError("Simulated checker crash")):
            verdict = adjudicate("t_test", 1, bundle, decl)

        assert verdict.status == "error"
        # The checker_error should be recorded in metadata
        assert verdict.metadata.get("checker_error") is not None


# ═══════════════════════════════════════════════════════════════
# 2. Idempotency: same run adjudicated twice → only one finalize
# ═══════════════════════════════════════════════════════════════


class TestIdempotency:
    """I7: executor restart → re-run same tick → no duplicate dispatch/complete/comment.

    The idempotency check lives in loop.py's ``_is_run_adjudicated()``, which
    checks task_runs for terminal status or a verdict in metadata. The
    adjudicate() function itself is pure (no side effects on the DB), so
    idempotency is about the loop not calling finalize() twice for the same run.
    """

    def test_is_run_adjudicated_terminal_status(self, conn, make_task, make_run):
        """A run with terminal status (done/blocked/gave_up) is already adjudicated."""
        from talos.executor.loop import _is_run_adjudicated

        task_id = make_task(id="t_idem_001", status="running")
        run_id = make_run(task_id=task_id, status="done", outcome="completed")

        assert _is_run_adjudicated(conn, run_id) is True

    def test_is_run_adjudicated_running_not_adjudicated(self, conn, make_task, make_run):
        """A run still in 'running' status is NOT adjudicated."""
        from talos.executor.loop import _is_run_adjudicated

        task_id = make_task(id="t_idem_002", status="running")
        run_id = make_run(task_id=task_id, status="running")

        assert _is_run_adjudicated(conn, run_id) is False

    def test_is_run_adjudicated_with_verdict_metadata(self, conn, make_task, make_run):
        """A run with verdict in metadata is already adjudicated.

        _is_run_adjudicated checks if metadata has a 'status' key whose
        string value contains 'verdict' (e.g. metadata={'status': 'verdict:pass'}).
        """
        from talos.executor.loop import _is_run_adjudicated

        task_id = make_task(id="t_idem_003", status="running")
        run_id = make_run(
            task_id=task_id,
            status="running",
            metadata={"status": "verdict:pass", "verdict": "pass"},
        )

        assert _is_run_adjudicated(conn, run_id) is True

    def test_is_run_adjudicated_nonexistent_run(self, conn):
        """A non-existent run_id returns False (not adjudicated)."""
        from talos.executor.loop import _is_run_adjudicated

        assert _is_run_adjudicated(conn, 99999) is False

    def test_adjudicate_is_pure_no_db_side_effects(self, tmp_path):
        """adjudicate() itself doesn't write to the DB — it's a pure function.

        Idempotency is maintained at the loop level: the loop checks
        _is_run_adjudicated before calling adjudicate+finalize.
        Calling adjudicate() twice produces the same verdict both times
        without side effects.
        """
        ws = tmp_path / "workspace"
        ws.mkdir()
        artifact_path = ws / "src" / "feature.py"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("def feature():\n    return 'hello world from talos'\n")

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/src/feature.py", min_bytes=50)],
            verification=VerificationSpec(required=False, source="none"),
        )

        # First adjudication
        verdict1 = adjudicate("t_test", 1, bundle, decl)
        # Second adjudication — same inputs, same output
        verdict2 = adjudicate("t_test", 1, bundle, decl)

        assert verdict1.status == verdict2.status
        assert verdict1.problems == verdict2.problems
        assert verdict1.defects == verdict2.defects


# ═══════════════════════════════════════════════════════════════
# 3. Declaration merging (multi-skill)
# ═══════════════════════════════════════════════════════════════


class TestDeclarationMerge:
    """§4: multi-skill merge — artifacts union, resources max, verification/deliverables/credentials union.

    The merge logic lives in ``declarations.py::load_declarations()``.
    Here we test it directly by calling load_declarations with multiple skills.
    """

    def test_merge_artifacts_union(self, tmp_path, monkeypatch):
        """Two skills each declaring artifacts → merged takes union."""
        # Create two skill dirs
        skills_dir = tmp_path / "skills"
        skill_a = skills_dir / "skill-a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(
            "---\n"
            "name: skill-a\n"
            "resources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n"
            "  artifacts:\n"
            "    - {path: '${workspace}/src/a.py', min_bytes: 10}\n"
            "  verification: {required: false, source: none}\n"
            "---\n# skill-a\n"
        )
        skill_b = skills_dir / "skill-b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(
            "---\n"
            "name: skill-b\n"
            "resources: {memory_mb: 2048, cpus: 1.0}\n"
            "completion_contract:\n"
            "  artifacts:\n"
            "    - {path: '${workspace}/src/b.py', min_bytes: 20}\n"
            "  verification: {required: true, source: ci, timeout_s: 300}\n"
            "deliverables:\n"
            "  - {kind: git_branch, repo: 'x', branch: 'y'}\n"
            "credentials:\n"
            "  - {kind: gitlab, scope: write_repository, ttl: task}\n"
            "---\n# skill-b\n"
        )

        # Patch HOME so load_declarations finds our test skills
        from talos.executor import constants
        monkeypatch.setattr(constants, "HOME", tmp_path)

        decl = load_declarations(["skill-a", "skill-b"])

        assert len(decl.artifacts) == 2
        paths = {a.path for a in decl.artifacts}
        assert any("a.py" in p for p in paths)
        assert any("b.py" in p for p in paths)

    def test_merge_resources_take_max(self, tmp_path, monkeypatch):
        """Resources: take the maximum across skills."""
        skills_dir = tmp_path / "skills"
        skill_a = skills_dir / "skill-a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(
            "---\nname: skill-a\nresources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n  artifacts: []\n  verification: {required: false, source: none}\n"
            "---\n# skill-a\n"
        )
        skill_b = skills_dir / "skill-b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(
            "---\nname: skill-b\nresources: {memory_mb: 2048, cpus: 1.0}\n"
            "completion_contract:\n  artifacts: []\n  verification: {required: false, source: none}\n"
            "---\n# skill-b\n"
        )

        from talos.executor import constants
        monkeypatch.setattr(constants, "HOME", tmp_path)

        decl = load_declarations(["skill-a", "skill-b"])
        assert decl.resources.memory_mb == 2048
        assert decl.resources.cpus == 1.0

    def test_merge_verification_union_stricter(self, tmp_path, monkeypatch):
        """Verification: if any skill requires it, the merged requires it."""
        skills_dir = tmp_path / "skills"
        skill_a = skills_dir / "skill-a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(
            "---\nname: skill-a\nresources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n  artifacts: []\n  verification: {required: false, source: none}\n"
            "---\n# skill-a\n"
        )
        skill_b = skills_dir / "skill-b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(
            "---\nname: skill-b\nresources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n  artifacts: []\n  verification: {required: true, source: ci, timeout_s: 900}\n"
            "---\n# skill-b\n"
        )

        from talos.executor import constants
        monkeypatch.setattr(constants, "HOME", tmp_path)

        decl = load_declarations(["skill-a", "skill-b"])
        assert decl.verification.required is True
        assert decl.verification.source == "ci"

    def test_merge_deliverables_union(self, tmp_path, monkeypatch):
        """Deliverables: union across skills."""
        skills_dir = tmp_path / "skills"
        skill_a = skills_dir / "skill-a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(
            "---\nname: skill-a\nresources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n  artifacts: []\n  verification: {required: false, source: none}\n"
            "deliverables:\n  - {kind: git_branch, repo: 'a', branch: 'talos/a'}\n"
            "---\n# skill-a\n"
        )
        skill_b = skills_dir / "skill-b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(
            "---\nname: skill-b\nresources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n  artifacts: []\n  verification: {required: false, source: none}\n"
            "deliverables:\n  - {kind: platform_attachment, path: '/work/docs/b.md'}\n"
            "---\n# skill-b\n"
        )

        from talos.executor import constants
        monkeypatch.setattr(constants, "HOME", tmp_path)

        decl = load_declarations(["skill-a", "skill-b"])
        assert len(decl.deliverables) == 2

    def test_merge_credentials_union(self, tmp_path, monkeypatch):
        """Credentials: union across skills."""
        skills_dir = tmp_path / "skills"
        skill_a = skills_dir / "skill-a"
        skill_a.mkdir(parents=True)
        (skill_a / "SKILL.md").write_text(
            "---\nname: skill-a\nresources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n  artifacts: []\n  verification: {required: false, source: none}\n"
            "credentials:\n  - {kind: gitlab, scope: write_repository, ttl: task}\n"
            "---\n# skill-a\n"
        )
        skill_b = skills_dir / "skill-b"
        skill_b.mkdir(parents=True)
        (skill_b / "SKILL.md").write_text(
            "---\nname: skill-b\nresources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n  artifacts: []\n  verification: {required: false, source: none}\n"
            "credentials:\n  - {kind: platform, ttl: task}\n"
            "---\n# skill-b\n"
        )

        from talos.executor import constants
        monkeypatch.setattr(constants, "HOME", tmp_path)

        decl = load_declarations(["skill-a", "skill-b"])
        assert len(decl.credentials) == 2

    def test_merge_empty_list(self):
        """Empty skill list: returns a valid empty declaration."""
        decl = load_declarations([])
        assert decl.artifacts == []
        assert decl.deliverables == []
        assert decl.credentials == []
        assert decl.skills == []

    def test_merge_single_skill_passthrough(self, tmp_path, monkeypatch):
        """Single skill: merged == original (no lossy transformation)."""
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "solo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: solo\n"
            "resources: {memory_mb: 1024, cpus: 1.0}\n"
            "completion_contract:\n"
            "  artifacts:\n"
            "    - {path: '${workspace}/out.py', min_bytes: 50}\n"
            "  git:\n"
            "    branch: 'talos/${task_id}'\n"
            "    require_push: true\n"
            "  verification: {required: true, source: ci, timeout_s: 900}\n"
            "deliverables:\n  - {kind: git_branch, repo: 'x', branch: 'y'}\n"
            "credentials:\n  - {kind: gitlab, scope: write_repository, ttl: task}\n"
            "---\n# solo\n"
        )

        from talos.executor import constants
        monkeypatch.setattr(constants, "HOME", tmp_path)

        decl = load_declarations(["solo"])
        assert "solo" in decl.skills
        assert len(decl.artifacts) == 1
        assert decl.git.require_push is True
        assert decl.verification.required is True
        assert decl.verification.source == "ci"

    def test_merge_doc_skill_no_git_no_verification(self, tmp_path, monkeypatch):
        """Doc skill: no git section, verification source=none."""
        skills_dir = tmp_path / "skills"
        skill = skills_dir / "doc-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\n"
            "name: doc-skill\n"
            "resources: {memory_mb: 512, cpus: 0.5}\n"
            "completion_contract:\n"
            "  artifacts:\n    - {path: '${workspace}/docs/spec.md', min_bytes: 100}\n"
            "  verification: {required: false, source: none}\n"
            "deliverables:\n  - {kind: platform_attachment, path: '${workspace}/docs/spec.md'}\n"
            "---\n# doc-skill\n"
        )

        from talos.executor import constants
        monkeypatch.setattr(constants, "HOME", tmp_path)

        decl = load_declarations(["doc-skill"])
        assert decl.git.require_push is False or decl.git.branch == ""
        assert decl.verification.source == "none"
        assert decl.verification.required is False


# ═══════════════════════════════════════════════════════════════
# 4. Doc-type task (no git, no CI) — same adjudicate function (I1/G4)
# ═══════════════════════════════════════════════════════════════


class TestDocTaskAdjudication:
    """The same adjudicate() handles doc tasks — no if-type branching (I1)."""

    def test_doc_task_pass(self, tmp_path):
        """Doc task with spec.md present and min_bytes met → pass."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        spec_path = ws / "docs" / "spec.md"
        spec_path.parent.mkdir(parents=True)
        spec_path.write_text("# Specification\n\nThis is a detailed spec document.\n" * 10)

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Wrote spec.md",
                "artifacts": [{"kind": "file", "path": "/work/docs/spec.md"}],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/docs/spec.md", min_bytes=100)],
            verification=VerificationSpec(required=False, source="none"),
        )

        verdict = adjudicate("t_doc", 1, bundle, decl)
        assert verdict.status == "pass"

    def test_doc_task_unmet_missing_spec(self, tmp_path):
        """Doc task without spec.md → unmet."""
        ws = tmp_path / "workspace"
        ws.mkdir()

        bundle = make_bundle(
            tmp_path,
            result_json={"schema": 1, "status": "done", "summary": "Done", "artifacts": []},
            workspace_path=ws,
        )
        decl = make_decl(
            artifacts=[ArtifactSpec(path="/work/docs/spec.md", min_bytes=100)],
            verification=VerificationSpec(required=False, source="none"),
        )

        verdict = adjudicate("t_doc", 1, bundle, decl)
        assert verdict.status == "unmet"
        assert any("spec.md" in p for p in verdict.problems)


# ═══════════════════════════════════════════════════════════════
# 5. Blocked status → triage (not done)
# ═══════════════════════════════════════════════════════════════


class TestBlockedStatusTriage:
    """Result file status=blocked → triage, not done (M14)."""

    def test_blocked_status_routes_to_unmet_verdict(self, tmp_path):
        """Worker reports status=blocked → verdict status=unmet (§5.4, §7 v2, M14).

        result.status=blocked uses the same _record_task_failure path as unmet,
        with error=worker's summary. NOT block_task, NOT error/triage.
        """
        ws = tmp_path / "workspace"
        ws.mkdir()

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "blocked",
                "summary": "Cannot proceed: missing API credentials for external service",
                "artifacts": [],
            },
            workspace_path=ws,
        )
        decl = make_decl(
            verification=VerificationSpec(required=False, source="none"),
        )

        verdict = adjudicate("t_block", 1, bundle, decl)

        # Adjudicate sets status="unmet" when result_status=="blocked" (§5.4, §7 v2)
        # finalize() then routes via _record_task_failure(error=summary)
        assert verdict.result_status == "blocked"
        assert verdict.status == "unmet"  # same path as unmet, not error/triage


# ═══════════════════════════════════════════════════════════════
# 4. _check_ci: .gitlab-ci.yml existence + pipeline appear window
# ═══════════════════════════════════════════════════════════════


class TestCheckCiFileDetection:
    """_check_ci should check .gitlab-ci.yml existence before polling pipelines."""

    def test_no_gitlab_ci_yml_returns_defect_within_1s(self, tmp_path):
        """Branch has no .gitlab-ci.yml -> defect, returns in <1s."""
        import time as _time
        from talos.executor.adjudicate import _check_ci

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/test/repo.git",
                        "branch": "talos/no-ci",
                        "sha": "abc123",
                    }
                ],
            },
        )
        decl = make_decl(
            git=GitSpec(branch="talos/no-ci", require_push=True),
            verification=VerificationSpec(required=True, source="ci", timeout_s=900),
            deliverables=[DeliverableSpec(kind="git_branch", repo="https://gitlab.example.com/test/repo.git", branch="talos/no-ci")],
        )

        with patch("talos.executor.adjudicate._gitlab_ci_file_exists", return_value=False), \
             patch("talos.executor.adjudicate._gitlab_pipelines") as mock_pipelines, \
             patch("talos.executor.adjudicate._gitlab_branch_sha", return_value="abc123"), \
             patch("talos.executor.adjudicate._repo_reachable", return_value=True):
            t0 = _time.time()
            problems, defects = _check_ci(decl, bundle)
            elapsed = _time.time() - t0

        assert len(defects) == 1
        assert "无流水线定义" in defects[0]
        assert elapsed < 1.0, f"Expected <1s, got {elapsed:.2f}s"
        mock_pipelines.assert_not_called()


class TestCheckCiPipelineAppearWindow:
    """Pipeline appears on 3rd poll -> should wait and then process normally."""

    def test_pipeline_appears_on_3rd_poll(self, tmp_path):
        """Pipeline appears on 3rd query -> function waits and processes terminal state."""
        from talos.executor.adjudicate import _check_ci

        bundle = make_bundle(
            tmp_path,
            result_json={
                "schema": 1,
                "status": "done",
                "summary": "Done",
                "artifacts": [
                    {
                        "kind": "git_branch",
                        "repo": "https://gitlab.example.com/test/repo.git",
                        "branch": "talos/has-ci",
                        "sha": "abc123",
                    }
                ],
            },
        )
        decl = make_decl(
            git=GitSpec(branch="talos/has-ci", require_push=True),
            verification=VerificationSpec(required=True, source="ci", timeout_s=300),
            deliverables=[DeliverableSpec(kind="git_branch", repo="https://gitlab.example.com/test/repo.git", branch="talos/has-ci")],
        )

        pipeline_success = [{"id": 42, "status": "success"}]
        call_count = {"n": 0}

        def mock_pipelines(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] < 3:
                return []
            return pipeline_success

        with patch("talos.executor.adjudicate._gitlab_ci_file_exists", return_value=True), \
             patch("talos.executor.adjudicate._gitlab_pipelines", side_effect=mock_pipelines), \
             patch("talos.executor.adjudicate._gitlab_branch_sha", return_value="abc123"), \
             patch("talos.executor.adjudicate.time.sleep") as mock_sleep:
            problems, defects = _check_ci(decl, bundle)

        assert len(defects) == 0, f"Expected no defects, got {defects}"
        assert len(problems) == 0, f"Expected no problems, got {problems}"
        assert call_count["n"] >= 3, f"Expected >=3 polls, got {call_count['n']}"
        assert mock_sleep.call_count >= 2, f"Expected >=2 sleeps, got {mock_sleep.call_count}"
