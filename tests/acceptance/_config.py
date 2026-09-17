"""Shared configuration for acceptance tests.

All acceptance tests import from here. Environment-specific values MUST be
provided via environment variables — there are no hardcoded defaults for
GitLab URLs, project IDs, or local paths.

Required environment variables:
  TALOS_GITLAB_URL          GitLab base URL (e.g. https://gitlab.example.com)
  TALOS_PILOT_REPO          Pilot repo URL (e.g. https://gitlab.example.com/group/project.git)
  TALOS_PILOT_PROJECT_ID    URL-encoded project path (e.g. group%2Fproject)
  TALOS_ACC_RESULTS_FILE    Path to write acceptance results JSONL

Optional (with sensible defaults derived from HERMES_HOME):
  HERMES_HOME               Default: ~/.hermes
  TALOS_HOME                Default: $HERMES_HOME/talos
  HERMES_AGENT_SRC          Default: $HERMES_HOME/hermes-agent
  HERMES_VENV_PYTHON        Default: $HERMES_AGENT_SRC/venv/bin/python
  HERMES_KANBAN_DB          Default: $HERMES_HOME/kanban/kanban.db
"""
import os
import sys
import pathlib

# ── Required env vars — exit if missing ──────────────────────

_REQUIRED = {
    "TALOS_GITLAB_URL": "GitLab base URL, e.g. https://gitlab.example.com",
    "TALOS_PILOT_REPO": "Pilot repo URL, e.g. https://gitlab.example.com/group/project.git",
    "TALOS_PILOT_PROJECT_ID": "URL-encoded project path, e.g. group%2Fproject",
    "TALOS_ACC_RESULTS_FILE": "Path to write acceptance results JSONL",
}

_missing = []
for _var, _desc in _REQUIRED.items():
    if not os.environ.get(_var):
        _missing.append(f"  {_var}: {_desc}")

if _missing:
    print("Acceptance tests require the following environment variables:", file=sys.stderr)
    for _line in _missing:
        print(_line, file=sys.stderr)
    print("\nSet them in your shell or ~/.hermes/talos.env before running.", file=sys.stderr)
    sys.exit(1)

# ── Resolved values ──────────────────────────────────────────

GITLAB_URL = os.environ["TALOS_GITLAB_URL"]
PILOT_REPO = os.environ["TALOS_PILOT_REPO"]
PILOT_PROJECT_ID = os.environ["TALOS_PILOT_PROJECT_ID"]
PILOT_PID = os.environ["TALOS_PILOT_PROJECT_ID"]  # alias
RESULTS_FILE = os.environ["TALOS_ACC_RESULTS_FILE"]

# ── Optional with defaults ───────────────────────────────────

HERMES_HOME = pathlib.Path(os.environ.get("HERMES_HOME", str(pathlib.Path.home() / ".hermes")))
TALOS_HOME = pathlib.Path(os.environ.get("TALOS_HOME", str(HERMES_HOME / "talos")))
HERMES_AGENT_SRC = pathlib.Path(os.environ.get("HERMES_AGENT_SRC", str(HERMES_HOME / "hermes-agent")))
HERMES_VENV_PYTHON = os.environ.get("HERMES_VENV_PYTHON", str(HERMES_AGENT_SRC / "venv" / "bin" / "python"))
KANBAN_DB = os.environ.get("HERMES_KANBAN_DB", str(HERMES_HOME / "kanban" / "kanban.db"))

# Repo root = parent of tests/ directory (this file is in tests/acceptance/)
REPO_DIR = pathlib.Path(__file__).resolve().parent.parent.parent

# ── Apply to os.environ for downstream code ──────────────────

os.environ.setdefault("HERMES_KANBAN_DB", KANBAN_DB)
os.environ.setdefault("TALOS_GITLAB_URL", GITLAB_URL)
os.environ.setdefault("TALOS_HOME", str(TALOS_HOME))

# ── sys.path setup ───────────────────────────────────────────

_hermes_src = str(HERMES_AGENT_SRC)
_repo_src = str(REPO_DIR)
if _hermes_src not in sys.path:
    sys.path.insert(0, _hermes_src)
if _repo_src not in sys.path:
    sys.path.insert(0, _repo_src)

# Prepend venv to PATH so hermes_cli is importable
_venv_bin = str(HERMES_AGENT_SRC / "venv" / "bin")
os.environ["PATH"] = _venv_bin + ":" + os.environ.get("PATH", "")
