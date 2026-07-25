# Stage 5 guidance: neutral internal-maintenance selection

Choose provisionally by evidence-backed causal coverage. Family IDs are compatibility
labels, not a ranking. Do not favor a shared contract, canonical source, or class-level
mechanism unless at least two independent consumers or failure paths have evidence refs;
repeated observations of one path count once.
That rule governs broad problem claims and new reusable abstractions. It does not prohibit a
single evidenced operation from crossing multiple existing call sites or components,
extending an existing helper, or requiring ordering and protection changes. Keep such work
single-path when the edits are necessary for the same reachable operation, and inspect
affected callers for compatibility.

Compare mechanism fit, unsupported assumptions, residual paths, compatibility risk, and
testability. Set UX review from the actual surface. An independent repository-aware
falsification pass then tries to disprove the selection. It may cite only exact runner-owned
`mechanism_evidence_id` values for the selected mechanism across typed exception, output,
control, harness, static, and live evidence. The server content-addresses the review.
Critical causal/interface/change-surface findings cannot be accepted; residual compatibility
risk requires evidence, rationale, and verification.
The falsifier must also decide whether the option's outcome strategy proves intended
maintenance behavior rather than only suppressing an error, returning exit zero, or emitting
a new marker. It reviews and content-addresses that strategy even when Stage 3 retained an
executed positive contract. Such a contract is baseline/additional evidence, not an exclusive
post-change gate: a surface-only baseline does not block a sufficient option strategy and
cannot rescue a surface-only strategy. Stage 5 approves prospective semantics for Stage 6;
only later execution can establish resolved or live-verified evidence.
Separate unsupported facts about the current mechanism, requirements, consumers, protection
signals, or reachable control surface from explicit prospective design. A new finite threshold,
configurable bound/default, identity/alias rule, or interface extension may proceed without
proof that the old implementation already intended it when tradeoffs, safety constraints, and
Stage-6/outcome verification are bounded. Unsupported pre-build recovery, manual/shared
consumer, active/protected/external-reference, or verification claims still block. Return to
Stage 3 only for an indispensable missing present-state fact, not to choose the future policy.
Trace the reachable operation through its earliest same-resource failure boundary. Reject a
later-only intervention because, after the boundary, it cannot recover the operation; do not
remove an evidenced earlier intervention as unsupported breadth merely because it crosses
existing components.
Maximize safe useful throughput: a reported partial supporting-operation error need not abort
when verification proves a safe postcondition, sufficient actual progress, and successful
continuation. Reject swallowed errors and assumed progress, as well as needless
abort-on-any-error behavior. A benchmark value or current default may inform a prospective
default or test case, but cannot become a universal supported maximum without a repository
requirement or capacity basis.
Valid `reject` and `insufficient_evidence` findings return content-addressed feedback to the
original selector session. The selector may choose another existing option or request a
revised zero-to-three option set from the original optioner session; every new selection gets
a fresh independent falsifier. Structural errors return only to their authoring role. A
stalled ancillary labeler uses a neutral server-owned label and cannot invalidate selection.
