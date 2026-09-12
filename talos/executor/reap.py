"""Reap: clean up containers and credentials after adjudication+archive (§8, §2 step 13).

Order: ``docker rm`` → revoke GitLab token → delete creds directory.
Task directory contents are preserved for 7 days (configurable) then cleaned.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from talos.executor.constants import container_name, log_event, task_dir
from talos.executor.credentials import revoke_gitlab_token


def reap_container(task_id: str, run_id: int) -> bool:
    """``docker rm -f`` the worker container (§8).

    Returns True if the container was removed or didn't exist.
    """
    cname = container_name(task_id, run_id)
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", cname],
            capture_output=True, text=True, timeout=30,
        )
        # returncode 0 = removed; non-zero with "No such container" is also fine
        ok = result.returncode == 0 or "No such container" in result.stderr
        log_event("cleaned", task_id=task_id, run_id=run_id,
                  extra={"action": "docker_rm", "container": cname, "ok": ok})
        return ok
    except subprocess.TimeoutExpired:
        log_event("error", task_id=task_id, run_id=run_id,
                  msg=f"docker rm timeout: {cname}")
        return False


def reap_credentials(task_id: str, run_id: int) -> None:
    """Revoke GitLab token and delete creds directory (§8, I6)."""
    tdir = task_dir(task_id, run_id)
    creds_dir = tdir / "creds"
    if creds_dir.exists():
        revoke_gitlab_token(task_id, run_id, creds_dir)
        # Remove creds dir contents
        try:
            shutil.rmtree(creds_dir)
        except OSError:
            pass


def reap_task_dir(task_id: str, run_id: int, *, retain_days: int = 7) -> None:
    """Remove task directory if older than *retain_days* (§8).

    Only removes the specific run directory; other runs of the same task
    are preserved.
    """
    tdir = task_dir(task_id, run_id)
    if not tdir.exists():
        return

    # Check age of the directory
    try:
        mtime = tdir.stat().st_mtime
        age = time.time() - mtime
        if age < retain_days * 86400:
            return
        shutil.rmtree(tdir)
        log_event("cleaned", task_id=task_id, run_id=run_id,
                  extra={"action": "task_dir_removed", "age_days": age / 86400})
    except OSError:
        pass


def reap(task_id: str, run_id: int, *, retain_days: int = 7) -> None:
    """Full cleanup: write adjudicated marker → container → credentials → old task dirs (§8).

    Order matters: container must be removed AFTER collect+adjudicate+archive
    have finished (I8: adjudication before reaping). The adjudicated marker
    is written first to release the sentinel.
    """
    t0 = time.time()
    # Write adjudicated marker so the sentinel can exit (I8)
    tdir = task_dir(task_id, run_id)
    marker = tdir / "adjudicated"
    try:
        marker.touch()
    except OSError:
        pass
    reap_container(task_id, run_id)
    reap_credentials(task_id, run_id)
    reap_task_dir(task_id, run_id, retain_days=retain_days)
    log_event("cleaned", task_id=task_id, run_id=run_id,
              duration_ms=(time.time() - t0) * 1000,
              extra={"action": "full_reap"})


def list_exited_containers() -> list[dict]:
    """List exited worker containers (§3 main loop).

    Returns a list of ``{name, task_id, run_id}`` dicts.
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "name=hermes-worker-",
                "--filter", "status=exited",
                "--format", "{{.Names}}",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return []

    from talos.executor.constants import parse_container_name
    containers: list[dict] = []
    for name in result.stdout.strip().splitlines():
        name = name.strip()
        if not name:
            continue
        parsed = parse_container_name(name)
        if parsed:
            task_id, run_id = parsed
            containers.append({
                "name": name,
                "task_id": task_id,
                "run_id": run_id,
            })
    return containers


def list_running_containers() -> list[dict]:
    """List running worker containers (for heartbeat)."""
    try:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", "name=hermes-worker-",
                "--format", "{{.Names}}",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        return []

    from talos.executor.constants import parse_container_name
    containers: list[dict] = []
    for name in result.stdout.strip().splitlines():
        name = name.strip()
        if not name:
            continue
        parsed = parse_container_name(name)
        if parsed:
            task_id, run_id = parsed
            containers.append({
                "name": name,
                "task_id": task_id,
                "run_id": run_id,
            })
    return containers


def reap_orphans() -> None:
    """Kill containers whose sentinel PID is dead (§3 reap_orphans).

    For each running worker container, check if the corresponding sentinel
    PID is still alive. If not, docker kill the container.
    """
    import os
    from talos.executor.constants import parse_container_name, task_dir

    containers = list_running_containers()
    for ctr in containers:
        task_id = ctr["task_id"]
        run_id = ctr["run_id"]

        # Check if sentinel PID is alive by looking at the task's worker_pid
        # in the kanban DB. If the kernel already cleaned the PID (set to None),
        # the sentinel is dead and we should kill the container.
        try:
            from hermes_cli import kanban_db_connect as kbc
            with kbc.connect() as conn:
                row = conn.execute(
                    "SELECT worker_pid FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    # Task not found — kill orphan container
                    _kill_container(ctr["name"], task_id, run_id, reason="task_not_found")
                    continue

                worker_pid = row["worker_pid"] if "worker_pid" in row.keys() else None
                if worker_pid is None:
                    # Sentinel PID cleared by kernel — orphan
                    _kill_container(ctr["name"], task_id, run_id, reason="sentinel_dead")
                    continue

                # Check if PID is alive
                try:
                    os.kill(int(worker_pid), 0)
                except (ProcessLookupError, ValueError, PermissionError):
                    # PID is dead — orphan
                    _kill_container(ctr["name"], task_id, run_id, reason="sentinel_dead")
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"reap_orphans check failed: {e}")


def _kill_container(cname: str, task_id: str, run_id: int, reason: str) -> None:
    """Kill a container and log."""
    try:
        subprocess.run(
            ["docker", "kill", cname],
            capture_output=True, text=True, timeout=10,
        )
        log_event("cleaned", task_id=task_id, run_id=run_id,
                  extra={"action": "reap_orphan", "container": cname, "reason": reason})
    except Exception as e:
        log_event("error", task_id=task_id, run_id=run_id,
                  msg=f"reap_orphan kill failed: {e}")
