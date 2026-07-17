# Stage 1 guidance: problem mining

## Goal

Extract explicit problem records from observed evidence atoms. A problem record is a
structured statement that a problem exists, grounded in one or more atoms. It is not a
solution proposal.

## Duration guardrail

Stage artifacts and tool calls must not invent sleeps, polling delays, retry delays, or
timeout values. If waiting behavior is part of the path being analyzed, use an existing
repo-configured value or document the limitation instead of making up a duration. This
also applies to tool calls: do not pass `timeout`, `timeout_ms`, or similar duration
parameters to shell/tool invocations unless the assigned evidence includes that exact
configured value.

## What to favor

- Recurring stability problems observed across multiple runs or agents.
- Problems with high-severity or blocker evidence atoms.
- Problems where confusion points and failures point to the same underlying issue.
- Problems with clear user-blocking impact even if the root cause is not yet known.

## Interpret observations in their run outcome

The workspace may contain `context_atom_ids` in addition to `assigned_atom_ids`. Context atoms
are terminal reports or failures from the same originating runs. Read them in full and use them to
interpret sequence, recovery, verification, and residual impact, but do not decide them or cite
them as problem evidence. Only assigned atoms may receive decisions or citations.

- A successful originating run is counterevidence, not a blanket negative. A failed diagnostic,
  prerequisite probe, or early attempt is not an actionable problem when terminal evidence shows
  the intended workflow recovered and its relevant verification passed with no residual impact.
- A successful run can still establish a real problem. Preserve an assigned observed issue when
  the terminal report says a feature remained degraded, a user-facing defect persisted, or a
  separate verification oracle failed.
- Separate an upstream blocker from consequences of that blocker. Missing downstream validation,
  confidence, or artifacts are not independent problems unless evidence shows they persist after
  the upstream blocker is removed or have a distinct mechanism and impact.
- Ancillary stderr, optional service failures, and warnings require demonstrated effect on the
  requested task or user experience. Their presence alone does not establish an actionable case.
- Missing or ambiguous terminal context is uncertainty, not proof of either success or failure.
  Use `unresolved` or `deferred` with a concrete reconsideration trigger.

## Compare repeated structured observations

When two or more assigned atoms describe the same observed surface at different timestamps,
read them as a series before choosing the problem statement. Order the observations by their
evidence timestamp and compare like-named structured fields. The evidence summary should state
both the material changes and the relevant invariants; for example, a growing count alongside
an unchanged zero-success count can be more informative than either observation in isolation.
Do not infer a causal mechanism or violated policy that the series does not establish.

A measurement caveat is a boundary on the claim, not automatically the problem. Fields that
say a value is unknown, proxy-based, unavailable, or incomplete limit what can be concluded.
Promote the missing measurement itself only when evidence shows that the absence obstructed the
requested task or user. Otherwise prefer the strongest observed behavior and preserve the
measurement limitation as uncertainty around its extent or impact.

Do not manufacture user impact to make a concrete observation look actionable. If the behavior
is directly observed but its user or runtime effect is only plausible, say explicitly in
`user_impact` that the impact is unverified and name the evidence that would establish it. This
honest uncertainty does not by itself require suppressing the observation; use `unresolved` only
when the missing evidence is material to deciding whether a problem exists at all.

## What to avoid

- Do not propose solutions, fixes, or implementation ideas. Stage 1 only identifies that
  a problem exists and what it looks like from evidence.
- Do not merge unrelated problems just because they appear in the same run.
- Do not invent problems not directly grounded in evidence atoms.
- Do not copy `proposed_fix`, `investigation_steps`, or `success_criteria` from miner
  output into problem records. Those fields belong to later stages.

## Output contract

The runner assigns each miner a bounded, disjoint subset of the eligible corpus. Every complete
assigned chunk must be read, and every atom in those chunks must appear exactly once in
`atom_decisions`. Index previews are routing aids only and cannot support a citation. Uncertainty
is not a reason to omit an atom: use explicit `unresolved` or `deferred` with an evidence-specific
rationale.

When `atoms.json.origin_attachment_evidence.atom_refs` assigns retained attachment evidence to
an atom, inspect the workspace copy rather than the host `artifact_ref.path`. Read every bounded
file in that artifact's `chunks` list in full; overlapping chunk boundaries ensure a diagnostic in
the middle or at a boundary remains visible. The runner verifies every declared source hash before
materialization and retains a full-read event for every chunk. If the manifest records a
materialization error for an atom, keep that atom `unresolved` rather than deciding from its excerpt.

A `deferred` decision is not a parking lot. It must name the concrete event or missing evidence
that would settle the classification, and the runner will reconsider it on every later full
evidence cycle until it becomes a case, a duplicate, or expected noise. If no such trigger is
known, use `unresolved`, which is also reconsidered on later cycles.

The runner applies the neutral problem-identification lens to every bounded job and sends every
non-support decision through a separate adversarial pass. Disagreement remains unresolved; a
second pass may recover a concrete problem, but cannot erase a supported case from the first pass.
The adversarial pass receives the same terminal context. A finding that ignored recovery,
verification, or residual impact is feedback for correction of that same authored result; it is
not a reason to discard an otherwise improving result and start over immediately.

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

The response envelope also includes `atom_decisions`. `supports_case` decisions identify the
problem records that cite the atom; `duplicate`, `expected_noise`, `deferred`, and `unresolved`
carry no problem IDs. The runner verifies the exact assignment partition and retained full-read
events before accepting the stage.

Model agreement is not authority for permanent suppression. A suspected duplicate must first
be emitted as a supported case so relation review can bind an exact canonical target and
content-addressed relation receipt. `expected_noise` is accepted only with a runner-owned,
versioned rule bound to exact atom fields (currently proposal evidence); otherwise the runner
coerces the decision to reconsiderable `deferred`.

## Relation-review guidance

At this stage the relation reviewer may merge two records when the evidence atoms clearly
describe the same root cause from the same failure surface, or may mark them
`same_cause_group` when they appear to have a common cause but are distinct problems.
Split hints appear when a single candidate atom set shows disjoint failure paths (e.g.
two unrelated components each broken in different ways that happen to appear together).
Collapse decisions require exact evidence citations from both sides, reciprocal decisions
for active cases, non-empty rationale, and bounded numeric confidence. Surface similarity,
one-sided decisions, or confidence below 0.7 leaves cases separate for independent research.
