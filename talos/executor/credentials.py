"""Credential minting and revocation (§9).

Per-task short-lived credentials:
  - ``gitlab``: project access token via GitLab API (scope=write_repository,
    role=Developer, expires=tomorrow). Written to ``<tdir>/creds/git-credentials``.
  - ``platform``: copy the executor's OAuth cache file to ``<tdir>/creds/mcp-tokens/``.

I6 invariant: all tokens in the container are per-task, minted at spawn and
revoked at cleanup. The executor's only long-lived credential is the admin
token in managed config.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from talos.executor.constants import GITLAB_ADMIN_TOKEN, GITLAB_URL, log_event


def _gitlab_api(method: str, path: str, body: Optional[dict] = None) -> dict:
    """Call GitLab API v4 with the admin token."""
    url = f"{GITLAB_URL}/api/v4{path}"
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("PRIVATE-TOKEN", GITLAB_ADMIN_TOKEN)
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception as de:
            # 实例：禁止空吞异常（v2.1 FIX #3）
            log_event("error", msg=f"GitLab API error body decode failed: {de}")
        raise RuntimeError(f"GitLab API {method} {path} → HTTP {e.code}: {body_text}") from e


def _project_id_from_repo(repo_url: str) -> Optional[str]:
    """Extract URL-encoded project path from a GitLab repo URL.

    ``https://hgit.haier.net/S05190/talos-pilot.git`` → ``S05190%2Ftalos-pilot``
    """
    # Strip .git suffix
    clean = repo_url.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    # Take the path portion after the host
    idx = clean.find("//")
    if idx >= 0:
        rest = clean[idx + 2:]
        slash = rest.find("/")
        if slash >= 0:
            project_path = rest[slash + 1:]
        else:
            return None
    else:
        project_path = clean
    # URL-encode slashes
    return project_path.replace("/", "%2F")


def mint_gitlab_token(
    task_id: str,
    run_id: int,
    repo_url: str,
    creds_dir: Path,
) -> Optional[str]:
    """Create a project access token for this task and write git-credentials.

    Returns the token string, or ``None`` on failure.
    """
    if not GITLAB_ADMIN_TOKEN:
        log_event("error", task_id=task_id, run_id=run_id,
                  msg="TALOS_GITLAB_ADMIN_TOKEN not set; cannot mint gitlab token")
        return None

    project_id = _project_id_from_repo(repo_url)
    if not project_id:
        log_event("error", task_id=task_id, run_id=run_id,
                  msg=f"cannot parse project from repo URL: {repo_url}")
        return None

    expires = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    token_name = f"talos-{task_id}-{run_id}"

    try:
        result = _gitlab_api("POST", f"/projects/{project_id}/access_tokens", {
            "name": token_name,
            "scopes": ["write_repository", "read_repository"],
            "access_level": 30,  # Developer
            "expires_at": expires,
        })
    except RuntimeError as e:
        log_event("error", task_id=task_id, run_id=run_id, msg=f"mint token failed: {e}")
        return None

    token = result.get("token")
    if not token:
        log_event("error", task_id=task_id, run_id=run_id, msg="GitLab returned no token")
        return None

    token_id = result.get("id")

    # Write git-credentials file: https://oauth2:<token>@<host>
    host = GITLAB_URL.replace("https://", "").replace("http://", "").rstrip("/")
    creds_dir.mkdir(parents=True, exist_ok=True)
    cred_file = creds_dir / "git-credentials"
    cred_file.write_text(f"https://oauth2:{token}@{host}\n", encoding="utf-8")
    cred_file.chmod(0o600)

    # Persist token metadata for revocation
    meta_file = creds_dir / "token-meta.json"
    meta_file.write_text(json.dumps({
        "token_id": token_id,
        "token_name": token_name,
        "project_id": project_id,
        "expires_at": expires,
    }), encoding="utf-8")
    meta_file.chmod(0o600)

    log_event("dispatched", task_id=task_id, run_id=run_id,
              extra={"minted_token": token_name, "token_id": token_id})
    return token


def revoke_gitlab_token(task_id: str, run_id: int, creds_dir: Path) -> None:
    """Revoke the project access token for this task (§8 cleanup).

    Retries up to 3 times on failure (§8, M19: no token leakage).
    """
    meta_file = creds_dir / "token-meta.json"
    if not meta_file.exists():
        return

    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    token_id = meta.get("token_id")
    project_id = meta.get("project_id")
    token_name = meta.get("token_name")
    if token_id and project_id and GITLAB_ADMIN_TOKEN:
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                _gitlab_api("DELETE", f"/projects/{project_id}/access_tokens/{token_id}")
                log_event("cleaned", task_id=task_id, run_id=run_id,
                          extra={"revoked_token": token_name, "attempt": attempt})
                break
            except RuntimeError as e:
                if attempt < max_retries:
                    log_event("error", task_id=task_id, run_id=run_id,
                              msg=f"revoke token attempt {attempt} failed: {e}; retrying...")
                    time.sleep(2 * attempt)
                else:
                    log_event("error", task_id=task_id, run_id=run_id,
                              msg=f"revoke token FAILED after {max_retries} attempts: {e}")

    # Remove credential files
    for f in creds_dir.glob("*"):
        try:
            f.unlink()
        except OSError as e:
            # 实例：禁止空吞异常（v2.1 FIX #3）
            log_event("error", task_id=task_id, run_id=run_id,
                      msg=f"failed to unlink cred file {f}: {e}")


def cleanup_orphan_tokens() -> int:
    """Scan GitLab for talos-* tokens not belonging to any live run; revoke them.

    Called at executor startup (§8, M19: no token leakage).
    Returns the number of tokens revoked.
    """
    if not GITLAB_ADMIN_TOKEN:
        return 0

    # Collect all active runs from the kanban DB
    import sqlite3
    from talos.executor.constants import KANBAN_DB
    live_token_names: set[str] = set()
    try:
        conn = sqlite3.connect(str(KANBAN_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, current_run_id FROM tasks WHERE status = 'running'"
        ).fetchall()
        conn.close()
        for row in rows:
            if row["current_run_id"]:
                live_token_names.add(f"talos-{row['id']}-{row['current_run_id']}")
    except Exception as e:
        # 实例：禁止空吞异常（v2.1 FIX #3）
        log_event("error", msg=f"cleanup_orphan_tokens: DB query failed: {e}")

    # Scan all projects the executor has used (talos-pilot + Talos)
    project_ids = ["16280", "16288"]  # talos-pilot + Talos
    revoked = 0
    for pid in project_ids:
        try:
            tokens = _gitlab_api("GET", f"/projects/{pid}/access_tokens")
        except RuntimeError:
            continue
        if not isinstance(tokens, list):
            continue
        for tok in tokens:
            name = tok.get("name", "")
            if not name.startswith("talos-"):
                continue
            if name in live_token_names:
                continue  # Belongs to a live run — skip
            tok_id = tok.get("id")
            if not tok_id:
                continue
            try:
                _gitlab_api("DELETE", f"/projects/{pid}/access_tokens/{tok_id}")
                log_event("cleaned", extra={"orphan_token_revoked": name, "project": pid})
                revoked += 1
            except RuntimeError as e:
                log_event("error", msg=f"failed to revoke orphan token {name}: {e}")

    if revoked:
        log_event("cleaned", extra={"orphan_tokens_revoked": revoked})
    return revoked


def copy_platform_tokens(creds_dir: Path) -> None:
    """Copy the executor's OAuth cache to the task's creds dir (§9 platform).

    The source is ``~/.hermes/mcp-tokens/``. This is a best-effort copy for
    the third batch; the second batch only does the copy + mount.
    """
    from talos.executor.constants import HOME
    src = HOME / "mcp-tokens"
    if not src.exists():
        return
    dst = creds_dir / "mcp-tokens"
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, dst / item.name)


def mint_credentials(
    task: Any,
    decl_creds: list,
    repo_url: str,
    tdir: Path,
) -> dict:
    """Mint all declared credentials for a task.

    Returns a dict with keys ``git_credentials_file``, ``mcp_tokens_dir``,
    ``git_token`` (the raw token, for internal use only).
    """
    creds_dir = tdir / "creds"
    creds_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {}

    for cred in decl_creds:
        if cred.kind == "gitlab":
            token = mint_gitlab_token(task.id, task.current_run_id, repo_url, creds_dir)
            if token:
                result["git_credentials_file"] = str(creds_dir / "git-credentials")
                result["git_token"] = token
        elif cred.kind == "platform":
            copy_platform_tokens(creds_dir)
            result["mcp_tokens_dir"] = str(creds_dir / "mcp-tokens")

    return result
