# Stage 6 guidance: implementation planning

## Goal

Convert the selected solution into an actionable change plan. This is where
`proposed_fix` and `implementation_steps` appear for the first time, backed by all
prior stage evidence.

## What to include in a change plan

- A concrete description of what to change and why (grounded in research + selection).
- Ordered `implementation_steps` that a developer could follow.
- `verification_steps` that confirm the problem is resolved.
- `success_criteria` that are observable and testable.
- `rollback_notes` if the change has risk of regression.
- A `suggested_owner` based on the component affected.

## Splitting guidance

A single selected solution may become multiple change-plan units if:
- The repo guidance prefers smaller, independently mergeable changes.
- The implementation has a clearly separable test-only component and a code component.
- A docs change is unrelated enough to merit its own ticket.

When splitting, each change plan unit must have its own `change_plan_id`, `title`, and
scope. Cross-reference the sibling plans in `related_change_plan_ids`.

## What to avoid

- Do not pad implementation steps with speculative tasks not grounded in research.
- Do not mark `change_plan_status="planned"` until implementation steps and verification
  steps are both non-empty.
- Do not invent new options or solutions; the plan must derive from the stage-5
  selected option.

## Output contract

A change plan must include:
- `change_plan_id`
- `problem_id`
- `selected_option_id`
- `title`
- `problem` (summary from research)
- `user_impact`
- `proposed_fix` (the selected option's approach, made concrete)
- `implementation_steps` (ordered list)
- `verification_steps` (ordered list)
- `success_criteria` (observable, testable)
- `rollback_notes`
- `suggested_owner`
- `change_plan_status` = `"planned"`
- `related_change_plan_ids` (list, may be empty)
