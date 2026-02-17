---
id: self_batch_targets_prevalidation
name: Author a Batch Targets YAML and Evaluate Validation/Error UX
extends: null
tags: [selftest, ux, batch, p1, requires_write]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Evaluate the UX of **batch mode configuration**:

- can a user write a `targets.yaml` correctly?
- does the tool fail fast with clear, actionable diagnostics when it’s wrong?
- can the user fix it quickly without reading source code?

## Scenario

You want to run usertests across multiple targets via `usertest batch --targets <file>`.

## Tasks

- Start from whatever examples/docs you can find.
- Create a targets YAML with at least **two** targets.
- Intentionally include **at least two** common authoring mistakes (e.g., unknown agent name, missing required field, bad path, invalid YAML structure).
- Run `usertest batch` to observe the validation errors.
- Fix the file until the *initial validation layer* is satisfied.

You do **not** need to actually run a full batch of agent executions; the focus is on the authoring and validation experience.

## Evidence to include in your report (measurable)

- The path to the YAML file you created.
- A short snippet of the “broken” YAML.
- The exact batch command you ran.
- A verbatim snippet of at least **one** validation/error message.
- A short snippet of the “fixed” YAML.
- A note on whether you could reach the point where the tool starts attempting real runs, and if not, why.

## UX focus prompts

- Are error messages pointing to the right file/line/field?
- Do errors suggest the fix?
- Is there an obvious “schema” or docs reference for the targets format?
- What one improvement would make batch setup dramatically easier?

## Stop conditions

Stop after you’ve demonstrated at least one “bad YAML → clear error → fix → improved state” loop.
