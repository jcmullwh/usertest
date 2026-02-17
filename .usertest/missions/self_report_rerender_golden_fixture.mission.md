---
id: self_report_rerender_golden_fixture
name: Re-render a Report and Recompute Metrics (Golden Fixture)
extends: null
tags: [selftest, ux, reporting, p0, requires_write]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Validate the UX of the **“I already have a run directory; how do I regenerate the report/metrics?”** workflow.

This should work without needing any external agent credentials by using the repo’s **golden run fixture(s)**.

## Scenario

A teammate sent you a run directory zip. You want to:

- regenerate `report.md`
- optionally recompute `normalized_events.jsonl` + `metrics.json` from `raw_events.jsonl`

## Tasks

- Find an existing fixture run directory in the repo.
- Re-render the markdown report.
- Then re-run with the option that recomputes metrics/events.
- Inspect the updated artifacts and confirm you can explain what changed.

## Evidence to include in your report (measurable)

- The run directory path you used (prefer a copy of a fixture to avoid editing tracked files).
- The exact commands you ran.
- A snippet of the CLI output showing success.
- One before/after confirmation (e.g., a file timestamp change, or a metric value you can explain).
- At least **3** UX observations about:
  - discoverability of this command
  - clarity of flags / naming
  - readability of the regenerated report

## Stop conditions

If you hit a blocker (missing dependency, unclear usage):

- attempt at most **two** fixes
- then stop with evidence + suggested remediation.
