# Stage 3 guidance: reproduce-plus-research

## Goal

Reproduce the problem and bound its root cause. The goal is a reproduced or well-bounded
issue with evidence. The goal is NOT a clean working tree and NOT a fixed symptom.

## The mission contract (read carefully)

This stage runs in an isolated writable workspace. Writes are allowed for:
- Failing tests that demonstrate the problem
- Temporary instrumentation that observes the failure
- Minimal fixture or setup changes required to trigger the issue
- Repro harness scripts

Writes are NOT allowed for:
- Fixing the bug or implementing the solution
- Changing production behavior to make the symptom disappear
- Adding new user-visible features or commands
- Broad refactors unrelated to the reproduced failure path
- Documentation written as if the feature shipped

`implementation_performed` must be `false`. A research run that produces
implementation-like changes is suspicious, not successful. Suspicious diffs are surfaced
in the dossier but do not automatically fail the run.

## Acceptable research outcomes

1. **Reproduced**: A failing test, command failure, or instrumentation output confirms the
   problem. Evidence includes the reproduction artifact and a diff classification of
   `allowed_research_edits`.

2. **Bounded failure**: The problem could not be fully reproduced, but the investigation
   established what was tried, what the likely boundary conditions are, and what remains
   unknown.

## What to document

- Reproduction status: `reproduced` / `reproduction_failed` / `partial`
- Which writes were made and why (`writes_used`, `writes_purpose`)
- Root cause hypotheses, ranked by confidence
- Broader class assessment: is this an isolated instance or a repeated pattern?
- What remains unknown or requires more evidence

## What to avoid

- Do not mark a run as successful just because code changed.
- Do not propose or implement the fix.
- Do not judge success by a clean diff or passing tests if the problem was not reproduced.

## Output contract

A research dossier must include:
- `problem_id`
- `reproduction_status` (reproduced / reproduction_failed / partial)
- `writes_used` (boolean)
- `writes_purpose` (list, from: failing_test, temporary_instrumentation, repro_harness, fixture_change, none)
- `implementation_performed` (must be false)
- `diff_classification` (allowed_research_edits / suspicious_implementation / no_changes)
- `root_cause_hypotheses` (list)
- `broader_class_assessment` (isolated_instance / repeated_variant / unknown)
- `unknowns` (list)
- `research_status` = `"researched"`
