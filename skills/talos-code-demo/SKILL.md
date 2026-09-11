---
name: talos-code-demo
description: "Use when writing code for a Talos demo task. Produces a Python feature file, pushes to a GitLab branch, and passes CI."
resources:
  memory_mb: 1024
  cpus: 1.0
completion_contract:
  artifacts:
    - path: "${workspace}/src/feature.py"
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
credentials:
  - kind: gitlab
    scope: write_repository
    ttl: task
---

# talos-code-demo

You are a code-writing execution unit. Your task is to produce a Python source file
that implements the feature described in the kanban task body.

## What to do

1. Clone the repository specified in the task context (the `repo:` line in the body).
2. Create and switch to branch `talos/${task_id}`.
3. Write `src/feature.py` implementing the required feature.
4. Commit and push to the remote branch.
5. Write the result file at `/task/out/result.json` (see format below).

## Result file format

```json
{
  "schema": 1,
  "status": "done",
  "summary": "Implemented feature.py with <description>",
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
- Push ONLY to the `talos/<task_id>` branch. Pushing to `main` will be rejected by branch protection.
- After pushing, the executor will verify the CI pipeline status on GitLab. Your `self_check` is advisory only.
