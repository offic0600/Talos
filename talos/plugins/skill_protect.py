"""skill_protect — prevent workers from modifying managed skills.

``pre_tool_call`` hook that intercepts ``skill_manage`` write operations
(create, patch, delete, write_file, remove_file) when the target skill is
in the protected set.  Read-only operations (view, list) are always allowed.

This is the I3 / §5.1 container-side skill protection.  In the first batch
this was part of the monolithic ``completion-contract`` plugin; in batch 2
it is split out as a standalone plugin with **no** ``kanban_complete``
interception (adjudication moved to the executor, §6).

The protected skill list is sourced from the ``TALOS_PROTECTED_SKILLS``
environment variable (comma-separated), set by the executor at spawn time
based on the task's declared ``skills``.  In the container, all skills under
``$HH/skills/`` are mounted read-only (§5.1), but this hook provides a
defence-in-depth: even if the worker somehow obtains the ``skill_manage``
tool, it cannot modify the contract that governs its own adjudication.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Actions that modify skill state — blocked on protected skills.
_WRITE_ACTIONS = frozenset({
    "create",
    "patch",
    "delete",
    "write_file",
    "remove_file",
})

#: Log file for audit trail (alongside trace).
_LOG_FILE = os.environ.get(
    "TALOS_SKILL_PROTECT_LOG",
    "/tmp/hermes-worker-home/trace/skill_protect.jsonl",
)


def _protected_skills() -> set[str]:
    """Parse the comma-separated protected skill list from the environment.

    The executor sets ``TALOS_PROTECTED_SKILLS`` at spawn time.  Falls back
    to an empty set (no protection) if unset — the read-only mount (§5.1)
    is the primary defence; this hook is defence-in-depth.
    """
    raw = os.environ.get("TALOS_PROTECTED_SKILLS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _log(event: dict) -> None:
    """Append one JSONL line to the skill_protect audit log (best-effort)."""
    try:
        entry = {"ts": time.time(), **event}
        os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never affect the gate


def _parse_operations(args: Dict[str, Any]) -> List[Tuple[int, str, str]]:
    """Extract ``(op_index, skill_name, action)`` tuples from skill_manage args.

    The ``skill_manage`` tool accepts two shapes (v5 lesson from dd1):
    - Flat: ``{"name": "...", "action": "..."}``
    - Batch: ``{"operations": [{"name": "...", "action": "..."}, ...]}``

    Both are parsed; all calls are logged for audit.
    """
    ops_raw = args.get("operations")
    if isinstance(ops_raw, list):
        result = []
        for i, op in enumerate(ops_raw):
            if isinstance(op, dict):
                name = str(op.get("name") or op.get("skill") or "")
                action = str(op.get("action") or "")
                result.append((i, name, action))
        return result
    # Flat shape
    name = str(args.get("name") or args.get("skill") or "")
    action = str(args.get("action") or "")
    if name or action:
        return [(0, name, action)]
    return []


def _pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Block write operations on protected skills.

    Returns ``{"action": "block", "message": ...}`` when a write operation
    targets a protected skill; ``None`` to allow otherwise.  All
    ``skill_manage`` calls are logged for audit regardless of outcome.
    """
    if tool_name != "skill_manage":
        return None

    args = args if isinstance(args, dict) else {}
    protected = _protected_skills()
    targets = _parse_operations(args)

    # Audit log: all skill_manage calls are recorded (v5 lesson: without
    # this, operations-shaped calls are invisible in logs).
    _log({
        "event": "skill_manage_call",
        "task_id": task_id,
        "ops": [
            {"i": i, "skill": s, "action": a} for i, s, a in targets
        ],
    })

    for op_index, skill_name, action in targets:
        if action in _WRITE_ACTIONS and skill_name in protected:
            _log({
                "event": "skill_manage_blocked",
                "task_id": task_id,
                "skill": skill_name,
                "action": action,
                "op_index": op_index,
            })
            return {
                "action": "block",
                "message": (
                    f"[talos/skill_protect] Skill '{skill_name}' is managed "
                    f"and cannot be modified via skill_manage (operation "
                    f"#{op_index}: {action}). The skill contract is maintained "
                    f"by the platform; if you need a change, request it through "
                    f"the configuration process."
                ),
            }

    return None
