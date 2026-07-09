# IDEA-003 Ticket 05 Delegation A/B Validation

This directory records the first representative disabled/enabled delegation validation for
Ticket `447b9812a01f72dd` against PR #204.

## Run Pair

- Disabled arm: `runs/usertest/usertest/20260709T110952Z/claude/447106`
- Enabled arm: `runs/usertest/usertest/20260709T112311Z/claude/447108`
- Target ref: `backlog/447b9812a01f`
- Agent/model: `claude` / `claude-sonnet-5`
- Policy: `write`
- Verification command: `pwd`

Both arms reviewed whether PR #204 satisfied Ticket 05. The disabled prompt
forbade subagents. The enabled prompt required exactly one concise Claude
`Agent` delegation before parent synthesis.

## Findings

- Both arms produced completed reports and passed verification.
- The disabled arm had `no_delegation`.
- The enabled arm had one `Agent` invocation classified as
  `delegation_parent_context_summary`; the result was concise and did not leak
  raw broad-source output.
- Claude token telemetry is not available to the current local token monitor, so
  parent-input and combined-token tradeoffs are explicitly unattributable rather
  than treated as zero.

## Decision

Do not make delegation more aggressive from this evidence alone. The enabled
arm shows concise delegation can work for this review-style maintenance task,
but the token tradeoff cannot be evaluated until provider-equivalent Claude
token telemetry is available or a comparable Codex-enabled delegation path is
measured.
