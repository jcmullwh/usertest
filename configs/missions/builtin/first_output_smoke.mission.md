---
id: first_output_smoke
name: First Output Smoke (Preflight Probe)
extends: null
tags: [generic, p0, preflight]
execution_mode: single_pass_inline_report
prompt_template: default_inline_report.prompt.md
report_schema: default_report.schema.json
requires_shell: true
requires_edits: false
---
Establish a first sign of life or isolate the first concrete blocker with minimal risk.

This mission is a preflight probe. It does **not** establish that the repo works in a representative or adoption-worthy way.

- Identify the quickest low-risk thing to run that confirms an entry point or reveals the first blocker (for example `--help`, `--version`, a tiny smoke test, or a narrow example command).
- Prefer read-only inspection and narrow commands.
- Use this mission to choose the next representative workflow, not to claim end-to-end success.
- If a fuller run appears possible, name the next representative workflow to try.
- If even the probe is blocked, return the smallest reproducible blocker or a validated explanation of the missing prerequisite.

When reporting, keep any adoption-style recommendation tentative because this mission intentionally stops before representative validation.
