"""Sentinel: per-task subprocess that keeps the PID alive until adjudication (§3, I8).

The sentinel is an executor child process. Its PID is what the kernel sees as
the worker PID. It:

1. Starts the docker container (``docker run -d``)
2. Waits for the container to exit (``docker wait``)
3. Waits for the ``<tdir>/adjudicated`` marker file (set by the executor after
   finalize completes), with a timeout of ADJ_TIMEOUT
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

#: How long the sentinel waits for adjudication to complete after container exit.
ADJ_TIMEOUT = 300  # 5 minutes — must be > adjudicate+finalize+archive worst case


def run_sentinel(task_id: str, run_id: int, tdir: Path, docker_cmd: list[str]) -> int:
    """Run the sentinel process for one task.

    Parameters
    ----------
    task_id : str
    run_id : int
    tdir : Path
        Per-run task directory (``~/.hermes/talos/tasks/<task_id>/<run_id>/``).
    docker_cmd : list[str]
        The full ``docker run -d --name ...`` command to start the container.

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
        except Exception:
            pass
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
        pass

    log_event("sentinel_container_exited", task_id=task_id, run_id=run_id)

    # 3. Wait for adjudication marker
    deadline = time.time() + ADJ_TIMEOUT
    while time.time() < deadline:
        if adjudicated_marker.exists():
            log_event("sentinel_adjudicated", task_id=task_id, run_id=run_id)
            return 0
        time.sleep(1)

    # Timeout — sentinel exits without adjudication
    log_event("error", task_id=task_id, run_id=run_id,
              msg=f"sentinel: adjudication marker not set within {ADJ_TIMEOUT}s")
    return 1


def start_sentinel(task_id: str, run_id: int, tdir: Path, docker_cmd: list[str]) -> int:
    """Fork a sentinel process and return its PID.

    The sentinel is a child of the executor process. If the executor crashes,
    the sentinel is reparented to init and continues running.
    """
    pid = os.fork()
    if pid == 0:
        # Child — become the sentinel
        # Decouple from parent's process group so parent signals don't affect us
        os.setsid()
        # Run sentinel (this blocks until container exits + adjudication)
        exit_code = run_sentinel(task_id, run_id, tdir, docker_cmd)
        os._exit(exit_code)

    # Parent — return sentinel PID
    log_event("sentinel_forked", task_id=task_id, run_id=run_id,
              extra={"sentinel_pid": pid})
    return pid
