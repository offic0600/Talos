"""Sentinel: per-task subprocess that keeps the PID alive until adjudication (§3, I8).

The sentinel is an executor child process. Its PID is what the kernel sees as
the worker PID. It:

1. Starts the docker container (``docker run -d``)
2. Waits for the container to exit (``docker wait``)
3. Waits for the ``<tdir>/adjudicated`` marker file (set by the executor after
   finalize completes), with a timeout of ``adj_timeout`` (computed as
   60 + verification_timeout_s + 60, v2.1 FIX #4)
4. Exits 0

On SIGTERM (from kernel ``enforce_max_runtime``): kills the container via
``docker kill`` so it doesn't outlive the sentinel.

If the sentinel is SIGKILL'd before it can handle SIGTERM, the executor's
``reap_orphans`` will clean up the container on the next tick (sentinel PID
dead + container still running → docker kill).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from talos.executor.constants import (
    CONTAINER_HH,
    WORKER_IMAGE,
    container_name,
    log_event,
    task_dir,
)

#: Default adjudication timeout if no verification_timeout_s is provided.
#: 哨兵寿命由裁决决定，不由常数决定（v2.1 FIX #4）；
#: 实际值 = 60 + verification_timeout_s + 60，由调用方传入。
DEFAULT_ADJ_TIMEOUT = 300

#: How often (seconds) to check executor liveness during marker wait.
EXECUTOR_LIVENESS_CHECK_INTERVAL = 30

#: Max age (seconds) of executor heartbeat before sentinel considers it dead.
EXECUTOR_HEARTBEAT_MAX_AGE = 60


def _executor_is_alive() -> bool:
    """Check if the executor is alive by reading executor.alive mtime (v2.3 §18.2 #2).

    The executor runs a background thread that touches ~/.hermes/talos/executor.alive
    every 5 seconds. If the file's mtime is within EXECUTOR_HEARTBEAT_MAX_AGE (60s),
    the executor is considered alive.

    This replaces the old executor.jsonl tail-read, which was unreliable when the
    executor was blocked in a long adjudication (CI polling) — the jsonl wasn't
    being written but the executor was still alive.
    """
    from talos.executor.constants import TALOS_HOME

    alive_file = TALOS_HOME / "executor.alive"
    if not alive_file.exists():
        return False
    try:
        age = time.time() - alive_file.stat().st_mtime
        return age <= EXECUTOR_HEARTBEAT_MAX_AGE
    except OSError as e:
        log_event("error", msg=f"sentinel: executor liveness check failed: {e}")
        return False


def run_sentinel(task_id: str, run_id: int, tdir: Path, docker_cmd: list[str],
                 adj_timeout: int = DEFAULT_ADJ_TIMEOUT) -> int:
    """Run the sentinel process for one task.

    Parameters
    ----------
    task_id : str
    run_id : int
    tdir : Path
        Per-run task directory (``~/.hermes/talos/tasks/<task_id>/<run_id>/``).
    docker_cmd : list[str]
        The full ``docker run -d --name ...`` command to start the container.
    adj_timeout : int
        How long to wait for the adjudication marker after container exit.
        由调用方计算 60 + verification_timeout_s + 60 传入（v2.1 FIX #4）。

    Returns
    ----------
    int
        Exit code (0 = clean).
    """
    cname = container_name(task_id, run_id)
    adjudicated_marker = tdir / "adjudicated"

    # Signal handler: SIGTERM → kill container
    def _on_sigterm(_signum, _frame):
        try:
            subprocess.run(
                ["docker", "kill", cname],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            # 禁止空吞异常（v2.1 FIX #3）
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"sentinel: docker kill on SIGTERM failed: {e}")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    # 1. Start the container
    try:
        result = subprocess.run(
            docker_cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"sentinel: docker run failed: {result.stderr.strip()[:500]}")
            return 1
    except subprocess.TimeoutExpired:
        log_event("error", task_id=task_id, run_id=run_id,
                  msg="sentinel: docker run timed out")
        return 1

    log_event("sentinel_started", task_id=task_id, run_id=run_id,
              extra={"container": cname})

    # 2. Wait for container to exit
    try:
        subprocess.run(
            ["docker", "wait", cname],
            capture_output=True, text=True, timeout=3600,
        )
    except subprocess.TimeoutExpired:
        # Container ran too long — kernel should have killed the sentinel by now
        # 禁止空吞异常（v2.1 FIX #3）
        log_event("error", task_id=task_id, run_id=run_id,
                  msg="sentinel: docker wait timed out (3600s)")

    log_event("sentinel_container_exited", task_id=task_id, run_id=run_id)

    # 3. Wait for adjudication marker
    # 哨兵寿命 = adj_timeout（由调用方计算 60 + verification_timeout_s + 60）；
    # 等待期间每 30 秒检查执行器心跳，若超过 60 秒无心跳则退出让内核回收
    # （v2.1 FIX #4）。
    deadline = time.time() + adj_timeout
    last_liveness_check = time.time()
    while time.time() < deadline:
        if adjudicated_marker.exists():
            log_event("sentinel_adjudicated", task_id=task_id, run_id=run_id)
            return 0
        # 每 30 秒检查执行器存活（v2.1 FIX #4）
        if time.time() - last_liveness_check >= EXECUTOR_LIVENESS_CHECK_INTERVAL:
            last_liveness_check = time.time()
            if not _executor_is_alive():
                log_event("error", task_id=task_id, run_id=run_id,
                          msg="sentinel: executor heartbeat stale >60s, exiting")
                return 1
        time.sleep(1)

    # Timeout — sentinel exits without adjudication
    log_event("error", task_id=task_id, run_id=run_id,
              msg=f"sentinel: adjudication marker not set within {adj_timeout}s")
    return 1


def start_sentinel(task_id: str, run_id: int, tdir: Path, docker_cmd: list[str],
                   adj_timeout: int = DEFAULT_ADJ_TIMEOUT) -> int:
    """Fork a sentinel process and return its PID.

    The sentinel is a child of the executor process. If the executor crashes,
    the sentinel is reparented to init and continues running.

    adj_timeout 由 spawn.py 计算 60 + verification_timeout_s + 60 传入
    （v2.1 FIX #4）。
    """
    pid = os.fork()
    if pid == 0:
        # Child — become the sentinel
        # Decouple from parent's process group so parent signals don't affect us
        os.setsid()
        # Run sentinel (this blocks until container exits + adjudication)
        exit_code = run_sentinel(task_id, run_id, tdir, docker_cmd,
                                 adj_timeout=adj_timeout)
        os._exit(exit_code)

    # Parent — return sentinel PID
    log_event("sentinel_forked", task_id=task_id, run_id=run_id,
              extra={"sentinel_pid": pid})
    return pid
