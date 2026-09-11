# Talos — Autonomous Task Execution System

Talos is the executor component of the AI-native delivery platform built on
Hermes Agent's Kanban subsystem. It runs a persistent loop that:

1. **Adjudicates** exited worker containers (collect → adjudicate → finalize → archive → reap)
2. **Heartbeats** live worker containers
3. **Dispatches** new ready tasks via the kernel's `dispatch_once`

## Design

- **Design doc**: `docs/dd2/详细设计-第二批-执行器-v1.md` (design-side, not in this repo)
- **Invariants I1–I10**: see PR description
- **Hermes source**: v0.21.1 (read-only reference; this repo does NOT modify Hermes)

## Structure

```
talos/
  executor/          # Main loop, spawn, sentinel, adjudicate, finalize, archive, credentials, reap
  plugins/           # Container-side plugins: skill_protect, path_protect, trace_collect
  observability/     # ES forwarder + index template
deploy/              # systemd unit, env template, docker-compose
skills/              # Example execution units: talos-code-demo, talos-doc-demo
tests/               # Pytest fixtures with real 38-column schema
```

## Quick Start

```bash
# Install
pip install -e .

# Configure
cp deploy/talos.env.template /etc/hermes/talos.env
# Edit /etc/hermes/talos.env with your values

# Run
python -m talos.executor.main
```

## Testing

```bash
python -m pytest tests/ -v
```
