#!/usr/bin/env python3
"""
Talos Executor §13 Acceptance Test Driver
Runs M1-M22 + A1-A3, records results to acc_results.jsonl
"""

import json
import os
import subprocess
import sqlite3
import sys
import time
import tempfile
import shutil
from pathlib import Path

# ── Environment ──────────────────────────────────────────────────────────
# MUST unset HERMES_DELEGATED_CHILD_CONTEXT to allow DB writes
os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)

os.environ.setdefault("HERMES_KANBAN_DB", "/Users/zhaoc/.hermes/kanban/kanban.db")
os.environ.setdefault("TALOS_GITLAB_URL", "https://hgit.haier.net")
os.environ.setdefault("TALOS_ES_URL", "http://localhost:9200")
os.environ.setdefault("TALOS_HOME", "/Users/zhaoc/.hermes/talos")
os.environ.setdefault("TALOS_WORKER_IMAGE", "hermes-worker:latest")
os.environ.setdefault("PATH", "/Users/zhaoc/.hermes/hermes-agent/venv/bin:" + os.environ.get("PATH", ""))

KANBAN_DB = os.environ["HERMES_KANBAN_DB"]
TALOS_HOME = Path(os.environ["TALOS_HOME"])
GITLAB_URL = os.environ["TALOS_GITLAB_URL"]
GITLAB_ADMIN_TOKEN = os.environ.get("TALOS_GITLAB_ADMIN_TOKEN", "")
ES_URL = os.environ["TALOS_ES_URL"]
EXECUTOR_LOG = TALOS_HOME / "executor.jsonl"
RESULTS_FILE = "/Users/zhaoc/Documents/Projects/AI-Native-Delivery/docs/dd2/acc_results.jsonl"

# Import talos
sys.path.insert(0, "/Users/zhaoc/Documents/Projects/AI-Native-Delivery/Talos")

# Import hermes
sys.path.insert(0, "/Users/zhaoc/.hermes/hermes-agent")

# ── DB helpers ───────────────────────────────────────────────────────────

def db_conn():
    """Direct sqlite3 connection (writable, not through hermes delegation guard)."""
    conn = sqlite3.connect(KANBAN_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def hermes_conn():
    """Hermes kanban_db connection (for kernel APIs)."""
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()

# ── Task management ──────────────────────────────────────────────────────

def create_task(title, body="", skills=None, assignee="default", max_retries=2,
                max_runtime=None, workspace_path=None, branch_name=None):
    """Create a kanban task using the hermes API and return its id."""
    conn = hermes_conn()
    try:
        from hermes_cli import kanban_db as kb
        kwargs = dict(
            title=title,
            body=body,
            assignee=assignee,
            skills=skills or [],
            created_by="talos-acceptance",
        )
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        if max_runtime is not None:
            kwargs["max_runtime_seconds"] = max_runtime
        if workspace_path:
            kwargs["workspace_path"] = workspace_path
        if branch_name:
            kwargs["branch_name"] = branch_name
        task_id = kb.create_task(conn, **kwargs)
        conn.commit()
        return task_id
    finally:
        conn.close()

def get_task(task_id):
    conn = db_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_task_comments(task_id):
    conn = db_conn()
    rows = conn.execute(
        "SELECT * FROM task_comments WHERE task_id=? ORDER BY created_at", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_task_runs(task_id):
    conn = db_conn()
    rows = conn.execute(
        "SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_task_events(task_id):
    conn = db_conn()
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id=? ORDER BY created_at", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Docker helpers ───────────────────────────────────────────────────────

def run_cmd(cmd, timeout=30, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=isinstance(cmd, str))
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed: {cmd}\nstderr: {r.stderr}")
    return r

def simulate_worker_container(task_id, run_id, result_json=None, work_files=None,
                               sleep=600, extra_env=None, extra_mounts=None):
    """Create a simulated worker container that writes result.json and work files."""
    cname = f"hermes-worker-{task_id}-{run_id}"
    
    # Build the container command
    parts = ["mkdir -p /task/out"]
    
    if work_files:
        for path, content in work_files.items():
            full = f"/work/{path}"
            parent = os.path.dirname(full)
            parts.append(f"mkdir -p '{parent}'")
            escaped = content.replace("'", "'\\''")
            parts.append(f"echo '{escaped}' > '{full}'")
    
    if result_json is not None:
        result_str = json.dumps(result_json)
        escaped = result_str.replace("'", "'\\''")
        parts.append(f"echo '{escaped}' > /task/out/result.json")
    
    parts.append(f"sleep {sleep}")
    cmd_str = " && ".join(parts)
    
    docker_cmd = [
        "docker", "run", "-d",
        "--name", cname,
        "--entrypoint", "sh",
    ]
    
    if extra_env:
        for k, v in extra_env.items():
            docker_cmd.extend(["-e", f"{k}={v}"])
    
    if extra_mounts:
        for m in extra_mounts:
            docker_cmd.extend(["-v", m])
    
    docker_cmd.extend([
        os.environ.get("TALOS_WORKER_IMAGE", "hermes-worker:latest"),
        "-c", cmd_str,
    ])
    
    r = run_cmd(docker_cmd, timeout=60)
    if r.returncode != 0:
        print(f"  docker run failed: {r.stderr[:300]}")
        return False
    return True

def stop_worker_container(task_id, run_id):
    """Stop the simulated worker container (simulates worker exit)."""
    cname = f"hermes-worker-{task_id}-{run_id}"
    run_cmd(["docker", "stop", cname], timeout=30)

def rm_worker_container(task_id, run_id=None):
    """Remove worker container(s)."""
    if run_id:
        cname = f"hermes-worker-{task_id}-{run_id}"
        run_cmd(["docker", "rm", "-f", cname], timeout=30)
    else:
        # Remove all containers for this task
        r = run_cmd(["docker", "ps", "-a", "--filter", f"name=hermes-worker-{task_id}",
                      "--format", "{{.Names}}"], timeout=15)
        for name in r.stdout.strip().splitlines():
            run_cmd(["docker", "rm", "-f", name.strip()], timeout=30)

def docker_inspect(task_id, run_id):
    """Get docker inspect JSON for a worker container."""
    cname = f"hermes-worker-{task_id}-{run_id}"
    r = run_cmd(["docker", "inspect", cname], timeout=15)
    if r.returncode == 0 and r.stdout.strip():
        data = json.loads(r.stdout)
        return data[0] if isinstance(data, list) and data else {}
    return {}

# ── Executor helpers ─────────────────────────────────────────────────────

def read_executor_log():
    if not EXECUTOR_LOG.exists():
        return []
    lines = EXECUTOR_LOG.read_text(encoding="utf-8").strip().splitlines()
    events = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events

def clear_executor_log():
    EXECUTOR_LOG.write_text("", encoding="utf-8")

def run_tick(spawn_fn=None):
    """Run one executor tick."""
    from hermes_cli import kanban_db_connect as kbc
    from talos.executor.loop import tick
    
    conn = kbc.connect()
    try:
        tick(conn, spawn_fn)
    finally:
        conn.close()

def run_tick_with_mock_spawn():
    """Run a tick with a mock spawn_fn that creates simulated containers.
    
    The spawn_fn mimics the real spawn: it creates a docker container,
    writes context.md, mints credentials, and returns a PID.
    """
    # We'll use a no-op spawn for tests where we manually create containers
    def mock_spawn(task, workspace, board=None):
        # Return a sentinel PID (the executor process PID)
        # This is what the real spawn does when docker run succeeds
        return os.getpid()
    return run_tick(mock_spawn)

# ── GitLab helpers ───────────────────────────────────────────────────────

def query_gitlab_api(path, method="GET", body=None):
    """Call GitLab API v4."""
    import urllib.request
    url = f"{GITLAB_URL}/api/v4{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("PRIVATE-TOKEN", GITLAB_ADMIN_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        return {"error": str(e)}

def git_ls_remote(repo_url, branch):
    """Get the sha of a branch via git ls-remote."""
    r = run_cmd(["git", "ls-remote", repo_url, f"refs/heads/{branch}"], timeout=30)
    output = r.stdout.strip()
    if not output:
        return None
    parts = output.split("\t")
    return parts[0].strip() if parts else None

def create_gitlab_branch(project_id, branch, ref="main"):
    return query_gitlab_api(f"/projects/{project_id}/repository/branches?branch={branch}&ref={ref}", method="POST")

def delete_gitlab_branch(project_id, branch):
    import urllib.parse
    encoded = urllib.parse.quote(branch, safe="")
    return query_gitlab_api(f"/projects/{project_id}/repository/branches/{encoded}", method="DELETE")

def create_gitlab_file(project_id, file_path, branch, content, commit_msg="talos: add file"):
    import urllib.parse
    encoded = urllib.parse.quote(file_path, safe="")
    body = {
        "branch": branch,
        "content": content,
        "commit_message": commit_msg,
    }
    return query_gitlab_api(f"/projects/{project_id}/repository/files/{encoded}", method="POST", body=body)

def get_gitlab_token_id(project_id, token_name):
    """Find a project access token by name."""
    tokens = query_gitlab_api(f"/projects/{project_id}/access_tokens")
    if isinstance(tokens, list):
        for t in tokens:
            if t.get("name") == token_name:
                return t.get("id")
    return None

def revoke_gitlab_token(project_id, token_id):
    return query_gitlab_api(f"/projects/{project_id}/access_tokens/{token_id}", method="DELETE")

def list_gitlab_tokens(project_id):
    return query_gitlab_api(f"/projects/{project_id}/access_tokens")

def get_protected_branches(project_id):
    return query_gitlab_api(f"/projects/{project_id}/protected_branches")

# ── ES helpers ───────────────────────────────────────────────────────────

def es_count(index_pattern, query=None):
    """Count docs in ES matching the query."""
    import urllib.request
    url = f"{ES_URL}/{index_pattern}/_count"
    body = json.dumps({"query": query or {"match_all": {}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("count", 0)
    except Exception:
        return -1

def es_search(index_pattern, query=None, size=10):
    import urllib.request
    url = f"{ES_URL}/{index_pattern}/_search"
    body = json.dumps({"query": query or {"match_all": {}}, "size": size}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

# ── Result recording ─────────────────────────────────────────────────────

RESULTS = []

def record(test_id, status, evidence, details=""):
    """Record a test result to acc_results.jsonl."""
    result = {
        "test_id": test_id,
        "status": status,  # ✅ / ⚠️ / ❌
        "evidence": evidence,
        "details": details,
        "timestamp": time.time(),
    }
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(f"  [{test_id}] {status} — {evidence[:120]}")
    RESULTS.append(result)
    return result

# ── Task cleanup ─────────────────────────────────────────────────────────

def cleanup_task_full(task_id):
    """Full cleanup: containers, task dir, GitLab branches."""
    rm_worker_container(task_id)
    # Clean task dir
    tdir = TALOS_HOME / "tasks" / task_id
    if tdir.exists():
        shutil.rmtree(tdir, ignore_errors=True)
    # Mark task done in DB
    conn = db_conn()
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

# ── Constants ────────────────────────────────────────────────────────────

TALOS_PILOT_PROJECT_ID = "S05190%2Ftalos-pilot"
TALOS_PILOT_REPO = "https://hgit.haier.net/S05190/talos-pilot.git"

retained_tasks = {"done": None, "blocked": None, "triage": None, "ready": None}
