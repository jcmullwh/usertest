# Stage 4 guidance: solution optioning (internal maintenance)

## Goal

Produce one solution option per configured family for each researched problem. Options
are grounded in the research dossier and must stay within the configured taxonomy.

## Option families (from configs/backlog_taxonomy.json)

The current three families are:
1. `most_direct` - smallest targeted change addressing the specific instance
2. `most_robust` - adds defense-in-depth or broader correctness
3. `most_comprehensive` - addresses the problem class, not just the instance

Every output must include exactly one option per configured family. Do not invent new
families. Do not omit a family. If a family genuinely does not apply, explain why in
the option's tradeoffs and still include the entry.

## What to favor

- Options grounded in the research dossier, not in speculation.
- Options that reflect what the repo actually does (small, composable changes unless
  breadth is compelling based on research).
- Options that include honest tradeoffs, including recurrence prevention and test
  implications.
- In internal-maintenance mode, repeated observations across runs and agents are valid
  evidence for a class-level internal fix even when missions, targets, and repo_inputs
  are structurally constant.
- `most_comprehensive` may still stay within existing commands, flags, and config
  surfaces when it addresses the problem class rather than only the observed instance.

## What to avoid

- Do not use banned steering terms: fastest, quickest, easiest, simplest, lowest-effort.
  Use family labels instead to express tradeoffs.
- Do not invent a solution not grounded in the research dossier.
- Do not include `selected_solution` in this stage's output.
- Do not describe implementation steps as if selection has happened.
- Do not smuggle new top-level surface into `most_comprehensive` without cross-context
  evidence breadth.

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
