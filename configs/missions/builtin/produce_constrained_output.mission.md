---
id: produce_constrained_output
name: Produce an Output Under Constraints
extends: null
tags: [builtin, generic, constraints]
requires_shell: true
requires_edits: true
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: constrained_output_v1.schema.json
---

## Goal

Produce an output that satisfies explicit constraints, using the project’s supported configuration mechanisms.

## Constraints to enforce (use best-effort if not fully applicable)

- Output should be limited in size or scope (small, minimal, or “sample-scale”).
- Output should be in a specific form if configurable (format, structure, verbosity level).
- The workflow should be repeatable.

## Approach

1) First produce a baseline output (even if it doesn’t meet constraints yet).
2) Identify 1–3 configuration “knobs” that plausibly control the constraints.
3) Apply the smallest change that moves toward compliance, then re-run.

## Safety constraints

- Do not publish or upload.
- Prefer reversible config changes.

## Stop conditions

If constraints cannot be satisfied due to missing support, return a report that explains why and what would be needed.
