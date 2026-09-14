"""Collect: extract artifacts from exited containers (§2 step 8).

Before a container is removed, we ``docker cp`` out:
  - ``/task/out/result.json`` — the result file (§5.4)
  - ``/work/`` — the workspace (for declared artifact paths)
  - Container inspect JSON (for audit)

The collected bundle is stored under ``<tdir>/`` and passed to the
adjudicator.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from talos.executor.constants import CONTAINER_HH, container_name, log_event, task_dir


@dataclass
class CollectedBundle:
    """Everything collected from an exited worker container."""
    task_id: str
    run_id: int
    tdir: Path
    result_json: Optional[dict] = None
    result_raw: Optional[str] = None
    result_path: Optional[Path] = None
    workspace_path: Optional[Path] = None
    inspect: Optional[dict] = None
    exit_code: Optional[int] = None
    checker_error: Optional[str] = None

    @property
    def exited_cleanly(self) -> bool:
        return self.exit_code == 0


def _docker_cp(container: str, src: str, dst: Path, timeout: int = 120) -> bool:
    """``docker cp <container>:<src> <dst>``. Returns True on success."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["docker", "cp", f"{container}:{src}", str(dst)],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _docker_inspect(container: str) -> dict:
    """Get container inspect JSON."""
    try:
        result = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return data[0] if isinstance(data, list) and data else {}
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        # 实例：禁止空吞异常（v2.1 FIX #3）
        log_event("error", msg=f"docker inspect failed: {e}")
    return {}


def _parse_result_json(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Parse result.json. Returns (parsed_dict, raw_text_or_error)."""
    if not path.exists():
        return None, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"cannot read result.json: {e}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON in result.json: {e}"
    if not isinstance(data, dict):
        return None, f"result.json is not a JSON object"
    if data.get("schema") != 1:
        return None, f"result.json schema mismatch: expected 1, got {data.get('schema')}"
    return data, raw


def collect(task_id: str, run_id: int) -> CollectedBundle:
    """Collect all artifacts from an exited worker container.

    Steps (§2 step 8):
      1. ``docker inspect`` for audit + exit code
      2. ``docker cp`` result.json from /task/out/
      3. ``docker cp`` /work/ workspace (for declared artifact paths)
      4. Parse result.json

    The container is NOT removed here — that happens in ``reap`` after
    adjudication and archiving are complete.
    """
    t0 = time.time()
    tdir = task_dir(task_id, run_id)
    tdir.mkdir(parents=True, exist_ok=True)
    cname = container_name(task_id, run_id)

    bundle = CollectedBundle(task_id=task_id, run_id=run_id, tdir=tdir)

    # 1. Inspect
    inspect_data = _docker_inspect(cname)
    bundle.inspect = inspect_data
    state = inspect_data.get("State", {}) if inspect_data else {}
    bundle.exit_code = state.get("ExitCode")

    # 2. Copy result.json
    out_dir = tdir / "out"
    _docker_cp(cname, "/task/out/", out_dir)
    result_path = out_dir / "result.json"
    bundle.result_path = result_path if result_path.exists() else None

    # Parse result.json
    if result_path.exists():
        parsed, raw = _parse_result_json(result_path)
        bundle.result_json = parsed
        bundle.result_raw = raw
    # If result.json is missing or invalid, that's a problem for the adjudicator

    # 3. Copy workspace /work/ (Q3: works on exited containers)
    work_copy = tdir / "work"
    if not work_copy.exists():
        _docker_cp(cname, "/work/", work_copy)
    if work_copy.exists():
        bundle.workspace_path = work_copy

    # 4. Copy state.db from container HERMES_HOME (§8, M21)
    #    This is the evidence ledger used by check_verification and archived.
    state_db_dst = tdir / "state.db"
    if not state_db_dst.exists():
        _docker_cp(cname, f"{CONTAINER_HH}/state.db", state_db_dst)

    # 5. Copy trace JSONL from container HERMES_HOME/trace/ (§8, M21)
    trace_dst = tdir / "trace"
    if not trace_dst.exists():
        _docker_cp(cname, f"{CONTAINER_HH}/trace/", trace_dst)

    log_event("collected", task_id=task_id, run_id=run_id,
              duration_ms=(time.time() - t0) * 1000,
              extra={
                  "exit_code": bundle.exit_code,
                  "has_result": bundle.result_json is not None,
                  "has_workspace": bundle.workspace_path is not None,
              })

    return bundle
