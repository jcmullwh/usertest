---
id: self_quickstart_minimal_smoke
name: Repo Quickstart to Representative Result
extends: null
tags: [selftest, ux, onboarding, p0]
execution_mode: single_pass_inline_report
prompt_template: inline_report_evidence_v1.prompt.md
report_schema: task_run_evidence_v1.schema.json
---

## Goal

From a fresh checkout, reach the shortest representative result that demonstrates this repo’s main value *as a tool a person would actually use*.

This is a UX mission: the point is to see whether a new user can **find the right entry point**, **follow the documented setup**, and **reach a tangible result quickly**.

## What counts as a representative result

Choose the fastest workflow that still exercises the product promise. Good examples include:

- running `usertest` against a small target and locating the generated run directory
- executing a documented CLI flow that produces a report or metrics artifact created by this run
- running a smoke-sized workflow that still touches the main install-to-result path

## Non-success cases

These do not count by themselves:

- `--help` only
- import-only checks
- opening checked-in golden fixtures
- rerendering an existing report without a fresh run
- a test or subcommand that does not demonstrate the repo’s main user journey

## Suggested approach (not a script)

- Start where a real user would: README, `apps/usertest/README.md`, and CLI `--help`.
- Prefer the documented setup path first (don’t invent your own install process unless docs are broken).
- Aim for a “representative result” loop:
  1) install/setup
  2) run one representative command
  3) locate the output artifact and explain where it lives
  4) perform one sanity check on the result

## Evidence to include in your report (measurable)

- **Commands run**: include the exact commands you used for setup and for the successful run.
- **Representative rationale**: state why the chosen workflow matches the repo’s main value.
- **Output proof**: include a short snippet of the successful command output.
- **Artifact proof**: point to at least one generated or updated file path (or a test output log) that only exists if you actually used this repo.
- **Verification proof**: include one explicit correctness or sanity check.
- **Time/effort proxy**: record the number of commands you had to run before success (and which ones felt like detours).

## UX focus prompts

- Where did you look first, and was it the right place?
- What slowed you down (missing prereqs, confusing docs, unclear defaults, unclear file layout)?
- If you had to guess, what did you guess wrong?
- What single change would most reduce time-to-representative-success?

## Stop conditions

If you hit a blocker:

- attempt at most **two** reasonable fixes driven by the docs/errors
- then stop and report the blocker with concrete evidence (error text + where it occurred), the minimal remediation path, and the next representative step that was blocked.
