---
id: self_reports_pipeline_offline
name: Run the Reports Pipeline (Compile → Analyze → Backlog/Intent Dry-Run)
extends: null
tags: [selftest, ux, ops, reporting, p1, requires_write]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

Evaluate the UX of the **operator workflow** for turning many run outputs into actionable artifacts.

Specifically, exercise the `usertest reports ...` pipeline in a way that is safe/offline:

- compile report history
- analyze recurring issues
- generate backlog/intent/review artifacts using `--dry-run` where LLM calls would happen

## Scenario

You have a directory of past runs (real or fixtures) and you want to produce:

- a compiled JSONL history
- an issue analysis summary
- backlog/intent/review/export scaffolding artifacts (at least one of these stages)

## Tasks

- Identify or construct a small runs directory with at least **2** runs (fixtures are fine).
- Run `reports compile` and `reports analyze`.
- Then run at least **one** of:
  - `reports backlog --dry-run`
  - `reports intent-snapshot --dry-run` (or without summary)
  - `reports review-ux --dry-run`
  - `reports export-tickets` (only if you have suitable staged backlog data)

You’re not trying to get “perfect” analysis output; you’re assessing whether a user can:

- figure out the right command sequence
- understand where outputs go
- understand what inputs each stage expects

## Evidence to include in your report (measurable)

- The runs directory you used.
- The exact commands you ran.
- Paths to at least **3** generated output artifacts (e.g., compiled JSONL, analysis JSON/MD, atoms JSON, intent snapshot JSON, prompt artifacts).
- A brief description of what each artifact is for.

## UX focus prompts

- Is the “happy path” obvious from docs and `--help`?
- Are output filenames predictable?
- Are the `--dry-run`/`--resume`/`--force` semantics clear?
- What’s the first thing you’d simplify for a weekly operator workflow?

## Stop conditions

If you hit blockers, do at most two fixes, then stop with evidence.
