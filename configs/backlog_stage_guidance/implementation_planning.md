# Stage 6 guidance: decision-complete implementation planning

## Goal

Translate a falsification-accepted option into an implementation plan that leaves no
architecture, interface, target, or verification decision to the implementer.

## Required grounding

- Record the stable case, model-authored change-plan, and repository-revision IDs.
- The server assigns the plan-revision ID from canonical plan content; the planner must
  not emit, invent, or increment it.
- Name exact repository-relative files or modules and symbols, with the concrete behavior,
  interface, schema, or data-flow change at each target.
- Mark every target `modify` or `create` and name the intended anchor symbols.
- Preserve the selected option's causal coverage.
- Copy its `scope_evidence` exactly and map every verified intervention point to the same
  `change_targets` path and symbol. Copy the verified intervention text exactly as that
  production target's `change`. Enumerate the production symbols/import/config keys that
  explain the intervention. Additional inspected callers/compatibility targets are allowed
  only with `causal_propagation`/`compatibility` rationale and bound mechanism evidence. A
  new production target cannot itself have been inspected: bind its `action: create` entry
  to an existing inspected path and symbol on the verified mechanism using
  `integration_binding`, explain how that boundary loads/calls/registers/consumes it, and
  cite the same typed evidence. An unbound target or a new mechanism returns to research.
  The server content-addresses this intent. Implementation review
  blocks when a required planned production path is untouched; extra support/test paths and
  wider hunks are surfaced for semantic review rather than rejected mechanically. Return to
  optioning/research when the selected mechanism itself needs another production target.
  Do not turn every symbol in a researched call path into a planned edit. One selected,
  causally sufficient control point is enough when runner evidence places it on every
  selected mechanism path; its rationale explains but does not establish that sufficiency.
  Plan other production changes only when they are independently necessary for causal
  propagation or compatibility.
- Include executable verification commands and an original-scenario before/after mapping.
  Every command entry is one invocation with an unmasked exit code; shell chaining,
  pipelines, redirection, nested shell scripts, command substitution, inline interpreter
  payloads, and forced-success exits are forbidden.
  Bind that mapping to a verified supporting original, faithful, or correctly-platformed
  live experiment by ID. A deterministic static trace may establish root cause but cannot
  serve as post-change behavioral outcome proof. The before command, exit code, and observable
  assertion must match the experiment. The after command must be the same scenario, appear
  in `verification_commands`, and satisfy a problem-specific observable oracle. Do not force
  exit code zero when an established provider/platform failure should remain: mark the plan
  as mitigation and prove the corrected classification, diagnostic, cleanup, or recovery.
  Every plan claiming `resolved` must assert a concrete positive post-change stream value,
  retained artifact value, or runner-addressed state value in addition to exit status and
  any old symptom's absence. This includes nonzero-to-zero scenarios: catching the failure
  and returning zero is not proof that the required operation or side effect occurred.
  When a `wrong_value_corrected` causal control establishes the correct output, bind the
  post-change assertion to that exact runner-observed value, preferably with equality. A
  planner-invented success string that the patch could merely emit is not outcome proof.
  A plan preserving `multiple_independent_paths` or `shared_abstraction` scope must bind
  post-change outcome evidence for every selected runner independence key. One oracle may
  cover several keys only when its typed mechanism evidence actually spans those paths;
  otherwise return to research for separate path-specific oracles. Do not impose this breadth
  requirement on a genuine `single_path` problem.
- Define runner-owned `outcome_verification_roles` for the exact original replay, the
  recurrence check, every required live probe, and any effect that could support a
  `mitigated` outcome. Original/live/mitigation commands need machine predicates. Recurrence
  should normally rely on the centralized refresh's two later canonical-case shadow snapshots
  (empty commands/predicates), not an invented bespoke probe. Generic tests cannot stand in
  for operational proof, and a timeout remains blocked.
- Do not waive an available runner-verified replay with a planner-authored proof
  limitation. If that replay is no longer faithful or executable, return the case to
  research rather than substituting a generic command.
- State preserved behavior, intentional changes, migrations, and credible failure modes.
- Preserve the pipeline-inferred live-verification requirement and explain it from the
  cited provenance.

If faithful before/after proof is impossible, state the exact limitation and an executable
alternate, and cite an exact material unknown or evidence boundary from research. Keep
code/test confidence distinct from live-runtime confidence.

## Rejection rules

Reject plans whose implementation steps begin with discovery such as locate, identify,
determine, inspect, audit, review, investigate, find, explore, or decide. Reject vague
targets, “relevant tests,” placeholders, missing commands, or a repository revision that
does not match the inspected checkout. Planning uses the workspace read-only and must not
invent durations absent from repository evidence.

Split plans only when units are independently implementable and verifiable. Do not create
separate breadth or documentation tickets merely to make a plan look comprehensive.
