---
id: verify_install_to_result
name: Verify Install to Representative Result
extends: null
tags: [builtin, generic, onboarding, representative]
execution_mode: single_pass_inline_report
prompt_template: inline_report_evidence_v1.prompt.md
report_schema: task_run_evidence_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Validate the end-to-end path from setup to one representative result using the repo’s primary documented workflow.

## Success requires all of:

- setup was completed, or explicitly proven unnecessary
- one documented/default workflow was executed end-to-end
- one output was generated during this run
- one explicit correctness or sanity check was performed
- the report explains why the chosen workflow is representative of the repo’s main value

## Non-success cases

These do not count as success unless the repo’s product is exactly that:

- `--help`, `--version`, or import-only checks
- dry-runs with no primary result
- reading or reformatting checked-in artifacts
- rerendering existing fixtures without exercising the install-to-result path
- proving a non-critical code path while the main workflow remains untested

## Approach

1) Identify the canonical setup path from docs, examples, or obvious entry points.
2) Execute the shortest representative usage flow that produces a user-visible result.
3) Verify the result with at least one explicit correctness check tied to repo intent.
4) Capture what a new user would need to reproduce the outcome quickly.

## Constraints

- Prefer the documented path over ad-hoc shortcuts.
- Keep changes reversible and scoped.
- Do not publish, deploy, or perform irreversible external actions.

## Stop conditions

If blocked, attempt up to two targeted fixes from docs/errors, then return:

- blocker evidence,
- minimal remediation path,
- the next representative step that was blocked,
- confidence level for expected success after remediation.
