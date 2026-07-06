# Enable and encourage subagent usage where it adds value

- Source: July 2026 Codex usage analysis
- Stage: researched plan
- Severity: medium
- Change surface kinds: agent_capability, token_efficiency, workflow_quality
- User-visible: true
- Research completed: 2026-07-06

## Title

Enable and encourage subagent usage where it adds value

## Problem

July usage analysis found no subagent usage in authoritative local Codex sessions. The high-token runs were single-agent workflows that loaded broad source/config context into one retained transcript.

The key issue is capability exposure and configuration, not simply assignment strategy. If Claude or Gemini is run under a policy that does not expose its delegation/subagent tools, "assigning less work" or "checking earlier" only surfaces the issue sooner. It does not fix the underlying missing capability.

## User impact

Single agents do all exploration, implementation, verification, and log analysis in one context. This increases context size, makes reasoning noisier, and can force broad source reads into the parent transcript instead of receiving concise summaries.

Local token-monitoring artifacts excluding `_workspaces` showed strong evidence that parent-context pressure is real:

- `broad_source_config_read`: 45 signals, 47,639,913 input tokens, 823 calls
- `large_context_resend`: 10 signals, 19,113,086 input tokens, 154 large-context calls

Subagents should be evaluated as a way to keep broad exploration and log triage out of the parent transcript while preserving or improving implementation quality.

## Current configuration findings

`configs/policies.yaml` currently exposes no delegation/subagent tools:

- `safe.claude.allowed_tools`: `Read`, `Grep`, `Glob`
- `inspect.claude.allowed_tools`: `Read`, `Grep`, `Glob`, `Bash`
- `write.claude.allowed_tools`: `Read`, `Edit`, `Bash`, `Grep`, `Glob`
- `safe.gemini.allowed_tools`: `read_file`, `search_file_content`
- `inspect.gemini.allowed_tools`: `read_file`, `search_file_content`, `run_shell_command`
- `write.gemini.allowed_tools`: `read_file`, `search_file_content`, `write_file`, `replace`, `write_todos`, `run_shell_command`

The adapters pass those lists directly:

- `packages/agent_adapters/src/agent_adapters/claude_cli.py` passes `--allowedTools`
- `packages/agent_adapters/src/agent_adapters/gemini_cli.py` passes repeated `--allowed-tools`

`configs/agents.yaml` sets agent defaults but does not define delegation capabilities:

- Codex default model: `gpt-5.5`
- Claude default model: `sonnet`
- Gemini default model: `auto`

`configs/missions/builtin/implement_maintenance_backlog_ticket_v1.mission.md` gives a single-agent workflow and does not mention subagents or delegation.

`packages/token_monitoring` currently recognizes parallel tool use but does not classify subagent/delegation events or parent-summary effects.

## Implementation plan

### Ticket 1: Add delegation capability discovery

Before changing policy defaults, add a probe that records whether the installed agent CLI and current policy expose delegation/subagent tools.

The probe should capture:

- agent name and CLI version
- configured allowed tools
- delegation tool names detected from the installed CLI or adapter contract
- whether delegation is available under the selected policy
- evidence source and confidence

Acceptance criteria:

- No tool names are guessed in code without a local probe or documented adapter contract.
- Preflight artifacts record delegation availability for Codex, Claude, and Gemini.
- Tests cover available, unavailable, and unknown capability states.

### Ticket 2: Expose delegation tools through policy when confirmed

After tool names are confirmed for the installed versions, update `configs/policies.yaml` and adapter validation so appropriate policies can use delegation.

Rules:

- Write policy must still expose normal write tools. Delegation is additive, not a replacement for edit capability.
- Inspect policy may expose read-only delegation only if delegated agents cannot mutate the workspace.
- Safe policy remains read-only.
- If an agent uses different tool names across versions, the policy should fail loudly or mark delegation unavailable rather than silently falling back to no delegation.

Acceptance criteria:

- Claude and Gemini implementation runs with write policy are not accidentally read-only.
- Delegation-capable policies show the tool in the actual argv passed to the CLI.
- Shell/preflight diagnostics distinguish "policy does not expose delegation" from "CLI does not support delegation."

### Ticket 3: Add prompt guidance for appropriate delegation

Update implementation and review prompts so agents delegate only where it helps:

- broad read-only exploration of large files or cross-module contracts
- test failure triage and log summarization
- independent review of implementation risks
- narrow investigation of one module or workflow

Prompt guidance should require concise summaries back to the parent. It should not require delegation for small, obvious changes.

Acceptance criteria:

- Prompts mention when to use delegation and when not to.
- The guidance emphasizes keeping raw broad-source dumps out of the parent context.
- No prompt suggests that lack of delegation should be worked around by under-scoping the ticket.

### Ticket 4: Teach token monitoring to classify delegation behavior

Extend token monitoring and normalizers to detect:

- subagent/delegation tool invocations
- parent-context summaries returned by subagents
- raw broad-source output leaking back into the parent
- total tokens versus parent input tokens

Acceptance criteria:

- Reports can distinguish "total tokens increased but parent context decreased" from simple token waste.
- A run with no delegation remains explicitly classified as no-delegation, not unknown.
- Tests cover Codex, Claude, and Gemini event-shape examples where available.

### Ticket 5: Run representative A/B validation

Run comparable maintenance tickets with delegation disabled and enabled.

Compare:

- implementation quality and review findings
- parent input-token peak
- total input tokens
- broad source/config read signals
- large context resend signals
- verification behavior and elapsed time

Acceptance criteria:

- The project has evidence before making delegation more aggressive.
- If delegation increases total tokens, the PR evaluates whether it still improves quality or parent-context pressure.
- If delegation produces low-value summaries or noisy outputs, the next action is prompt/policy tightening based on evidence.

## Risks and guardrails

- Do not assume the exact Claude or Gemini tool name without checking the installed CLI behavior.
- Do not make a write-capable implementation policy read-only while adding delegation.
- Do not solve this by refusing to assign substantial tickets. The defect is missing capability exposure and lack of guidance.
- Do not expose mutation-capable subagents under read-only policies.
- Do not treat subagents as automatically token-saving. Measure total tokens, parent context, and quality together.

## Ready state

This ticket is ready to split into implementation tickets. Start with delegation capability discovery and artifacting, then expose confirmed tools through policy, then add prompt guidance and token-monitoring classification.
