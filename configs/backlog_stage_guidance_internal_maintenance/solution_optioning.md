# Stage 4 guidance: internal-maintenance optioning

Generate zero to three distinct mechanisms from an evidence-sufficient research proof and
the recorded repository source. The configured family IDs are optional compatibility
lenses, not required slots and not a breadth ranking.
Multiple genuinely distinct mechanisms may share one family label.

Repeated runs or agents establish recurrence only when they expose independent consumers
or failure paths. Repeated output from one research or implementation loop is one path.
A shared contract, canonical source, centralized mechanism, or class-level abstraction
requires at least two independently evidenced paths.
That requirement applies to broad problem coverage or a new reusable abstraction. The
complete recovery path for one evidenced operation may touch multiple existing callers or
components, extend an existing helper, and add ordering or protection changes while remaining
`single_path`; inspect affected callers for compatibility and retain causally necessary work.

Every option must record causal coverage, unsupported assumptions, residual recurrence
paths, compatibility risks, testability, a prospective outcome strategy, and scope evidence.
The outcome strategy names the useful bounded operation, success properties, safety
constraints, and retained Stage-3 baseline; it is not pre-change proof of a future fix. Omit rhetorical variants that
differ only by breadth. Return `insufficient_evidence` with no options when an indispensable
present-state fact about the mechanism, reachable change surface, existing requirement, or
available protection signal is unknown. Choosing a new finite threshold, configurable
default, identity/alias rule, or interface extension is prospective Stage-4 design; bound its
tradeoffs, safety constraints, and verification instead of returning it to research. Return
`no_safe_option` with no options when adequate evidence shows that no considered mechanism is
safe.

Prefer a clean runner-verified fail-first command for the same source atoms and mark the
strategy `post_change_replay_mode=verified_fail_first`. Do not turn an exit-zero assertion of
the old symptom into a post-change oracle by rewriting its retained research file. When no
fail-first exists, use `post_change_replay_mode=stage6_planned_unverified`; a future
solution-specific proof is Stage-6 work, not automatically missing research.

Trace the reachable operation through its earliest same-resource failure boundary; an
intervention after that boundary cannot recover the operation. Design partial
supporting-operation errors for safe throughput: surface them, verify a safe postcondition and
sufficient actual progress, and continue only when the intended operation can still succeed.
Avoid both blanket abort-on-any-error behavior and swallowed errors. An observed benchmark or
current default may inform a prospective default or test fixture, but cannot become a universal
supported maximum without a repository requirement or capacity constraint.

Bind each option to one exact verified research hypothesis by copying its statement,
mechanism symbols, supporting evidence, genuine counterevidence, and exactly one runner-owned
causal proof route. Normally copy every falsification-attempt ID and require a survived
runner-replayed challenge. For a runner-minted deterministic closure, copy every closure
receipt ID and keep falsification-attempt refs empty. Never invent an alternative or challenge;
an unrelated refuting experiment cannot substitute. Map every mechanism symbol
to an exact inspected target symbol/path with a stated intervention. Unrelated or
uninspected interventions are invalid.

Every scope reference must be one exact runner-owned `failure_path_id` or typed
`mechanism_evidence_id`; copy its `path_name` without relabeling. Broad/shared scope
requires two receipts with distinct `independence_key` values. One run may expose
independent consumers; atom sets need not be disjoint. Arbitrary artifacts,
experiments, files, or symbols do not establish path independence.

Use the supplied workspace read-only. Do not modify files, install dependencies, run
mutating commands, or invent duration values absent from repository evidence.
