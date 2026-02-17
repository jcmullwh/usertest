---
id: generate_variants_and_select
name: Generate Variants and Select a Winner
extends: null
tags: [builtin, generic, variants]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: variants_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Generate multiple meaningfully different candidate outputs and select a recommended winner with clear rationale.

## Approach

1) Establish a baseline output.
2) Generate N variants (default N=3) by changing one â€œintent-levelâ€ dimension per variant (for example: structure, verbosity, ordering, emphasis, style, or a key parameter).
3) Compare variants side-by-side using simple criteria: usability, clarity, fidelity to goal, and risk.

## Output requirements

- Provide a shortlist with identifiers for each variant.
- Recommend one winner and explain tradeoffs.
- Provide how to reproduce each variant.
