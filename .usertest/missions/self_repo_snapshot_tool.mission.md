---
id: self_repo_snapshot_tool
name: Create a Repo Snapshot (Operator Utility UX)
extends: null
tags: [selftest, ux, tooling, p2, requires_write]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Evaluate the UX of the repo’s **snapshot utility** (`tools/snapshot_repo.py`):

- Can you discover it?
- Is `--help` sufficient?
- Does it create an artifact you can confidently share?
- Are failure cases (missing git, bad paths) explained clearly?

## Scenario

You want to share a “clean” copy of the repo (respecting ignore rules) with a teammate or an external system.

## Tasks

- Find the snapshot tool and read its `--help`.
- Attempt to create a snapshot zip for this repo (or, if this environment isn’t a git checkout, report that limitation clearly).
- Inspect the produced zip at a high level (e.g., file count / spot check a few entries) to confirm it seems reasonable.

## Evidence to include in your report (measurable)

- The exact snapshot command you ran (or attempted).
- The output path to the zip artifact (if created).
- A snippet of tool output showing the snapshot plan summary.
- One confirmation check you performed on the zip contents.
- At least **3** UX observations (discoverability, flags, defaults, error messages).

## Stop conditions

If the tool cannot run (e.g., missing git metadata), stop after capturing concrete error output and explain what the doc/tool could do to guide the user.
