"""Spawn: prepare context, mint credentials, launch worker container (§5).

The spawn function is called by the kernel's ``dispatch_once`` as a callback.
It must return an ``Optional[int]`` — the sentinel PID (I4/Q1).

Container contract (§5):
  - Mounts: plugins (ro), skills (ro), config.yaml (ro), context.md (ro),
    out/ (rw), creds/git-credentials (ro), creds/mcp-tokens (ro).
  - NO kanban directory mounted (I2/I9).
  - Env: TALOS_TASK_ID, TALOS_RUN_ID, HERMES_TASK_WORKSPACE, HERMES_TENANT,
    GIT_CONFIG_* for git auth. NO HERMES_KANBAN_* (§5.2).
  - Entry: ``hermes -p worker --cli --accept-hooks chat -q "$(cat /task/context.md)"``
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from talos.executor.constants import (
    CONTAINER_HH,
    HOME,
    WORKER_IMAGE,
    container_name,
    log_event,
    task_dir,
)
from talos.executor.credentials import mint_credentials
from talos.executor.declarations import Declaration, _extract_repo, load_declarations
from talos.executor.sentinel import start_sentinel


def _build_context_md(conn: Any, task: Any, decl: Declaration) -> str:
    """Build the context.md file (§5.3).

    Order: ① ``hermes kanban context`` output ② declaration summary
    ③ closing requirements (fixed text).
    """
    parts: list[str] = []

    # ① Kanban context (from the kernel's build_worker_context)
    try:
        from hermes_cli.kanban_db import build_worker_context
        parts.append(build_worker_context(conn, task.id))
    except Exception:
        # Fallback: minimal header
        parts.append(f"# Kanban task {task.id}: {task.title}")
        if task.body:
            parts.append(f"\n## Body\n{task.body}")

    # ② Declaration summary
    parts.append("\n---\n## Execution Unit Declaration\n")
    if decl.artifacts:
        parts.append("**Expected artifacts:**")
        for art in decl.artifacts:
            parts.append(f"  - `{art.path}` (min {art.min_bytes} bytes)")
    if decl.git.branch:
        parts.append(f"\n**Git branch:** `{decl.git.branch}` (push required: {decl.git.require_push})")
    if decl.verification.required:
        parts.append(f"\n**Verification:** source={decl.verification.source}, timeout={decl.verification.timeout_s}s")
    if decl.deliverables:
        parts.append("\n**Deliverables:**")
        for dl in decl.deliverables:
            if dl.kind == "git_branch":
                parts.append(f"  - git_branch: `{dl.repo}` → `{dl.branch}`")
            elif dl.kind == "platform_attachment":
                parts.append(f"  - platform_attachment: `{dl.path}`")

    # ③ Closing requirements (fixed text, §5.3)
    parts.append("\n---\n## Completion Requirements\n")
    parts.append(
        "完成后必须做两件事："
        "(1) 把本任务的全部改动提交并推到分支 "
        f"`{decl.git.branch or 'talos/' + task.id}`；"
        "(2) 在 `/task/out/result.json` 写入结果（格式见下）。"
        "不写结果文件视为失败。不要尝试操作看板，你没有看板工具。"
    )
    parts.append(
        "\n```json\n"
        '{ "schema": 1, "status": "done|blocked|failed",\n'
        '  "summary": "≤ 2000 字",\n'
        '  "artifacts": [{"kind":"git_branch","repo":"…","branch":"…","sha":"…"},'
        '{"kind":"file","path":"…"}],\n'
        '  "subtasks": [{"title":"…","body":"…","skills":["…"]}],\n'
        '  "request_review": false,\n'
        '  "comments": ["…"],\n'
        '  "self_check": {"verification_ran": true, "notes": "仅参考，不作裁决依据"} }\n'
        "```"
    )

    return "\n".join(parts)


def _build_env(task: Any, decl: Declaration, creds: dict, repo_url: str) -> list[str]:
    """Build the Docker ``--env`` arguments (§5.2).

    Critically does NOT set ``HERMES_KANBAN_*`` — this prevents the kanban
    toolset from being injected into the worker (§5.2, I2).
    """
    env: list[str] = [
        f"TALOS_TASK_ID={task.id}",
        f"TALOS_RUN_ID={task.current_run_id}",
        "HERMES_TASK_WORKSPACE=/work",
    ]

    if getattr(task, "tenant", None):
        env.append(f"HERMES_TENANT={task.tenant}")

    # Resource spec
    env.append(f"HERMES_RESOURCE_SPEC=memory={decl.resources.memory_mb}mb,cpus={decl.resources.cpus}")
    env.append("HERMES_RESOURCE_SOURCE=talos-executor")

    # Git config (§5.2): credential helper + author identity
    env.append("GIT_CONFIG_COUNT=2")
    env.append("GIT_CONFIG_KEY_0=credential.helper")
    env.append("GIT_CONFIG_VALUE_0=store --file=/task/creds/git-credentials")
    env.append("GIT_CONFIG_KEY_1=user.name")
    env.append(f"GIT_CONFIG_VALUE_1=talos[{task.id}]")

    # Repo / branch for the worker
    if repo_url:
        env.append(f"TALOS_REPO={repo_url}")
    if decl.git.branch:
        env.append(f"TALOS_BRANCH={decl.git.branch}")

    # Container HERMES_HOME
    env.append(f"HERMES_HOME={CONTAINER_HH}")

    # LLM API keys — the worker needs an inference provider to function.
    # Pass through keys from the executor's environment (not from .env file,
    # to avoid leaking other secrets). Only keys explicitly listed here.
    _api_key_passsthrough = (
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "API_SERVER_KEY",
    )
    for key in _api_key_passsthrough:
        val = os.environ.get(key)
        if val:
            env.append(f"{key}={val}")

    return env


def _build_mounts(task: Any, decl: Declaration, tdir: Path, creds: dict) -> list[str]:
    """Build Docker ``-v`` mount arguments (§5.1 mount whitelist, I9)."""
    mounts: list[str] = []

    # Plugins (ro)
    plugins = HOME / "plugins"
    if plugins.exists():
        mounts.append(f"{plugins}:{CONTAINER_HH}/plugins:ro")

    # Managed skills (ro) — only the skills declared on this task
    for skill_name in (task.skills or []):
        skill_dir = HOME / "skills" / skill_name
        if skill_dir.exists():
            mounts.append(f"{skill_dir}:{CONTAINER_HH}/skills/{skill_name}:ro")

    # Config.yaml (ro) — required for plugin registration
    config = Path("/etc/hermes/config.yaml")
    if config.exists():
        mounts.append(f"{config}:{CONTAINER_HH}/config.yaml:ro")
    else:
        # Fallback: managed config in HOME
        hc = HOME / "config.yaml"
        if hc.exists():
            mounts.append(f"{hc}:{CONTAINER_HH}/config.yaml:ro")

    # Context file (ro)
    ctx_file = tdir / "context.md"
    mounts.append(f"{ctx_file}:/task/context.md:ro")

    # Output directory (rw) — the only writable exit
    out_dir = tdir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    mounts.append(f"{out_dir}:/task/out:rw")

    # Credentials (ro)
    creds_dir = tdir / "creds"
    if (creds_dir / "git-credentials").exists():
        mounts.append(f"{creds_dir / 'git-credentials'}:/task/creds/git-credentials:ro")
    if (creds_dir / "mcp-tokens").exists():
        mounts.append(f"{creds_dir / 'mcp-tokens'}:{CONTAINER_HH}/mcp-tokens:ro")

    # NO kanban directory mounted — I2/I9

    return mounts


def _build_docker_command(
    task: Any,
    decl: Declaration,
    tdir: Path,
    creds: dict,
    repo_url: str,
) -> list[str]:
    """Build the full ``docker run`` command line."""
    name = container_name(task.id, task.current_run_id)
    env = _build_env(task, decl, creds, repo_url)
    mounts = _build_mounts(task, decl, tdir, creds)

    cmd: list[str] = [
        "docker", "run",
        "-d",
        "--name", name,
        "--memory", f"{decl.resources.memory_mb}m",
        "--cpus", str(decl.resources.cpus),
        "--restart", "no",
        # Override entrypoint: hermes-worker image has ENTRYPOINT ["hermes"],
        # so we must use sh as entrypoint to make `$(cat ...)` work.
        "--entrypoint", "sh",
    ]

    for e in env:
        cmd.extend(["-e", e])

    for m in mounts:
        cmd.extend(["-v", m])

    # Remove filesystem on exit but keep logs accessible via docker inspect
    # NOTE: We do NOT use --rm because we need to docker cp from the exited
    # container's filesystem before removing it (Q3: cp works on exited
    # containers, but only before rm).
    cmd.append(WORKER_IMAGE)

    # Worker entry point (§5.3)
    # NOTE: design doc says `hermes -p worker --cli --accept-hooks chat -q "..."`
    # but `-p` is --provider in hermes CLI, not a profile. Correct syntax is
    # `hermes chat --cli --accept-hooks -q "..."`. This is an implementation
    # fix, not a design change.
    cmd.extend([
        "sh", "-c",
        'hermes chat --cli --accept-hooks -q "$(cat /task/context.md)"',
    ])

    return cmd


def make_spawn_fn(conn: Any):
    """Create a spawn_fn closure for ``dispatch_once``.

    The returned callable has signature ``(task, workspace) -> Optional[int]``
    and returns the sentinel PID (Q1: ``_call_spawn_fn`` expects ``Optional[int]``).

    The PID returned is the container's main PID — Docker reports it via
    ``docker inspect --format '{{.State.Pid}}'``. The kernel uses this PID
    for liveness checks (``os.kill(pid, 0)``) and signal delivery
    (``os.kill(pid, SIGTERM/SIGKILL)``).
    """

    def spawn_fn(task: Any, workspace: str, board: Optional[str] = None) -> Optional[int]:
        """Spawn a worker container for *task*.

        Returns the container PID (int) or None on failure.
        """
        t0 = time.time()
        run_id = task.current_run_id
        if run_id is None:
            log_event("error", task_id=task.id, run_id=None,
                      msg="spawn called with no current_run_id")
            return None

        tdir = task_dir(task.id, run_id)
        tdir.mkdir(parents=True, exist_ok=True)

        # Load declarations (I1: type-agnostic, driven by skill frontmatter)
        decl = load_declarations(task.skills, task)

        # Determine repo URL
        repo_url = _extract_repo(task)

        # Mint credentials (§9)
        creds = mint_credentials(task, decl.credentials, repo_url, tdir)

        # Write context.md (§5.3)
        context_md = _build_context_md(conn, task, decl)
        (tdir / "context.md").write_text(context_md, encoding="utf-8")

        # Build docker command
        cmd = _build_docker_command(task, decl, tdir, creds, repo_url)

        # Start sentinel process (forks; sentinel runs docker run, waits for
        # container exit, then waits for adjudication marker — §3, I8)
        pid = start_sentinel(task.id, run_id, tdir, cmd)

        log_event("dispatched", task_id=task.id, run_id=run_id,
                  duration_ms=(time.time() - t0) * 1000,
                  extra={"container": container_name(task.id, run_id), "sentinel_pid": pid})
        return pid

    return spawn_fn
