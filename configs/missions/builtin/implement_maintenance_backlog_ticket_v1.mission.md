---
id: implement_maintenance_backlog_ticket_v1
name: Implement a Maintenance Backlog Ticket (v1)
extends: null
tags: [builtin, backlog, implement, maintenance]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Implement the requested maintenance change described in the backlog ticket.

## How the ticket is provided

The ticket content is provided as "append system prompt" text and is saved as
`append_system_prompt.md` in the workspace root (the agent working directory) for in-run access.
The canonical staged artifact is also recorded under `<run_dir>/agent_prompts/append_system_prompt.md`.
Treat the ticket content in this prompt (and `append_system_prompt.md` when present)
as the source-of-truth requirements for this run.

If `append_system_prompt.md` is missing or unreadable, do **not** spend time scanning the repo for it: the full
ticket text is already included in this prompt as append system prompt content. Proceed using that text.

## Approach

1) Read the ticket carefully and restate the concrete requirements.
2) Understand the existing mechanism before changing it. Favor the smallest coherent fix that resolves
   the underlying maintenance issue rather than a one-off branch or narrow special case.
3) When a change touches shared behavior, contracts, diagnostics, docs, or tests, treat that follow-through
   as part of the task instead of optional cleanup.
4) Run relevant validation commands (tests, lint, or a targeted repro) while iterating, and capture:
   - the exact commands you ran
   - the results (including failures)
5) If the prompt includes a runner-provided final handoff verification command, treat that as the
   canonical final gate:
   - run it once you believe the work is complete
   - it blocks until verification completes and must pass before you finish
   - if it fails, fix the issue and run it again
   - once it passes, do not make further workspace changes before finishing
6) If you cannot fully complete the ticket:
   - clearly describe what is blocked
   - propose the smallest next steps a human should take

## Constraints

- Prefer maintainable fixes over the narrowest possible diff when those goals conflict.
- Avoid unrelated refactors, but do not leave behind obvious inconsistency in touched areas.
- Do not change external behavior unless the ticket explicitly asks for it or the maintenance fix requires it.
- If a change impacts user-visible workflows, update docs/tests accordingly.
