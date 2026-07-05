# Stage 4 guidance: solution optioning

## Goal

Produce one solution option per configured family for each researched problem. Options
are grounded in the research dossier and must stay within the configured taxonomy.

## Duration guardrail

Stage artifacts and tool calls must not invent sleeps, polling delays, retry delays, or
timeout values. If waiting behavior is part of the path being analyzed, use an existing
repo-configured value or document the limitation instead of making up a duration. This
also applies to tool calls: do not pass `timeout`, `timeout_ms`, or similar duration
parameters to shell/tool invocations unless the assigned evidence includes that exact
configured value.

## Option families (from configs/backlog_taxonomy.json)

The current three families are:
1. `most_direct` – smallest targeted change addressing the specific instance
2. `most_robust` – adds defense-in-depth or broader correctness
3. `most_comprehensive` – addresses the problem class, not just the instance

Every output must include exactly one option per configured family. Do not invent new
families. Do not omit a family. If a family genuinely does not apply, explain why in
the option's tradeoffs and still include the entry.

## What to favor

- Options grounded in the research dossier, not in speculation.
- Options that reflect what the repo actually does (small, composable changes unless
  breadth is compelling based on research).
- Options that include honest tradeoffs, including recurrence prevention and test
  implications.
- Options where the change-surface hypothesis is realistic given evidence breadth.
- Options that solve the underlying mechanism when research shows repeated or shared
  causes, not just the exact observed symptom.

## What to avoid

- Do not use banned steering terms: fastest, quickest, easiest, simplest, lowest-effort.
  Use family labels instead to express tradeoffs.
- Do not invent a solution not grounded in the research dossier.
- Do not include `selected_solution` in this stage's output.
- Do not describe implementation steps as if selection has happened.
- Do not frame a hardcoded branch, one-off exception, or narrow special-case workaround
  as a good option unless the research dossier supports an isolated instance or an
  intentional boundary.

## Output contract

Each option must include:
- `option_id` (stable, e.g. `option:readme-quickstart:most_direct`)
- `problem_id` (matching stage-1 record)
- `family_id` (one of the configured family IDs from taxonomy)
- `summary`
- `tradeoffs`
- `recurrence_prevention`
- `change_surface_hypothesis`
- `test_implications`
- `rationale` (grounded in research dossier)
- `option_status` = `"optioned"`

## Relation-review guidance

At this stage the relation reviewer may merge option sets from two problems when research
showed they share a root cause and a single option set can address both. The reviewer may
also split an option set when research showed multiple independent causes that deserve
separate option sets.
