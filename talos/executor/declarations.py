"""Execution-unit declaration loader (§4).

Declarations live in SKILL.md frontmatter. The executor reads them to know:
  - What artifacts to expect (paths, min sizes)
  - What git branch to verify
  - What verification source to use (ci / evidence / none)
  - What deliverables to check
  - What credentials to mint
  - What resource limits to set

**I1 invariant**: The executor never branches on skill *names* — it only
reads declaration *fields*. Multi-skill merge: artifacts union, resources
max, verification/deliverables/credentials union.

Declaration schema (§4):

```yaml
resources: { memory_mb: 1024, cpus: 1.0 }
completion_contract:
  artifacts:
    - { path: "${workspace}/src/feature.py", min_bytes: 50 }
  git:
    branch: "talos/${task_id}"
    require_push: true
  verification:
    required: true
    source: ci          # ci | evidence | none
    timeout_s: 900
deliverables:
  - { kind: git_branch, repo: "${task.repo}", branch: "${git.branch}" }
  - { kind: platform_attachment, path: "${workspace}/docs/spec.md" }
credentials:
  - { kind: gitlab, scope: write_repository, ttl: task }
  - { kind: platform, ttl: task }
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from talos.executor import constants


class DeclarationError(Exception):
    """Raised when a skill declaration (frontmatter) is malformed (§6, M12).

    This triggers verdict=error → _record_task_failure(error="校验器故障：…").
    """


@dataclass
class ArtifactSpec:
    path: str
    min_bytes: int = 0


@dataclass
class GitSpec:
    branch: str = ""
    require_push: bool = True


@dataclass
class VerificationSpec:
    required: bool = True
    source: str = "none"   # ci | evidence | none; "none" until a skill sets it
    timeout_s: int = 900


@dataclass
class DeliverableSpec:
    kind: str                  # git_branch | platform_attachment
    repo: str = ""
    branch: str = ""
    path: str = ""


@dataclass
class CredentialSpec:
    kind: str                  # gitlab | platform
    scope: str = ""
    ttl: str = "task"


@dataclass
class ResourceSpec:
    memory_mb: int = 1024
    cpus: float = 1.0


@dataclass
class Declaration:
    """Merged declaration for one task (possibly multi-skill)."""
    skills: list[str] = field(default_factory=list)
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    artifacts: list[ArtifactSpec] = field(default_factory=list)
    git: GitSpec = field(default_factory=GitSpec)
    verification: VerificationSpec = field(default_factory=VerificationSpec)
    deliverables: list[DeliverableSpec] = field(default_factory=list)
    credentials: list[CredentialSpec] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def _load_skill_frontmatter(skill_name: str) -> dict:
    """Load YAML frontmatter from ``~/.hermes/skills/<name>/SKILL.md``.

    Returns ``{}`` if the skill file is not found or has no frontmatter.
    """
    candidates = [
        constants.HOME / "skills" / skill_name / "SKILL.md",
        constants.HOME / "skills" / skill_name.replace("-", "_") / "SKILL.md",
    ]
    for path in candidates:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            return _parse_frontmatter(text, skill_name)
    return {}


def _parse_frontmatter(text: str, skill_name: str = "") -> dict:
    """Extract and parse YAML frontmatter from markdown.

    Raises ``DeclarationError`` if the frontmatter exists but is invalid YAML
    or not a dict (§6, M12).
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise DeclarationError(f"skill {skill_name}: frontmatter YAML 非法: {e}")
    if not isinstance(data, dict):
        raise DeclarationError(f"skill {skill_name}: frontmatter 不是 YAML 对象")
    return data


def _substitute(template: str, task: Any) -> str:
    """Substitute ``${task_id}``, ``${task.repo}``, ``${git.branch}``, etc."""
    if not template:
        return template
    values: dict[str, str] = {
        "task_id": getattr(task, "id", "") or "",
        "workspace": "/work",
    }
    repo = _extract_repo(task)
    if repo:
        values["task.repo"] = repo
    branch_name = getattr(task, "branch_name", None)
    if branch_name:
        values["git.branch"] = branch_name
    result = template
    for key, val in values.items():
        result = result.replace("${" + key + "}", val)
    return result


def _extract_repo(task: Any) -> str:
    """Extract repo URL from task (body first-line ``repo:`` or workspace_path)."""
    body = getattr(task, "body", None) or ""
    for line in body.splitlines():
        line = line.strip()
        if line.lower().startswith("repo:"):
            return line.split(":", 1)[1].strip()
    wp = getattr(task, "workspace_path", None)
    if wp and "://" in wp:
        return wp
    return ""


def load_declarations(skills: Optional[list[str]], task: Any = None) -> Declaration:
    """Load and merge declarations for all skills on a task.

    Merge rules (§4): artifacts → union; resources → max; verification /
    deliverables / credentials → union. Backward-compatible: missing
    ``completion_contract`` sub-fields use defaults.
    """
    if not skills:
        return Declaration(skills=[])

    merged = Declaration(skills=list(skills))
    all_artifacts: list[ArtifactSpec] = []
    all_deliverables: list[DeliverableSpec] = []
    all_credentials: list[CredentialSpec] = []
    max_mem = 0
    max_cpus = 0.0

    for skill_name in skills:
        fm = _load_skill_frontmatter(skill_name)
        if not fm:
            continue
        merged.raw.setdefault("_skills", []).append({skill_name: fm})

        # Resources
        res = fm.get("resources", {})
        if isinstance(res, dict):
            max_mem = max(max_mem, int(res.get("memory_mb", 0)))
            max_cpus = max(max_cpus, float(res.get("cpus", 0.0)))

        # completion_contract (first batch compat)
        cc = fm.get("completion_contract", {})
        if isinstance(cc, dict):
            # artifacts
            for art in cc.get("artifacts", []) or []:
                if isinstance(art, dict):
                    path = _substitute(str(art.get("path", "")), task)
                    try:
                        min_b = int(art.get("min_bytes", 0))
                    except (ValueError, TypeError) as e:
                        raise DeclarationError(
                            f"skill {skill_name}: artifact min_bytes 非法: {art.get('min_bytes')!r}"
                        ) from e
                    all_artifacts.append(ArtifactSpec(path=path, min_bytes=min_b))

            # git
            git = cc.get("git", {})
            if isinstance(git, dict):
                branch = _substitute(str(git.get("branch", "")), task)
                if branch and not merged.git.branch:
                    merged.git.branch = branch
                if git.get("require_push", True):
                    merged.git.require_push = True

            # verification
            ver = cc.get("verification", {})
            if isinstance(ver, dict):
                # Explicit required value: set True or False
                if "required" in ver:
                    merged.verification.required = bool(ver["required"])
                source = str(ver.get("source") or "none")
                # ci > evidence > none: pick the strongest source seen
                _source_rank = {"": -1, "none": 0, "evidence": 1, "ci": 2}
                if _source_rank.get(source, 1) > _source_rank.get(merged.verification.source, -1):
                    merged.verification.source = source
                merged.verification.timeout_s = max(
                    merged.verification.timeout_s,
                    int(ver.get("timeout_s", 900)),
                )

        # deliverables (v2)
        for dl in fm.get("deliverables", []) or []:
            if isinstance(dl, dict):
                kind = str(dl.get("kind", ""))
                repo = _substitute(str(dl.get("repo", "")), task)
                branch = _substitute(str(dl.get("branch", "")), task)
                path = _substitute(str(dl.get("path", "")), task)
                all_deliverables.append(
                    DeliverableSpec(kind=kind, repo=repo, branch=branch, path=path)
                )

        # credentials (v2)
        for cred in fm.get("credentials", []) or []:
            if isinstance(cred, dict):
                all_credentials.append(CredentialSpec(
                    kind=str(cred.get("kind", "")),
                    scope=str(cred.get("scope", "")),
                    ttl=str(cred.get("ttl", "task")),
                ))

    if max_mem > 0:
        merged.resources.memory_mb = max_mem
    if max_cpus > 0:
        merged.resources.cpus = max_cpus
    merged.artifacts = all_artifacts
    merged.deliverables = all_deliverables
    merged.credentials = all_credentials

    # Default git branch if not set
    if not merged.git.branch and task is not None:
        tid = getattr(task, "id", "")
        if tid:
            merged.git.branch = f"talos/{tid}"

    return merged
