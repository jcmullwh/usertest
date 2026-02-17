---
id: self_run_artifacts_orientation
name: Navigate and Explain Run Artifacts
extends: null
tags: [selftest, ux, artifacts, p1]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Validate the **operator/reviewer experience** of interpreting a run directory:

- Can you quickly find “what happened”?
- Can you tell success vs failure paths?
- Are filenames and docs aligned with what’s actually produced?

## Scenario

You have a run directory (it can be a real run under `runs/usertest/...` or a fixture under `examples/golden_runs/...`).

Your job is to produce a short “run directory map” that a teammate could use to debug a failed run without reading source code.

## Tasks

- Find (or generate) one representative run directory.
- Identify the *minimum set* of artifacts needed to answer:
  - what target was tested?
  - what persona/mission/policy was used?
  - what commands were executed?
  - did anything fail? where is the failure evidence?
  - what outputs should I read first?
- Cross-check against `docs/design/run-artifacts.md`.

## Evidence to include in your report (measurable)

- The path to the run directory you analyzed.
- A bullet list describing at least **10** artifact filenames and what they’re for.
- A “triage order” list: the first 5 things you’d open for debugging.
- At least **2** mismatches or ambiguity points (doc vs reality, naming confusion, missing links, etc.).

## UX focus prompts

- Which artifacts are hard to interpret without domain context?
- Are there obvious “summary” artifacts missing?
- Are the docs easy to find from the README?
