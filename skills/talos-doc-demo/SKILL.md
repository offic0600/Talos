---
name: talos-doc-demo
description: "Use when writing documentation for a Talos demo task. Produces a spec markdown file as a platform attachment."
resources:
  memory_mb: 512
  cpus: 0.5
completion_contract:
  artifacts:
    - path: "${workspace}/docs/spec.md"
      min_bytes: 100
  verification:
    required: false
    source: none
deliverables:
  - kind: platform_attachment
    path: "${workspace}/docs/spec.md"
requires: []
---

# talos-doc-demo

You are a documentation-writing execution unit. Your task is to produce a specification
markdown document based on the requirements in the kanban task body.

## What to do

1. Read the task body for the documentation requirements.
2. Write `docs/spec.md` containing the specification.
3. Write the result file at `/task/out/result.json` (see format below).

## Result file format

```json
{
  "schema": 1,
  "status": "done",
  "summary": "Wrote spec.md covering <topic>",
  "artifacts": [
    {"kind": "file", "path": "/work/docs/spec.md"}
  ],
  "subtasks": [],
  "request_review": false,
  "comments": [],
  "self_check": {"verification_ran": false, "notes": "Documentation task — no CI verification required"}
}
```

## Rules

- Do NOT attempt to operate the kanban board — you do not have kanban tools.
- No git push or CI pipeline is required for this task type.
- The executor will verify that `docs/spec.md` exists and meets the minimum byte requirement.
- Platform attachment upload will be handled in a future batch; for now, the file must exist in the workspace.
