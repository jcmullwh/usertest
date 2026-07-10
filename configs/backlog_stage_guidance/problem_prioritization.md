# Stage 2 guidance: problem prioritization

## Goal

Rank every canonical problem for deeper research. Stage 1 has already separated noise,
duplicates, proposals, and unresolved evidence from canonical problems, so priority
controls research order and urgency—not whether a real problem is ever researched.
Every valid decision must use `selected_for_research=true`.

## Duration guardrail

Stage artifacts and tool calls must not invent sleeps, polling delays, retry delays, or
timeout values. If waiting behavior is part of the path being analyzed, use an existing
repo-configured value or document the limitation instead of making up a duration. This
also applies to tool calls: do not pass `timeout`, `timeout_ms`, or similar duration
parameters to shell/tool invocations unless the assigned evidence includes that exact
configured value.

## Priority buckets

| Bucket | Meaning |
|--------|---------|
| `p0` | Blocking: prevents a majority of runs from completing normally. Research now. |
| `p1` | High: recurring issue that meaningfully degrades quality or confidence. Research now. |
| `p2` | Medium: real problem with limited evidence breadth. Research after p0/p1. |
| `p3` | Low: isolated or low-impact problem. Research after higher-priority cases. |
| `watch` | Uncertain mechanism or impact. Research to resolve the uncertainty after ranked cases. |

## What to favor for research now (p0 / p1)

- Problems with evidence from multiple distinct runs and multiple distinct agents.
- Problems where severity is `high` or `blocker` across multiple evidence atoms.
- Problems where user impact is severe (blocked from proceeding, incorrect output).
- Problems that appear in more than one mission or target context.
- Problems with strong signal from `run_failure_event` or `report_validation_error` atoms.

## What to rank later without suppressing

- Single-run observations of medium severity without corroboration.
- Problems where confidence is low and evidence is sparse.
- Problems that appear only in one agent or one persona context.
- Problems that may be environmental rather than systematic.

These signals lower ordering priority; they do not make a canonical problem ineligible.
Research is responsible for determining whether the evidence establishes a mechanism.

## What to avoid

- Do not propose solutions. This stage decides research priority only.
- Do not invent new problem IDs; work only with the problem records from stage 1.
- Do not silently drop or indefinitely defer problems. Every canonical problem uses
  `selected_for_research=true`; use the bucket to express ordering.
- Do not use the word "simplest," "easiest," "quickest," or similar steering terms.

## Output contract

A prioritization decision must include:
- `problem_id` (matching a stage-1 problem record)
- `priority_bucket` (one of: p0, p1, p2, p3, watch)
- `selected_for_research` (boolean)
- `priority_rationale`
- `evidence_atom_ids_used`
- `priority_status` = `"prioritized"`
