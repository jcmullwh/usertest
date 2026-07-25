# Stage 4 guidance: evidence-backed solution optioning

## Goal

Produce zero to three genuinely distinct causal mechanisms from an evidence-sufficient
research proof and source inspected at the recorded repository revision.

## Progression gate

Do not option a dossier unless the research-readiness contract passes. Return an explicit
`insufficient_evidence` outcome when a material present-state fact affects the root cause,
reachable change surface, indispensable existing requirement, or available protection
signal. A prospective policy or interface choice, such as a finite threshold, configurable
default, identity/alias rule, or interface extension, is Stage-4 work, not automatically
missing research: state the choice, bounded tradeoffs, safety constraints, and verification. Return
`no_safe_option` when the evidence is sufficient but all considered mechanisms leave
unacceptable risk.

## Optional family lenses

The configured family IDs are compatibility labels, not mandatory slots or a quality
ranking. Omit a family when it does not represent a distinct mechanism. Different amounts
of validation or abstraction around the same mechanism do not automatically create
different options. Multiple distinct mechanisms may share the same family label.

## Causal and scope evidence

- Record the mechanism, covered symptoms, unsupported assumptions, residual recurrence
  paths, compatibility risks, and before/after testability for every option.
- Define an `outcome_strategy` for every option: the intended useful operation, concrete
  success properties, relevant safety constraints, and the verified Stage-3 baseline
  scenarios that Stage 6 must replay. This is a proposed success contract, not pre-change
  proof that the option works. Error disappearance, exit zero, or a new diagnostic alone
  is surface-level unless it is the actual source requirement.
- Prefer a runner-verified fail-first experiment for the same source atoms. Its unchanged
  command fails before and may pass after the implementation. Mark it
  `post_change_replay_mode=verified_fail_first`. An exit-zero experiment that asserts the old
  symptom is mechanism evidence, not an exact post-change oracle, and its retained research
  asset cannot be rewritten. If no fail-first exists, use
  `post_change_replay_mode=stage6_planned_unverified`; Stage 6 may design a distinct future
  proof without requiring that solution-specific command to have already run.
- Bind every option to one exact verified research hypothesis: copy its statement,
  mechanism symbols, supporting evidence, genuine counterevidence (including an honest empty
  list), and exactly one runner-owned causal proof route without paraphrase. Normally copy every
  causal falsification attempt ID and require a survived runner-replayed challenge. For an exact
  deterministic static/config mechanism with a runner-minted closure, copy every deterministic
  closure receipt ID and keep falsification attempt refs empty. Never invent an alternative or
  challenge to make a record advance; an unrelated refuting experiment is invalid. Require
  at least one exact inspected control point that is causally sufficient to reverse the
  evidenced mechanism. A multi-symbol call path does not imply one edit per symbol: bind
  the sufficient boundary to the full exact symbol chain it controls. The runner must show
  that boundary on every selected failure path through an exact mechanism link or strong
  causal control; `sufficiency_rationale` explains the choice but cannot prove it. An
  unrelated or uninspected intervention cannot advance. Do not add edits at every traversed
  symbol merely to satisfy this gate; return to research when the causal boundary is not
  evidenced.
- Record only runner-minted failure or typed mechanism paths. Copy each `path_name` and its
  single `failure_path_id` or `mechanism_evidence_id` exactly.
- Require at least two independent consumers or paths before describing an option as
  shared, canonical, centralized, class-level, or system-wide. Their runner-owned
  `independence_key` values must differ. One run may expose independent consumers; atom
  sets need not be disjoint.
- Reserve that two-path requirement for broad problem coverage or a new reusable abstraction.
  The complete recovery path for one evidenced operation may touch several existing callers,
  functions, or components, extend an existing shared helper, and add sequencing or protection
  without becoming broad scope. Keep it `single_path`, inspect affected callers for
  compatibility, and preserve each causally necessary step.
- Follow the reachable operation to its earliest same-resource failure boundary. A control
  that runs only after that point cannot recover the operation; propose an earlier ordering
  when the inspected call path supports it.
- Maximize safe useful throughput. Partial supporting-operation errors may be surfaced while
  the intended operation continues only when the option verifies a safe postcondition and
  sufficient actual progress. Avoid both blanket abort-on-any-error behavior and swallowed
  errors or assumed progress.
- Treat an observed benchmark or current configuration value as a possible prospective
  default or test case, not a universal supported maximum unless a repository requirement or
  capacity constraint establishes it.
- Artifact IDs, experiment IDs, files, symbols, renamed paths, and multiple observations
  of the same execution path do not establish independent scope.

## Repository use

Inspect relevant source and tests in the supplied read-only workspace. Do not modify files,
install dependencies, or run mutating commands. Do not invent sleep, polling, retry, or
timeout values that are absent from repository evidence.

## Output

Return the optioning envelope defined by the stage prompt. `options_produced` carries one
to three validated options; `insufficient_evidence` and `no_safe_option` carry none.
