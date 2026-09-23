"""trace_collect — record LLM API calls to a JSONL trace file.

``post_api_request`` hook that appends one JSONL line per LLM API call to
``/tmp/hermes-worker-home/trace/trace.jsonl``.  The executor harvests this
file after container exit (§8 archive, §10 observability) and forwards it
to Elasticsearch via the trace forwarder.

This is the same mechanism as the first-batch ``p6_trace_hook`` and the
dd1 ``completion-contract`` trace, but split into its own plugin with no
gate logic.  The hook is observer-only: it never returns a directive and
never blocks or transforms anything.

The trace file is the primary observability source for worker containers
(§10): ``api_request`` line count in ES must equal the assistant-turn count
in ``state.db`` (M21 cross-check).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Trace output directory (container-internal HERMES_HOME / trace).
_TRACE_DIR = os.environ.get(
    "TALOS_TRACE_DIR",
    "/tmp/hermes-worker-home/trace",
)

#: Trace JSONL file name.
_TRACE_FILE = os.path.join(_TRACE_DIR, "trace.jsonl")

#: Whether to include assistant content length (privacy: off by default
#: — content is not sent to ES, only token counts and latency).
_INCLUDE_CONTENT_CHARS = os.environ.get(
    "TALOS_TRACE_INCLUDE_CONTENT_CHARS", "1"
) == "1"


def _write_trace(record: dict) -> None:
    """Append one JSONL line to the trace file (best-effort, never raises)."""
    try:
        os.makedirs(_TRACE_DIR, exist_ok=True)
        with open(_TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("[talos/trace_collect] write failed: %s", exc)


def _post_api_request(
    task_id: str = "",
    turn_id: str = "",
    api_request_id: str = "",
    session_id: str = "",
    platform: str = "",
    model: str = "",
    provider: str = "",
    base_url: str = "",
    api_mode: str = "",
    api_call_count: int = 0,
    api_duration: float = 0,
    started_at: Optional[float] = None,
    ended_at: Optional[float] = None,
    first_chunk_at: Optional[float] = None,
    finish_reason: str = "",
    message_count: int = 0,
    response_model: str = "",
    usage: Optional[Dict[str, Any]] = None,
    assistant_content_chars: int = 0,
    assistant_tool_call_count: int = 0,
    moa_references: Any = None,
    **kwargs: Any,
) -> None:
    """Record one LLM API call to the trace JSONL file.

    Called by the Hermes plugin dispatch after every API request completes
    (success or failure).  The payload fields mirror the ``post_api_request``
    hook contract (see hermes_cli/hooks.py ``_DEFAULT_PAYLOADS``).

    This hook is observer-only: it returns ``None`` and never blocks or
    transforms anything.
    """
    u = usage if isinstance(usage, dict) else {}

    # Time to first byte (TTFB) — a key latency metric.
    ttfb = None
    if first_chunk_at is not None and started_at is not None:
        try:
            ttfb = float(first_chunk_at) - float(started_at)
        except (TypeError, ValueError):
            ttfb = None

    record: Dict[str, Any] = {
        # Timestamp in milliseconds for ES compatibility.
        "timestamp": int(time.time() * 1000),
        "session_id": session_id,
        "task_id": task_id or os.environ.get("TALOS_TASK_ID", ""),
        "run_id": os.environ.get("TALOS_RUN_ID", ""),
        "turn_id": turn_id,
        "api_request_id": api_request_id,
        "platform": platform,
        "model": model,
        "response_model": response_model,
        "provider": provider,
        "base_url": base_url,
        "api_mode": api_mode,
        "api_call_count": api_call_count,
        "api_duration_s": api_duration,
        "started_at": started_at,
        "ended_at": ended_at,
        "first_chunk_at": first_chunk_at,
        "ttfb_s": ttfb,
        "finish_reason": finish_reason,
        "message_count": message_count,
        "prompt_tokens": u.get("prompt_tokens", u.get("input_tokens", 0)),
        "completion_tokens": u.get(
            "completion_tokens", u.get("output_tokens", 0)
        ),
        "total_tokens": u.get("total_tokens", 0),
        "assistant_tool_call_count": assistant_tool_call_count,
        "source": "talos_trace_collect",
    }

    if _INCLUDE_CONTENT_CHARS:
        record["assistant_content_chars"] = assistant_content_chars

    if moa_references is not None:
        record["moa_references"] = moa_references

    _write_trace(record)
