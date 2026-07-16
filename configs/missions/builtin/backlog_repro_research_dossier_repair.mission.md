---
id: backlog_repro_research_dossier_repair
name: Backlog Research Dossier Targeted Repair
extends: null
tags: [repo_local, backlog, research, correction]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: troubleshoot_v1.schema.json
requires_shell: false
requires_edits: false
---

## Goal

Correct deterministic structure or reference errors in a retained research dossier without
repeating the investigation or changing its evidence.

This is a short correction turn, not research and not implementation.

## Hard contract

- Do not inspect the repository, run commands, invoke tools, edit files, or create evidence.
- Treat the field hints as likely correction locations, not a closed whitelist. Correlated
  model-owned structure and interpretation changes are allowed when needed to resolve the exact
  validator feedback.
- Preserve retained commands, results, exit codes, inspected files/symbols, and artifact
  references byte-for-byte. The runner will reverify revised interpretations against them.
- An honest downgrade from `evidence_sufficient` to `insufficient_evidence` is allowed when the
  retained evidence cannot support the claim. Never upgrade status or fabricate support.
- Apply only corrections needed for the listed deterministic errors. Do not opportunistically
  rewrite unrelated prose.
- Return the complete baseline dossier, not a JSON Patch and not a partial fragment.
- Do not weaken an honest `insufficient_evidence` or `blocked` result to pass validation.

## Output

Return one complete `troubleshoot_v1` JSON report. Its `extensions.backlog_repro_research`
value must contain the complete corrected dossier. The runner will deterministically validate it,
retain the attempt, and return any remaining or newly exposed errors to this same session while
progress remains plausible.
