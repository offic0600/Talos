---
name: talos-acc-subtasks
description: "Acceptance fixture: self-reports 15 subtasks in result.json. For M26 subtask limit test."
resources:
  memory_mb: 768
  cpus: 1.0
completion_contract:
  artifacts:
    - path: "${workspace}/out/a.md"
      min_bytes: 10
  git:
    require_push: false
  verification:
    required: false
    source: none
---
# talos-acc-subtasks

You are a file-creating execution unit.

## What to do

1. Create the file(s) specified in the task body under `/work/`.
2. Write `/task/out/result.json` with the exact JSON given in the task body,
   including the `subtasks` array with 15 entries.

## Rules

- The `subtasks` array in result.json MUST contain exactly 15 entries as
  given in the task body.
- Do NOT attempt to operate the kanban board.
- Do NOT attempt git push.
- Write `/task/out/result.json` when done.
