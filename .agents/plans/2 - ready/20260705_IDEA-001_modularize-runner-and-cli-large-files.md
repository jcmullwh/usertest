# Modularize runner and CLI large files to reduce agent context load

- Source: July 2026 Codex usage analysis
- Stage: researched plan
- Severity: high
- Change surface kinds: maintainability, token_efficiency, developer_experience
- User-visible: true
- Research completed: 2026-07-06

## Title

Modularize runner and CLI large files to reduce agent context load

## Problem

Codex usage analysis showed that large contexts were primarily caused by broad source/config reads over a small number of very large implementation files. Local measurements on 2026-07-06 confirmed the same surfaces are still the dominant source-read risk:

- `packages/runner_core/src/runner_core/runner.py`: 8,717 lines, 393,825 bytes
- `apps/usertest/src/usertest/cli.py`: 3,861 lines, 167,033 bytes
- `apps/usertest_backlog/src/usertest_backlog/cli.py`: 8,028 lines, 331,330 bytes
- `apps/usertest_implement/src/usertest_implement/cli.py`: 3,405 lines, 144,880 bytes

Because shared contracts, preflight behavior, verification handoff, reporting, and CLI behavior are concentrated in these files, agents tend to read broad overlapping ranges instead of targeted modules.

Local `token_monitoring.json` artifacts excluding `_workspaces` showed:

- `broad_source_config_read`: 45 signals, 47,639,913 input tokens, 823 calls
- `large_context_resend`: 10 signals, 19,113,086 input tokens, 154 large-context calls
- `wait_poll_resend`: 17 signals, 3,320,332 input tokens, 57 wait/poll calls

The modularization work should be judged by reduced source/config retained-output pressure, not by cosmetic file splitting alone.

## User impact

Implementation runs consume many tokens before useful work begins. Large retained source chunks make later verification, debugging, and finalization calls expensive because the same context is resent repeatedly. Humans also pay the same cost when ownership boundaries are only discoverable by scanning thousands of lines.

## Current responsibility map

### `packages/runner_core/src/runner_core/runner.py`

The file already sits next to focused modules such as `execution_backend.py`, `verification_broker.py`, `python_runtime.py`, and `remote_effects.py`, but `runner.py` still owns many unrelated clusters:

- public request/result/config dataclasses
- agent model metadata and Codex metadata capture
- stderr sanitization and failure excerpt rendering
- preflight command construction and local command probing
- shell capability and policy resolution
- prompt staging and agent-visible file path mapping
- verification broker client command creation and follow-up prompt construction
- verification command rewriting, subprocess execution, and failure summaries
- Python toolchain/context capability probing
- git diff/status/numstat helpers
- token-monitoring artifact writes
- the large `run_once` orchestration path

### `apps/usertest/src/usertest/cli.py`

The main CLI combines parser construction with command handlers for:

- run and batch execution
- matrix planning/execution
- repo/root/path resolution
- policy/catalog linting
- run report rendering
- init/persona/mission commands
- reports compile/analyze
- token-monitoring analysis and batch-context commands

### `apps/usertest_backlog/src/usertest_backlog/cli.py`

The backlog CLI combines parser construction, report commands, UX review, export-ticket rendering, plan-folder cleanup, atom-action updates, and all staged backlog pipeline functions:

- problem mining
- prioritization
- reproduction research
- solution optioning
- solution selection
- implementation planning
- staged workflow orchestration

### `apps/usertest_implement/src/usertest_implement/cli.py`

This app has already started extracting modules (`batch_runner.py`, `tickets.py`, `git_ops.py`, `ledger.py`, `finalize.py`), but `cli.py` still owns:

- settings schema and application
- backlog refresh and ticket selection
- GitHub/PR helpers
- CI polling and review context collection
- review prompt construction and PR review submission
- selected-ticket run orchestration
- command handlers and parser construction

## Implementation plan

This should be split into several behavior-preserving implementation tickets. Do not try to flatten all four files in one PR.

### Ticket 1: Freeze contracts and produce an extraction map

- Add or update tests that capture public CLI help for the affected command groups.
- Add import-level smoke tests for the new module boundaries before moving behavior.
- Generate a small checked-in or artifacted size report for the four large files so later PRs can show before/after movement.
- Document the intended module map in a short design note or in the first PR body.

Acceptance criteria:

- Current behavior is unchanged.
- Future extraction PRs have a clear test set to run.
- The four measured file sizes are recorded from the current tree.

### Ticket 2: Extract low-risk `runner_core.runner` leaf helpers

Start with helpers that are called by `run_once` but do not control the whole orchestration:

- stderr sanitization and quota/capacity failure excerpt helpers
- prompt staging and agent-visible path mapping
- verification follow-up prompt rendering
- git diff/status/numstat/user-config helpers
- JSON and tail-text artifact helpers

Avoid moving `run_once` itself in this slice.

Acceptance criteria:

- No public runner APIs change.
- Existing runner tests pass.
- The moved helpers have direct imports from focused module names.

### Ticket 3: Extract shell/preflight and Python capability clusters

Move the shell and runtime capability clusters out of `runner.py` after the leaf-helper extraction stabilizes:

- preflight command list construction
- local command probing
- shell policy and shell capability resolution
- Python command probing and capability summaries
- Windows Python remediation formatting

Acceptance criteria:

- Shell capability contract tests continue to pass.
- Windows Python preflight behavior remains unchanged.
- The implementation does not reintroduce arbitrary short timeouts; any probe timeout must be an explicit probe budget, not an implementation-run timeout.

### Ticket 4: Split `apps/usertest/src/usertest/cli.py` by command group

Create command modules with a thin parser entry point:

- `commands/run.py`
- `commands/batch.py`
- `commands/matrix.py`
- `commands/lint.py`
- `commands/reports.py`
- `commands/token_monitor.py`
- `parser.py` or `cli_parser.py`

Acceptance criteria:

- Existing entry point and command names remain stable.
- CLI help text remains equivalent except for harmless ordering/formatting changes approved by tests.
- Shared helpers move to intentionally named modules instead of remaining in a grab-bag.

### Ticket 5: Split `apps/usertest_backlog/src/usertest_backlog/cli.py` by workflow ownership

Prioritize the backlog file because it is the second largest source-read surface:

- parser construction
- report commands
- export-ticket rendering and plan-folder cleanup
- atom-action/reconciliation commands
- UX review commands
- staged pipeline modules for each backlog stage

Acceptance criteria:

- Export, backlog report, sync, and staged-pipeline tests remain passing.
- Full plan index behavior remains separate from export dedupe behavior.
- Discard/deferred/actioned semantics remain unchanged.

### Ticket 6: Finish `apps/usertest_implement/src/usertest_implement/cli.py` extraction

Continue the existing modularization direction:

- settings loading/application
- CI wait helpers
- PR review body/context helpers
- review run/status/merge command handlers
- selected-ticket run command handlers
- parser construction

Acceptance criteria:

- Batch refresh, ticket discard/move, review, and merge tests remain passing.
- Review commands keep their remote-effect classifications.
- No local-only implementation path is introduced for ticket work that requires PR/review/merge.

## Measurement plan

Each implementation ticket should record:

- line and byte counts for changed large files before and after
- affected tests run
- any token-monitoring changes from representative runs, especially `broad_source_config_read` and `large_context_resend`
- whether agents can now inspect a focused module instead of reading the original large file

## Risks and guardrails

- Do not change CLI behavior as a side effect of moving parser code.
- Do not move code into modules with vague names like `utils.py` unless there is already a local pattern requiring it.
- Do not split so aggressively that related contracts become harder to read.
- Keep imports one-directional; command modules may import shared helpers, but shared helpers should not import command modules.
- Avoid broad opportunistic refactors. The goal is lower context load and clearer ownership, not style churn.

## Ready state

This ticket is ready to split into implementation tickets. The first implementation should be the contract-freeze and extraction-map ticket, followed by the low-risk runner helper extraction.
