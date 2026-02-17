---
id: resilience_under_constraints
name: Resilience Under Constraints
extends: null
tags: [builtin, generic, reliability]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: troubleshoot_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Assess whether the target can still produce a useful outcome when common constraints are present (missing tools, reduced permissions, flaky external dependencies, or partial configuration).

## Approach

1) Run a standard workflow.
2) When failure occurs, classify the failure domain (environment, dependency, permissions, runtime, or documentation gap).
3) Apply up to two low-risk remediations and re-test.
4) Produce a resilience summary: what degrades gracefully, what hard-fails, and why.

## Constraints

- Avoid destructive operations.
- Prioritize diagnostic clarity over broad exploratory changes.
- Keep remediation steps concrete and reproducible.

## Expected output

- Primary blocker chain
- What worked vs what failed under constraints
- Targeted improvement recommendations ordered by impact on reliability
