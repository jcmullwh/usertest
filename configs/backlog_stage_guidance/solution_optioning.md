# Stage 4 guidance: evidence-backed solution optioning

## Goal

Produce zero to three genuinely distinct causal mechanisms from an evidence-sufficient
research proof and source inspected at the recorded repository revision.

## Progression gate

Do not option a dossier unless the research-readiness contract passes. Return an explicit
`insufficient_evidence` outcome when a material unknown affects the root cause, interface,
or change surface. Return `no_safe_option` when the evidence is sufficient but all
considered mechanisms leave unacceptable risk.

## Optional family lenses

The configured family IDs are compatibility labels, not mandatory slots or a quality
ranking. Omit a family when it does not represent a distinct mechanism. Different amounts
of validation or abstraction around the same mechanism do not automatically create
different options. Multiple distinct mechanisms may share the same family label.

## Causal and scope evidence

- Record the mechanism, covered symptoms, unsupported assumptions, residual recurrence
  paths, compatibility risks, and before/after testability for every option.
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
- Artifact IDs, experiment IDs, files, symbols, renamed paths, and multiple observations
  of the same execution path do not establish independent scope.

## Repository use

Inspect relevant source and tests in the supplied read-only workspace. Do not modify files,
install dependencies, or run mutating commands. Do not invent sleep, polling, retry, or
timeout values that are absent from repository evidence.

## Output

Return the optioning envelope defined by the stage prompt. `options_produced` carries one
to three validated options; `insufficient_evidence` and `no_safe_option` carry none.
