---
id: troubleshoot_blocker
name: Troubleshoot a Blocker and Provide a Fix Path
extends: null
tags: [builtin, generic, troubleshoot]
requires_shell: true
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: troubleshoot_v1.schema.json
requires_edits: true
---

## Goal

When a workflow is blocked by an error or unclear step, produce the shortest credible fix path and a clear diagnostic record.

## Approach

1) Attempt a reasonable â€œhappy pathâ€ workflow (small scope).
2) If it fails, capture:
   - the failing step
   - the error evidence
3) Attempt up to two targeted fixes:
   - fixes must be directly suggested by error messages or documented guidance
4) If unresolved, propose next diagnostic steps that are safe and likely to clarify the issue.

## Constraints

- No publish/upload.
- Avoid destructive operations.
- Keep changes minimal and reversible.
