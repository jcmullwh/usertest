---
id: produce_default_output
name: Produce a Usable Output with Defaults
extends: null
tags: [builtin, generic, first_output]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Produce at least one usable output artifact or observable result using the most straightforward default workflow.

## What counts as â€œusable outputâ€

Any one of the following, depending on what the project is:
- a generated file/artifact (export, report, build output, transformed data, compiled binary, etc.)
- a running local service that responds to a local check
- a CLI command that produces a meaningful result (not just help text)
- a minimal example run that produces visible output

## Approach (choose the best fit)

1) Prefer project-provided guidance and examples.
2) If there is a â€œquickstartâ€ or â€œexample,â€ run that path.
3) If the project is primarily a library, use the smallest documented example to produce output.
4) If the project is a service/app, start it locally and validate with a simple local request or check.

## Constraints

- Do not publish, deploy, upload, or perform irreversible actions.
- Minimize configuration. Defaults first.

## Stop conditions

If you hit setup or runtime blockers, attempt at most 1â€“2 targeted fixes suggested by errors/docs, then return a partial report.
