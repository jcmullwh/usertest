---
id: create_repeatable_recipe
name: Create a Repeatable Recipe for Future Runs
extends: null
tags: [builtin, generic, runbook]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: runbook_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Produce a concise, repeatable â€œrecipeâ€ that someone else (or automation) can run later to reproduce the same kind of output reliably.

## Approach

1) Identify the minimal inputs and prerequisites.
2) Write a short runbook:
   - prerequisites
   - exact commands or steps
   - how to validate success
   - common failure modes and how to recover
3) Prefer stable interfaces and avoid fragile assumptions.

## Constraints

- No publishing or deployment.
- Avoid steps that require interactive UI unless unavoidable; if unavoidable, document the manual step clearly.
