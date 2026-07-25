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
make the overall run “offline” or provide end-to-end privacy guarantees.

`--exec-network` (when using `--exec-backend docker`) only controls the Docker sandbox container's *runtime* network
(`docker run --network ...`). `docker build` may still pull base images and download dependencies.

In this repo’s Docker execution backend, the agent CLI runs inside the container by default, so setting
`--exec-network none` will also prevent hosted agents (Codex/Claude/Gemini) from reaching their APIs and the run will
fail.

## Approach

1) Identify any documented “offline,” “local-only,” or “no-telemetry” modes.
2) Prefer workflows that can run fully locally.
3) If a network call appears necessary, describe what, why, and how to disable it. Do not proceed unless the mission constraints allow it.

## Constraints

- No publish/deploy/upload.
- Avoid credentials.
- Prefer read-only inspection if uncertainty is high.

## Output requirements

Return a boundary-focused report: what would or did touch network, files, credentials, or external services.
