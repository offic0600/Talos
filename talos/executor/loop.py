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
import signal
import threading
import time
from typing import Any, Optional

from talos.executor.adjudicate import adjudicate
from talos.executor.archive import archive
from talos.executor.collect import collect
from talos.executor.constants import (
    DEFAULT_FAILURE_LIMIT,
    EXECUTOR_AUTHOR,
    TICK_INTERVAL,
    log_event,
)
from talos.executor.declarations import load_declarations
from talos.executor.finalize import finalize
from talos.executor.reap import list_exited_containers, list_running_containers, reap
from talos.executor.spawn import make_spawn_fn


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
        except (json.JSONDecodeError, TypeError):
            pass

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
            except Exception:
                pass

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
        except Exception:
            pass

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
        except Exception:
            decl = load_declarations(None)

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
    """Call ``dispatch_once`` to claim and spawn ready tasks (§3)."""
    from hermes_cli.kanban_db_dispatch import dispatch_once, DEFAULT_FAILURE_LIMIT

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
        spawn_fn = make_spawn_fn(conn)

    # 1. Adjudicate exited containers (collect → adjudicate → finalize → archive → reap)
    _adjudicate_exited(conn)

    # 2. Heartbeat live containers
    _heartbeat_live_containers(conn)

    # 3. Dispatch new ready tasks
    _dispatch(conn, spawn_fn)

    log_event("heartbeat", duration_ms=(time.time() - t0) * 1000,
              extra={"tick": True})


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

    # Print resolved paths on startup (§11)
    from talos.executor.constants import KANBAN_DB, EXECUTOR_LOG, TALOS_HOME
    print(f"[talos-executor] TALOS_HOME={TALOS_HOME}")
    print(f"[talos-executor] KANBAN_DB={KANBAN_DB}")
    print(f"[talos-executor] EXECUTOR_LOG={EXECUTOR_LOG}")
    print(f"[talos-executor] tick interval={interval}s")

    from hermes_cli import kanban_db_connect as kbc

    spawn_fn: Optional[Any] = None

    while not stop_event.is_set():
        try:
            with contextlib.closing(kbc.connect()) as conn:
                if spawn_fn is None:
                    spawn_fn = make_spawn_fn(conn)
                tick(conn, spawn_fn)
        except Exception as e:
            import traceback
            traceback.print_exc()
            log_event("error", msg=f"tick exception: {e}")

        if on_tick is not None:
            with contextlib.suppress(Exception):
                on_tick()

        stop_event.wait(timeout=interval)

    print("[talos-executor] stopped")
