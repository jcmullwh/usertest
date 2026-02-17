---
id: self_quickstart_minimal_smoke
name: Repo Quickstart to First Useful Output
extends: null
tags: [selftest, ux, onboarding, p0]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

From a fresh checkout, get to the first **meaningful** output that demonstrates the repo’s main value *as a tool a person would use*.

This is a UX mission: the point is to see whether a new user can **find the right entry point**, **follow the documented setup**, and **reach a tangible result quickly**.

## What counts as a “meaningful output”

Choose the lowest-risk, fastest option that still proves real progress. Any one of:

- a rendered report artifact (e.g., `report.md` generated or re-rendered by the CLI)
- a CLI operation that produces non-trivial output (not just `--help`)
- a small test run that validates a key workflow (prefer a smoke test subset if available)

## Suggested approach (not a script)

- Start where a real user would: README, `apps/usertest/README.md`, and CLI `--help`.
- Prefer the simplest setup path first (don’t invent your own install process unless docs are broken).
- Aim for a “first success” loop:
  1) install/setup
  2) run one meaningful command
  3) locate the output artifact and explain where it lives

## Evidence to include in your report (measurable)

- **Commands run**: include the exact commands you used for setup and for the successful run.
- **Output proof**: include a short snippet of the successful command output.
- **Artifact proof**: point to at least one generated/updated file path (or a test output log) that only exists if you actually used this repo.
- **Time/effort proxy**: record the number of commands you had to run before success (and which ones felt like detours).

## UX focus prompts

- Where did you look first, and was it the right place?
- What slowed you down (missing prereqs, confusing docs, unclear defaults, unclear file layout)?
- If you had to guess, what did you guess wrong?
- What single change would most reduce time-to-first-success?

## Stop conditions

If you hit a blocker:

- attempt at most **two** reasonable fixes driven by the docs/errors
- then stop and report the blocker with concrete evidence (error text + where it occurred) and the minimal remediation path.
