---
id: review_backlog_implementation_pr_v1
name: Review a Backlog Implementation PR (v1)
extends: null
tags: [builtin, backlog, review, implementation]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
requires_shell: true
requires_edits: false
---

## Goal

Review a PR-backed implementation of an already-selected backlog ticket.

## Review boundary

- Do **not** re-decide the backlog ticket or propose a different solution unless the PR clearly diverges from the ticket.
- Do **not** merge the PR.
- Do **not** modify repository source files.
- Review only:
  - whether the implementation stays aligned with the chosen ticket approach
  - whether the scope is appropriate and free of unnecessary additions
  - whether there are implementation defects, regressions, or missing follow-through
  - whether the PR is ready to merge given the supplied CI state

## Inputs

The append system prompt contains:
- the ticket markdown
- handoff metadata from the implementation run
- current PR metadata
- current CI/check status
- changed-file list
- a PR diff excerpt

Treat that content as the source of truth for the review.

## Required output

Use `task_run_v1` and set `report.extensions.review_summary` to an object with:

- `review_decision`: `approved` | `changes_requested` | `blocked`
- `approach_alignment`: `aligned` | `diverged` | `unclear`
- `scope_assessment`: `appropriate` | `excessive` | `unclear`
- `rationale`: short explanation of the decision

Use `issues[]` for concrete findings. Put blocking findings there instead of hiding them in prose.

## Approach

1) Compare the PR to the ticket's selected approach and success criteria.
2) Check whether any changed files or additions are unnecessary for the ticket.
3) Check for implementation defects, regressions, missing tests/docs, or incomplete follow-through.
4) Ground the decision in the current CI and PR state provided in the prompt.
5) Produce a clear approve / changes_requested / blocked outcome.
