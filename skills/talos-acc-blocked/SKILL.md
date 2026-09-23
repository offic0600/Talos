---
name: talos-acc-blocked
description: "Acceptance fixture: writes result.json with status=blocked. For M14 worker-blocked test."
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
# talos-acc-blocked

You are a file-creating execution unit.

## What to do

1. Create the file(s) specified in the task body under `/work/`.
2. Write `/task/out/result.json` with the exact JSON given in the task body,
   including `status: "blocked"`.

## Rules

- The `status` field in result.json MUST be `"blocked"` as given in the task body.
- Do NOT attempt to operate the kanban board.
- Do NOT attempt git push.
- Write `/task/out/result.json` when done.
