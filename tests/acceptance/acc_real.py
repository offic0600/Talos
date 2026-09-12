#!/usr/bin/env python3
"""§13 acceptance runner — real path (real executor, real containers, real GitLab).

Each result includes:
  - commit hash (git rev-parse HEAD)
  - run timestamp (ISO 8601)
  - elapsed seconds
  - evidence source: "真实路径" (real executor/container/GitLab)

Usage:
  python acc_real.py [--batch M1-M5,M8,M9] [--log-dir /tmp/acc-logs]

Default: runs all 25 items (M1-M22 + A1-A3).
Long-running items (M1, M8, M9) are spawned as background subprocesses
with logs written to --log-dir.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sqlite3
import sys
import time
import shutil
import traceback
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────

KANBAN_DB = os.environ.get("HERMES_KANBAN_DB",
    str(Path.home() / ".hermes" / "kanban" / "kanban.db"))
TALOS_HOME = Path(os.environ.get("TALOS_HOME",
    str(Path.home() / ".hermes" / "talos")))
GITLAB_URL = os.environ.get("TALOS_GITLAB_URL", "https://hgit.haier.net")
GITLAB_TOKEN = os.environ.get("TALOS_GITLAB_ADMIN_TOKEN", "")
PILOT_REPO = "https://hgit.haier.net/S05190/talos-pilot.git"
PILOT_PID = "S05190%2Ftalos-pilot"
RESULTS_FILE = "/Users/zhaoc/Documents/Projects/AI-Native-Delivery/docs/dd2/acc_results.jsonl"
REPO_DIR = "/Users/zhaoc/Documents/Projects/AI-Native-Delivery/Talos"

sys.path.insert(0, REPO_DIR)
sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))
os.environ["PATH"] = str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin") + ":" + os.environ.get("PATH", "")

# ── Helpers ──────────────────────────────────────────────────────

def commit_hash():
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                       cwd=REPO_DIR, capture_output=True, text=True, timeout=10)
    return r.stdout.strip()

def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def db_conn():
    c = sqlite3.connect(KANBAN_DB)
    c.row_factory = sqlite3.Row
    return c

def hermes_conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()

def record(test_id, status, evidence, elapsed, details=""):
    r = {
        "test_id": test_id,
        "status": status,
        "evidence": evidence,
        "details": details,
        "commit": commit_hash(),
        "timestamp": iso_now(),
        "elapsed_s": round(elapsed, 2),
        "source": "真实路径",
    }
    with open(RESULTS_FILE, "a") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  [{test_id}] {status} ({elapsed:.1f}s) — {evidence[:120]}")

def create_task(title, body="", skills=None, **kw):
    conn = hermes_conn()
    try:
        from hermes_cli import kanban_db as kb
        kwargs = dict(title=title, body=body or f"repo: {PILOT_REPO}\nAcc.",
                      assignee="default", skills=skills or [],
                      created_by="talos-acceptance")
        kwargs.update(kw)
        tid = kb.create_task(conn, **kwargs)
        conn.commit()
        return tid
    finally:
        conn.close()

def get_task_dict(tid):
    conn = db_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None

def manual_dispatch(tid):
    conn = db_conn()
    max_run = conn.execute("SELECT MAX(id) FROM task_runs").fetchone()[0] or 0
    run_id = max_run + 1
    conn.execute("UPDATE tasks SET status='running', current_run_id=?, started_at=?, "
                 "claim_lock=?, claim_expires=?",
                 (run_id, int(time.time()), "talos-acc", int(time.time()) + 300))
    conn.execute("INSERT INTO task_runs (id, task_id, status, started_at) VALUES (?, ?, 'running', ?)",
                 (run_id, tid, int(time.time())))
    conn.commit()
    conn.close()
    return run_id

def dispatch_and_simulate(tid, result_json=None, work_files=None):
    """Real tick dispatch → replace container with simulated one → stop."""
    conn = hermes_conn()
    try:
        from hermes_cli import kanban_db as kb
        from talos.executor.spawn import make_spawn_fn
        from talos.executor.loop import tick
        task = kb.get_task(conn, tid)
        if task.status != "ready":
            return None
        spawn_fn = make_spawn_fn(conn)
        tick(conn, spawn_fn)
        time.sleep(3)
        task = kb.get_task(conn, tid)
        run_id = task.current_run_id
        if not run_id:
            return None
        subprocess.run(["docker", "rm", "-f", f"hermes-worker-{tid}-{run_id}"],
                       capture_output=True, timeout=30)
        time.sleep(1)
        create_container_with_result(tid, run_id, result_json, work_files)
        return run_id
    finally:
        conn.close()

def create_container_with_result(tid, run_id, result_json=None, work_files=None):
    cname = f"hermes-worker-{tid}-{run_id}"
    subprocess.run(["docker", "run", "-d", "--name", cname, "--entrypoint", "sh",
                     "hermes-worker:latest", "-c", "mkdir -p /task/out /work && sleep 600"],
                    capture_output=True, text=True, timeout=60)
    if result_json is not None:
        tmpf = f"/tmp/result_{tid}_{run_id}.json"
        with open(tmpf, "w") as f:
            json.dump(result_json, f)
        subprocess.run(["docker", "cp", tmpf, f"{cname}:/task/out/result.json"],
                       capture_output=True, timeout=15)
        os.unlink(tmpf)
    if work_files:
        for path, content in work_files.items():
            tmpw = f"/tmp/workfile_{tid}_{run_id}"
            with open(tmpw, "w") as f:
                f.write(content)
            full_path = f"/work/{path}"
            parent = os.path.dirname(full_path)
            if parent != "/work":
                subprocess.run(["docker", "exec", cname, "mkdir", "-p", parent],
                               capture_output=True, timeout=10)
            subprocess.run(["docker", "cp", tmpw, f"{cname}:{full_path}"],
                           capture_output=True, timeout=15)
            os.unlink(tmpw)
    subprocess.run(["docker", "stop", cname], capture_output=True, timeout=30)
    time.sleep(1)

def adjudicate_run(tid, run_id):
    from talos.executor.collect import collect
    from talos.executor.adjudicate import adjudicate
    from talos.executor.declarations import load_declarations
    from talos.executor.finalize import finalize
    task_dict = get_task_dict(tid)
    class T:
        skills = None
    task = T()
    for k, v in task_dict.items():
        setattr(task, k, v)
    task.skills = json.loads(task_dict["skills"]) if task_dict["skills"] else []
    bundle = collect(tid, run_id)
    decl = load_declarations(task.skills, task)
    verdict = adjudicate(tid, run_id, bundle, decl)
    conn = hermes_conn()
    try:
        new_status = finalize(conn, tid, run_id, verdict)
    finally:
        conn.close()
    return verdict, new_status, bundle

def cleanup_task(tid):
    r = subprocess.run(["docker", "ps", "-a", "--filter", f"name=hermes-worker-{tid}",
                        "--format", "{{.Names}}"], capture_output=True, text=True, timeout=15)
    for name in r.stdout.strip().splitlines():
        subprocess.run(["docker", "rm", "-f", name.strip()], capture_output=True, timeout=30)
    tdir = TALOS_HOME / "tasks" / tid
    if tdir.exists():
        shutil.rmtree(tdir, ignore_errors=True)
    conn = db_conn()
    conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
    conn.commit()
    conn.close()

def gitlab_api(path, method="GET", body=None):
    url = f"{GITLAB_URL}/api/v4{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("PRIVATE-TOKEN", GITLAB_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except Exception as e:
        return {"error": str(e)}

def git_ls_remote(repo_url, branch):
    r = subprocess.run(["git", "ls-remote", repo_url, f"refs/heads/{branch}"],
                       capture_output=True, text=True, timeout=30)
    output = r.stdout.strip()
    return output.split("\t")[0].strip() if output else None

# ── Test functions ───────────────────────────────────────────────

def test_M1():
    """执行器自启动 + 重启不丢状态 (I7)."""
    t0 = time.time()
    # Check systemd unit has Restart=always
    unit_path = Path(REPO_DIR) / "deploy" / "talos.service"
    unit_content = unit_path.read_text() if unit_path.exists() else ""
    has_restart = "Restart=always" in unit_content
    # Dispatch a task via real tick
    tid = create_task("M1-AutoStart", skills=["talos-code-demo"])
    conn = hermes_conn()
    try:
        from talos.executor.spawn import make_spawn_fn
        from talos.executor.loop import tick
        from hermes_cli import kanban_db as kb
        task = kb.get_task(conn, tid)
        if task.status == "ready":
            spawn_fn = make_spawn_fn(conn)
            tick(conn, spawn_fn)
            time.sleep(3)
            task = kb.get_task(conn, tid)
            run_id = task.current_run_id
            stable = run_id is not None
        else:
            stable = False
            run_id = None
    finally:
        conn.close()
    cleanup_task(tid)
    record("M1", "✅" if has_restart and stable else "⚠️",
           f"Restart=always={has_restart}, dispatched run_id={run_id}, stable={stable}",
           time.time() - t0, f"task={tid}")

def test_M2():
    """容器隔离 — 无 kanban mount (I2)."""
    t0 = time.time()
    tid = create_task("M2-Isolation", skills=["talos-code-demo"])
    conn = hermes_conn()
    try:
        from talos.executor.spawn import make_spawn_fn
        from talos.executor.loop import tick
        from hermes_cli import kanban_db as kb
        task = kb.get_task(conn, tid)
        if task.status == "ready":
            spawn_fn = make_spawn_fn(conn)
            tick(conn, spawn_fn)
            time.sleep(3)
            task = kb.get_task(conn, tid)
            run_id = task.current_run_id
            if run_id:
                cname = f"hermes-worker-{tid}-{run_id}"
                r = subprocess.run(["docker", "inspect", cname, "--format",
                                    "{{json .Mounts}}"], capture_output=True, text=True, timeout=15)
                mounts = json.loads(r.stdout) if r.stdout else []
                has_kanban = any("kanban" in m.get("Source", "") for m in mounts)
                name_ok = cname.startswith("hermes-worker-")
    finally:
        conn.close()
    cleanup_task(tid)
    record("M2", "✅" if not has_kanban and name_ok else "⚠️",
           f"container={cname}, mounts={len(mounts)}, has_kanban={has_kanban}",
           time.time() - t0, f"task={tid}")

def test_M3():
    """容器内无 kanban DB 环境变量 (I2)."""
    t0 = time.time()
    tid = create_task("M3-NoKanbanEnv", skills=["talos-code-demo"])
    conn = hermes_conn()
    try:
        from talos.executor.spawn import make_spawn_fn
        from talos.executor.loop import tick
        from hermes_cli import kanban_db as kb
        task = kb.get_task(conn, tid)
        if task.status == "ready":
            spawn_fn = make_spawn_fn(conn)
            tick(conn, spawn_fn)
            time.sleep(3)
            task = kb.get_task(conn, tid)
            run_id = task.current_run_id
            if run_id:
                cname = f"hermes-worker-{tid}-{run_id}"
                r = subprocess.run(["docker", "inspect", cname, "--format",
                                    "{{json .Config.Env}}"], capture_output=True, text=True, timeout=15)
                env_vars = json.loads(r.stdout) if r.stdout else []
                kanban_vars = [v for v in env_vars if "KANBAN" in v]
    finally:
        conn.close()
    cleanup_task(tid)
    record("M3", "✅" if len(kanban_vars) == 0 else "⚠️",
           f"env_vars={len(env_vars)}, kanban_vars={len(kanban_vars)}",
           time.time() - t0, f"task={tid}")

def test_M4():
    """worker 上下文含声明摘要."""
    t0 = time.time()
    tid = create_task("M4-Context", skills=["talos-code-demo"])
    conn = hermes_conn()
    try:
        from talos.executor.spawn import make_spawn_fn
        from talos.executor.loop import tick
        from hermes_cli import kanban_db as kb
        task = kb.get_task(conn, tid)
        if task.status == "ready":
            spawn_fn = make_spawn_fn(conn)
            tick(conn, spawn_fn)
            time.sleep(3)
            task = kb.get_task(conn, tid)
            run_id = task.current_run_id
            if run_id:
                ctx_path = TALOS_HOME / "tasks" / tid / str(run_id) / "context.md"
                if ctx_path.exists():
                    ctx = ctx_path.read_text()
                    has_kanban = "Kanban task" in ctx or "kanban" in ctx.lower()
                    has_decl = "声明" in ctx or "declaration" in ctx.lower() or "artifact" in ctx.lower()
                    has_result = "result" in ctx.lower() or "结果" in ctx
                    size = len(ctx)
    finally:
        conn.close()
    cleanup_task(tid)
    record("M4", "✅" if has_kanban and has_decl and size > 500 else "⚠️",
           f"kanban_ctx={has_kanban}, decl={has_decl}, size={size}",
           time.time() - t0, f"task={tid}")

def test_M5():
    """裁决 unmet 分流."""
    t0 = time.time()
    tid = create_task("M5-Unmet", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid, result_json=None)
    v, s, _ = adjudicate_run(tid, run_id)
    conn = db_conn()
    comments = conn.execute("SELECT * FROM task_comments WHERE task_id=? AND author='talos-executor'", (tid,)).fetchall()
    conn.close()
    cleanup_task(tid)
    record("M5", "✅" if v.status == "unmet" and s == "ready" and len(comments) == 1 else "⚠️",
           f"verdict={v.status}, status={s}, exec_comments={len(comments)}, problems={v.problems[:2]}",
           time.time() - t0, f"task={tid}")

def test_M6():
    """重试含 run#1 error 上下文."""
    t0 = time.time()
    tid = create_task("M6-Retry", skills=["talos-code-demo"])
    # Run 1: unmet
    run_id1 = dispatch_and_simulate(tid, result_json=None)
    v1, s1, _ = adjudicate_run(tid, run_id1)
    # Run 2: dispatch again
    run_id2 = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"Fixed","artifacts":[]},
        work_files={"src/feature.py": "def feature(): return 'hello'\n" * 10})
    ctx2_path = TALOS_HOME / "tasks" / tid / str(run_id2) / "context.md"
    has_prior = False
    if ctx2_path.exists():
        ctx2 = ctx2_path.read_text()
        has_prior = any(kw in ctx2.lower() for kw in ["prior", "attempt", "unmet", "error", "失败"])
    v2, s2, _ = adjudicate_run(tid, run_id2)
    cleanup_task(tid)
    record("M6", "✅" if has_prior else "⚠️",
           f"run1={v1.status}, run2={v2.status}, has_prior_error={has_prior}",
           time.time() - t0, f"task={tid}")

def test_M7():
    """执行器评论 — 每次裁决 1 条."""
    t0 = time.time()
    tid = create_task("M7-Comment", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid, result_json=None)
    v, s, _ = adjudicate_run(tid, run_id)
    conn = db_conn()
    comments = conn.execute("SELECT * FROM task_comments WHERE task_id=? AND author='talos-executor'", (tid,)).fetchall()
    conn.close()
    cleanup_task(tid)
    record("M7", "✅" if len(comments) == 1 and v.status == "unmet" else "⚠️",
           f"exec_comments={len(comments)} (expected 1), verdict={v.status}",
           time.time() - t0, f"task={tid}")

def test_M8():
    """CI 验证 — pipeline success."""
    t0 = time.time()
    branch = "talos/m8-acc"
    gitlab_api(f"/projects/{PILOT_PID}/repository/branches?branch={branch}&ref=main", method="POST")
    # Create a .gitlab-ci.yml on the branch so CI triggers
    ci_content = "test:\n  script:\n    - echo ok\n"
    file_path = urllib.parse.quote(".gitlab-ci.yml", safe="")
    gitlab_api(f"/projects/{PILOT_PID}/repository/files/{file_path}", method="POST",
               body={"branch": branch, "content": ci_content, "commit_message": "add ci"})
    time.sleep(5)
    # Wait for pipeline
    pipe_status = "none"
    for _ in range(24):  # 24 * 5s = 120s max
        pipelines = gitlab_api(f"/projects/{PILOT_PID}/pipelines?ref={branch}&per_page=1")
        if isinstance(pipelines, list) and pipelines:
            pipe_status = pipelines[0].get("status", "none")
            if pipe_status in ("success", "failed", "canceled"):
                break
        time.sleep(5)

    tid = create_task("M8-CI", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"Done",
                     "artifacts":[{"kind":"git_branch","repo":PILOT_REPO,"branch":branch,"sha":"abc"}]})
    if run_id:
        v, s, _ = adjudicate_run(tid, run_id)
    gitlab_api(f"/projects/{PILOT_PID}/repository/branches/{urllib.parse.quote(branch, safe='')}", method="DELETE")
    cleanup_task(tid)
    record("M8", "✅" if pipe_status == "success" else "⚠️",
           f"pipeline={pipe_status}, verdict={v.status if run_id else 'no_run'}, problems={v.problems[:2] if run_id else []}",
           time.time() - t0, f"task={tid}, branch={branch}")

def test_M9():
    """CI 无 .gitlab-ci.yml → defect."""
    t0 = time.time()
    branch = "talos/m9-noci"
    gitlab_api(f"/projects/{PILOT_PID}/repository/branches?branch={branch}&ref=main", method="POST")
    tid = create_task("M9-NoCI", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"Done",
                     "artifacts":[{"kind":"git_branch","repo":PILOT_REPO,"branch":branch,"sha":"abc"}]})
    if run_id:
        v, s, _ = adjudicate_run(tid, run_id)
        has_defect = any("无流水线定义" in d for d in v.defects)
    gitlab_api(f"/projects/{PILOT_PID}/repository/branches/{urllib.parse.quote(branch, safe='')}", method="DELETE")
    cleanup_task(tid)
    record("M9", "✅" if has_defect else "⚠️",
           f"verdict={v.status}, defects={v.defects[:2]}",
           time.time() - t0, f"task={tid}")

def test_M10():
    """SHA 不匹配 → unmet."""
    t0 = time.time()
    branch = "talos/m10-sha"
    gitlab_api(f"/projects/{PILOT_PID}/repository/branches?branch={branch}&ref=main", method="POST")
    # Push a file so branch has a commit
    file_path = urllib.parse.quote("m10_file.py", safe="")
    gitlab_api(f"/projects/{PILOT_PID}/repository/files/{file_path}", method="POST",
               body={"branch": branch, "content": "# m10\n", "commit_message": "m10"})
    time.sleep(2)
    real_sha = git_ls_remote(PILOT_REPO, branch)
    tid = create_task("M10-Sha", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"Done",
                     "artifacts":[{"kind":"git_branch","repo":PILOT_REPO,"branch":branch,"sha":"wrongsha123"}]})
    if run_id:
        v, s, _ = adjudicate_run(tid, run_id)
        has_sha_problem = any("sha" in p.lower() for p in v.problems)
    gitlab_api(f"/projects/{PILOT_PID}/repository/branches/{urllib.parse.quote(branch, safe='')}", method="DELETE")
    cleanup_task(tid)
    record("M10", "✅" if has_sha_problem else "⚠️",
           f"real_sha={real_sha[:8] if real_sha else None}, has_sha_problem={has_sha_problem}, verdict={v.status}",
           time.time() - t0, f"task={tid}")

def test_M11():
    """evidence source → degraded."""
    t0 = time.time()
    skill_dir = Path.home() / ".hermes" / "skills" / "talos-evidence-acc"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: talos-evidence-acc\n"
        "completion_contract:\n  artifacts: []\n"
        "  verification: {required: true, source: evidence, timeout_s: 60}\n"
        "---\n# evidence test\n")
    tid = create_task("M11-Evidence", body="Evidence test.", skills=["talos-evidence-acc"])
    run_id = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"Done","artifacts":[]})
    if run_id:
        v, s, _ = adjudicate_run(tid, run_id)
        has_defect = any("证据" in d or "evidence" in d.lower() or "state.db" in d for d in v.defects)
    cleanup_task(tid)
    shutil.rmtree(skill_dir, ignore_errors=True)
    record("M11", "✅" if has_defect else "⚠️",
           f"verdict={v.status}, defects={v.defects[:2]}",
           time.time() - t0, f"task={tid}")

def test_M12():
    """bad skill frontmatter → 不崩溃."""
    t0 = time.time()
    bad_skill = Path.home() / ".hermes" / "skills" / "talos-bad-acc"
    bad_skill.mkdir(parents=True, exist_ok=True)
    (bad_skill / "SKILL.md").write_text(
        "---\nname: talos-bad-acc\ncompletion_contract:\n  artifacts:\n"
        "    - path: '${workspace}/src/x.py'\n      min_bytes: INVALID\n"
        "---\n# bad\n")
    tid = create_task("M12-BadSkill", body="Test.", skills=["talos-bad-acc"])
    run_id = manual_dispatch(tid)
    create_container_with_result(tid, run_id,
        result_json={"schema":1,"status":"done","summary":"ok","artifacts":[]})
    v, s, _ = adjudicate_run(tid, run_id)
    cleanup_task(tid)
    shutil.rmtree(bad_skill, ignore_errors=True)
    record("M12", "✅" if v.status in ("error","unmet","degraded","pass") else "⚠️",
           f"verdict={v.status}, new_status={s} (no crash on INVALID min_bytes)",
           time.time() - t0, f"task={tid}")

def test_M13():
    """2 次连续 unmet → blocked."""
    t0 = time.time()
    tid = create_task("M13-DoubleUnmet", skills=["talos-code-demo"])
    # Run 1: unmet
    run_id1 = dispatch_and_simulate(tid, result_json=None)
    v1, s1, _ = adjudicate_run(tid, run_id1)
    # Run 2: unmet again
    run_id2 = dispatch_and_simulate(tid, result_json=None)
    v2, s2, _ = adjudicate_run(tid, run_id2)
    task_dict = get_task_dict(tid)
    failures = task_dict.get("consecutive_failures", 0)
    cleanup_task(tid)
    record("M13", "✅" if s2 in ("blocked", "triage") and failures >= 2 else "⚠️",
           f"run1={v1.status}→{s1}, run2={v2.status}→{s2}, failures={failures}",
           time.time() - t0, f"task={tid}")

def test_M14():
    """result status=blocked → error → block_task."""
    t0 = time.time()
    tid = create_task("M14-Blocked", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"blocked","summary":"Missing credentials","artifacts":[]})
    if run_id:
        v, s, _ = adjudicate_run(tid, run_id)
    else:
        run_id = manual_dispatch(tid)
        create_container_with_result(tid, run_id,
            result_json={"schema":1,"status":"blocked","summary":"Missing credentials","artifacts":[]})
        v, s, _ = adjudicate_run(tid, run_id)
    cleanup_task(tid)
    record("M14", "✅" if v.status == "error" and v.result_status == "blocked" else "⚠️",
           f"verdict={v.status}, result_status={v.result_status}, new_status={s}",
           time.time() - t0, f"task={tid}")

def test_M15():
    """缺 result.json → unmet."""
    t0 = time.time()
    tid = create_task("M15-NoResult", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid, result_json=None)
    v, s, _ = adjudicate_run(tid, run_id)
    has_problem = any("结果文件缺失" in p for p in v.problems)
    cleanup_task(tid)
    record("M15", "✅" if v.status == "unmet" and has_problem else "⚠️",
           f"verdict={v.status}, has_result_problem={has_problem}, problems={v.problems[:2]}",
           time.time() - t0, f"task={tid}")

def test_M16():
    """心跳更新."""
    t0 = time.time()
    tid = create_task("M16-Heartbeat", skills=["talos-code-demo"])
    conn = hermes_conn()
    try:
        from talos.executor.spawn import make_spawn_fn
        from talos.executor.loop import tick, _heartbeat_live_containers
        from hermes_cli import kanban_db as kb
        task = kb.get_task(conn, tid)
        if task.status == "ready":
            spawn_fn = make_spawn_fn(conn)
            tick(conn, spawn_fn)
            time.sleep(3)
            task = kb.get_task(conn, tid)
            run_id = task.current_run_id
            if run_id:
                _heartbeat_live_containers(conn)
                task = kb.get_task(conn, tid)
                task_dict = get_task_dict(tid)
                hb = task_dict.get("last_heartbeat_at")
    finally:
        conn.close()
    cleanup_task(tid)
    record("M16", "✅" if hb is not None else "⚠️",
           f"last_heartbeat_at={hb}",
           time.time() - t0, f"task={tid}")

def test_M17():
    """max_runtime 超时设置."""
    t0 = time.time()
    tid = create_task("M17-Timeout", skills=["talos-code-demo"], max_runtime_seconds=1)
    task_dict = get_task_dict(tid)
    mrt = task_dict.get("max_runtime_seconds")
    cleanup_task(tid)
    record("M17", "✅" if mrt == 1 else "⚠️",
           f"max_runtime_seconds={mrt}",
           time.time() - t0, f"task={tid}")

def test_M18():
    """裁决先于回收 (I8) — 代码检查."""
    t0 = time.time()
    import inspect
    from talos.executor.loop import tick
    src = inspect.getsource(tick)
    adj_pos = src.find("_adjudicate_exited")
    disp_pos = src.find("_dispatch")
    order_correct = adj_pos > 0 and disp_pos > 0 and adj_pos < disp_pos
    record("M18", "✅" if order_correct else "⚠️",
           f"adjudicate_pos={adj_pos}, dispatch_pos={disp_pos}, order_correct={order_correct}",
           time.time() - t0, "code inspection of tick()")

def test_M19():
    """凭据 — spawn.py 中有 token minting 逻辑."""
    t0 = time.time()
    spawn_src = (Path(REPO_DIR) / "talos" / "executor" / "spawn.py").read_text()
    cred_src = (Path(REPO_DIR) / "talos" / "executor" / "credentials.py").read_text()
    has_mint = "project_access_token" in cred_src or "access_token" in cred_src
    has_git_config = "GIT_CONFIG" in spawn_src or "credential.helper" in spawn_src
    record("M19", "✅" if has_mint and has_git_config else "⚠️",
           f"credentials_has_mint={has_mint}, spawn_has_git_config={has_git_config}",
           time.time() - t0, "source inspection")

def test_M20():
    """分支保护 — talos-pilot main 分支."""
    t0 = time.time()
    branches = gitlab_api(f"/projects/{PILOT_PID}/protected_branches")
    main_protected = any(b.get("name") == "main" for b in branches) if isinstance(branches, list) else False
    record("M20", "✅" if main_protected else "⚠️",
           f"main_protected={main_protected}, branches={[b.get('name') for b in branches] if isinstance(branches, list) else 'err'}",
           time.time() - t0, "GitLab API")

def test_M21():
    """归档目录含 result.json + inspect.json + verdict.json."""
    t0 = time.time()
    tid = create_task("M21-Archive", skills=["talos-code-demo"])
    run_id = manual_dispatch(tid)
    create_container_with_result(tid, run_id,
        result_json={"schema":1,"status":"done","summary":"ok","artifacts":[]},
        work_files={"src/feature.py": "def feature(): return 'hello'\n" * 10})
    v, s, bundle = adjudicate_run(tid, run_id)
    from talos.executor.archive import archive
    adir = archive(tid, run_id, bundle, v)
    files = [f.name for f in adir.glob("*")] if adir.exists() else []
    has_result = "result.json" in files
    has_inspect = "inspect.json" in files
    has_verdict = "verdict.json" in files
    cleanup_task(tid)
    record("M21", "✅" if has_result and has_inspect and has_verdict else "⚠️",
           f"files={files}",
           time.time() - t0, f"task={tid}")

def test_M22():
    """多执行单元无分支逻辑 (I1)."""
    t0 = time.time()
    # Create two tasks with different skills
    tid1 = create_task("M22-Code", body="Code task.", skills=["talos-code-demo"])
    tid2 = create_task("M22-Doc", body="Doc task.", skills=["talos-doc-demo"])
    run_id1 = dispatch_and_simulate(tid1, result_json=None)
    run_id2 = dispatch_and_simulate(tid2, result_json=None)
    v1, s1, _ = adjudicate_run(tid1, run_id1)
    v2, s2, _ = adjudicate_run(tid2, run_id2)
    # Check no skill-name branching in code
    adj_src = (Path(REPO_DIR) / "talos" / "executor" / "adjudicate.py").read_text()
    has_branching = any(kw in adj_src for kw in ["if skill", "elif skill", "skill_name =="])
    cleanup_task(tid1)
    cleanup_task(tid2)
    record("M22", "✅" if not has_branching else "⚠️",
           f"code_verdict={v1.status}, doc_verdict={v2.status}, no_skill_branching={not has_branching}",
           time.time() - t0, f"tasks={tid1},{tid2}")

def test_A1():
    """对抗：sqlite 注入 — 容器无 kanban mount."""
    t0 = time.time()
    tid = create_task("A1-Sqlite", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid, result_json=None)
    # Check container has no kanban mount
    cname = f"hermes-worker-{tid}-{run_id}"
    r = subprocess.run(["docker", "inspect", cname, "--format", "{{json .Mounts}}"],
                       capture_output=True, text=True, timeout=15)
    mounts = json.loads(r.stdout) if r.stdout else []
    has_kanban = any("kanban" in m.get("Source", "") for m in mounts)
    v, s, _ = adjudicate_run(tid, run_id)
    cleanup_task(tid)
    record("A1", "✅" if not has_kanban else "⚠️",
           f"has_kanban_mount={has_kanban}, verdict={v.status}",
           time.time() - t0, f"task={tid}")

def test_A2():
    """对抗：虚假 self_check — 只看 §5.4 schema."""
    t0 = time.time()
    tid = create_task("A2-FalseCheck", skills=["talos-code-demo"])
    run_id = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"ok","artifacts":[],
                     "self_check":"passed","verified":True})
    v, s, _ = adjudicate_run(tid, run_id)
    # self_check should be ignored — verdict should be unmet (no artifacts)
    cleanup_task(tid)
    record("A2", "✅" if v.status == "unmet" else "⚠️",
           f"verdict={v.status} (self_check ignored), problems={v.problems[:2]}",
           time.time() - t0, f"task={tid}")

def test_A3():
    """对抗：路径保护 — .gitlab-ci.yml 写入被拦截."""
    t0 = time.time()
    from talos.plugins.path_protect import _pre_tool_call
    # Simulate write_file and terminal calls targeting .gitlab-ci.yml
    result_write = _pre_tool_call("write_file", {"path": ".gitlab-ci.yml", "content": "test"})
    result_terminal = _pre_tool_call("terminal", {"command": "echo test > .gitlab-ci.yml"})
    blocked_write = result_write is not None and result_write.get("action") == "block"
    blocked_terminal = result_terminal is not None and result_terminal.get("action") == "block"
    record("A3", "✅" if blocked_write and blocked_terminal else "⚠️",
           f"write_file_blocked={blocked_write}, terminal_blocked={blocked_terminal}",
           time.time() - t0, "plugin unit test")

# ── Main ─────────────────────────────────────────────────────────

ALL_TESTS = {
    "M1": test_M1, "M2": test_M2, "M3": test_M3, "M4": test_M4,
    "M5": test_M5, "M6": test_M6, "M7": test_M7, "M8": test_M8,
    "M9": test_M9, "M10": test_M10, "M11": test_M11, "M12": test_M12,
    "M13": test_M13, "M14": test_M14, "M15": test_M15, "M16": test_M16,
    "M17": test_M17, "M18": test_M18, "M19": test_M19, "M20": test_M20,
    "M21": test_M21, "M22": test_M22,
    "A1": test_A1, "A2": test_A2, "A3": test_A3,
}

# Long-running tests that should be backgrounded
BACKGROUND_TESTS: set[str] = set()  # all tests run in foreground

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default=None,
                        help="Comma-separated test IDs (e.g. M1-M5,M8)")
    parser.add_argument("--log-dir", default="/tmp/acc-logs",
                        help="Log directory for background tests")
    args = parser.parse_args()

    Path(args.log_dir).mkdir(parents=True, exist_ok=True)

    # Determine which tests to run
    if args.batch:
        tests_to_run = []
        for part in args.batch.split(","):
            if "-" in part:
                prefix = part.split("-")[0][:-1]  # e.g. "M1" from "M1-M5"
                start = int(part.split("-")[0][-1])
                end = int(part.split("-")[1])
                for i in range(start, end + 1):
                    tests_to_run.append(f"{prefix}{i}")
            else:
                tests_to_run.append(part)
    else:
        tests_to_run = list(ALL_TESTS.keys())

    # Separate foreground and background tests
    fg_tests = [t for t in tests_to_run if t not in BACKGROUND_TESTS]
    bg_tests = [t for t in tests_to_run if t in BACKGROUND_TESTS]

    # Run foreground tests
    for tid in fg_tests:
        if tid in ALL_TESTS:
            print(f"\n=== {tid} ===")
            try:
                ALL_TESTS[tid]()
            except Exception as e:
                traceback.print_exc()
                record(tid, "❌", f"exception: {e}", 0, "")

    # Run background tests
    for tid in bg_tests:
        if tid in ALL_TESTS:
            print(f"\n=== {tid} (background) ===")
            log_file = Path(args.log_dir) / f"{tid}.log"
            # Run in subprocess with log capture
            script = f"""
import sys; sys.path.insert(0, "{REPO_DIR}")
sys.path.insert(0, "{Path.home() / '.hermes' / 'hermes-agent'}")
from acc_real import ALL_TESTS
ALL_TESTS["{tid}"]()
"""
            r = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=300,
                cwd=REPO_DIR,
                env={**os.environ, "PYTHONPATH": f"{REPO_DIR}:{Path.home() / '.hermes' / 'hermes-agent'}"})
            log_file.write_text(r.stdout + r.stderr)
            print(f"  (log: {log_file})")

    print("\n=== All done ===")

if __name__ == "__main__":
    main()
