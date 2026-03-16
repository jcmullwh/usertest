# ADR: `usertest smoke` shortcut command

## Status

Rejected (2026-02-19)

## Context

There was a proposal to add a new top-level CLI entry point, `usertest smoke`, as a single obvious onboarding command.

This repo already provides onboarding commands without adding a new top-level CLI:

- canonical newcomer-first path: `scripts/offline_first_success.sh` / `scripts/offline_first_success.ps1`
- secondary developer sanity check: `scripts/smoke.sh` / `scripts/smoke.ps1`
- diagnostic alternate: `scripts/doctor.sh` / `scripts/doctor.ps1`

Canonical wrapper commands:

- Windows PowerShell: `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline_first_success.ps1`
- macOS/Linux: `bash ./scripts/offline_first_success.sh`
- Doctor (Windows): `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\doctor.ps1`
- Doctor (macOS/Linux): `bash ./scripts/doctor.sh`
- Smoke (Windows): `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1`
- Smoke (macOS/Linux): `bash ./scripts/smoke.sh`

The repo intent explicitly prefers a small number of composable commands and cautions against adding new top-level
commands for mission-local friction when docs/examples or parameterization can address the issue (`configs/repo_intent.md`).

## Decision

Do not add a new top-level `usertest smoke` command.

Instead, keep improving discoverability and reliability of the existing wrapper scripts and the
README quickstart path, with one explicit newcomer-first precedence order.

## Consequences

- No new CLI surface area or long-term maintenance burden for a thin wrapper command.
- Onboarding keeps one explicit newcomer-first path per OS via `offline_first_success.*`.
- `smoke.*` remains a secondary developer sanity check instead of a competing first step.
- `doctor.*` remains the explicit diagnostic alternate when setup guidance is needed first.
