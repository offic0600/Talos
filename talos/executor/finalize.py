"""Finalize: apply verdict to the kanban board via kernel APIs (§7).

The executor calls ONLY these kernel APIs (I2 — executor is the sole writer):
  - ``kb.complete_task`` — pass / degraded → done
  - ``_record_task_failure`` — unmet → requeue or block
  - ``kb.block_task`` + ``_route_block`` — blocked → triage on recurrence
  - ``kb.request_review`` — result.json request_review=true
  - ``kb.add_comment`` — verdict comment
  - ``kb.create_task`` / ``kb.link_tasks`` — subtasks from result.json

Terminal state routing (§7):
  pass        → complete_task → done
  degraded    → complete_task (metadata marks degraded_checks) → done
  unmet (<limit) → _record_task_failure(release_claim, end_run) → ready (requeue)
  unmet (≥limit) → _record_task_failure(force_trip) → blocked → block_task → triage
  error       → block_task → triage
  result.status=blocked → block_task(kind=capability) → triage
"""

from __future__ import annotations

import time
from typing import Any, Optional

from talos.executor.adjudicate import Verdict
from talos.executor.constants import (
    DEFAULT_FAILURE_LIMIT,
    EXECUTOR_AUTHOR,
    log_event,
)


def _format_comment(verdict: Verdict, task_id: str) -> str:
    """Format the verdict as a kanban comment (§7, M7)."""
    if verdict.status == "pass":
        parts = ["[执行器] 裁决：通过"]
        # Add branch links from artifacts
        for art in verdict.artifacts:
            if isinstance(art, dict) and art.get("kind") == "git_branch":
                parts.append(f"分支: {art.get('repo', '')}/-/tree/{art.get('branch', '')}")
        if verdict.summary:
            parts.append(f"摘要: {verdict.summary[:500]}")
        return "\n".join(parts)

    elif verdict.status == "degraded":
        parts = ["[执行器] ⚠️ 降级放行"]
        if verdict.defects:
            parts.append("缺陷: " + "; ".join(verdict.defects[:5]))
        if verdict.summary:
            parts.append(f"摘要: {verdict.summary[:500]}")
        return "\n".join(parts)

    elif verdict.status == "unmet":
        parts = [f"[执行器] ⛔ 裁决未通过"]
        if verdict.problems:
            parts.append("缺项: " + "; ".join(verdict.problems[:10]))
        return "\n".join(parts)

    elif verdict.status == "error":
        if verdict.result_status == "blocked":
            parts = ["[执行器] 任务自报 blocked，转人工"]
            if verdict.summary:
                parts.append(f"Worker summary: {verdict.summary[:500]}")
            return "\n".join(parts)
        else:
            parts = ["[执行器] 校验器故障，转人工"]
            if verdict.defects:
                parts.append("; ".join(verdict.defects[:5]))
            return "\n".join(parts)

    return f"[执行器] 裁决: {verdict.status}"


def _create_subtasks(conn: Any, task_id: str, verdict: Verdict) -> None:
    """Create subtasks declared in result.json (§3 interface signatures)."""
    if not verdict.subtasks:
        return
    try:
        from hermes_cli.kanban_db import create_task, link_tasks
    except ImportError:
        return

    for st in verdict.subtasks:
        if not isinstance(st, dict):
            continue
        title = st.get("title", "").strip()
        if not title:
            continue
        try:
            child_id = create_task(
                conn,
                title=title,
                body=st.get("body"),
                skills=st.get("skills", []),
                created_by=EXECUTOR_AUTHOR,
            )
            link_tasks(conn, task_id, child_id)
        except Exception as e:
            log_event("error", task_id=task_id,
                      msg=f"failed to create subtask '{title}': {e}")


def finalize(
    conn: Any,
    task_id: str,
    run_id: int,
    verdict: Verdict,
    *,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
) -> str:
    """Apply the verdict to the kanban board (§7).

    Returns the new task status: ``done``, ``ready``, ``blocked``, or ``triage``.
    """
    t0 = time.time()
    new_status: str = "unknown"

    # Import kernel APIs (late-bound to avoid import cycles)
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _record_task_failure

    # Always write a verdict comment (M7: one per adjudication)
    comment = _format_comment(verdict, task_id)
    try:
        kb.add_comment(conn, task_id, author=EXECUTOR_AUTHOR, body=comment)
    except Exception as e:
        log_event("error", task_id=task_id, run_id=run_id, msg=f"add_comment failed: {e}")

    # Handle request_review from result.json
    if verdict.request_review and verdict.status in ("pass", "degraded"):
        try:
            kb.request_review(conn, task_id, summary=verdict.summary,
                              expected_run_id=run_id)
            log_event("finalized", task_id=task_id, run_id=run_id,
                      extra={"action": "request_review"})
            new_status = "review"
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"request_review failed: {e}")
            # Fall through to normal completion
            verdict.request_review = False

    if new_status == "review":
        # Still create subtasks before returning
        _create_subtasks(conn, task_id, verdict)
        log_event("finalized", task_id=task_id, run_id=run_id,
                  duration_ms=(time.time() - t0) * 1000,
                  extra={"new_status": new_status})
        return new_status

    # Route based on verdict
    if verdict.status == "pass":
        # → done
        try:
            ok = kb.complete_task(
                conn, task_id,
                summary=verdict.summary[:2000],
                metadata=verdict.as_dict(),
                expected_run_id=run_id,
            )
            new_status = "done" if ok else "unknown"
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"complete_task failed: {e}")
            new_status = "unknown"

    elif verdict.status == "degraded":
        # → done (with degraded metadata)
        metadata = verdict.as_dict()
        metadata["degraded"] = True
        metadata["degraded_checks"] = verdict.defects
        try:
            ok = kb.complete_task(
                conn, task_id,
                summary=f"⚠️ 降级放行: {'; '.join(verdict.defects[:3])}\n\n{verdict.summary[:1500]}",
                metadata=metadata,
                expected_run_id=run_id,
            )
            new_status = "done" if ok else "unknown"
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"complete_task (degraded) failed: {e}")
            new_status = "unknown"

    elif verdict.status == "unmet":
        # → requeue (ready) or blocked (at limit)
        error_msg = "; ".join(verdict.problems[:10])[:500]
        try:
            blocked = _record_task_failure(
                conn, task_id, error_msg,
                outcome="adjudication_unmet",
                failure_limit=failure_limit,
                release_claim=True,
                end_run=True,
                event_payload_extra={
                    "verdict": verdict.status,
                    "problems": verdict.problems,
                    "run_id": run_id,
                },
            )
            if blocked:
                # _record_task_failure set status to blocked.
                # Now apply block_task to route through _route_block for
                # recurrence tracking → triage if needed.
                try:
                    kb.block_task(conn, task_id, reason=error_msg,
                                  kind="transient", expected_run_id=run_id)
                except Exception:
                    pass
                # Check if it went to triage
                task = kb.get_task(conn, task_id)
                new_status = task.status if task else "blocked"
                if new_status == "blocked":
                    # Not triage yet — consecutive_failures hit limit but
                    # block_recurrences hasn't. The task is blocked.
                    log_event("finalized", task_id=task_id, run_id=run_id,
                              extra={"action": "blocked_at_limit"})
                else:
                    log_event("finalized", task_id=task_id, run_id=run_id,
                              extra={"action": "triage"})
            else:
                new_status = "ready"
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"_record_task_failure failed: {e}")
            new_status = "unknown"

    elif verdict.status == "error":
        if verdict.result_status == "blocked":
            # Worker self-reported blocked → triage via block_task
            try:
                kb.block_task(conn, task_id,
                              reason=f"worker blocked: {verdict.summary[:300]}",
                              kind="capability",
                              expected_run_id=run_id)
                task = kb.get_task(conn, task_id)
                new_status = task.status if task else "triage"
            except Exception as e:
                log_event("error", task_id=task_id, run_id=run_id,
                          msg=f"block_task (capability) failed: {e}")
                new_status = "unknown"
        else:
            # Checker error → triage
            error_msg = "; ".join(verdict.defects[:5])[:500]
            try:
                # Force-trip through _record_task_failure to blocked,
                # then block_task for routing.
                _record_task_failure(
                    conn, task_id, error_msg,
                    outcome="checker_error",
                    force_trip=True,
                    release_claim=True,
                    end_run=True,
                    event_payload_extra={"verdict": "error"},
                )
                try:
                    kb.block_task(conn, task_id, reason=error_msg,
                                  kind="transient", expected_run_id=run_id)
                except Exception:
                    pass
                task = kb.get_task(conn, task_id)
                new_status = task.status if task else "triage"
            except Exception as e:
                log_event("error", task_id=task_id, run_id=run_id,
                          msg=f"checker_error finalize failed: {e}")
                new_status = "unknown"

    # Create subtasks from result.json
    _create_subtasks(conn, task_id, verdict)

    log_event("finalized", task_id=task_id, run_id=run_id,
              duration_ms=(time.time() - t0) * 1000,
              extra={"new_status": new_status, "verdict": verdict.status})

    return new_status
