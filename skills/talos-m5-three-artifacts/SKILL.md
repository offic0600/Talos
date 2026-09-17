---
name: talos-m5-three-artifacts
description: "M5 verification skill: declares 3 artifacts, worker only makes 2."
resources:
  memory_mb: 512
  cpus: 1.0
completion_contract:
  artifacts:
    - path: "${workspace}/src/feature.py"
      min_bytes: 50
    - path: "${workspace}/src/utils.py"
      min_bytes: 50
    - path: "${workspace}/src/helpers.py"
      min_bytes: 50
  git:
    branch: "talos/${task_id}"
    require_push: true
  verification:
    required: true
    source: ci
    timeout_s: 900
deliverables:
  - kind: git_branch
    repo: "${task.repo}"
    branch: "${git.branch}"
requires: [repo, branch]
credentials:
  - kind: gitlab
    scope: write_repository
    ttl: task
---

# talos-m5-three-artifacts

You are a code-writing execution unit. Your task is to produce Python source files
that implement the features described in the kanban task body.

## What to do

1. Clone the repository specified in the task context (the `repo:` line in the body).
2. Create and switch to branch `talos/${task_id}`.
3. Write the source files as described in the task body.
4. Commit and push to the remote branch.
5. Write the result file at `/task/out/result.json` (see format below).

## Result file format

```json
{
  "schema": 1,
  "status": "done",
  "summary": "Implemented files with <description>",
  "artifacts": [
    {"kind": "git_branch", "repo": "<repo_url>", "branch": "talos/<task_id>", "sha": "<commit_sha>"}
  ],
  "subtasks": [],
  "request_review": false,
  "comments": [],
  "self_check": {"verification_ran": true, "notes": "CI pipeline triggered on push"}
}
```

## Rules

- Do NOT attempt to operate the kanban board — you do not have kanban tools.
- Do NOT modify `.gitlab-ci.yml` or other CI configuration files.
- Push ONLY to the `talos/<task_id>` branch.
