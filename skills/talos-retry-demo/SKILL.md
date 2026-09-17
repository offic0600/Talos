---
name: talos-retry-demo
description: "Retry demo: 3 artifacts, no git push, no verification. Used for M5/M6 retry-after-unmet test."
resources:
  memory_mb: 768
  cpus: 1.0
completion_contract:
  artifacts:
    - path: "${workspace}/out/a.md"
      min_bytes: 50
    - path: "${workspace}/out/b.md"
      min_bytes: 50
    - path: "${workspace}/out/c.md"
      min_bytes: 50
  git:
    require_push: false
  verification:
    required: false
    source: none
---
# talos-retry-demo

You are a file-creating execution unit. Your task is to create markdown files
in `/work/out/` as specified in the task body.

## What to do

1. Create the files specified in the task body under `/work/out/`.
2. Write the result file at `/task/out/result.json`.

## Result file format

```json
{
  "schema": 1,
  "status": "done",
  "summary": "Created <list of files>",
  "artifacts": [],
  "subtasks": [],
  "request_review": false,
  "comments": [],
  "self_check": {"verification_ran": false, "notes": "no verification required"}
}
```

## Rules

- Create files ONLY as specified in the task body.
- Do NOT attempt to operate the kanban board — you do not have kanban tools.
- Do NOT attempt git push — this skill does not require it.
- Write `/task/out/result.json` when done.
