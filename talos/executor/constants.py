"""Constants, paths, and event-logging for the Talos executor.

All path conventions and tunables live here so other modules stay focused.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from talos.executor.redact import redact_dict

# ── Paths ────────────────────────────────────────────────────────────────

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

#: Root for all executor state.
TALOS_HOME = Path(os.environ.get("TALOS_HOME", HOME / "talos"))

#: Per-run task directories: ``<TASKS_ROOT>/<task_id>/<run_id>/``
TASKS_ROOT = TALOS_HOME / "tasks"

#: Archived run bundles: ``<ARCHIVE_ROOT>/<task_id>/<run_id>/``
ARCHIVE_ROOT = TALOS_HOME / "archived"

#: Executor event log (§10): JSONL with one line per lifecycle event.
EXECUTOR_LOG = TALOS_HOME / "executor.jsonl"

#: Kanban DB path — no default. Must be explicitly set via HERMES_KANBAN_DB.
#  起不来是响亮的失败，起在错的库上是静默的失败（ZhaoC 方案 1）。


def _resolve_kanban_db() -> Path:
    """Resolve the kanban DB path.

    HERMES_KANBAN_DB must be set to a non-empty value.
    No fallback default — failing to start is a loud failure;
    starting on the wrong DB is a silent one.
    """
    raw = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if not raw:
        print(
            "[talos-executor] FATAL: HERMES_KANBAN_DB is not set or empty.\n"
            "  Set it in ~/.hermes/.env (CLI/gateway) and ~/.hermes/talos.env (executor).\n"
            "  Example: HERMES_KANBAN_DB=/Users/$(whoami)/.hermes/kanban/kanban.db"
        )
        sys.exit(1)
    return Path(raw)


def _check_shadow_db(home: Optional[Path] = None) -> Optional[str]:
    """Check if the hermes base default path has a kanban.db (shadow DB).

    Only alerts on the EXACT path ~/.hermes/kanban.db.
    Archive files (kanban.db.archived-*, .bak, -wal, -shm) are ignored
    so they don't cause permanent false positives.
    """
    base = home if home is not None else HOME
    shadow = base / "kanban.db"
    if shadow.is_file():
        return (
            f"影子库告警：{shadow} 仍然存在——有进程在用旧路径读写账本。"
            f"请检查是否有进程未读取 HERMES_KANBAN_DB 环境变量。"
        )
    return None


KANBAN_DB: Optional[Path] = _resolve_kanban_db()

#: GitLab base URL.
GITLAB_URL = os.environ.get("TALOS_GITLAB_URL", "https://hgit.haier.net")

#: Admin token for minting per-task credentials (managed config only).
GITLAB_ADMIN_TOKEN = os.environ.get("TALOS_GITLAB_ADMIN_TOKEN", "")

#: ES URL for trace forwarding (§10).
ES_URL = os.environ.get("TALOS_ES_URL", "")

#: Worker container name prefix.
WORKER_PREFIX = "hermes-worker-"

#: Worker image.  Default changed in v2.1 §11 (#9) to match the actual
#: image built/used by hermes-agent.
WORKER_IMAGE = os.environ.get("TALOS_WORKER_IMAGE", "hermes-worker:latest")

#: Container-internal HERMES_HOME.
CONTAINER_HH = "/tmp/hermes-worker-home"

#: Tick interval (seconds).
TICK_INTERVAL = float(os.environ.get("TALOS_TICK_INTERVAL", "5"))

#: GitLab pipeline polling interval (seconds).
PIPELINE_POLL_INTERVAL = 5

#: How long to wait for a pipeline to appear after .gitlab-ci.yml exists (seconds).
PIPELINE_APPEAR_WINDOW = 60

#: Default verification timeout (seconds).
DEFAULT_VERIFICATION_TIMEOUT = 900

#: Executor author tag for kanban comments.
EXECUTOR_AUTHOR = "talos-executor"

#: Failures before auto-block (matches kernel DEFAULT_FAILURE_LIMIT).
DEFAULT_FAILURE_LIMIT = 2

#: Max concurrent spawned containers per tick (prevents fork-bomb on large
#: ready backlogs). Override via TALOS_MAX_SPAWN env var.
import os as _os
TALOS_MAX_SPAWN = int(_os.environ.get("TALOS_MAX_SPAWN", "2"))

#: Block recurrence limit (matches kernel BLOCK_RECURRENCE_LIMIT).
BLOCK_RECURRENCE_LIMIT = 2


# ── Event log ────────────────────────────────────────────────────────────

def log_event(
    kind: str,
    *,
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
    duration_ms: Optional[float] = None,
    **extra: Any,
) -> None:
    """Append one JSONL line to ``executor.jsonl`` (§10).

    Events: dispatched / heartbeat / collected / adjudicated /
    finalized / archived / cleaned / error.
    """
    record: dict[str, Any] = {
        "ts": time.time(),
        "kind": kind,
    }
    if task_id is not None:
        record["task_id"] = task_id
    if run_id is not None:
        record["run_id"] = run_id
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 1)
    record.update(extra)
    # v2.1 §9 I6: redact sensitive values before writing to disk.
    record = redact_dict(record)
    EXECUTOR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(EXECUTOR_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


# ── Helpers ──────────────────────────────────────────────────────────────

def task_dir(task_id: str, run_id: int) -> Path:
    """Per-run task directory: ``~/.hermes/talos/tasks/<task_id>/<run_id>/``."""
    return TASKS_ROOT / task_id / str(run_id)


def archive_dir(task_id: str, run_id: int) -> Path:
    """Archive directory for one run."""
    return ARCHIVE_ROOT / task_id / str(run_id)


def container_name(task_id: str, run_id: int) -> str:
    """Docker container name: ``hermes-worker-<task_id>-<run_id>``."""
    return f"{WORKER_PREFIX}{task_id}-{run_id}"


def parse_container_name(name: str) -> Optional[tuple[str, int]]:
    """Parse ``hermes-worker-<task_id>-<run_id>`` → ``(task_id, run_id)``.

    Returns ``None`` if the name doesn't match.
    """
    if not name.startswith(WORKER_PREFIX):
        return None
    rest = name[len(WORKER_PREFIX):]
    # task_id may contain hyphens; run_id is the last segment and must be an int.
    idx = rest.rfind("-")
    if idx < 0:
        return None
    task_id = rest[:idx]
    try:
        run_id = int(rest[idx + 1:])
    except ValueError:
        return None
    if not task_id:
        return None
    return task_id, run_id
