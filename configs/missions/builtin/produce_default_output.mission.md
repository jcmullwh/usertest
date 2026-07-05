---
id: produce_default_output
name: Produce a Representative Output with Defaults
extends: null
tags: [builtin, generic, onboarding]
execution_mode: single_pass_inline_report
prompt_template: inline_report_evidence_v1.prompt.md
report_schema: task_run_evidence_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Produce at least one usable, representative output artifact or observable result using the most straightforward documented default workflow.

## Success requires all of:

- a documented or clearly intended default workflow was executed
- the workflow produced a user-visible result during this run
- the result is tied to the repo’s main value rather than a side path
- at least one explicit correctness or sanity check was performed

## Non-success cases

These do not count by themselves unless the repo’s product is exactly that:

- CLI help or version output only
- import-only or dry-run-only checks
- reading or quoting checked-in artifacts
- rerendering existing fixtures without exercising the default workflow
- a tiny test that does not connect to the repo’s main value

## Approach

1) Prefer project-provided quickstarts, examples, or default commands.
2) If the project is primarily a library, run the smallest documented example that produces a real result.
3) If the project is a service/app, start it locally and validate with a simple local check tied to user value.
4) Choose the shortest path that is still representative.

## Constraints

- Do not publish, deploy, upload, or perform irreversible actions.
- Minimize configuration. Defaults first.
- Keep changes reversible and scoped.

## Stop conditions

If you hit setup or runtime blockers, attempt at most 1–2 targeted fixes suggested by errors/docs, then return a partial report with blocker evidence and the next representative step that was blocked.
