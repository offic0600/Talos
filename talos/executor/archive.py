"""Archive: persist run artifacts for audit and trace forwarding (§8, §2 step 12).

Archive layout: ``~/.hermes/talos/archived/<task_id>/<run_id>/``

Contents:
  - ``trace.jsonl`` — API request trace from the container
  - ``contract.jsonl`` — contract check trace
  - ``state.db`` — evidence ledger from the container
  - ``result.json`` — the worker's result file
  - ``inspect.json`` — docker inspect output
  - ``verdict.json`` — the adjudicator's verdict

Trace forwarding to ES happens via the forwarder (§10); this module only
copies files to the archive directory.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from talos.executor.adjudicate import Verdict
from talos.executor.collect import CollectedBundle
from talos.executor.constants import archive_dir, log_event, task_dir


def archive(
    task_id: str,
    run_id: int,
    bundle: CollectedBundle,
    verdict: Optional[Verdict] = None,
) -> Path:
    """Archive all run artifacts to ``archived/<task_id>/<run_id>/`` (§8).

    Returns the archive directory path.
    """
    t0 = time.time()
    adir = archive_dir(task_id, run_id)
    adir.mkdir(parents=True, exist_ok=True)

    tdir = task_dir(task_id, run_id)

    # 1. result.json
    if bundle.result_path and bundle.result_path.exists():
        shutil.copy2(bundle.result_path, adir / "result.json")

    # 2. state.db — look in workspace or out
    state_db_src = None
    if bundle.workspace_path:
        candidate = bundle.workspace_path / "state.db"
        if candidate.exists():
            state_db_src = candidate
    if not state_db_src:
        candidate = tdir / "out" / "state.db"
        if candidate.exists():
            state_db_src = candidate
    if not state_db_src:
        candidate = tdir / "state.db"
        if candidate.exists():
            state_db_src = candidate
    if state_db_src:
        shutil.copy2(state_db_src, adir / "state.db")

    # 3. inspect.json
    if bundle.inspect:
        (adir / "inspect.json").write_text(
            json.dumps(bundle.inspect, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    # 4. verdict.json
    if verdict:
        (adir / "verdict.json").write_text(
            json.dumps(verdict.as_dict(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    # 5. Trace files — look in workspace and out
    trace_src = None
    if bundle.workspace_path:
        candidate = bundle.workspace_path / "trace.jsonl"
        if candidate.exists():
            trace_src = candidate
    if not trace_src:
        candidate = tdir / "out" / "trace.jsonl"
        if candidate.exists():
            trace_src = candidate
    if trace_src:
        shutil.copy2(trace_src, adir / "trace.jsonl")

    # 6. Contract trace
    contract_src = None
    if bundle.workspace_path:
        candidate = bundle.workspace_path / "contract.jsonl"
        if candidate.exists():
            contract_src = candidate
    if not contract_src:
        candidate = tdir / "out" / "contract.jsonl"
        if candidate.exists():
            contract_src = candidate
    if contract_src:
        shutil.copy2(contract_src, adir / "contract.jsonl")

    # 7. Context.md (for audit)
    ctx_src = tdir / "context.md"
    if ctx_src.exists():
        shutil.copy2(ctx_src, adir / "context.md")

    log_event("archived", task_id=task_id, run_id=run_id,
              duration_ms=(time.time() - t0) * 1000,
              extra={"archive_dir": str(adir)})

    return adir
