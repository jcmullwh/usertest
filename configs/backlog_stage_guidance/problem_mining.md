# Stage 1 guidance: problem mining

## Goal

Extract explicit problem records from observed evidence atoms. A problem record is a
structured statement that a problem exists, grounded in one or more atoms. It is not a
solution proposal.

## What to favor

- Recurring stability problems observed across multiple runs or agents.
- Problems with high-severity or blocker evidence atoms.
- Problems where confusion points and failures point to the same underlying issue.
- Problems with clear user-blocking impact even if the root cause is not yet known.

## What to avoid

- Do not propose solutions, fixes, or implementation ideas. Stage 1 only identifies that
  a problem exists and what it looks like from evidence.
- Do not merge unrelated problems just because they appear in the same run.
- Do not invent problems not directly grounded in evidence atoms.
- Do not copy `proposed_fix`, `investigation_steps`, or `success_criteria` from miner
  output into problem records. Those fields belong to later stages.

## Output contract

A problem record must include:
- `problem_id` (stable string, e.g. `problem:readme-quickstart-missing`)
- `title`
- `problem` (what is observed, not what should be done)
- `user_impact`
- `severity` (low / medium / high / blocker)
- `confidence` (0.0–1.0)
- `evidence_atom_ids` (non-empty)
- `evidence_summary`
- `problem_status` = `"identified"`

A problem record must NOT include: `proposed_fix`, `selected_solution`, `family_id`,
`option_id`, `implementation_steps`, or any other solution or selection field.

## Relation-review guidance

At this stage the relation reviewer may merge two records when the evidence atoms clearly
describe the same root cause from the same failure surface, or may mark them
`same_cause_group` when they appear to have a common cause but are distinct problems.
Split hints appear when a single candidate atom set shows disjoint failure paths (e.g.
two unrelated components each broken in different ways that happen to appear together).
