# Stage 2 guidance: problem prioritization

## Goal

Decide which problems merit deeper research now (`selected_for_research=true`), which
should be deferred (`p2` / `p3` / `watch`), and which can be deprioritized. The
prioritization decision must be explicit and inspectable.

## Priority buckets

| Bucket | Meaning |
|--------|---------|
| `p0` | Blocking: prevents a majority of runs from completing normally. Research now. |
| `p1` | High: recurring issue that meaningfully degrades quality or confidence. Research now. |
| `p2` | Medium: real problem, limited evidence breadth. Defer until more runs confirm. |
| `p3` | Low: nice-to-have or isolated observation. Watch for recurrence. |
| `watch` | Uncertain: insufficient evidence to prioritize. Re-evaluate next cycle. |

## What to favor for research now (p0 / p1)

- Problems with evidence from multiple distinct runs and multiple distinct agents.
- Problems where severity is `high` or `blocker` across multiple evidence atoms.
- Problems where user impact is severe (blocked from proceeding, incorrect output).
- Problems that appear in more than one mission or target context.
- Problems with strong signal from `run_failure_event` or `report_validation_error` atoms.

## What to defer or watch

- Single-run observations of medium severity without corroboration.
- Problems where confidence is low and evidence is sparse.
- Problems that appear only in one agent or one persona context.
- Problems that may be environmental rather than systematic.

## What to avoid

- Do not propose solutions. This stage decides research priority only.
- Do not invent new problem IDs; work only with the problem records from stage 1.
- Do not silently drop problems. Problems not selected for research stay in the artifact
  with `selected_for_research=false`.
- Do not use the word "simplest," "easiest," "quickest," or similar steering terms.

## Output contract

A prioritization decision must include:
- `problem_id` (matching a stage-1 problem record)
- `priority_bucket` (one of: p0, p1, p2, p3, watch)
- `selected_for_research` (boolean)
- `priority_rationale`
- `evidence_atom_ids_used`
- `priority_status` = `"prioritized"`
