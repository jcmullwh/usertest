---
id: maint_scaffold_toolchain_workflow
name: "Maintainer Workflow: Use the Scaffold Toolchain to Run Lint/Test"
extends: null
tags: [selftest, maintainer, dx, p1, requires_write]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Evaluate the **maintainer/developer experience** of working in this monorepo:

- discovering the preferred dev workflow
- running installs/tests/lints in the intended way
- understanding how projects are organized and automated

This is not about whether tests pass; it’s about whether a maintainer can figure out how to run them quickly.

## Scenario

You are a new contributor asked to:

- install dependencies for *one* project (pick the CLI app or one library)
- run that project’s tests and/or lint

## Tasks

- Find the repo’s recommended dev workflow (README, docs, scripts).
- Identify the “project registry” / how the monorepo defines packages.
- Attempt to run the intended install + test path for one project.

If tools are missing (e.g., `pdm`), treat that as a UX signal: capture the failure and what you wish the docs told you.

## Evidence to include in your report (measurable)

- The project you chose (path/name).
- The exact commands you tried.
- A short snippet of output for:
  - the first command that worked
  - the first command that failed (if any)
- A short “runbook” you would paste into CONTRIBUTING.md for a teammate.
- At least **4** maintainer UX observations (prereqs clarity, time-to-first-test, confusing scripts, etc.).

## Stop conditions

Stop after you’ve either:

- successfully run one project’s tests/lint, or
- captured a concrete blocker + minimal remediation steps.
