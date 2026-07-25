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

- Do **not** re-decide the backlog ticket or propose a different solution unless the evidence shows that the researched mechanism is wrong or the PR diverges from it.
- Do **not** merge the PR.
- Do **not** modify repository source files.
- Review in this order:
  - whether the diff changes the researched failure mechanism or merely suppresses a visible symptom
  - whether verification exercises the ticket's bound original-scenario oracle
  - which causal paths can still reproduce the problem after the change
  - whether there are implementation defects, regressions, or missing follow-through
  - whether the PR is ready to merge given the supplied CI state
  - whether any extra breadth is unnecessary; scope is a brief secondary advisory, not proof of causal correctness

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
- `mechanism_assessment`: `mechanism_addressed` | `symptom_only` | `unclear`
- `original_scenario_oracle`: `exercised` | `not_exercised` | `unclear`
- `causal_path_assessment`: `closed` | `residual` | `unclear`
- `remaining_causal_paths`: array naming every known residual causal path; empty only when none remain
- `scope_assessment`: `appropriate` | `excessive` | `unclear`
- `rationale`: short explanation of the decision

Approval requires `mechanism_addressed`, `exercised`, and `closed`. Use `issues[]` for concrete findings. Put symptom-only changes, missing oracle coverage, residual causal paths, and other blocking findings there instead of hiding them in prose.

`review_decision` is the causal/code acceptance judgment. The runner computes mutable merge readiness separately. A draft PR, pending CI, an infrastructure failure, or a failure already present on the base branch makes the current PR not merge-ready, but must not by itself change an otherwise sound implementation to `changes_requested` or `blocked`. A CI failure caused by the reviewed diff is an implementation defect and should affect the decision.

## Approach

1) Reconstruct the researched mechanism and the selected intervention from the ticket evidence.
2) Trace the diff through that mechanism and decide whether it closes the cause or only a symptom.
3) Match verification to the bound original-scenario oracle; a nearby unit test or generic green suite is not a replay of that oracle.
4) Enumerate any causal path that remains open, including bypasses, alternate callers, compatibility paths, and runtime-only paths.
5) Check for implementation defects, regressions, missing tests/docs, or incomplete follow-through.
6) Treat extra paths and wider changes as a short scope advisory after causal review.
7) Use current CI and PR state to identify implementation-caused failures and report operational readiness separately from the causal/code decision.

## Delegation guidance

Use delegation only when it helps preserve parent context or improve independent review coverage. Good candidates are:

- broad read-only exploration of large files or cross-module contracts
- test failure triage and log summarization
- independent review of implementation risks
- narrow investigation of one module or workflow

Do not delegate small, obvious reviews where delegation overhead would add noise. When you do delegate, require a concise
summary back to the parent covering findings, paths, risks, and recommended review disposition; keep raw broad-source
dumps, full logs, and copied file contents out of the parent context unless a short excerpt is essential evidence.

Delegation is not a scope gate. If no delegation tool or capability is available, perform the full review yourself rather
than under-scoping, skipping necessary investigation, or treating the lack of delegation as a reason to leave review
criteria unchecked.
