---
id: verify_install_to_result
name: Verify Install to Result Path
extends: null
tags: [builtin, generic, onboarding]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Validate the end-to-end path from setup to one meaningful result using the most representative workflow for the target.

## Approach

1) Identify the canonical setup path (dependencies, environment, credentials if required).
2) Execute one realistic usage flow that produces an observable result.
3) Verify output quality with at least one explicit correctness check.
4) Capture what would let a new user reproduce this outcome quickly.

## Constraints

- Prefer the documented path over ad-hoc shortcuts.
- Keep changes reversible and scoped.
- Do not publish, deploy, or perform irreversible external actions.

## Stop conditions

If blocked, attempt up to two targeted fixes from docs/errors, then return:

- blocker evidence,
- minimal remediation path,
- confidence level for expected success after remediation.
