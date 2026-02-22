# ADR: `usertest smoke` shortcut command

## Status

Rejected (2026-02-19)

## Context

There was a proposal to add a new top-level CLI entry point, `usertest smoke`, as a single obvious onboarding command.

This repo already provides a “single command” onboarding path via:

- `scripts/smoke.sh` (POSIX shells)
- `scripts/smoke.ps1` (PowerShell)

The repo intent explicitly prefers a small number of composable commands and cautions against adding new top-level
commands for mission-local friction when docs/examples or parameterization can address the issue (`configs/repo_intent.md`).

## Decision

Do not add a new top-level `usertest smoke` command.

Instead, keep improving discoverability and reliability of the existing smoke scripts and the README quickstart path.

## Consequences

- No new CLI surface area or long-term maintenance burden for a thin wrapper command.
- Onboarding remains “one obvious command per OS” via the smoke scripts, with docs as the primary discovery mechanism.

