---
id: first_output_smoke
name: First Successful Output (Smoke)
extends: null
tags: [generic, p0]
execution_mode: single_pass_inline_report
prompt_template: default_inline_report.prompt.md
report_schema: default_report.schema.json
requires_shell: true
requires_edits: true
---
Produce a first useful, correct output from the target repository with minimal risk:

- Identify the quickest way to run something meaningful (tests, a build, a CLI help output, etc.).
- Prefer read-only inspection and narrow commands.
- If a full run is too heavy, provide a minimal reproduction or a validated explanation of how to run it.
