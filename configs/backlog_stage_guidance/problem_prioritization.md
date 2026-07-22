# Stage 2 guidance: problem prioritization

## Goal

Rank every canonical problem and explain research urgency. Canonical identity is persistent,
but persistence does not mean the system should repeat an unchanged blocked research mission in
every cycle. The runner owns the per-cycle `research_route` and final
`selected_for_research` value. The model ranks evidence and impact; it cannot delete, terminate,
or permanently suppress a case.

Runner routes distinguish `research_new`, `research_update`, `resume_prior`,
`reassess_actionability`, `await_evidence`, and eventually `continue_downstream`. An
`await_evidence` case remains active with an explicit `reconsider_when` trigger. A legacy malformed
proof receives one current actionability reassessment; if the same frontier remains blocked, it is
retained without repeatedly starting over.

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

These signals lower ordering priority; they do not terminate a canonical case. Research is
responsible for determining whether the evidence establishes a mechanism. Scheduling is
runner-owned and may wait on a named evidence change rather than spend another identical mission.

## What to avoid

- Do not propose solutions. This stage decides research priority only.
- Do not invent new problem IDs; work only with the problem records from stage 1.
- Do not silently drop or indefinitely defer problems. Rank every record even when its runner-owned
  route is waiting. Do not attempt to override a runner route from prose.
- Do not use the word "simplest," "easiest," "quickest," or similar steering terms.

## Output contract

A prioritization decision must include:
- `problem_id` (matching a stage-1 problem record)
- `priority_bucket` (one of: p0, p1, p2, p3, watch)
- `selected_for_research` (boolean model recommendation; the runner replaces it from the durable
  research route before dispatch)
- `priority_rationale`
- `evidence_atom_ids_used`
- `priority_status` = `"prioritized"`
