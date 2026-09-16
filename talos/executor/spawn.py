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


def _build_context_md(task: Any, decl: Declaration) -> str:
    """Build the context.md file (§5.3).

    Order: ⓪ 绑定参数段 (v2.1, I11) ① ``hermes kanban context`` output
    ② declaration summary ③ closing requirements (fixed text).

    本函数自行通过 ``kanban_db_connect`` 开连接，不接收外部 conn，
    避免闭包持有已关闭的连接（v2.1 FIX #3）。
    """
    parts: list[str] = []

    # ⓪ 绑定参数段 (v2.1, I11): injected binding params for the 实例 to read,
    #   not discover from the kanban context body.
    repo_url = _extract_repo(task)
    branch = getattr(task, "branch_name", None) or decl.git.branch or ""
    tenant = getattr(task, "tenant", None) or ""
    deliverables_str = "; ".join(
        f"{dl.kind}({dl.repo or dl.path}→{dl.branch})"
        for dl in decl.deliverables
    ) if decl.deliverables else "—"
    parts.append("## 绑定参数\n")
    parts.append(f"- repo: {repo_url or '—'}")
    parts.append(f"- branch: {branch or '—'}")
    parts.append(f"- tenant: {tenant or '—'}")
    parts.append(f"- workdir: /work")
    parts.append(f"- deliverables: {deliverables_str}")
    parts.append(f"- verification.source: {decl.verification.source}")

    # ① Kanban context (from the kernel's build_worker_context)
    try:
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli.kanban_db import build_worker_context
        with kbc.connect() as conn:
            parts.append(build_worker_context(conn, task.id))
    except Exception as e:
        # 上下文生成失败必须记 error，不得静默退化（v2.1 FIX #3）
        log_event("error", task_id=getattr(task, "id", None),
                  run_id=getattr(task, "current_run_id", None),
                  msg=f"build_worker_context failed: {e}")
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
        f"`{decl.git.branch or 'talos/' + task.id}`"
        "（`git push origin HEAD:$TALOS_BRANCH`）；"
        "(2) 在 `/task/out/result.json` 写入结果（格式见下）。"
        "不写结果文件视为失败。不要尝试操作看板，你没有看板工具。"
        "仓库、分支等绑定参数以本文件「绑定参数」段为准，不要自行选择或更改。"
        "仓库已指定 clone 到 /work 本身（`git clone $TALOS_REPO /work` 或在 /work 内 `git init` + `remote add`），"
        "不要建子目录；声明的产物路径以 /work 为根解析。"
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


def _generate_container_config(tdir: Path, task: Any) -> Path:
    """Generate a managed config.yaml for the worker container (v2.3 §18.2 #1).

    Reads ``deploy/container-config.template.yaml`` from the Talos repo and
    substitutes ``${TALOS_*}`` placeholders from the executor's environment.

    The generated config:
      - Enables talos-plugins (path_protect, skill_protect, trace_collect)
      - Sets hooks_auto_accept: true (无人值守审批)
      - Sets dialog_policy: auto_accept
      - Contains model/provider config (no secrets — key_env is an env var *name*)
      - Contains NO host API keys or secrets (those are injected via env vars)

    Returns the path to the generated config file.

    Raises:
        RuntimeError: if any required ``TALOS_MODEL_*`` env var is missing.
    """
    config_path = tdir / "config.yaml"

    # Locate the template relative to this file (spawn.py → executor/ → talos/ → repo root)
    template_path = Path(__file__).resolve().parent.parent.parent / "deploy" / "container-config.template.yaml"
    if not template_path.is_file():
        raise RuntimeError(
            f"container-config.template.yaml not found at {template_path}"
        )

    template = template_path.read_text(encoding="utf-8")

    # Required env vars — all must be present to render a valid config.
    # TALOS_MODEL_KEY_ENV is the *name* of the env var holding the key,
    # not the key value itself, so it is safe to write into config.yaml.
    required_vars = [
        "TALOS_MODEL",
        "TALOS_MODEL_PROVIDER",
        "TALOS_MODEL_PROVIDER_NAME",
        "TALOS_MODEL_BASE_URL",
        "TALOS_MODEL_KEY_ENV",
    ]
    substitutions: dict[str, str] = {}
    missing: list[str] = []
    for var in required_vars:
        val = os.environ.get(var)
        if not val:
            missing.append(var)
        else:
            substitutions[var] = val

    if missing:
        raise RuntimeError(
            f"Cannot render container config: missing env vars: {missing}. "
            f"Set them in ~/.hermes/talos.env (see deploy/talos.env.template)."
        )

    # Simple ${VAR} substitution — no shell expansion, no format-string injection.
    rendered = template
    for var, val in substitutions.items():
        rendered = rendered.replace(f"${{{var}}}", val)

    config_path.write_text(rendered, encoding="utf-8")
    return config_path


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
    # 注入 user.email，避免提交无作者邮箱（v2.1 FIX #6）
    env.append("GIT_CONFIG_COUNT=3")
    env.append("GIT_CONFIG_KEY_0=credential.helper")
    env.append("GIT_CONFIG_VALUE_0=store --file=/task/creds/git-credentials")
    env.append("GIT_CONFIG_KEY_1=user.name")
    env.append(f"GIT_CONFIG_VALUE_1=talos[{task.id}]")
    env.append("GIT_CONFIG_KEY_2=user.email")
    env.append("GIT_CONFIG_VALUE_2=talos-worker@haier.net")

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

    # NO_PROXY — Docker Desktop intercepts container HTTPS traffic via a built-in
    # proxy (http.docker.internal:3128).  That proxy breaks TLS to internal IPs,
    # causing SSLEOFError when the worker tries to reach an on-prem model gateway.
    # Setting NO_PROXY/no_proxy tells the container's HTTP libraries to bypass the
    # proxy for the listed hosts.  The value is read from the deployment env file
    # (TALOS_NO_PROXY) so no internal domain is hardcoded in source.
    no_proxy = os.environ.get("TALOS_NO_PROXY")
    if no_proxy:
        env.append(f"NO_PROXY={no_proxy}")
        env.append(f"no_proxy={no_proxy}")

    return env


def _build_mounts(task: Any, decl: Declaration, tdir: Path, creds: dict) -> list[str]:
    """Build Docker ``-v`` mount arguments (§5.1 mount whitelist, I9)."""
    mounts: list[str] = []

    # Talos plugins only (ro) — trace_collect, path_protect, skill_protect.
    # Mount from the Talos repo, NOT from ~/.hermes/plugins (avoid duplicates).
    # Hermes discovers plugins by scanning HERMES_HOME/plugins/<name>/plugin.yaml
    talos_plugins = Path(__file__).resolve().parent.parent / "plugins"
    if talos_plugins.exists():
        mounts.append(f"{talos_plugins}:{CONTAINER_HH}/plugins/talos:ro")

    # Managed skills (ro) — only the skills declared on this task
    for skill_name in (task.skills or []):
        skill_dir = HOME / "skills" / skill_name
        if skill_dir.exists():
            mounts.append(f"{skill_dir}:{CONTAINER_HH}/skills/{skill_name}:ro")

    # Managed config.yaml (ro) — generated per-task by _generate_container_config
    # (v2.3 §18.2 #1: spawn generates config with talos plugins enabled,
    #  no host secrets, auto-accept). No longer mounts host config.yaml.
    managed_config = tdir / "config.yaml"
    if managed_config.exists():
        mounts.append(f"{managed_config}:{CONTAINER_HH}/config.yaml:ro")
    else:
        log_event("error", task_id=getattr(task, "id", None),
                  run_id=getattr(task, "current_run_id", None),
                  msg="managed config.yaml not found — _generate_container_config not called?")

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
    # --accept-hooks is now redundant — managed config.yaml sets
    # hooks_auto_accept: true (v2.3 §18.2 #1). Keep --cli for headless mode.
    # With --entrypoint sh, CMD args are ["-c", '...'] (not ["sh", "-c", ...]).
    cmd.extend([
        "-c",
        'hermes chat --cli --accept-hooks -q "$(cat /task/context.md)"',
    ])

    return cmd


def make_spawn_fn():
    """Create a spawn_fn closure for ``dispatch_once``.

    The returned callable has signature ``(task, workspace) -> Optional[int]``
    and returns the sentinel PID (Q1: ``_call_spawn_fn`` expects ``Optional[int]``).

    The PID returned is the container's main PID — Docker reports it via
    ``docker inspect --format '{{.State.Pid}}'``. The kernel uses this PID
    for liveness checks (``os.kill(pid, 0)``) and signal delivery
    (``os.kill(pid, SIGTERM/SIGKILL)``).

    闭包不接收 conn 参数，spawn_fn 内部自行通过 ``kanban_db_connect``
    开连接（v2.1 FIX #3）。
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
        # DeclarationError → block immediately, do NOT requeue (re-running won't
        # change the skill's frontmatter).  This must happen BEFORE any container
        # starts, per §6/M12.
        from talos.executor.declarations import DeclarationError
        try:
            decl = load_declarations(task.skills, task)
        except DeclarationError as e:
            log_event("error", task_id=task.id, run_id=run_id,
                      msg=f"declaration load failed: {e}")
            try:
                from hermes_cli import kanban_db_connect as kbc
                from hermes_cli import kanban_db as kb
                from talos.executor.constants import EXECUTOR_AUTHOR
                reason = f"校验器故障：声明非法: {e}"
                with kbc.connect() as conn:
                    # P1-2: block_task first, comment only on success.
                    # A "blocked" comment without a successful block is misleading.
                    kb.block_task(
                        conn, task.id,
                        reason=reason,
                        kind="capability",
                        expected_run_id=run_id,
                    )
                    kb.add_comment(
                        conn, task.id,
                        author=EXECUTOR_AUTHOR,
                        body=f"[执行器] 拉起前检查(run {run_id})：{reason}",
                    )
            except Exception as fe:
                log_event("error", task_id=task.id, run_id=run_id,
                          msg=f"block_task (decl error) failed: {fe}")
            return None

        # Determine repo URL (I11: binding param from task body first line 'repo:')
        repo_url = _extract_repo(task)

        # ── I11 binding param check (v2.1): verify all declared `requires`
        #    are present before starting the 实例. Missing binding → defect,
        #    do NOT start container, return None, degrade-complete the task.
        branch = getattr(task, "branch_name", None) or decl.git.branch or ""
        tenant = getattr(task, "tenant", None) or ""
        bindings = {
            "repo": repo_url,
            "branch": branch,
            "tenant": tenant,
        }
        missing = [name for name in decl.requires if not bindings.get(name)]
        if missing:
            log_event("error", task_id=task.id, run_id=run_id,
                      msg=f"I11 binding params missing: {missing}")
            try:
                from hermes_cli import kanban_db_connect as kbc
                from hermes_cli import kanban_db as kb
                from talos.executor.constants import EXECUTOR_AUTHOR
                with kbc.connect() as conn:
                    reason = f"缺失绑定参数: {', '.join(missing)}（I11），不拉起实例"
                    kb.add_comment(
                        conn, task.id,
                        author=EXECUTOR_AUTHOR,
                        body=f"[执行器] {reason}",
                    )
                    # v2.3 §18.2 #4: pre-spawn defect → block_task, NOT done.
                    # Missing binding params = capability block (the executor
                    # cannot fulfill the task without the binding).
                    kb.block_task(
                        conn, task.id,
                        reason=reason,
                        kind="capability",
                        expected_run_id=run_id,
                    )
            except Exception as e:
                log_event("error", task_id=task.id, run_id=run_id,
                          msg=f"I11 block_task failed: {e}")
            return None

        # Mint credentials (§9)
        creds = mint_credentials(task, decl.credentials, repo_url, tdir)

        # Write context.md (§5.3)
        context_md = _build_context_md(task, decl)
        (tdir / "context.md").write_text(context_md, encoding="utf-8")

        # Generate managed container config.yaml (v2.3 §18.2 #1)
        # Enables talos-plugins, auto-accept, no host secrets
        _generate_container_config(tdir, task)

        # Build docker command
        cmd = _build_docker_command(task, decl, tdir, creds, repo_url)

        # Start sentinel process (forks; sentinel runs docker run, waits for
        # container exit, then waits for adjudication marker — §3, I8)
        # 哨兵寿命 = 60 + verification_timeout_s + 60（v2.1 FIX #4）
        verification_timeout_s = decl.verification.timeout_s if decl.verification.required else 0
        adj_timeout = 60 + verification_timeout_s + 60
        pid = start_sentinel(task.id, run_id, tdir, cmd, adj_timeout=adj_timeout)

        log_event("dispatched", task_id=task.id, run_id=run_id,
                  duration_ms=(time.time() - t0) * 1000,
                  extra={"container": container_name(task.id, run_id), "sentinel_pid": pid})
        return pid

    return spawn_fn
