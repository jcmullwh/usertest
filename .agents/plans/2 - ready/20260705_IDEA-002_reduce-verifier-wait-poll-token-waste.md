# Reduce verifier wait polling token waste

- Source: July 2026 Codex usage analysis
- Stage: researched plan
- Severity: high
- Change surface kinds: token_efficiency, workflow_reliability, agent_guidance
- User-visible: true
- Research completed: 2026-07-06

## Title

Reduce verifier wait polling token waste

## Problem

High-token `usertest_implement` runs spent a large share of input tokens on model calls whose next action was only waiting for an already-running verifier process. The expensive part is not the verifier progress text itself. The expensive part is that each wait decision resends the full accumulated context.

Local `token_monitoring.json` artifacts excluding `_workspaces` showed:

- `wait_poll_resend`: 17 signals, 3,320,332 input tokens, 57 wait/poll calls
- `verification_or_dependency_loop`: 6 signals, 2,409,881 input tokens, 26 calls
- `large_context_resend`: 10 signals, 19,113,086 input tokens, 154 large-context calls

Local `verification.json` artifacts excluding `_workspaces` showed:

- verification runs: 71 total
- verification run duration: min 36.93s, p05 71.25s, median 490.56s, mean 486.28s, p95 1,280.00s, max 1,997.79s
- verification commands: 223 total
- command duration: min 1.84s, p05 2.48s, median 71.61s, mean 154.82s, p95 603.89s, max 1,954.22s

These numbers mean any supplied 600-second verifier budget is too short for current successful history and should not be treated as the default expected duration or hang guard. The current CLI default falls back to the runner/broker default, and the current broker default is a high hang guard, not an expected-duration budget:

- `packages/runner_core/src/runner_core/verification_broker.py` defines `_BROKER_DEFAULT_COMMAND_TIMEOUT_SECONDS = 10_800.0`
- the broker client polls internally every 0.2s, but that internal process polling does not require model turns
- the model-side waste comes from repeated assistant wait/poll turns while an external verifier is already running

## User impact

Runs burn large token volumes while no reasoning work is happening. This is especially costly after broad source reads have already made the context large. Short or arbitrary model wait intervals also create false "hung" interpretations when the verifier is simply still inside historically successful duration ranges.

## Current behavior and gaps

- `token_monitoring.run_analysis` already emits `wait_poll_resend` and `verification_or_dependency_loop`.
- `verification_broker.py` now has a high default command timeout guard.
- `_build_verification_followup_prompt` reports failed command tails and artifact paths after verification fails.
- The implementation mission says the final handoff verification command blocks until verification completes, but it does not provide concrete expected wait durations.
- The current system does not consistently surface historical verifier timing profiles to the agent before the wait begins.

## Implementation plan

### Ticket 1: Build a verification timing profile artifact

Add a small timing-profile builder that reads recent `verification.json` artifacts and computes:

- run count and command count
- min, p05, median, mean, p95, and max wall seconds
- slowest command labels and artifact paths
- recommended initial model wait duration
- high hang-guard duration

Recommended initial logic:

- recommended initial wait should be close to p95 when enough history exists
- high hang guard should be at least the broker timeout guard or the max successful duration plus a clear margin
- the report should explicitly distinguish "expected wait" from "hang guard"

Acceptance criteria:

- The profile can be generated without scanning `_workspaces`.
- Empty or sparse history returns a conservative fallback with an explicit "insufficient history" reason.
- Unit tests cover percentile computation and sparse-history fallback.

### Ticket 2: Surface timing expectations in runner handoff and broker metadata

Include the timing profile in the final handoff verification prompt or adjacent broker metadata so the agent sees:

- expected duration range
- recommended first wait
- when it is reasonable to check once
- when it is reasonable to suspect a hang
- artifact paths to inspect after the result returns

The language must be direct:

- use one long wait rather than frequent short polling
- do not repeatedly poll only to watch progress
- if verification is expected to take minutes, wait near the expected duration before checking again
- one or two checks are acceptable; continuous wait loops are not
- do not call a run hung until it exceeds the high hang guard or shows concrete failure evidence

Acceptance criteria:

- Runner tests prove the prompt includes timing guidance when a profile is available.
- The prompt does not include huge progress logs or raw historical artifact dumps.
- Existing final handoff verification behavior still blocks and returns pass/fail.

### Ticket 3: Reduce model-visible progress volume

Keep detailed progress in artifacts, but keep model-visible status small:

- final result summary
- failed command tail excerpts only when failing
- artifact directory
- command counts and wall-time totals
- no repeated progress stream dumps into the model transcript

Acceptance criteria:

- Verification failures still provide enough detail for the next fix attempt.
- Passing verification produces a compact result.
- Tests cover truncation limits for command tails and prior assistant output.

### Ticket 4: Evaluate runner-owned blocking verification

Design and then implement a path where the runner, not the model, owns the blocking wait after the agent says work is ready:

- the agent launches or requests final verification once
- runner waits for completion through the broker/client
- runner returns one structured result to the agent only if a fix is needed
- a passing result finalizes without another model wait loop

Acceptance criteria:

- The model does not need to issue repeated `write_stdin` wait actions for normal verifier completion.
- Failed verification still re-enters the agent with a compact fix prompt.
- The implementation preserves the existing artifact contract.

## Measurement plan

After each implementation slice, run representative maintenance tickets and compare:

- `wait_poll_resend` signal count
- wait/poll call count
- `wait_poll_resend` input tokens
- `verification_or_dependency_loop` signal count and input tokens
- elapsed verification duration and pass/fail outcome

Expected direction:

- wait/poll model calls should drop materially on long verifier runs
- verifier failure diagnosability should not regress
- successful verifier durations near 20 to 35 minutes should be treated as normal historical evidence, not hangs

## Risks and guardrails

- Do not replace arbitrary short waits with another arbitrary short wait. Any wait guidance must be derived from history or explicit config.
- Do not lower broker command timeouts while solving token waste.
- Do not hide failure details needed for repair; compact does not mean opaque.
- Do not treat the broker client's internal 0.2s process polling as token waste. The waste is model-turn polling.
- Keep timeout wording precise: expected duration, check interval, and hang guard are different concepts.

## Ready state

This ticket is ready to split into implementation tickets. Start with the timing-profile artifact and prompt-surfacing work before attempting runner-owned blocking verification.
