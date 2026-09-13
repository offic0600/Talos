# Talos

AI-Native Delivery Platform — Executor.

## Overview

Talos is the execution layer for the AI-native delivery platform built on Hermes Agent's Kanban subsystem.

## Components

- **Executor** (`talos/executor/`): Long-running process that picks up ready tasks, spawns worker containers, adjudicates results, and manages the full task lifecycle.
- **Plugins** (`talos/plugins/`): In-container contract enforcement (path protection, skill protection, trace collection).
- **Deploy** (`deploy/`): systemd services and Docker Compose for production deployment.

## Design

See `docs/dd2/详细设计-第二批-执行器-v2.md` for the detailed design document.
