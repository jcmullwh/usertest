# Large-file modularization extraction map

Date: 2026-07-06

This note freezes the behavior-preserving extraction boundaries for the
token-saving modularization work.  The first slice deliberately does not move
runtime behavior; it records the current large-file sizes, reserves importable
module names, and adds CLI help-contract tests so later PRs can move code behind
stable public command names.

## Baseline size report

The checked-in companion report is
[`large-file-size-baseline.json`](large-file-size-baseline.json).  It was
generated from the current tree with `wc -l -c` for the four files identified by
the token-usage analysis.

| File | Lines | Bytes |
| --- | ---: | ---: |
| `packages/runner_core/src/runner_core/runner.py` | 9445 | 393825 |
| `apps/usertest/src/usertest/cli.py` | 4272 | 167033 |
| `apps/usertest_backlog/src/usertest_backlog/cli.py` | 8926 | 331330 |
| `apps/usertest_implement/src/usertest_implement/cli.py` | 3781 | 144880 |

## Extraction map

### `runner_core.runner`

| Planned module | Initial ownership |
| --- | --- |
| `runner_core.stderr_diagnostics` | stderr sanitization, quota/capacity excerpts, failure diagnostics |
| `runner_core.prompt_staging` | prompt staging, workspace/run-dir path mapping, agent-visible paths |
| `runner_core.verification_prompts` | verification retry/follow-up prompt rendering |
| `runner_core.git_helpers` | git diff/status/numstat/user-config helpers |
| `runner_core.artifacts` | JSON writes, token-monitoring artifacts, tail-text helpers |
| `runner_core.preflight` | preflight command list construction and local command probing |
| `runner_core.shell_capability` | shell policy and shell capability resolution |
| `runner_core.python_capability` | Python command probing, runtime capability summaries, Windows remediation text |

`runner_core.runner.run_once` remains the orchestration entry point until the
leaf helpers and capability clusters have been extracted and covered by their
focused tests.

### `usertest` CLI

| Planned module | Initial ownership |
| --- | --- |
| `usertest.parser` | parser construction, public entry-point wiring |
| `usertest.commands.run` | `usertest run` command handling |
| `usertest.commands.batch` | batch validation and execution command handling |
| `usertest.commands.matrix` | matrix planning and execution commands |
| `usertest.commands.lint` | policy/catalog linting commands |
| `usertest.commands.reports` | report rendering plus `reports compile/analyze` commands |
| `usertest.commands.token_monitor` | token-monitoring analysis and batch-context commands |

### `usertest-backlog` CLI

| Planned module | Initial ownership |
| --- | --- |
| `usertest_backlog.parser` | parser construction, public entry-point wiring |
| `usertest_backlog.commands.reports` | report/window/backlog command handlers |
| `usertest_backlog.commands.export_tickets` | external ticket rendering and export dedupe |
| `usertest_backlog.commands.plan_cleanup` | plan-folder cleanup and staged-file maintenance |
| `usertest_backlog.commands.atom_actions` | atom action reconciliation commands |
| `usertest_backlog.commands.review_ux` | UX review commands |
| `usertest_backlog.workflows.problem_mining` | problem-mining stage |
| `usertest_backlog.workflows.prioritization` | prioritization stage |
| `usertest_backlog.workflows.reproduction_research` | reproduction-research stage |
| `usertest_backlog.workflows.solution_options` | solution-optioning stage |
| `usertest_backlog.workflows.solution_selection` | solution-selection stage |
| `usertest_backlog.workflows.implementation_planning` | implementation-planning stage |
| `usertest_backlog.workflows.staged` | staged workflow orchestration |

### `usertest-implement` CLI

| Planned module | Initial ownership |
| --- | --- |
| `usertest_implement.parser` | parser construction, public entry-point wiring |
| `usertest_implement.settings` | settings schema, loading, and application |
| `usertest_implement.ci` | CI polling and status helpers |
| `usertest_implement.review_context` | review body construction and PR context collection |
| `usertest_implement.commands.run` | selected-ticket implementation run command handlers |
| `usertest_implement.commands.review` | review run/status/merge command handlers |
| `usertest_implement.commands.tickets` | local ticket queue command handlers |
| `usertest_implement.commands.reports` | implementation report commands |
| `usertest_implement.commands.maintenance_images` | maintenance image list/cleanup commands |

## Contract tests for future extraction PRs

The modularization smoke tests import the planned modules without importing from
future implementation details.  CLI help-contract tests exercise public command
groups and assert stable command/option text:

- `apps/usertest/tests/test_modularization_contracts.py`
- `apps/usertest_backlog/tests/test_modularization_contracts.py`
- `apps/usertest_implement/tests/test_modularization_contracts.py`
- `packages/runner_core/tests/test_modularization_boundaries.py`

Future extraction PRs should keep these tests passing and add focused tests to
the destination modules as behavior moves out of the large files.
