# Repo-specific usertest missions

This folder contains **repo-specific** missions/personas intended for usertesting **this repository** (agentic-usertest-monorepo) itself.

These are loaded via `.usertest/catalog.yaml` when this repo is the *target* under test.

## Defaults for this repo

- Persona: `repo_adoption_gatekeeper`
- Mission: `self_end_to_end_run_single_target`

That default pair is meant to exercise the repo’s primary user journey rather than a minimal sign-of-life probe.

## How to use

- When running usertest against this repo as a target, select a mission ID from `.usertest/missions/*.mission.md`.
- These missions reuse the runner’s built-in prompt templates and report schemas (see `configs/`).
- The quick smoke mission remains available for preflight, but it is not the default adoption test.

Tip: many missions are tagged `requires_write` because they generate artifacts (runs, compiled outputs, snapshots).
