"""Finalize: apply verdict to the kanban board via kernel APIs (§7).

The executor calls ONLY these kernel APIs (I2 — executor is the sole writer):
  - ``kb.complete_task`` — pass / degraded → done
  - ``_record_task_failure`` — unmet / error / status=blocked → requeue or block
  - ``kb.request_review`` — result.json request_review=true
  - ``kb.add_comment`` — verdict comment
  - ``kb.create_task`` / ``kb.link_tasks`` — subtasks from result.json

Terminal state routing (§7 v2):
  pass        → complete_task → done
  degraded    → complete_task (metadata marks degraded_checks) → done
  unmet (<limit) → _record_task_failure → ready (requeue)
  unmet (≥limit) → _record_task_failure → blocked (kernel 断路)
  error       → _record_task_failure(error="校验器故障：…") → same as unmet
  result.status=blocked → _record_task_failure(error=summary) → same as unmet
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


def _format_comment(verdict: Verdict, task_id: str, run_id: int) -> str:
    """Format the verdict as a kanban comment (§7, M7).

    评论体带 run 号（v2.1 FIX #8）：``[执行器] 裁决(run N)：...``
    """
    if verdict.status == "pass":
        parts = [f"[执行器] 裁决(run {run_id})：通过"]
        # Add branch links from artifacts (verified by adjudicator)
        for art in verdict.artifacts:
            if isinstance(art, dict) and art.get("kind") == "git_branch":
                parts.append(f"分支: {art.get('repo', '')}/-/tree/{art.get('branch', '')}")
        if verdict.summary:
            parts.append("以下为实例自述，未经验证：")
            parts.append(verdict.summary[:500])
        return "\n".join(parts)

    elif verdict.status == "degraded":
        parts = [f"[执行器] 裁决(run {run_id})：⚠️ 降级放行"]
        if verdict.defects:
            parts.append("缺陷: " + "; ".join(verdict.defects[:5]))
        if verdict.summary:
            parts.append("以下为实例自述，未经验证：")
            parts.append(verdict.summary[:500])
        return "\n".join(parts)

    elif verdict.status == "unmet":
        parts = [f"[执行器] 裁决(run {run_id})：⛔ 裁决未通过"]
        if verdict.problems:
            parts.append("缺项: " + "; ".join(verdict.problems[:10]))
        return "\n".join(parts)

    elif verdict.status == "error":
        parts = [f"[执行器] 裁决(run {run_id})：校验器故障"]
        if verdict.defects:
            parts.append("; ".join(verdict.defects[:5]))
        return "\n".join(parts)

    elif verdict.status == "instance_not_started":
        parts = [f"[执行器] 裁决(run {run_id})：⚠️ 实例未能启动"]
        if verdict.problems:
            parts.append("; ".join(verdict.problems[:5]))
        return "\n".join(parts)

    return f"[执行器] 裁决(run {run_id}): {verdict.status}"


def _format_blocked_comment(verdict: Verdict, task_id: str, run_id: int,
                             failure_limit: int) -> str:
    """P1-1: Format the '转人工' comment when a task hits the failure limit.

    Written as a SEPARATE comment after the verdict comment, because
    ``_format_comment`` only looks at ``verdict.status`` (always 'unmet'
    for both requeue and block), not the resulting ``new_status``.
    """
    parts = [f"[执行器] 连续 {failure_limit} 次未满足契约，转人工处理"]
    if verdict.problems:
        parts.append("缺项清单: " + "; ".join(verdict.problems[:10]))
    if verdict.defects:
        parts.append("缺陷: " + "; ".join(verdict.defects[:5]))
    return "\n".join(parts)


def _create_subtasks(conn: Any, task_id: str, verdict: Verdict) -> None:
    """Create subtasks declared in result.json (§3 interface signatures).

    P0 安全边界：
    - 数量上限 MAX_SUBTASKS（默认 10），超出截断 + 记 error 事件
    - created_by 标注 talos-executor(via <父任务号>)，方便追溯
    - title 为空跳过
    - 单条 create_task 抛错不中断其余项
    """
    if not verdict.subtasks:
        return
    try:
        from hermes_cli.kanban_db import create_task, link_tasks
    except ImportError:
        return

    from talos.executor.constants import MAX_SUBTASKS

    requested = len(verdict.subtasks)
    if requested > MAX_SUBTASKS:
        log_event("error", task_id=task_id,
                  msg=f"实例请求创建 {requested} 个子任务，超出上限 {MAX_SUBTASKS}，已截断")

    created_count = 0
    for st in verdict.subtasks:
        if created_count >= MAX_SUBTASKS:
            break
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
                created_by=f"{EXECUTOR_AUTHOR}(via {task_id})",
            )
            link_tasks(conn, task_id, child_id)
            created_count += 1
        except Exception as e:
            log_event("error", task_id=task_id,
                      msg=f"failed to create subtask '{title}': {e}")


def _is_run_kernel_closed(conn: Any, task_id: str, run_id: int) -> bool:
    """Check if a run was closed by the kernel (§7 last row).

    内核已回收的 run（超时/哨兵死）不再落终局；
    判定依据：task_runs.ended_at 非空 或 tasks.current_run_id 已清
    （v2.1 FIX #4 / FIX #8）。
    """
    try:
        # Check task_runs.ended_at
        row = conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is not None:
            ended_at = row["ended_at"] if "ended_at" in row.keys() else row[0]
            if ended_at is not None:
                return True

        # Check tasks.current_run_id
        row = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is not None:
            current_run_id = row["current_run_id"] if "current_run_id" in row.keys() else row[0]
            if current_run_id is None:
                return True

    except Exception as e:
        log_event("error", task_id=task_id, run_id=run_id,
                  msg=f"_is_run_kernel_closed check failed: {e}")

    return False


def _write_kernel_closed_comment(conn: Any, task_id: str, run_id: int,
                                 verdict: Verdict) -> None:
    """Write a 'verdict voided by kernel reclaim' comment (§7 last row).

    内核回收的 run，裁决结果作废，写说明评论（v2.1 FIX #4 / FIX #8）。
    """
    from hermes_cli import kanban_db as kb
    summary = verdict.summary[:500] if verdict.summary else verdict.status
    body = f"本次裁决结果因内核回收作废：{summary}"
    try:
        kb.add_comment(conn, task_id, author=EXECUTOR_AUTHOR, body=body)
    except Exception as e:
        log_event("error", task_id=task_id, run_id=run_id,
                  msg=f"write kernel-closed comment failed: {e}")


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

    先调内核 API 改状态，成功后再写评论（v2.1 FIX #8）；
    评论体带 run 号。改状态返回 False → 记 error 事件、不写裁决评论，
    并检查该 run 是否已被内核关闭（v2.1 FIX #4 / FIX #8）。
    """
    t0 = time.time()
    new_status: str = "unknown"

    # Import kernel APIs (late-bound to avoid import cycles)
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _record_task_failure

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
        # request_review 成功，写裁决评论后返回
        comment = _format_comment(verdict, task_id, run_id)
        try:
            kb.add_comment(conn, task_id, author=EXECUTOR_AUTHOR, body=comment)
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id, msg=f"add_comment failed: {e}")
        _create_subtasks(conn, task_id, verdict)
        log_event("finalized", task_id=task_id, run_id=run_id,
                  duration_ms=(time.time() - t0) * 1000,
                  extra={"new_status": new_status})
        return new_status

    # 先调内核 API 改状态，再写评论（v2.1 FIX #8）
    kernel_ok = False

    # Route based on verdict
    if verdict.status == "instance_not_started":
        # 设计侧方案 1：零次模型调用 = 环境缺陷。
        # 与「缺绑定参数」「坏声明」同类：直接转人工、不重拉、不消耗 retry 次数。
        # 用 block_task 而不是 _record_task_failure。
        error_msg = "; ".join(verdict.problems[:5])[:500]
        try:
            kb.block_task(
                conn, task_id,
                reason=error_msg,
                kind="environment",
            )
            kernel_ok = True
            new_status = "blocked"
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"block_task (instance_not_started) failed: {e}")
            new_status = "unknown"

    elif verdict.status == "pass":
        # → done
        try:
            kernel_ok = kb.complete_task(
                conn, task_id,
                summary=verdict.summary[:2000],
                metadata=verdict.as_dict(),
                expected_run_id=run_id,
            )
            new_status = "done" if kernel_ok else "unknown"
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
            kernel_ok = kb.complete_task(
                conn, task_id,
                summary=f"⚠️ 降级放行: {'; '.join(verdict.defects[:3])}\n\n{verdict.summary[:1500]}",
                metadata=metadata,
                expected_run_id=run_id,
            )
            new_status = "done" if kernel_ok else "unknown"
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"complete_task (degraded) failed: {e}")
            new_status = "unknown"

    elif verdict.status == "unmet":
        # → requeue (ready) or blocked (at limit) via _record_task_failure (§7 v2)
        # error = problems list or worker summary for status=blocked
        if verdict.result_status == "blocked":
            error_msg = verdict.summary[:500]
        else:
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
            kernel_ok = True
            new_status = "blocked" if blocked else "ready"
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"_record_task_failure failed: {e}")
            new_status = "unknown"

    elif verdict.status == "error":
        # Checker error → same path as unmet: _record_task_failure (§7 v2)
        # error = "校验器故障：<defects>"
        error_msg = f"校验器故障: {'; '.join(verdict.defects[:5])}"[:500]
        try:
            blocked = _record_task_failure(
                conn, task_id, error_msg,
                outcome="checker_error",
                failure_limit=failure_limit,
                release_claim=True,
                end_run=True,
                event_payload_extra={
                    "verdict": "error",
                    "run_id": run_id,
                },
            )
            kernel_ok = True
            new_status = "blocked" if blocked else "ready"
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"_record_task_failure (error) failed: {e}")
            new_status = "unknown"

    # 内核 API 成功后写裁决评论；失败则检查内核回收（v2.1 FIX #8 / FIX #4）
    if kernel_ok:
        comment = _format_comment(verdict, task_id, run_id)
        try:
            kb.add_comment(conn, task_id, author=EXECUTOR_AUTHOR, body=comment)
        except Exception as e:
            log_event("error", task_id=task_id, run_id=run_id, msg=f"add_comment failed: {e}")

        # P1-1: 任务落成 blocked 后追加「转人工」评论。
        # _format_comment 只看 verdict.status（unmet），不区分回 ready 还是转 blocked。
        # 这里在 new_status 确定后追加一条单独评论。
        if new_status == "blocked":
            blocked_comment = _format_blocked_comment(verdict, task_id, run_id, failure_limit)
            try:
                kb.add_comment(conn, task_id, author=EXECUTOR_AUTHOR, body=blocked_comment)
            except Exception as e:
                log_event("error", task_id=task_id, run_id=run_id, msg=f"blocked comment failed: {e}")
    else:
        # 内核 API 返回 False → 记 error，不写裁决评论，检查内核回收
        log_event("error", task_id=task_id, run_id=run_id,
                  msg=f"kernel API returned False for verdict={verdict.status}")
        if _is_run_kernel_closed(conn, task_id, run_id):
            _write_kernel_closed_comment(conn, task_id, run_id, verdict)

    # Create subtasks from result.json
    _create_subtasks(conn, task_id, verdict)

    log_event("finalized", task_id=task_id, run_id=run_id,
              duration_ms=(time.time() - t0) * 1000,
              extra={"new_status": new_status, "verdict": verdict.status})

    return new_status
