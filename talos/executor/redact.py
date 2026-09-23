"""Desensitization utilities (v2.1 fix #2, §9 I6 exception).

LLM inference keys (ANTHROPIC_API_KEY etc.) are long-lived credentials that
must be passed through to containers, but they must never appear in archives,
executor logs, or acceptance logs.  This module provides three redaction
primitives used by ``archive.py`` and ``constants.py`` before data hits disk.

Design ref: §9 I6 explicit exception — keys containing KEY / TOKEN / SECRET /
PASSWORD / CREDENTIAL have their values replaced with ``***``.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# ── Pattern-based redaction for arbitrary strings ──────────────────────

# GitLab personal access tokens: glpat- followed by 20 alphanumerics.
_GLPAT_RE = re.compile(r"glpat-[A-Za-z0-9_-]{20}")
# OAuth2 credential strings: oauth2:<token>@host  (as written into git-credentials).
_OAUTH2_RE = re.compile(r"(oauth2:)[^@\s]+(@)")
# Generic long hex/alnum tokens that look like API keys (≥32 chars, no spaces).
_GENERIC_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")

# Env-var keys whose values must be masked.
_SENSITIVE_KEY_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def redact_string(s: str) -> str:
    """Redact known token patterns in an arbitrary string.

    Handles:
      - ``glpat-xxxxxxxxxxxxxxxxxxxx`` → ``glpat-***``
      - ``oauth2:<token>@host`` → ``oauth2:***@host``
      - bare 32+ char alphanumeric tokens → ``***``
    """
    s = _GLPAT_RE.sub("glpat-***", s)
    s = _OAUTH2_RE.sub(r"\1***\2", s)
    s = _GENERIC_TOKEN_RE.sub("***", s)
    return s


def redact_dict(d: dict) -> dict:
    """Recursively redact all string values in a dict (returns a new dict).

    Walks lists and nested dicts.  Non-string scalars are copied as-is.
    """
    return _redact_value(copy.deepcopy(d))


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def redact_env(inspect_json: dict) -> dict:
    """Redact ALL environment variables in a ``docker inspect`` JSON (v2.3 §18.2 #5).

    v2.3 change: redact EVERY Config.Env entry's value to ``***``, regardless of
    whether the key name contains KEY/TOKEN/SECRET/etc. This closes the gap where
    values like GPG_KEY or API_SERVER_KEY (which don't match the old markers)
    leaked into archives and logs.

    Returns a deep copy; the original is not modified.
    """
    result = copy.deepcopy(inspect_json)

    # Config.Env lives at the top level for a single-container inspect, but
    # ``docker inspect`` returns a *list* of containers.  Handle both shapes.
    containers = result if isinstance(result, list) else [result]
    for ctr in containers:
        if not isinstance(ctr, dict):
            continue
        config = ctr.get("Config")
        if not isinstance(config, dict):
            continue
        env = config.get("Env")
        if not isinstance(env, list):
            continue
        new_env: list[str] = []
        for entry in env:
            if not isinstance(entry, str) or "=" not in entry:
                new_env.append(entry)
                continue
            key, _, _val = entry.partition("=")
            # v2.3 §18.2 #5: redact ALL env values unconditionally.
            new_env.append(f"{key}=***")
        config["Env"] = new_env

    return result
