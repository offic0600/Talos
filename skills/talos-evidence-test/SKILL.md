---
name: talos-evidence-test
description: "Evidence-based verification skill for M11 testing."
resources:
  memory_mb: 768
  cpus: 1.0
completion_contract:
  artifacts:
    - path: "${workspace}/out/a.md"
      min_bytes: 50
  git:
    require_push: false
  verification:
    required: true
    source: evidence
    timeout_s: 60
---
# talos-evidence-test

Evidence-based verification test skill. Creates a file and relies on
state.db evidence for verification (not CI).

## What to do

1. Create `/work/out/a.md` with at least 50 bytes of content.
2. Write `/task/out/result.json` with status=done.

## Rules

- Do NOT attempt to operate the kanban board.
- Do NOT attempt git push.
