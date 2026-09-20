"""Test: collect() handles pre-existing out_dir correctly.

When out_dir already exists (e.g. from a previous failed collect),
docker cp c:/task/out/ out_dir would nest: out_dir/out/result.json.
Using c:/task/out/. copies contents into out_dir directly.

This test creates a real container, pre-creates out_dir, then calls
collect() and asserts result.json lands at out_dir/result.json (not
out_dir/out/result.json).
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

# Fail-safe guard
_TEST_DB = os.environ.get("TALOS_TEST_DB")
if not _TEST_DB:
    pytest.skip("TALOS_TEST_DB not set", allow_module_level=True)

TALOS_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(TALOS_ROOT))

HERMES_SRC = Path(os.environ.get("HERMES_AGENT_SRC", str(Path.home() / ".hermes" / "hermes-agent")))
sys.path.insert(0, str(HERMES_SRC))


def test_collect_preexisting_out_dir():
    """collect() must write result.json to out_dir/ not out_dir/out/ when
    out_dir already exists."""
    from talos.executor.collect import collect, task_dir
    from talos.executor.constants import TASKS_ROOT

    tid = "t_collect_cp_test"
    run_id = 9999
    cname = f"hermes-worker-{tid}-{run_id}"

    # Clean up any previous state
    subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=10)
    tdir = task_dir(tid, run_id)
    if tdir.exists():
        import shutil
        shutil.rmtree(tdir, ignore_errors=True)

    # 1. Create container with result.json
    subprocess.run(
        ["docker", "run", "-d", "--name", cname, "--entrypoint", "sh",
         "hermes-worker:latest",
         "-c", "mkdir -p /task/out && echo '{\"schema\":1,\"status\":\"done\",\"summary\":\"ok\",\"artifacts\":[]}' > /task/out/result.json && sleep 600"],
        capture_output=True, text=True, timeout=60
    )
    # Wait for container to be running
    for _ in range(10):
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", cname],
                           capture_output=True, text=True, timeout=10)
        if r.stdout.strip() == "running":
            break
        time.sleep(0.5)

    # Verify result.json is in container
    r = subprocess.run(["docker", "exec", cname, "test", "-f", "/task/out/result.json"],
                       capture_output=True, timeout=10)
    assert r.returncode == 0, "result.json should exist in container"

    # 2. Stop container
    subprocess.run(["docker", "stop", cname], capture_output=True, timeout=30)
    time.sleep(0.5)

    # 3. Pre-create out_dir (simulating a previous collect that left it behind)
    tdir.mkdir(parents=True, exist_ok=True)
    out_dir = tdir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)  # Pre-existing!

    # 4. Call collect()
    bundle = collect(tid, run_id)

    # 5. Assertions
    assert out_dir.exists(), "out_dir should exist"
    assert (out_dir / "result.json").exists(), \
        "result.json must be at out_dir/result.json, not out_dir/out/result.json"
    assert not (out_dir / "out").exists(), \
        "out_dir/out/ must NOT exist (docker cp /task/out/. copies contents, not the dir)"
    assert bundle.result_json is not None, "bundle.result_json should not be None"
    assert bundle.result_json.get("status") == "done"

    # Cleanup
    subprocess.run(["docker", "rm", "-f", cname], capture_output=True, timeout=10)
    import shutil
    shutil.rmtree(str(tdir), ignore_errors=True)
