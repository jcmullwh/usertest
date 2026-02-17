# Repo-specific usertest missions

This folder contains **repo-specific** missions/personas intended for usertesting **this repository** (agentic-usertest-monorepo) itself.

These are loaded via `.usertest/catalog.yaml` when this repo is the *target* under test.

## How to use

- When running usertest against this repo as a target, select a mission ID from `.usertest/missions/*.mission.md`.
- These missions reuse the runner’s built-in prompt templates and report schemas (see `configs/`).

Tip: many missions are tagged `requires_write` because they generate artifacts (runs, compiled outputs, snapshots).
