"""Talos container-side plugins (batch-1).

Three plugins are registered for execution inside worker containers:

1. **skill_protect** — ``pre_tool_call``: blocks write operations on
   ``skill_manage`` (create / patch / delete / write_file / remove_file)
   targeting managed skills.  Prevents a worker from loosening its own
   contract.  (I3, §5.1)

2. **path_protect** — ``pre_tool_call``: blocks writes to protected paths
   (CI configs like ``.gitlab-ci.yml``, branch protection rules, etc.)
   so a worker cannot neuter the verification pipeline.  (I3, A3, §5.1)

3. **trace_collect** — ``post_api_request``: appends one JSONL line per
   LLM API call to ``/tmp/hermes-worker-home/trace/trace.jsonl``.  The
   executor harvests this file after container exit (§10, §8).

Design ref: docs/dd2/详细设计-第二批-执行器-v1.md §5.1, I3, §10.

I3 invariant: the container has **no** mechanism that decides task terminal
state.  There is no ``kanban_complete`` interception — adjudication is the
sole authority of the executor (§6).  These plugins only protect skill
integrity and CI paths, and collect observability traces.
"""

from __future__ import annotations

import logging

from talos.plugins.path_protect import _pre_tool_call as _path_pre_tool_call
from talos.plugins.skill_protect import _pre_tool_call as _skill_pre_tool_call
from talos.plugins.trace_collect import _post_api_request

logger = logging.getLogger(__name__)

__all__ = ["register"]


def register(ctx) -> None:
    """Register all three container-side plugins.

    Called by the Hermes plugin loader when the plugin manifest is discovered.
    The plugins directory is mounted read-only at ``$HH/plugins`` inside the
    worker container (§5.1 mount table).
    """
    ctx.register_hook("pre_tool_call", _skill_pre_tool_call)
    ctx.register_hook("pre_tool_call", _path_pre_tool_call)
    ctx.register_hook("post_api_request", _post_api_request)
    logger.info(
        "[talos-plugins] registered: skill_protect, path_protect, trace_collect"
    )
