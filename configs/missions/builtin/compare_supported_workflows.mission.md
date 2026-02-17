---
id: compare_supported_workflows
name: Compare Supported Workflows
extends: null
tags: [builtin, generic, evaluation]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: variants_v1.schema.json
requires_shell: true
requires_edits: true
---

## Goal

Compare two or more supported ways to achieve the same intended result and identify the most reliable default path.

## Approach

1) Select at least two legitimate workflows documented or implied by the target (for example: CLI vs API, quickstart vs full config, local vs containerized).
2) Execute each workflow to produce equivalent output goals.
3) Compare by reproducibility, setup complexity, runtime stability, and result quality.
4) Recommend a default path and when to choose alternatives.

## Constraints

- Keep comparisons fair (same success criteria).
- Avoid irreversible external side effects.
- Do not optimize for one-off hacks; prefer maintainable workflows.

## Expected output

- Side-by-side workflow comparison
- Preferred default with rationale
- Clear fallback path when the default is blocked
