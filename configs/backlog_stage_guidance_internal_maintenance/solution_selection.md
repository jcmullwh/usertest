# Stage 5 guidance: solution selection (internal maintenance)

## Goal

Choose among the existing option set. Do not create new options. The selection must
reference one of the configured family IDs and explain why the other options were not
chosen.

## What to favor for this repo

- Incremental changes over new top-level commands unless evidence breadth is compelling.
- Changes that are consistent with the existing composable-command philosophy described in
  `configs/repo_intent.md`.
- Changes that solve the problem class within existing surfaces when observation breadth
  across runs and agents shows a repeated pattern.
- Changes that fix the underlying mechanism instead of introducing case-by-case behavior
  for each observed failure mode.
- `most_direct` when the recurrence is a single mechanism and the direct fix fully
  resolves it, even if that mechanism has appeared in multiple runs.
- `most_robust` when the main value is validation, guardrails, or defense-in-depth on
  top of the direct fix because the direct fix alone still leaves credible recurrence
  vectors.
- `most_comprehensive` when repeated observations point to one subsystem-level gap or
  missing shared contract, and the comprehensive option fixes that class of failure
  while staying within existing command/config surface area.
- When both `most_robust` and `most_comprehensive` stay inside existing surfaces, prefer
  `most_comprehensive` if it establishes the clearer shared contract, canonical source
  of truth, or class-level mechanism rather than merely adding guardrails.

## What to avoid

- Do not create a new option not in the stage-4 option set.
- Do not select an option based on convenience, ease, or speed. Select based on fit with
  repo intent and evidence.
- Do not use banned steering terms: fastest, quickest, easiest, simplest, lowest-effort.
- Do not choose a hardcoded special-case fix when the observed recurrence suggests a
  shared mechanism that should be handled directly.
- Do not treat `repeated_variant` alone as an automatic mandate for `most_robust`.
- Do not require cross-context breadth for internal class-level hardening that stays
  within existing user-visible surfaces.
- Do not use `most_robust` as the default middle choice just because `most_direct`
  seems narrow and `most_comprehensive` seems broader.
- Do not reject `most_comprehensive` solely because missions, targets, or repo_inputs
  are structurally 1 in internal-maintenance mode.
- Do not describe a class-level internal fix as "too broad" when its surface area is
  still limited to an existing subsystem and the repeated observations are all pointing
  at the same mechanism.
- Do not skip `why_other_options_were_not_selected`. This field is required.

## UX review trigger

Set `needs_ux_review=true` when:
- The selected option proposes a new user-visible command, flag, or mode.
- The change surface includes `new_command`, `new_top_level_mode`, `new_config_schema`,
  `breaking_change`, or `new_api`.
- The selected option's breadth assessment is broader than the research supports.

## Output contract

A selection decision must include:
- `problem_id`
- `selected_option_id`
- `selected_family_id`
- `selection_rationale`
- `repo_intent_alignment`
- `why_other_options_were_not_selected`
- `needs_ux_review` (boolean)
- `selection_status` = `"selected"`
