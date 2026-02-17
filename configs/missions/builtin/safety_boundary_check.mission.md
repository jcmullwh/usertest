---
id: safety_boundary_check
name: Safety Boundary Check (Draft vs Share/Publish)
extends: null
tags: [builtin, generic, safety]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: boundary_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Determine whether the workflow has a clear, safe separation between â€œproduce/export locallyâ€ and any â€œshare/publish externallyâ€ action.

## Approach

1) Identify operations that might cause external effects:
   - share, publish, upload, sync, deploy, send, push
2) Identify whether there are dry-runs, confirmations, or explicit â€œare you sureâ€ steps.
3) Identify how a user can reliably stay in â€œdraft/export onlyâ€ mode.

## Constraints

- Do not actually publish/upload.
- Prefer discovery by documentation/config/flags over triggering risky actions.
