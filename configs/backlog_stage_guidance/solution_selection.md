# Stage 5 guidance: solution selection

## Goal

Choose among the existing option set. Do not create new options. The selection must
reference one of the configured family IDs and explain why the other options were not
chosen.

## Duration guardrail

Stage artifacts and tool calls must not invent sleeps, polling delays, retry delays, or
timeout values. If waiting behavior is part of the path being analyzed, use an existing
repo-configured value or document the limitation instead of making up a duration. This
also applies to tool calls: do not pass `timeout`, `timeout_ms`, or similar duration
parameters to shell/tool invocations unless the assigned evidence includes that exact
configured value.

## What to favor for this repo

- Incremental changes over new top-level commands unless evidence breadth is compelling.
- Changes that are consistent with the existing composable-command philosophy described in
  `configs/repo_intent.md`.
- Options where the change surface is narrow and the test implications are manageable.
- Options that fix the underlying mechanism rather than adding a narrow exception when
  research points to a repeated or shared cause.
- `most_robust` over `most_direct` when research showed recurrence risk.
- `most_comprehensive` only when research strongly supports a class-level fix.

## What to avoid

- Do not create a new option not in the stage-4 option set.
- Do not select an option based on convenience, ease, or speed. Select based on fit with
  repo intent and evidence.
- Do not use banned steering terms: fastest, quickest, easiest, simplest, lowest-effort.
- Do not choose a hardcoded special-case fix unless the research dossier supports an
  isolated instance or an intentional product boundary.
- Do not skip `why_other_options_were_not_selected`. This field is required.

## UX review trigger

Set `needs_ux_review=true` when:
- The selected option proposes a new user-visible command, flag, or mode.
- The change surface includes `new_command`, `new_top_level_mode`, `new_config_schema`,
  or `breaking_change`.
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
