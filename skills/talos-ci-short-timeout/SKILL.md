---
name: talos-ci-short-timeout
description: "CI task with short 120s pipeline timeout for M9 testing."
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
    timeout_s: 120
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
# talos-ci-short-timeout

Same as talos-code-demo but with 120s pipeline timeout (for M9 CI timeout test).

## What to do

1. Clone the repository specified in the task context.
2. Create and switch to branch `talos/${task_id}`.
3. Write `src/feature.py` implementing the required feature.
4. Commit and push to the remote branch.
5. Write the result file at `/task/out/result.json`.

## Rules

- Do NOT attempt to operate the kanban board.
- Do NOT modify `.gitlab-ci.yml`.
- Push ONLY to the `talos/<task_id>` branch.
