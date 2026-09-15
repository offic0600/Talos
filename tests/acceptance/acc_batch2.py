#!/usr/bin/env python3
"""Run acceptance tests M5-M22 + A1-A3 using direct collect+adjudicate+finalize."""
import json, os, sys, time, subprocess, sqlite3, shutil
from pathlib import Path

os.environ.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
os.environ.setdefault("HERMES_KANBAN_DB", "/Users/zhaoc/.hermes/kanban/kanban.db")
os.environ.setdefault("TALOS_GITLAB_URL", "https://hgit.haier.net")
os.environ.setdefault("TALOS_GITLAB_ADMIN_TOKEN", os.environ.get("TALOS_GITLAB_ADMIN_TOKEN", ""))
os.environ.setdefault("TALOS_ES_URL", "http://localhost:9200")
os.environ.setdefault("TALOS_HOME", "/Users/zhaoc/.hermes/talos")
os.environ.setdefault("TALOS_WORKER_IMAGE", "hermes-worker:latest")
os.environ["PATH"] = "/Users/zhaoc/.hermes/hermes-agent/venv/bin:" + os.environ.get("PATH", "")

sys.path.insert(0, "/Users/zhaoc/Documents/Projects/AI-Native-Delivery/Talos")
sys.path.insert(0, "/Users/zhaoc/.hermes/hermes-agent")

KANBAN_DB = os.environ["HERMES_KANBAN_DB"]
TALOS_HOME = Path(os.environ["TALOS_HOME"])
RESULTS_FILE = "/Users/zhaoc/Documents/Projects/AI-Native-Delivery/docs/dd2/acc_results.jsonl"

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
        if max_retries is not None: kwargs["max_retries"] = max_retries
        if max_runtime is not None: kwargs["max_runtime_seconds"] = max_runtime
        tid = kb.create_task(conn, **kwargs)
        conn.commit()
        return tid
    finally:
        conn.close()

def get_task(tid):
    conn = db_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row: return None
    class T: pass
    t = T()
    for k in row.keys(): setattr(t, k, row[k])
    import json as j
    t.skills = j.loads(row["skills"]) if row["skills"] else []
    return t

def get_task_dict(tid):
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

def record(test_id, status, evidence, details=""):
    result = {"test_id": test_id, "status": status, "evidence": evidence,
              "details": details, "timestamp": time.time()}
    with open(RESULTS_FILE, "a") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(f"  [{test_id}] {status} — {evidence[:150]}")
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
    subprocess.run(["docker", "stop", f"hermes-worker-{tid}-{run_id}"],
                   capture_output=True, timeout=30)

def rm_container(tid, run_id=None):
    if run_id:
        subprocess.run(["docker", "rm", "-f", f"hermes-worker-{tid}-{run_id}"],
                       capture_output=True, timeout=30)
    else:
        r = subprocess.run(["docker", "ps", "-a", "--filter", f"name=hermes-worker-{tid}",
                            "--format", "{{.Names}}"], capture_output=True, text=True, timeout=15)
        for name in r.stdout.strip().splitlines():
            subprocess.run(["docker", "rm", "-f", name.strip()], capture_output=True, timeout=30)

def dispatch_and_simulate(tid, result_json=None, work_files=None):
    """Dispatch task (get run_id) then create simulated container."""
    conn = hermes_conn()
    try:
        from hermes_cli import kanban_db as kb
        from talos.executor.spawn import make_spawn_fn
        from talos.executor.loop import tick

        task = kb.get_task(conn, tid)
        if task.status != "ready":
            print(f"  Task not ready: {task.status}")
            return None, None

        # Use real spawn to get run_id, but container will exit immediately
        spawn_fn = make_spawn_fn(conn)
        tick(conn, spawn_fn)
        time.sleep(3)

        task = kb.get_task(conn, tid)
        run_id = task.current_run_id
        if not run_id:
            print(f"  No run_id after dispatch")
            return None, None

        # Remove the real (exited) container and create our simulated one
        rm_container(tid, run_id)
        time.sleep(1)
        simulate_container(tid, run_id, result_json, work_files)
        stop_container(tid, run_id)
        time.sleep(1)

        return run_id, task
    finally:
        conn.close()

def adjudicate_run(tid, run_id):
    """Run collect + adjudicate + finalize on a task."""
    from talos.executor.collect import collect
    from talos.executor.adjudicate import adjudicate
    from talos.executor.declarations import load_declarations
    from talos.executor.finalize import finalize

    task = get_task(tid)
    bundle = collect(tid, run_id)
    decl = load_declarations(task.skills if task else None, task)
    verdict = adjudicate(tid, run_id, bundle, decl)

    conn = hermes_conn()
    try:
        new_status = finalize(conn, tid, run_id, verdict)
    finally:
        conn.close()
    return verdict, new_status, bundle

def cleanup_task(tid):
    rm_container(tid)
    tdir = TALOS_HOME / "tasks" / tid
    if tdir.exists(): shutil.rmtree(tdir, ignore_errors=True)
    conn = db_conn()
    conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
    conn.commit()
    conn.close()

PILOT_REPO = "https://hgit.haier.net/S05190/talos-pilot.git"

# ═══ M5: 声明 3 产物，worker 只做 2 → unmet ═══
print("\n=== M5: Adjudication unmet ===")
try:
    tid = create_task("M5-Unmet", body=f"repo: {PILOT_REPO}\nWrite 2 of 3.", skills=["talos-code-demo"])
    run_id, _ = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"2/3","artifacts":[]},
        work_files={"src/a.py": "def a(): pass\n" * 5, "src/b.py": "def b(): pass\n" * 5})
    if run_id:
        verdict, new_status, _ = adjudicate_run(tid, run_id)
        comments = get_comments(tid)
        exec_comments = [c for c in comments if c.get("author") == "talos-executor"]
        record("M5", "✅" if verdict.status == "unmet" and len(exec_comments) >= 1 else "⚠️",
               f"verdict={verdict.status}, new_status={new_status}, exec_comments={len(exec_comments)}, problems={verdict.problems[:2]}",
               f"task={tid}")
    cleanup_task(tid)
except Exception as e:
    record("M5", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══ M7: 每次裁决恰好一条 [执行器] 评论 ═══
print("\n=== M7: One executor comment ===")
try:
    tid = create_task("M7-Comment", body=f"repo: {PILOT_REPO}\nTest.", skills=["talos-code-demo"])
    run_id, _ = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"Done","artifacts":[]})
    if run_id:
        verdict, new_status, _ = adjudicate_run(tid, run_id)
        comments = get_comments(tid)
        exec_comments = [c for c in comments if c.get("author") == "talos-executor"]
        record("M7", "✅" if len(exec_comments) == 1 else "⚠️",
               f"exec_comments={len(exec_comments)} (expected 1), verdict={verdict.status}",
               f"task={tid}")
    cleanup_task(tid)
except Exception as e:
    record("M7", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══ M12: 声明 frontmatter 非法 → error → triage ═══
print("\n=== M12: Bad skill frontmatter ===")
try:
    bad_skill = Path.home() / ".hermes" / "skills" / "talos-bad-skill"
    bad_skill.mkdir(parents=True, exist_ok=True)
    (bad_skill / "SKILL.md").write_text(
        "---\nname: talos-bad-skill\ncompletion_contract:\n  artifacts:\n"
        "    - {path: '${workspace}/src/x.py', min_bytes: INVALID}\n---\n# bad\n")
    tid = create_task("M12-BadSkill", body="Test.", skills=["talos-bad-skill"])
    run_id, _ = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"ok","artifacts":[]})
    if run_id:
        verdict, new_status, _ = adjudicate_run(tid, run_id)
        record("M12", "✅" if verdict.status == "error" else "⚠️",
               f"verdict={verdict.status}, new_status={new_status}",
               f"task={tid}")
    cleanup_task(tid)
    shutil.rmtree(bad_skill, ignore_errors=True)
except Exception as e:
    record("M12", "❌", f"exception: {e}", "")
    try: cleanup_task(tid); shutil.rmtree(bad_skill, ignore_errors=True)
    except: pass

# ═══ M13: 连续 2 次 unmet → triage ═══
print("\n=== M13: Two consecutive unmet → triage ===")
try:
    tid = create_task("M13-TripleFail", body=f"repo: {PILOT_REPO}\nFail.", skills=["talos-code-demo"], max_retries=2)
    # Run 1: no result → unmet
    run_id1, _ = dispatch_and_simulate(tid, result_json=None)
    if run_id1:
        verdict1, status1, _ = adjudicate_run(tid, run_id1)
        print(f"  Run 1: verdict={verdict1.status}, status={status1}")
    # Run 2: no result → unmet again → should triage
    task = get_task_dict(tid)
    if task["status"] == "ready":
        run_id2, _ = dispatch_and_simulate(tid, result_json=None)
        if run_id2:
            verdict2, status2, _ = adjudicate_run(tid, run_id2)
            print(f"  Run 2: verdict={verdict2.status}, status={status2}")
    final = get_task_dict(tid)
    record("M13", "✅" if final["status"] in ("triage", "blocked") else "⚠️",
           f"final_status={final['status']}, failures={final['consecutive_failures']}",
           f"task={tid}")
    cleanup_task(tid)
except Exception as e:
    record("M13", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══ M14: status=blocked → triage ═══
print("\n=== M14: Result status=blocked ===")
try:
    tid = create_task("M14-Blocked", body=f"repo: {PILOT_REPO}\nBlocked.", skills=["talos-code-demo"])
    run_id, _ = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"blocked","summary":"Missing API","artifacts":[]})
    if run_id:
        verdict, new_status, _ = adjudicate_run(tid, run_id)
        record("M14", "✅" if new_status == "triage" else "⚠️",
               f"verdict={verdict.status}, new_status={new_status}",
               f"task={tid}")
    cleanup_task(tid)
except Exception as e:
    record("M14", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══ M15: 结果文件缺失 → unmet ═══
print("\n=== M15: Missing result file ===")
try:
    tid = create_task("M15-NoResult", body=f"repo: {PILOT_REPO}\nNo result.", skills=["talos-code-demo"])
    run_id, _ = dispatch_and_simulate(tid, result_json=None)
    if run_id:
        verdict, new_status, _ = adjudicate_run(tid, run_id)
        comments = get_comments(tid)
        has_result_problem = any("结果文件" in c.get("body","") or "result" in c.get("body","").lower() for c in comments)
        record("M15", "✅" if verdict.status == "unmet" and has_result_problem else "⚠️",
               f"verdict={verdict.status}, has_result_problem={has_result_problem}, problems={verdict.problems[:2]}",
               f"task={tid}")
    cleanup_task(tid)
except Exception as e:
    record("M15", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══ M22: 两个不同执行单元由同一执行器跑（I1） ═══
print("\n=== M22: Two different execution units ===")
try:
    # Code task
    tid1 = create_task("M22-Code", body=f"repo: {PILOT_REPO}\nCode task.", skills=["talos-code-demo"])
    run_id1, _ = dispatch_and_simulate(tid1,
        result_json={"schema":1,"status":"done","summary":"Code done","artifacts":[]})
    if run_id1:
        v1, s1, _ = adjudicate_run(tid1, run_id1)
        print(f"  Code task: verdict={v1.status}, status={s1}")

    # Doc task
    tid2 = create_task("M22-Doc", body="Write a doc.", skills=["talos-doc-demo"])
    run_id2, _ = dispatch_and_simulate(tid2,
        result_json={"schema":1,"status":"done","summary":"Doc done","artifacts":[]},
        work_files={"docs/spec.md": "# Spec\n\nDetailed spec.\n" * 10})
    if run_id2:
        v2, s2, _ = adjudicate_run(tid2, run_id2)
        print(f"  Doc task: verdict={v2.status}, status={s2}")

    # Check I1: no skill-name branching in executor code
    r = subprocess.run(["grep", "-rn", "talos-code-demo\|talos-doc-demo",
                        "talos/executor/"], capture_output=True, text=True, timeout=10)
    no_branching = len(r.stdout.strip()) == 0

    record("M22", "✅" if no_branching else "⚠️",
           f"code_verdict={v1.status if run_id1 else '?'}, doc_verdict={v2.status if run_id2 else '?'}, no_skill_branching={no_branching}",
           f"tasks={tid1},{tid2}")
    cleanup_task(tid1)
    cleanup_task(tid2)
except Exception as e:
    record("M22", "❌", f"exception: {e}", "")
    try: cleanup_task(tid1); cleanup_task(tid2)
    except: pass

# ═══ A1: 对抗 — worker 尝试用 sqlite 改任务状态 ═══
print("\n=== A1: Adversarial — sqlite attack ===")
try:
    tid = create_task("A1-SqliteAttack", body=f"repo: {PILOT_REPO}\nTry sqlite.", skills=["talos-code-demo"])
    run_id, _ = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"Tried sqlite","artifacts":[]})
    if run_id:
        # Check container had no kanban DB
        from talos.executor.collect import collect
        bundle = collect(tid, run_id)
        # The container should not have kanban.db
        inspect = bundle.inspect or {}
        mounts = inspect.get("Mounts", [])
        has_kanban = any("kanban" in m.get("Destination","") for m in mounts)
        # Adjudicate should work normally
        verdict, new_status, _ = adjudicate_run(tid, run_id)
        record("A1", "✅" if not has_kanban else "⚠️",
               f"has_kanban_mount={has_kanban}, verdict={verdict.status}, status={new_status}",
               f"task={tid}")
    cleanup_task(tid)
except Exception as e:
    record("A1", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══ A2: 对抗 — self_check.verification_ran=true 但不跑测试 ═══
print("\n=== A2: Adversarial — false self_check ===")
try:
    tid = create_task("A2-FalseSelfCheck", body=f"repo: {PILOT_REPO}\nFake self_check.", skills=["talos-code-demo"])
    run_id, _ = dispatch_and_simulate(tid,
        result_json={"schema":1,"status":"done","summary":"Done",
                     "artifacts":[],"self_check":{"verification_ran":True,"notes":"Claimed but didn't run"}})
    if run_id:
        verdict, new_status, _ = adjudicate_run(tid, run_id)
        # Verdict should NOT be pass just because self_check says so
        # It should be unmet or degraded (no CI pipeline, no artifacts)
        record("A2", "✅" if verdict.status != "pass" else "⚠️",
               f"verdict={verdict.status} (self_check ignored), problems={verdict.problems[:2]}",
               f"task={tid}")
    cleanup_task(tid)
except Exception as e:
    record("A2", "❌", f"exception: {e}", "")
    try: cleanup_task(tid)
    except: pass

# ═══ A3: 对抗 — 修改 .gitlab-ci.yml ═══
print("\n=== A3: Adversarial — path protection ===")
try:
    # Test the path_protect plugin directly
    sys.path.insert(0, "/Users/zhaoc/Documents/Projects/AI-Native-Delivery/Talos")
    from talos.plugins.path_protect import _pre_tool_call

    # Simulate worker trying to write .gitlab-ci.yml
    result = _pre_tool_call("write_file", {"path": "/work/.gitlab-ci.yml", "content": "test: always_pass"})
    blocked = result is not None and result.get("action") == "block"

    # Also test terminal command trying to write it
    result2 = _pre_tool_call("terminal", {"command": "echo 'test' > /work/.gitlab-ci.yml"})
    blocked2 = result2 is not None and result2.get("action") == "block"

    record("A3", "✅" if blocked and blocked2 else "⚠️",
           f"write_file_blocked={blocked}, terminal_blocked={blocked2}",
           f"path_protect plugin test")
except Exception as e:
    record("A3", "❌", f"exception: {e}", "")

print("\n=== Batch 2 complete ===")
