"""path_protect — prevent workers from writing to protected paths.

``pre_tool_call`` hook that intercepts write operations targeting protected
files and directories.  The primary use case (A3) is preventing a worker
from modifying ``.gitlab-ci.yml`` to make tests always pass.

Protected paths are sourced from the ``TALOS_PROTECTED_PATHS`` environment
variable (newline-separated glob patterns).  The executor sets this at spawn
time.  Defaults cover common CI configuration files.

This is defence-in-depth alongside the read-only mounts (§5.1): the worker
workspace (``/work``) is a fresh clone and thus writable, but CI config
files inside it must not be tampered with — the adjudicator (§6) checks the
GitLab pipeline, and a modified ``.gitlab-ci.yml`` could neuter that check.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from fnmatch import fnmatch
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Log file for audit trail.
_LOG_FILE = os.environ.get(
    "TALOS_PATH_PROTECT_LOG",
    "/tmp/hermes-worker-home/trace/path_protect.jsonl",
)

#: Default protected path patterns (relative to workspace root or absolute).
#: These are CI / pipeline config files that could neuter verification.
_DEFAULT_PROTECTED_PATTERNS = [
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "Jenkinsfile",
    ".circleci/config.yml",
    "azure-pipelines.yml",
    ".travis.yml",
    "bitbucket-pipelines.yml",
]

#: Arg keys that commonly carry file paths in write/edit tools.
_PATH_ARG_KEYS = (
    "path",
    "file_path",
    "filename",
    "file",
    "target",
    "dest",
    "destination",
)

#: Tools that write files.
_WRITE_TOOLS = frozenset({
    "write_file",
    "patch",
    "edit_file",
    "execute_code",
    "python",
    "python3",
})

#: Tools that execute shell commands (may write via redirection).
_SHELL_TOOLS = frozenset({
    "terminal",
    "bash",
    "shell",
})


def _log(event: dict) -> None:
    """Append one JSONL line to the path_protect audit log (best-effort).

    安全证据：审计日志写不进去时同时往 stderr 打一条，
    确保拦截信号不会因磁盘/权限问题被静默吞掉。
    """
    try:
        entry = {"ts": time.time(), **event}
        os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        import sys
        print(f"[path_protect] AUDIT LOG WRITE FAILED: {e} | event={event}", file=sys.stderr)


def _protected_patterns() -> list[str]:
    """Load protected path patterns from env or defaults."""
    raw = os.environ.get("TALOS_PROTECTED_PATHS", "")
    if raw.strip():
        return [line.strip() for line in raw.splitlines() if line.strip()]
    return list(_DEFAULT_PROTECTED_PATTERNS)


def _normalize_path(path: str, workspace: str) -> str:
    """Normalize a path for matching.

    Relative paths are resolved against the workspace root so that patterns
    like ``.gitlab-ci.yml`` match ``/work/.gitlab-ci.yml``.
    """
    path = os.path.expanduser(str(path))
    if not os.path.isabs(path):
        path = os.path.join(workspace, path)
    # realpath is avoided here because the file may not exist yet (write_file
    # creates it).  normpath collapses ``..`` etc.
    return os.path.normpath(path)


def _is_protected(path: str, patterns: list[str], workspace: str) -> bool:
    """Check whether *path* matches any protected pattern.

    Matching is done on both the full normalized path and the path relative
    to the workspace root, so glob patterns work regardless of whether the
    worker uses absolute or relative paths.
    """
    norm = _normalize_path(path, workspace)
    # Relative-to-workspace form for glob matching.
    rel = os.path.relpath(norm, workspace) if workspace else norm
    for pattern in patterns:
        # Match against the basename (handles bare filenames like
        # ``.gitlab-ci.yml`` anywhere in the tree).
        if os.sep not in pattern and not any(c in pattern for c in "*?["):
            if os.path.basename(norm) == pattern:
                return True
        # Glob match against relative path.
        if fnmatch(rel, pattern):
            return True
        # Glob match against full path.
        if fnmatch(norm, f"*{pattern}"):
            return True
    return False


def _candidate_paths(tool_name: str, args: Dict[str, Any]) -> List[str]:
    """Extract candidate file paths from tool arguments.

    - ``write_file`` / ``patch`` / ``edit_file``: known path arg keys.
    - ``terminal`` / ``bash`` / ``shell``: shell-parse the command for
      paths and redirection targets.
    - ``execute_code`` / ``python``: extract string literals containing
      path separators (not shlex — Python code isn't shell).
    """
    out: List[str] = []

    if tool_name in _SHELL_TOOLS:
        cmd = args.get("command") or args.get("code") or args.get("script") or ""
        if not isinstance(cmd, str) or not cmd:
            return out
        # Shell tokenise and pick path-like tokens.
        try:
            tokens = shlex.split(cmd, posix=True)
        except ValueError:
            tokens = cmd.split()
        for t in tokens:
            if os.sep in t and not t.startswith("-"):
                out.append(t)
        # Redirection targets: >file, >>file
        for m in re.finditer(r">>?\s*([^\s;|&]+)", cmd):
            out.append(m.group(1))
        return out

    if tool_name in ("execute_code", "python", "python3"):
        code = args.get("code") or args.get("script") or args.get("command") or ""
        if not isinstance(code, str):
            return out
        # Extract quoted string literals that contain path separators.
        for m in re.finditer(r"""(['"])((?:(?!\1).)+?)\1""", code):
            lit = m.group(2)
            if os.sep in lit:
                out.append(lit)
        return out

    # write_file / patch / edit_file and similar: known path keys.
    for k in _PATH_ARG_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v:
            out.append(v)
    # Batch edit tools: edits: [{path: ...}, ...]
    for v in args.values():
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    for k in _PATH_ARG_KEYS:
                        val = item.get(k)
                        if isinstance(val, str) and val:
                            out.append(val)
    return out


def _workspace() -> str:
    """Container workspace path (set by executor at spawn, §5.2)."""
    return os.environ.get("HERMES_TASK_WORKSPACE", "/work")


def _pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Block writes to protected paths.

    Returns ``{"action": "block", "message": ...}`` when a write tool or
    shell command targets a protected path; ``None`` to allow otherwise.
    """
    if tool_name not in _WRITE_TOOLS and tool_name not in _SHELL_TOOLS:
        return None

    args = args if isinstance(args, dict) else {}
    patterns = _protected_patterns()
    if not patterns:
        return None

    workspace = _workspace()
    candidates = _candidate_paths(tool_name, args)

    for cand in candidates:
        if _is_protected(cand, patterns, workspace):
            _log({
                "event": "path_write_blocked",
                "task_id": task_id,
                "tool": tool_name,
                "path": cand,
            })
            return {
                "action": "block",
                "message": (
                    f"[talos/path_protect] Path '{cand}' is protected "
                    f"(matches CI/pipeline config pattern). Workers cannot "
                    f"modify CI configuration files — this would compromise "
                    f"the verification pipeline. If this is a legitimate "
                    f"change, request it through the platform configuration "
                    f"process."
                ),
            }

    return None
