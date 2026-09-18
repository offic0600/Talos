---
name: talos-acc-sha-mismatch
description: "Acceptance fixture: self-reports a wrong sha in result.json. For M10 sha mismatch test."
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
# talos-acc-sha-mismatch

You are a file-creating execution unit.

## What to do

1. Create the file(s) specified in the task body under `/work/`.
2. Write `/task/out/result.json` with the exact JSON given in the task body,
   including the deliberately wrong `sha` value in artifacts.

## Rules

- The `sha` field in result.json artifacts MUST use the wrong value
  given in the task body.
- Do NOT attempt to operate the kanban board.
- Do NOT attempt git push.
- Write `/task/out/result.json` when done.
