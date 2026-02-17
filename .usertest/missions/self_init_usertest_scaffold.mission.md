---
id: self_init_usertest_scaffold
name: Initialize a Target Repo with init-usertest and Add a Local Override
extends: null
tags: [selftest, ux, setup, customization, p1, requires_write]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Evaluate the UX of setting up a **target repo** to work well with this runner:

- generating the `.usertest/` scaffold
- understanding what the scaffold does
- making a small, realistic customization (local mission/persona override)

This is the “maintainer of the *target repo*” perspective.

## Scenario

You own a target project and want to:

- opt into usertesting
- add one repo-specific mission or persona without modifying the runner’s built-in catalog

## Tasks

- Create (or reuse) a tiny local repo directory to act as a target.
- Run `init-usertest` against it.
- Inspect the generated files and explain what each is for.
- Make one small customization that proves you understand the mechanism:
  - e.g., add a `missions_dirs` entry and a tiny `.mission.md` file, or add a custom persona.
- Confirm the runner can discover your override (e.g., via `missions list --repo ...` / `personas list --repo ...`).

## Evidence to include in your report (measurable)

- The filesystem path to the target repo you created.
- The path to the generated `.usertest/` directory.
- A snippet of the generated `catalog.yaml` and what you changed.
- Proof the custom mission/persona is discoverable (a CLI output snippet that shows it listed).
- At least **3** UX observations:
  - how discoverable this feature is
  - what was confusing about the override model
  - what docs or templates would make it faster

## Stop conditions

If you hit a blocker, do at most two fixes, then stop with evidence.
