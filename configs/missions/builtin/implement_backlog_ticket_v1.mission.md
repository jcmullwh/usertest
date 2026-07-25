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

The ticket content is provided as "append system prompt" text and is saved as
`append_system_prompt.md` in the workspace root (the agent working directory) for in-run access.
The canonical staged artifact is also recorded under `<run_dir>/agent_prompts/append_system_prompt.md`.
Treat the ticket content in this prompt (and `append_system_prompt.md` when present)
as the source-of-truth requirements for this run.

If `append_system_prompt.md` is missing or unreadable, do **not** spend time scanning the repo for it: the full
ticket text is already included in this prompt as append system prompt content. Proceed using that text.

## Approach

1) Read the ticket carefully and restate the concrete requirements.
2) Make the smallest, most direct code change that satisfies the ticket.
3) Run relevant validation commands (tests, lint, or a targeted repro) while iterating, and capture:
   - the exact commands you ran
   - the results (including failures)
4) If the prompt includes a runner-provided final handoff verification command, treat that as the
   canonical final gate:
   - run it once you believe the work is complete
   - it blocks until verification completes and must pass before you finish
   - if it fails, fix the issue and run it again
   - once it passes, do not make further workspace changes before finishing
5) If you cannot fully complete the ticket:
   - clearly describe what is blocked
   - propose the smallest next steps a human should take

## Delegation guidance

Use delegation only when it helps preserve parent context or improve independent coverage. Good candidates are:

- broad read-only exploration of large files or cross-module contracts
- test failure triage and log summarization
- independent review of implementation risks
- narrow investigation of one module or workflow

Do not delegate small, obvious changes where delegation overhead would add noise. When you do delegate, require a concise
summary back to the parent covering findings, paths, risks, and recommended next steps; keep raw broad-source dumps, full
logs, and copied file contents out of the parent context unless a short excerpt is essential evidence.

Delegation is not a scope gate. If no delegation tool or capability is available, complete the full ticket yourself rather
than under-scoping, skipping broad investigation, or treating the lack of delegation as a reason to leave acceptance
criteria unmet.

## Constraints

- Prefer minimal diffs; avoid unrelated refactors.
- Do not change external behavior unless the ticket explicitly asks for it.
- If a change impacts user-visible workflows, update docs/tests accordingly.
