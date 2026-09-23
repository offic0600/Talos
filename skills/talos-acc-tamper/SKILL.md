---
name: talos-acc-tamper
description: "Acceptance fixture: writes result.json with tampered repo/branch in artifacts. For M28 artifact source filter test."
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
# talos-acc-tamper

You are a file-creating execution unit.

## What to do

1. Create the file(s) specified in the task body under `/work/`.
2. Write `/task/out/result.json` with the exact JSON given in the task body,
   including artifacts with the tampered repo and branch values.

## Rules

- The `artifacts` array in result.json MUST use the repo and branch values
  exactly as given in the task body (these are intentionally wrong).
- Do NOT attempt to operate the kanban board.
- Do NOT attempt git push.
- Write `/task/out/result.json` when done.
