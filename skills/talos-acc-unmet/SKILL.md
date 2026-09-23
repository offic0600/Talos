---
name: talos-acc-unmet
description: "Acceptance fixture: declares 3 artifacts, task body says make only 2. For M5/M13 unmet verdict test."
resources:
  memory_mb: 768
  cpus: 1.0
completion_contract:
  artifacts:
    - path: "${workspace}/out/a.md"
      min_bytes: 10
    - path: "${workspace}/out/b.md"
      min_bytes: 10
    - path: "${workspace}/out/c.md"
      min_bytes: 10
  git:
    require_push: false
  verification:
    required: false
    source: none
---
# talos-acc-unmet

You are a file-creating execution unit. Your task is to create files
exactly as specified in the task body.

## What to do

1. Read the task body for the list of files to create and their exact content.
2. Create ONLY the files listed in the task body — no more, no less.
3. Write `/task/out/result.json` with the exact JSON given in the task body.

## Rules

- Create files ONLY as specified in the task body.
- Do NOT attempt to operate the kanban board.
- Do NOT attempt git push.
- Write `/task/out/result.json` when done (or skip it if the task body says so).
