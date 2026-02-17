---
id: complete_output_smoke
name: Complete Output (Smoke)
extends: null
tags: [generic, p0]
execution_mode: single_pass_inline_report
prompt_template: default_inline_report.prompt.md
report_schema: default_report.schema.json
requires_shell: true
requires_edits: true
---
Produce a correct and complete output from the target repository:

- Identify the standard way to run a full execution such as CLI Command, etc and produce the full output as intended by the repository.
- Ensure that the output is correct and complete as per the repository's purpose.
- This is not a minimal or simulated run; the goal is to produce a full, correct output.
- If execution is blocked (e.g., external dependencies, credentials), use the tools at your disposal to address the blockers (installing dependencies, etc.).
