"""Talos executor v1.

A persistent loop that:
  1. Adjudicates exited worker containers (§6)
  2. Heartbeats live worker containers
  3. Dispatches new ready tasks via the kernel's ``dispatch_once``

The executor is **type-agnostic** (I1): no branching on skill names or task
types. All behaviour is driven by execution-unit declarations (§4).

Design ref: docs/dd2/详细设计-第二批-执行器-v1.md
"""

from talos.executor.loop import run_executor, tick

__all__ = ["run_executor", "tick"]
