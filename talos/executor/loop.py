"""Main executor loop (§3).

Tick order (I8 — fixed):
  1. Adjudicate exited containers (collect → adjudicate → finalize → archive → reap)
  2. Heartbeat live containers
  3. Dispatch new ready tasks via ``dispatch_once``

I7 (idempotent): before adjudicating, check if the run already has a verdict
in ``task_runs.metadata``. If so, skip to reap.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from talos.executor.adjudicate import adjudicate
from talos.executor.archive import archive
from talos.executor.collect import collect
from talos.executor.constants import (
    DEFAULT_FAILURE_LIMIT,
    EXECUTOR_AUTHOR,
    TALOS_HOME,
    TICK_INTERVAL,
    log_event,
)
from talos.executor.declarations import load_declarations
from talos.executor.finalize import finalize
from talos.executor.reap import list_exited_containers, list_running_containers, reap, reap_orphans
from talos.executor.spawn import make_spawn_fn

#: Alive-signal file path (v2.3 §18.2 #2).
_EXECUTOR_ALIVE_FILE = TALOS_HOME / "executor.alive"

#: How often (seconds) to touch the alive file.
_ALIVE_INTERVAL = 5.0


def _start_alive_thread(stop_event: threading.Event) -> threading.Thread:
    """Start a daemon thread that touches executor.alive every 5 seconds (v2.3 §18.2 #2).

    The sentinel reads this file's mtime to determine if the executor is alive,
    instead of reading executor.jsonl (which is blocked during long adjudication).
    """
    def _touch():
        while not stop_event.is_set():
            try:
                _EXECUTOR_ALIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
                _EXECUTOR_ALIVE_FILE.touch()
            except OSError as e:
                log_event("error", msg=f"alive touch failed: {e}")
            stop_event.wait(timeout=_ALIVE_INTERVAL)

    t = threading.Thread(target=_touch, daemon=True, name="talos-alive")
    t.start()
    # Touch immediately so the file exists before the first tick
    try:
        _EXECUTOR_ALIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _EXECUTOR_ALIVE_FILE.touch()
    except OSError:
        pass
    return t


def _is_run_adjudicated(conn: Any, run_id: int) -> bool:
    """Check if a run already has a verdict (I7 idempotency).

    A run is adjudicated if ``task_runs.metadata`` contains a ``verdict`` key
    or if the run's status is already terminal (done/blocked/gave_up/etc.).
    """
    if run_id is None:
        return False
    try:
        row = conn.execute(
            "SELECT status, metadata, outcome FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    except Exception:
        return False
    if row is None:
        return False

    status = row["status"] if "status" in row.keys() else None
    outcome = row["outcome"] if "outcome" in row.keys() else None

    # Terminal run statuses
    terminal = {"done", "blocked", "gave_up", "completed", "timed_out",
                "crashed", "adjudication_unmet", "checker_error"}
    if status in terminal or outcome in terminal:
        return True

    # Check metadata for a verdict key (set by finalize)
    meta_raw = row["metadata"] if "metadata" in row.keys() else None
    if meta_raw:
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            if isinstance(meta, dict) and "verdict" in meta:
                return True
        except (json.JSONDecodeError, TypeError) as e:
            # 禁止空吞异常（v2.1 FIX #3）
            log_event("error", run_id=run_id,
                      msg=f"metadata parse failed: {e}")

    return False


def _heartbeat_live_containers(conn: Any) -> None:
    """Heartbeat all running worker containers (§8, I8)."""
    from hermes_cli import kanban_db as kb

    containers = list_running_containers()
    for ctr in containers:
        task_id = ctr["task_id"]
        run_id = ctr["run_id"]
        try:
            # Get the task to find its claim_lock
            task = kb.get_task(conn, task_id)
            if task is None or task.status != "running":
                continue

            claimer = task.claim_lock
            if claimer:
                kb.heartbeat_claim(conn, task_id, claimer=claimer)

            # Also heartbeat the worker (updates last_heartbeat_at)
            try:
                from hermes_cli.kanban_db_dispatch import heartbeat_worker
                heartbeat_worker(conn, task_id, note="talos-executor",
                                 expected_run_id=run_id)
            except Exception as e:
                # 禁止空吞异常（v2.1 FIX #3）
                log_event("error", task_id=task_id, run_id=run_id,
                          msg=f"heartbeat_worker failed: {e}")

            log_event("heartbeat", task_id=task_id, run_id=run_id)
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"heartbeat failed: {e}")


def _adjudicate_exited(conn: Any) -> None:
    """Collect, adjudicate, finalize, archive, and reap exited containers (§3, I8)."""
    from hermes_cli import kanban_db as kb

    containers = list_exited_containers()
    for ctr in containers:
        task_id = ctr["task_id"]
        run_id = ctr["run_id"]

        # I7: skip if already adjudicated
        if _is_run_adjudicated(conn, run_id):
            # Still reap the container if it's still around
            reap(task_id, run_id)
            continue

        # I8: heartbeat during adjudication
        try:
            task = kb.get_task(conn, task_id)
            if task and task.status == "running" and task.claim_lock:
                kb.heartbeat_claim(conn, task_id, claimer=task.claim_lock)
        except Exception as e:
            # 禁止空吞异常（v2.1 FIX #3）
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"adjudication heartbeat failed: {e}")

        # Collect
        try:
            bundle = collect(task_id, run_id)
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"collect failed: {e}")
            reap(task_id, run_id)
            continue

        # Load declarations for adjudication
        try:
            task = kb.get_task(conn, task_id)
            decl = load_declarations(task.skills if task else None, task)
        except Exception as e:
            # DeclarationError or other load failure → error verdict (§6, M12)
            # v2.3 §18.2 #4: pre-spawn defect → block_task, NOT requeue.
            from talos.executor.adjudicate import Verdict
            from talos.executor.declarations import DeclarationError
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"declaration load failed: {e}")
            verdict = Verdict(
                status="error",
                defects=[f"校验器故障: {e}"],
            )
            try:
                # v2.3 §18.2 #4: bad declaration = capability block
                from hermes_cli import kanban_db as kb
                from talos.executor.constants import EXECUTOR_AUTHOR
                reason = f"校验器故障: {e}"
                kb.add_comment(conn, task_id, author=EXECUTOR_AUTHOR,
                               body=f"[执行器] 裁决(run {run_id})：{reason}")
                kb.block_task(conn, task_id, reason=reason,
                              kind="capability", expected_run_id=run_id)
            except Exception as fe:
                log_event("error", task_id=task_id, run_id=run_id,
                          msg=f"block_task (decl error) failed: {fe}")
            # bundle 已在上方收集，不再重复 collect（v2.1 FIX #10）
            try:
                archive(task_id, run_id, bundle, verdict)
            except Exception as ae:
                # 禁止空吞异常（v2.1 FIX #3）
                log_event("error", task_id=task_id, run_id=run_id,
                          msg=f"archive (decl error) failed: {ae}")
            reap(task_id, run_id)
            continue

        # Adjudicate
        try:
            verdict = adjudicate(task_id, run_id, bundle, decl)
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"adjudicate failed: {e}")
            # Create an error verdict
            from talos.executor.adjudicate import Verdict
            verdict = Verdict(status="error", defects=[f"adjudicate exception: {e}"])

        # Finalize (apply to kanban board)
        try:
            finalize(conn, task_id, run_id, verdict)
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"finalize failed: {e}")

        # Archive
        try:
            archive(task_id, run_id, bundle, verdict)
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"archive failed: {e}")

        # Reap (container + credentials)
        reap(task_id, run_id)


def _dispatch(conn: Any, spawn_fn: Any) -> None:
    """Call ``dispatch_once`` to claim and spawn ready tasks (§3).

    使用模块级 ``DEFAULT_FAILURE_LIMIT``，不在函数内重复导入
    造成 shadowing（v2.1 FIX #10）。
    """
    from hermes_cli.kanban_db_dispatch import dispatch_once

    try:
        result = dispatch_once(
            conn,
            spawn_fn=spawn_fn,
            failure_limit=DEFAULT_FAILURE_LIMIT,
        )
        if result.spawned:
            for task_id, assignee, workspace in result.spawned:
                log_event("dispatched", task_id=task_id,
                          extra={"assignee": assignee})
    except Exception as e:
        log_event("error", msg=f"dispatch_once failed: {e}")


def tick(conn: Any, spawn_fn: Optional[Any] = None) -> None:
    """Run one executor tick (§3).

    Order (I8):
      1. Adjudicate exited containers
      2. Heartbeat live containers
      3. Dispatch new tasks
    """
    t0 = time.time()

    if spawn_fn is None:
        spawn_fn = make_spawn_fn()

    # 1. Adjudicate exited containers (collect → adjudicate → finalize → archive → reap)
    _adjudicate_exited(conn)

    # 2. Heartbeat live containers
    _heartbeat_live_containers(conn)

    # 3. Reap orphan containers (sentinel dead but container still running)
    reap_orphans()

    # 4. Dispatch new ready tasks
    _dispatch(conn, spawn_fn)

    log_event("heartbeat", duration_ms=(time.time() - t0) * 1000,
              extra={"tick": True})


def _self_check(
    worker_image: str,
    kanban_db: Any,
    gitlab_url: str,
) -> None:
    """Startup self-check (§11, v2.1 #9, v2.2 #4).

    Hard-fail (exit) items:
      1. ``hermes_cli`` importable
      2. ``WORKER_IMAGE`` exists in ``docker images``
      3. Kanban DB path readable
      4. GitLab reachable (v2.2: hard fail)
      5. Skills installed and match repo (v2.2: md5 check)

    Each result is printed.  Any hard failure calls ``sys.exit(1)``.
    """
    # ── (1) hermes_cli importable — HARD FAIL ────────────────────────
    try:
        import hermes_cli  # noqa: F401
        print("[talos-executor] self-check (1/5) hermes_cli importable: OK")
    except ImportError as e:
        print(f"[talos-executor] self-check (1/5) hermes_cli importable: FAIL — {e}")
        print("[talos-executor] hint: pip install -e ~/.hermes/hermes-agent")
        sys.exit(1)

    # ── (2) WORKER_IMAGE in docker images — HARD FAIL ────────────────
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", worker_image],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            print(f"[talos-executor] self-check (2/5) docker image '{worker_image}': OK")
        else:
            print(
                f"[talos-executor] self-check (2/5) docker image '{worker_image}': FAIL — "
                "image not found"
            )
            print(
                f"[talos-executor] hint: docker build -t {worker_image} <context>"
            )
            sys.exit(1)
    except FileNotFoundError:
        print("[talos-executor] self-check (2/5) docker image: FAIL — docker not found")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("[talos-executor] self-check (2/5) docker image: FAIL — docker inspect timed out")
        sys.exit(1)

    # ── (3) Kanban DB path readable — HARD FAIL ──────────────────────
    db_path = Path(kanban_db) if not isinstance(kanban_db, Path) else kanban_db
    if db_path.is_file() and os.access(str(db_path), os.R_OK):
        print(f"[talos-executor] self-check (3/5) kanban DB readable: OK ({db_path})")
    else:
        print(
            f"[talos-executor] self-check (3/5) kanban DB readable: FAIL — "
            f"{db_path} does not exist or is not readable"
        )
        print("[talos-executor] hint: check HERMES_KANBAN_DB in /etc/hermes/talos.env")
        sys.exit(1)

    # ── (4) GitLab reachable — HARD FAIL (v2.2) ─────────────────────
    import urllib.request
    import urllib.error
    import ssl

    gl_token = os.environ.get("TALOS_GITLAB_ADMIN_TOKEN", "")
    # hgit.haier.net may use an internal CA — disable SSL verification
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(
            f"{gitlab_url}/api/v4/version",
            headers={"PRIVATE-TOKEN": gl_token} if gl_token else {},
        )
        resp = urllib.request.urlopen(req, timeout=10, context=ssl_ctx)
        print(f"[talos-executor] self-check (4/5) GitLab reachable: OK ({gitlab_url})")
    except urllib.error.HTTPError as e:
        print(
            f"[talos-executor] self-check (4/5) GitLab reachable: FAIL — "
            f"HTTP {e.code} {e.reason}"
        )
        print("[talos-executor] hint: check TALOS_GITLAB_ADMIN_TOKEN in ~/.hermes/talos.env")
        sys.exit(1)
    except Exception as e:
        print(
            f"[talos-executor] self-check (4/5) GitLab reachable: FAIL — "
            f"{gitlab_url} not reachable ({e})"
        )
        print("[talos-executor] hint: check network / VPN / GitLab URL")
        sys.exit(1)

    # ── (5) Skills installed and match repo (v2.2) — HARD FAIL ──────
    import hashlib

    repo_skills = Path(__file__).resolve().parent.parent.parent / "skills"
    home_skills = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))) / "skills"

    skill_dirs = []
    if repo_skills.is_dir():
        for child in sorted(repo_skills.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file():
                skill_dirs.append(child.name)

    if not skill_dirs:
        print(f"[talos-executor] self-check (5/5) skills: FAIL — no skills found in {repo_skills}")
        sys.exit(1)

    all_ok = True
    for sname in skill_dirs:
        repo_md = repo_skills / sname / "SKILL.md"
        home_md = home_skills / sname / "SKILL.md"
        if not home_md.is_file():
            print(f"[talos-executor] self-check (5/5) skill '{sname}': FAIL — not installed in {home_skills}")
            all_ok = False
            continue
        repo_hash = hashlib.md5(repo_md.read_bytes()).hexdigest()
        home_hash = hashlib.md5(home_md.read_bytes()).hexdigest()
        if repo_hash != home_hash:
            print(f"[talos-executor] self-check (5/5) skill '{sname}': FAIL — md5 mismatch (repo={repo_hash[:8]} home={home_hash[:8]})")
            all_ok = False
        else:
            print(f"[talos-executor] self-check (5/5) skill '{sname}': OK (md5 {repo_hash[:8]})")

    if not all_ok:
        print(f"[talos-executor] hint: run deploy/install-skills.sh to sync skills to {home_skills}")
        sys.exit(1)

    # ── (6) Container config renderable — HARD FAIL (v2.3 P0 fix) ───
    # Verifies that all TALOS_MODEL_* env vars are present and the
    # template renders to a valid config.yaml containing model/provider.
    try:
        import tempfile
        from talos.executor.spawn import _generate_container_config
        with tempfile.TemporaryDirectory() as tmpdir:
            tdir = Path(tmpdir)
            # _generate_container_config only needs tdir and a task-like object
            # (task is unused in the function body)
            cfg_path = _generate_container_config(tdir, task=None)
            content = cfg_path.read_text(encoding="utf-8")
            # Verify key fields are present (model, provider, base_url)
            has_model = "model:" in content and "default:" in content
            has_provider = "provider:" in content
            has_base_url = "base_url:" in content
            if not (has_model and has_provider and has_base_url):
                print(
                    f"[talos-executor] self-check (6/6) config render: FAIL — "
                    f"rendered config missing model/provider/base_url"
                )
                sys.exit(1)
            # Print model info (no key values — key_env is just a var name)
            model = os.environ.get("TALOS_MODEL", "?")
            provider = os.environ.get("TALOS_MODEL_PROVIDER", "?")
            base_url = os.environ.get("TALOS_MODEL_BASE_URL", "?")
            print(
                f"[talos-executor] self-check (6/6) config render: OK "
                f"(model={model}, provider={provider}, base_url={base_url})"
            )
    except RuntimeError as e:
        print(f"[talos-executor] self-check (6/6) config render: FAIL — {e}")
        print("[talos-executor] hint: set TALOS_MODEL/TALOS_MODEL_PROVIDER/TALOS_MODEL_BASE_URL/TALOS_MODEL_KEY_ENV in ~/.hermes/talos.env")
        sys.exit(1)
    except Exception as e:
        print(f"[talos-executor] self-check (6/6) config render: FAIL — {e}")
        sys.exit(1)


def run_executor(
    *,
    interval: float = TICK_INTERVAL,
    stop_event: Optional[threading.Event] = None,
    on_tick: Optional[Any] = None,
) -> None:
    """Run the executor as a persistent loop (§3, G1).

    Signal-safe (SIGINT/SIGTERM → set stop_event).
    Each tick: adjudicate → heartbeat → dispatch.
    """
    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    # Start alive-signal thread (v2.3 §18.2 #2)
    _start_alive_thread(stop_event)

    # Print resolved paths on startup (§11)
    from talos.executor.constants import (
        EXECUTOR_LOG, GITLAB_URL, KANBAN_DB, TALOS_HOME, WORKER_IMAGE,
    )
    print(f"[talos-executor] TALOS_HOME={TALOS_HOME}")
    print(f"[talos-executor] KANBAN_DB={KANBAN_DB}")
    print(f"[talos-executor] EXECUTOR_LOG={EXECUTOR_LOG}")
    print(f"[talos-executor] tick interval={interval}s")

    # ── Deploy self-check (§11, v2.1 #9) ──────────────────────────
    # Hard-fail items: hermes_cli importable, WORKER_IMAGE exists in
    # docker images, kanban DB path readable.
    # Warn-only item: GitLab reachable.
    # Each result is printed; any hard failure exits immediately.
    _self_check(WORKER_IMAGE, KANBAN_DB, GITLAB_URL)

    # Cleanup orphan tokens at startup (§8, M19) — best-effort, non-blocking.
    # If GitLab is unreachable, the executor still starts and processes local
    # exited containers. cleanup is retried every tick until it succeeds once.
    from talos.executor.credentials import cleanup_orphan_tokens
    _orphan_cleanup_done = False
    try:
        orphan_count = cleanup_orphan_tokens()
        _orphan_cleanup_done = True
        if orphan_count:
            print(f"[talos-executor] revoked {orphan_count} orphan token(s)")
    except Exception as e:
        log_event("error", msg=f"orphan token cleanup failed at startup: {e}")
        print(f"[talos-executor] orphan cleanup deferred: {e}")

    from hermes_cli import kanban_db_connect as kbc

    spawn_fn: Optional[Any] = None

    while not stop_event.is_set():
        try:
            with contextlib.closing(kbc.connect()) as conn:
                if spawn_fn is None:
                    spawn_fn = make_spawn_fn()
                tick(conn, spawn_fn)
        except Exception as e:
            import traceback
            traceback.print_exc()
            log_event("error", msg=f"tick exception: {e}")

        # Retry orphan token cleanup if startup failed (§8, M19)
        if not _orphan_cleanup_done:
            try:
                orphan_count = cleanup_orphan_tokens()
                _orphan_cleanup_done = True
                if orphan_count:
                    print(f"[talos-executor] revoked {orphan_count} orphan token(s) (deferred)")
            except Exception as e:
                log_event("error", msg=f"orphan token cleanup retry failed: {e}")

        if on_tick is not None:
            with contextlib.suppress(Exception):
                on_tick()

        stop_event.wait(timeout=interval)

    print("[talos-executor] stopped")
