---
id: batch_process_multiple_inputs
name: Batch Process Multiple Inputs
extends: null
tags: [builtin, generic, batch]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: batch_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Run the workflow on multiple inputs with minimal manual effort, producing a set of outputs and a summary.

## Approach

1) Identify how the project accepts inputs:
   - files, directories, URLs, API calls, configs, etc.
2) Locate or create a small set of inputs (default 3) that are safe and representative:
   - Prefer included examples.
   - Otherwise create minimal synthetic inputs consistent with documented formats.
3) Run the workflow across all inputs:
   - Prefer a built-in batch mode if it exists.
   - Otherwise script or loop safely in the simplest way.

## Constraints

- Keep inputs small.
- Do not publish or upload.
- Capture a mapping from input â†’ output.

## Stop conditions

If batch mode is not feasible, return what prevents it and a minimal path toward enabling it.
