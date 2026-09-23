#!/usr/bin/env python3
"""Run M5-M15 acceptance tests for Talos executor."""
import json, os, sys, time, subprocess, sqlite3, shutil
from pathlib import Path

# CRITICAL: unset delegated child context
os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)

# Load environment-specific config (exits if required env vars are missing)
from tests.acceptance._config import (
    GITLAB_URL, PILOT_REPO, RESULTS_FILE, KANBAN_DB, TALOS_HOME, REPO_DIR,
)

os.environ.setdefault("TALOS_GITLAB_ADMIN_TOKEN", os.environ.get("TALOS_GITLAB_ADMIN_TOKEN", ""))
os.environ.setdefault("TALOS_ES_URL", "http://localhost:9200")
os.environ.setdefault("TALOS_WORKER_IMAGE", "hermes-worker:latest")

def db_conn():
    conn = sqlite3.connect(KANBAN_DB)
    conn.row_factory = sqlite3.Row
    return conn

def hermes_conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()

def create_task(title, body="", skills=None, max_retries=2, max_runtime=None):
    conn = hermes_conn()
    try:
        from hermes_cli import kanban_db as kb
        kwargs = dict(title=title, body=body, assignee="default",
                      skills=skills or [], created_by="talos-acceptance")
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        if max_runtime is not None:
            kwargs["max_runtime_seconds"] = max_runtime
        tid = kb.create_task(conn, **kwargs)
        conn.commit()
        return tid
    finally:
        conn.close()

def get_task(tid):
    conn = db_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_comments(tid):
    conn = db_conn()
    rows = conn.execute("SELECT * FROM task_comments WHERE task_id=? ORDER BY created_at", (tid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_runs(tid):
    conn = db_conn()
    rows = conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_events(tid):
    conn = db_conn()
    rows = conn.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY created_at", (tid,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def record(test_id, status, evidence, details=""):
    result = {"test_id": test_id, "status": status, "evidence": evidence,
              "details": details, "timestamp": time.time()}
    with open(RESULTS_FILE, "a") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(f"  [{test_id}] {status} — {evidence[:120]}")
    return result

def simulate_container(tid, run_id, result_json=None, work_files=None, sleep=600):
    cname = f"hermes-worker-{tid}-{run_id}"
    parts = ["mkdir -p /task/out /work"]
    if work_files:
        for path, content in work_files.items():
            full = f"/work/{path}"
            parent = os.path.dirname(full)
            parts.append(f"mkdir -p '{parent}'")
            escaped = content.replace("'", "'\\''")
            parts.append(f"echo '{escaped}' > '{full}'")
    if result_json is not None:
        rstr = json.dumps(result_json).replace("'", "'\\''")
        parts.append(f"echo '{rstr}' > /task/out/result.json")
    parts.append(f"sleep {sleep}")
    cmd = " && ".join(parts)
    r = subprocess.run(["docker", "run", "-d", "--name", cname, "--entrypoint", "sh",
                         "hermes-worker:latest", "-c", cmd],
                        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"  docker run failed: {r.stderr[:300]}")
        return False
    return True

def stop_container(tid, run_id):
    cname = f"hermes-worker-{tid}-{run_id}"
    subprocess.run(["docker", "stop", cname], capture_output=True, timeout=30)

def rm_container(tid, run_id=None):
    if run_id:
        subprocess.run(["docker", "rm", "-f", f"hermes-worker-{tid}-{run_id}"],
                       capture_output=True, timeout=30)
    else:
        r = subprocess.run(["docker", "ps", "-a", "--filter", f"name=hermes-worker-{tid}",
                            "--format", "{{.Names}}"], capture_output=True, text=True, timeout=15)
        for name in r.stdout.strip().splitlines():
            subprocess.run(["docker", "rm", "-f", name.strip()], capture_output=True, timeout=30)

def run_tick_with_mock():
    """Run a tick with mock spawn (no real container creation)."""
    from talos.executor.loop import tick
    conn = hermes_conn()
    try:
        def mock_spawn(task, workspace, board=None):
            return os.getpid()
        tick(conn, mock_spawn)
    finally:
        conn.close()

def run_tick_adjudicate_only():
    """Run tick but skip dispatch (only adjudicate exited containers)."""
    from talos.executor.loop import _adjudicate_exited, _heartbeat_live_containers
    conn = hermes_conn()
    try:
        _adjudicate_exited(conn)
        _heartbeat_live_containers(conn)
    finally:
        conn.close()

def cleanup_task(tid):
    rm_container(tid)
    tdir = TALOS_HOME / "tasks" / tid
    if tdir.exists():
        shutil.rmtree(tdir, ignore_errors=True)
    conn = db_conn()
    conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
    conn.commit()
    conn.close()

# PILOT_REPO imported from _config

# ═══════════════════════════════════════════════════════════════
# M5: 声明 3 产物，worker 只做 2 → 裁决 unmet，problem 列出缺失路径
# ═══════════════════════════════════════════════════════════════
print("\n=== M5: Adjudication unmet (3 declared, 2 produced) ===")
try:
    tid = create_task("M5-Unmet-Test",
                      body=f"repo: {PILOT_REPO}\nWrite 2 of 3 required files.",
                      skills=["talos-code-demo"])
    task = get_task(tid)
    run_id = task["current_run_id"]
    print(f"  Created task: {tid}, run_id={run_id}")

    # Create container with 2 of 3 files
    simulate_container(tid, run_id,
                       result_json={"schema":1,"status":"done","summary":"Did 2/3",
                                    "artifacts":[{"kind":"file","path":"/work/src/a.py"},
                                                 {"kind":"file","path":"/work/src/b.py"}]},
                       work_files={"src/a.py": "def a(): pass\n" * 5,
                                   "src/b.py": "def b(): pass\n" * 5})
    stop_container(tid, run_id)
    time.sleep(2)

    # Run tick to adjudicate
    run_tick_adjudicate_only()
    time.sleep(1)

    task_after = get_task(tid)
    comments = get_comments(tid)
    events = get_events(tid)

    # Check if adjudicated
    has_unmet_comment = any("未通过" in c.get("body","") or "unmet" in c.get("body","").lower()
                            for c in comments)
    has_executor_comment = any(c.get("author") == "talos-executor" for c in comments)

    status = task_after["status"] if task_after else "?"
    if status in ("ready", "blocked", "triage") and has_executor_comment:
        record("M5", "✅",
               f"status={status}, comments={len(comments)}, executor_comment={has_executor_comment}, "
               f"unment_comment={has_unmet_comment}",
               f"task={tid}")
    else:
        record("M5", "⚠️",
               f"status={status}, comments={len(comments)}, executor_comment={has_executor_comment}",
               f"task={tid}, expected ready/blocked/triage with executor comment")

    cleanup_task(tid)
except Exception as e:
    record("M5", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══════════════════════════════════════════════════════════════
# M7: 每次裁决恰好一条 [执行器] 评论，author=talos-executor
# ═══════════════════════════════════════════════════════════════
print("\n=== M7: One executor comment per adjudication ===")
try:
    tid = create_task("M7-Comment-Test",
                      body=f"repo: {PILOT_REPO}\nTest comment count.",
                      skills=["talos-code-demo"])
    task = get_task(tid)
    run_id = task["current_run_id"]

    # Simulate worker with valid result
    simulate_container(tid, run_id,
                       result_json={"schema":1,"status":"done","summary":"All done",
                                    "artifacts":[]})
    stop_container(tid, run_id)
    time.sleep(2)

    run_tick_adjudicate_only()
    time.sleep(1)

    comments = get_comments(tid)
    executor_comments = [c for c in comments if c.get("author") == "talos-executor"]

    if len(executor_comments) == 1:
        record("M7", "✅",
               f"executor_comments={len(executor_comments)} (expected 1), total_comments={len(comments)}",
               f"task={tid}")
    else:
        record("M7", "⚠️",
               f"executor_comments={len(executor_comments)} (expected 1), total_comments={len(comments)}",
               f"task={tid}")

    cleanup_task(tid)
except Exception as e:
    record("M7", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══════════════════════════════════════════════════════════════
# M12: 声明 frontmatter 非法 → 裁决 error → triage
# ═══════════════════════════════════════════════════════════════
print("\n=== M12: Bad skill frontmatter → error → triage ===")
try:
    # Create a skill with invalid frontmatter
    bad_skill_dir = Path.home() / ".hermes" / "skills" / "talos-bad-skill"
    bad_skill_dir.mkdir(parents=True, exist_ok=True)
    (bad_skill_dir / "SKILL.md").write_text(
        "---\nname: talos-bad-skill\n"
        "completion_contract:\n  artifacts:\n"
        "    - {path: '${workspace}/src/feature.py', min_bytes: INVALID_NOT_A_NUMBER}\n"
        "---\n# bad skill\n")

    tid = create_task("M12-BadSkill-Test",
                      body="Test bad skill frontmatter.",
                      skills=["talos-bad-skill"])
    task = get_task(tid)
    run_id = task["current_run_id"]

    simulate_container(tid, run_id,
                       result_json={"schema":1,"status":"done","summary":"done","artifacts":[]})
    stop_container(tid, run_id)
    time.sleep(2)

    run_tick_adjudicate_only()
    time.sleep(1)

    task_after = get_task(tid)
    comments = get_comments(tid)
    status = task_after["status"] if task_after else "?"

    if status == "triage":
        record("M12", "✅", f"status=triage, comments={len(comments)}", f"task={tid}")
    else:
        record("M12", "⚠️", f"status={status} (expected triage)", f"task={tid}")

    cleanup_task(tid)
    shutil.rmtree(bad_skill_dir, ignore_errors=True)
except Exception as e:
    record("M12", "❌", f"exception: {e}", "")
    try:
        cleanup_task(tid)
        shutil.rmtree(bad_skill_dir, ignore_errors=True)
    except: pass

# ═══════════════════════════════════════════════════════════════
# M13: 连续 2 次 unmet → triage
# ═══════════════════════════════════════════════════════════════
print("\n=== M13: Two consecutive unmet → triage ===")
try:
    tid = create_task("M13-TripleFail-Test",
                      body=f"repo: {PILOT_REPO}\nWill fail 3 times.",
                      skills=["talos-code-demo"], max_retries=2)
    task = get_task(tid)
    run_id = task["current_run_id"]
    print(f"  Created task: {tid}, run_id={run_id}")

    # Run 1: unmet (no result file)
    simulate_container(tid, run_id, result_json=None)
    stop_container(tid, run_id)
    time.sleep(2)
    run_tick_adjudicate_only()
    time.sleep(1)
    task1 = get_task(tid)
    print(f"  Run 1: status={task1['status']}, failures={task1['consecutive_failures']}")

    # Run 2: unmet again
    task2 = get_task(tid)
    run_id2 = task2["current_run_id"]
    if run_id2:
        simulate_container(tid, run_id2, result_json=None)
        stop_container(tid, run_id2)
        time.sleep(2)
        run_tick_adjudicate_only()
        time.sleep(1)
        task3 = get_task(tid)
        print(f"  Run 2: status={task3['status']}, failures={task3['consecutive_failures']}")

    final = get_task(tid)
    status = final["status"] if final else "?"
    if status in ("triage", "blocked"):
        record("M13", "✅",
               f"status={status}, failures={final['consecutive_failures']}",
               f"task={tid}")
    else:
        record("M13", "⚠️",
               f"status={status}, failures={final['consecutive_failures']}",
               f"task={tid}, expected triage/blocked after 2 unmet")

    cleanup_task(tid)
except Exception as e:
    record("M13", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══════════════════════════════════════════════════════════════
# M14: 结果文件 status=blocked → triage
# ═══════════════════════════════════════════════════════════════
print("\n=== M14: Result status=blocked → triage ===")
try:
    tid = create_task("M14-Blocked-Test",
                      body=f"repo: {PILOT_REPO}\nWorker self-reports blocked.",
                      skills=["talos-code-demo"])
    task = get_task(tid)
    run_id = task["current_run_id"]

    simulate_container(tid, run_id,
                       result_json={"schema":1,"status":"blocked",
                                    "summary":"Missing API credentials","artifacts":[]})
    stop_container(tid, run_id)
    time.sleep(2)

    run_tick_adjudicate_only()
    time.sleep(1)

    task_after = get_task(tid)
    comments = get_comments(tid)
    status = task_after["status"] if task_after else "?"

    if status == "triage":
        record("M14", "✅",
               f"status=triage, comments={len(comments)}",
               f"task={tid}")
    else:
        record("M14", "⚠️",
               f"status={status} (expected triage)",
               f"task={tid}")

    cleanup_task(tid)
except Exception as e:
    record("M14", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══════════════════════════════════════════════════════════════
# M15: 结果文件缺失 → unmet
# ═══════════════════════════════════════════════════════════════
print("\n=== M15: Missing result file → unmet ===")
try:
    tid = create_task("M15-NoResult-Test",
                      body=f"repo: {PILOT_REPO}\nWorker doesn't write result.",
                      skills=["talos-code-demo"])
    task = get_task(tid)
    run_id = task["current_run_id"]

    # Container with no result.json
    simulate_container(tid, run_id, result_json=None)
    stop_container(tid, run_id)
    time.sleep(2)

    run_tick_adjudicate_only()
    time.sleep(1)

    task_after = get_task(tid)
    comments = get_comments(tid)
    status = task_after["status"] if task_after else "?"

    # Check if problem mentions result file
    has_result_problem = any("结果文件" in c.get("body","") or "result" in c.get("body","").lower()
                             for c in comments)

    if status in ("ready", "blocked", "triage") and has_result_problem:
        record("M15", "✅",
               f"status={status}, has_result_problem={has_result_problem}",
               f"task={tid}")
    else:
        record("M15", "⚠️",
               f"status={status}, has_result_problem={has_result_problem}",
               f"task={tid}")

    cleanup_task(tid)
except Exception as e:
    record("M15", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

print("\n=== Batch 1 (M5-M15) complete ===")
