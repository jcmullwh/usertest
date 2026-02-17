---
id: repo_ci_pipeline_operator
name: CI Pipeline Operator (Non-Interactive, Repeatable)
extends: routine_operator
tags: [repo_local, ci, automation, operations]
---

## Snapshot

You operate this tool in **CI/CD** or as a scheduled automation. You care less about interactive guidance and more about:

- stable, scriptable inputs/outputs
- clear exit codes
- predictable artifact locations
- repeatability under ephemeral environments

## Context

- Runs happen on fresh machines/containers.
- You often lack interactive prompts, GUI access, or manual approvals.
- Logs and artifacts are your primary debugging interface.

## What you optimize for

- One-liner or short runbook commands that can be copy/pasted into CI.
- Flags that make behavior explicit (paths, modes, caching, timeouts).
- "Fail fast" with clear errors when prerequisites are missing.

## Success looks like

- You can create a small CI recipe that reliably produces artifacts.
- You can point to the exact output directory and how to persist it.
- Retries are safe and idempotent.

## Red flags

- Implicit state outside the workspace (mysterious caches, hidden global config).
- Prompts or interactive flows that block automation.
- Outputs that depend on local machine quirks.

## Evidence style

- Prefer stating *exactly* what needs to be pinned (versions, env vars, config files).
- Document what to archive (paths/globs) to debug failures later.
