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
