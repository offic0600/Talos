"""Adjudicate: verify worker output against declarations (§6).

The adjudicator is the **sole authority** on task terminal state (I3).
Its inputs are ONLY (I4):
  - GitLab API (pipelines, ls-remote)
  - ``git ls-remote`` on the declared repo/branch
  - Files copied out of the container

It does NOT read container-internal process state as authoritative.

Check pipeline:
  1. ``check_result_file`` — result.json exists, valid JSON, schema=1, status≠failed
  2. ``check_artifacts`` — declared artifact files exist and meet min_bytes
  3. ``check_git_pushed`` — ls-remote confirms branch exists and sha matches
  4. ``check_verification`` — ci: GitLab pipeline; evidence: state.db; none: skip
  5. ``check_deliverables`` — git_branch: ls-remote; platform_attachment: file exists

Verdict分流:
  error (checker_error) > unmet (problems non-empty) > degraded (defects) > pass
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from talos.executor.collect import CollectedBundle
from talos.executor.constants import (
    GITLAB_ADMIN_TOKEN,
    GITLAB_URL,
    PIPELINE_POLL_INTERVAL,
    PIPELINE_APPEAR_WINDOW,
    log_event,
)
from talos.executor.declarations import Declaration


# ── Verdict ──────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    """Result of adjudicating one run."""
    status: str  # pass | degraded | unmet | error
    problems: list[str] = field(default_factory=list)
    defects: list[str] = field(default_factory=list)
    result_status: Optional[str] = None  # from result.json: done|blocked|failed
    summary: str = ""
    artifacts: list[dict] = field(default_factory=list)
    subtasks: list[dict] = field(default_factory=list)
    request_review: bool = False
    comments: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "problems": self.problems,
            "defects": self.defects,
            "result_status": self.result_status,
            "summary": self.summary[:2000],
            "artifacts": self.artifacts,
            "subtasks": self.subtasks,
            "request_review": self.request_review,
            "comments": self.comments,
            "metadata": self.metadata,
        }


# ── Individual checks ────────────────────────────────────────────────────

def check_result_file(decl: Declaration, bundle: CollectedBundle) -> tuple[list[str], list[str]]:
    """Check result.json exists, is valid JSON, schema=1, status≠failed (§6)."""
    problems: list[str] = []
    defects: list[str] = []

    if bundle.result_json is None:
        if bundle.result_path and bundle.result_path.exists():
            # File exists but couldn't parse
            problems.append(f"结果文件不合法: {bundle.result_raw or 'parse error'}")
        else:
            problems.append("结果文件缺失: /task/out/result.json")
        return problems, defects

    data = bundle.result_json
    if data.get("status") == "failed":
        problems.append(f"结果文件 status=failed: {data.get('summary', '')[:200]}")

    return problems, defects


def check_artifacts(decl: Declaration, bundle: CollectedBundle) -> tuple[list[str], list[str]]:
    """Check declared artifacts exist and meet min_bytes (§6)."""
    problems: list[str] = []
    defects: list[str] = []

    for art in decl.artifacts:
        # Artifacts may be in /work/ (copied to bundle.workspace_path) or /task/out/
        # Also handle ${workspace} variable (substituted to /work)
        art_path = art.path.replace("${workspace}", "/work")
        # Try to resolve relative to workspace
        candidates: list[Path] = []
        if art_path.startswith("/work/"):
            rel = art_path[len("/work/"):]
            if bundle.workspace_path:
                candidates.append(bundle.workspace_path / rel)
        elif art_path.startswith("/task/out/"):
            rel = art_path[len("/task/out/"):]
            candidates.append(bundle.tdir / "out" / rel)
        else:
            # Try both locations
            if bundle.workspace_path:
                candidates.append(bundle.workspace_path / art_path.lstrip("/"))
            candidates.append(bundle.tdir / "out" / art_path.lstrip("/"))

        found = False
        for cand in candidates:
            if cand.exists() and cand.is_file():
                size = cand.stat().st_size
                if size < art.min_bytes:
                    problems.append(f"产物过小: {art_path} ({size} bytes < {art.min_bytes})")
                found = True
                break

        if not found:
            problems.append(f"产物缺失: {art_path}")

    return problems, defects


def check_git_pushed(decl: Declaration, bundle: CollectedBundle) -> tuple[list[str], list[str]]:
    """Check git branch exists on remote and sha matches self-report (§6, I4).

    Uses ``git ls-remote`` — the authoritative source, not the worker's
    self-report.
    """
    problems: list[str] = []
    defects: list[str] = []

    if not decl.git.require_push or not decl.git.branch:
        return problems, defects

    # Determine repo URL from result.json artifacts or task
    repo_url = _get_repo_url(decl, bundle)
    if not repo_url:
        # No repo declared — skip git check
        return problems, defects

    branch = decl.git.branch
    try:
        remote_sha = _ls_remote(repo_url, branch)
    except RuntimeError:
        # Repo unreachable → defect
        defects.append(f"仓库不可达: {repo_url}")
        return problems, defects

    if remote_sha is None:
        # Branch doesn't exist (repo is reachable since _ls_remote didn't raise)
        problems.append(f"分支不存在: {branch} on {repo_url}")
        return problems, defects

    # Compare with self-reported sha
    self_sha = _get_self_reported_sha(bundle, branch)
    if self_sha and remote_sha != self_sha:
        problems.append(
            f"sha 不匹配: 自报 {self_sha[:12]}, 远端 {remote_sha[:12]}"
        )

    return problems, defects


def check_verification(decl: Declaration, bundle: CollectedBundle) -> tuple[list[str], list[str]]:
    """Check verification source (§6).

    - ``ci``: GitLab pipelines API, poll to terminal state
    - ``evidence``: state.db from the container
    - ``none``: skip
    """
    problems: list[str] = []
    defects: list[str] = []

    if not decl.verification.required:
        return problems, defects

    source = decl.verification.source

    if source == "none":
        return problems, defects

    if source == "ci":
        return _check_ci(decl, bundle)
    elif source == "evidence":
        return _check_evidence(decl, bundle)
    else:
        # Unknown source — defect
        defects.append(f"未知验证来源: {source}")
        return problems, defects


def check_deliverables(decl: Declaration, bundle: CollectedBundle) -> tuple[list[str], list[str]]:
    """Check declared deliverables (§6).

    - ``git_branch``: ls-remote confirms branch exists + sha matches
    - ``platform_attachment``: file exists (third batch does actual upload)
    """
    problems: list[str] = []
    defects: list[str] = []

    for dl in decl.deliverables:
        if dl.kind == "git_branch":
            try:
                remote_sha = _ls_remote(dl.repo, dl.branch)
            except RuntimeError:
                defects.append(f"交付仓库不可达: {dl.repo}")
                continue
            if remote_sha is None:
                problems.append(f"交付分支不存在: {dl.branch} on {dl.repo}")
            else:
                self_sha = _get_self_reported_sha(bundle, dl.branch)
                if self_sha and remote_sha != self_sha:
                    problems.append(
                        f"交付 sha 不匹配: 自报 {self_sha[:12]}, 远端 {remote_sha[:12]}"
                    )

        elif dl.kind == "platform_attachment":
            # Check file exists in workspace or out
            path = dl.path.replace("${workspace}", "/work")
            candidates: list[Path] = []
            if path.startswith("/work/"):
                rel = path[len("/work/"):]
                if bundle.workspace_path:
                    candidates.append(bundle.workspace_path / rel)
            elif path.startswith("/task/out/"):
                rel = path[len("/task/out/"):]
                candidates.append(bundle.tdir / "out" / rel)
            else:
                if bundle.workspace_path:
                    candidates.append(bundle.workspace_path / path.lstrip("/"))
                candidates.append(bundle.tdir / "out" / path.lstrip("/"))

            found = any(c.exists() and c.is_file() for c in candidates)
            if not found:
                problems.append(f"交付文件不存在: {path}")

    return problems, defects


# ── CI verification ──────────────────────────────────────────────────────

def _check_ci(decl: Declaration, bundle: CollectedBundle) -> tuple[list[str], list[str]]:
    """Poll GitLab pipelines API for the branch/sha (§6).

    Decision flow:
      1. GET /projects/:id/repository/files/.gitlab-ci.yml?ref=<branch>
         - 404 → defect「无流水线定义」, return immediately
         - 200 → proceed to step 2
      2. Wait up to PIPELINE_APPEAR_WINDOW (60s) for a pipeline to appear
         (poll every PIPELINE_POLL_INTERVAL=5s)
         - No pipeline in 60s → defect「流水线未触发」, return
      3. Once pipeline exists, poll until terminal state or timeout_s
    """
    problems: list[str] = []
    defects: list[str] = []

    repo_url = _get_repo_url(decl, bundle)
    if not repo_url:
        defects.append("CI 验证无法确定仓库 URL")
        return problems, defects

    branch = decl.git.branch
    sha = _get_self_reported_sha(bundle, branch)

    project_id = _project_id_from_url(repo_url)
    if not project_id:
        defects.append(f"无法从 URL 解析项目: {repo_url}")
        return problems, defects

    # Step 1: check if .gitlab-ci.yml exists on this branch
    ci_file_ok = _gitlab_ci_file_exists(project_id, branch)
    if ci_file_ok is False:
        defects.append(f"无流水线定义: branch={branch} 无 .gitlab-ci.yml")
        return problems, defects
    if ci_file_ok is None:
        # API error — can't determine, treat as defect
        defects.append(f"GitLab API 不可达: 无法检查 .gitlab-ci.yml")
        return problems, defects

    # Step 2: wait up to PIPELINE_APPEAR_WINDOW for a pipeline to appear
    appear_deadline = time.time() + PIPELINE_APPEAR_WINDOW
    pipeline_found = False
    while time.time() < appear_deadline:
        pipelines = _gitlab_pipelines(project_id, branch, sha)
        if pipelines:
            pipeline_found = True
            break
        time.sleep(PIPELINE_POLL_INTERVAL)

    if not pipeline_found:
        defects.append(
            f"流水线未触发: {PIPELINE_APPEAR_WINDOW}s 内无流水线 branch={branch}"
        )
        return problems, defects

    # Step 3: poll until terminal state or timeout_s
    timeout = decl.verification.timeout_s
    deadline = time.time() + timeout

    while time.time() < deadline:
        pipelines = _gitlab_pipelines(project_id, branch, sha)
        if pipelines is None:
            defects.append("GitLab pipelines API 不可达")
            return problems, defects

        if not pipelines:
            # Pipeline disappeared — treat as defect
            defects.append(f"流水线消失: branch={branch}")
            return problems, defects

        pipe = pipelines[0]
        status = pipe.get("status", "")
        if status in ("success",):
            return problems, defects
        elif status in ("failed", "canceled"):
            problems.append(f"流水线 {status}: #{pipe.get('id')} branch={branch}")
            return problems, defects
        # running / pending / created / etc — keep polling
        time.sleep(PIPELINE_POLL_INTERVAL)

    defects.append(f"流水线超时: {timeout}s 内未出终态 branch={branch}")
    return problems, defects


def _check_evidence(decl: Declaration, bundle: CollectedBundle) -> tuple[list[str], list[str]]:
    """Check evidence state.db from the container (§6, first-batch logic).

    The state.db is copied out and read on the host with HERMES_HOME pointed
    at the task directory. This is explicitly marked as potentially tamperable
    in the design doc — it's the evidence *source*, not an authority.
    """
    problems: list[str] = []
    defects: list[str] = []

    # The state.db should be in the workspace or out directory
    state_db_candidates: list[Path] = []
    if bundle.workspace_path:
        state_db_candidates.append(bundle.workspace_path / "state.db")
    state_db_candidates.append(bundle.tdir / "out" / "state.db")
    state_db_candidates.append(bundle.tdir / "state.db")

    state_db = None
    for cand in state_db_candidates:
        if cand.exists():
            state_db = cand
            break

    if state_db is None:
        defects.append("证据账本 state.db 不可读")
        return problems, defects

    # Try to read verification status from state.db
    try:
        import sqlite3
        conn = sqlite3.connect(str(state_db))
        conn.row_factory = sqlite3.Row
        # Look for a verification table or similar
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()

        if "verification_status" in tables:
            conn = sqlite3.connect(str(state_db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status FROM verification_status ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                vstatus = row["status"]
                if vstatus in ("unverified", "stale", "failed"):
                    problems.append(f"证据状态: {vstatus}")
        elif "verification" in tables:
            conn = sqlite3.connect(str(state_db))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status FROM verification ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row:
                vstatus = row["status"]
                if vstatus in ("unverified", "stale", "failed"):
                    problems.append(f"证据状态: {vstatus}")
        else:
            # No verification table — can't determine
            defects.append("证据账本无验证表")
    except Exception as e:
        defects.append(f"证据账本不可读: {e}")

    return problems, defects


# ── Helpers ──────────────────────────────────────────────────────────────

def _get_repo_url(decl: Declaration, bundle: CollectedBundle) -> str:
    """Extract repo URL from result.json artifacts or declaration deliverables."""
    if bundle.result_json:
        for art in bundle.result_json.get("artifacts", []):
            if isinstance(art, dict) and art.get("repo"):
                return art["repo"]
    for dl in decl.deliverables:
        if dl.kind == "git_branch" and dl.repo:
            return dl.repo
    return ""


def _get_self_reported_sha(bundle: CollectedBundle, branch: str) -> Optional[str]:
    """Get the sha the worker self-reported in result.json for *branch*."""
    if not bundle.result_json:
        return None
    for art in bundle.result_json.get("artifacts", []):
        if isinstance(art, dict):
            if art.get("branch") == branch or art.get("kind") == "git_branch":
                return art.get("sha")
    return None


def _ls_remote(repo_url: str, branch: str) -> Optional[str]:
    """``git ls-remote <repo> refs/heads/<branch>`` → sha or None.

    Returns the sha if the branch exists, ``None`` if the branch doesn't
    exist, and raises ``RuntimeError`` if the repo is unreachable.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo_url, f"refs/heads/{branch}"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ls-remote timeout: {repo_url}")

    if result.returncode != 0:
        err = result.stderr.strip()
        if "Could not read from remote" in err or "does not appear to be a git" in err or "Permission denied" in err:
            raise RuntimeError(f"repo unreachable: {repo_url}: {err}")
        # Non-zero exit with no specific error → branch likely doesn't exist
        return None

    output = result.stdout.strip()
    if not output:
        # Branch doesn't exist
        return None

    # Output format: "<sha>\trefs/heads/<branch>"
    parts = output.split("\t")
    if len(parts) >= 1 and parts[0]:
        return parts[0].strip()
    return None


def _repo_reachable(repo_url: str) -> bool:
    """Check if the repo URL is reachable (for distinguishing problem vs defect)."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo_url, "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, Exception):
        return False


def _project_id_from_url(repo_url: str) -> Optional[str]:
    """Extract URL-encoded project path from a GitLab repo URL."""
    clean = repo_url.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    idx = clean.find("//")
    if idx >= 0:
        rest = clean[idx + 2:]
        slash = rest.find("/")
        if slash >= 0:
            project_path = rest[slash + 1:]
        else:
            return None
    else:
        project_path = clean
    return project_path.replace("/", "%2F")


def _gitlab_pipelines(project_id: str, branch: str, sha: Optional[str] = None) -> Optional[list]:
    """Query GitLab API for pipelines on a branch/sha."""
    if not GITLAB_ADMIN_TOKEN:
        return None
    path = f"/projects/{project_id}/pipelines?ref={branch}&per_page=5"
    if sha:
        path += f"&sha={sha}"
    url = f"{GITLAB_URL}/api/v4{path}"
    req = Request(url)
    req.add_header("PRIVATE-TOKEN", GITLAB_ADMIN_TOKEN)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError:
        return None
    except Exception:
        return None


def _gitlab_ci_file_exists(project_id: str, branch: str) -> Optional[bool]:
    """Check if .gitlab-ci.yml exists on the given branch.

    Returns True if the file exists, False if 404, None on API error.
    """
    if not GITLAB_ADMIN_TOKEN:
        return None
    # URL-encode the file path: .gitlab-ci.yml → .gitlab-ci.yml (already safe)
    import urllib.parse
    file_path = urllib.parse.quote(".gitlab-ci.yml", safe="")
    url = (
        f"{GITLAB_URL}/api/v4/projects/{project_id}"
        f"/repository/files/{file_path}?ref={urllib.parse.quote(branch, safe='')}"
    )
    req = Request(url)
    req.add_header("PRIVATE-TOKEN", GITLAB_ADMIN_TOKEN)
    try:
        with urlopen(req, timeout=15) as resp:
            return True  # 200 — file exists
    except HTTPError as e:
        if e.code == 404:
            return False
        return None  # other HTTP error
    except Exception:
        return None


# ── Main adjudication function ───────────────────────────────────────────

def adjudicate(
    task_id: str,
    run_id: int,
    bundle: CollectedBundle,
    decl: Declaration,
) -> Verdict:
    """Run all checks and produce a Verdict (§6).

    Check order: result_file → artifacts → git_pushed → verification → deliverables.
    Verdict: error (checker_error) > unmet (problems) > degraded (defects) > pass.
    """
    t0 = time.time()
    problems: list[str] = []
    defects: list[str] = []
    checker_error: Optional[str] = None
    check_name = ""

    checks = [
        ("result_file", check_result_file),
        ("artifacts", check_artifacts),
        ("git_pushed", check_git_pushed),
        ("verification", check_verification),
        ("deliverables", check_deliverables),
    ]

    for idx, (check_name, check_fn) in enumerate(checks):
        try:
            p, d = check_fn(decl, bundle)
            problems.extend(p)
            defects.extend(d)
        except Exception as e:
            checker_error = f"{check_name}: {e}"
            checks_run = [c[0] for c in checks[:idx + 1]]
            break
    else:
        checks_run = [c[0] for c in checks]

    # Extract result.json fields for the verdict
    result_status = None
    summary = ""
    artifacts = []
    subtasks = []
    request_review = False
    comments = []
    if bundle.result_json:
        data = bundle.result_json
        result_status = data.get("status")
        summary = data.get("summary", "")[:2000]
        artifacts = data.get("artifacts", [])
        subtasks = data.get("subtasks", [])
        request_review = bool(data.get("request_review", False))
        comments = data.get("comments", [])

    # Determine verdict status
    if checker_error:
        status = "error"
        defects.append(f"校验器故障: {checker_error}")
    elif result_status == "blocked":
        # result.json says blocked → same path as unmet (§5.4, §7)
        # _record_task_failure(error=summary), NOT block_task
        status = "unmet"
        if not problems:
            problems.append(f"worker 报告能力不足 (status=blocked): {summary[:300]}")
    elif problems:
        status = "unmet"
    elif defects:
        status = "degraded"
    else:
        status = "pass"

    verdict = Verdict(
        status=status,
        problems=problems,
        defects=defects,
        result_status=result_status,
        summary=summary,
        artifacts=artifacts,
        subtasks=subtasks,
        request_review=request_review,
        comments=comments,
        metadata={
            "checker_error": checker_error,
            "exit_code": bundle.exit_code,
            "checks_run": checks_run,
        },
    )

    log_event("adjudicated", task_id=task_id, run_id=run_id,
              duration_ms=(time.time() - t0) * 1000,
              extra={"verdict": status, "problems": len(problems), "defects": len(defects)})

    return verdict
