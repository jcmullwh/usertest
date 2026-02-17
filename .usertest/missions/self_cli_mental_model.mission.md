---
id: self_cli_mental_model
name: Build a Minimal Mental Model of the CLI
extends: null
tags: [selftest, ux, discoverability, p0]
execution_mode: single_pass_inline_report
prompt_template: inline_report_v1.prompt.md
report_schema: task_run_v1.schema.json
---

## Goal

As if you were onboarding a teammate, build a **minimal mental model** of this repo’s user-facing surface:

- what the `usertest` CLI does
- which subcommands exist and when you would use each
- where configuration lives
- what artifacts it creates and how you’re expected to consume them

This mission is about **discoverability** and **information architecture**: can a user find the right command + flag without reading the whole codebase?

## Deliverable

Create a short “cheat sheet” that answers these questions with concrete references (CLI help output and/or repo docs):

1) What is the shortest path to run a single target repo and where do outputs land?
2) How do personas and missions affect a run, and how do you list what’s available?
3) How do policies (`safe` / `inspect` / `write`) change behavior?
4) How would you rerender or recompute a report for an existing run?
5) What is the difference between `run`, `batch`, `report`, and `reports ...`?
6) If you want compiled history/backlog/export, what sequence of `reports` commands would you run?

## Constraints

- Prefer reading docs / CLI help over code spelunking.
- Avoid “just-so” explanations: tie each claim to an actual command or file in the repo.

## Evidence to include in your report (measurable)

- A cheat sheet with:
  - at least **8** specific command examples (subcommand + 1–3 flags) written exactly as you’d run them
  - at least **5** referenced flags/options explained in plain language
- Evidence that you actually consulted the repo:
  - cite at least **3** file paths you read (README/docs/config)
  - include at least **2** short snippets from CLI help output or docs that were critical to your understanding

## UX focus prompts

- Which command names or subcommand groupings were surprising?
- What do you wish `--help` told you earlier?
- What “next step” links should exist between docs pages?
- Any inconsistencies between README, docs, and actual CLI flags?
