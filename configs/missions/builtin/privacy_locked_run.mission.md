---
id: privacy_locked_run
name: Privacy-Locked Run (No Unintended External Calls)
extends: null
tags: [builtin, generic, privacy]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: boundary_v1.schema.json
requires_shell: true
requires_edits: false
---

## Goal

Execute a useful workflow while minimizing external side effects: avoid unintended network calls, uploads, telemetry, and publishing.

Note: this mission constrains the *target workflow* (commands the agent chooses to run against the repo). It does **not**
make the run “offline” in the sense of preventing the agent CLI from reaching its hosted API provider. In this repo’s
Docker execution backend, the agent CLI runs inside the container; setting `--exec-network none` will prevent hosted
agents (Codex/Claude/Gemini) from running.

## Approach

1) Identify any documented â€œoffline,â€ â€œlocal-only,â€ or â€œno-telemetryâ€ modes.
2) Prefer workflows that can run fully locally.
3) If a network call appears necessary, describe what, why, and how to disable it. Do not proceed unless the mission constraints allow it.

## Constraints

- No publish/deploy/upload.
- Avoid credentials.
- Prefer read-only inspection if uncertainty is high.

## Output requirements

Return a boundary-focused report: what would or did touch network, files, credentials, or external services.
