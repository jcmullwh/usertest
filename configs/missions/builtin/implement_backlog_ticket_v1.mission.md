---
id: implement_backlog_ticket_v1
name: Implement a Backlog Ticket (v1)
extends: null
tags: [builtin, backlog, implement]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Implement the requested change described in the backlog ticket.

## How the ticket is provided

The ticket content is provided as "append system prompt" text and is saved as `append_system_prompt.md`
in the workspace root (the agent working directory). Treat it as the source-of-truth requirements for this run.

## Approach

1) Read the ticket carefully and restate the concrete requirements.
2) Make the smallest, most direct code change that satisfies the ticket.
3) Run relevant validation commands (tests, lint, or a targeted repro) and capture:
   - the exact commands you ran
   - the results (including failures)
4) If you cannot fully complete the ticket:
   - clearly describe what is blocked
   - propose the smallest next steps a human should take

## Constraints

- Prefer minimal diffs; avoid unrelated refactors.
- Do not change external behavior unless the ticket explicitly asks for it.
- If a change impacts user-visible workflows, update docs/tests accordingly.
